import os
import re
import asyncio
import tempfile
import subprocess
import logging
import time
from pathlib import Path

import numpy as np
import soundfile as sf

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ================= CONFIGURATION =================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

log = logging.getLogger("levithan-voice-ai")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
MODEL_ID = os.getenv(
    "TTS_MODEL_ID",
    "ai4bharat/indic-parler-tts",
)

MAX_PART_MB = int(os.getenv("MAX_PART_MB", "48"))
MAX_UPLOAD = MAX_PART_MB * 1024 * 1024

STATE = {}
MODEL = None
TOKENIZER = None
DESC_TOKENIZER = None
MODEL_LOCK = asyncio.Lock()

# ================= TEXT PROCESSING =================


def split_text(text, limit=600):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [
        re.sub(r"\s+", " ", p).strip()
        for p in text.split("\n")
        if p.strip()
    ]

    chunks = []
    current = ""

    for paragraph in paragraphs:
        sentences = re.split(
            r"(?<=[.!?।])\s+",
            paragraph,
        )

        for sentence in sentences:
            while len(sentence) > limit:
                if current:
                    chunks.append(current)
                    current = ""

                chunks.append(sentence[:limit])
                sentence = sentence[limit:]

            if not sentence:
                continue

            if len(current) + len(sentence) + 1 <= limit:
                current = (current + " " + sentence).strip()
            else:
                if current:
                    chunks.append(current)
                current = sentence

    if current:
        chunks.append(current)

    return chunks


def get_demo_text(text):
    chunks = split_text(text, 250)

    if not chunks:
        return ""

    return chunks[0][:350]


# ================= MODEL LOADING =================


def load_model():
    global MODEL, TOKENIZER, DESC_TOKENIZER

    if MODEL is not None:
        return

    import torch
    from parler_tts import ParlerTTSForConditionalGeneration
    from transformers import AutoTokenizer

    device = "cuda" if torch.cuda.is_available() else "cpu"

    log.info("Loading model: %s", MODEL_ID)
    log.info("Device: %s", device)

    MODEL = ParlerTTSForConditionalGeneration.from_pretrained(
        MODEL_ID,
        token=os.getenv("HF_TOKEN") or None,
    ).to(device)

    TOKENIZER = AutoTokenizer.from_pretrained(
        MODEL_ID,
        token=os.getenv("HF_TOKEN") or None,
    )

    DESC_TOKENIZER = AutoTokenizer.from_pretrained(
        MODEL.config.text_encoder._name_or_path,
        token=os.getenv("HF_TOKEN") or None,
    )

    MODEL.eval()

    log.info("TTS model loaded successfully")


def voice_description(voice):
    if voice == "female":
        return (
            "Divya speaks Hindi with a clear, natural female voice. "
            "She narrates a Hindi audiobook with expressive delivery, "
            "clear pronunciation, and a steady pace."
        )

    return (
        "Rohit speaks Hindi with a clear, natural male voice. "
        "He narrates a Hindi audiobook with expressive delivery, "
        "clear pronunciation, and a steady pace."
    )


def synthesize_sync(text, voice, wav_path):
    import torch

    load_model()

    device = next(MODEL.parameters()).device

    description = voice_description(voice)

    description_inputs = DESC_TOKENIZER(
        description,
        return_tensors="pt",
    )

    text_inputs = TOKENIZER(
        text,
        return_tensors="pt",
    )

    description_inputs = {
        key: value.to(device)
        for key, value in description_inputs.items()
    }

    text_inputs = {
        key: value.to(device)
        for key, value in text_inputs.items()
    }

    with torch.inference_mode():
        audio = MODEL.generate(
            input_ids=description_inputs["input_ids"],
            attention_mask=description_inputs.get("attention_mask"),
            prompt_input_ids=text_inputs["input_ids"],
            prompt_attention_mask=text_inputs.get("attention_mask"),
        )

    audio_array = audio.detach().float().cpu().numpy().squeeze()

    if audio_array.size == 0:
        raise RuntimeError("TTS model returned empty audio")

    if not np.isfinite(audio_array).all():
        raise RuntimeError("TTS model returned invalid audio samples")

    sample_rate = int(MODEL.config.sampling_rate)

    sf.write(
        str(wav_path),
        audio_array,
        sample_rate,
    )


# ================= AUDIO CONVERSION =================


def convert_to_mp3(wav_path, mp3_path, speed):
    speed = max(1.0, min(2.0, float(speed)))

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(wav_path),
    ]

    if speed != 1.0:
        command.extend([
            "-filter:a",
            f"atempo={speed:.2f}",
        ])

    command.extend([
        "-ac",
        "1",
        "-b:a",
        "48k",
        "-codec:a",
        "libmp3lame",
        str(mp3_path),
    ])

    subprocess.run(
        command,
        check=True,
        timeout=1800,
    )


# ================= TELEGRAM COMMANDS =================


