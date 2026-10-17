
import os
import re
import time
import math
import asyncio
import logging
import shutil
import tempfile
import subprocess
from pathlib import Path
from datetime import timedelta

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from edge_tts import Communicate
from gtts import gTTS

try:
    from docx import Document
except ImportError:
    Document = None

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

try:
    from ebooklib import epub
    from bs4 import BeautifulSoup
except ImportError:
    epub = None
    BeautifulSoup = None


# ---------------- CONFIG ----------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
LOCAL_BOT_API = os.getenv("LOCAL_BOT_API", "1").lower() in {
    "1", "true", "yes"
}
LOCAL_BOT_API_URL = os.getenv(
    "LOCAL_BOT_API_URL", "http://127.0.0.1:8081"
).rstrip("/")

MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "1900"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1900"))
SEND_PART_MB = int(os.getenv("SEND_PART_MB", "1800"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))
TTS_CONCURRENCY = max(
    1, min(int(os.getenv("TTS_CONCURRENCY", "3")), 5)
)
DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "edge").lower()
DEFAULT_RATE = os.getenv("DEFAULT_RATE", "+10%")
DEFAULT_PITCH = os.getenv("DEFAULT_PITCH", "+0Hz")

WORK_DIR = Path(os.getenv("TTS_WORK_DIR", "tts_work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tts-bot")

VOICES = {
    "madhur": {
        "name": "Hindi Male",
        "voice": "hi-IN-MadhurNeural",
        "lang": "hi",
    },
    "swara": {
        "name": "Hindi Female",
        "voice": "hi-IN-SwaraNeural",
        "lang": "hi",
    },
    "guy": {
        "name": "English Male",
        "voice": "en-US-GuyNeural",
        "lang": "en",
    },
    "jenny": {
        "name": "English Female",
        "voice": "en-US-JennyNeural",
        "lang": "en",
    },
}

USER_SETTINGS = {}
USER_JOBS = {}
USER_NAMES = {}


def settings_for(user_id):
    return USER_SETTINGS.setdefault(user_id, {
        "voice": "madhur",
        "engine": DEFAULT_ENGINE,
        "rate": DEFAULT_RATE,
        "pitch": DEFAULT_PITCH,
    })


def progress_bar(percent, width=16):
    percent = max(0, min(100, int(percent)))
    filled = int(width * percent / 100)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


def format_time(seconds):
    return str(timedelta(seconds=max(0, int(seconds))))


def safe_name(name):
    name = re.sub(r'[\\/:*?"<>|]+', "_", name or "")
    return name[:100].strip(" .") or "Audiobook"


def split_text(text, max_chars=CHUNK_CHARS):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        return []

    chunks, current = [], ""
    for piece in re.split(r"(\n\n+|(?<=[.!?।])\s+)", text):
        if not piece:
            continue
        while len(piece) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            chunks.append(piece[:max_chars].strip())
            piece = piece[max_chars:]
        if len(current) + len(piece) > max_chars:
            if current.strip():
                chunks.append(current.strip())
            current = piece.strip()
        else:
            current += piece

    if current.strip():
        chunks.append(current.strip())
    return chunks


# ---------------- INPUT EXTRACTION ----------------

def extract_text(file_path):
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        return path.read_text(encoding="utf-8-sig", errors="replace")

    if suffix == ".docx":
        if Document is None:
            raise RuntimeError("python-docx is not installed.")
        doc = Document(str(path))
        lines = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            for row in table.rows:
                lines.append(" | ".join(c.text for c in row.cells))
        return "\n\n".join(lines)

    if suffix == ".pdf":
        if PdfReader is None:
            raise RuntimeError("pypdf is not installed.")
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)

    if suffix == ".epub":
        if epub is None or BeautifulSoup is None:
            raise RuntimeError("EbookLib/BeautifulSoup is not installed.")
        book = epub.read_epub(str(path))
        sections = []
        for item in book.get_items():
            if item.get_type() == 9:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()
                text = soup.get_text("\n", strip=True)
                if text:
                    sections.append(text)
        return "\n\n".join(sections)

    raise ValueError("Supported formats: TXT, DOCX, PDF, EPUB.")


