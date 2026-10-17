#!/usr/bin/env python3
import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import edge_tts
from gtts import gTTS
from pypdf import PdfReader
from docx import Document
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("tts-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
LOCAL_BOT_API = os.getenv("LOCAL_BOT_API", "0").lower() in {"1", "true", "yes"}
LOCAL_BOT_API_URL = os.getenv("LOCAL_BOT_API_URL", "http://127.0.0.1:8081").rstrip("/")
LOCAL_API_CONTAINER = os.getenv("LOCAL_API_CONTAINER", "telegram-bot-api")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1900" if LOCAL_BOT_API else "50"))
MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "1900" if LOCAL_BOT_API else "50"))
SEND_PART_MB = int(os.getenv("SEND_PART_MB", "1800" if LOCAL_BOT_API else "45"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "20000000"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))
WORK_ROOT = Path(os.getenv("TTS_WORKDIR", "/tmp/telegram-tts-work"))
WORK_ROOT.mkdir(parents=True, exist_ok=True)

VOICES = {
    "madhur": ("hi-IN-MadhurNeural", "Hindi Male — Madhur"),
    "swara": ("hi-IN-SwaraNeural", "Hindi Female — Swara"),
    "guy": ("en-US-GuyNeural", "English Male — Guy"),
    "jenny": ("en-US-JennyNeural", "English Female — Jenny"),
}
SETTINGS = {}
TASKS = {}
PROGRESS = {}


def cfg(uid):
    return SETTINGS.setdefault(uid, {
        "voice": os.getenv("DEFAULT_VOICE", "hi-IN-MadhurNeural"),
        "rate": float(os.getenv("DEFAULT_RATE", "1.1")),
        "pitch": int(os.getenv("DEFAULT_PITCH", "0")),
        "engine": os.getenv("DEFAULT_ENGINE", "edge").lower(),
        "name": "Audiobook",
    })


def safe_name(value):
    value = Path(value or "Audiobook").stem
    value = re.sub(r"[^A-Za-z0-9 _().-]+", "", value)
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value or "Audiobook")[:100]


def edge_rate(rate):
    return f"{round((rate - 1.0) * 100):+d}%"


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
        parts = []
        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                parts.append(soup.get_text(" ", strip=True))
        return "\n\n".join(parts)
    raise ValueError("Supported formats: TXT, MD, PDF, DOCX, EPUB.")


def split_text(text, limit=CHUNK_CHARS):
    text = re.sub(r"\r\n?", "\n", text).strip()
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = max(text.rfind("\n", 0, limit), text.rfind(". ", 0, limit), text.rfind(" ", 0, limit))
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return [x for x in chunks if x]


async def make_edge(text, path, s):
    tts = edge_tts.Communicate(
        text=text, voice=s["voice"], rate=edge_rate(s["rate"]),
        pitch=f"{s['pitch']:+d}Hz"
    )
    await tts.save(str(path))


async def make_google(text, path, s):
    lang = "hi" if s["voice"].startswith("hi-") else "en"
    await asyncio.to_thread(gTTS(text=text, lang=lang, slow=False).save, str(path))
    # Google TTS has no native rate/pitch controls. Apply FFmpeg processing.
    if s["rate"] != 1.0 or s["pitch"] != 0:
        adjusted = path.with_name(path.stem + "_adjusted.mp3")
        factor = 2 ** (s["pitch"] / 1200)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-filter:a", f"asetrate=44100*{factor:.6f},aresample=44100,atempo={s['rate']:.6f}",
            "-y", str(adjusted), stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            adjusted.unlink(missing_ok=True)
            raise RuntimeError("Google audio processing failed: " + err.decode(errors="replace")[-800:])
        path.unlink(missing_ok=True)
        adjusted.rename(path)