async def reply(update, text):
    if update.effective_message:
        await update.effective_message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply(
        update,
        "🎙️ Levithan Voice AI\n\n"
        "अपनी TXT novel भेजो।\n"
        "पहले demo audio बनेगा।\n"
        "उसके बाद voice और speed चुनकर audiobook बना सकते हो।\n\n"
        "Commands:\n"
        "/start - Bot शुरू करें\n"
        "/demo - Demo बनाएँ\n"
        "/cancel - काम रद्द करें",
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    state = STATE.get(chat_id)

    if state:
        state["cancelled"] = True

    STATE.pop(chat_id, None)

    await reply(
        update,
        "Cancellation request भेज दी गई। नई TXT भेज सकते हो।",
    )


async def demo_command(update, context):
    chat_id = update.effective_chat.id

    if chat_id not in STATE:
        await reply(update, "पहले TXT file भेजो।")
        return

    await make_demo(context, chat_id)


# ================= RECEIVE TXT =================


async def receive_txt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    document = update.message.document

    if not document:
        return

    filename = document.file_name or ""

    if not filename.lower().endswith(".txt"):
        await reply(update, "कृपया केवल .txt file भेजें।")
        return

    if document.file_size and document.file_size > 25 * 1024 * 1024:
        await reply(update, "TXT file 25 MB से छोटी करके भेजें।")
        return

    status = await update.message.reply_text(
        "📥 TXT डाउनलोड हो रही है..."
    )

    path = None

    try:
        telegram_file = await context.bot.get_file(document.file_id)

        with tempfile.NamedTemporaryFile(
            suffix=".txt",
            delete=False,
        ) as temp:
            path = Path(temp.name)

        await telegram_file.download_to_drive(
            custom_path=str(path)
        )

        raw = path.read_bytes()

        text = raw.decode(
            "utf-8-sig",
            errors="replace",
        ).strip()

        if len(text) < 20:
            await status.edit_text(
                "TXT file खाली या बहुत छोटी है।"
            )
            return

        STATE[update.effective_chat.id] = {
            "text": text,
            "filename": Path(filename).stem[:80],
            "created": time.time(),
            "demo_done": False,
            "voice": None,
            "speed": None,
            "running": False,
            "cancelled": False,
        }

        await status.edit_text(
            f"✅ TXT loaded\n"
            f"Characters: {len(text):,}\n\n"
            f"अब demo बनाओ।",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🎧 Generate Demo",
                        callback_data="demo",
                    )
                ]
            ]),
        )

    except Exception as error:
        log.exception("TXT processing failed")

        await status.edit_text(
            f"❌ TXT error: {type(error).__name__}: "
            f"{str(error)[:250]}"
        )

    finally:
        if path:
            path.unlink(missing_ok=True)


# ================= DEMO GENERATION =================


async def make_demo(context, chat_id):
    state = STATE.get(chat_id)

    if not state:
        await context.bot.send_message(
            chat_id,
            "पहले TXT file भेजो।",
        )
        return

    if state.get("demo_running"):
        await context.bot.send_message(
            chat_id,
            "Demo पहले से बन रहा है।",
        )
        return

    state["demo_running"] = True

    status = await context.bot.send_message(
        chat_id,
        "⏳ Demo audio बन रही है। पहली बार model load होने में समय लग सकता है।",
    )

    try:
        demo_text = get_demo_text(state["text"])

        if not demo_text:
            raise RuntimeError("Demo text नहीं मिला")

        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)

            wav = directory / "demo.wav"
            mp3 = directory / "demo.mp3"

            async with MODEL_LOCK:
                await asyncio.to_thread(
                    synthesize_sync,
                    demo_text,
                    "male",
                    wav,
                )

            await asyncio.to_thread(
                convert_to_mp3,
                wav,
                mp3,
                1.0,
            )

            if mp3.stat().st_size > MAX_UPLOAD:
                raise RuntimeError(
                    "Demo audio upload size limit से बड़ी है"
                )

            with mp3.open("rb") as audio:
                await context.bot.send_audio(
                    chat_id,
                    audio=audio,
                    filename="Levithan_demo.mp3",
                    title="Levithan Voice AI Demo",
                )

        state["demo_done"] = True
        state["voice"] = None
        state["speed"] = None

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "👨 Male",
                    callback_data="voice:male",
                ),
                InlineKeyboardButton(
                    "👩 Female",
                    callback_data="voice:female",
                ),
            ],
            [
                InlineKeyboardButton(
                    "1.0×",
                    callback_data="speed:1.0",
                ),
                InlineKeyboardButton(
                    "1.25×",
                    callback_data="speed:1.25",
                ),
                InlineKeyboardButton(
                    "1.5×",
                    callback_data="speed:1.5",
                ),
                InlineKeyboardButton(
                    "2.0×",
                    callback_data="speed:2.0",
                ),
            ],
        ])

        await context.bot.send_message(
            chat_id,
            "पहले voice चुनो, फिर speed चुनो। "
            "दोनों चुनने के बाद audiobook शुरू होगी।",
            reply_markup=keyboard,
        )

    except Exception as error:
        log.exception("Demo generation failed")

        await context.bot.send_message(
            chat_id,
            f"❌ Demo failed\n"
            f"{type(error).__name__}: {str(error)[:350]}\n\n"
            f"GitHub Actions logs जाँचें।",
        )

    finally:
        state = STATE.get(chat_id)

        if state:
            state["demo_running"] = False

        try:
            await status.delete()
        except Exception:
            pass


