
#!/usr/bin/env python3

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

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
)
from telegram.error import TelegramError
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tts-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]

LOCAL_BOT_API = os.getenv("LOCAL_BOT_API", "0").lower() in {
    "1", "true", "yes"
}
LOCAL_BOT_API_URL = os.getenv(
    "LOCAL_BOT_API_URL",
    "http://127.0.0.1:8081",
).rstrip("/")

MAX_UPLOAD_MB = int(
    os.getenv("MAX_UPLOAD_MB", "1900" if LOCAL_BOT_API else "50")
)
MAX_INPUT_MB = int(
    os.getenv("MAX_INPUT_MB", "1900" if LOCAL_BOT_API else "50")
)
SEND_PART_MB = int(
    os.getenv("SEND_PART_MB", "1800" if LOCAL_BOT_API else "45")
)
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "20000000"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))

WORK_ROOT = Path(
    os.getenv("TTS_WORKDIR", "/tmp/telegram-tts-work")
)
WORK_ROOT.mkdir(parents=True, exist_ok=True)

# Settings are stored in memory during this runner session.
USER_SETTINGS = {}
USER_TASKS = {}
USER_PROGRESS = {}

VOICES = {
    "madhur": ("hi-IN-MadhurNeural", "Hindi Male — Madhur"),
    "swara": ("hi-IN-SwaraNeural", "Hindi Female — Swara"),
    "guy": ("en-US-GuyNeural", "English Male — Guy"),
    "jenny": ("en-US-JennyNeural", "English Female — Jenny"),
}


# =========================================================
# SETTINGS
# =========================================================

def settings_for(user_id):
    return USER_SETTINGS.setdefault(
        user_id,
        {
            "voice": os.getenv(
                "DEFAULT_VOICE", "hi-IN-MadhurNeural"
            ),
            "rate": float(os.getenv("DEFAULT_RATE", "1.1")),
            "pitch": int(os.getenv("DEFAULT_PITCH", "0")),
            "engine": os.getenv("DEFAULT_ENGINE", "edge").lower(),
            "name": "Audiobook",
        },
    )


def safe_name(name):
    name = Path(name or "Audiobook").stem
    name = re.sub(r"[^A-Za-z0-9 _().-]+", "", name)
    name = re.sub(r"\s+", " ", name).strip(" ._-")
    return (name or "Audiobook")[:100]


def get_proxy():
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()

    if not host or not port:
        return None

    auth = ""
    if username and password:
        auth = (
            f"{quote(username, safe='')}:"
            f"{quote(password, safe='')}@"
        )

    return f"http://{auth}{host}:{port}"


def rate_to_edge(rate):
    """1.0 = normal; 1.5 = +50%; 2.0 = +100%."""
    percent = round((rate - 1.0) * 100)
    return f"{percent:+d}%"


def pitch_to_edge(pitch):
    return f"{pitch:+d}Hz"


def voice_label(voice):
    for _, (voice_id, label) in VOICES.items():
        if voice_id == voice:
            return label
    return voice


def settings_text(user_id):
    s = settings_for(user_id)
    return (
        "⚙️ Your TTS Settings\n\n"
        f"🎙️ Voice: {voice_label(s['voice'])}\n"
        f"🚀 Rate/Speed: {s['rate']:.2f}x\n"
        f"🎚️ Pitch: {s['pitch']:+d} Hz\n"
        f"🔊 Engine: {s['engine']}\n"
        f"📝 Audio name: {s['name']}\n\n"
        "Rate: 1.0x–2.0x\n"
        "Pitch: -20 Hz to +20 Hz"
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎙️ Voice", callback_data="menu:voice"),
            InlineKeyboardButton("🚀 Rate", callback_data="menu:rate"),
        ],
        [
            InlineKeyboardButton("🎚️ Pitch", callback_data="menu:pitch"),
            InlineKeyboardButton("🔊 Engine", callback_data="menu:engine"),
        ],
        [
            InlineKeyboardButton("📝 Audio Name", callback_data="menu:name"),
            InlineKeyboardButton("🔄 Refresh", callback_data="menu:refresh"),
        ],
    ])


