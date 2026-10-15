import os
import re
import json
import asyncio
import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts
from gtts import gTTS
from pypdf import PdfReader
from docx import Document
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"

MAX_FILE_SIZE = 20 * 1024 * 1024
MAX_TEXT_LENGTH = 2_000_000
MAX_CHUNK_SIZE = 2500

DEFAULT_SETTINGS = {
    "engine": "edge",
    "voice": "hi-IN-MadhurNeural",
    "speed": 1.1,
    "pitch": 0,
}

ACTIVE_TASKS = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("speech_bot")


# =========================================================
# SETTINGS
# =========================================================
def load_settings():
    if not SETTINGS_FILE.exists():
        return {}
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.exception("Could not read settings")
        return {}


def save_settings(data):
    temp_path = SETTINGS_FILE.with_suffix(".tmp")
    temp_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(SETTINGS_FILE)


def get_settings(user_id):
    all_settings = load_settings()
    key = str(user_id)
    if key not in all_settings:
        all_settings[key] = DEFAULT_SETTINGS.copy()
        save_settings(all_settings)
    result = DEFAULT_SETTINGS.copy()
    result.update(all_settings[key])
    return result


def update_setting(user_id, name, value):
    all_settings = load_settings()
    key = str(user_id)
    if key not in all_settings:
        all_settings[key] = DEFAULT_SETTINGS.copy()
    all_settings[key][name] = value
    save_settings(all_settings)


# =========================================================
# MENUS
# =========================================================
def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 Text to Speech", "📄 Convert File"],
            ["🗣️ Select Voice", "⚙️ Settings"],
            ["⚡ Speech Rate", "🎚️ Pitch"],
            ["❌ Cancel Task", "ℹ️ Help"],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Microsoft Edge TTS", callback_data="engine_edge")],
        [InlineKeyboardButton("Google Translate TTS", callback_data="engine_google")],
        [InlineKeyboardButton("🗣️ Select Voice", callback_data="menu_voices")],
        [InlineKeyboardButton("⚡ Speech Rate", callback_data="menu_speed")],
        [InlineKeyboardButton("🎚️ Pitch", callback_data="menu_pitch")],
    ])


# =========================================================
# TEXT / DOCUMENT EXTRACTION
# =========================================================
def clean_text(text):
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text, limit=MAX_CHUNK_SIZE):
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = text.rfind(". ", 0, limit + 1)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        piece = text[:cut].strip()
        if piece:
            chunks.append(piece)
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def extract_txt(path):
    for encoding in ("utf-8-sig", "utf-8", "utf-16", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def extract_pdf(path):
    reader = PdfReader(str(path))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_docx(path):
    doc = Document(str(path))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n\n".join(parts)


def extract_epub(path):
    book = epub.read_epub(str(path))
    parts = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for node in soup(["script", "style", "nav"]):
            node.decompose()
        text = soup.get_text(separator="\n", strip=True)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def extract_document(path):
    ext = path.suffix.lower()
    if ext == ".txt":
        return extract_txt(path)
    if ext == ".pdf":
        return extract_pdf(path)
    if ext == ".docx":
        return extract_docx(path)
    if ext == ".epub":
        return extract_epub(path)
    raise ValueError("Unsupported file. Supported: TXT, EPUB, PDF, DOCX.")


# =========================================================
# RATE / PITCH
# =========================================================
VALID_RATES = [round(x / 10, 1) for x in range(11, 21)]
VALID_PITCHES = [-6, -3, 0, 3, 6]


def edge_rate(speed):
    speed = float(speed)
    if not 1.1 <= speed <= 2.0:
        raise ValueError("Rate must be between 1.1x and 2.0x.")
    return f"+{round((speed - 1.0) * 100)}%"


def edge_pitch(pitch):
    pitch = int(pitch)
    if pitch not in VALID_PITCHES:
        raise ValueError("Invalid pitch.")
    return f"{pitch:+d}Hz"


def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed on the runner.")


def run_ffmpeg(args):
    require_ffmpeg()
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"] + args,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-1200:] or "FFmpeg failed.")


def adjust_google_audio(source, destination, speed, pitch):
    speed = max(1.1, min(float(speed), 2.0))
    pitch = int(pitch)
    filters = []
    if pitch:
        ratio = 2 ** (pitch / 12)
        filters.extend([
            f"asetrate=44100*{ratio:.8f}",
            "aresample=44100",
            f"atempo={speed / ratio:.8f}",
        ])
    else:
        filters.append(f"atempo={speed:.8f}")
    run_ffmpeg([
        "-i", str(source), "-vn",
        "-af", ",".join(filters),
        "-codec:a", "libmp3lame", "-b:a", "96k",
        str(destination),
    ])


def merge_audio(files, output):
    if not files:
        raise RuntimeError("No audio chunks generated.")
    require_ffmpeg()
    list_file = output.parent / "concat_list.txt"
    with list_file.open("w", encoding="utf-8") as f:
        for item in files:
            safe_path = str(item.resolve()).replace("'", "'\\''")
            f.write(f"file '{safe_path}'\n")
    try:
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", str(list_file),
            "-vn", "-codec:a", "libmp3lame", "-b:a", "96k",
            str(output),
        ])
    finally:
        list_file.unlink(missing_ok=True)


