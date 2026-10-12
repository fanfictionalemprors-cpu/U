import asyncio
import logging
import os
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOCAL_API = os.getenv("LOCAL_API_URL", "").rstrip("/")
BITRATE_KBPS = 48
PERFORMER = "AudioBook Pro"

CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1000"))
CONCURRENCY = max(1, int(os.getenv("TTS_CONCURRENCY", "1")))
TTS_RETRIES = max(1, int(os.getenv("TTS_RETRIES", "3")))
TTS_TIMEOUT_SECONDS = int(os.getenv("TTS_TIMEOUT_SECONDS", "75"))

if LOCAL_API:
    MAX_PART_BYTES = 2_000_000_000
    MAX_DOWNLOAD_BYTES = 2_000_000_000
else:
    MAX_PART_BYTES = 50_000_000
    MAX_DOWNLOAD_BYTES = 20_000_000

MAX_PART_SECONDS = MAX_PART_BYTES * 0.97 / (BITRATE_KBPS * 1000 / 8)

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ttsbot")


def fmt_clock(seconds):
    seconds = max(0, int(seconds))
    return (
        f"{seconds // 3600}:"
        f"{(seconds % 3600) // 60:02d}:"
        f"{seconds % 60:02d}"
    )


def fmt_time(seconds):
    seconds = max(0, int(seconds))
    return (
        f"{seconds // 3600}h "
        f"{(seconds % 3600) // 60}m "
        f"{seconds % 60}s"
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

    return [chunk for chunk in chunks if chunk.strip()]


async def run_command(*command):
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(
            stderr.decode(errors="ignore")[-1000:]
        )

    return stdout.decode(errors="ignore").strip()


async def audio_duration(path):
    result = await run_command(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    )
    return float(result)


async def google_tts_chunk(text, output, rate):
    """
    gTTS fallback. Public Google TTS may return HTTP 429.
    Retries with backoff, but availability is not guaranteed.
    """
    language = (
        "hi"
        if any("\u0900" <= char <= "\u097f" for char in text)
        else "en"
    )

    raw_output = output.with_name(output.stem + "_raw.mp3")
    retry_delays = [30, 60, 120, 180]
    last_error = None

    for attempt in range(len(retry_delays) + 1):
        output.unlink(missing_ok=True)
        raw_output.unlink(missing_ok=True)

        try:
            def generate():
                gTTS(
                    text=text,
                    lang=language,
                    slow=False,
                ).save(str(raw_output))

            await asyncio.wait_for(
                asyncio.to_thread(generate),
                timeout=180,
            )

            if (
                not raw_output.exists()
                or raw_output.stat().st_size <= 256
            ):
                raise RuntimeError("gTTS returned empty audio")

            await asyncio.wait_for(
                run_command(
                    "ffmpeg",
                    "-y",
                    "-i", str(raw_output),
                    "-vn",
                    "-filter:a", f"atempo={rate:.1f}",
                    "-b:a", f"{BITRATE_KBPS}k",
                    "-ac", "1",
                    "-ar", "24000",
                    str(output),
                ),
                timeout=180,
            )

            if output.exists() and output.stat().st_size > 256:
                raw_output.unlink(missing_ok=True)
                return

            raise RuntimeError("Fallback MP3 is empty")

        except asyncio.CancelledError:
            output.unlink(missing_ok=True)
            raw_output.unlink(missing_ok=True)
            raise

        except Exception as error:
            last_error = error
            output.unlink(missing_ok=True)
            raw_output.unlink(missing_ok=True)

            log.warning(
                "gTTS fallback failed, attempt %s/%s: %r",
                attempt + 1,
                len(retry_delays) + 1,
                error,
            )

            if attempt < len(retry_delays):
                await asyncio.sleep(retry_delays[attempt])

    raise RuntimeError(
        f"gTTS fallback failed after retries: {last_error!r}"
    )


async def tts_chunk(text, voice, output, semaphore, rate):
    async with semaphore:
        output.unlink(missing_ok=True)

        try:
            edge_rate = f"+{int((rate - 1) * 100)}%"

            communicator = edge_tts.Communicate(
                text,
                voice,
                rate=edge_rate,
            )

            await asyncio.wait_for(
                communicator.save(str(output)),
                timeout=TTS_TIMEOUT_SECONDS,
            )

            if output.exists() and output.stat().st_size > 256:
                return

            raise RuntimeError("Edge TTS returned empty audio")

        except asyncio.CancelledError:
            output.unlink(missing_ok=True)
            raise

        except Exception as error:
            output.unlink(missing_ok=True)
            log.warning(
                "Edge TTS failed; switching to gTTS: %r",
                error,
            )

        await google_tts_chunk(text, output, rate)


async def concat_mp3(files, output):
    list_file = output.with_suffix(".concat.txt")

    # Escape apostrophes for FFmpeg concat-list syntax.
    lines = []
    for file in files:
        path_text = file.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{path_text}'\n")

    list_file.write_text("".join(lines), encoding="utf-8")

    try:
        await run_command(
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c:a", "libmp3lame",
            "-b:a", f"{BITRATE_KBPS}k",
            "-ac", "1",
            "-ar", "24000",
            str(output),
        )
    finally:
        list_file.unlink(missing_ok=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Namaste!\n\n"
        "Mujhe ek .txt file bhejo.\n"
        "Main use MP3 audiobook mein convert karunga.\n\n"
        "1. TXT file bhejo\n"
        "2. Voice choose karo\n"
        "3. Speed choose karo\n"
        "4. Audiobook parts receive karo 🎧"
    )


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document

    if not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text(
            "❌ Sirf .txt file bhejo."
        )
        return

    if (
        document.file_size
        and document.file_size > MAX_DOWNLOAD_BYTES
    ):
        await update.message.reply_text(
            "❌ File limit se badi hai: "
            f"{MAX_DOWNLOAD_BYTES // 1_000_000} MB."
        )
        return

    previous_job = context.user_data.get("job")
    if previous_job:
        await update.message.reply_text(
            "⚠️ Tumhari pichhli file ka selection abhi pending hai. "
            "Pehle uska process complete karo."
        )
        return

    work = Path(tempfile.mkdtemp(prefix="tts_"))
    input_path = work / "input.txt"

    try:
        telegram_file = await context.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(input_path)

        text = input_path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )

        if not text.strip():
            shutil.rmtree(work, ignore_errors=True)
            await update.message.reply_text(
                "❌ TXT file khaali hai."
            )
            return

        context.user_data["job"] = {
            "dir": str(work),
            "path": str(input_path),
            "name": Path(document.file_name).stem,
        }

        keyboard = [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"v:{key}",
                )
            ]
            for key, (label, voice_id) in VOICES.items()
        ]

        await update.message.reply_text(
            f"📘 {document.file_name} receive ho gayi.\n"
            "Voice choose karo:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as error:
        shutil.rmtree(work, ignore_errors=True)
        log.exception("File download failed")
        await update.message.reply_text(
            f"❌ File download/read error: {error}"
        )


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    job = context.user_data.get("job")
    if not job:
        await query.edit_message_text(
            "File expire ho gayi. TXT dobara bhejo."
        )
        return

    voice_key = query.data.split(":", 1)[1]
    if voice_key not in VOICES:
        await query.edit_message_text("Invalid voice.")
        return

    context.user_data["selected_voice"] = VOICES[voice_key][1]

    rates = [round(1.0 + index * 0.1, 1) for index in range(11)]
    keyboard = [
        [
            InlineKeyboardButton(
                f"{rate:.1f}x",
                callback_data=f"r:{rate:.1f}",
            )
            for rate in rates[index:index + 3]
        ]
        for index in range(0, len(rates), 3)
    ]

    await query.edit_message_text(
        "⚡ Playback speed choose karo:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    job = context.user_data.get("job")
    voice = context.user_data.get("selected_voice")

    if not job or not voice:
        await query.edit_message_text(
            "File/voice selection expire ho gayi. TXT dobara bhejo."
        )
        return

    try:
        rate = float(query.data.split(":", 1)[1])
        if not 1.0 <= rate <= 2.0:
            raise ValueError("Invalid speed")
    except ValueError:
        await query.edit_message_text("Invalid speed selection.")
        return

    await query.edit_message_text(
        f"🎧 Voice select ho gayi.\n"
        f"⚡ Speed: {rate:.1f}x\n"
        "Audiobook process start ho raha hai..."
    )

    await process_job(
        update=query,
        context=context,
        job=job,
        voice=voice,
        rate=rate,
    )


async def process_job(update, context, job, voice, rate):
    chat_id = update.message.chat_id if update.message else update.from_user.id
    status = await context.bot.send_message(
        chat_id=chat_id,
        text="📖 TXT read kar raha hoon...",
    )

    work = Path(job["dir"])
    input_path = Path(job["path"])
    name = job["name"]
    started_at = time.monotonic()

    try:
        text = input_path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )

        chunks = split_text(text)
        if not chunks:
            raise RuntimeError("TXT mein readable text nahi mila.")

        total = len(chunks)
        files = [
            work / f"chunk_{index:05d}.mp3"
            for index in range(total)
        ]

        await status.edit_text(
            f"📚 Total chunks: {total}\n"
            f"🎙 Voice: {voice}\n"
            f"⚡ Speed: {rate:.1f}x\n"
            f"{progress_bar(0)} 0%",
        )

        semaphore = asyncio.Semaphore(CONCURRENCY)
        tasks = [
            asyncio.create_task(
                tts_chunk(chunk, voice, files[index], semaphore, rate)
            )
            for index, chunk in enumerate(chunks)
        ]

        done = 0
        last_update = time.monotonic()

        while True:
            completed = sum(task.done() for task in tasks)
            if completed == total:
                break

            failed = [
                task for task in tasks
                if task.done() and not task.cancelled()
                and task.exception() is not None
            ]

            if failed:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                failed[0].result()

            now = time.monotonic()
            if now - last_update >= 5:
                done = sum(
                    task.done()
                    and not task.cancelled()
                    and task.exception() is None
                    for task in tasks
                )

                percent = int(done * 100 / total)
                elapsed = now - started_at
                eta = elapsed / done * (total - done) if done else 0

                completed_duration = 0.0
                for file in files:
                    if file.exists() and file.stat().st_size > 256:
                        try:
                            completed_duration += await audio_duration(file)
                        except Exception:
                            pass

                estimated_duration = (
                    completed_duration / done * total
                    if done and completed_duration
                    else 0
                )

                message = (
                    "🎙 <b>Audiobook ban raha hai</b>\n"
                    f"{progress_bar(percent)} <b>{percent}%</b> "
                    f"({done}/{total} chunks)\n"
                    f"🎧 Completed audio: {fmt_clock(completed_duration)}\n"
                )

                if estimated_duration:
                    message += (
                        "🎵 Estimated total: "
                        f"{fmt_clock(estimated_duration)}\n"
                    )

                message += (
                    f"⏱ Elapsed: {fmt_clock(elapsed)}\n"
                    f"⏳ ETA: {fmt_clock(eta)}"
                )

                try:
                    await status.edit_text(
                        message,
                        parse_mode="HTML",
                    )
                except Exception:
                    log.debug("Progress message update failed", exc_info=True)

                last_update = now

            await asyncio.sleep(1)

        for task in tasks:
            task.result()

        elapsed_tts = time.monotonic() - started_at
        durations = [await audio_duration(file) for file in files]
        total_duration = sum(durations)

        await status.edit_text(
            "✅ TTS generation complete!\n"
            f"{progress_bar(100)} 100%\n"
            f"🎧 Audio duration: {fmt_clock(total_duration)}\n"
            f"⏱ Processing time: {fmt_clock(elapsed_tts)}"
        )

        parts = []
        current_files = []
        current_duration = 0.0

        for file, duration in zip(files, durations):
            if (
                current_files
                and current_duration + duration > MAX_PART_SECONDS
            ):
                parts.append((current_files, current_duration))
                current_files = []
                current_duration = 0.0

            current_files.append(file)
            current_duration += duration

        if current_files:
            parts.append((current_files, current_duration))

        total_parts = len(parts)

        for index, (group, duration) in enumerate(parts, start=1):
            elapsed = time.monotonic() - started_at
            await status.edit_text(
                "📤 Audiobook parts send ho rahe hain\n"
                f"{progress_bar((index - 1) * 100 // total_parts)} "
                f"{index - 1}/{total_parts} parts sent\n"
                f"🎧 Total duration: {fmt_clock(total_duration)}\n"
                f"📀 Current part: {fmt_clock(duration)}\n"
                f"⏱ Elapsed: {fmt_clock(elapsed)}"
            )

            output = work / f"{name} - Part {index}.mp3"
            await concat_mp3(group, output)

            if output.stat().st_size > MAX_PART_BYTES:
                raise RuntimeError(
                    f"Part {index} Telegram file-size limit se badi hai."
                )

            caption = (
                f"{name}\n"
                f"Audiobook - Part {index}/{total_parts}\n"
                f"{fmt_time(duration)} | Speed {rate:.1f}x"
            )

            with output.open("rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    title=f"{name} - Part {index}",
                    performer=PERFORMER,
                    duration=int(duration),
                    caption=caption,
                    filename=output.name,
                    read_timeout=900,
                    write_timeout=900,
                    connect_timeout=60,
                )

            output.unlink(missing_ok=True)

        await status.edit_text(
            "✅ <b>Audiobook complete!</b>\n"
            f"{progress_bar(100)} <b>100%</b>\n"
            f"🎧 Total duration: {fmt_clock(total_duration)}\n"
            f"📀 Total parts: {total_parts}\n"
            f"⏱ Total processing time: "
            f"{fmt_clock(time.monotonic() - started_at)}",
            parse_mode="HTML",
        )

    except Exception as error:
        log.exception("Audiobook job failed")
        try:
            await status.edit_text(
                f"❌ Error: {str(error)[:3000]}"
            )
        except Exception:
            pass

    finally:
        shutil.rmtree(work, ignore_errors=True)
        context.user_data.pop("job", None)
        context.user_data.pop("selected_voice", None)


def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable set nahi hai."
        )

    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_API:
        builder = (
            builder
            .base_url(f"{LOCAL_API}/bot")
            .base_file_url(f"{LOCAL_API}/file/bot")
            .local_mode(True)
        )

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_voice, pattern=r"^v:"))
    app.add_handler(CallbackQueryHandler(on_rate, pattern=r"^r:"))

    log.info("Audiobook bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
