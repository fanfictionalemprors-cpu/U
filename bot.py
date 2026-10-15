
import os
import re
import time
import math
import asyncio
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor

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

# Optional input format libraries
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


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "100"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
SEND_PART_MB = int(os.getenv("SEND_PART_MB", "45"))

CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))
TTS_CONCURRENCY = max(1, min(int(os.getenv("TTS_CONCURRENCY", "3")), 10))

DEFAULT_RATE = os.getenv("DEFAULT_RATE", "+10%")
DEFAULT_PITCH = os.getenv("DEFAULT_PITCH", "+0Hz")
DEFAULT_ENGINE = os.getenv("DEFAULT_ENGINE", "edge").lower()

WORK_DIR = Path(os.getenv("TTS_WORK_DIR", "tts_work"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("tts-bot")


# ============================================================
# VOICES
# ============================================================

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


# ============================================================
# PROXY SUPPORT
# Supports PROXY_LIST, PROXY_1...PROXY_10, or individual
# PROXY_1_HOST / PROXY_1_PORT / PROXY_1_USERNAME /
# PROXY_1_PASSWORD variables.
# ============================================================

def load_proxies():
    proxies = []

    proxy_list = os.getenv("PROXY_LIST", "").strip()
    if proxy_list:
        for item in re.split(r"[\n,;]+", proxy_list):
            item = item.strip()
            if item:
                proxies.append(item)

    for i in range(1, 11):
        direct = os.getenv(f"PROXY_{i}", "").strip()
        if direct:
            proxies.append(direct)
            continue

        host = os.getenv(f"PROXY_{i}_HOST", "").strip()
        port = os.getenv(f"PROXY_{i}_PORT", "").strip()
        username = os.getenv(f"PROXY_{i}_USERNAME", "").strip()
        password = os.getenv(f"PROXY_{i}_PASSWORD", "").strip()

        if host and port:
            if username:
                proxies.append(
                    f"http://{username}:{password}@{host}:{port}"
                )
            else:
                proxies.append(f"http://{host}:{port}")

    # Backwards compatibility with single-proxy secrets
    host = os.getenv("PROXY_HOST", "").strip()
    port = os.getenv("PROXY_PORT", "").strip()
    username = os.getenv("PROXY_USERNAME", "").strip()
    password = os.getenv("PROXY_PASSWORD", "").strip()

    if host and port:
        if username:
            proxies.append(f"http://{username}:{password}@{host}:{port}")
        else:
            proxies.append(f"http://{host}:{port}")

    # Remove duplicates while preserving order
    return list(dict.fromkeys(proxies))[:10]


PROXIES = load_proxies()


def get_proxy(index):
    if not PROXIES:
        return None
    return PROXIES[index % len(PROXIES)]


# ============================================================
# USER SETTINGS AND JOB STATUS
# ============================================================

USER_SETTINGS = {}
USER_JOBS = {}
USER_NAMES = {}
JOB_LOCKS = {}


def settings_for(user_id):
    if user_id not in USER_SETTINGS:
        USER_SETTINGS[user_id] = {
            "voice": "madhur",
            "engine": DEFAULT_ENGINE,
            "rate": DEFAULT_RATE,
            "pitch": DEFAULT_PITCH,
        }
    return USER_SETTINGS[user_id]


def progress_bar(percent, width=16):
    percent = max(0, min(100, int(percent)))
    filled = int(width * percent / 100)
    return "[" + ("█" * filled) + ("░" * (width - filled)) + "]"


def format_time(seconds):
    seconds = max(0, int(seconds))
    return str(timedelta(seconds=seconds))


def split_text(text, max_chars=CHUNK_CHARS):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    if not text:
        return []

    chunks = []
    current = ""

    # Prefer paragraph/sentence boundaries.
    pieces = re.split(r"(\n\n+|(?<=[.!?।])\s+)", text)

    for piece in pieces:
        if not piece:
            continue

        if len(piece) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""

            for start in range(0, len(piece), max_chars):
                part = piece[start:start + max_chars].strip()
                if part:
                    chunks.append(part)
            continue

        if len(current) + len(piece) > max_chars:
            if current.strip():
                chunks.append(current.strip())
            current = piece.strip()
        else:
            current += piece

    if current.strip():
        chunks.append(current.strip())

    return chunks


# ============================================================
# INPUT FILE EXTRACTION
# ============================================================

def extract_text(file_path):
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".txt":
        for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
            try:
                return path.read_text(encoding=encoding)
            except UnicodeDecodeError:
                continue
        raise ValueError("Could not decode this TXT file.")

    if suffix == ".docx":
        if Document is None:
            raise RuntimeError("Install python-docx to read DOCX files.")

        doc = Document(str(path))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]

        for table in doc.tables:
            for row in table.rows:
                paragraphs.append(" | ".join(cell.text for cell in row.cells))

        return "\n\n".join(paragraphs)

    if suffix == ".pdf":
        if PdfReader is None:
            raise RuntimeError("Install pypdf to read PDF files.")

        reader = PdfReader(str(path))
        pages = []

        for page in reader.pages:
            pages.append(page.extract_text() or "")

        return "\n\n".join(pages)

    if suffix == ".epub":
        if epub is None or BeautifulSoup is None:
            raise RuntimeError(
                "Install EbookLib and beautifulsoup4 to read EPUB files."
            )

        book = epub.read_epub(str(path))
        sections = []

        for item in book.get_items():
            if item.get_type() == 9:  # EbookLib ITEM_DOCUMENT
                soup = BeautifulSoup(item.get_content(), "html.parser")
                for tag in soup(["script", "style"]):
                    tag.decompose()

                content = soup.get_text("\n", strip=True)
                if content:
                    sections.append(content)

        return "\n\n".join(sections)

    raise ValueError("Supported files: TXT, DOCX, PDF and EPUB.")


# ============================================================
# AUDIO GENERATION
# ============================================================

async def make_edge(text, output_path, settings, proxy=None):
    voice_key = settings.get("voice", "madhur")
    voice = VOICES.get(voice_key, VOICES["madhur"])["voice"]

    communicate_args = {
        "text": text,
        "voice": voice,
        "rate": settings.get("rate", DEFAULT_RATE),
        "pitch": settings.get("pitch", DEFAULT_PITCH),
    }

    if proxy:
        communicate_args["proxy"] = proxy

    communicate = Communicate(**communicate_args)
    await communicate.save(str(output_path))

    if not Path(output_path).is_file() or Path(output_path).stat().st_size == 0:
        raise RuntimeError("Edge TTS generated an empty file.")


async def make_google(text, output_path, settings):
    """
    gTTS does not accept session= or proxies= in its constructor.
    This function deliberately does not pass unsupported arguments.
    """
    voice_key = settings.get("voice", "madhur")
    lang = VOICES.get(voice_key, VOICES["madhur"])["lang"]

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

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
        raise RuntimeError("Google TTS generated an empty file.")


async def generate_chunk(index, text, output_path, settings, semaphore):
    async with semaphore:
        engine = settings.get("engine", "edge").lower()
        last_error = None

        for attempt in range(3):
            try:
                if engine == "google":
                    await make_google(text, output_path, settings)
                else:
                    await make_edge(
                        text,
                        output_path,
                        settings,
                        proxy=get_proxy(index + attempt),
                    )

                return output_path

            except Exception as exc:
                last_error = exc
                log.warning(
                    "Chunk %s attempt %s failed: %s",
                    index + 1,
                    attempt + 1,
                    exc,
                )

                # Try Google as fallback after Edge failures.
                if engine != "google" and attempt == 1:
                    try:
                        await make_google(text, output_path, settings)
                        return output_path
                    except Exception as fallback_error:
                        last_error = fallback_error
                        log.warning("Google fallback failed: %s", fallback_error)

                await asyncio.sleep(min(2 ** attempt, 8))

        raise RuntimeError(
            f"Chunk {index + 1} failed after retries: {last_error}"
        )


# ============================================================
# FFMPEG HELPERS
# ============================================================

def run_command(args):
    result = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            result.stderr[-1500:] or "Command failed."
        )

    return result


