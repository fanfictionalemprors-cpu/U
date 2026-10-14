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
# CONFIGURATION
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
        return json.loads(
            SETTINGS_FILE.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        logger.exception("Settings could not be loaded")
        return {}


def save_settings(data):
    temporary = SETTINGS_FILE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary.replace(SETTINGS_FILE)


def get_settings(user_id):
    all_settings = load_settings()
    user_key = str(user_id)

    if user_key not in all_settings:
        all_settings[user_key] = DEFAULT_SETTINGS.copy()
        save_settings(all_settings)

    settings = DEFAULT_SETTINGS.copy()
    settings.update(all_settings[user_key])

    return settings


def update_setting(user_id, key, value):
    all_settings = load_settings()
    user_key = str(user_id)

    if user_key not in all_settings:
        all_settings[user_key] = DEFAULT_SETTINGS.copy()

    all_settings[user_key][key] = value

    save_settings(all_settings)


# =========================================================
# MAIN MENU
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
                "🗣️ Select Voice",
                callback_data="menu_voices",
            ),
        ],
        [
            InlineKeyboardButton(
                "⚡ Speech Rate",
                callback_data="menu_speed",
            ),
        ],
        [
            InlineKeyboardButton(
                "🎚️ Pitch",
                callback_data="menu_pitch",
            ),
        ],
    ])


# =========================================================
# TEXT UTILITIES
# =========================================================

def clean_text(text):
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def split_text(text, limit=MAX_CHUNK_SIZE):
    """
    Split text without intentionally dropping characters.
    """

    chunks = []

    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)

        if cut < limit // 2:
            cut = text.rfind(". ", 0, limit + 1)

        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)

        if cut <= 0:
            cut = limit

        chunk = text[:cut].strip()

        if chunk:
            chunks.append(chunk)

        text = text[cut:].strip()

    if text:
        chunks.append(text)

    return chunks


# =========================================================
# DOCUMENT EXTRACTION
# =========================================================

def extract_txt(path):
    for encoding in (
        "utf-8-sig",
        "utf-8",
        "utf-16",
        "cp1252",
    ):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeError:
            continue

    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def extract_pdf(path):
    reader = PdfReader(str(path))
    sections = []

    for page in reader.pages:
        sections.append(page.extract_text() or "")

    return "\n\n".join(sections)


def extract_docx(path):
    document = Document(str(path))
    sections = []

    for paragraph in document.paragraphs:
        if paragraph.text.strip():
            sections.append(paragraph.text)

    for table in document.tables:
        for row in table.rows:
            cells = [
                cell.text.strip()
                for cell in row.cells
                if cell.text.strip()
            ]

            if cells:
                sections.append(" | ".join(cells))

    return "\n\n".join(sections)


def extract_epub(path):
    book = epub.read_epub(str(path))
    sections = []

    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(
            item.get_content(),
            "html.parser",
        )

        for element in soup(
            ["script", "style", "nav"]
        ):
            element.decompose()

        text = soup.get_text(
            separator="\n",
            strip=True,
        )

        if text:
            sections.append(text)

    return "\n\n".join(sections)


def extract_document(path):
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
        "Unsupported file. Send TXT, EPUB, PDF or DOCX."
    )


# =========================================================
# RATE AND PITCH
# =========================================================

def get_edge_rate(speed):
    """
    Converts 1.1x to +10%, 1.5x to +50%, 2.0x to +100%.
    """

    speed = float(speed)

    if not 1.1 <= speed <= 2.0:
        raise ValueError(
            "Speech rate must be between 1.1x and 2.0x."
        )

    percent = round((speed - 1.0) * 100)

    return f"+{percent}%"


def get_pitch_value(pitch):
    pitch = int(pitch)

    if not -6 <= pitch <= 6:
        raise ValueError("Pitch must be between -6 and +6.")

    return f"{pitch:+d}Hz"


# =========================================================
# FFMPEG
# =========================================================

def require_ffmpeg():
    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "FFmpeg is not installed on the server."
        )


def run_ffmpeg(args):
    require_ffmpeg()

    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
        ] + args,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-1200:]
            or "FFmpeg processing failed."
        )


