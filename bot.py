import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import edge_tts
from gtts import gTTS
from bs4 import BeautifulSoup
from docx import Document
from ebooklib import epub, ITEM_DOCUMENT
from pypdf import PdfReader
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("tts-bot")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_TEXT_LENGTH = 20_000_000
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CHUNK_SIZE = 2800
TTS_CONCURRENCY = 4  # Parallel TTS requests; set to 2 if rate-limited.
DEFAULT_SETTINGS = {"engine": "edge", "voice": "hi-IN-MadhurNeural", "rate": 1.1, "pitch": 0}
SETTINGS = {}
ACTIVE = {}
CANCEL = set()
KEYBOARD = ReplyKeyboardMarkup(
    [["🎙 Voice", "⚡ Speed / Rate"], ["🎚 Pitch", "🔧 Engine"], ["❌ Cancel", "ℹ️ Help"]],
    resize_keyboard=True,
)
VOICES = [
    ("Hindi Male — Madhur", "hi-IN-MadhurNeural"),
    ("Hindi Female — Swara", "hi-IN-SwaraNeural"),
    ("English Male — Andrew", "en-US-AndrewNeural"),
    ("English Female — Jenny", "en-US-JennyNeural"),
]

def prefs(uid):
    if uid not in SETTINGS:
        SETTINGS[uid] = DEFAULT_SETTINGS.copy()
    return SETTINGS[uid]

def split_text(text, limit=CHUNK_SIZE):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    out = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        if text[:cut].strip():
            out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out

def extract(path):
    ext = path.suffix.lower()
    if ext in (".txt", ".text", ".md"):
        return path.read_text(encoding="utf-8-sig", errors="replace")
    if ext == ".docx":
        doc = Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if ext == ".pdf":
        return "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
    if ext == ".epub":
        book = epub.read_epub(str(path))
        parts = []
        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                for tag in soup(["script", "style", "nav"]):
                    tag.decompose()
                value = soup.get_text("\n", strip=True)
                if value:
                    parts.append(value)
        return "\n\n".join(parts)
    raise ValueError("Unsupported format. Send TXT, EPUB, PDF, or DOCX.")

def edge_rate(rate):
    return f"{round((float(rate) - 1) * 100):+d}%"

def make_google(text, out, rate):
    gTTS(text=text, lang="hi", slow=False).save(str(out))
    if abs(rate - 1) > 0.01:
        temp = out.with_name(out.stem + "_speed.mp3")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(out),
                        "-filter:a", f"atempo={max(.5, min(2.0, float(rate)))}",
                        "-vn", str(temp)], check=True)
        temp.replace(out)

async def make_audio(text, out, p):
    if p["engine"] == "google":
        await asyncio.to_thread(make_google, text, out, p["rate"])
    else:
        await edge_tts.Communicate(
            text=text, voice=p["voice"], rate=edge_rate(p["rate"]),
            pitch=f"{int(p['pitch']):+d}Hz"
        ).save(str(out))

def merge(files, output, workdir):
    concat = workdir / "concat.txt"
    concat.write_text("\n".join("file '" + str(f.resolve()).replace("'", r"'\''") + "'" for f in files),
                      encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c:a", "libmp3lame", "-q:a", "4", str(output)], check=True)

def fmt_seconds(seconds):
    if seconds is None or seconds < 0: return "calculating…"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600); m, sec = divmod(rem, 60)
    return f"{h}h {m}m {sec}s" if h else (f"{m}m {sec}s" if m else f"{sec}s")

def progress_bar(done, total, width=18):
    total = max(1, int(total)); filled = int(width * min(done, total) / total)
    return "█" * filled + "░" * (width - filled)

def probe_duration(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path)], check=True,
            capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception: return None

def estimate_audio_seconds(text, rate):
    words = len(text.split())
    return max(1, int(words / (145.0 * max(0.5, float(rate))) * 60))

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = prefs(update.effective_user.id)
    await update.effective_message.reply_text(
        "📚 *Text to Audiobook Bot*\n\nSend TXT, EPUB, PDF, or DOCX. It extracts the text, "
        "converts it, merges the chunks, and sends one MP3.\n\n"
        f"Engine: `{p['engine']}`\nVoice: `{p['voice']}`\nRate: `{p['rate']:.1f}x`\n"
        f"Pitch: `{p['pitch']:+d} Hz`",
        parse_mode="Markdown", reply_markup=KEYBOARD)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Commands:\n/start — menu\n/settings — current settings\n/voice — choose voice\n"
        "/rate 1.5 — set speed (1.1–2.0)\n/pitch 2 — set pitch (-10 to +10 Hz)\n"
        "/engine edge or /engine google\n/cancel — cancel conversion\n\n"
        "Supported: TXT, EPUB, PDF, DOCX. Large books can take a long time. "
        "GitHub Actions has runtime limits and is not 24/7 hosting.", reply_markup=KEYBOARD)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = prefs(update.effective_user.id)
    await update.effective_message.reply_text(
        f"Engine: {p['engine']}\nVoice: {p['voice']}\nRate: {p['rate']:.1f}x\nPitch: {p['pitch']:+d} Hz",
        reply_markup=KEYBOARD)