# ---------------- TTS ----------------

async def make_edge(text, output_path, settings):
    voice = VOICES.get(
        settings.get("voice", "madhur"), VOICES["madhur"]
    )["voice"]

    # No proxy is used.
    communicate = Communicate(
        text=text,
        voice=voice,
        rate=settings.get("rate", DEFAULT_RATE),
        pitch=settings.get("pitch", DEFAULT_PITCH),
    )
    await communicate.save(str(output_path))

    if not Path(output_path).is_file() or Path(output_path).stat().st_size == 0:
        raise RuntimeError("Edge TTS returned an empty file.")


async def make_google(text, output_path, settings):
    lang = VOICES.get(
        settings.get("voice", "madhur"), VOICES["madhur"]
    )["lang"]

    output_path = Path(output_path)
    output_path.unlink(missing_ok=True)

    tts = gTTS(
        text=text,
        lang=lang,
        slow=False,
        timeout=(15, 60),
    )
    await asyncio.wait_for(
        asyncio.to_thread(tts.save, str(output_path)),
        timeout=180,
    )

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("Google TTS returned an empty file.")


async def generate_chunk(index, text, output_path, settings, semaphore):
    async with semaphore:
        engine = settings.get("engine", "edge").lower()
        last_error = None

        for attempt in range(3):
            try:
                if engine == "google":
                    await make_google(text, output_path, settings)
                else:
                    await make_edge(text, output_path, settings)
                return output_path
            except Exception as exc:
                last_error = exc
                log.warning(
                    "Chunk %s attempt %s failed: %s",
                    index + 1, attempt + 1, type(exc).__name__,
                )
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)

        if engine != "google":
            try:
                await make_google(text, output_path, settings)
                return output_path
            except Exception as exc:
                last_error = exc

        raise RuntimeError(
            f"Chunk {index + 1} failed: "
            f"{type(last_error).__name__}: {last_error}"
        ) from last_error


# ---------------- LOCAL BOT API FILE DOWNLOAD ----------------

async def download_input(document, output_path):
    """
    Local Bot API returns a server-side absolute file path.
    The workflow mounts the same directory into the runner.
    If the returned path is not accessible, use the normal
    PTB download URL as a fallback.
    """
    tg_file = await document.get_file()
    remote_path = tg_file.file_path

    if LOCAL_BOT_API and remote_path:
        candidate = Path(remote_path)
        if candidate.is_absolute() and candidate.is_file():
            shutil.copy2(candidate, output_path)
            return

    await tg_file.download_to_drive(custom_path=str(output_path))

    if not Path(output_path).is_file():
        raise RuntimeError("Input file download failed.")


# ---------------- FFMPEG ----------------

def run_command(args):
    result = subprocess.run(
        args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    if result.returncode:
        raise RuntimeError(result.stderr[-1500:] or "FFmpeg failed.")
    return result


def get_duration(path):
    result = run_command([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0


def merge_audio(audio_paths, output_path, work_dir):
    if not audio_paths:
        raise RuntimeError("No audio chunks exist.")

    list_file = Path(work_dir) / "concat_list.txt"
    with list_file.open("w", encoding="utf-8") as f:
        for path in audio_paths:
            f.write("file '" + str(Path(path).resolve()).replace("'", "'\\''") + "'\n")

    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_file),
        "-vn", "-c:a", "libmp3lame", "-b:a", "64k",
        "-y", str(output_path),
    ])

    if not Path(output_path).is_file() or Path(output_path).stat().st_size == 0:
        raise RuntimeError("Audio merge failed.")
    return output_path


