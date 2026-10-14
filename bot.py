import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import soundfile as sf
import torch
from parler_tts import ParlerTTSForConditionalGeneration
from transformers import AutoTokenizer
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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
HF_TOKEN = os.getenv("HF_TOKEN", "")
MODEL_ID = os.getenv("TTS_MODEL_ID", "ai4bharat/indic-parler-tts")
CHUNK_CHARS = max(180, min(500, int(os.getenv("CHUNK_CHARS", "350"))))
BITRATE_KBPS = 48
PERFORMER = "Indic Parler Hindi Audiobook"
SAMPLE_RATE = 24000

MAX_PART_BYTES = 2_000_000_000 if LOCAL_API else 50_000_000
MAX_DOWNLOAD_BYTES = 2_000_000_000 if LOCAL_API else 20_000_000
MAX_PART_SECONDS = MAX_PART_BYTES * 0.96 / (BITRATE_KBPS * 1000 / 8)

if LOCAL_API:
    MAX_PART_SECONDS = min(
        MAX_PART_SECONDS,
        int(os.getenv("PART_MINUTES", "180")) * 60,
    )

VOICE_DESCRIPTION = (
    "Divya's voice is a natural, warm Hindi female voice, expressive and clear, "
    "with a moderate speaking pace, natural pauses and a friendly storytelling tone. "
    "The recording is very clear, close-mic, and has no background noise."
)

VOICE_LABEL = "🇮🇳 Hindi Female — Divya (Indic Parler-TTS)"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("indic-parler-audiobook")

torch.set_num_threads(
    max(1, min(4, int(os.getenv("TORCH_NUM_THREADS", "2"))))
)

_model = None
_tokenizer = None
_description_tokenizer = None


def get_model():
    global _model, _tokenizer, _description_tokenizer

    if _model is None:
        if not HF_TOKEN:
            raise RuntimeError(
                "HF_TOKEN missing. Accept the model conditions on Hugging Face, "
                "create a read token, then add it as GitHub Actions secret HF_TOKEN."
            )

        log.info("Loading %s on CPU. First load may take several minutes.", MODEL_ID)

        _model = ParlerTTSForConditionalGeneration.from_pretrained(
            MODEL_ID,
            token=HF_TOKEN,
        ).to("cpu")

        _tokenizer = AutoTokenizer.from_pretrained(
            MODEL_ID,
            token=HF_TOKEN,
        )

        text_encoder_name = _model.config.text_encoder._name_or_path

        _description_tokenizer = AutoTokenizer.from_pretrained(
            text_encoder_name,
            token=HF_TOKEN,
        )

        _model.eval()
        log.info("Indic Parler-TTS model loaded.")

    return _model, _tokenizer, _description_tokenizer


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
    proc = await asyncio.create_subprocess_exec(
        *map(str, cmd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await proc.communicate()

    if proc.returncode:
        raise RuntimeError(
            stderr.decode(errors="replace")[-1800:]
        )

    return stdout.decode(errors="replace").strip()


async def audio_duration(path):
    out = await run(
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1",
        path,
    )

    return float(out)


def synthesize_wav(text, wav_path):
    model, tokenizer, description_tokenizer = get_model()

    description_inputs = description_tokenizer(
        VOICE_DESCRIPTION,
        return_tensors="pt",
    )

    prompt_inputs = tokenizer(
        text,
        return_tensors="pt",
    )

    with torch.inference_mode():
        generated = model.generate(
            input_ids=description_inputs.input_ids,
            attention_mask=description_inputs.attention_mask,
            prompt_input_ids=prompt_inputs.input_ids,
            prompt_attention_mask=prompt_inputs.attention_mask,
            max_new_tokens=1536,
        )

    audio = generated.detach().cpu().numpy().squeeze()

    if audio.size == 0:
        raise RuntimeError("Indic Parler-TTS returned empty audio")

    sf.write(
        str(wav_path),
        audio,
        model.config.sampling_rate,
    )


async def tts_chunk(text, wav_path, mp3_path, rate):
    wav_path.unlink(missing_ok=True)
    mp3_path.unlink(missing_ok=True)

    await asyncio.to_thread(
        synthesize_wav,
        text,
        wav_path,
    )

    if not wav_path.exists() or wav_path.stat().st_size < 256:
        raise RuntimeError("TTS generated an empty WAV")

    await run(
        "ffmpeg",
        "-y",
        "-i", wav_path,
        "-vn",
        "-filter:a", f"atempo={rate:.1f}",
        "-ac", "1",
        "-ar", str(SAMPLE_RATE),
        "-b:a", f"{BITRATE_KBPS}k",
        mp3_path,
    )

    wav_path.unlink(missing_ok=True)

    if not mp3_path.exists() or mp3_path.stat().st_size < 256:
        raise RuntimeError("FFmpeg generated an empty MP3")


async def concat_mp3(files, output):
    listing = output.with_suffix(".concat.txt")
    lines = []

    for file in files:
        safe = file.resolve().as_posix().replace("'", "'\\''")
        lines.append(f"file '{safe}'\n")

    listing.write_text(
        "".join(lines),
        encoding="utf-8",
    )

    try:
        await run(
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", listing,
            "-c:a", "libmp3lame",
            "-b:a", f"{BITRATE_KBPS}k",
            "-ac", "1",
            "-ar", str(SAMPLE_RATE),
            output,
        )
    finally:
        listing.unlink(missing_ok=True)


async def start(update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔊 Demo Voice सुनें",
                callback_data="demo",
            )
        ]
    ])

    await update.effective_message.reply_text(
        "👋 नमस्ते! Hindi Female Audiobook Bot\n\n"
        "🔊 पहले Demo Voice सुनें दबाकर voice sample बनाएं।\n"
        "फिर अपनी .txt file भेजें और 1.0x–2.0x speed चुनें।\n\n"
        "पहली बार model Hugging Face से download होगा।",
        reply_markup=keyboard,
    )


