"""Offline Hindi audiobook Telegram bot using Piper TTS."""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
import wave
from pathlib import Path

from piper import PiperVoice
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
MODEL_PATH = Path(os.getenv("PIPER_MODEL", "/app/models/hi_IN-priyamvada-medium.onnx"))
CONFIG_PATH = Path(os.getenv("PIPER_CONFIG", "/app/models/hi_IN-priyamvada-medium.onnx.json"))
CHUNK_CHARS = max(300, int(os.getenv("CHUNK_CHARS", "1000")))
BITRATE_KBPS = 48
PERFORMER = "Offline Hindi Audiobook"
SAFETY = 0.96
MAX_PART_BYTES = 2_000_000_000 if LOCAL_API else 50_000_000
MAX_DOWNLOAD_BYTES = 2_000_000_000 if LOCAL_API else 20_000_000
MAX_PART_SECONDS = MAX_PART_BYTES * SAFETY / (BITRATE_KBPS * 1000 / 8)

if LOCAL_API:
    MAX_PART_SECONDS = min(
        MAX_PART_SECONDS,
        int(os.getenv("PART_MINUTES", "180")) * 60,
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("piper-audiobook")

VOICE_LABEL = "🇮🇳 Hindi Female — Priyamvada (Offline)"
_piper_voice = None


def get_voice():
    global _piper_voice

    if _piper_voice is None:
        if not MODEL_PATH.exists() or not CONFIG_PATH.exists():
            raise RuntimeError(
                f"Piper model files missing: {MODEL_PATH} and {CONFIG_PATH}"
            )

        log.info("Loading offline Piper voice model: %s", MODEL_PATH.name)
        _piper_voice = PiperVoice.load(
            str(MODEL_PATH),
            config_path=str(CONFIG_PATH),
        )
        log.info("Piper voice model loaded")

    return _piper_voice


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
    text = text.replace("\x00", " ")
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
            if cut < 1:
                cut = limit

            if current:
                chunks.append(current)
                current = ""

            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()

        if not sentence:
            continue

        if len(current) + len(sentence) + 1 > limit:
            if current:
                chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()

    if current:
        chunks.append(current)

    return [chunk for chunk in chunks if chunk.strip()]


async def run(*cmd):
    process = await asyncio.create_subprocess_exec(
        *map(str, cmd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    if process.returncode:
        raise RuntimeError(stderr.decode(errors="replace")[-1800:])

    return stdout.decode(errors="replace").strip()


async def audio_duration(path):
    result = await run(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        path,
    )
    return float(result)


def synthesize_wav(text, wav_path):
    """Generate speech locally using Piper."""
    voice = get_voice()

    with wave.open(str(wav_path), "wb") as wav_file:
        voice.synthesize(text, wav_file)


async def tts_chunk(text, wav_path, mp3_path, rate):
    wav_path.unlink(missing_ok=True)
    mp3_path.unlink(missing_ok=True)

    await asyncio.to_thread(synthesize_wav, text, wav_path)

    if not wav_path.exists() or wav_path.stat().st_size < 256:
        raise RuntimeError("Piper ne khaali WAV banayi")

    await run(
        "ffmpeg", "-y",
        "-i", wav_path,
        "-vn",
        "-filter:a", f"atempo={rate:.1f}",
        "-ac", "1",
        "-ar", "24000",
        "-b:a", f"{BITRATE_KBPS}k",
        mp3_path,
    )

    wav_path.unlink(missing_ok=True)

    if not mp3_path.exists() or mp3_path.stat().st_size < 256:
        raise RuntimeError("FFmpeg ne khaali MP3 banayi")


async def concat_mp3(files, output):
    listing = output.with_suffix(".concat.txt")
    lines = []

    for file in files:
        safe = file.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe}'\n")

    listing.write_text("".join(lines), encoding="utf-8")

    try:
        await run(
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", listing,
            "-c:a", "libmp3lame",
            "-b:a", f"{BITRATE_KBPS}k",
            "-ac", "1",
            "-ar", "24000",
            output,
        )
    finally:
        listing.unlink(missing_ok=True)


# --------------------------------------------------
# START MESSAGE + DEMO VOICE
# --------------------------------------------------

DEMO_TEXT = (
    "नमस्ते! यह हिंदी फीमेल आवाज़ का डेमो है। "
    "इस ऑडियोबुक बॉट में आप टेक्स्ट फ़ाइल को MP3 में बदल सकते हैं "
    "और अपनी पसंद से एक से दो गुना तक स्पीड चुन सकते हैं।"
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔊 Demo Voice सुनें",
                callback_data="demo",
            )
        ],
    ])

    await update.effective_message.reply_text(
        "👋 Namaste! Offline Hindi Female Audiobook Bot\n\n"
        "🔊 पहले Demo Voice दबाकर आवाज़ सुन सकते हो।\n"
        "फिर अपनी .txt file भेजो और 1.0x से 2.0x speed चुनो।\n\n"
        "Speech local Piper model से generate होगी; "
        "Edge TTS/Google gTTS की जरूरत नहीं है।",
        reply_markup=keyboard,
    )