def split_large_audio(audio_path, work_dir, max_bytes):
    audio_path = Path(audio_path)
    if audio_path.stat().st_size <= max_bytes:
        return [audio_path]

    duration = get_duration(audio_path)
    if duration <= 0:
        raise RuntimeError("Unable to read audio duration.")

    parts_needed = math.ceil(audio_path.stat().st_size / max_bytes)
    part_duration = max(30, int(duration / parts_needed))
    pattern = str(Path(work_dir) / "part_%03d.mp3")

    run_command([
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-i", str(audio_path), "-f", "segment",
        "-segment_time", str(part_duration),
        "-reset_timestamps", "1",
        "-c:a", "libmp3lame", "-b:a", "48k",
        "-y", pattern,
    ])

    parts = sorted(Path(work_dir).glob("part_*.mp3"))
    if not parts:
        raise RuntimeError("Audio splitting produced no files.")

    for part in parts:
        if part.stat().st_size > max_bytes:
            raise RuntimeError(
                f"{part.name} exceeds the configured upload size."
            )
    return parts


# ---------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎧 Novel TTS Bot\n\n"
        "Send TXT, DOCX, PDF or EPUB.\n\n"
        "/voice — voice selection\n"
        "/engine edge or /engine google\n"
        "/rate +10%\n"
        "/pitch +0Hz\n"
        "/name My Audiobook\n"
        "/status — progress\n"
        "/cancel — cancel job\n\n"
        "Voices: madhur, swara, guy, jenny"
    )


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    settings = settings_for(uid)
    if not context.args:
        await update.effective_message.reply_text(
            "\n".join(f"{k}: {v['name']}" for k, v in VOICES.items())
        )
        return
    choice = context.args[0].lower()
    if choice not in VOICES:
        await update.effective_message.reply_text(
            "Choose madhur, swara, guy or jenny."
        )
        return
    settings["voice"] = choice
    await update.effective_message.reply_text(
        f"Voice set to {VOICES[choice]['name']}."
    )


async def engine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    settings = settings_for(uid)
    if not context.args or context.args[0].lower() not in {"edge", "google"}:
        await update.effective_message.reply_text("Use /engine edge or /engine google.")
        return
    settings["engine"] = context.args[0].lower()
    await update.effective_message.reply_text(
        f"Engine set to {settings['engine']}."
    )


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args or not re.fullmatch(r"[+-]?\d{1,3}%", context.args[0]):
        await update.effective_message.reply_text("Example: /rate +10%")
        return
    settings_for(uid)["rate"] = context.args[0]
    await update.effective_message.reply_text("Rate updated.")


