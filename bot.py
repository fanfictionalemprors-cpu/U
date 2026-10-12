
import asyncio
import logging
import random
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import edge_tts
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("ttsbot")


def fmt_time(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}h {(sec % 3600) // 60}m {sec % 60}s"


def fmt_clock(sec):
    sec = max(0, int(sec))
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def progress_bar(pct, width=12):
    pct = max(0, min(100, int(pct)))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def split_text(text, limit=CHUNK_CHARS):
    text = re.sub(r"[ \t]+", " ", text)
    sentences = re.split(r"(?<=[।.!?\n])\s*", text)
    chunks = []
    cur = ""

    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue

        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit

            if cur:
                chunks.append(cur)
                cur = ""

            chunks.append(sentence[:cut])
            sentence = sentence[cut:].strip()

        if len(cur) + len(sentence) + 1 > limit:
            if cur:
                chunks.append(cur)
            cur = sentence
        else:
            cur = f"{cur} {sentence}".strip()

    if cur:
        chunks.append(cur)

    return [chunk for chunk in chunks if chunk.strip()]


async def run(*cmd):
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await process.communicate()

    if process.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-1000:])

    return out.decode().strip()


async def audio_duration(path):
    result = await run(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        str(path),
    )
    return float(result)


async def tts_chunk(text, voice, output, semaphore):
    async with semaphore:
        last_error = None

        for attempt in range(1, TTS_RETRIES + 1):
            output.unlink(missing_ok=True)

            try:
                communicate = edge_tts.Communicate(text, voice)

                await asyncio.wait_for(
                    communicate.save(str(output)),
                    timeout=TTS_TIMEOUT_SECONDS,
                )

                if output.exists() and output.stat().st_size > 256:
                    return

                raise RuntimeError("Empty or incomplete TTS audio")

            except asyncio.CancelledError:
                output.unlink(missing_ok=True)
                raise

            except Exception as error:
                last_error = error
                output.unlink(missing_ok=True)

                log.warning(
                    "TTS attempt %s/%s failed; voice=%s; chars=%s; error=%r",
                    attempt,
                    TTS_RETRIES,
                    voice,
                    len(text),
                    error,
                )

                if attempt < TTS_RETRIES:
                    delay = min(45, 3 * (2 ** (attempt - 1)))
                    delay += random.uniform(0, 1.5)
                    await asyncio.sleep(delay)

        raise RuntimeError(
            f"TTS failed after {TTS_RETRIES} attempts: {last_error!r}"
        )


