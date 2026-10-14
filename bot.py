import os
import re
import time
import asyncio
import logging
import tempfile
import subprocess
import threading

from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import soundfile as sf

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
LOCAL_API_URL = os.environ.get(
    "LOCAL_API_URL", "http://tgapi:8081"
).rstrip("/")

MODEL_DIR = Path(
    os.environ.get("TTS_MODEL_DIR", "/app/indus-pocket-tts")
)

MAX_CHARS = int(os.environ.get("MAX_CHARS", "450"))

PART_MINUTES = int(os.environ.get("PART_MINUTES", "30"))

WORK_DIR = Path("/tmp/audiobook_bot")
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("audiobook-bot")

# Keep model inference off the asyncio event loop.
executor = ThreadPoolExecutor(max_workers=1)

tts_model = None
tts_voice = None

model_lock = threading.Lock()

# Latest generated WAV file for each Telegram user.
latest_audio = {}

# --------------------------------------------------
# MODEL LOADING
# --------------------------------------------------

def load_tts_model():
    global tts_model, tts_voice

    if tts_model is not None:
        return

    with model_lock:
        if tts_model is not None:
            return

        if not MODEL_DIR.exists():
            raise RuntimeError(
                f"TTS model directory not found: {MODEL_DIR}"
            )

        import sys

        sys.path.insert(0, str(MODEL_DIR))

        from speak import load

        log.info("Loading Inaya Hindi TTS model...")

        # The model card documents load(int8=True).
        tts_model, tts_voice = load(int8=True)

        log.info("Inaya TTS model loaded successfully.")


def synthesize_wav(text, output_path):
    """
    Generate speech using the public Inaya model.
    """
    load_tts_model()

    from speak import synthesize

    synthesize(
        tts_model,
        tts_voice,
        text,
        str(output_path),
    )

    if not Path(output_path).exists():
        raise RuntimeError("TTS did not create the WAV file.")

    if Path(output_path).stat().st_size < 1000:
        raise RuntimeError("Generated WAV file is unexpectedly small.")

    return str(output_path)


# --------------------------------------------------
# TEXT PROCESSING
# --------------------------------------------------

def clean_text(text):
    text = text.replace("\ufeff", "")
    text = text.replace("\x00", " ")

    # Normalize line endings.
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Remove excessive blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove repeated spaces.
    text = re.sub(r"[ \t]+", " ", text)

    return text.strip()


def split_text(text, max_chars=MAX_CHARS):
    """
    Split text without intentionally cutting words.
    Prefer paragraph/sentence boundaries.
    """
    text = clean_text(text)

    if not text:
        return []

    paragraphs = re.split(r"\n+", text)
    chunks = []
    current = ""

    def flush():
        nonlocal current

        if current.strip():
            chunks.append(current.strip())

        current = ""

    for paragraph in paragraphs:
        paragraph = paragraph.strip()

        if not paragraph:
            continue

        if len(paragraph) <= max_chars:
            candidate = (
                current + "\n\n" + paragraph
                if current else paragraph
            )

            if len(candidate) <= max_chars:
                current = candidate
            else:
                flush()
                current = paragraph

            continue

        # Split oversized paragraphs by sentence.
        sentences = re.split(
            r"(?<=[.!?।])\s+",
            paragraph
        )

        for sentence in sentences:
            sentence = sentence.strip()

            if not sentence:
                continue

            # Extremely long sentence: split by words.
            if len(sentence) > max_chars:
                flush()

                words = sentence.split()
                piece = ""

                for word in words:
                    candidate = (
                        piece + " " + word
                        if piece else word
                    )

                    if len(candidate) <= max_chars:
                        piece = candidate
                    else:
                        if piece:
                            chunks.append(piece.strip())

                        piece = word

                if piece:
                    current = piece

                continue

            candidate = (
                current + " " + sentence
                if current else sentence
            )

            if len(candidate) <= max_chars:
                current = candidate
            else:
                flush()
                current = sentence

    flush()

    return chunks


# --------------------------------------------------
# AUDIO CONVERSION
# --------------------------------------------------

def convert_wav_to_mp3(wav_path, mp3_path, speed=1.0):
    """
    Convert generated WAV to mono 48 kbps MP3.
    Apply playback speed using FFmpeg.
    """

    speed = max(1.0, min(float(speed), 2.0))

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(wav_path),
        "-vn",
        "-ac", "1",
        "-ar", "24000",
        "-af", f"atempo={speed:.2f}",
        "-b:a", "48k",
        str(mp3_path),
    ]

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=600,
    )

    if result.returncode != 0:
        raise RuntimeError(
            "FFmpeg conversion failed: "
            + result.stderr[-1500:]
        )

    if not Path(mp3_path).exists():
        raise RuntimeError("MP3 file was not created.")

    return str(mp3_path)