async def pitch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args or not re.fullmatch(r"[+-]?\d{1,4}Hz", context.args[0]):
        await update.effective_message.reply_text("Example: /pitch +0Hz")
        return
    settings_for(uid)["pitch"] = context.args[0]
    await update.effective_message.reply_text("Pitch updated.")


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(
            f"Current name: {USER_NAMES.get(uid, 'Audiobook')}"
        )
        return
    USER_NAMES[uid] = safe_name(" ".join(context.args))
    await update.effective_message.reply_text(f"Name set: {USER_NAMES[uid]}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job = USER_JOBS.get(str(update.effective_user.id))
    await update.effective_message.reply_text(
        job.get("status", "No active job.") if job else "No active job."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    job = USER_JOBS.get(str(update.effective_user.id))
    if job and job["task"] and not job["task"].done():
        job["task"].cancel()
        await update.effective_message.reply_text("Cancellation requested.")
    else:
        await update.effective_message.reply_text("No active job.")


# ---------------- DOCUMENT PROCESSING ----------------

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    uid = str(update.effective_user.id)
    document = message.document
    if not document:
        return

    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in {".txt", ".docx", ".pdf", ".epub"}:
        await message.reply_text("Send TXT, DOCX, PDF or EPUB.")
        return

    if document.file_size and document.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(f"Input limit: {MAX_INPUT_MB} MB.")
        return

    existing = USER_JOBS.get(uid)
    if existing and not existing["task"].done():
        await message.reply_text("A job is already running. Use /cancel first.")
        return

    work_dir = Path(tempfile.mkdtemp(prefix="tts_", dir=WORK_DIR))
    status_message = await message.reply_text("Downloading input...")

    async def run_job():
        started = time.monotonic()
        try:
            settings = settings_for(uid).copy()
            input_path = work_dir / ("input" + suffix)
            await download_input(document, input_path)

            text = await asyncio.to_thread(extract_text, input_path)
            if not text.strip():
                raise RuntimeError("No readable text found in the file.")

            chunks = split_text(text)
            if not chunks:
                raise RuntimeError("No text chunks found.")

            semaphore = asyncio.Semaphore(TTS_CONCURRENCY)
            paths = [work_dir / f"chunk_{i:05d}.mp3" for i in range(len(chunks))]
            total_chars = len(text)
            completed = 0
            done_chars = 0
            last_update = 0.0

            async def work_one(i, chunk):
                nonlocal completed, done_chars, last_update
                await generate_chunk(i, chunk, paths[i], settings, semaphore)
                completed += 1
                done_chars += len(chunk)

                elapsed = time.monotonic() - started
                eta = (total_chars - done_chars) / max(done_chars / max(elapsed, 1), 1)
                size_mb = sum(p.stat().st_size for p in paths if p.exists()) / 1048576
                status = (
                    f"🎙️ Audio generation\n\n"
                    f"{progress_bar(completed / len(chunks) * 100)} "
                    f"{completed / len(chunks) * 100:.1f}%\n"
                    f"Audio made: {completed}/{len(chunks)} chunks\n"
                    f"Estimated audio: {format_time(total_chars / 12)}\n"
                    f"Est time remaining: {format_time(eta)}\n"
                    f"Used time: {format_time(elapsed)}\n"
                    f"File size: {size_mb:.1f} MB"
                )
                USER_JOBS[uid]["status"] = status
                now = time.monotonic()
                if now - last_update > 3 or completed == len(chunks):
                    last_update = now
                    try:
                        await status_message.edit_text(status)
                    except Exception:
                        pass

            tasks = [asyncio.create_task(work_one(i, c)) for i, c in enumerate(chunks)]
            try:
                await asyncio.gather(*tasks)
            except Exception:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            output_name = safe_name(USER_NAMES.get(uid, Path(document.file_name).stem))
            output_path = work_dir / f"{output_name}.mp3"
            await status_message.edit_text("Merging audio chunks...")
            await asyncio.to_thread(merge_audio, paths, output_path, work_dir)

            max_bytes = min(SEND_PART_MB, MAX_UPLOAD_MB) * 1024 * 1024
            parts = await asyncio.to_thread(
                split_large_audio, output_path, work_dir, max_bytes
            )

            for i, part in enumerate(parts, 1):
                caption = output_name
                if len(parts) > 1:
                    caption += f" — Part {i}/{len(parts)}"
                with part.open("rb") as audio:
                    await message.reply_audio(
                        audio=audio,
                        filename=part.name,
                        caption=caption,
                        read_timeout=900,
                        write_timeout=900,
                        connect_timeout=120,
                        pool_timeout=120,
                    )

            elapsed = time.monotonic() - started
            await status_message.edit_text(
                f"✅ Completed\nChunks: {len(chunks)}\n"
                f"Parts sent: {len(parts)}\n"
                f"Used time: {format_time(elapsed)}\n"
                f"Final size: {output_path.stat().st_size / 1048576:.1f} MB"
            )

        except asyncio.CancelledError:
            try:
                await status_message.edit_text("Job cancelled.")
            except Exception:
                pass
            raise
        except Exception as exc:
            log.exception("TTS job failed")
            try:
                await status_message.edit_text(
                    f"❌ Job failed:\n{str(exc)[:2500]}"
                )
            except Exception:
                pass
        finally:
            USER_JOBS.pop(uid, None)
            shutil.rmtree(work_dir, ignore_errors=True)

    task = asyncio.create_task(run_job())
    USER_JOBS[uid] = {"task": task, "status": "Job started."}


async def error_handler(update, context):
    log.error("Telegram handler error", exc_info=context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing from GitHub Secrets.")

    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_BOT_API:
        builder = (
            builder
            .base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
        )

    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("voice", voice_command))
    app.add_handler(CommandHandler("engine", engine_command))
    app.add_handler(CommandHandler("rate", rate_command))
    app.add_handler(CommandHandler("pitch", pitch_command))
    app.add_handler(CommandHandler("name", name_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_error_handler(error_handler)

    log.info("Bot starting. Local Bot API enabled: %s", LOCAL_BOT_API)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