# =========================================================
# DOCUMENT TEXT EXTRACTION
# =========================================================

def extract_text(path):
    ext = path.suffix.lower()

    if ext in {".txt", ".text", ".md"}:
        return path.read_text(
            encoding="utf-8",
            errors="replace",
        )

    if ext == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

    if ext == ".docx":
        document = Document(str(path))
        paragraphs = [
            paragraph.text
            for paragraph in document.paragraphs
        ]
        return "\n".join(paragraphs)

    if ext == ".epub":
        book = epub.read_epub(str(path))
        sections = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(
                    item.get_content(),
                    "html.parser",
                )
                sections.append(soup.get_text(" ", strip=True))

        return "\n\n".join(sections)

    raise ValueError(
        "Supported formats: TXT, MD, PDF, DOCX and EPUB."
    )


def split_text(text, limit=CHUNK_CHARS):
    text = re.sub(r"\r\n?", "\n", text).strip()
    chunks = []

    while text:
        if len(text) <= limit:
            chunks.append(text)
            break

        cut = max(
            text.rfind("\n", 0, limit),
            text.rfind(". ", 0, limit),
            text.rfind(" ", 0, limit),
        )

        if cut < limit // 2:
            cut = limit

        chunks.append(text[:cut].strip())
        text = text[cut:].strip()

    return [chunk for chunk in chunks if chunk]


# =========================================================
# TTS ENGINES
# =========================================================

async def make_edge_audio(text, output_path, settings):
    kwargs = {}
    proxy = get_proxy()

    if proxy:
        kwargs["proxy"] = proxy

    communicator = edge_tts.Communicate(
        text=text,
        voice=settings["voice"],
        rate=rate_to_edge(settings["rate"]),
        pitch=pitch_to_edge(settings["pitch"]),
        **kwargs,
    )

    await communicator.save(str(output_path))


async def make_google_audio(text, output_path, settings):
    proxy = get_proxy()
    proxies = (
        {"http": proxy, "https": proxy}
        if proxy else None
    )

    lang = (
        "hi"
        if settings["voice"].startswith("hi-")
        else "en"
    )

    tts = gTTS(
        text=text,
        lang=lang,
        slow=settings["rate"] < 1.0,
        proxies=proxies,
    )

    await asyncio.to_thread(
        tts.save,
        str(output_path),
    )

    # Google Translate TTS does not expose the same
    # continuous rate and pitch controls as Edge TTS.
    # Apply playback-rate changes with FFmpeg if needed.
    if settings["rate"] != 1.0 or settings["pitch"] != 0:
        adjusted = output_path.with_name(
            output_path.stem + "_adjusted.mp3"
        )

        tempo = settings["rate"]
        pitch_factor = 2 ** (settings["pitch"] / 1200)

        # atempo supports 0.5–2.0 per filter.
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(output_path),
            "-filter:a",
            f"asetrate=44100*{pitch_factor:.6f},"
            f"aresample=44100,atempo={tempo:.6f}",
            "-y",
            str(adjusted),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _, error = await proc.communicate()

        if proc.returncode != 0:
            adjusted.unlink(missing_ok=True)
            raise RuntimeError(
                "Google audio adjustment failed: "
                + error.decode(errors="replace")[-800:]
            )

        output_path.unlink(missing_ok=True)
        adjusted.rename(output_path)