async def send_demo_voice(chat_id, context, status):
    """Create and send a short Hindi female sample."""
    work = Path(tempfile.mkdtemp(prefix="piper_demo_"))
    wav_path = work / "demo.wav"
    mp3_path = work / "Hindi_Female_Demo.mp3"

    try:
        await asyncio.wait_for(
            asyncio.to_thread(synthesize_wav, DEMO_TEXT, wav_path),
            timeout=180,
        )

        await asyncio.wait_for(
            run(
                "ffmpeg", "-y",
                "-i", wav_path,
                "-vn",
                "-ac", "1",
                "-ar", "24000",
                "-b:a", f"{BITRATE_KBPS}k",
                mp3_path,
            ),
            timeout=120,
        )

        if not mp3_path.exists() or mp3_path.stat().st_size < 256:
            raise RuntimeError("Demo MP3 खाली बनी")

        duration = await audio_duration(mp3_path)

        with mp3_path.open("rb") as audio:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title="Hindi Female Voice Demo",
                performer=PERFORMER,
                duration=int(duration),
                caption=(
                    "🔊 Hindi Female — Priyamvada (Offline)\n"
                    f"🎧 Duration: {fmt_clock(duration)}\n"
                    "⚡ Demo speed: 1.0x"
                ),
                filename="Hindi_Female_Demo.mp3",
                read_timeout=180,
                write_timeout=180,
                connect_timeout=60,
            )

        await status.edit_text(
            "✅ Demo voice भेज दी है। अब अपनी .txt file भेजो।"
        )

    except Exception as exc:
        log.exception("Demo voice generation failed")
        await status.edit_text(
            f"❌ Demo voice error: {str(exc)[:1000]}"
        )

    finally:
        shutil.rmtree(work, ignore_errors=True)


async def demo_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Demo voice बना रहा हूँ…")

    status = await query.message.reply_text(
        "🔊 Hindi female demo voice generate हो रही है…"
    )

    await send_demo_voice(
        query.message.chat_id,
        context,
        status,
    )


async def demo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = await update.effective_message.reply_text(
        "🔊 Demo voice generate हो रही है…"
    )

    await send_demo_voice(
        update.effective_chat.id,
        context,
        status,
    )


# --------------------------------------------------
# TXT FILE HANDLING
# --------------------------------------------------