# ================= BUTTON HANDLER =================


async def callbacks(update, context):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    state = STATE.get(chat_id)

    if query.data == "demo":
        await make_demo(context, chat_id)
        return

    if not state:
        await query.message.reply_text(
            "TXT file दोबारा भेजें।"
        )
        return

    if query.data.startswith("voice:"):
        state["voice"] = query.data.split(":", 1)[1]

        await query.message.reply_text(
            f"Voice selected: {state['voice']}"
        )

    elif query.data.startswith("speed:"):
        state["speed"] = float(
            query.data.split(":", 1)[1]
        )

        await query.message.reply_text(
            f"Speed selected: {state['speed']}×"
        )

    if (
        state.get("demo_done")
        and state.get("voice")
        and state.get("speed")
        and not state.get("running")
    ):
        state["running"] = True
        state["cancelled"] = False

        asyncio.create_task(
            generate_book(context, chat_id)
        )


# ================= FULL AUDIOBOOK =================


async def generate_book(context, chat_id):
    state = STATE.get(chat_id)

    if not state:
        return

    text = state["text"]
    voice = state["voice"]
    speed = state["speed"]
    filename = state["filename"]

    chunks = split_text(text, 600)

    started = time.time()
    sent_parts = 0

    status = await context.bot.send_message(
        chat_id,
        f"🚀 Audiobook शुरू!\n"
        f"Text chunks: {len(chunks)}\n"
        f"Voice: {voice}\n"
        f"Speed: {speed}×\n"
        f"Format: Mono MP3, 48 kbps",
    )

    try:
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)

            for index, chunk in enumerate(chunks, start=1):
                state = STATE.get(chat_id)

                if not state or state.get("cancelled"):
                    await context.bot.send_message(
                        chat_id,
                        "⏹️ Audiobook generation रद्द कर दी गई।",
                    )
                    return

                wav = directory / f"part_{index:05d}.wav"
                mp3 = directory / f"part_{index:05d}.mp3"

                async with MODEL_LOCK:
                    await asyncio.to_thread(
                        synthesize_sync,
                        chunk,
                        voice,
                        wav,
                    )

                await asyncio.to_thread(
                    convert_to_mp3,
                    wav,
                    mp3,
                    speed,
                )

                wav.unlink(missing_ok=True)

                size = mp3.stat().st_size

                if size > MAX_UPLOAD:
                    raise RuntimeError(
                        f"Part {index} size limit से बड़ी है।"
                    )

                with mp3.open("rb") as audio:
                    await context.bot.send_audio(
                        chat_id,
                        audio=audio,
                        filename=f"{filename}_{index:05d}.mp3",
                        title=f"{filename} - Part {index}",
                        caption=f"🎧 Part {index}/{len(chunks)}",
                    )

                sent_parts += 1
                mp3.unlink(missing_ok=True)

                elapsed = int(time.time() - started)

                if index % 5 == 0 or index == len(chunks):
                    await status.edit_text(
                        f"🎙️ Audiobook generation\n"
                        f"Progress: {index}/{len(chunks)} "
                        f"({index * 100 // len(chunks)}%)\n"
                        f"Parts sent: {sent_parts}\n"
                        f"Elapsed: {elapsed // 60}m "
                        f"{elapsed % 60}s"
                    )

        elapsed = int(time.time() - started)

        await context.bot.send_message(
            chat_id,
            f"✅ Audiobook complete!\n"
            f"Parts sent: {sent_parts}\n"
            f"Time: {elapsed // 60} minutes",
        )

    except Exception as error:
        log.exception("Audiobook generation failed")

        await context.bot.send_message(
            chat_id,
            f"❌ Generation failed\n"
            f"Parts sent: {sent_parts}\n"
            f"{type(error).__name__}: {str(error)[:350]}",
        )

    finally:
        if chat_id in STATE:
            STATE[chat_id]["running"] = False


# ================= STARTUP =================


def main():
    if not TOKEN:
        raise RuntimeError(
            "BOT_TOKEN environment variable missing"
        )

    if subprocess.run(
        ["ffmpeg", "-version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0:
        raise RuntimeError("ffmpeg installed नहीं है")

    app = (
        Application.builder()
        .token(TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("cancel", cancel)
    )

    app.add_handler(
        CommandHandler("demo", demo_command)
    )

    app.add_handler(
        CallbackQueryHandler(callbacks)
    )

    app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            receive_txt,
        )
    )

    log.info("Levithan Voice AI starting")

    app.run_polling(
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