# =========================================================
# TTS ENGINES
# =========================================================
async def generate_edge(text, output, settings):
    communicate = edge_tts.Communicate(
        text=text,
        voice=settings["voice"],
        rate=edge_rate(settings["speed"]),
        pitch=edge_pitch(settings["pitch"]),
    )
    await communicate.save(str(output))


def generate_google(text, output):
    # gTTS uses Google Translate TTS; it does not provide named Madhur voice.
    gTTS(text=text, lang="hi", slow=False).save(str(output))


async def generate_chunk(text, output, settings):
    if settings["engine"] == "edge":
        await generate_edge(text, output, settings)
    elif settings["engine"] == "google":
        await asyncio.to_thread(generate_google, text, output)
        adjusted = output.with_name(output.stem + "_adjusted.mp3")
        await asyncio.to_thread(
            adjust_google_audio,
            output, adjusted, settings["speed"], settings["pitch"],
        )
        output.unlink(missing_ok=True)
        adjusted.replace(output)
    else:
        raise ValueError("Unknown TTS engine.")
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("TTS engine produced an empty audio file.")


async def convert_text(text, settings, output, progress_callback=None):
    text = clean_text(text)
    if not text:
        raise ValueError("No readable text found.")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError("Text exceeds the 2,000,000 character limit.")

    chunks = split_text(text)
    with tempfile.TemporaryDirectory(prefix="tts_chunks_") as tmp:
        temp_dir = Path(tmp)
        generated = []
        total = len(chunks)

        for i, chunk in enumerate(chunks, start=1):
            if progress_callback:
                await progress_callback(i, total, "Generating speech")
            audio_path = temp_dir / f"chunk_{i:05d}.mp3"
            await generate_chunk(chunk, audio_path, settings)
            generated.append(audio_path)

        if progress_callback:
            await progress_callback(total, total, "Merging audio")
        await asyncio.to_thread(merge_audio, generated, output)

    return output


# =========================================================
# UI HELPERS
# =========================================================
async def show_voices(update, context, from_callback=False):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🇮🇳 Madhur — Hindi Male", callback_data="voice_madhur")],
        [InlineKeyboardButton("🇮🇳 Swara — Hindi Female", callback_data="voice_swara")],
        [InlineKeyboardButton("🇬🇧 Sonia — English Female", callback_data="voice_sonia")],
    ])
    if from_callback:
        await update.callback_query.edit_message_text("🗣️ Choose a voice:", reply_markup=keyboard)
    else:
        await update.effective_message.reply_text("🗣️ Choose a voice:", reply_markup=keyboard)


async def show_rates(update, context, from_callback=False):
    keyboard = []
    for i in range(0, len(VALID_RATES), 2):
        row = [
            InlineKeyboardButton(
                f"⚡ {rate:.1f}x",
                callback_data=f"speed_{rate:.1f}",
            )
            for rate in VALID_RATES[i:i + 2]
        ]
        keyboard.append(row)
    markup = InlineKeyboardMarkup(keyboard)
    text = "⚡ Choose speech rate (1.1x–2.0x):\nOr send /rate 1.5"
    if from_callback:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def show_pitch(update, context, from_callback=False):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("-6", callback_data="pitch_-6"),
            InlineKeyboardButton("-3", callback_data="pitch_-3"),
            InlineKeyboardButton("0", callback_data="pitch_0"),
        ],
        [
            InlineKeyboardButton("+3", callback_data="pitch_3"),
            InlineKeyboardButton("+6", callback_data="pitch_6"),
        ],
    ])
    if from_callback:
        await update.callback_query.edit_message_text("🎚️ Choose pitch:", reply_markup=keyboard)
    else:
        await update.effective_message.reply_text("🎚️ Choose pitch:", reply_markup=keyboard)