def adjust_google_audio(
    source,
    destination,
    speed,
    pitch,
):
    """
    Google Translate TTS does not expose a named voice,
    arbitrary pitch, or a direct rate control.

    This function applies speed and optional pitch
    processing after generating the source audio.
    """

    speed = max(1.1, min(float(speed), 2.0))
    pitch = max(-6, min(int(pitch), 6))

    filters = []

    if pitch != 0:
        ratio = 2 ** (pitch / 12)

        filters.append(
            f"asetrate=44100*{ratio:.8f}"
        )
        filters.append("aresample=44100")
        filters.append(
            f"atempo={speed / ratio:.8f}"
        )
    else:
        filters.append(f"atempo={speed:.8f}")

    run_ffmpeg([
        "-i", str(source),
        "-vn",
        "-af", ",".join(filters),
        "-codec:a", "libmp3lame",
        "-b:a", "96k",
        str(destination),
    ])


def merge_audio(audio_files, output):
    if not audio_files:
        raise RuntimeError("No audio chunks were generated.")

    require_ffmpeg()

    concat_file = output.parent / "concat_list.txt"

    with concat_file.open(
        "w",
        encoding="utf-8",
    ) as file:
        for audio in audio_files:
            path = str(audio.resolve())
            path = path.replace("'", "'\\''")

            file.write(f"file '{path}'\n")

    try:
        run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-vn",
            "-codec:a", "libmp3lame",
            "-b:a", "96k",
            str(output),
        ])
    finally:
        concat_file.unlink(missing_ok=True)


# =========================================================
# TTS ENGINES
# =========================================================

async def edge_tts_generate(
    text,
    output,
    settings,
):
    communicate = edge_tts.Communicate(
        text=text,
        voice=settings["voice"],
        rate=get_edge_rate(settings["speed"]),
        pitch=get_pitch_value(settings["pitch"]),
    )

    await communicate.save(str(output))


def google_tts_generate(text, output):
    tts = gTTS(
        text=text,
        lang="hi",
        slow=False,
    )

    tts.save(str(output))


async def generate_audio_chunk(
    text,
    output,
    settings,
):
    engine = settings["engine"]

    if engine == "edge":
        await edge_tts_generate(
            text,
            output,
            settings,
        )

    elif engine == "google":
        await asyncio.to_thread(
            google_tts_generate,
            text,
            output,
        )

        adjusted = output.with_name(
            output.stem + "_adjusted.mp3"
        )

        await asyncio.to_thread(
            adjust_google_audio,
            output,
            adjusted,
            settings["speed"],
            settings["pitch"],
        )

        output.unlink(missing_ok=True)
        adjusted.replace(output)

    else:
        raise ValueError("Unsupported TTS engine.")

    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError("TTS engine generated an empty file.")


# =========================================================
# CONVERSION ENGINE
# =========================================================

async def convert_text(
    text,
    settings,
    output,
    progress_callback=None,
):
    text = clean_text(text)

    if not text:
        raise ValueError("No readable text was found.")

    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(
            "Text exceeds the configured 2,000,000 character limit."
        )

    chunks = split_text(text)

    with tempfile.TemporaryDirectory(
        prefix="tts_chunks_"
    ) as directory:
        temp = Path(directory)
        audio_files = []

        total = len(chunks)

        for index, chunk in enumerate(chunks, start=1):
            if progress_callback:
                await progress_callback(
                    index,
                    total,
                    "Generating speech",
                )

            audio_path = temp / f"chunk_{index:05d}.mp3"

            await generate_audio_chunk(
                chunk,
                audio_path,
                settings,
            )

            audio_files.append(audio_path)

        if progress_callback:
            await progress_callback(
                total,
                total,
                "Merging audio",
            )

        await asyncio.to_thread(
            merge_audio,
            audio_files,
            output,
        )

    return output


# =========================================================
# VOICE MENU
# =========================================================

