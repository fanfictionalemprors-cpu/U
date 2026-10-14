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

# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

LOCAL_API_URL = os.environ.get(
    "LOCAL_API_URL",
    "http://tgapi:8081",
).rstrip("/")

MODEL_DIR = Path(
    os.environ.get(
        "TTS_MODEL_DIR",
        "/app/indus-pocket-tts",
    )
)

MAX_CHARS = int(os.environ.get("MAX_CHARS", "450"))

WORK_DIR = Path("/tmp/audiobook_bot")
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("audiobook-bot")

# Only one model inference at a time.
executor = ThreadPoolExecutor(max_workers=1)

tts_model = None
tts_voice = None
model_lock = threading.Lock()

# Keep recently generated source WAV files for speed conversion.
latest_audio = {}

# Keep active user jobs from running simultaneously.
active_jobs = set()
active_jobs_lock = asyncio.Lock()


# =========================================================
# TTS MODEL
# =========================================================

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

        if str(MODEL_DIR) not in sys.path:
            sys.path.insert(0, str(MODEL_DIR))

        try:
            from speak import load
        except Exception as exc:
            raise RuntimeError(
                "Could not import load() from the model's speak.py. "
                f"Details: {exc}"
            ) from exc

        log.info("Loading Inaya TTS model...")

        try:
            tts_model, tts_voice = load(int8=True)
        except Exception as exc:
            raise RuntimeError(
                f"Could not load Inaya TTS model: {exc}"
            ) from exc

        log.info("Inaya TTS model loaded.")


def synthesize_wav(text, output_path):
    load_tts_model()

    if not text or not text.strip():
        raise ValueError("Cannot generate audio from empty text.")

    import sys

    if str(MODEL_DIR) not in sys.path:
        sys.path.insert(0, str(MODEL_DIR))

    from speak import synthesize

    output_path = Path(output_path)

    synthesize(
        tts_model,
        tts_voice,
        text.strip(),
        str(output_path),
    )

    if not output_path.exists():
        raise RuntimeError(
            "TTS did not create the output WAV file."
        )

    if output_path.stat().st_size < 1000:
        raise RuntimeError(
            "Generated WAV is empty or unexpectedly small."
        )

    return str(output_path)


# =========================================================
# TEXT UTILITIES
# =========================================================

