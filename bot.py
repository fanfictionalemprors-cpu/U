
import asyncio
import logging
import os
import random
import re
import shutil
import tempfile
import time
from pathlib import Path

import edge_tts
from gtts import gTTS
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOCAL_API = os.getenv("LOCAL_API_URL", "").rstrip("/")

BITRATE_KBPS = 48
SAFETY = 0.97
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1500"))
CONCURRENCY = int(os.getenv("TTS_CONCURRENCY", "2"))
TTS_RETRIES = int(os.getenv("TTS_RETRIES", "7"))
TTS_TIMEOUT_SECONDS = int(os.getenv("TTS_TIMEOUT_SECONDS", "75"))
PROGRESS_INTERVAL = 12
PERFORMER = "AudioBook Pro"

if LOCAL_API:
    MAX_PART_BYTES = 2000 * 1000 * 1000
    MAX_DOWNLOAD_BYTES = 2000 * 1000 * 1000
else:
    MAX_PART_BYTES = 50 * 1000 * 1000
    MAX_DOWNLOAD_BYTES = 20 * 1000 * 1000

MAX_PART_SECONDS = MAX_PART_BYTES * SAFETY / (BITRATE_KBPS * 1000 / 8)

if LOCAL_API:
    MAX_PART_SECONDS = min(
        MAX_PART_SECONDS,
        int(os.getenv("PART_MINUTES", "180")) * 60,
    )

VOICES = {
    "hi_m": ("🇮🇳 Hindi - Madhur (M)", "hi-IN-MadhurNeural"),
    "hi_f": ("🇮🇳 Hindi - Swara (F)", "hi-IN-SwaraNeural"),
    "en_m": ("🇬🇧 English - Guy (M)", "en-US-GuyNeural"),
    "en_f": ("🇺🇸 English - Aria (F)", "en-US-AriaNeural"),
}

RATES = [
    ("1.0x", 1.0),
    ("1.1x", 1.1),
    ("1.2x", 1.2),
    ("1.3x", 1.3),
    ("1.4x", 1.4),
    ("1.5x", 1.5),
    ("1.6x", 1.6),
    ("1.7x", 1.7),
    ("1.8x", 1.8),
    ("1.9x", 1.9),
    ("2.0x", 2.0),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ttsbot")


# ================= HELPERS =================

def fmt_clock(seconds):
    seconds = max(0, int(seconds))
    return (
        f"{seconds // 3600}:"
        f"{(seconds % 3600) // 60:02d}:"
        f"{seconds % 60:02d}"
    )


def progress_bar(percent, width=12):
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def split_text(text, limit=CHUNK_CHARS):
    text = re.sub(r"[ \t]+", " ", text)
    sentences = re.split(r"(?<=[।.!?\n])\s*", text)
    chunks = []
    current = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit

            if current:
                chunks.append(current)
                current = ""

            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()

        if len(current) + len(sentence) + 1 > limit:
            if current:
                chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk]


async def run_command(*command):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="replace")[-1500:]
        )

    return stdout.decode().strip()


async def audio_duration(path):
    result = await run_command(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    return float(result)


# ================= TTS ENGINES =================

def is_http_403(error):
    return (
        getattr(error, "status", None) == 403
        or getattr(error, "status_code", None) == 403
        or "status=403" in repr(error)
        or "403 Forbidden" in str(error)
    )


async def edge_tts_chunk(text, voice, output, rate):
    last_error = None
    rate_percent = round((rate - 1) * 100)
    rate_string = f"{rate_percent:+d}%"

    for attempt in range(1, TTS_RETRIES + 1):
        output.unlink(missing_ok=True)

        try:
            communicate = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=rate_string,
            )

            await asyncio.wait_for(
                communicate.save(str(output)),
                timeout=TTS_TIMEOUT_SECONDS,
            )

            if output.exists() and output.stat().st_size > 256:
                return

            raise RuntimeError("Edge TTS generated empty audio")

        except asyncio.CancelledError:
            output.unlink(missing_ok=True)
            raise

        except Exception as error:
            last_error = error
            output.unlink(missing_ok=True)

            log.warning(
                "Edge TTS attempt %s/%s failed: %r",
                attempt,
                TTS_RETRIES,
                error,
            )

            # 403 usually will not be fixed by repeating the same request.
            if is_http_403(error):
                break

            if attempt < TTS_RETRIES:
                await asyncio.sleep(
                    min(30, 2 ** attempt) + random.uniform(0, 1)
                )

    raise RuntimeError(f"Edge TTS failed: {last_error!r}")


