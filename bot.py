
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

from gtts import gTTS
from pypdf import PdfReader
from docx import Document
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tts-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
LOCAL_BOT_API = os.getenv("LOCAL_BOT_API", "0") == "1"
LOCAL_BOT_API_URL = os.getenv(
    "LOCAL_BOT_API_URL", "http://127.0.0.1:8081"
).rstrip("/")

MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1900" if LOCAL_BOT_API else "50"))
MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "1900" if LOCAL_BOT_API else "50"))
SEND_PART_MB = int(os.getenv("SEND_PART_MB", "1800" if LOCAL_BOT_API else "45"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "20000000"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))

WORK_ROOT = Path("/tmp/telegram-tts-work")
WORK_ROOT.mkdir(parents=True, exist_ok=True)

VOICES = {
    "madhur": ("hi-IN-MadhurNeural", "Hindi male — Madhur"),
    "swara": ("hi-IN-SwaraNeural", "Hindi female — Swara"),
    "guy": ("en-US-GuyNeural", "English male — Guy"),
    "jenny": ("en-US-JennyNeural", "English female — Jenny"),
}
SETTINGS = {}
TASKS = {}


def cfg(uid):
    return SETTINGS.setdefault(uid, {
        "voice": "hi-IN-MadhurNeural",
        "speed": 1.1,
        "pitch": 0,
        "engine": "edge",
        "name": "Audiobook",
    })


