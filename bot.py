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

def proxy_url():
    """Build authenticated HTTP proxy URL from GitHub Actions secrets."""
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "80").strip()
    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()
    if not host or not port or not username or not password:
        return None
    from urllib.parse import quote
    user = quote(username, safe="")
    pwd = quote(password, safe="")
    return f"http://{user}:{pwd}@{host}:{port}"

PROXY_URL = proxy_url()
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
    requests_kwargs = {}
    if PROXY_URL:
        requests_kwargs["proxies"] = {"http": PROXY_URL, "https": PROXY_URL}
    gTTS(text=text, lang="hi", slow=False, requests_kwargs=requests_kwargs).save(str(out))
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
        communicate = edge_tts.Communicate(
            text=text,
            voice=p["voice"],
            rate=edge_rate(p["rate"]),
            pitch=f"{int(p['pitch']):+d}Hz",
            proxy=PROXY_URL,
        )
        await communicate.save(str(out))

def merge(files, output, workdir):
    """Join compatible MP3 chunks without re-encoding (faster and avoids quality loss)."""
    concat = workdir / (output.stem + "_concat.txt")
    concat.write_text("\n".join("file '" + str(f.resolve()).replace("'", r"'\''") + "'" for f in files),
                      encoding="utf-8")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                    "-i", str(concat), "-c", "copy", str(output)], check=True)

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
    # Keep generated audio segments below Telegram's practical 50 MB upload ceiling.
    TARGET_PART_BYTES = 48 * 1024 * 1024
    status = await msg.reply_text(
        f"📥 Downloading: {filename}\n📦 Input file size: {input_size / 1048576:.2f} MB\n"
        f"📤 Output parts target: 48 MB"
    )
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="tts_") as td:
        wd = Path(td)
        source = wd / Path(filename).name
        tgfile = await context.bot.get_file(doc.file_id)
        await tgfile.download_to_drive(custom_path=str(source))
        await status.edit_text(f"📖 Extracting text…\n📦 Input size: {input_size / 1048576:.2f} MB")
        text = await asyncio.to_thread(extract, source)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            await status.edit_text("❌ No readable text found.")
            return
        if len(text) > MAX_TEXT_LENGTH:
            await status.edit_text(
                f"❌ Text has {len(text):,} characters; limit is {MAX_TEXT_LENGTH:,}. Split the book into smaller files."
            )
            return
        chunks = split_text(text)
        if not chunks:
            await status.edit_text("❌ No text to convert.")
            return

        total_chunks = len(chunks)
        estimated_audio = estimate_audio_seconds(text, p["rate"])
        completed = 0
        sent_parts = 0
        pending_audio = []
        pending_bytes = 0
        total_sent_bytes = 0
        total_sent_duration = 0.0
        size_mb = lambda n: n / (1024 * 1024)

        async def send_pending(final=False):
            nonlocal sent_parts, pending_audio, pending_bytes, total_sent_bytes, total_sent_duration
            if not pending_audio:
                return
            sent_parts += 1
            output = wd / f"{Path(filename).stem[:55]}_part_{sent_parts:03d}.mp3"
            await status.edit_text(
                f"📦 Preparing part {sent_parts}…\n"
                f"Part size before merge: {size_mb(pending_bytes):.2f} MB\n"
                f"🎙️ Conversion: {progress_bar(completed, total_chunks)} {completed * 100 // total_chunks}%\n"
                f"⌛ Used time: {fmt_seconds(time.monotonic() - started)}"
            )
            await asyncio.to_thread(merge, list(pending_audio), output, wd)
            output_bytes = output.stat().st_size
            duration = probe_duration(output)
            # A safety check: the target is 48 MiB, so this should normally stay below 49 MiB.
            if output_bytes > 49 * 1024 * 1024:
                await status.edit_text(
                    f"⚠️ Part {sent_parts} merged to {size_mb(output_bytes):.2f} MB, over 49 MB. "
                    "It will be split into smaller audio files instead."
                )
                # Split this oversized joined file into shorter segments by stream-copying at 25 minutes.
                split_dir = wd / f"split_{sent_parts:03d}"
                split_dir.mkdir(exist_ok=True)
                pattern = split_dir / "segment_%03d.mp3"
                subprocess.run([
                    "ffmpeg", "-y", "-loglevel", "error", "-i", str(output),
                    "-f", "segment", "-segment_time", "1500", "-c", "copy", str(pattern)
                ], check=True)
                output.unlink(missing_ok=True)
                segments = sorted(split_dir.glob("segment_*.mp3"))
                for seg in segments:
                    seg_bytes = seg.stat().st_size
                    seg_duration = probe_duration(seg)
                    with seg.open("rb") as f:
                        await msg.reply_document(
                            document=f, filename=f"{Path(filename).stem[:45]}_part_{sent_parts:03d}_{seg.name}",
                            caption=(f"✅ Audio part {sent_parts} (split)\n📦 Size: {size_mb(seg_bytes):.2f} MB\n"
                                     f"🔊 Duration: {fmt_seconds(seg_duration)}\n⌛ Used time: {fmt_seconds(time.monotonic()-started)}"),
                            read_timeout=300, write_timeout=300, connect_timeout=60
                        )
                    total_sent_bytes += seg_bytes
                    total_sent_duration += seg_duration or 0
            else:
                with output.open("rb") as f:
                    await msg.reply_document(
                        document=f,
                        filename=output.name,
                        caption=(
                            f"✅ Audiobook part {sent_parts}\n"
                            f"📦 Part size: {size_mb(output_bytes):.2f} MB\n"
                            f"🔊 Audio duration: {fmt_seconds(duration)}\n"
                            f"📚 Overall progress: {completed:,}/{total_chunks:,} chunks "
                            f"({completed * 100 // total_chunks}%)\n"
                            f"⏳ Estimated total audio: ~{fmt_seconds(estimated_audio)}\n"
                            f"⌛ Used time: {fmt_seconds(time.monotonic()-started)}\n"
                            f"⚡ Engine: {p['engine']} | Rate: {p['rate']:.1f}x"
                        ),
                        read_timeout=300, write_timeout=300, connect_timeout=60
                    )
                total_sent_bytes += output_bytes
                total_sent_duration += duration or 0
            # Clear this batch; generation continues for remaining chunks.
            pending_audio = []
            pending_bytes = 0
            output.unlink(missing_ok=True)
            await status.edit_text(
                f"📤 Sent part {sent_parts} successfully\n"
                f"📦 Sent so far: {size_mb(total_sent_bytes):.2f} MB\n"
                f"🔊 Sent audio duration: {fmt_seconds(total_sent_duration)}\n"
                f"🎙️ Progress: {progress_bar(completed, total_chunks)} {completed * 100 // total_chunks}% "
                f"({completed:,}/{total_chunks:,} chunks)\n"
                f"⏳ Estimated total audio: ~{fmt_seconds(estimated_audio)}\n"
                f"⌛ Used time: {fmt_seconds(time.monotonic() - started)}\n"
                f"🚀 Continuing to generate remaining audio…"
            )

        await status.edit_text(
            f"🚀 Starting conversion\n{progress_bar(0, total_chunks)} 0% (0/{total_chunks:,})\n\n"
            f"📦 Input size: {size_mb(input_size):.2f} MB\n📝 Characters: {len(text):,}\n"
            f"🔊 Estimated total audio: ~{fmt_seconds(estimated_audio)}\n"
            f"📤 Auto-send threshold: 48 MB per part\n"
            f"⚡ Engine: {p['engine']} | Rate: {p['rate']:.1f}x | Parallel: {TTS_CONCURRENCY}"
        )

        # Process small waves concurrently. As soon as accumulated audio reaches ~48 MiB,
        # merge/send that part while the remaining novel is still waiting to be generated.
        for batch_start in range(0, total_chunks, TTS_CONCURRENCY):
            if uid in CANCEL:
                raise asyncio.CancelledError()
            batch = list(enumerate(chunks[batch_start:batch_start + TTS_CONCURRENCY], start=batch_start))
            async def render(index, chunk):
                out = wd / f"chunk_{index + 1:06d}.mp3"
                await make_audio(chunk, out, p)
                return index, out
            results = await asyncio.gather(*(render(i, chunk) for i, chunk in batch))
            for index, out in sorted(results, key=lambda item: item[0]):
                if uid in CANCEL:
                    raise asyncio.CancelledError()
                pending_audio.append(out)
                pending_bytes += out.stat().st_size
                completed += 1
                elapsed = time.monotonic() - started
                eta = (elapsed / max(1, completed)) * (total_chunks - completed)
                # Avoid Telegram message-edit rate limits; always refresh at the send threshold.
                if completed % 5 == 0 or pending_bytes >= TARGET_PART_BYTES or completed == total_chunks:
                    await status.edit_text(
                        f"🎙️ Converting audiobook\n{progress_bar(completed, total_chunks)} "
                        f"{completed * 100 // total_chunks}% ({completed:,}/{total_chunks:,} chunks)\n\n"
                        f"📦 Input size: {size_mb(input_size):.2f} MB\n"
                        f"📦 Current unsent audio: {size_mb(pending_bytes):.2f} / 48 MB\n"
                        f"🔊 Estimated total audio: ~{fmt_seconds(estimated_audio)}\n"
                        f"⏳ Estimated time remaining: ~{fmt_seconds(eta)}\n"
                        f"⌛ Used time: {fmt_seconds(elapsed)}\n"
                        f"📤 Parts already sent: {sent_parts}\n"
                        f"⚡ Engine: {p['engine']} | Rate: {p['rate']:.1f}x | Parallel: {TTS_CONCURRENCY}"
                    )
                if pending_bytes >= TARGET_PART_BYTES:
                    await send_pending()

        if uid in CANCEL:
            raise asyncio.CancelledError()
        if pending_audio:
            await send_pending(final=True)

        await status.edit_text(
            f"✅ All audio sent!\n{progress_bar(total_chunks, total_chunks)} 100%\n"
            f"📤 Parts sent: {sent_parts}\n📦 Total sent size: {size_mb(total_sent_bytes):.2f} MB\n"
            f"🔊 Total audio duration: {fmt_seconds(total_sent_duration)}\n"
            f"⌛ Total used time: {fmt_seconds(time.monotonic() - started)}"
        )

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
    log.info("Proxy configured: %s", bool(PROXY_URL))
    log.info("Starting polling bot")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