def make_mp3(text, destination, speed=1.0):
    """
    Generate WAV, then convert it to MP3.
    """

    destination = Path(destination)
    wav_path = destination.with_suffix(".wav")

    synthesize_wav(text, wav_path)

    convert_wav_to_mp3(
        wav_path,
        destination,
        speed=speed,
    )

    return str(destination)


def get_audio_duration(path):
    try:
        info = sf.info(str(path))
        return float(info.duration)
    except Exception:
        return 0.0


def format_time(seconds):
    seconds = max(0, int(seconds))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours}h {minutes}m {secs}s"

    return f"{minutes}m {secs}s"


# --------------------------------------------------
# TELEGRAM HELPERS
# --------------------------------------------------

async def status_message(update, text):
    if update.message:
        return await update.message.reply_text(text)

    return None


async def safe_edit(message, text):
    try:
        await message.edit_text(text)
    except Exception:
        pass


async def send_audio_file(
    context,
    chat_id,
    path,
    title,
    part_number=None,
    total_parts=None,
    user_id=None,
):
    """
    Send the completed audio part immediately.
    """

    keyboard = []

    speed_row_1 = [
        InlineKeyboardButton(
            "1.0x",
            callback_data="speed:1.0"
        ),
        InlineKeyboardButton(
            "1.1x",
            callback_data="speed:1.1"
        ),
        InlineKeyboardButton(
            "1.2x",
            callback_data="speed:1.2"
        ),
        InlineKeyboardButton(
            "1.3x",
            callback_data="speed:1.3"
        ),
        InlineKeyboardButton(
            "1.4x",
            callback_data="speed:1.4"
        ),
        InlineKeyboardButton(
            "1.5x",
            callback_data="speed:1.5"
        ),
    ]

    speed_row_2 = [
        InlineKeyboardButton(
            "1.6x",
            callback_data="speed:1.6"
        ),
        InlineKeyboardButton(
            "1.7x",
            callback_data="speed:1.7"
        ),
        InlineKeyboardButton(
            "1.8x",
            callback_data="speed:1.8"
        ),
        InlineKeyboardButton(
            "1.9x",
            callback_data="speed:1.9"
        ),
        InlineKeyboardButton(
            "2.0x",
            callback_data="speed:2.0"
        ),
    ]

    keyboard = InlineKeyboardMarkup(
        [speed_row_1, speed_row_2]
    )

    with open(path, "rb") as audio_file:
        sent = await context.bot.send_audio(
            chat_id=chat_id,
            audio=audio_file,
            title=title[:64],
            caption=(
                f"🎧 {title}\n"
                "🎙️ Voice: Inaya Hindi Female\n"
                "⚡ Speed buttons: 1.0x–2.0x"
            ),
            reply_markup=keyboard,
            read_timeout=300,
            write_timeout=300,
            connect_timeout=60,
        )

    if user_id is not None:
        latest_audio[user_id] = {
            "path": str(path),
            "text": None,
            "title": title,
            "chat_id": chat_id,
            "message_id": sent.message_id,
        }

    return sent


# --------------------------------------------------
# COMMANDS
# --------------------------------------------------

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    text = (
        "🎙️ Hindi Audiobook Bot\n\n"
        "Send me a .txt file to create an audiobook.\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/demo - Listen to Inaya voice demo\n"
        "/help - Help\n"
        "/speed 1.5 - Set requested speed\n\n"
        "Features:\n"
        "• Hindi female voice: Inaya\n"
        "• TXT to MP3\n"
        "• Audio parts sent as they finish\n"
        "• Mono MP3, 48 kbps\n"
        "• Speed buttons from 1.0x to 2.0x\n\n"
        "Note: The model works best with conversational Hindi."
    )

    await update.message.reply_text(text)


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.message.reply_text(
        "📚 How to use:\n\n"
        "1. Send a TXT file.\n"
        "2. Wait while the bot creates audio parts.\n"
        "3. Each part is sent when ready.\n"
        "4. Use the speed buttons below each audio.\n\n"
        "Use /demo to hear a sample.\n"
        "Use /speed 1.5 to request a speed value."
    )


