import os
import re
import json
import asyncio
import logging
import tempfile
import shutil
import subprocess

from pathlib import Path

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

import edge_tts
from gtts import gTTS
from pypdf import PdfReader
from docx import Document
from ebooklib import epub
from bs4 import BeautifulSoup


# =========================================================
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

DATA_DIR = Path("bot_data")
DATA_DIR.mkdir(exist_ok=True)

SETTINGS_FILE = DATA_DIR / "settings.json"

DEFAULT_VOICE = "hi-IN-MadhurNeural"

DEFAULT_SETTINGS = {
    "engine": "edge",
    "voice": DEFAULT_VOICE,
    "speed": 1.0,
    "pitch": 0,
    "chapter_mode": False,
}

MAX_TEXT_LENGTH = 500_000
MAX_CHUNK_SIZE = 3000

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# Active jobs, keyed by Telegram user ID.
ACTIVE_TASKS = {}


# =========================================================
# SETTINGS
# =========================================================

def load_settings():
    if not SETTINGS_FILE.exists():
        return {}

    try:
        return json.loads(
            SETTINGS_FILE.read_text(encoding="utf-8")
        )
    except (json.JSONDecodeError, OSError):
        logger.exception("Could not load settings")
        return {}


def save_settings(settings):
    SETTINGS_FILE.write_text(
        json.dumps(
            settings,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def get_settings(user_id):
    settings = load_settings()
    key = str(user_id)

    if key not in settings:
        settings[key] = DEFAULT_SETTINGS.copy()
        save_settings(settings)

    return settings[key]


def update_setting(user_id, name, value):
    settings = load_settings()
    key = str(user_id)

    if key not in settings:
        settings[key] = DEFAULT_SETTINGS.copy()

    settings[key][name] = value
    save_settings(settings)


# =========================================================
# KEYBOARDS
# =========================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📝 Text to Speech", "📄 Convert File"],
            ["🗣️ Select Voice", "⚙️ Settings"],
            ["🎚️ Speech Speed", "🎛️ Pitch"],
            ["📚 Chapter Mode", "❌ Cancel Task"],
            ["ℹ️ Help"],
        ],
        resize_keyboard=True,
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "Microsoft Edge TTS",
                callback_data="engine_edge",
            ),
        ],
        [
            InlineKeyboardButton(
                "Google Translate TTS",
                callback_data="engine_google",
            ),
        ],
        [
            InlineKeyboardButton(
                "Select Hindi Voice",
                callback_data="voices",
            ),
        ],
        [
            InlineKeyboardButton(
                "Speech Speed",
                callback_data="speed_menu",
            ),
        ],
        [
            InlineKeyboardButton(
                "Pitch",
                callback_data="pitch_menu",
            ),
        ],
        [
            InlineKeyboardButton(
                "Chapter Mode ON/OFF",
                callback_data="chapter_toggle",
            ),
        ],
    ])


# =========================================================
# TEXT PROCESSING
# =========================================================

def clean_text(text):
    text = text.replace("\x00", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def split_text(text, limit=MAX_CHUNK_SIZE):
    """
    Split text without losing content.
    Prefer paragraph and sentence boundaries.
    """

    chunks = []

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)

        if cut < limit // 2:
            cut = text.rfind(". ", 0, limit)

        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)

        if cut <= 0:
            cut = limit

        chunk = text[:cut].strip()

        if chunk:
            chunks.append(chunk)

        text = text[cut:].strip()

    if text:
        chunks.append(text)

    return chunks


def extract_txt(path):
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue

    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def extract_pdf(path):
    reader = PdfReader(str(path))
    pages = []

    for page in reader.pages:
        pages.append(page.extract_text() or "")

    return "\n\n".join(pages)


def extract_docx(path):
    document = Document(str(path))
    paragraphs = [
        paragraph.text
        for paragraph in document.paragraphs
        if paragraph.text.strip()
    ]

    return "\n\n".join(paragraphs)