async def generate_audio(text, folder, s, status, uid):
    chunks = split_text(text)
    if not chunks:
        raise ValueError("No readable text found.")
    pieces = []
    PROGRESS[uid] = {"stage": "Generating", "done": 0, "total": len(chunks)}
    for i, chunk in enumerate(chunks, 1):
        out = folder / f"chunk_{i:06d}.mp3"
        if s["engine"] == "google":
            await make_google(chunk, out, s)
        else:
            await make_edge(chunk, out, s)
        pieces.append(out)
        PROGRESS[uid] = {"stage": "Generating", "done": i, "total": len(chunks)}
        if i == 1 or i % 5 == 0 or i == len(chunks):
            try:
                await status.edit_text(f"🎙️ Generating audio: {i}/{len(chunks)} chunks")
            except TelegramError:
                pass
    concat = folder / "concat.txt"
    concat.write_text("".join(f"file '{x.as_posix()}'\n" for x in pieces), encoding="utf-8")
    merged = folder / "merged.mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-c", "copy", "-y", str(merged),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode:
        raise RuntimeError("FFmpeg merge failed: " + err.decode(errors="replace")[-1000:])
    return merged


async def split_audio(source, folder):
    max_bytes = SEND_PART_MB * 1024 * 1024
    if source.stat().st_size <= max_bytes:
        return [source]
    probe = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(source),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await probe.communicate()
    duration = float(out.decode().strip() or "0")
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration.")
    segment = max(20, duration * max_bytes / source.stat().st_size * 0.80)
    for _ in range(10):
        for old in folder.glob("part_*.mp3"):
            old.unlink(missing_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-f", "segment", "-segment_time", str(segment), "-c", "copy",
            "-reset_timestamps", "1", "-y", str(folder / "part_%03d.mp3"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode:
            raise RuntimeError("Audio split failed: " + err.decode(errors="replace")[-1000:])
        parts = sorted(folder.glob("part_*.mp3"))
        if parts and all(p.stat().st_size <= max_bytes for p in parts):
            return parts
        segment *= 0.65
    raise RuntimeError("Could not split audio under SEND_PART_MB; lower the value.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎧 TTS Bot ready!\nSend TXT, MD, PDF, DOCX or EPUB.\nUse /settings for voice, rate, pitch and filename."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — Start\n/help — Help\n/settings — Settings menu\n"
        "/voice madhur|swara|guy|jenny — Voice\n"
        "/rate 1.5 — Rate (1.0–2.0)\n/speed 1.5 — Rate alias\n"
        "/pitch 5 — Pitch (-20 to +20 Hz)\n/engine edge|google — Engine\n"
        "/name My Audiobook — Output filename\n/demo — Voice demo\n"
        "/status — Progress\n/cancel — Cancel conversion"
    )


def settings_text(uid):
    s = cfg(uid)
    label = next((v[1] for v in VOICES.values() if v[0] == s["voice"]), s["voice"])
    return (
        "⚙️ TTS Settings\n\n"
        f"Voice: {label}\nRate: {s['rate']:.2f}x\nPitch: {s['pitch']:+d} Hz\n"
        f"Engine: {s['engine']}\nAudio name: {s['name']}"
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Voice", callback_data="menu:voice"),
         InlineKeyboardButton("Rate", callback_data="menu:rate")],
        [InlineKeyboardButton("Pitch", callback_data="menu:pitch"),
         InlineKeyboardButton("Engine", callback_data="menu:engine")],
        [InlineKeyboardButton("Audio name help", callback_data="menu:name"),
         InlineKeyboardButton("Refresh", callback_data="menu:refresh")],
    ])


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        settings_text(update.effective_user.id), reply_markup=settings_keyboard()
    )


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        key = context.args[0].lower()
        if key not in VOICES:
            await update.effective_message.reply_text("Use madhur, swara, guy or jenny.")
            return
        cfg(uid)["voice"] = VOICES[key][0]
        await update.effective_message.reply_text(f"Voice set: {VOICES[key][1]}")
        return
    kb = [[InlineKeyboardButton(label, callback_data=f"voice:{key}")]
          for key, (_, label) in VOICES.items()]
    await update.effective_message.reply_text("Choose voice:", reply_markup=InlineKeyboardMarkup(kb))