async def concat_mp3(files, output):
    list_file = output.with_suffix(".concat.txt")

    try:
        list_file.write_text(
            "".join(
                f"file '{file.as_posix()}'\n"
                for file in files
            ),
            encoding="utf-8",
        )

        await run(
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


async def start(update, ctx):
    await update.message.reply_text(
        "👋 Namaste! Mujhe ek .txt file bhejo.\n\n"
        "1️⃣ TXT file bhejo\n"
        "2️⃣ Voice choose karo\n"
        "3️⃣ Audiobook MP3 parts milenge 🎧"
    )


async def on_document(update, ctx):
    document = update.message.document

    if not document.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ Sirf .txt file bhejo.")
        return

    if document.file_size and document.file_size > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"❌ File limit {MAX_DOWNLOAD_BYTES // 1_000_000} MB hai."
        )
        return

    work = Path(tempfile.mkdtemp(prefix="tts_"))

    try:
        path = work / "input.txt"
        telegram_file = await ctx.bot.get_file(document.file_id)
        await telegram_file.download_to_drive(path)

        ctx.user_data["job"] = {
            "dir": work,
            "path": path,
            "name": Path(document.file_name).stem,
        }

        keyboard = [
            [
                InlineKeyboardButton(
                    label,
                    callback_data=f"v:{key}",
                )
            ]
            for key, (label, _) in VOICES.items()
        ]

        await update.message.reply_text(
            f"📘 {document.file_name} mil gayi.\nVoice choose karo:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise


async def on_voice(update, ctx):
    query = update.callback_query
    await query.answer()

    job = ctx.user_data.get("job")

    if not job:
        await query.edit_message_text("File expire ho gayi. Dobara bhejo.")
        return

    ctx.user_data["job"] = None

    voice_key = query.data.split(":", 1)[1]

    if voice_key not in VOICES:
        await query.edit_message_text("Invalid voice. TXT dobara bhejo.")
        return

    voice = VOICES[voice_key][1]
    status = await query.edit_message_text("⏳ Audiobook shuru ho raha hai...")

    ctx.application.create_task(
        process(status, ctx, job, voice, query.message.chat_id)
    )


async def process(status, ctx, job, voice, chat_id):
    work = job["dir"]
    name = job["name"]
    tasks = []
    started_at = time.monotonic()

    try:
        raw = job["path"].read_bytes()

        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-16", errors="replace")

        chunks = split_text(text)
        total = len(chunks)

        if not total:
            await status.edit_text("❌ TXT file khaali hai.")
            return

        semaphore = asyncio.Semaphore(CONCURRENCY)
        files = [work / f"chunk_{i:05d}.mp3" for i in range(total)]
        done = 0

        async def worker(index):
            nonlocal done
            await tts_chunk(
                chunks[index],
                voice,
                files[index],
                semaphore,
            )
            done += 1

        tasks = [
            asyncio.create_task(worker(i))
            for i in range(total)
        ]

        await status.edit_text(
            f"🎙 Audiobook ban raha hai\n"
            f"{progress_bar(0)} 0% (0/{total} chunks)\n"
            f"🎧 MP3 duration: calculating...\n"
            f"⏱ Elapsed: 0:00:00 | ETA: calculating..."
        )

        last_pct = -1
        last_update = time.monotonic()

        while not all(task.done() for task in tasks):
            await asyncio.sleep(3)

            failed = next(
                (
                    task for task in tasks
                    if task.done()
                    and not task.cancelled()
                    and task.exception() is not None
                ),
                None,
            )

            if failed is not None:
                for task in tasks:
                    if not task.done():
                        task.cancel()

                await asyncio.gather(*tasks, return_exceptions=True)
                failed.result()

            pct = done * 100 // total
            elapsed = time.monotonic() - started_at

            should_update = (
                pct != last_pct
                or time.monotonic() - last_update >= PROGRESS_INTERVAL
            )

            if not should_update:
                continue

            last_pct = pct
            last_update = time.monotonic()

            eta = elapsed / done * (total - done) if done else None

            completed_audio = 0.0

            for file in files:
                if file.exists() and file.stat().st_size > 256:
                    try:
                        completed_audio += await audio_duration(file)
                    except Exception:
                        pass

            estimated_audio = (
                completed_audio / done * total
                if done and completed_audio else 0
            )

            message = (
                f"🎙 Audiobook ban raha hai\n"
                f"{progress_bar(pct)} {pct}% ({done}/{total} chunks)\n"
                f"🎧 MP3 duration: {fmt_clock(completed_audio)} complete\n"
            )

            if estimated_audio:
                message += (
                    f"📀 Estimated total duration: "
                    f"~{fmt_clock(estimated_audio)}\n"
                )

            message += (
                f"⏱ Elapsed: {fmt_clock(elapsed)} | "
                f"ETA: {fmt_clock(eta) if eta is not None else 'calculating...'}"
            )

            if done == 0:
                message += (
                    f"\n🔄 First chunk running; "
                    f"timeout is {TTS_TIMEOUT_SECONDS}s."
                )

            try:
                await status.edit_text(message)
            except Exception:
                log.warning("Progress update failed", exc_info=True)

        for task in tasks:
            task.result()

        elapsed_tts = time.monotonic() - started_at
        durations = [await audio_duration(file) for file in files]
        total_duration = sum(durations)

        await status.edit_text(
            f"✅ TTS generation complete!\n"
            f"{progress_bar(100)} 100% ({total}/{total} chunks)\n"
            f"🎧 MP3 duration: {fmt_clock(total_duration)}\n"
            f"⏱ TTS processing time: {fmt_clock(elapsed_tts)}"
        )

        parts = []
        current_files = []
        current_duration = 0.0

        for file, duration in zip(files, durations):
            if current_files and current_duration + duration > MAX_PART_SECONDS:
                parts.append((current_files, current_duration))
                current_files = []
                current_duration = 0.0

            current_files.append(file)
            current_duration += duration

        if current_files:
            parts.append((current_files, current_duration))

        total_parts = len(parts)

        for index, (group, duration) in enumerate(parts, 1):
            elapsed = time.monotonic() - started_at
            eta = (
                elapsed / (index - 1) * (total_parts - index + 1)
                if index > 1 else None
            )

            await status.edit_text(
                f"📤 Audiobook parts send ho rahe hain\n"
                f"{progress_bar((index - 1) * 100 // total_parts)} "
                f"{(index - 1) * 100 // total_parts}% "
                f"(Part {index - 1}/{total_parts})\n"
                f"🎧 Total MP3 duration: {fmt_clock(total_duration)}\n"
                f"📀 Current part: {fmt_clock(duration)}\n"
                f"⏱ Elapsed: {fmt_clock(elapsed)} | "
                f"ETA: {fmt_clock(eta) if eta is not None else 'calculating...'}"
            )

            output = work / f"{name} - Part {index}.mp3"
            await concat_mp3(group, output)

            if output.stat().st_size > MAX_PART_BYTES:
                raise RuntimeError(
                    f"Part {index} exceeds the upload size limit."
                )

            with open(output, "rb") as audio_file:
                await ctx.bot.send_audio(
                    chat_id,
                    audio=audio_file,
                    title=f"{name} - Part {index}",
                    performer=PERFORMER,
                    duration=int(duration),
                    caption=(
                        f"{name}\nAudiobook - Part {index}\n"
                        f"{fmt_time(duration)} | {voice}"
                    ),
                    filename=output.name,
                    read_timeout=900,
                    write_timeout=900,
                    connect_timeout=60,
                )

            output.unlink(missing_ok=True)

        await status.edit_text(
            f"✅ Audiobook complete!\n"
            f"{progress_bar(100)} 100%\n"
            f"🎧 Total MP3 duration: {fmt_clock(total_duration)}\n"
            f"📀 MP3 parts: {total_parts}\n"
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
        log.exception("Audiobook job failed")

        try:
            await status.edit_text(
                f"❌ TTS/Audiobook error:\n{str(error)[:3000]}"
            )
        except Exception:
            log.exception("Could not send error status")

    finally:
        shutil.rmtree(work, ignore_errors=True)


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
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_voice, pattern=r"^v:"))

    log.info("TTS audiobook bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