def extract_epub(path):
    book = epub.read_epub(str(path))
    sections = []

    for item in book.get_items():
        if item.get_type() != 9:
            continue

        soup = BeautifulSoup(
            item.get_content(),
            "html.parser",
        )

        for element in soup([
            "script",
            "style",
            "nav",
        ]):
            element.decompose()

        text = soup.get_text(
            separator="\n",
            strip=True,
        )

        if text:
            sections.append(text)

    return "\n\n".join(sections)


def extract_file(path):
    extension = path.suffix.lower()

    if extension == ".txt":
        return extract_txt(path)

    if extension == ".pdf":
        return extract_pdf(path)

    if extension == ".docx":
        return extract_docx(path)

    if extension == ".epub":
        return extract_epub(path)

    raise ValueError(
        "Unsupported file format. Send TXT, PDF, DOCX or EPUB."
    )


# =========================================================
# AUDIO PROCESSING
# =========================================================

def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg is missing. Install FFmpeg on the server."
        )


def run_ffmpeg(args):
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        + args,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-1500:]
            or "FFmpeg processing failed."
        )


def apply_audio_settings(
    source,
    destination,
    speed,
    pitch,
):
    """
    Adjust speed with atempo.

    Pitch adjustment uses asetrate and resampling.
    This changes the pitch while attempting to preserve
    the requested overall playback speed.
    """

    require_ffmpeg()

    speed = max(0.5, min(float(speed), 2.0))
    pitch = max(-6, min(int(pitch), 6))

    filters = []

    if pitch != 0:
        ratio = 2 ** (pitch / 12)

        filters.append(
            f"asetrate=44100*{ratio:.6f}"
        )
        filters.append("aresample=44100")

        # Counteract the pitch-induced duration change.
        filters.append(
            f"atempo={speed / ratio:.6f}"
        )

    else:
        filters.append(f"atempo={speed:.6f}")

    run_ffmpeg([
        "-i", str(source),
        "-vn",
        "-af", ",".join(filters),
        "-codec:a", "libmp3lame",
        "-b:a", "96k",
        str(destination),
    ])


def merge_audio(files, destination):
    """
    Concatenate generated MP3 files in their original order.
    """

    require_ffmpeg()

    if not files:
        raise RuntimeError("No audio chunks were generated.")

    list_file = destination.parent / "concat.txt"

    with list_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        for item in files:
            safe_path = str(item.resolve()).replace(
                "'", "'\\''"
            )

            file.write(
                f"file '{safe_path}'\n"
            )

    try:
        run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "96k",
            str(destination),
        ])
    finally:
        list_file.unlink(missing_ok=True)


# =========================================================
# TTS ENGINES
# =========================================================

async def generate_edge_audio(
    text,
    output,
    voice,
    speed,
    pitch,
):
    rate_percent = round(
        (float(speed) - 1.0) * 100
    )

    rate = f"{rate_percent:+d}%"
    pitch_value = f"{int(pitch):+d}Hz"

    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch_value,
    )

    await communicate.save(str(output))


def generate_google_audio(
    text,
    output,
    language,
):
    """
    Google Translate TTS through gTTS.

    gTTS does not expose a named Madhur voice or
    arbitrary pitch control.
    """

    tts = gTTS(
        text=text,
        lang=language,
        slow=False,
    )

    tts.save(str(output))


async def generate_chunk(
    text,
    output,
    settings,
):
    engine = settings["engine"]

    if engine == "edge":
        await generate_edge_audio(
            text=text,
            output=output,
            voice=settings["voice"],
            speed=settings["speed"],
            pitch=settings["pitch"],
        )

    elif engine == "google":
        language = "hi"

        # Google Translate TTS has no Madhur voice selector.
        # Speed and pitch are applied after generation.
        await asyncio.to_thread(
            generate_google_audio,
            text,
            output,
            language,
        )

    else:
        raise ValueError("Unknown TTS engine.")


# =========================================================
# CONVERSION WORKER
# =========================================================