async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current rate: {s['rate']:.2f}x. Use /rate 1.5 (1.0–2.0).")
        return
    try:
        value = float(context.args[0])
        if not 1.0 <= value <= 2.0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Rate must be 1.0–2.0, e.g. /rate 1.5")
        return
    s["rate"] = value
    await update.effective_message.reply_text(f"Rate set to {value:.2f}x.")


async def pitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current pitch: {s['pitch']:+d} Hz. Use /pitch -5 (-20 to +20).")
        return
    try:
        value = int(context.args[0])
        if not -20 <= value <= 20:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Pitch must be an integer from -20 to +20.")
        return
    s["pitch"] = value
    await update.effective_message.reply_text(f"Pitch set to {value:+d} Hz.")


async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Engine: {s['engine']}. Use /engine edge or /engine google.")
        return
    engine = context.args[0].lower()
    if engine not in {"edge", "google"}:
        await update.effective_message.reply_text("Use /engine edge or /engine google.")
        return
    s["engine"] = engine
    await update.effective_message.reply_text(f"Engine set to {engine}.")


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current name: {s['name']}\nUse /name My Audiobook")
        return
    s["name"] = safe_name(" ".join(context.args))
    await update.effective_message.reply_text(f"Audio name set to {s['name']}.mp3")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    task = TASKS.get(uid)
    p = PROGRESS.get(uid)
    if not task or task.done():
        await update.effective_message.reply_text("No active conversion.")
    elif p:
        await update.effective_message.reply_text(f"{p['stage']}: {p['done']}/{p['total']}")
    else:
        await update.effective_message.reply_text("Conversion running.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = TASKS.get(update.effective_user.id)
    if not task or task.done():
        await update.effective_message.reply_text("No active conversion.")
        return
    task.cancel()
    await update.effective_message.reply_text("Cancellation requested; temporary files will be cleared.")


async def demo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    folder = Path(tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT))
    try:
        s = cfg(uid).copy()
        output = folder / f"{safe_name(s['name'])}_demo.mp3"
        if s["engine"] == "google":
            await make_google("Hello! नमस्ते! This is a voice demo.", output, s)
        else:
            await make_edge("Hello! नमस्ते! This is a voice demo.", output, s)
        with output.open("rb") as audio:
            await update.effective_message.reply_audio(audio=audio, filename=output.name, title=output.stem[:64])
    except Exception as exc:
        log.exception("Demo failed")
        await update.effective_message.reply_text(f"Demo failed: {type(exc).__name__}: {str(exc)[:700]}")
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    s = cfg(uid)
    data = q.data

    if data == "menu:refresh":
        await q.edit_message_text(settings_text(uid), reply_markup=settings_keyboard())
    elif data == "menu:voice":
        kb = [[InlineKeyboardButton(label, callback_data=f"voice:{key}")]
              for key, (_, label) in VOICES.items()]
        await q.edit_message_text("Choose voice:", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "menu:rate":
        kb = [
            [InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate:{v:.1f}") for v in (1.0, 1.1, 1.2)],
            [InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate:{v:.1f}") for v in (1.3, 1.5, 1.7)],
            [InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate:{v:.1f}") for v in (1.8, 1.9, 2.0)],
        ]
        await q.edit_message_text("Choose rate:", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "menu:pitch":
        kb = [
            [InlineKeyboardButton(f"{v:+d} Hz", callback_data=f"pitch:{v}") for v in (-20, -10, -5, 0)],
            [InlineKeyboardButton(f"{v:+d} Hz", callback_data=f"pitch:{v}") for v in (5, 10, 15, 20)],
        ]
        await q.edit_message_text("Choose pitch:", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "menu:engine":
        kb = [[InlineKeyboardButton("Edge TTS", callback_data="engine:edge")],
              [InlineKeyboardButton("Google TTS", callback_data="engine:google")]]
        await q.edit_message_text("Choose engine:", reply_markup=InlineKeyboardMarkup(kb))
    elif data == "menu:name":
        await q.edit_message_text("Set the filename in chat using:\n/name My Audiobook")
    elif data.startswith("voice:"):
        key = data.split(":", 1)[1]
        if key in VOICES:
            s["voice"] = VOICES[key][0]
            await q.edit_message_text(f"Voice set: {VOICES[key][1]}")
    elif data.startswith("rate:"):
        s["rate"] = float(data.split(":", 1)[1])
        await q.edit_message_text(f"Rate set to {s['rate']:.1f}x")
    elif data.startswith("pitch:"):
        s["pitch"] = int(data.split(":", 1)[1])
        await q.edit_message_text(f"Pitch set to {s['pitch']:+d} Hz")
    elif data.startswith("engine:"):
        s["engine"] = data.split(":", 1)[1]
        await q.edit_message_text(f"Engine set to {s['engine']}")


async def download_input_file(context, file_id, input_path):
    """Download input from Telegram, robustly resolving Local Bot API paths."""
    from urllib.parse import urlsplit, unquote

    tg_file = await context.bot.get_file(file_id)
    returned_path = str(tg_file.file_path or "")
    destination = Path(input_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if not LOCAL_BOT_API:
        await tg_file.download_to_drive(custom_path=str(destination))
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("Telegram download returned an empty file.")
        return destination

    if not returned_path:
        raise FileNotFoundError("Local Bot API getFile returned an empty path.")

    candidates = []
    if os.path.isabs(returned_path):
        candidates.append(Path("/" + returned_path.lstrip("/")))
    else:
        parsed = urlsplit(returned_path)
        url_path = unquote(parsed.path) if parsed.scheme in {"http", "https"} else returned_path
        marker = "/file/bot"
        marker_pos = url_path.find(marker)
        if marker_pos >= 0:
            after_marker = url_path[marker_pos + len(marker):]
            slash_pos = after_marker.find("/")
            if slash_pos >= 0:
                candidates.append(Path("/" + after_marker[slash_pos:].lstrip("/")))
        elif url_path.startswith("/"):
            candidates.append(Path("/" + url_path.lstrip("/")))

    for source_path in candidates:
        try:
            if source_path.is_file():
                shutil.copyfile(source_path, destination)
                if destination.is_file() and destination.stat().st_size > 0:
                    log.info("Input copied from shared Local Bot API storage.")
                    return destination
        except OSError:
            pass

    async def run_docker(*args):
        """Run Docker without sudo first, then try sudo for runner permissions."""
        last_error = b""
        for prefix in (("docker",), ("sudo", "docker")):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *prefix, *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout, b""
                last_error = stderr or stdout
            except FileNotFoundError as exc:
                last_error = str(exc).encode()
        return b"", last_error

    last_err = b""
    for source_path in candidates:
        _, last_err = await run_docker(
            "cp", f"{LOCAL_API_CONTAINER}:{source_path}", str(destination)
        )
        if destination.is_file() and destination.stat().st_size > 0:
            log.info("Input copied from Local Bot API container using returned path.")
            return destination
        destination.unlink(missing_ok=True)

    # Local Bot API can return only /documents/file_0.txt, although the file
    # is actually stored under /var/lib/telegram-bot-api/<token>/documents/.
    # Search inside the container rather than guessing that path.
    basename = Path(returned_path.split("?", 1)[0]).name
    if not basename or basename in {".", ".."}:
        raise RuntimeError("Local Bot API returned an invalid uploaded-file path.")

    found, find_err = await run_docker(
        "exec", LOCAL_API_CONTAINER, "find", "/", "-type", "f", "-name", basename
    )
    found_paths = []
    if found:
        for line in found.decode("utf-8", errors="replace").splitlines():
            path = line.strip()
            if path.startswith("/") and path not in found_paths:
                found_paths.append(path)

    copy_errors = []
    for actual_path in found_paths:
        _, copy_err = await run_docker(
            "cp", f"{LOCAL_API_CONTAINER}:{actual_path}", str(destination)
        )
        if destination.is_file() and destination.stat().st_size > 0:
            log.info("Input copied from Local Bot API container after path lookup.")
            return destination
        destination.unlink(missing_ok=True)
        if copy_err:
            copy_errors.append(copy_err.decode("utf-8", errors="replace"))

    details = copy_errors[-1] if copy_errors else last_err.decode("utf-8", errors="replace")
    details = details[-500:].replace(BOT_TOKEN, "[REDACTED]")
    if find_err:
        details = (details + " " + find_err.decode("utf-8", errors="replace")[-250:])
    raise RuntimeError(
        f"Could not locate uploaded file '{basename}' inside Local Bot API container "
        f"'{LOCAL_API_CONTAINER}'. The returned path and container search both failed. {details}"
    )


async def convert_document(update, context, filename):
    message = update.effective_message
    uid = update.effective_user.id
    folder = Path(tempfile.mkdtemp(prefix=f"user-{uid}-", dir=WORK_ROOT))
    status = await message.reply_text("📥 Downloading file…")

    try:
        input_path = folder / Path(filename).name
        await download_input_file(context, message.document.file_id, input_path)

        await status.edit_text("📖 Extracting text…")
        text = extract_text(input_path)
        if not text.strip():
            raise ValueError("No readable text found; scanned PDFs need OCR.")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"Text exceeds {MAX_TEXT_CHARS:,} characters.")

        s = cfg(uid).copy()
        merged = await generate_audio(text, folder, s, status, uid)
        await status.edit_text("✂️ Preparing audio parts…")
        parts = await split_audio(merged, folder)
        base = safe_name(s["name"] if s["name"] != "Audiobook" else Path(filename).stem)

        PROGRESS[uid] = {"stage": "Sending", "done": 0, "total": len(parts)}
        for i, part in enumerate(parts, 1):
            if part.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise RuntimeError("Part exceeds MAX_UPLOAD_MB; lower SEND_PART_MB.")
            outname = f"{base}.mp3" if len(parts) == 1 else f"{base}_part_{i:03d}.mp3"
            await status.edit_text(f"📤 Sending audio {i}/{len(parts)}…")
            with part.open("rb") as audio:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id, audio=audio,
                    filename=outname, title=Path(outname).stem[:64],
                    read_timeout=600, write_timeout=600,
                    connect_timeout=60, pool_timeout=60,
                )
            if part != merged:
                part.unlink(missing_ok=True)
            PROGRESS[uid] = {"stage": "Sending", "done": i, "total": len(parts)}

        await status.edit_text(f"✅ Complete! Sent {len(parts)} audio file(s). Temporary files cleared.")
    except asyncio.CancelledError:
        try:
            await status.edit_text("⛔ Conversion cancelled; clearing temporary files.")
        except TelegramError:
            pass
        raise
    except Exception as exc:
        log.exception("Conversion failed")
        text = f"❌ Conversion failed: {type(exc).__name__}: {str(exc)[:900]}"
        try:
            await status.edit_text(text)
        except TelegramError:
            await message.reply_text(text)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        PROGRESS.pop(uid, None)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    uid = update.effective_user.id
    existing = TASKS.get(uid)
    if existing and not existing.done():
        await message.reply_text("A conversion is already running. Use /cancel.")
        return

    filename = message.document.file_name or "input.txt"
    if Path(filename).suffix.lower() not in {".txt", ".text", ".md", ".pdf", ".docx", ".epub"}:
        await message.reply_text("Supported: TXT, MD, PDF, DOCX, EPUB.")
        return
    if message.document.file_size and message.document.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(f"File exceeds configured {MAX_INPUT_MB} MB limit.")
        return

    task = asyncio.create_task(convert_document(update, context, filename))
    TASKS[uid] = task
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        if TASKS.get(uid) is task:
            TASKS.pop(uid, None)


async def error_handler(update, context):
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
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("speed", rate_cmd))
    app.add_handler(CommandHandler("pitch", pitch_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("demo", demo_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_error_handler(error_handler)

    log.info("Bot starting; local API=%s; target part size=%s MB", LOCAL_BOT_API, SEND_PART_MB)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