async def generate_audio(text, folder, settings, status, user_id):
    chunks = split_text(text)

    if not chunks:
        raise ValueError("No readable text was found.")

    pieces = []
    USER_PROGRESS[user_id] = {
        "stage": "Generating",
        "done": 0,
        "total": len(chunks),
    }

    for index, chunk in enumerate(chunks, start=1):
        task = USER_TASKS.get(user_id)

        if task and task.cancelled():
            raise asyncio.CancelledError()

        output = folder / f"chunk_{index:06d}.mp3"

        if settings["engine"] == "google":
            await make_google_audio(chunk, output, settings)
        else:
            await make_edge_audio(chunk, output, settings)

        pieces.append(output)

        USER_PROGRESS[user_id] = {
            "stage": "Generating",
            "done": index,
            "total": len(chunks),
        }

        if index == 1 or index % 5 == 0 or index == len(chunks):
            try:
                await status.edit_text(
                    f"🎙️ Generating audio: {index}/{len(chunks)} chunks"
                )
            except TelegramError:
                pass

    concat_list = folder / "concat.txt"
    concat_list.write_text(
        "".join(
            f"file '{piece.as_posix()}'\n"
            for piece in pieces
        ),
        encoding="utf-8",
    )

    merged = folder / "merged.mp3"

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-y", str(merged),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, error = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            "FFmpeg merge failed: "
            + error.decode(errors="replace")[-1000:]
        )

    return merged


# =========================================================
# LARGE AUDIO SPLITTING
# =========================================================

async def split_audio(source, folder):
    max_bytes = SEND_PART_MB * 1024 * 1024

    if source.stat().st_size <= max_bytes:
        return [source]

    probe = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    output, _ = await probe.communicate()
    duration = float(output.decode().strip() or "0")

    if duration <= 0:
        raise RuntimeError("Unable to determine audio duration.")

    ratio = max_bytes / source.stat().st_size
    segment_seconds = max(20, duration * ratio * 0.80)

    for _ in range(10):
        for old_part in folder.glob("part_*.mp3"):
            old_part.unlink(missing_ok=True)

        pattern = str(folder / "part_%03d.mp3")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(source),
            "-f", "segment",
            "-segment_time", str(segment_seconds),
            "-c", "copy",
            "-reset_timestamps", "1",
            "-y", pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _, error = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                "Audio split failed: "
                + error.decode(errors="replace")[-1000:]
            )

        parts = sorted(folder.glob("part_*.mp3"))

        if parts and all(
            part.stat().st_size <= max_bytes
            for part in parts
        ):
            return parts

        segment_seconds *= 0.65

    raise RuntimeError(
        "Could not split audio under SEND_PART_MB. "
        "Lower SEND_PART_MB in the workflow."
    )


# =========================================================
# COMMANDS
# =========================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎧 Telegram TTS Bot is ready!\n\n"
        "Send a TXT, MD, PDF, DOCX or EPUB file to convert it to MP3.\n\n"
        "Use /settings to configure voice, rate, pitch and filename.\n"
        "Use /help for all commands."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "📚 TTS Bot Commands\n\n"
        "/start — Start bot\n"
        "/help — Help\n"
        "/settings — Open settings menu\n"
        "/voice — Choose voice\n"
        "/voice madhur — Hindi male\n"
        "/voice swara — Hindi female\n"
        "/rate 1.5 — Set speech rate\n"
        "/speed 1.5 — Alias for /rate\n"
        "/pitch 5 — Set pitch in Hz\n"
        "/engine edge — Microsoft Edge TTS\n"
        "/engine google — Google TTS\n"
        "/name My Audiobook — Set output filename\n"
        "/status — Conversion progress\n"
        "/cancel — Cancel current conversion\n"
        "/demo — Generate voice demo\n\n"
        "Rate range: 1.0x to 2.0x\n"
        "Pitch range: -20 to +20 Hz\n"
        "Supported files: TXT, MD, PDF, DOCX, EPUB"
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        settings_text(update.effective_user.id),
        reply_markup=settings_keyboard(),
    )


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if context.args:
        key = context.args[0].lower()

        if key not in VOICES:
            await update.effective_message.reply_text(
                "Choose: /voice madhur, /voice swara, "
                "/voice guy or /voice jenny"
            )
            return

        settings_for(user_id)["voice"] = VOICES[key][0]
        await update.effective_message.reply_text(
            f"Voice set: {VOICES[key][1]}"
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                label,
                callback_data=f"voice:{key}",
            )
        ]
        for key, (_, label) in VOICES.items()
    ]

    await update.effective_message.reply_text(
        "Choose your voice:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = settings_for(update.effective_user.id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current rate: {settings['rate']:.2f}x\n"
            "Use /rate 1.5\n"
            "Allowed range: 1.0 to 2.0"
        )
        return

    try:
        rate = float(context.args[0])
        if not 1.0 <= rate <= 2.0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Rate must be from 1.0 to 2.0.\n"
            "Example: /rate 1.5"
        )
        return

    settings["rate"] = rate

    await update.effective_message.reply_text(
        f"✅ Speech rate set to {rate:.2f}x."
    )