async def convert_text(
    text,
    settings,
    output_path,
    progress_callback=None,
):
    text = clean_text(text)

    if not text:
        raise ValueError("The supplied text is empty.")

    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(
            "This file exceeds the configured text limit."
        )

    chunks = split_text(text)

    with tempfile.TemporaryDirectory(
        prefix="speech_"
    ) as temp_directory:
        temp = Path(temp_directory)
        generated = []

        total = len(chunks)

        for index, chunk in enumerate(chunks, start=1):
            if progress_callback:
                await progress_callback(
                    index,
                    total,
                    "Generating speech",
                )

            raw_path = temp / f"part_{index:05d}.mp3"

            await generate_chunk(
                chunk,
                raw_path,
                settings,
            )

            if not raw_path.exists() or raw_path.stat().st_size == 0:
                raise RuntimeError(
                    f"Audio generation failed at chunk {index}."
                )

            # Edge TTS applies rate and pitch directly.
            # Google TTS requires post-processing.
            if settings["engine"] == "google":
                adjusted_path = temp / (
                    f"adjusted_{index:05d}.mp3"
                )

                await asyncio.to_thread(
                    apply_audio_settings,
                    raw_path,
                    adjusted_path,
                    settings["speed"],
                    settings["pitch"],
                )

                raw_path = adjusted_path

            generated.append(raw_path)

        if progress_callback:
            await progress_callback(
                total,
                total,
                "Merging audio",
            )

        await asyncio.to_thread(
            merge_audio,
            generated,
            output_path,
        )

    return output_path


# =========================================================
# TELEGRAM HANDLERS
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user = update.effective_user

    settings = get_settings(user.id)

    await update.effective_message.reply_text(
        "🎙️ Welcome to Speech Bot!\n\n"
        "Convert text and documents into speech.\n\n"
        f"Engine: {settings['engine']}\n"
        f"Voice: {settings['voice']}\n"
        f"Speed: {settings['speed']}x\n"
        f"Pitch: {settings['pitch']}\n\n"
        "Send a text message or upload a file.",
        reply_markup=main_keyboard(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "📚 Supported formats:\n"
        "• TXT\n"
        "• EPUB\n"
        "• PDF\n"
        "• DOCX\n\n"
        "Send text directly to create an MP3.\n"
        "Use Settings to select an engine.\n\n"
        "Madhur voice is available through Edge TTS "
        "if the configured voice ID is supported.\n\n"
        "Google Translate TTS does not provide "
        "a named Madhur voice selector.",
        reply_markup=main_keyboard(),
    )


async def show_settings(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    await update.effective_message.reply_text(
        "⚙️ Speech Settings",
        reply_markup=settings_keyboard(),
    )


async def select_voice(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🇮🇳 Madhur (Hindi Male)",
                callback_data="voice_madhur",
            ),
        ],
        [
            InlineKeyboardButton(
                "🇮🇳 Swara (Hindi Female)",
                callback_data="voice_sw ara".replace(" ", ""),
            ),
        ],
        [
            InlineKeyboardButton(
                "🇬🇧 Sonia (English UK)",
                callback_data="voice_sonia",
            ),
        ],
    ])

    await update.effective_message.reply_text(
        "🗣️ Choose a voice:",
        reply_markup=keyboard,
    )


async def show_speed(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "0.75x",
                callback_data="speed_0.75",
            ),
            InlineKeyboardButton(
                "1.0x",
                callback_data="speed_1.0",
            ),
        ],
        [
            InlineKeyboardButton(
                "1.25x",
                callback_data="speed_1.25",
            ),
            InlineKeyboardButton(
                "1.5x",
                callback_data="speed_1.5",
            ),
        ],
        [
            InlineKeyboardButton(
                "2.0x",
                callback_data="speed_2.0",
            ),
        ],
    ])

    await update.effective_message.reply_text(
        "⚡ Select speech speed:",
        reply_markup=keyboard,
    )


