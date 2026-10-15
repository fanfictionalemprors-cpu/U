#!/usr/bin/env python3
"""Telegram TTS bot with Local Bot API support, configurable output names and cleanup."""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import edge_tts
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

try:
    from gtts import gTTS
except Exception:
    gTTS = None

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None

try:
    from docx import Document
except Exception:
    Document = None

try:
    from ebooklib import epub, ITEM_DOCUMENT
    from bs4 import BeautifulSoup
except Exception:
    epub = None
    ITEM_DOCUMENT = None
    BeautifulSoup = None

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("telegram-tts")

BOT_TOKEN = os.environ["BOT_TOKEN"]

LOCAL_BOT_API = os.getenv(
    "LOCAL_BOT_API", "0"
).lower() in {"1", "true", "yes"}

LOCAL_BOT_API_URL = os.getenv(
    "LOCAL_BOT_API_URL",
    "http://127.0.0.1:8081"
).rstrip("/")

MAX_UPLOAD_MB = int(
    os.getenv("MAX_UPLOAD_MB", "1900" if LOCAL_BOT_API else "50")
)

SEND_PART_MB = int(
    os.getenv("SEND_PART_MB", "1800" if LOCAL_BOT_API else "45")
)

MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "20000000"))

MAX_INPUT_MB = int(
    os.getenv("MAX_INPUT_MB", "1900" if LOCAL_BOT_API else "50")
)

CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))

DEFAULT_VOICE = os.getenv(
    "DEFAULT_VOICE", "hi-IN-MadhurNeural"
)

DEFAULT_SPEED = float(os.getenv("DEFAULT_SPEED", "1.1"))
DEFAULT_PITCH = int(os.getenv("DEFAULT_PITCH", "0"))
DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "edge")

WORK_ROOT = Path(
    os.getenv("TTS_WORKDIR", "/tmp/telegram-tts-work")
)
WORK_ROOT.mkdir(parents=True, exist_ok=True)

VOICES = {
    "madhur": ("hi-IN-MadhurNeural", "Hindi male — Madhur"),
    "swara": ("hi-IN-SwaraNeural", "Hindi female — Swara"),
    "guy": ("en-US-GuyNeural", "English male — Guy"),
    "jenny": ("en-US-JennyNeural", "English female — Jenny"),
}

# Settings remain in memory while the current runner is alive.
USER_SETTINGS = {}
USER_TASKS = {}


def settings_for(uid):
    return USER_SETTINGS.setdefault(uid, {
        "voice": DEFAULT_VOICE,
        "speed": DEFAULT_SPEED,
        "pitch": DEFAULT_PITCH,
        "engine": DEFAULT_ENGINE,
        "name": "Audiobook",
    })


def safe_name(name):
    name = Path(name or "Audiobook").stem
    name = re.sub(r"[^A-Za-z0-9 _().-]+", "", name).strip(" ._-")
    name = re.sub(r"\s+", " ", name)
    return (name or "Audiobook")[:100]


def proxy_url():
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    user = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()

    if not host or not port:
        return None

    from urllib.parse import quote

    auth = ""
    if user and password:
        auth = (
            f"{quote(user, safe='')}:"
            f"{quote(password, safe='')}@"
        )

    return f"http://{auth}{host}:{port}"


def speed_to_rate(speed):
    pct = round((speed - 1.0) * 100)
    return f"{pct:+d}%"


def pitch_to_text(pitch):
    return f"{pitch:+d}Hz"


def extract_text(path):
    suffix = path.suffix.lower()

    if suffix in {".txt", ".text", ".md"}:
        return path.read_text(
            encoding="utf-8",
            errors="replace"
        )

    if suffix == ".pdf":
        if PdfReader is None:
            raise RuntimeError("PDF support missing (pypdf).")

        reader = PdfReader(str(path))
        return "\n\n".join(
            page.extract_text() or ""
            for page in reader.pages
        )

    if suffix == ".docx":
        if Document is None:
            raise RuntimeError("DOCX support missing (python-docx).")

        doc = Document(str(path))
        return "\n".join(
            paragraph.text for paragraph in doc.paragraphs
        )

    if suffix == ".epub":
        if epub is None:
            raise RuntimeError(
                "EPUB support missing (EbookLib/BeautifulSoup)."
            )

        book = epub.read_epub(str(path))
        parts = []

        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(
                    item.get_content(),
                    "html.parser"
                )
                parts.append(soup.get_text(" ", strip=True))

        return "\n\n".join(parts)

    raise ValueError(
        "Supported files: TXT, MD, PDF, DOCX, EPUB."
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


async def edge_chunk(text, out_path, voice, speed, pitch):
    proxy = proxy_url()
    kwargs = {}

    if proxy:
        kwargs["proxy"] = proxy

    comm = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=speed_to_rate(speed),
        pitch=pitch_to_text(pitch),
        **kwargs
    )

    await comm.save(str(out_path))