async def google_tts_chunk(text, voice, output, rate):
    language = "hi" if voice.startswith("hi-IN") else "en"

    output.unlink(missing_ok=True)
    raw_output = output.with_name(output.stem + "_raw.mp3")

    def generate():
        gTTS(
            text=text,
            lang=language,
            slow=False,
        ).save(str(raw_output))

    try:
        await asyncio.wait_for(
            asyncio.to_thread(generate),
            timeout=120,
        )

        if not raw_output.exists() or raw_output.stat().st_size <= 256:
            raise RuntimeError("gTTS generated empty audio")

        # FFmpeg atempo changes playback speed and generally preserves pitch.
        await asyncio.wait_for(
            run_command(
                "ffmpeg",
                "-y",
                "-i", str(raw_output),
                "-vn",
                "-filter:a", f"atempo={rate:.1f}",
                "-b:a", f"{BITRATE_KBPS}k",
                str(output),
            ),
            timeout=180,
        )

        if output.exists() and output.stat().st_size > 256:
            log.info(
                "gTTS fallback succeeded; rate=%sx",
                rate,
            )
            return

        raise RuntimeError("gTTS fallback output is empty")

    finally:
        raw_output.unlink(missing_ok=True)


async def tts_chunk(text, voice, output, rate, semaphore):
    async with semaphore:
        try:
            await edge_tts_chunk(text, voice, output, rate)

        except asyncio.CancelledError:
            raise

        except Exception as edge_error:
            log.warning(
                "Edge TTS failed; trying gTTS fallback: %r",
                edge_error,
            )

            try:
                await google_tts_chunk(
                    text,
                    voice,
                    output,
                    rate,
                )
            except asyncio.CancelledError:
                raise
            except Exception as fallback_error:
                output.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Edge TTS failed: {edge_error!r}; "
                    f"gTTS fallback failed: {fallback_error!r}"
                ) from fallback_error


# ================= MP3 PROCESSING =================

async def concat_mp3(files, output):
    list_file = output.with_suffix(".concat.txt")

    try:
        lines = []

        for file in files:
            safe_path = str(file.resolve()).replace("'", "'\\''")
            lines.append(f"file '{safe_path}'\n")

        list_file.write_text(
            "".join(lines),
            encoding="utf-8",
        )

        await run_command(
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", f"{BITRATE_KBPS}k",
            "-ac", "1",
            str(output),
        )

    finally:
        list_file.unlink(missing_ok=True)