def get_duration(file_path):
    result = run_command([
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(file_path),
    ])

    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def merge_audio(audio_paths, output_path, work_dir):
    if not audio_paths:
        raise RuntimeError("No audio chunks were generated.")

    list_file = Path(work_dir) / "concat_list.txt"

    with list_file.open("w", encoding="utf-8") as file:
        for audio_path in audio_paths:
            safe_path = str(Path(audio_path).resolve()).replace("'", "'\\''")
            file.write(f"file '{safe_path}'\n")

    run_command([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-f", "concat",
        "-safe", "0",
        "-i", str(list_file),
        "-vn",
        "-c:a", "libmp3lame",
        "-b:a", "64k",
        "-y",
        str(output_path),
    ])

    if not Path(output_path).is_file() or Path(output_path).stat().st_size == 0:
        raise RuntimeError("Audio merge failed.")

    return output_path


def split_large_audio(audio_path, work_dir, max_bytes):
    audio_path = Path(audio_path)
    size = audio_path.stat().st_size

    if size <= max_bytes:
        return [audio_path]

    duration = get_duration(audio_path)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration for splitting.")

    # Estimate each part's duration from the file's average bitrate.
    parts_needed = math.ceil(size / max_bytes)
    part_duration = max(60, int(duration / parts_needed))

    output_pattern = str(Path(work_dir) / "part_%03d.mp3")

    run_command([
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(audio_path),
        "-f", "segment",
        "-segment_time", str(part_duration),
        "-reset_timestamps", "1",
        "-c:a", "libmp3lame",
        "-b:a", "48k",
        "-y",
        output_pattern,
    ])

    parts = sorted(Path(work_dir).glob("part_*.mp3"))

    if not parts:
        raise RuntimeError("Could not split large audio file.")

    # Verify every part fits within the requested size.
    for part in parts:
        if part.stat().st_size > max_bytes:
            raise RuntimeError(
                f"{part.name} exceeds the size limit. "
                "Lower SEND_PART_MB and try again."
            )

    return parts


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = (
        "🎧 **Novel TTS Bot**\n\n"
        "Send a TXT, DOCX, PDF or EPUB file to create an audiobook.\n\n"
        "**Commands**\n"
        "/voice — choose voice\n"
        "/engine edge or /engine google — choose engine\n"
        "/rate +10% — set speech rate for Edge TTS\n"
        "/pitch +0Hz — set pitch for Edge TTS\n"
        "/name My Audiobook — set output name\n"
        "/status — check current job\n"
        "/cancel — cancel current job\n\n"
        "Available voices: madhur, swara, guy, jenny"
    )

    await update.effective_message.reply_text(
        message,
        parse_mode="Markdown",
    )