async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.effective_message.document

    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await update.effective_message.reply_text(
            "❌ Sirf .txt file bhejo."
        )
        return

    if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
        await update.effective_message.reply_text(
            f"❌ File limit {MAX_DOWNLOAD_BYTES // 1_000_000} MB hai."
        )
        return

    if context.user_data.get("job"):
        await update.effective_message.reply_text(
            "⚠️ Pehli file ka selection pending hai. "
            "Pehle uska process complete karo."
        )
        return

    work = Path(tempfile.mkdtemp(prefix="piper_tts_"))

    try:
        telegram_file = await context.bot.get_file(doc.file_id)
        input_path = work / "input.txt"
        await telegram_file.download_to_drive(input_path)

        text = input_path.read_text(
            encoding="utf-8-sig",
            errors="replace",
        )

        if not text.strip():
            shutil.rmtree(work, ignore_errors=True)
            await update.effective_message.reply_text(
                "❌ TXT file khaali hai."
            )
            return

        context.user_data["job"] = {
            "dir": work,
            "path": input_path,
            "name": Path(doc.file_name).stem,
        }

        rates = [
            [1.0, 1.1, 1.2, 1.3],
            [1.4, 1.5, 1.6, 1.7],
            [1.8, 1.9, 2.0],
        ]

        keyboard = [
            [
                InlineKeyboardButton(
                    f"⚡ {rate:.1f}x",
                    callback_data=f"r:{rate:.1f}",
                )
                for rate in row
            ]
            for row in rates
        ]

        await update.effective_message.reply_text(
            f"📘 {doc.file_name} receive ho gayi.\n"
            f"🎙 Voice: {VOICE_LABEL}\n\n"
            "Playback speed choose karo:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as exc:
        shutil.rmtree(work, ignore_errors=True)
        log.exception("TXT download failed")
        await update.effective_message.reply_text(
            f"❌ File read error: {str(exc)[:700]}"
        )


async def on_rate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    job = context.user_data.get("job")

    if not job:
        await query.edit_message_text(
            "File selection expire ho gayi. TXT dobara bhejo."
        )
        return

    try:
        rate = float(query.data.split(":", 1)[1])

        if not 1.0 <= rate <= 2.0:
            raise ValueError("Invalid rate")

    except Exception:
        await query.edit_message_text("Invalid speed selection.")
        return

    context.user_data["job"] = None

    status = await query.edit_message_text(
        "⏳ Offline Piper TTS start ho raha hai…\n"
        f"Speed: {rate:.1f}x"
    )

    context.application.create_task(
        process(
            status,
            context,
            job,
            query.message.chat_id,
            rate,
        )
    )


# --------------------------------------------------
# AUDIOBOOK CONVERSION
# --------------------------------------------------

async def process(status, context, job, chat_id, rate):
    work = job["dir"]
    started = time.monotonic()

    try:
        await asyncio.to_thread(get_voice)

        text = job["path"].read_text(
            encoding="utf-8-sig",
            errors="replace",
        )

        chunks = split_text(text)

        if not chunks:
            raise RuntimeError("TXT mein readable text nahi mila")

        total = len(chunks)
        mp3_files = [
            work / f"chunk_{i:05d}.mp3"
            for i in range(total)
        ]

        await status.edit_text(
            "🎙 <b>Offline audiobook ban raha hai</b>\n"
            f"{VOICE_LABEL}\n"
            f"{progress_bar(0)} <b>0%</b> (0/{total})\n"
            f"⚡ Speed: {rate:.1f}x\n"
            "⏱ Elapsed: 0:00:00 | ETA: calculating...",
            parse_mode="HTML",
        )

        durations = [0.0] * total

        for i, chunk in enumerate(chunks):
            wav_path = work / f"chunk_{i:05d}.wav"

            await tts_chunk(
                chunk,
                wav_path,
                mp3_files[i],
                rate,
            )

            durations[i] = await audio_duration(mp3_files[i])

            done = i + 1
            elapsed = time.monotonic() - started
            percent = int(done * 100 / total)
            completed_audio = sum(durations[:done])
            estimated_total = (
                completed_audio / done * total if done else 0
            )
            eta = elapsed / done * (total - done) if done else 0

            try:
                await status.edit_text(
                    "🎙 <b>Offline audiobook ban raha hai</b>\n"
                    f"{progress_bar(percent)} <b>{percent}%</b> "
                    f"({done}/{total} chunks)\n"
                    f"🎧 Completed audio: {fmt_clock(completed_audio)}\n"
                    f"🎵 Estimated total: {fmt_clock(estimated_total)}\n"
                    f"⚡ Speed: {rate:.1f}x\n"
                    f"⏱ Elapsed: {fmt_clock(elapsed)} | "
                    f"ETA: {fmt_clock(eta)}",
                    parse_mode="HTML",
                )
            except Exception:
                log.warning(
                    "Progress update failed",
                    exc_info=True,
                )

        total_duration = sum(durations)

        parts = []
        current = []
        current_duration = 0.0

        for file, duration in zip(mp3_files, durations):
            if current and current_duration + duration > MAX_PART_SECONDS:
                parts.append((current, current_duration))
                current = []
                current_duration = 0.0

            current.append(file)
            current_duration += duration

        if current:
            parts.append((current, current_duration))

        for index, (group, duration) in enumerate(parts, 1):
            await status.edit_text(
                "📤 <b>MP3 parts bhej raha hoon</b>\n"
                f"{progress_bar((index - 1) * 100 // len(parts))} "
                f"{index - 1}/{len(parts)} parts\n"
                f"🎧 Total duration: {fmt_clock(total_duration)}\n"
                f"📀 Current part: {fmt_clock(duration)}",
                parse_mode="HTML",
            )

            output = work / f"{job['name']} - Part {index}.mp3"
            await concat_mp3(group, output)

            if output.stat().st_size > MAX_PART_BYTES:
                raise RuntimeError(
                    f"Part {index} Telegram size limit se bada hai"
                )

            with output.open("rb") as audio:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio,
                    title=f"{job['name']} - Part {index}",
                    performer=PERFORMER,
                    duration=int(duration),
                    caption=(
                        f"{job['name']}\n"
                        f"Part {index}/{len(parts)} • "
                        f"{fmt_time(duration)} • {rate:.1f}x"
                    ),
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
            f"📀 Parts: {len(parts)}\n"
            f"⏱ Processing time: "
            f"{fmt_clock(time.monotonic() - started)}",
            parse_mode="HTML",
        )

    except Exception as exc:
        log.exception("Audiobook job failed")

        try:
            await status.edit_text(
                f"❌ Error: {str(exc)[:2500]}"
            )
        except Exception:
            pass

    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_TOKEN_HERE":
        raise RuntimeError(
            "BOT_TOKEN GitHub Actions secret/environment mein set nahi hai"
        )

    if LOCAL_API:
        builder = (
            Application.builder()
            .token(BOT_TOKEN)
            .base_url(f"{LOCAL_API}/bot")
            .base_file_url(f"{LOCAL_API}/file/bot")
            .local_mode(True)
        )
    else:
        builder = Application.builder().token(BOT_TOKEN)

    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("demo", demo_command))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(
        CallbackQueryHandler(demo_voice, pattern=r"^demo$")
    )
    app.add_handler(
        CallbackQueryHandler(on_rate, pattern=r"^r:")
    )

    log.info("Bot starting; offline Piper Hindi female voice selected")
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