async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Choose voice:", reply_markup=InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data="voice|" + voice)] for label, voice in VOICES]))

async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = prefs(update.effective_user.id)
    if context.args:
        try: value = float(context.args[0].lower().replace("x", ""))
        except ValueError:
            await update.effective_message.reply_text("Example: /rate 1.5"); return
        if not 1.1 <= value <= 2.0:
            await update.effective_message.reply_text("Rate must be from 1.1 to 2.0."); return
        p["rate"] = round(value, 1)
        await update.effective_message.reply_text(f"✅ Rate: {p['rate']:.1f}x"); return
    vals = [round(1.1 + i / 10, 1) for i in range(10)]
    rows = [[InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate|{v:.1f}") for v in vals[i:i+5]]
            for i in range(0, 10, 5)]
    await update.effective_message.reply_text(f"Choose rate (current {p['rate']:.1f}x):",
                                               reply_markup=InlineKeyboardMarkup(rows))

async def pitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = prefs(update.effective_user.id)
    if context.args:
        try: value = int(context.args[0].lower().replace("hz", ""))
        except ValueError:
            await update.effective_message.reply_text("Example: /pitch 2"); return
        if not -10 <= value <= 10:
            await update.effective_message.reply_text("Pitch must be from -10 to +10."); return
        p["pitch"] = value
        await update.effective_message.reply_text(f"✅ Pitch: {value:+d} Hz"); return
    vals = list(range(-10, 11))
    rows = [[InlineKeyboardButton(f"{v:+d} Hz", callback_data=f"pitch|{v}") for v in vals[i:i+5]]
            for i in range(0, len(vals), 5)]
    await update.effective_message.reply_text("Choose pitch:", reply_markup=InlineKeyboardMarkup(rows))

async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = prefs(update.effective_user.id)
    if context.args:
        value = context.args[0].lower()
        if value not in ("edge", "google"):
            await update.effective_message.reply_text("Use /engine edge or /engine google"); return
        p["engine"] = value
        await update.effective_message.reply_text(f"✅ Engine: {value}"); return
    await update.effective_message.reply_text("Choose engine:", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("Microsoft Edge TTS", callback_data="engine|edge")],
        [InlineKeyboardButton("Google Translate TTS", callback_data="engine|google")]]))

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    task = ACTIVE.get(uid)
    if task and not task.done():
        CANCEL.add(uid)
        task.cancel()
        await update.effective_message.reply_text("🛑 Cancellation requested.")
    else:
        await update.effective_message.reply_text("No active conversion.")

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    p = prefs(update.effective_user.id)
    key, value = (q.data or "").split("|", 1)
    if key == "voice": p["voice"] = value
    elif key == "rate": p["rate"] = float(value)
    elif key == "pitch": p["pitch"] = int(value)
    elif key == "engine": p["engine"] = value
    await q.edit_message_text(f"✅ Updated: {key} = {p.get(key, value)}")