async def google_chunk(text, out_path, voice):
    if gTTS is None:
        raise RuntimeError("Google TTS library unavailable.")

    lang = "hi" if voice.startswith("hi-") else "en"
    proxy = proxy_url()

    proxies = (
        {"http": proxy, "https": proxy}
        if proxy else None
    )

    tts = gTTS(
        text=text,
        lang=lang,
        slow=False,
        proxies=proxies
    )

    await asyncio.to_thread(
        tts.save,
        str(out_path)
    )


async def run_tts(text, workdir, opts, progress_cb):
    chunks = split_text(text)

    if not chunks:
        raise ValueError("No readable text was found in the file.")

    pieces = []

    for idx, chunk in enumerate(chunks, 1):
        if opts["cancel"].is_set():
            raise asyncio.CancelledError()

        path = workdir / f"chunk_{idx:06d}.mp3"

        if opts["engine"] == "google":
            await google_chunk(
                chunk, path, opts["voice"]
            )
        else:
            await edge_chunk(
                chunk,
                path,
                opts["voice"],
                opts["speed"],
                opts["pitch"],
            )

        pieces.append(path)
        await progress_cb(idx, len(chunks))

    list_file = workdir / "concat.txt"

    list_file.write_text(
        "".join(
            f"file '{p.as_posix()}'\n"
            for p in pieces
        ),
        encoding="utf-8"
    )

    merged = workdir / "merged.mp3"

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-c", "copy",
        "-y", str(merged),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )

    _, err = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            "FFmpeg merge failed: "
            + err.decode(errors="replace")[-1200:]
        )

    return merged


async def split_mp3(merged, workdir, max_mb):
    max_bytes = max(1, max_mb) * 1024 * 1024

    if merged.stat().st_size <= max_bytes:
        return [merged]

    probe = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(merged),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    out, _ = await probe.communicate()
    duration = float(out.decode().strip() or "0")

    if duration <= 0:
        raise RuntimeError(
            "Could not determine audio duration for splitting."
        )

    size = merged.stat().st_size
    segment_seconds = max(
        30,
        duration * max_bytes / size * 0.88
    )

    for attempt in range(8):
        out_pattern = str(workdir / "part_%03d.mp3")

        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-i", str(merged),
            "-f", "segment",
            "-segment_time", str(segment_seconds),
            "-c", "copy",
            "-reset_timestamps", "1",
            "-y", out_pattern,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

        _, err = await proc.communicate()
        parts = sorted(workdir.glob("part_*.mp3"))

        if proc.returncode != 0:
            raise RuntimeError(
                "FFmpeg splitting failed: "
                + err.decode(errors="replace")[-1200:]
            )

        if parts and all(
            p.stat().st_size <= max_bytes for p in parts
        ):
            return parts

        for part in parts:
            part.unlink(missing_ok=True)

        segment_seconds *= 0.70

    raise RuntimeError(
        "Could not split audio below configured part size. "
        "Lower SEND_PART_MB."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎧 TTS Bot ready!\n\n"
        "Send a TXT, EPUB, PDF or DOCX file to convert it to MP3.\n"
        "Use /help to see commands and /settings to view your settings."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Commands:\n"
        "/start — start bot\n"
        "/help — help\n"
        "/settings — current settings\n"
        "/voice — choose voice; /voice madhur|swara|guy|jenny\n"
        "/speed 1.1 — speed from 1.0 to 2.0\n"
        "/pitch 0 — pitch from -20 to +20 Hz\n"
        "/engine edge|google — TTS engine\n"
        "/name My Audiobook — output audio filename\n"
        "/cancel — cancel current conversion\n"
        "/status — current job status\n"
        "/demo — generate a short voice demo\n\n"
        "Send TXT/MD/PDF/DOCX/EPUB to convert.\n"
        "Temporary input, chunks and output are deleted after sending."
    )


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = settings_for(update.effective_user.id)

    voice_name = next(
        (v[1] for v in VOICES.values() if v[0] == s["voice"]),
        s["voice"]
    )

    await update.effective_message.reply_text(
        f"Voice: {voice_name}\n"
        f"Speed: {s['speed']:.1f}x\n"
        f"Pitch: {s['pitch']} Hz\n"
        f"Engine: {s['engine']}\n"
        f"Output name: {s['name']}\n"
        f"Local Bot API: {'ON' if LOCAL_BOT_API else 'OFF'}\n"
        f"Max output part: {SEND_PART_MB} MB"
    )


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    if context.args:
        key = context.args[0].lower()

        if key not in VOICES:
            await update.effective_message.reply_text(
                "Use: /voice madhur, swara, guy or jenny"
            )
            return

        settings_for(uid)["voice"] = VOICES[key][0]

        await update.effective_message.reply_text(
            f"Voice set: {VOICES[key][1]}"
        )
        return

    keyboard = [
        [
            InlineKeyboardButton(
                label,
                callback_data=f"voice:{key}"
            )
        ]
        for key, (_, label) in VOICES.items()
    ]

    await update.effective_message.reply_text(
        "Choose voice:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.startswith("voice:"):
        key = q.data.split(":", 1)[1]

        if key in VOICES:
            settings_for(q.from_user.id)["voice"] = VOICES[key][0]
            await q.edit_message_text(
                f"Voice set: {VOICES[key][1]}"
            )


async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            "Current speed: %.1fx\nUse /speed 1.1 (range 1.0–2.0)."
            % settings_for(update.effective_user.id)["speed"]
        )
        return

    try:
        speed = float(context.args[0])
        if not 1.0 <= speed <= 2.0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Speed must be between 1.0 and 2.0, e.g. /speed 1.3"
        )
        return

    settings_for(update.effective_user.id)["speed"] = speed

    await update.effective_message.reply_text(
        f"Speed set to {speed:.1f}x."
    )