async def demo_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    msg = await update.message.reply_text(
        "🎙️ Generating Inaya voice demo..."
    )

    text = (
        "नमस्ते! आपका स्वागत है। "
        "आज हम एक छोटी सी कहानी सुनेंगे। "
        "आप आराम से सुनिए और अपनी पसंद की playback speed चुनिए।"
    )

    try:
        loop = asyncio.get_running_loop()

        with tempfile.TemporaryDirectory(
            prefix="tts_demo_",
            dir=WORK_DIR,
        ) as tmp:
            path = Path(tmp) / "inaya_demo.mp3"

            await loop.run_in_executor(
                executor,
                make_mp3,
                text,
                path,
                1.0,
            )

            with open(path, "rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio_file,
                    title="Inaya Hindi Female Voice Demo",
                    caption="🎙️ Inaya Hindi Female Voice Demo",
                )

        await safe_edit(msg, "✅ Demo sent.")

    except Exception as exc:
        log.exception("Demo failed")
        await safe_edit(
            msg,
            "❌ Demo failed:\n" + str(exc)[:2500]
        )


async def speed_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.message.reply_text(
            "Example: /speed 1.5\n"
            "Supported speed: 1.0 to 2.0"
        )
        return

    try:
        speed = float(context.args[0])
    except ValueError:
        await update.message.reply_text(
            "❌ Enter a valid number, e.g. /speed 1.5"
        )
        return

    if speed < 1.0 or speed > 2.0:
        await update.message.reply_text(
            "❌ Speed must be between 1.0 and 2.0."
        )
        return

    context.user_data["speed"] = speed

    await update.message.reply_text(
        f"⚡ Requested speed: {speed:.1f}x\n\n"
        "This setting is used when generating new audio. "
        "Use the buttons under an audio to create a faster "
        "or slower copy of that part."
    )


# --------------------------------------------------
# SPEED BUTTON CALLBACK
# --------------------------------------------------

async def speed_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    try:
        speed = float(query.data.split(":", 1)[1])
    except Exception:
        await query.message.reply_text("❌ Invalid speed.")
        return

    if speed < 1.0 or speed > 2.0:
        await query.message.reply_text("❌ Invalid speed.")
        return

    info = latest_audio.get(user_id)

    if not info:
        await query.message.reply_text(
            "❌ No recent audio is available for speed conversion."
        )
        return

    source_path = Path(info["path"])

    if not source_path.exists():
        await query.message.reply_text(
            "⚠️ The original audio is no longer available. "
            "Please send the TXT again."
        )
        return

    status = await query.message.reply_text(
        f"⚡ Creating audio at {speed:.1f}x..."
    )

    try:
        loop = asyncio.get_running_loop()

        with tempfile.TemporaryDirectory(
            prefix="speed_",
            dir=WORK_DIR,
        ) as tmp:
            output_path = Path(tmp) / (
                f"audio_{speed:.1f}x.mp3"
            )

            await loop.run_in_executor(
                executor,
                convert_wav_to_mp3,
                source_path,
                output_path,
                speed,
            )

            with open(output_path, "rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=audio_file,
                    title=(
                        f"{info['title']} - {speed:.1f}x"
                    ),
                    caption=f"⚡ Playback speed: {speed:.1f}x",
                )

        await safe_edit(
            status,
            f"✅ Audio sent at {speed:.1f}x."
        )

    except Exception as exc:
        log.exception("Speed conversion failed")
        await safe_edit(
            status,
            "❌ Speed conversion failed:\n"
            + str(exc)[:2000]
        )


# --------------------------------------------------
# TXT FILE PROCESSING
# --------------------------------------------------