DEMO_TEXT = (
    "नमस्ते! यह हिंदी महिला आवाज़ का डेमो है। "
    "इस ऑडियोबुक बॉट में आप अपनी टेक्स्ट फ़ाइल को MP3 में बदल सकते हैं। "
    "आप अपनी पसंद के अनुसार एक से दो गुना तक स्पीड चुन सकते हैं। "
    "आइए, कहानी सुनना शुरू करते हैं।"
)


async def make_demo(chat_id, context, status):
    work = Path(tempfile.mkdtemp(prefix="parler_demo_"))

    wav_path = work / "demo.wav"
    mp3_path = work / "Hindi_Female_Demo.mp3"

    try:
        await status.edit_text(
            "🔊 Demo voice तैयार हो रही है। "
            "पहली बार model download होने में समय लग सकता है..."
        )

        await asyncio.wait_for(
            asyncio.to_thread(
                synthesize_wav,
                DEMO_TEXT,
                wav_path,
            ),
            timeout=1800,
        )

        await run(
            "ffmpeg",
            "-y",
            "-i", wav_path,
            "-vn",
            "-ac", "1",
            "-ar", str(SAMPLE_RATE),
            "-b:a", f"{BITRATE_KBPS}k",
            mp3_path,
        )

        duration = await audio_duration(mp3_path)

        with mp3_path.open("rb") as audio:
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=audio,
                title="Hindi Female Divya Demo",
                performer=PERFORMER,
                duration=int(duration),
                caption=(
                    "🔊 Hindi Female — Divya\n"
                    f"🎧 {fmt_clock(duration)}\n"
                    "⚡ Demo: 1.0x"
                ),
                filename="Hindi_Female_Demo.mp3",
                read_timeout=900,
                write_timeout=900,
                connect_timeout=60,
            )

        await status.edit_text(
            "✅ Demo audio भेज दी है। अब अपनी .txt file भेजें।"
        )

    except Exception as exc:
        log.exception("Demo generation failed")

        await status.edit_text(
            f"❌ Demo error: {str(exc)[:1500]}"
        )

    finally:
        shutil.rmtree(work, ignore_errors=True)


async def demo_callback(update, context):
    query = update.callback_query
    await query.answer("Demo तैयार कर रहा हूँ…")

    status = await query.message.reply_text(
        "🔊 Hindi female demo तैयार हो रहा है..."
    )

    await make_demo(
        query.message.chat_id,
        context,
        status,
    )


async def demo_command(update, context):
    status = await update.effective_message.reply_text(
        "🔊 Demo voice तैयार हो रही है..."
    )

    await make_demo(
        update.effective_chat.id,
        context,
        status,
    )


async def on_document(update, context):
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
            "⚠️ Pehli file ka selection pending hai."
        )
        return

    work = Path(tempfile.mkdtemp(prefix="parler_tts_"))

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


async def on_rate(update, context):
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
        await query.edit_message_text(
            "Invalid speed selection."
        )
        return

    context.user_data["job"] = None

    status = await query.edit_message_text(
        "⏳ Offline Parler-TTS start ho raha hai...\n"
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


async def process(status, context, job, chat_id, rate):
    work = job["dir"]
    started = time.monotonic()

    try:
        await asyncio.to_thread(get_model)

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

        durations = [0.0] * total

        await status.edit_text(
            "🎙 <b>Audiobook बन रहा है</b>\n"
            f"{progress_bar(0)} <b>0%</b> (0/{total})\n"
            f"⚡ Speed: {rate:.1f}x",
            parse_mode="HTML",
        )

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
            completed_duration = sum(durations[:done])

            estimated_total = (
                completed_duration / done * total
                if done else 0
            )

            eta = (
                elapsed / done * (total - done)
                if done else 0
            )

            try:
                await status.edit_text(
                    "🎙 <b>Audiobook बन रहा है</b>\n"
                    f"{progress_bar(percent)} <b>{percent}%</b> "
                    f"({done}/{total})\n"
                    f"🎧 Completed: {fmt_clock(completed_duration)}\n"
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
            if (
                current
                and current_duration + duration > MAX_PART_SECONDS
            ):
                parts.append((current, current_duration))
                current = []
                current_duration = 0.0

            current.append(file)
            current_duration += duration

        if current:
            parts.append((current, current_duration))

        for index, (group, duration) in enumerate(parts, 1):
            await status.edit_text(
                "📤 <b>Parts भेज रहे हैं</b>\n"
                f"{progress_bar((index - 1) * 100 // len(parts))} "
                f"{index - 1}/{len(parts)}\n"
                f"🎧 Total: {fmt_clock(total_duration)}",
                parse_mode="HTML",
            )

            output = work / f"{job['name']} - Part {index}.mp3"

            await concat_mp3(group, output)

            if output.stat().st_size > MAX_PART_BYTES:
                raise RuntimeError(
                    f"Part {index} Telegram size limit से बड़ी है"
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
            f"🎧 Total: {fmt_clock(total_duration)}\n"
            f"📀 Parts: {len(parts)}\n"
            f"⏱ Processing: "
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
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing"
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
    app.add_handler(CommandHandler("demo", demo_command))

    app.add_handler(
        MessageHandler(filters.Document.ALL, on_document)
    )

    app.add_handler(
        CallbackQueryHandler(
            demo_callback,
            pattern=r"^demo$",
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            on_rate,
            pattern=r"^r:",
        )
    )

    log.info(
        "Starting Telegram bot with Indic Parler-TTS (CPU)"
    )

    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