async def show_voices(
    update,
    context,
    use_callback=False,
):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🇮🇳 Madhur — Hindi Male",
                callback_data="voice_madhur",
            ),
        ],
        [
            InlineKeyboardButton(
                "🇮🇳 Swara — Hindi Female",
                callback_data="voice_swara",
            ),
        ],
        [
            InlineKeyboardButton(
                "🇬🇧 Sonia — English Female",
                callback_data="voice_sonia",
            ),
        ],
    ])

    if use_callback:
        await update.callback_query.edit_message_text(
            "🗣️ Choose your voice:",
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(
            "🗣️ Choose your voice:",
            reply_markup=keyboard,
        )


# =========================================================
# SPEECH RATE MENU
# =========================================================

async def show_speed(
    update,
    context,
    use_callback=False,
):
    rates = [
        1.1, 1.2, 1.3, 1.4, 1.5,
        1.6, 1.7, 1.8, 1.9, 2.0,
    ]

    keyboard = []

    for index in range(0, len(rates), 2):
        row = []

        for rate in rates[index:index + 2]:
            row.append(
                InlineKeyboardButton(
                    f"⚡ {rate:.1f}x",
                    callback_data=f"speed_{rate:.1f}",
                )
            )

        keyboard.append(row)

    markup = InlineKeyboardMarkup(keyboard)

    if use_callback:
        await update.callback_query.edit_message_text(
            "⚡ Select Speech Rate\n\n"
            "Choose a speed from 1.1x to 2.0x:",
            reply_markup=markup,
        )
    else:
        await update.effective_message.reply_text(
            "⚡ Select Speech Rate\n\n"
            "Choose a speed from 1.1x to 2.0x:",
            reply_markup=markup,
        )


# =========================================================
# PITCH MENU
# =========================================================

async def show_pitch(
    update,
    context,
    use_callback=False,
):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "-6",
                callback_data="pitch_-6",
            ),
            InlineKeyboardButton(
                "-3",
                callback_data="pitch_-3",
            ),
            InlineKeyboardButton(
                "0",
                callback_data="pitch_0",
            ),
        ],
        [
            InlineKeyboardButton(
                "+3",
                callback_data="pitch_3",
            ),
            InlineKeyboardButton(
                "+6",
                callback_data="pitch_6",
            ),
        ],
    ])

    if use_callback:
        await update.callback_query.edit_message_text(
            "🎚️ Select Pitch:",
            reply_markup=keyboard,
        )
    else:
        await update.effective_message.reply_text(
            "🎚️ Select Pitch:",
            reply_markup=keyboard,
        )


# =========================================================
# START AND HELP
# =========================================================

async def start(update, context):
    user_id = update.effective_user.id
    settings = get_settings(user_id)

    await update.effective_message.reply_text(
        "🎙️ Welcome to Speech Bot!\n\n"
        "Convert text and books into audio.\n\n"
        f"Engine: {settings['engine']}\n"
        f"Voice: {settings['voice']}\n"
        f"Speech Rate: {settings['speed']:.1f}x\n"
        f"Pitch: {settings['pitch']}\n\n"
        "Send text or upload a TXT, EPUB, PDF or DOCX file.",
        reply_markup=main_keyboard(),
    )


async def help_command(update, context):
    await update.effective_message.reply_text(
        "📚 Speech Bot Help\n\n"
        "Commands:\n"
        "/start - Open menu\n"
        "/help - Help\n"
        "/settings - Settings\n\n"
        "Supported files:\n"
        "TXT, EPUB, PDF, DOCX\n\n"
        "Speech Rate: 1.1x to 2.0x\n"
        "Default voice: hi-IN-MadhurNeural\n\n"
        "Google Translate TTS does not offer a named Madhur voice.",
        reply_markup=main_keyboard(),
    )


async def settings_command(update, context):
    await update.effective_message.reply_text(
        "⚙️ Speech Settings",
        reply_markup=settings_keyboard(),
    )


# =========================================================
# CALLBACK HANDLER
# =========================================================