# ================= TELEGRAM UI =================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to AudioBook Pro!\n\n"
        "📄 Send a .txt file.\n"
        "🎙 Choose a voice.\n"
        "⚡ Select speech rate from 1.0x to 2.0x.\n"
        "🎧 Receive MP3 audiobook parts with progress and duration."
    )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text(
            "❌ Please send a .txt file."
        )
        return

    if document.file_size and document.file_size > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"❌ File limit: {MAX_DOWNLOAD_BYTES // 1_000_000} MB."
        )
        return

    work = Path(tempfile.mkdtemp(prefix="audiobook_"))

    try:
        input_path = work / "input.txt"
        telegram_file = await ctx.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(input_path)

        ctx.user_data["job"] = {
            "dir": str(work),
            "path": str(input_path),
            "name": Path(document.file_name).stem,
        }

        keyboard = [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"voice:{key}",
                )
            ]
            for key, (label, _) in VOICES.items()
        ]

        await update.message.reply_text(
            f"📘 File received: {document.file_name}\n"
            "Choose your voice:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        log.exception("File download failed")
        await update.message.reply_text(
            "❌ File download failed. Please try again."
        )


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    job = ctx.user_data.get("job")

    if not job:
        await query.edit_message_text(
            "❌ File session expired. Please send the TXT again."
        )
        return

    voice_key = query.data.split(":", 1)[1]

    if voice_key not in VOICES:
        await query.edit_message_text("❌ Invalid voice selection.")
        return

    ctx.user_data["selected_voice"] = VOICES[voice_key][1]

    keyboard = []

    for label, rate in RATES:
        keyboard.append([
            InlineKeyboardButton(
                label,
                callback_data=f"rate:{rate:.1f}",
            )
        ])

    await query.edit_message_text(
        f"🎙 Voice: {VOICES[voice_key][0]}\n\n"
        "⚡ Select speech rate:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_rate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    job = ctx.user_data.get("job")
    voice = ctx.user_data.get("selected_voice")

    if not job or not voice:
        await query.edit_message_text(
            "❌ Session expired. Please send the TXT file again."
        )
        return

    try:
        rate = float(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.edit_message_text("❌ Invalid rate.")
        return

    allowed_rates = {value for _, value in RATES}

    if rate not in allowed_rates:
        await query.edit_message_text("❌ Unsupported rate.")
        return

    ctx.user_data["job"] = None
    ctx.user_data["selected_voice"] = None

    status = await query.edit_message_text(
        f"⏳ Starting audiobook at {rate:.1f}x..."
    )

    ctx.application.create_task(
        process(
            status=status,
            ctx=ctx,
            job=job,
            voice=voice,
            rate=rate,
            chat_id=query.message.chat_id,
        )
    )


# ================= AUDIOBOOK WORKER =================

async def process(status, ctx, job, voice, rate, chat_id):
    work = Path(job["dir"])
    input_path = Path(job["path"])
    name = job["name"]

    tasks = []
    started_at = time.monotonic()

    try:
        raw = input_path.read_bytes()

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-16", errors="replace")

        chunks = split_text(text)
        total = len(chunks)

        if not total:
            await status.edit_text("❌ The TXT file is empty.")
            return

        semaphore = asyncio.Semaphore(CONCURRENCY)
        chunk_files = [
            work / f"chunk_{i:05d}.mp3"
            for i in range(total)
        ]

        done = 0

        async def worker(index):
            nonlocal done

            await tts_chunk(
                chunks[index],
                voice,
                chunk_files[index],
                rate,
                semaphore,
            )
            done += 1

        tasks = [
            asyncio.create_task(worker(i))
            for i in range(total)
        ]

        await status.edit_text(
            f"🎙 Audiobook generation started\n"
            f"⚡ Speech rate: {rate:.1f}x\n"
            f"{progress_bar(0)} 0% (0/{total} chunks)\n"
            "🎧 MP3 duration: calculating...\n"
            "⏱ Elapsed: 0:00:00 | ETA: calculating..."
        )

        last_percent = -1
        last_update = time.monotonic()

        while not all(task.done() for task in tasks):
            await asyncio.sleep(3)

            failed_task = next(
                (
                    task for task in tasks
                    if task.done()
                    and not task.cancelled()
                    and task.exception() is not None
                ),
                None,
            )

            if failed_task is not None:
                for task in tasks:
                    if not task.done():
                        task.cancel()

                await asyncio.gather(
                    *tasks,
                    return_exceptions=True,
                )
                failed_task.result()

            percent = done * 100 // total
            elapsed = time.monotonic() - started_at

            if (
                percent == last_percent
                and time.monotonic() - last_update < PROGRESS_INTERVAL
            ):
                continue

            last_percent = percent
            last_update = time.monotonic()

            eta = (
                elapsed / done * (total - done)
                if done else None
            )

            completed_duration = 0.0

            for file in chunk_files:
                if file.exists() and file.stat().st_size > 256:
                    try:
                        completed_duration += await audio_duration(file)
                    except Exception:
                        log.warning("Duration probe failed: %s", file.name)

            estimated_duration = (
                completed_duration / done * total
                if done and completed_duration else 0
            )

            message = (
                f"🎙 Audiobook generation\n"
                f"⚡ Rate: {rate:.1f}x\n"
                f"{progress_bar(percent)} {percent}% "
                f"({done}/{total} chunks)\n"
                f"🎧 Completed MP3 duration: "
                f"{fmt_clock(completed_duration)}\n"
            )

            if estimated_duration:
                message += (
                    f"📀 Estimated total duration: "
                    f"~{fmt_clock(estimated_duration)}\n"
                )

            message += (
                f"⏱ Elapsed: {fmt_clock(elapsed)} | "
                f"ETA: "
                f"{fmt_clock(eta) if eta is not None else 'calculating...'}"
            )

            if done == 0:
                message += "\n🔄 Processing first chunk..."

            try:
                await status.edit_text(message)
            except Exception:
                log.warning("Progress update failed", exc_info=True)

        for task in tasks:
            task.result()

        tts_elapsed = time.monotonic() - started_at

        durations = [
            await audio_duration(file)
            for file in chunk_files
        ]
        total_duration = sum(durations)

        await status.edit_text(
            f"✅ Audio generation complete\n"
            f"{progress_bar(100)} 100% ({total}/{total})\n"
            f"⚡ Rate: {rate:.1f}x\n"
            f"🎧 Total MP3 duration: {fmt_clock(total_duration)}\n"
            f"⏱ TTS processing time: {fmt_clock(tts_elapsed)}\n"
            "📤 Preparing MP3 parts..."
        )

        groups = []
        current_group = []
        current_duration = 0.0

        for file, duration in zip(chunk_files, durations):
            if (
                current_group
                and current_duration + duration > MAX_PART_SECONDS
            ):
                groups.append((current_group, current_duration))
                current_group = []
                current_duration = 0.0

            current_group.append(file)
            current_duration += duration

        if current_group:
            groups.append((current_group, current_duration))

        total_parts = len(groups)

        for part_number, (group, part_duration) in enumerate(groups, 1):
            output = work / f"{name} - Part {part_number}.mp3"

            await concat_mp3(group, output)

            if output.stat().st_size > MAX_PART_BYTES:
                raise RuntimeError(
                    f"Part {part_number} exceeds the upload limit."
                )

            elapsed = time.monotonic() - started_at

            await status.edit_text(
                "📤 Sending audiobook parts\n"
                f"⚡ Rate: {rate:.1f}x\n"
                f"{progress_bar((part_number - 1) * 100 // total_parts)} "
                f"{(part_number - 1) * 100 // total_parts}%\n"
                f"📀 Part: {part_number}/{total_parts}\n"
                f"🎧 Total duration: {fmt_clock(total_duration)}\n"
                f"⏱ Elapsed: {fmt_clock(elapsed)}"
            )

            with output.open("rb") as audio_file:
                await ctx.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=f"{name} - Part {part_number}",
                    performer=PERFORMER,
                    duration=int(part_duration),
                    caption=(
                        f"📚 {name}\n"
                        f"🎧 Part {part_number}/{total_parts}\n"
                        f"⚡ Rate: {rate:.1f}x\n"
                        f"⏱ Duration: {fmt_clock(part_duration)}"
                    ),
                    filename=output.name,
                    read_timeout=900,
                    write_timeout=900,
                    connect_timeout=60,
                )

            output.unlink(missing_ok=True)

        await status.edit_text(
            "✅ Audiobook completed!\n"
            f"{progress_bar(100)} 100%\n"
            f"⚡ Speech rate: {rate:.1f}x\n"
            f"🎧 Total MP3 duration: {fmt_clock(total_duration)}\n"
            f"📀 Total parts: {total_parts}\n"
            f"⏱ Total processing time: "
            f"{fmt_clock(time.monotonic() - started_at)}"
        )

    except asyncio.CancelledError:
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    except Exception as error:
        log.exception("Audiobook processing failed")

        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await status.edit_text(
                f"❌ TTS/Audiobook error:\n{str(error)[:2500]}"
            )
        except Exception:
            log.exception("Unable to report audiobook error")

    finally:
        shutil.rmtree(work, ignore_errors=True)


# ================= START BOT =================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable is missing.")

    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_API:
        builder = (
            builder.base_url(f"{LOCAL_API}/bot")
            .base_file_url(f"{LOCAL_API}/file/bot")
            .local_mode(True)
        )

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.Document.ALL, on_document)
    )
    app.add_handler(
        CallbackQueryHandler(on_voice, pattern=r"^voice:")
    )
    app.add_handler(
        CallbackQueryHandler(on_rate, pattern=r"^rate:")
    )

    log.info("AudioBook Pro starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