async def show_pitch(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "-4",
                callback_data="pitch_-4",
            ),
            InlineKeyboardButton(
                "-2",
                callback_data="pitch_-2",
            ),
            InlineKeyboardButton(
                "0",
                callback_data="pitch_0",
            ),
        ],
        [
            InlineKeyboardButton(
                "+2",
                callback_data="pitch_2",
            ),
            InlineKeyboardButton(
                "+4",
                callback_data="pitch_4",
            ),
        ],
    ])

    await update.effective_message.reply_text(
        "🎚️ Select pitch in semitones:",
        reply_markup=keyboard,
    )


async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "engine_edge":
        update_setting(user_id, "engine", "edge")

        await query.edit_message_text(
            "✅ Engine changed to Microsoft Edge TTS."
        )

    elif data == "engine_google":
        update_setting(user_id, "engine", "google")

        await query.edit_message_text(
            "✅ Engine changed to Google Translate TTS.\n\n"
            "Note: Google Translate TTS does not provide "
            "the named Madhur voice."
        )

    elif data == "voices":
        await query.edit_message_text(
            "Use Select Voice from the main menu."
        )

    elif data == "voice_madhur":
        update_setting(
            user_id,
            "voice",
            "hi-IN-MadhurNeural",
        )

        update_setting(user_id, "engine", "edge")

        await query.edit_message_text(
            "✅ Madhur Hindi male voice selected.\n"
            "Engine: Microsoft Edge TTS."
        )

    elif data == "voice_sw ara".replace(" ", ""):
        update_setting(
            user_id,
            "voice",
            "hi-IN-SwaraNeural",
        )

        update_setting(user_id, "engine", "edge")

        await query.edit_message_text(
            "✅ Swara Hindi female voice selected."
        )

    elif data == "voice_sonia":
        update_setting(
            user_id,
            "voice",
            "en-GB-SoniaNeural",
        )

        update_setting(user_id, "engine", "edge")

        await query.edit_message_text(
            "✅ Sonia English voice selected."
        )

    elif data.startswith("speed_"):
        speed = float(data.split("_", 1)[1])

        update_setting(
            user_id,
            "speed",
            speed,
        )

        await query.edit_message_text(
            f"✅ Speech speed set to {speed}x."
        )

    elif data.startswith("pitch_"):
        pitch = int(data.split("_", 1)[1])

        update_setting(
            user_id,
            "pitch",
            pitch,
        )

        await query.edit_message_text(
            f"✅ Pitch set to {pitch:+d} semitones."
        )

    elif data == "chapter_toggle":
        settings = get_settings(user_id)

        new_value = not settings["chapter_mode"]

        update_setting(
            user_id,
            "chapter_mode",
            new_value,
        )

        await query.edit_message_text(
            f"Chapter mode: {'ON' if new_value else 'OFF'}"
        )


# =========================================================
# AUDIO DELIVERY
# =========================================================