def safe_name(value):
    value = Path(value or "Audiobook").stem
    value = re.sub(r"[^A-Za-z0-9 _().-]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value or "Audiobook")[:100]


def get_proxy():
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    user = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()

    if not host or not port:
        return None

    auth = ""
    if user and password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"

    return f"http://{auth}{host}:{port}"


def extract_text(path):
    ext = path.suffix.lower()

    if ext in {".txt", ".text", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")

    if ext == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    if ext == ".docx":
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)

    if ext == ".epub":
        book = epub.read_epub(str(path))
        sections = []
        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                sections.append(soup.get_text(" ", strip=True))
        return "\n\n".join(sections)

    raise ValueError("Supported formats: TXT, MD, PDF, DOCX, EPUB.")


def split_text(text, limit=CHUNK_CHARS):
    text = re.sub(r"\r\n?", "\n", text).strip()
    result = []

    while text:
        if len(text) <= limit:
            result.append(text)
            break

        cut = max(
            text.rfind("\n", 0, limit),
            text.rfind(". ", 0, limit),
            text.rfind(" ", 0, limit),
        )
        if cut < limit // 2:
            cut = limit

        result.append(text[:cut].strip())
        text = text[cut:].strip()

    return [part for part in result if part]


async def make_edge(text, path, settings):
    kwargs = {}
    proxy = get_proxy()
    if proxy:
        kwargs["proxy"] = proxy

    tts = edge_tts.Communicate(
        text=text,
        voice=settings["voice"],
        rate=f"{round((settings['speed'] - 1) * 100):+d}%",
        pitch=f"{settings['pitch']:+d}Hz",
        **kwargs,
    )
    await tts.save(str(path))


async def make_google(text, path, settings):
    proxy = get_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    lang = "hi" if settings["voice"].startswith("hi-") else "en"
    tts = gTTS(text=text, lang=lang, slow=False, proxies=proxies)
    await asyncio.to_thread(tts.save, str(path))


async def make_audio(text, folder, settings, status):
    chunks = split_text(text)
    if not chunks:
        raise ValueError("No readable text was found.")

    pieces = []
    for index, chunk in enumerate(chunks, 1):
        out = folder / f"chunk_{index:06d}.mp3"

        if settings["engine"] == "google":
            await make_google(chunk, out, settings)
        else:
            await make_edge(chunk, out, settings)

        pieces.append(out)
        if index == 1 or index % 5 == 0 or index == len(chunks):
            await status.edit_text(
                f"🎙️ Generating audio: {index}/{len(chunks)}"
            )

    listing = folder / "concat.txt"
    listing.write_text(
        "".join(f"file '{p.as_posix()}'\n" for p in pieces),
        encoding="utf-8",
    )

    merged = folder / "merged.mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-y", str(merged),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, error = await proc.communicate()

    if proc.returncode:
        raise RuntimeError(
            "FFmpeg merge failed: " + error.decode(errors="replace")[-1000:]
        )

    return merged


async def split_audio(source, folder):
    max_bytes = SEND_PART_MB * 1024 * 1024

    if source.stat().st_size <= max_bytes:
        return [source]

    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(source),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    output, _ = await probe.communicate()
    duration = float(output.decode().strip() or "0")

    if duration <= 0:
        raise RuntimeError("Could not determine audio duration.")

    segment_seconds = max(
        20, duration * max_bytes / source.stat().st_size * 0.80
    )

    for _ in range(10):
        for old in folder.glob("part_*.mp3"):
            old.unlink(missing_ok=True)

        pattern = str(folder / "part_%03d.mp3")
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(source),
            "-f", "segment", "-segment_time", str(segment_seconds),
            "-c", "copy", "-reset_timestamps", "1",
            "-y", pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, error = await proc.communicate()

        if proc.returncode:
            raise RuntimeError(
                "Audio splitting failed: " +
                error.decode(errors="replace")[-1000:]
            )

        parts = sorted(folder.glob("part_*.mp3"))
        if parts and all(p.stat().st_size <= max_bytes for p in parts):
            return parts

        segment_seconds *= 0.65

    raise RuntimeError("Could not create parts below SEND_PART_MB.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎧 TTS Bot ready!\nSend TXT, MD, PDF, DOCX or EPUB.\nUse /help for commands."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — start\n/help — commands\n/settings — settings\n"
        "/voice madhur|swara|guy|jenny — choose voice\n"
        "/speed 1.5 — speed from 1.0 to 2.0\n"
        "/pitch 5 — pitch from -20 to 20\n"
        "/engine edge|google — engine\n"
        "/name My Audiobook — output filename\n"
        "/demo — voice demo\n/status — conversion status\n/cancel — cancel conversion"
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    voice = next((v[1] for v in VOICES.values() if v[0] == s["voice"]), s["voice"])
    await update.effective_message.reply_text(
        f"Voice: {voice}\nSpeed: {s['speed']}x\nPitch: {s['pitch']} Hz\n"
        f"Engine: {s['engine']}\nFilename: {s['name']}\n"
        f"Local API: {LOCAL_BOT_API}\nPart target: {SEND_PART_MB} MB"
    )


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        key = context.args[0].lower()
        if key not in VOICES:
            await update.effective_message.reply_text(
                "Use /voice madhur, swara, guy or jenny."
            )
            return
        cfg(uid)["voice"] = VOICES[key][0]
        await update.effective_message.reply_text(f"Voice set: {VOICES[key][1]}")
        return

    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"voice:{key}")]
        for key, (_, label) in VOICES.items()
    ]
    await update.effective_message.reply_text(
        "Choose voice:", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def voice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.split(":", 1)[1]
    if key in VOICES:
        cfg(query.from_user.id)["voice"] = VOICES[key][0]
        await query.edit_message_text(f"Voice set: {VOICES[key][1]}")


async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current speed: {s['speed']}x")
        return
    try:
        value = float(context.args[0])
        if not 1 <= value <= 2:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Use /speed 1.0 to /speed 2.0.")
        return
    s["speed"] = value
    await update.effective_message.reply_text(f"Speed set to {value}x.")


async def pitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current pitch: {s['pitch']} Hz")
        return
    try:
        value = int(context.args[0])
        if not -20 <= value <= 20:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Use /pitch -20 to /pitch 20.")
        return
    s["pitch"] = value
    await update.effective_message.reply_text(f"Pitch set to {value:+d} Hz.")


async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Engine: {s['engine']}")
        return
    value = context.args[0].lower()
    if value not in {"edge", "google"}:
        await update.effective_message.reply_text("Use /engine edge or /engine google.")
        return
    s["engine"] = value
    await update.effective_message.reply_text(f"Engine set to {value}.")


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(
            f"Current name: {s['name']}\nSet with /name My Audiobook"
        )
        return
    s["name"] = safe_name(" ".join(context.args))
    await update.effective_message.reply_text(f"Audio name: {s['name']}.mp3")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = TASKS.get(update.effective_user.id)
    await update.effective_message.reply_text(
        "Conversion running." if task and not task.done() else "No active conversion."
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = TASKS.get(update.effective_user.id)
    if not task or task.done():
        await update.effective_message.reply_text("No active conversion.")
        return
    task.cancel()
    await update.effective_message.reply_text("Cancellation requested.")


async def demo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    folder = Path(tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT))
    try:
        s = cfg(update.effective_user.id).copy()
        output = folder / f"{safe_name(s['name'])}_demo.mp3"
        await make_edge("Hello! नमस्ते! This is a voice demo.", output, s)
        with output.open("rb") as audio:
            await update.effective_message.reply_audio(
                audio=audio, filename=output.name, title=output.stem
            )
    except Exception as exc:
        log.exception("Demo failed")
        await update.effective_message.reply_text(
            f"Demo failed: {type(exc).__name__}: {exc}"
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def convert(update: Update, context: ContextTypes.DEFAULT_TYPE, filename):
    message = update.effective_message
    uid = update.effective_user.id
    folder = Path(tempfile.mkdtemp(prefix=f"user-{uid}-", dir=WORK_ROOT))
    status = await message.reply_text("📥 Downloading file…")

    try:
        file = await context.bot.get_file(message.document.file_id)
        input_path = folder / Path(filename).name

        # With local_mode=True, the Local Bot API may return an absolute path.
        # The shared /tmp volume lets the runner access the server's temp files.
        await file.download_to_drive(custom_path=str(input_path))

        await status.edit_text("📖 Extracting text…")
        text = extract_text(input_path)

        if not text.strip():
            raise ValueError("No readable text found in this file.")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"Text exceeds {MAX_TEXT_CHARS:,} characters.")

        s = cfg(uid).copy()
        merged = await make_audio(text, folder, s, status)
        parts = await split_audio(merged, folder)
        base = safe_name(s["name"] if s["name"] != "Audiobook" else Path(filename).stem)

        for index, part in enumerate(parts, 1):
            if part.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise RuntimeError("Audio part exceeds configured upload limit.")

            out_name = (
                f"{base}.mp3" if len(parts) == 1
                else f"{base}_part_{index:03d}.mp3"
            )
            await status.edit_text(f"📤 Sending audio {index}/{len(parts)}…")

            with part.open("rb") as audio:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=audio,
                    filename=out_name,
                    title=Path(out_name).stem[:64],
                    read_timeout=600,
                    write_timeout=600,
                    connect_timeout=60,
                    pool_timeout=60,
                )

            if part != merged:
                part.unlink(missing_ok=True)

        await status.edit_text(f"✅ Done. Sent {len(parts)} audio file(s).")

    except Exception as exc:
        log.exception("Conversion failed")
        try:
            await status.edit_text(
                f"❌ Conversion failed: {type(exc).__name__}: {str(exc)[:1000]}"
            )
        except Exception:
            await message.reply_text(
                f"❌ Conversion failed: {type(exc).__name__}: {str(exc)[:1000]}"
            )
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    uid = update.effective_user.id

    if uid in TASKS and not TASKS[uid].done():
        await message.reply_text("A conversion is already running. Use /cancel.")
        return

    document = message.document
    filename = document.file_name or "input.txt"
    ext = Path(filename).suffix.lower()

    if ext not in {".txt", ".text", ".md", ".pdf", ".docx", ".epub"}:
        await message.reply_text("Supported formats: TXT, MD, PDF, DOCX, EPUB.")
        return

    if document.file_size and document.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(f"File exceeds {MAX_INPUT_MB} MB input limit.")
        return

    task = asyncio.create_task(convert(update, context, filename))
    TASKS[uid] = task
    try:
        await task
    except asyncio.CancelledError:
        await message.reply_text("Conversion cancelled.")
    finally:
        TASKS.pop(uid, None)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled bot error", exc_info=context.error)


def main():
    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_BOT_API:
        builder = (
            builder
            .base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
        log.info("Using Local Bot API at %s", LOCAL_BOT_API_URL)

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("speed", speed_cmd))
    app.add_handler(CommandHandler("pitch", pitch_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("demo", demo_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(voice_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_error_handler(error_handler)

    log.info("Bot starting; local API=%s; part target=%s MB", LOCAL_BOT_API, SEND_PART_MB)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