async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await rate_cmd(update, context)


async def pitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = settings_for(update.effective_user.id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current pitch: {settings['pitch']:+d} Hz\n"
            "Use /pitch 5\n"
            "Allowed range: -20 to +20 Hz"
        )
        return

    try:
        pitch = int(context.args[0])
        if not -20 <= pitch <= 20:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "❌ Pitch must be an integer from -20 to +20.\n"
            "Example: /pitch -5"
        )
        return

    settings["pitch"] = pitch

    await update.effective_message.reply_text(
        f"✅ Pitch set to {pitch:+d} Hz."
    )


async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = settings_for(update.effective_user.id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current engine: {settings['engine']}\n"
            "Use /engine edge or /engine google."
        )
        return

    engine = context.args[0].lower()

    if engine not in {"edge", "google"}:
        await update.effective_message.reply_text(
            "Use /engine edge or /engine google."
        )
        return

    settings["engine"] = engine

    await update.effective_message.reply_text(
        f"✅ Engine set to {engine}."
    )


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    settings = settings_for(update.effective_user.id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current audio name: {settings['name']}\n"
            "Set with /name My Audiobook"
        )
        return

    settings["name"] = safe_name(" ".join(context.args))

    await update.effective_message.reply_text(
        f"✅ Audio filename set to {settings['name']}.mp3"
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task = USER_TASKS.get(user_id)
    progress = USER_PROGRESS.get(user_id)

    if not task or task.done():
        await update.effective_message.reply_text(
            "No active conversion."
        )
        return

    if progress:
        await update.effective_message.reply_text(
            f"Stage: {progress['stage']}\n"
            f"Progress: {progress['done']}/{progress['total']}"
        )
    else:
        await update.effective_message.reply_text(
            "Conversion is running."
        )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    task = USER_TASKS.get(user_id)

    if not task or task.done():
        await update.effective_message.reply_text(
            "No active conversion."
        )
        return

    task.cancel()
    await update.effective_message.reply_text(
        "Cancellation requested. Temporary files will be removed."
    )


async def demo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    folder = Path(
        tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT)
    )

    try:
        settings = settings_for(user_id).copy()
        output = folder / f"{safe_name(settings['name'])}_demo.mp3"

        if settings["engine"] == "google":
            await make_google_audio(
                "Hello! नमस्ते! This is a voice demo.",
                output,
                settings,
            )
        else:
            await make_edge_audio(
                "Hello! नमस्ते! This is a voice demo.",
                output,
                settings,
            )

        with output.open("rb") as audio:
            await update.effective_message.reply_audio(
                audio=audio,
                filename=output.name,
                title=output.stem[:64],
            )

    except Exception as exc:
        log.exception("Demo failed")
        await update.effective_message.reply_text(
            f"Demo failed: {type(exc).__name__}: {str(exc)[:800]}"
        )

    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def settings_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    settings = settings_for(user_id)

    if query.data == "menu:refresh":
        await query.edit_message_text(
            settings_text(user_id),
            reply_markup=settings_keyboard(),
        )
        return

    if query.data == "menu:voice":
        keyboard = [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"voice:{key}",
                )
            ]
            for key, (_, label) in VOICES.items()
        ]
        await query.edit_message_text(
            "Choose voice:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if query.data == "menu:rate":
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{rate:.1f}x",
                    callback_data=f"setrate:{rate:.1f}",
                )
                for rate in (1.0, 1.1, 1.2)
            ],
            [
                InlineKeyboardButton(
                    f"{rate:.1f}x",
                    callback_data=f"setrate:{rate:.1f}",
                )
                for rate in (1.3, 1.5, 1.7)
            ],
            [
                InlineKeyboardButton(
                    f"{rate:.1f}x",
                    callback_data=f"setrate:{rate:.1f}",
                )
                for rate in (1.8, 1.9, 2.0)
            ],
        ]
        await query.edit_message_text(
            f"Current rate: {settings['rate']:.2f}x\nChoose a rate:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if query.data == "menu:pitch":
        keyboard = [
            [
                InlineKeyboardButton(
                    f"{pitch:+d} Hz",
                    callback_data=f"setpitch:{pitch}",
                )
                for pitch in (-20, -10, -5, 0, 5)
            ],
            [
                InlineKeyboardButton(
                    f"{pitch:+d} Hz",
                    callback_data=f"setpitch:{pitch}",
                )
                for pitch in (10, 15, 20)
            ],
        ]
        await query.edit_message_text(
            f"Current pitch: {settings['pitch']:+d} Hz\nChoose pitch:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if query.data == "menu:engine":
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "Microsoft Edge TTS",
                    callback_data="setengine:edge",
                )
            ],
            [
                InlineKeyboardButton(
                    "Google TTS",
                    callback_data="setengine:google",
                )
            ],
        ])
        await query.edit_message_text(
            f"Current engine: {settings['engine']}\nChoose engine:",
            reply_markup=keyboard,
        )
        return

    if query.data == "menu:name":
        await query.edit_message_text(
            "Send the command in chat:\n/name My Audiobook\n\n"
            "Then open /settings again."
        )
        return

    if query.data.startswith("voice:"):
        key = query.data.split(":", 1)[1]
        if key in VOICES:
            settings["voice"] = VOICES[key][0]
            await query.edit_message_text(
                f"✅ Voice set: {VOICES[key][1]}\n\n"
                "Open /settings to continue."
            )
        return

    if query.data.startswith("setrate:"):
        rate = float(query.data.split(":", 1)[1])
        settings["rate"] = rate
        await query.edit_message_text(
            f"✅ Rate set to {rate:.1f}x.\n\n"
            "Open /settings to continue."
        )
        return

    if query.data.startswith("setpitch:"):
        pitch = int(query.data.split(":", 1)[1])
        settings["pitch"] = pitch
        await query.edit_message_text(
            f"✅ Pitch set to {pitch:+d} Hz.\n\n"
            "Open /settings to continue."
        )
        return

    if query.data.startswith("setengine:"):
        engine = query.data.split(":", 1)[1]
        if engine in {"edge", "google"}:
            settings["engine"] = engine
            await query.edit_message_text(
                f"✅ Engine set to {engine}.\n\n"
                "Open /settings to continue."
            )