# =========================================================
# COMMANDS
# =========================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    settings = get_settings(user_id)
    await update.effective_message.reply_text(
        "🎙️ Welcome to Speech Bot!\n\n"
        "Send text or upload TXT, EPUB, PDF, or DOCX.\n\n"
        f"Engine: {settings['engine']}\n"
        f"Voice: {settings['voice']}\n"
        f"Rate: {settings['speed']:.1f}x\n"
        f"Pitch: {settings['pitch']}\n\n"
        "Commands:\n"
        "/rate - choose rate\n"
        "/rate 1.5 - set rate\n"
        "/voice - choose voice\n"
        "/settings - settings",
        reply_markup=main_keyboard(),
    )


async def help_command(update, context):
    await update.effective_message.reply_text(
        "📚 Help\n\n"
        "/start — show menu\n"
        "/rate — select rate\n"
        "/rate 1.1 through /rate 2.0 — set rate\n"
        "/voice — select voice\n"
        "/settings — settings\n"
        "/cancel — cancel conversion\n\n"
        "Supported files: TXT, EPUB, PDF, DOCX.",
        reply_markup=main_keyboard(),
    )


async def settings_command(update, context):
    await update.effective_message.reply_text(
        "⚙️ Settings",
        reply_markup=settings_keyboard(),
    )


async def rate_command(update, context):
    user_id = update.effective_user.id

    if not context.args:
        current = get_settings(user_id)["speed"]
        await update.effective_message.reply_text(
            f"Current rate: {current:.1f}x\n"
            "Choose a button below or send /rate 1.5.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        f"{rate:.1f}x",
                        callback_data=f"speed_{rate:.1f}",
                    )
                    for rate in VALID_RATES[i:i + 2]
                ]
                for i in range(0, len(VALID_RATES), 2)
            ]),
        )
        return

    try:
        value = float(context.args[0])
        value = round(value, 1)
        if value not in VALID_RATES:
            raise ValueError
    except (ValueError, TypeError):
        await update.effective_message.reply_text(
            "❌ Invalid rate.\nUse /rate 1.1 through /rate 2.0."
        )
        return

    update_setting(user_id, "speed", value)
    await update.effective_message.reply_text(
        f"✅ Speech rate set to {value:.1f}x.\n"
        "This rate will be used for your next conversion.",
        reply_markup=main_keyboard(),
    )


async def voice_command(update, context):
    await show_voices(update, context)


async def cancel_command(update, context):
    user_id = update.effective_user.id
    task = ACTIVE_TASKS.get(user_id)
    if task and not task.done():
        task.cancel()
        await update.effective_message.reply_text("⏹️ Cancellation requested.")
    else:
        await update.effective_message.reply_text("No active conversion.")


# =========================================================
# CALLBACKS
# =========================================================
async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "engine_edge":
        update_setting(user_id, "engine", "edge")
        await query.edit_message_text("✅ Microsoft Edge TTS selected.")
        return

    if data == "engine_google":
        update_setting(user_id, "engine", "google")
        await query.edit_message_text(
            "✅ Google Translate TTS selected.\n"
            "Note: Google Translate TTS has no named Madhur voice."
        )
        return

    if data == "menu_voices":
        await show_voices(update, context, from_callback=True)
        return

    if data == "menu_speed":
        await show_rates(update, context, from_callback=True)
        return

    if data == "menu_pitch":
        await show_pitch(update, context, from_callback=True)
        return

    voices = {
        "voice_madhur": ("hi-IN-MadhurNeural", "Madhur Hindi male"),
        "voice_swara": ("hi-IN-SwaraNeural", "Swara Hindi female"),
        "voice_sonia": ("en-GB-SoniaNeural", "Sonia English female"),
    }
    if data in voices:
        voice_id, label = voices[data]
        update_setting(user_id, "voice", voice_id)
        update_setting(user_id, "engine", "edge")
        await query.edit_message_text(f"✅ Selected voice: {label}")
        return

    if data.startswith("speed_"):
        try:
            rate = round(float(data.split("_", 1)[1]), 1)
            if rate not in VALID_RATES:
                raise ValueError
            update_setting(user_id, "speed", rate)
            await query.edit_message_text(f"✅ Speech rate set to {rate:.1f}x.")
        except (ValueError, TypeError):
            await query.edit_message_text("❌ Invalid speech rate.")
        return

    if data.startswith("pitch_"):
        try:
            pitch = int(data.split("_", 1)[1])
            if pitch not in VALID_PITCHES:
                raise ValueError
            update_setting(user_id, "pitch", pitch)
            await query.edit_message_text(f"✅ Pitch set to {pitch:+d}.")
        except (ValueError, TypeError):
            await query.edit_message_text("❌ Invalid pitch.")
        return