async def txt_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message
    document = message.document

    if not document:
        return

    filename = document.file_name or "novel.txt"

    if not filename.lower().endswith(".txt"):
        await message.reply_text(
            "❌ Please send a .txt file."
        )
        return

    if document.file_size and document.file_size > 100 * 1024 * 1024:
        await message.reply_text(
            "❌ File is larger than the 100 MB bot limit."
        )
        return

    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        Path(filename).name,
    )

    status = await message.reply_text(
        "📥 Downloading TXT file..."
    )

    start_time = time.monotonic()

    try:
        with tempfile.TemporaryDirectory(
            prefix="audiobook_",
            dir=WORK_DIR,
        ) as tmp:
            tmpdir = Path(tmp)

            input_path = tmpdir / safe_name

            tg_file = await context.bot.get_file(
                document.file_id
            )

            await tg_file.download_to_drive(
                custom_path=str(input_path)
            )

            # Try UTF-8 first, then common alternatives.
            raw = input_path.read_bytes()

            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("utf-16")
                except UnicodeDecodeError:
                    text = raw.decode(
                        "latin-1",
                        errors="replace"
                    )

            text = clean_text(text)

            if not text:
                await safe_edit(
                    status,
                    "❌ The TXT file is empty."
                )
                return

            chunks = split_text(text)

            if not chunks:
                await safe_edit(
                    status,
                    "❌ No readable text was found."
                )
                return

            total = len(chunks)

            await safe_edit(
                status,
                "📚 Audiobook processing started.\n\n"
                f"📄 File: {filename}\n"
                f"🧩 Audio parts: {total}\n"
                f"📝 Text size: {len(text):,} characters\n"
                "🎙️ Voice: Inaya Hindi Female\n\n"
                "Each completed part will be sent immediately."
            )

            context.user_data["speed"] = float(
                context.user_data.get("speed", 1.0)
            )

            generated = 0
            failed = 0
            total_audio_seconds = 0.0

            loop = asyncio.get_running_loop()

            for index, chunk in enumerate(chunks, start=1):
                part_start = time.monotonic()

                audio_path = tmpdir / f"part_{index:04d}.wav"
                mp3_path = tmpdir / f"part_{index:04d}.mp3"

                elapsed = time.monotonic() - start_time

                remaining = total - index + 1

                eta = (
                    (elapsed / max(1, index - 1)) * remaining
                    if index > 1
                    else 0
                )

                await safe_edit(
                    status,
                    "🎙️ Generating audiobook...\n\n"
                    f"📚 File: {filename}\n"
                    f"📊 Progress: {index - 1}/{total}\n"
                    f"✅ Completed: {generated}\n"
                    f"❌ Failed: {failed}\n"
                    f"⏱️ Elapsed: {format_time(elapsed)}\n"
                    f"⌛ Estimated remaining: {format_time(eta)}"
                )

                try:
                    await context.bot.send_chat_action(
                        chat_id=chat_id,
                        action=ChatAction.UPLOAD_AUDIO,
                    )

                    await loop.run_in_executor(
                        executor,
                        synthesize_wav,
                        chunk,
                        audio_path,
                    )

                    await loop.run_in_executor(
                        executor,
                        convert_wav_to_mp3,
                        audio_path,
                        mp3_path,
                        context.user_data["speed"],
                    )

                    total_audio_seconds += get_audio_duration(
                        audio_path
                    )

                    await send_audio_file(
                        context=context,
                        chat_id=chat_id,
                        path=mp3_path,
                        title=(
                            f"{Path(filename).stem} "
                            f"- Part {index} of {total}"
                        ),
                        part_number=index,
                        total_parts=total,
                        user_id=user_id,
                    )

                    generated += 1

                    log.info(
                        "Part %s/%s completed in %.2fs",
                        index,
                        total,
                        time.monotonic() - part_start,
                    )

                except Exception as exc:
                    failed += 1

                    log.exception(
                        "Failed generating part %s",
                        index,
                    )

                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ Part {index}/{total} failed.\n"
                            f"Reason: {str(exc)[:1000]}"
                        ),
                    )

            elapsed = time.monotonic() - start_time

            await safe_edit(
                status,
                "🏁 Audiobook processing finished.\n\n"
                f"📄 File: {filename}\n"
                f"✅ Parts sent: {generated}\n"
                f"❌ Parts failed: {failed}\n"
                f"🧩 Total parts: {total}\n"
                f"⏱️ Processing time: {format_time(elapsed)}\n"
                f"🎧 Generated audio duration: "
                f"{format_time(total_audio_seconds)}"
            )

    except Exception as exc:
        log.exception("TXT processing failed")

        await safe_edit(
            status,
            "❌ TXT processing failed:\n\n"
            + str(exc)[:2500]
        )


# --------------------------------------------------
# ERROR HANDLER
# --------------------------------------------------

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    log.error(
        "Unhandled Telegram error",
        exc_info=context.error,
    )


# --------------------------------------------------
# MAIN
# --------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    log.info("Starting Telegram audiobook bot...")
    log.info("Local Telegram API: %s", LOCAL_API_URL)

    # Local Bot API endpoint.
    base_url = LOCAL_API_URL + "/bot"
    base_file_url = LOCAL_API_URL + "/file/bot"

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .base_url(base_url)
        .base_file_url(base_file_url)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("demo", demo_command)
    )

    application.add_handler(
        CommandHandler("speed", speed_command)
    )

    application.add_handler(
        CallbackQueryHandler(
            speed_callback,
            pattern=r"^speed:"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            txt_handler
        )
    )

    application.add_error_handler(error_handler)

    # Load the model before polling so startup failures are visible.
    load_tts_model()

    log.info("Bot is ready.")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