# =========================================================
# DOCUMENT CONVERSION
# =========================================================

async def convert_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    filename: str,
):
    message = update.effective_message
    user_id = update.effective_user.id
    settings = settings_for(user_id).copy()

    folder = Path(
        tempfile.mkdtemp(
            prefix=f"user-{user_id}-",
            dir=WORK_ROOT,
        )
    )

    status = await message.reply_text("📥 Downloading file…")

    try:
        tg_file = await context.bot.get_file(
            message.document.file_id
        )

        input_path = folder / Path(filename).name

        # Local Bot API returns a server-side absolute path.
        # The workflow must mount /var/lib/telegram-bot-api
        # and /tmp into the runner at the same host paths.
        if (
            LOCAL_BOT_API
            and tg_file.file_path
            and os.path.isabs(tg_file.file_path)
        ):
            source_path = Path(tg_file.file_path)

            if not source_path.is_file():
                raise FileNotFoundError(
                    "Local Bot API returned a file path that is not "
                    "visible to the runner: "
                    f"{source_path}. Check the shared Docker volume mounts."
                )

            shutil.copy2(source_path, input_path)

        else:
            await tg_file.download_to_drive(
                custom_path=str(input_path)
            )

        await status.edit_text("📖 Extracting text…")

        text = extract_text(input_path)

        if not text.strip():
            raise ValueError(
                "No readable text found. Scanned PDFs need OCR."
            )

        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(
                f"Text has {len(text):,} characters; maximum is "
                f"{MAX_TEXT_CHARS:,}."
            )

        merged = await generate_audio(
            text,
            folder,
            settings,
            status,
            user_id,
        )

        await status.edit_text("✂️ Preparing audio parts…")
        parts = await split_audio(merged, folder)

        base = safe_name(
            settings["name"]
            if settings["name"] != "Audiobook"
            else Path(filename).stem
        )

        USER_PROGRESS[user_id] = {
            "stage": "Sending",
            "done": 0,
            "total": len(parts),
        }

        for index, part in enumerate(parts, start=1):
            if part.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise RuntimeError(
                    f"Part {index} exceeds MAX_UPLOAD_MB. "
                    "Reduce SEND_PART_MB."
                )

            output_name = (
                f"{base}.mp3"
                if len(parts) == 1
                else f"{base}_part_{index:03d}.mp3"
            )

            await status.edit_text(
                f"📤 Sending audio {index}/{len(parts)}…"
            )

            with part.open("rb") as audio:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio,
                    filename=output_name,
                    title=Path(output_name).stem[:64],
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=60,
                    pool_timeout=60,
                )

            # Clear each sent part immediately.
            if part != merged:
                part.unlink(missing_ok=True)

            USER_PROGRESS[user_id] = {
                "stage": "Sending",
                "done": index,
                "total": len(parts),
            }

        await status.edit_text(
            f"✅ Finished! Sent {len(parts)} audio file(s).\n"
            "Temporary conversion files will now be cleared."
        )

    except asyncio.CancelledError:
        try:
            await status.edit_text(
                "⛔ Conversion cancelled. Clearing temporary files."
            )
        except TelegramError:
            pass
        raise

    except Exception as exc:
        log.exception("Conversion failed")
        error_text = (
            f"❌ Conversion failed: {type(exc).__name__}: "
            f"{str(exc)[:1000]}"
        )

        try:
            await status.edit_text(error_text)
        except TelegramError:
            await message.reply_text(error_text)

    finally:
        shutil.rmtree(folder, ignore_errors=True)
        USER_PROGRESS.pop(user_id, None)