async def voice_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    settings = settings_for(user_id)

    if not context.args:
        lines = ["Available voices:"]
        for key, item in VOICES.items():
            marker = " ✓" if settings["voice"] == key else ""
            lines.append(f"{key} — {item['name']}{marker}")

        await update.effective_message.reply_text("\n".join(lines))
        return

    choice = context.args[0].lower()

    if choice not in VOICES:
        await update.effective_message.reply_text(
            "Invalid voice. Choose: madhur, swara, guy or jenny."
        )
        return

    settings["voice"] = choice

    await update.effective_message.reply_text(
        f"Voice set to {VOICES[choice]['name']}."
    )


async def engine_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    settings = settings_for(user_id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current engine: {settings['engine']}\n"
            "Use /engine edge or /engine google."
        )
        return

    engine = context.args[0].lower()

    if engine not in {"edge", "google"}:
        await update.effective_message.reply_text(
            "Choose edge or google."
        )
        return

    settings["engine"] = engine

    await update.effective_message.reply_text(
        f"TTS engine set to {engine}."
    )


async def rate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    settings = settings_for(user_id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current rate: {settings['rate']}\nExample: /rate +10%"
        )
        return

    rate = context.args[0]
    if not re.fullmatch(r"[+-]?\d{1,3}%", rate):
        await update.effective_message.reply_text(
            "Invalid rate. Example: /rate +10%"
        )
        return

    settings["rate"] = rate
    await update.effective_message.reply_text(f"Rate set to {rate}.")