async def callback_handler(update, context):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "engine_edge":
        update_setting(user_id, "engine", "edge")

        await query.edit_message_text(
            "✅ Microsoft Edge TTS selected."
        )

    elif data == "engine_google":
        update_setting(user_id, "engine", "google")

        await query.edit_message_text(
            "✅ Google Translate TTS selected.\n\n"
            "Madhur voice is not available as a named "
            "voice in Google Translate TTS."
        )

    elif data == "menu_voices":
        await show_voices(
            update,
            context,
            use_callback=True,
        )

    elif data == "menu_speed":
        await show_speed(
            update,
            context,
            use_callback=True,
        )

    elif data == "menu_pitch":
        await show_pitch(
            update,
            context,
            use_callback=True,
        )

    elif data == "voice_madhur":
        update_setting(
            user_id,
            "voice",
            "hi-IN-MadhurNeural",
        )

        update_setting(user_id, "engine", "edge")

        await query.edit_message_text(
            "✅ Madhur Hindi male voice selected."
        )

    elif data == "voice_swara":
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
        try:
            speed = float(data.split("_", 1)[1])

            if not 1.1 <= speed <= 2.0:
                raise ValueError("Invalid speed")

            speed = round(speed, 1)

            update_setting(
                user_id,
                "speed",
                speed,
            )

            await query.edit_message_text(
                f"✅ Speech Rate Updated!\n\n"
                f"⚡ Selected Rate: {speed:.1f}x"
            )

        except (ValueError, TypeError):
            await query.edit_message_text(
                "❌ Invalid rate. Select 1.1x to 2.0x."
            )

    elif data.startswith("pitch_"):
        try:
            pitch = int(data.split("_", 1)[1])

            if pitch not in (-6, -3, 0, 3, 6):
                raise ValueError("Invalid pitch")

            update_setting(
                user_id,
                "pitch",
                pitch,
            )

            await query.edit_message_text(
                f"✅ Pitch set to {pitch:+d}."
            )

        except (ValueError, TypeError):
            await query.edit_message_text(
                "❌ Invalid pitch value."
            )


# =========================================================
# CONVERSION WORKER
# =========================================================

async def process_text(
    update,
    context,
    text,
    filename="audiobook",
):
    user_id = update.effective_user.id
    current = ACTIVE_TASKS.get(user_id)

    if current and not current.done():
        await update.effective_message.reply_text(
            "⚠️ Your conversion is already running.\n"
            "Cancel it before starting another."
        )
        return

    task = asyncio.current_task()
    ACTIVE_TASKS[user_id] = task

    try:
        settings = get_settings(user_id)

        safe_name = re.sub(
            r"[^a-zA-Z0-9_-]+",
            "_",
            filename,
        ).strip("_")[:60] or "audiobook"

        status = await update.effective_message.reply_text(
            "⏳ Preparing audio..."
        )

        with tempfile.TemporaryDirectory(
            prefix="tts_output_"
        ) as directory:
            output = Path(directory) / f"{safe_name}.mp3"

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
                output,
                progress_callback=progress,
            )

            await status.edit_text(
                "📤 Uploading audio..."
            )

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
        await update.effective_message.reply_text(
            "⏹️ Conversion cancelled."
        )
        raise

    except Exception as error:
        logger.exception("Audio conversion failed")

        await update.effective_message.reply_text(
            "❌ Conversion failed:\n"
            f"{str(error)[:700]}"
        )

    finally:
        if ACTIVE_TASKS.get(user_id) is task:
            ACTIVE_TASKS.pop(user_id, None)


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(update, context):
    message = update.effective_message
    text = (message.text or "").strip()

    if not text:
        return

    if text == "📝 Text to Speech":
        await message.reply_text(
            "📝 Send the text you want to convert."
        )
        return

    if text == "📄 Convert File":
        await message.reply_text(
            "📄 Upload a TXT, EPUB, PDF or DOCX file."
        )
        return

    if text == "🗣️ Select Voice":
        await show_voices(update, context)
        return

    if text == "⚙️ Settings":
        await settings_command(update, context)
        return

    if text == "⚡ Speech Rate":
        await show_speed(update, context)
        return

    if text == "🎚️ Pitch":
        await show_pitch(update, context)
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
                "ℹ️ No active conversion."
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

async def document_handler(update, context):
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
            "Supported: TXT, EPUB, PDF, DOCX."
        )
        return

    if (
        document.file_size is not None
        and document.file_size > MAX_FILE_SIZE
    ):
        await message.reply_text(
            "❌ File is larger than 20 MB."
        )
        return

    status = await message.reply_text(
        "📥 Downloading file..."
    )

    try:
        with tempfile.TemporaryDirectory(
            prefix="tts_source_"
        ) as directory:
            source = Path(directory) / Path(filename).name

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
                extract_document,
                source,
            )

            text = clean_text(text)

            if not text:
                await status.edit_text(
                    "❌ No readable text found."
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
        logger.exception("Document processing failed")

        try:
            await status.edit_text(
                "❌ File processing failed:\n"
                f"{str(error)[:700]}"
            )
        except Exception:
            pass


# =========================================================
# APPLICATION STARTUP
# =========================================================

async def post_init(application):
    logger.info("Speech Bot initialized successfully.")


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it to GitHub Secrets."
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
        CommandHandler("settings", settings_command)
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