async def process_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text,
    filename="audiobook",
):
    user_id = update.effective_user.id

    current = ACTIVE_TASKS.get(user_id)

    if current and not current.done():
        await update.effective_message.reply_text(
            "⚠️ You already have a conversion running.\n"
            "Cancel it before starting another."
        )
        return

    task = asyncio.current_task()
    ACTIVE_TASKS[user_id] = task

    try:
        settings = get_settings(user_id)

        status = await update.effective_message.reply_text(
            "⏳ Preparing your audio..."
        )

        safe_name = re.sub(
            r"[^a-zA-Z0-9_-]+",
            "_",
            filename,
        )[:60]

        with tempfile.TemporaryDirectory(
            prefix="telegram_tts_"
        ) as directory:
            output_path = (
                Path(directory) / f"{safe_name}.mp3"
            )

            async def progress(index, total, stage):
                try:
                    await status.edit_text(
                        f"🎙️ {stage}\n"
                        f"Progress: {index}/{total}"
                    )
                except Exception:
                    pass

            await convert_text(
                text,
                settings,
                output_path,
                progress_callback=progress,
            )

            if task.cancelling():
                return

            await status.edit_text(
                "📤 Uploading completed MP3..."
            )

            with output_path.open("rb") as audio:
                await update.effective_message.reply_document(
                    document=audio,
                    filename=f"{safe_name}.mp3",
                    caption=(
                        f"🎧 {safe_name}\n"
                        f"Engine: {settings['engine']}\n"
                        f"Voice: {settings['voice']}"
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
        await update.effective_message.reply_text(
            "⏹️ Conversion cancelled."
        )
        raise

    except Exception as error:
        logger.exception("Conversion failed")

        await update.effective_message.reply_text(
            "❌ Conversion failed.\n\n"
            f"Reason: {str(error)[:700]}"
        )

    finally:
        if ACTIVE_TASKS.get(user_id) is task:
            ACTIVE_TASKS.pop(user_id, None)


# =========================================================
# TEXT MESSAGE HANDLER
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    text = (message.text or "").strip()

    if not text:
        return

    if text == "📝 Text to Speech":
        await message.reply_text(
            "Send the text you want to convert."
        )
        return

    if text == "📄 Convert File":
        await message.reply_text(
            "Upload a TXT, EPUB, PDF or DOCX file."
        )
        return

    if text == "🗣️ Select Voice":
        await select_voice(update, context)
        return

    if text == "⚙️ Settings":
        await show_settings(update, context)
        return

    if text == "🎚️ Speech Speed":
        await show_speed(update, context)
        return

    if text == "🎛️ Pitch":
        await show_pitch(update, context)
        return

    if text == "📚 Chapter Mode":
        settings = get_settings(update.effective_user.id)

        value = not settings["chapter_mode"]

        update_setting(
            update.effective_user.id,
            "chapter_mode",
            value,
        )

        await message.reply_text(
            f"Chapter mode: {'ON' if value else 'OFF'}"
        )
        return

    if text == "❌ Cancel Task":
        user_id = update.effective_user.id
        task = ACTIVE_TASKS.get(user_id)

        if task and not task.done():
            task.cancel()

            await message.reply_text(
                "⏹️ Cancellation requested."
            )
        else:
            await message.reply_text(
                "There is no active conversion."
            )

        return

    if text == "ℹ️ Help":
        await help_command(update, context)
        return

    await process_text(
        update,
        context,
        text,
        filename="text_to_speech",
    )


# =========================================================
# FILE HANDLER
# =========================================================

async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    document = message.document

    if not document:
        return

    filename = document.file_name or "document.txt"
    extension = Path(filename).suffix.lower()

    if extension not in (
        ".txt",
        ".epub",
        ".pdf",
        ".docx",
    ):
        await message.reply_text(
            "❌ Unsupported file.\n"
            "Supported: TXT, EPUB, PDF and DOCX."
        )
        return

    if document.file_size and document.file_size > 20 * 1024 * 1024:
        await message.reply_text(
            "❌ File exceeds the configured 20 MB limit."
        )
        return

    status = await message.reply_text(
        "📥 Downloading your file..."
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="source_"
        ) as directory:
            source = Path(directory) / filename

            telegram_file = await context.bot.get_file(
                document.file_id
            )

            await telegram_file.download_to_drive(
                custom_path=str(source)
            )

            await status.edit_text(
                "📖 Extracting document text..."
            )

            text = await asyncio.to_thread(
                extract_file,
                source,
            )

            text = clean_text(text)

            if not text:
                await status.edit_text(
                    "❌ No readable text found in the file."
                )
                return

            try:
                await status.delete()
            except Exception:
                pass

            await process_text(
                update,
                context,
                text,
                filename=Path(filename).stem,
            )

    except Exception as error:
        logger.exception("File processing failed")

        try:
            await status.edit_text(
                "❌ File processing failed:\n"
                f"{str(error)[:700]}"
            )
        except Exception:
            pass


# =========================================================
# STARTUP
# =========================================================

async def post_init(application):
    logger.info("Speech Bot started successfully.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable is missing."
        )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("settings", show_settings)
    )

    application.add_handler(
        CallbackQueryHandler(callback_handler)
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler,
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    logger.info("Starting Telegram polling...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