async def document_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user_id = update.effective_user.id
    document = message.document

    existing = USER_TASKS.get(user_id)

    if existing and not existing.done():
        await message.reply_text(
            "A conversion is already running. Use /cancel first."
        )
        return

    filename = document.file_name or "input.txt"
    ext = Path(filename).suffix.lower()

    if ext not in {
        ".txt", ".text", ".md", ".pdf", ".docx", ".epub"
    }:
        await message.reply_text(
            "Supported formats: TXT, MD, PDF, DOCX, EPUB."
        )
        return

    if (
        document.file_size
        and document.file_size > MAX_INPUT_MB * 1024 * 1024
    ):
        await message.reply_text(
            f"File exceeds the configured {MAX_INPUT_MB} MB limit."
        )
        return

    task = asyncio.create_task(
        convert_document(update, context, filename)
    )
    USER_TASKS[user_id] = task

    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if USER_TASKS.get(user_id) is task:
            USER_TASKS.pop(user_id, None)


async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    log.error(
        "Unhandled bot error",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():
    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_BOT_API:
        builder = (
            builder
            .base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
        log.info(
            "Local Bot API enabled at %s",
            LOCAL_BOT_API_URL,
        )

    app = builder.build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("speed", speed_cmd))
    app.add_handler(CommandHandler("pitch", pitch_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("demo", demo_cmd))

    app.add_handler(CallbackQueryHandler(settings_callback))
    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            document_handler,
        )
    )
    app.add_error_handler(error_handler)

    log.info(
        "Bot starting | Local API=%s | part target=%s MB",
        LOCAL_BOT_API,
        SEND_PART_MB,
    )

    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