async def pitch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    settings = settings_for(user_id)

    if not context.args:
        await update.effective_message.reply_text(
            f"Current pitch: {settings['pitch']}\nExample: /pitch +0Hz"
        )
        return

    pitch = context.args[0]
    if not re.fullmatch(r"[+-]?\d{1,4}Hz", pitch):
        await update.effective_message.reply_text(
            "Invalid pitch. Example: /pitch +0Hz"
        )
        return

    settings["pitch"] = pitch
    await update.effective_message.reply_text(f"Pitch set to {pitch}.")


async def name_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    if not context.args:
        current = USER_NAMES.get(user_id, "Audiobook")
        await update.effective_message.reply_text(
            f"Current output name: {current}\n"
            "Use /name My Audiobook"
        )
        return

    name = " ".join(context.args).strip()
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)[:100].strip(" .")

    if not name:
        name = "Audiobook"

    USER_NAMES[user_id] = name
    await update.effective_message.reply_text(
        f"Output name set to: {name}"
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    job = USER_JOBS.get(user_id)

    if not job:
        await update.effective_message.reply_text("No active audiobook job.")
        return

    await update.effective_message.reply_text(
        job.get("status_text", "Job is running.")
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    job = USER_JOBS.get(user_id)

    if not job:
        await update.effective_message.reply_text("No active job to cancel.")
        return

    task = job.get("task")
    if task and not task.done():
        task.cancel()
        await update.effective_message.reply_text("Cancellation requested.")
    else:
        await update.effective_message.reply_text("No running task found.")


# ============================================================
# DOCUMENT HANDLER
# ============================================================

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    user_id = str(user.id)

    document = message.document
    if not document:
        return

    suffix = Path(document.file_name or "").suffix.lower()
    if suffix not in {".txt", ".docx", ".pdf", ".epub"}:
        await message.reply_text(
            "Unsupported file. Send TXT, DOCX, PDF or EPUB."
        )
        return

    if document.file_size and document.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(
            f"Input is too large. Limit: {MAX_INPUT_MB} MB."
        )
        return

    if user_id in USER_JOBS:
        old_task = USER_JOBS[user_id].get("task")
        if old_task and not old_task.done():
            await message.reply_text(
                "You already have a running job. Use /cancel first."
            )
            return

    work_dir = Path(tempfile.mkdtemp(prefix="tts_", dir=WORK_DIR))
    status_message = await message.reply_text("Downloading your file...")

    async def run_job():
        start_time = time.monotonic()
        settings = settings_for(user_id).copy()
        input_path = work_dir / f"input{suffix}"

        try:
            tg_file = await document.get_file()
            await tg_file.download_to_drive(custom_path=str(input_path))

            if not input_path.is_file():
                raise RuntimeError("Input download failed.")

            text = await asyncio.to_thread(extract_text, input_path)
            text = text.strip()

            if not text:
                raise RuntimeError(
                    "No readable text found. Scanned PDFs may need OCR."
                )

            chunks = split_text(text)
            if not chunks:
                raise RuntimeError("No text chunks were produced.")

            total = len(chunks)
            semaphore = asyncio.Semaphore(TTS_CONCURRENCY)
            audio_paths = [
                work_dir / f"chunk_{index:05d}.mp3"
                for index in range(total)
            ]

            completed = 0
            completed_chars = 0
            total_chars = len(text)
            last_update = 0.0

            async def update_progress(index, chunk):
                nonlocal completed, completed_chars, last_update

                await generate_chunk(
                    index,
                    chunk,
                    audio_paths[index],
                    settings,
                    semaphore,
                )

                completed += 1
                completed_chars += len(chunk)

                now = time.monotonic()
                elapsed = now - start_time

                percent = completed / total * 100
                speed = completed_chars / max(elapsed, 1)
                remaining_chars = max(0, total_chars - completed_chars)
                eta = remaining_chars / max(speed, 1)

                status_text = (
                    f"🎙️ **Audio generation**\n\n"
                    f"{progress_bar(percent)} {percent:.1f}%\n"
                    f"Audio made: {completed}/{total} chunks\n"
                    f"Estimated audio: {format_time(total_chars / 12)}\n"
                    f"Est time remaining: {format_time(eta)}\n"
                    f"Used time: {format_time(elapsed)}\n"
                    f"File size: "
                    f"{sum(p.stat().st_size for p in audio_paths if p.exists()) / 1048576:.1f} MB"
                )

                USER_JOBS[user_id]["status_text"] = status_text

                if now - last_update >= 3 or completed == total:
                    last_update = now
                    try:
                        await status_message.edit_text(
                            status_text,
                            parse_mode="Markdown",
                        )
                    except Exception:
                        pass

            # Run a limited number of chunk jobs concurrently.
            tasks = [
                asyncio.create_task(update_progress(i, chunk))
                for i, chunk in enumerate(chunks)
            ]

            try:
                await asyncio.gather(*tasks)
            except Exception:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            output_name = USER_NAMES.get(user_id, Path(document.file_name).stem)
            output_name = re.sub(r'[\\/:*?"<>|]+', "_", output_name).strip(" .")
            if not output_name:
                output_name = "Audiobook"

            output_path = work_dir / f"{output_name}.mp3"

            await status_message.edit_text(
                "Merging audio chunks in the correct order..."
            )

            await asyncio.to_thread(
                merge_audio,
                audio_paths,
                output_path,
                work_dir,
            )

            max_bytes = min(SEND_PART_MB, MAX_UPLOAD_MB) * 1024 * 1024
            parts = await asyncio.to_thread(
                split_large_audio,
                output_path,
                work_dir,
                max_bytes,
            )

            elapsed = time.monotonic() - start_time

            for index, part in enumerate(parts, start=1):
                caption = f"🎧 {output_name}"
                if len(parts) > 1:
                    caption += f" — Part {index}/{len(parts)}"

                with part.open("rb") as audio_file:
                    await message.reply_audio(
                        audio=audio_file,
                        filename=part.name,
                        caption=caption,
                        read_timeout=300,
                        write_timeout=300,
                        connect_timeout=60,
                    )

            await status_message.edit_text(
                "✅ Audiobook completed!\n\n"
                f"Chunks: {total}\n"
                f"Parts sent: {len(parts)}\n"
                f"Total time: {format_time(elapsed)}\n"
                f"Final size: {output_path.stat().st_size / 1048576:.1f} MB"
            )

        except asyncio.CancelledError:
            try:
                await status_message.edit_text("⏹️ Job cancelled.")
            except Exception:
                pass
            raise

        except Exception as exc:
            log.exception("Audiobook job failed")
            try:
                await status_message.edit_text(
                    f"❌ Job failed:\n{str(exc)[:3000]}"
                )
            except Exception:
                pass

        finally:
            USER_JOBS.pop(user_id, None)

            # Clean temporary files after the job.
            try:
                import shutil
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass

    task = asyncio.create_task(run_job())

    USER_JOBS[user_id] = {
        "task": task,
        "status_text": "Job started. Preparing input file...",
    }


# ============================================================
# ERROR HANDLER AND MAIN
# ============================================================

async def error_handler(update, context):
    log.error(
        "Telegram handler error",
        exc_info=context.error,
    )


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it to GitHub Actions Secrets."
        )

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("voice", voice_command))
    application.add_handler(CommandHandler("engine", engine_command))
    application.add_handler(CommandHandler("rate", rate_command))
    application.add_handler(CommandHandler("pitch", pitch_command))
    application.add_handler(CommandHandler("name", name_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(
        MessageHandler(filters.Document.ALL, document_handler)
    )
    application.add_error_handler(error_handler)

    log.info("TTS bot starting.")
    log.info("Loaded %s proxies.", len(PROXIES))
    log.info("TTS concurrency: %s", TTS_CONCURRENCY)

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