# =========================================================
# CONVERSION DELIVERY
# =========================================================
async def process_text(update, context, text, filename="audiobook"):
    user_id = update.effective_user.id
    active = ACTIVE_TASKS.get(user_id)
    if active and not active.done():
        await update.effective_message.reply_text(
            "⚠️ A conversion is already running. Send /cancel first."
        )
        return

    task = asyncio.current_task()
    ACTIVE_TASKS[user_id] = task

    try:
        settings = get_settings(user_id)
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", filename).strip("_")[:60]
        if not safe_name:
            safe_name = "audiobook"

        status = await update.effective_message.reply_text("⏳ Preparing audio...")

        with tempfile.TemporaryDirectory(prefix="tts_output_") as directory:
            output = Path(directory) / f"{safe_name}.mp3"

            async def progress(done, total, stage):
                try:
                    await status.edit_text(f"🎙️ {stage}\nProgress: {done}/{total}")
                except Exception:
                    pass

            await convert_text(text, settings, output, progress)
            await status.edit_text("📤 Uploading audio...")

            with output.open("rb") as audio:
                await update.effective_message.reply_document(
                    document=audio,
                    filename=f"{safe_name}.mp3",
                    caption=(
                        f"🎧 {safe_name}\n"
                        f"Engine: {settings['engine']}\n"
                        f"Voice: {settings['voice']}\n"
                        f"Rate: {settings['speed']:.1f}x"
                    ),
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                )

            try:
                await status.delete()
            except Exception:
                pass

    except asyncio.CancelledError:
        await update.effective_message.reply_text("⏹️ Conversion cancelled.")
        raise
    except Exception as exc:
        logger.exception("Conversion failed")
        await update.effective_message.reply_text(
            f"❌ Conversion failed:\n{str(exc)[:700]}"
        )
    finally:
        if ACTIVE_TASKS.get(user_id) is task:
            ACTIVE_TASKS.pop(user_id, None)


# =========================================================
# MESSAGE HANDLERS
# =========================================================
async def text_handler(update, context):
    message = update.effective_message
    text = (message.text or "").strip()
    if not text:
        return

    if text == "📝 Text to Speech":
        await message.reply_text("📝 Send the text you want to convert.")
        return
    if text == "📄 Convert File":
        await message.reply_text("📄 Upload a TXT, EPUB, PDF, or DOCX file.")
        return
    if text == "🗣️ Select Voice":
        await show_voices(update, context)
        return
    if text == "⚙️ Settings":
        await settings_command(update, context)
        return
    if text == "⚡ Speech Rate":
        await show_rates(update, context)
        return
    if text == "🎚️ Pitch":
        await show_pitch(update, context)
        return
    if text == "❌ Cancel Task":
        await cancel_command(update, context)
        return
    if text == "ℹ️ Help":
        await help_command(update, context)
        return

    await process_text(update, context, text, filename="text_to_speech")


async def document_handler(update, context):
    message = update.effective_message
    document = message.document
    if not document:
        return

    filename = document.file_name or "document.txt"
    ext = Path(filename).suffix.lower()

    if ext not in {".txt", ".epub", ".pdf", ".docx"}:
        await message.reply_text("❌ Supported formats: TXT, EPUB, PDF, DOCX.")
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.reply_text("❌ File is larger than 20 MB.")
        return

    status = await message.reply_text("📥 Downloading file...")
    try:
        with tempfile.TemporaryDirectory(prefix="tts_source_") as directory:
            source = Path(directory) / Path(filename).name
            telegram_file = await context.bot.get_file(document.file_id)
            await telegram_file.download_to_drive(custom_path=str(source))

            await status.edit_text("📖 Extracting text...")
            text = await asyncio.to_thread(extract_document, source)
            text = clean_text(text)

            if not text:
                await status.edit_text("❌ No readable text found.")
                return

            try:
                await status.delete()
            except Exception:
                pass

            await process_text(update, context, text, filename=Path(filename).stem)

    except Exception as exc:
        logger.exception("Document processing failed")
        try:
            await status.edit_text(f"❌ File processing failed:\n{str(exc)[:700]}")
        except Exception:
            pass


# =========================================================
# STARTUP
# =========================================================
async def post_init(application):
    logger.info("Bot initialized.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing from environment / GitHub Secrets.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    logger.info("Starting Telegram polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