async def pitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.effective_message.reply_text(
            f"Current pitch: {settings_for(update.effective_user.id)['pitch']} Hz. "
            "Use /pitch -5 (range -20 to 20)."
        )
        return

    try:
        pitch = int(context.args[0])
        if not -20 <= pitch <= 20:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Pitch must be an integer from -20 to 20, e.g. /pitch 5"
        )
        return

    settings_for(update.effective_user.id)["pitch"] = pitch

    await update.effective_message.reply_text(
        f"Pitch set to {pitch:+d} Hz."
    )


async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = settings_for(update.effective_user.id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current engine: {s['engine']}. "
            "Use /engine edge or /engine google."
        )
        return

    engine = context.args[0].lower()

    if engine not in {"edge", "google"}:
        await update.effective_message.reply_text(
            "Use /engine edge or /engine google."
        )
        return

    s["engine"] = engine

    await update.effective_message.reply_text(
        f"TTS engine set to {engine}."
    )


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = settings_for(update.effective_user.id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current output name: {s['name']}\n"
            "Set with /name My Audiobook"
        )
        return

    s["name"] = safe_name(" ".join(context.args))

    await update.effective_message.reply_text(
        f"Output filename set to: {s['name']}.mp3"
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = USER_TASKS.get(update.effective_user.id)

    if not task or task.done():
        await update.effective_message.reply_text(
            "No active conversion to cancel."
        )
        return

    task.cancel()

    await update.effective_message.reply_text(
        "Cancellation requested. Temporary files will be removed."
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = USER_TASKS.get(update.effective_user.id)

    await update.effective_message.reply_text(
        "Conversion running."
        if task and not task.done()
        else "No active conversion."
    )


async def demo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = settings_for(uid)

    tmp = Path(tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT))

    try:
        out = tmp / f"{safe_name(s['name'])}_demo.mp3"

        await edge_chunk(
            "Hello! नमस्ते! This is your selected text to speech voice demo.",
            out,
            s["voice"],
            s["speed"],
            s["pitch"],
        )

        with out.open("rb") as f:
            await update.effective_message.reply_audio(
                audio=f,
                filename=out.name,
                title=out.stem
            )

    except Exception as exc:
        await update.effective_message.reply_text(
            f"Demo failed: {exc}"
        )

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def process_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    uid = update.effective_user.id
    doc = msg.document

    if uid in USER_TASKS and not USER_TASKS[uid].done():
        await msg.reply_text(
            "You already have a conversion running. Use /cancel first."
        )
        return

    if not doc:
        return

    filename = doc.file_name or "input.txt"
    ext = Path(filename).suffix.lower()

    if ext not in {".txt", ".text", ".md", ".pdf", ".docx", ".epub"}:
        await msg.reply_text(
            "Supported formats: TXT, MD, PDF, DOCX, EPUB."
        )
        return

    if doc.file_size and doc.file_size > MAX_INPUT_MB * 1024 * 1024:
        await msg.reply_text(
            f"Input exceeds configured {MAX_INPUT_MB} MB limit."
        )
        return

    task = asyncio.create_task(
        convert_and_send(update, context, filename)
    )
    USER_TASKS[uid] = task

    try:
        await task
    except asyncio.CancelledError:
        await msg.reply_text("Conversion cancelled.")
    finally:
        USER_TASKS.pop(uid, None)


async def convert_and_send(update, context, filename):
    msg = update.effective_message
    uid = update.effective_user.id
    s = settings_for(uid).copy()

    workdir = Path(
        tempfile.mkdtemp(prefix=f"user-{uid}-", dir=WORK_ROOT)
    )

    status = await msg.reply_text("📥 Downloading file…")

    try:
        tgfile = await context.bot.get_file(msg.document.file_id)

        input_path = workdir / Path(filename).name

        await tgfile.download_to_drive(
            custom_path=str(input_path)
        )

        await status.edit_text("📖 Extracting text…")

        text = extract_text(input_path)

        if not text.strip():
            raise ValueError(
                "No text extracted. The file may be scanned or image-only."
            )

        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(
                f"Extracted text is {len(text):,} characters; "
                f"configured maximum is {MAX_TEXT_CHARS:,}."
            )

        cancel_event = asyncio.Event()
        opts = {**s, "cancel": cancel_event}
        started = time.monotonic()

        async def progress(done, total):
            if done == 1 or done % 5 == 0 or done == total:
                elapsed = max(
                    1, int(time.monotonic() - started)
                )
                try:
                    await status.edit_text(
                        f"🎙️ Generating audio: {done}/{total} chunks\n"
                        f"Elapsed: {elapsed}s"
                    )
                except Exception:
                    pass

        merged = await run_tts(
            text, workdir, opts, progress
        )

        await status.edit_text("✂️ Preparing audio parts…")

        parts = await split_mp3(
            merged, workdir, SEND_PART_MB
        )

        base = safe_name(
            s["name"]
            if s["name"] != "Audiobook"
            else Path(filename).stem
        )

        total = len(parts)

        for idx, part in enumerate(parts, 1):
            if part.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise RuntimeError(
                    f"Part {idx} is larger than configured upload limit. "
                    "Lower SEND_PART_MB."
                )

            out_name = (
                f"{base}.mp3"
                if total == 1
                else f"{base}_part_{idx:03d}.mp3"
            )

            await status.edit_text(
                f"📤 Sending audio {idx}/{total}…"
            )

            with part.open("rb") as f:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id,
                    audio=f,
                    filename=out_name,
                    title=Path(out_name).stem[:64],
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=30,
                    pool_timeout=30,
                )

            # Remove each sent part immediately.
            if part != merged:
                part.unlink(missing_ok=True)

        await status.edit_text(
            f"✅ Finished. Sent {total} audio file(s). "
            "Temporary files are being cleared."
        )

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log.exception("Conversion failed")

        try:
            await status.edit_text(
                f"❌ Conversion failed: {str(exc)[:1500]}"
            )
        except Exception:
            await msg.reply_text(
                f"❌ Conversion failed: {str(exc)[:1500]}"
            )

    finally:
        shutil.rmtree(workdir, ignore_errors=True)

        try:
            for p in WORK_ROOT.iterdir():
                if (
                    p.is_dir()
                    and p.name.startswith(("user-", "tts-demo-"))
                ):
                    if time.time() - p.stat().st_mtime > 86400:
                        shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error(
        "Unhandled exception",
        exc_info=context.error
    )


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
            LOCAL_BOT_API_URL
        )

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("speed", speed_cmd))
    app.add_handler(CommandHandler("pitch", pitch_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("demo", demo_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(
        MessageHandler(filters.Document.ALL, process_document)
    )

    app.add_error_handler(error_handler)

    log.info(
        "TTS bot starting. Local API=%s, send part=%s MB",
        LOCAL_BOT_API,
        SEND_PART_MB,
    )

    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