async def convert(update, context, filename):
    uid = update.effective_user.id
    msg = update.effective_message
    doc = msg.document
    p = prefs(uid).copy()
    input_size = doc.file_size or 0
    status = await msg.reply_text(f"📥 Downloading: {filename}\n📦 Input file size: {input_size / 1048576:.2f} MB")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tts_") as td:
        wd = Path(td)
        source = wd / Path(filename).name
        tgfile = await context.bot.get_file(doc.file_id)
        await tgfile.download_to_drive(custom_path=str(source))
        await status.edit_text(f"📖 Extracting text…\n📦 Input file size: {input_size / 1048576:.2f} MB")
        text = await asyncio.to_thread(extract, source)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            await status.edit_text("❌ No readable text found."); return
        if len(text) > MAX_TEXT_LENGTH:
            await status.edit_text(f"❌ Text has {len(text):,} characters; limit is {MAX_TEXT_LENGTH:,}. Split the book into smaller files."); return
        chunks = split_text(text)
        if not chunks:
            await status.edit_text("❌ No text to convert."); return
        total = len(chunks)
        estimated_audio = estimate_audio_seconds(text, p["rate"])
        completed = 0
        audio_parts = [None] * total
        lock = asyncio.Lock()

        async def render_one(index, chunk):
            nonlocal completed
            if uid in CANCEL: raise asyncio.CancelledError()
            out = wd / f"part_{index + 1:05d}.mp3"
            await make_audio(chunk, out, p)
            async with lock:
                audio_parts[index] = out
                completed += 1
                elapsed = time.monotonic() - started
                eta = (elapsed / max(1, completed)) * (total - completed)
                await status.edit_text(
                    f"🎙️ Converting audiobook\n{progress_bar(completed, total)} {completed * 100 // total}% ({completed:,}/{total:,})\n\n"
                    f"📦 Input size: {input_size / 1048576:.2f} MB\n📝 Characters: {len(text):,}\n"
                    f"🔊 Estimated audio duration: ~{fmt_seconds(estimated_audio)}\n"
                    f"⏳ Estimated time remaining: ~{fmt_seconds(eta)}\n⌛ Used time: {fmt_seconds(elapsed)}\n"
                    f"⚡ Engine: {p['engine']} | Rate: {p['rate']:.1f}x | Parallel: {TTS_CONCURRENCY}")

        semaphore = asyncio.Semaphore(TTS_CONCURRENCY)
        async def limited_render(i, chunk):
            async with semaphore:
                if uid in CANCEL: raise asyncio.CancelledError()
                await render_one(i, chunk)

        await status.edit_text(
            f"🚀 Starting parallel conversion\n{progress_bar(0, total)} 0% (0/{total:,})\n\n"
            f"📦 Input size: {input_size / 1048576:.2f} MB\n📝 Characters: {len(text):,}\n"
            f"🔊 Estimated audio duration: ~{fmt_seconds(estimated_audio)}\n"
            f"⏳ Estimated time remaining: calculating…\n⌛ Used time: 0s\n"
            f"⚡ Engine: {p['engine']} | Rate: {p['rate']:.1f}x | Parallel: {TTS_CONCURRENCY}")
        try:
            await asyncio.gather(*(limited_render(i, chunk) for i, chunk in enumerate(chunks)))
        except asyncio.CancelledError:
            raise
        if uid in CANCEL: raise asyncio.CancelledError()
        parts = [x for x in audio_parts if x is not None]
        await status.edit_text(f"🔗 Merging audio…\n{progress_bar(total, total)} 100%\n⌛ Used time: {fmt_seconds(time.monotonic()-started)}")
        output = wd / (Path(filename).stem[:70] + "_audiobook.mp3")
        await asyncio.to_thread(merge, parts, output, wd)
        actual_duration = probe_duration(output)
        output_size = output.stat().st_size
        elapsed = time.monotonic() - started
        if output_size > 49 * 1024 * 1024:
            await status.edit_text(f"⚠️ MP3 is {output_size/1048576:.2f} MB, too large to send as one file.\n🔊 Audio duration: {fmt_seconds(actual_duration)}\n⌛ Used time: {fmt_seconds(elapsed)}\nTry smaller input files."); return
        await status.edit_text(f"📤 Uploading MP3…\n📦 Output size: {output_size/1048576:.2f} MB\n🔊 Audio duration: {fmt_seconds(actual_duration)}\n⌛ Used time: {fmt_seconds(elapsed)}")
        with output.open("rb") as f:
            await msg.reply_document(document=f, filename=output.name,
                caption=(f"✅ Audiobook ready\n📦 Input: {input_size/1048576:.2f} MB\n📦 MP3 size: {output_size/1048576:.2f} MB\n"
                         f"🔊 Audio duration: {fmt_seconds(actual_duration)}\n📝 Characters: {len(text):,}\n🎙️ Chunks: {len(parts):,}\n"
                         f"⏳ Estimated audio: ~{fmt_seconds(estimated_audio)}\n⌛ Used time: {fmt_seconds(elapsed)}\n"
                         f"⚡ Engine: {p['engine']} | Rate: {p['rate']:.1f}x"),
                read_timeout=300, write_timeout=300, connect_timeout=60)
        await status.edit_text(f"✅ Complete!\n{progress_bar(total,total)} 100%\n📦 MP3 size: {output_size/1048576:.2f} MB\n🔊 Audio duration: {fmt_seconds(actual_duration)}\n⌛ Used time: {fmt_seconds(elapsed)}")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    doc = update.effective_message.document
    if not doc: return
    filename = doc.file_name or "document.txt"
    if Path(filename).suffix.lower() not in {".txt", ".text", ".md", ".epub", ".pdf", ".docx"}:
        await update.effective_message.reply_text("Send TXT, EPUB, PDF, or DOCX."); return
    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await update.effective_message.reply_text("File exceeds the 50 MB upload limit."); return
    old = ACTIVE.get(uid)
    if old and not old.done():
        await update.effective_message.reply_text("Conversion already running. Use /cancel first."); return
    task = asyncio.create_task(convert(update, context, filename))
    ACTIVE[uid] = task
    try:
        await task
    except asyncio.CancelledError:
        await update.effective_message.reply_text("🛑 Conversion cancelled.")
    except Exception:
        log.exception("Conversion failed")
        await update.effective_message.reply_text("❌ Conversion failed. Open GitHub Actions logs for the error.")
    finally:
        if ACTIVE.get(uid) is task: ACTIVE.pop(uid, None)
        CANCEL.discard(uid)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.effective_message.text or "").strip()
    if text == "🎙 Voice": await voice_cmd(update, context)
    elif text == "⚡ Speed / Rate": await rate_cmd(update, context)
    elif text == "🎚 Pitch": await pitch_cmd(update, context)
    elif text == "🔧 Engine": await engine_cmd(update, context)
    elif text == "❌ Cancel": await cancel_cmd(update, context)
    elif text == "ℹ️ Help": await help_cmd(update, context)

async def error_handler(update, context):
    log.error("Unhandled bot error", exc_info=context.error)

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN secret is missing.")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed.")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("pitch", pitch_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)
    log.info("Starting polling bot")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