def clean_text(text):
    text = text.replace("\ufeff", "")
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def split_text(text, max_chars=MAX_CHARS):
    """
    Split text near paragraph and sentence boundaries.
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
                if current
                else paragraph
            )

            if len(candidate) <= max_chars:
                current = candidate
            else:
                flush()
                current = paragraph

            continue

        sentences = re.split(
            r"(?<=[.!?।])\s+",
            paragraph,
        )

        for sentence in sentences:
            sentence = sentence.strip()

            if not sentence:
                continue

            if len(sentence) > max_chars:
                flush()

                words = sentence.split()
                piece = ""

                for word in words:
                    candidate = (
                        piece + " " + word
                        if piece
                        else word
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
                if current
                else sentence
            )

            if len(candidate) <= max_chars:
                current = candidate
            else:
                flush()
                current = sentence

    flush()

    return chunks


def format_time(seconds):
    seconds = max(0, int(seconds))

    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60

    if hours:
        return f"{hours}h {minutes}m {secs}s"

    if minutes:
        return f"{minutes}m {secs}s"

    return f"{secs}s"


# =========================================================
# AUDIO CONVERSION
# =========================================================

def convert_wav_to_mp3(
    wav_path,
    mp3_path,
    speed=1.0,
):
    """
    Convert WAV to mono 48 kbps MP3.
    """

    speed = max(1.0, min(float(speed), 2.0))

    wav_path = Path(wav_path)
    mp3_path = Path(mp3_path)

    if not wav_path.exists():
        raise FileNotFoundError(
            f"Source WAV not found: {wav_path}"
        )

    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(wav_path),
        "-vn",
        "-ac",
        "1",
        "-af",
        f"atempo={speed:.2f}",
        "-b:a",
        "48k",
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
            "FFmpeg failed: "
            + (result.stderr or "Unknown error")[-1500:]
        )

    if not mp3_path.exists():
        raise RuntimeError(
            "FFmpeg did not create the MP3 file."
        )

    if mp3_path.stat().st_size < 500:
        raise RuntimeError(
            "Generated MP3 is unexpectedly small."
        )

    return str(mp3_path)


def make_mp3(text, output_path, speed=1.0):
    output_path = Path(output_path)
    wav_path = output_path.with_suffix(".wav")

    synthesize_wav(text, wav_path)

    convert_wav_to_mp3(
        wav_path,
        output_path,
        speed,
    )

    return str(output_path)


def get_audio_duration(path):
    try:
        return float(sf.info(str(path)).duration)
    except Exception:
        return 0.0


# =========================================================
# TELEGRAM HELPERS
# =========================================================

async def safe_edit(message, text):
    try:
        await message.edit_text(
            text,
            disable_web_page_preview=True,
        )
    except Exception:
        pass


def get_speed_keyboard():
    row1 = [
        InlineKeyboardButton(
            f"{speed:.1f}x",
            callback_data=f"speed:{speed:.1f}",
        )
        for speed in (1.0, 1.1, 1.2, 1.3, 1.4, 1.5)
    ]

    row2 = [
        InlineKeyboardButton(
            f"{speed:.1f}x",
            callback_data=f"speed:{speed:.1f}",
        )
        for speed in (1.6, 1.7, 1.8, 1.9, 2.0)
    ]

    return InlineKeyboardMarkup([row1, row2])


async def send_audio_part(
    context,
    chat_id,
    audio_path,
    title,
    user_id,
    original_wav,
    speed,
):
    """
    Upload one completed MP3 part.
    """

    audio_path = Path(audio_path)
    original_wav = Path(original_wav)

    if not audio_path.exists():
        raise FileNotFoundError(
            f"MP3 does not exist: {audio_path}"
        )

    with audio_path.open("rb") as audio_file:
        sent = await context.bot.send_audio(
            chat_id=chat_id,
            audio=audio_file,
            title=title[:64],
            caption=(
                f"🎧 {title}\n"
                "🎙️ Inaya Hindi Female Voice\n"
                f"⚡ Generated speed: {speed:.1f}x"
            ),
            reply_markup=get_speed_keyboard(),
            read_timeout=300,
            write_timeout=300,
            connect_timeout=60,
            pool_timeout=60,
        )

    # Preserve WAV for later speed conversions.
    latest_audio[user_id] = {
        "wav_path": str(original_wav),
        "title": title,
        "chat_id": chat_id,
        "message_id": sent.message_id,
        "speed": speed,
    }

    return sent


# =========================================================
# COMMANDS
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "🎙️ Hindi Audiobook Bot\n\n"
        "Send a .txt file to create an audiobook.\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/help - Instructions\n"
        "/demo - Generate a voice demo\n"
        "/speed 1.5 - Set generation speed\n\n"
        "Features:\n"
        "• Inaya Hindi/Hinglish voice\n"
        "• TXT to MP3\n"
        "• 48 kbps mono audio\n"
        "• Completed parts sent immediately\n"
        "• Progress and ETA\n"
        "• Speed buttons from 1.0x to 2.0x"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "📚 How to use\n\n"
        "1. Send a TXT file.\n"
        "2. The bot splits the text into chunks.\n"
        "3. Each completed audio part is sent immediately.\n"
        "4. Use the speed buttons under an audio.\n\n"
        "Commands:\n"
        "/start\n"
        "/demo\n"
        "/speed 1.5"
    )


async def demo_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = await update.effective_message.reply_text(
        "🎙️ Generating Inaya voice demo..."
    )

    demo_text = (
        "नमस्ते! आपका स्वागत है। "
        "आज हम एक छोटी सी कहानी सुनेंगे। "
        "आप आराम से सुनिए।"
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="tts_demo_",
            dir=WORK_DIR,
        ) as tmp:
            output_path = Path(tmp) / "inaya_demo.mp3"

            loop = asyncio.get_running_loop()

            await loop.run_in_executor(
                executor,
                make_mp3,
                demo_text,
                output_path,
                1.0,
            )

            with output_path.open("rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio_file,
                    title="Inaya Hindi Female Voice Demo",
                    caption="🎙️ Inaya Hindi Female Voice Demo",
                )

        await safe_edit(message, "✅ Demo sent.")

    except Exception as exc:
        log.exception("Demo generation failed")

        await safe_edit(
            message,
            "❌ Demo failed.\n\n"
            + str(exc)[:2000],
        )


async def speed_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /speed 1.5\n"
            "Supported range: 1.0 to 2.0"
        )
        return

    try:
        speed = float(context.args[0])
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Enter a valid speed, e.g. /speed 1.5"
        )
        return

    if not 1.0 <= speed <= 2.0:
        await update.effective_message.reply_text(
            "❌ Speed must be between 1.0 and 2.0."
        )
        return

    context.user_data["speed"] = speed

    await update.effective_message.reply_text(
        f"⚡ Default generation speed set to {speed:.1f}x."
    )


# =========================================================
# SPEED BUTTON
# =========================================================

async def speed_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    try:
        speed = float(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text(
            "❌ Invalid speed."
        )
        return

    if not 1.0 <= speed <= 2.0:
        await query.message.reply_text(
            "❌ Speed must be between 1.0 and 2.0."
        )
        return

    info = latest_audio.get(user_id)

    if not info:
        await query.message.reply_text(
            "⚠️ No recent audio is available for conversion."
        )
        return

    wav_path = Path(info["wav_path"])

    if not wav_path.exists():
        await query.message.reply_text(
            "⚠️ The original WAV is no longer available. "
            "Send the TXT again to regenerate it."
        )
        return

    status = await query.message.reply_text(
        f"⚡ Converting audio to {speed:.1f}x..."
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="speed_",
            dir=WORK_DIR,
        ) as tmp:
            output_path = Path(tmp) / (
                f"audio_{speed:.1f}x.mp3"
            )

            loop = asyncio.get_running_loop()

            await loop.run_in_executor(
                executor,
                convert_wav_to_mp3,
                wav_path,
                output_path,
                speed,
            )

            with output_path.open("rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=query.message.chat_id,
                    audio=audio_file,
                    title=(
                        f"{info['title']} - {speed:.1f}x"
                    ),
                    caption=f"⚡ Playback speed: {speed:.1f}x",
                    reply_markup=get_speed_keyboard(),
                )

        await safe_edit(
            status,
            f"✅ Audio sent at {speed:.1f}x.",
        )

    except Exception as exc:
        log.exception("Speed conversion failed")

        await safe_edit(
            status,
            "❌ Speed conversion failed.\n\n"
            + str(exc)[:2000],
        )


# =========================================================
# TXT HANDLER
# =========================================================

async def txt_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    document = message.document

    if document is None:
        return

    filename = document.file_name or "novel.txt"

    if not filename.lower().endswith(".txt"):
        await message.reply_text(
            "❌ Please send a TXT file."
        )
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    async with active_jobs_lock:
        if user_id in active_jobs:
            await message.reply_text(
                "⏳ Your previous audiobook job is still running. "
                "Please wait until it finishes."
            )
            return

        active_jobs.add(user_id)

    started_at = time.monotonic()

    status = await message.reply_text(
        "📥 Downloading TXT file..."
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="audiobook_",
            dir=WORK_DIR,
        ) as tmp:
            tmpdir = Path(tmp)

            safe_name = re.sub(
                r"[^A-Za-z0-9._-]+",
                "_",
                Path(filename).name,
            )

            input_path = tmpdir / safe_name

            telegram_file = await context.bot.get_file(
                document.file_id
            )

            await telegram_file.download_to_drive(
                custom_path=str(input_path)
            )

            raw = input_path.read_bytes()

            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                try:
                    text = raw.decode("utf-16")
                except UnicodeDecodeError:
                    text = raw.decode(
                        "gb18030",
                        errors="replace",
                    )

            text = clean_text(text)

            if not text:
                await safe_edit(
                    status,
                    "❌ The TXT file is empty or unreadable."
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

            speed = float(
                context.user_data.get("speed", 1.0)
            )

            await safe_edit(
                status,
                "📚 Audiobook generation started.\n\n"
                f"📄 File: {filename}\n"
                f"🧩 Text chunks: {total}\n"
                f"📝 Characters: {len(text):,}\n"
                f"⚡ Speed: {speed:.1f}x\n\n"
                "📤 Each part will be sent when completed."
            )

            completed = 0
            failed = 0
            total_audio_seconds = 0.0

            loop = asyncio.get_running_loop()

            for index, chunk in enumerate(
                chunks,
                start=1,
            ):
                part_started = time.monotonic()

                wav_path = tmpdir / f"part_{index:05d}.wav"
                mp3_path = tmpdir / f"part_{index:05d}.mp3"

                elapsed = time.monotonic() - started_at

                if index > 1:
                    average = elapsed / (index - 1)
                    eta = average * (total - index + 1)
                else:
                    eta = 0

                await safe_edit(
                    status,
                    "🎙️ Generating audiobook...\n\n"
                    f"📄 {filename}\n"
                    f"📊 Progress: {index - 1}/{total}\n"
                    f"✅ Sent: {completed}\n"
                    f"❌ Failed: {failed}\n"
                    f"⏱️ Elapsed: {format_time(elapsed)}\n"
                    f"⌛ Estimated remaining: {format_time(eta)}"
                )

                try:
                    # Use a valid action for python-telegram-bot 21.x.
                    # UPLOAD_DOCUMENT is available in this version.
                    try:
                        await context.bot.send_chat_action(
                            chat_id=chat_id,
                            action=ChatAction.UPLOAD_DOCUMENT,
                        )
                    except Exception:
                        # Chat action is only cosmetic; continue processing.
                        log.warning(
                            "Could not send chat action.",
                            exc_info=True,
                        )

                    # Generate WAV.
                    await loop.run_in_executor(
                        executor,
                        synthesize_wav,
                        chunk,
                        wav_path,
                    )

                    # Convert to MP3.
                    await loop.run_in_executor(
                        executor,
                        convert_wav_to_mp3,
                        wav_path,
                        mp3_path,
                        speed,
                    )

                    audio_duration = get_audio_duration(
                        wav_path
                    )

                    # Upload immediately after generation.
                    await send_audio_part(
                        context=context,
                        chat_id=chat_id,
                        audio_path=mp3_path,
                        title=(
                            f"{Path(filename).stem} "
                            f"- Part {index} of {total}"
                        ),
                        user_id=user_id,
                        original_wav=wav_path,
                        speed=speed,
                    )

                    completed += 1
                    total_audio_seconds += audio_duration

                    log.info(
                        "Completed part %s/%s in %.1fs",
                        index,
                        total,
                        time.monotonic() - part_started,
                    )

                except Exception as exc:
                    failed += 1

                    log.exception(
                        "Part %s/%s failed",
                        index,
                        total,
                    )

                    # Report the exact failing stage.
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ Part {index}/{total} failed.\n"
                            f"Error: {str(exc)[:1500]}"
                        ),
                    )

            elapsed = time.monotonic() - started_at

            await safe_edit(
                status,
                "🏁 Audiobook processing finished.\n\n"
                f"📄 File: {filename}\n"
                f"🧩 Total chunks: {total}\n"
                f"✅ Sent successfully: {completed}\n"
                f"❌ Failed: {failed}\n"
                f"🎧 Generated audio duration: "
                f"{format_time(total_audio_seconds)}\n"
                f"⏱️ Total processing time: {format_time(elapsed)}"
            )

    except Exception as exc:
        log.exception("TXT processing failed")

        await safe_edit(
            status,
            "❌ TXT processing failed.\n\n"
            + str(exc)[:2500],
        )

    finally:
        async with active_jobs_lock:
            active_jobs.discard(user_id)


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    log.error(
        "Unhandled Telegram exception",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    log.info("Starting audiobook bot...")
    log.info("Local Bot API: %s", LOCAL_API_URL)
    log.info("TTS model directory: %s", MODEL_DIR)

    base_url = f"{LOCAL_API_URL}/bot"
    base_file_url = f"{LOCAL_API_URL}/file/bot"

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
            pattern=r"^speed:",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            txt_handler,
        )
    )

    application.add_error_handler(error_handler)

    log.info("Loading model before starting polling...")

    # Fail early if the model cannot load.
    load_tts_model()

    log.info("Bot is ready.")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
