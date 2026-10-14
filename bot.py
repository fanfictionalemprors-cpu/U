import os
import re
import sys
import math
import asyncio
import logging
import tempfile
import subprocess
import shutil
import time

from pathlib import Path
from threading import Lock

import soundfile as sf

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.error import (
    RetryAfter,
    TimedOut,
    NetworkError,
    BadRequest,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]

TTS_MODEL_DIR = os.getenv(
    "TTS_MODEL_DIR",
    "/app/indus-pocket-tts",
)

LOCAL_API_URL = os.getenv(
    "LOCAL_API_URL",
    "",
).rstrip("/")

AUDIO_BITRATE = "48k"
MAX_AUDIO_SIZE_MB = 48
MAX_AUDIO_SIZE_BYTES = MAX_AUDIO_SIZE_MB * 1024 * 1024

# Each text chunk is synthesized independently.
TEXT_CHUNK_CHARS = 450

# Keep the final MP3 under the target size with some headroom.
TARGET_PART_SIZE_BYTES = 44 * 1024 * 1024

# 15 non-empty lines for the initial demo.
DEMO_LINES = 15

# Background ambience is intentionally quiet.
BACKGROUND_VOLUME = 0.08

# User state.
USER_JOBS = {}
LATEST_FILES = {}

TTS_LOCK = Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("LevithanVoiceAI")


# ============================================================
# DEPENDENCY CHECKS
# ============================================================

def check_dependencies():
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is not installed.")

    if not shutil.which("ffprobe"):
        raise RuntimeError("FFprobe is not installed.")


# ============================================================
# TTS MODEL
# ============================================================

sys.path.insert(0, TTS_MODEL_DIR)

try:
    from speak import load, synthesize
except Exception as exc:
    log.exception("Could not import the TTS model.")
    raise RuntimeError(
        "TTS model import failed. Check TTS_MODEL_DIR and Dockerfile."
    ) from exc


MODEL = None
DEFAULT_VOICE = None


def load_tts_model():
    global MODEL, DEFAULT_VOICE

    with TTS_LOCK:
        if MODEL is None:
            log.info("Loading TTS model...")
            MODEL, DEFAULT_VOICE = load(int8=True)
            log.info("TTS model loaded.")


def synthesize_wav(text: str, output_path: str):
    """
    Uses the installed Indus Pocket TTS interface.
    The model's actual language and voice capabilities remain
    those of the installed model.
    """

    load_tts_model()

    with TTS_LOCK:
        synthesize(
            MODEL,
            DEFAULT_VOICE,
            text,
            output_path,
        )

    if not os.path.exists(output_path):
        raise RuntimeError("TTS did not create the WAV file.")

    if os.path.getsize(output_path) < 1000:
        raise RuntimeError("Generated WAV file is empty or too small.")


# ============================================================
# TEXT PROCESSING
# ============================================================

def read_text_file(path: str) -> str:
    encodings = [
        "utf-8-sig",
        "utf-8",
        "utf-16",
        "utf-16-le",
        "cp1252",
    ]

    for encoding in encodings:
        try:
            with open(path, "r", encoding=encoding) as f:
                text = f.read()

            if text.strip():
                return text

        except (UnicodeDecodeError, UnicodeError):
            continue

    raise ValueError(
        "TXT encoding could not be detected. Please save the file as UTF-8."
    )


def normalize_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = text.replace("\r\n", "\n")
    text = text.replace("\r", "\n")

    # Remove excessive blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)

    # Remove trailing spaces.
    text = "\n".join(line.strip() for line in text.splitlines())

    return text.strip()


def split_into_lines(text: str):
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]


def create_demo_text(text: str) -> str:
    lines = split_into_lines(text)

    if not lines:
        raise ValueError("The TXT file contains no readable text.")

    selected = lines[:DEMO_LINES]

    # Avoid an excessively long demo.
    demo = "\n".join(selected)

    if len(demo) > 2500:
        demo = demo[:2500]

    return demo


def split_text(text: str, max_chars: int = TEXT_CHUNK_CHARS):
    """
    Split text at sentence boundaries where possible.
    """

    paragraphs = [
        p.strip()
        for p in re.split(r"\n+", text)
        if p.strip()
    ]

    chunks = []
    current = ""

    for paragraph in paragraphs:
        sentences = re.split(r"(?<=[.!?।])\s+", paragraph)

        for sentence in sentences:
            sentence = sentence.strip()

            if not sentence:
                continue

            if len(sentence) > max_chars:
                for start in range(0, len(sentence), max_chars):
                    piece = sentence[start:start + max_chars]

                    if current:
                        chunks.append(current)
                        current = ""

                    chunks.append(piece)

                continue

            if not current:
                current = sentence

            elif len(current) + len(sentence) + 1 <= max_chars:
                current += " " + sentence

            else:
                chunks.append(current)
                current = sentence

    if current:
        chunks.append(current)

    return chunks


# ============================================================
# CINEMATIC SCENE DETECTION
# ============================================================

SCENE_KEYWORDS = {
    "rain": [
        "rain", "rainstorm", "बारिश", "वर्षा", "बरसात",
    ],
    "storm": [
        "thunder", "lightning", "storm", "thunderstorm",
        "बिजली", "गरज", "तूफान", "आकाश",
    ],
    "forest": [
        "forest", "jungle", "trees", "birds",
        "जंगल", "वन", "पेड़", "पक्षी",
    ],
    "battle": [
        "battle", "war", "sword", "attack", "fight",
        "युद्ध", "लड़ाई", "तलवार", "हमला", "प्रहार",
    ],
    "mystical": [
        "magic", "magical", "cultivation", "energy",
        "aura", "power", "spiritual", "mystic",
        "जादू", "शक्ति", "ऊर्जा", "आभा", "साधना",
    ],
    "horror": [
        "ghost", "haunted", "horror", "darkness",
        "भूत", "प्रेत", "डर", "अंधेरा", "चीख",
    ],
    "city": [
        "city", "street", "market", "traffic",
        "शहर", "बाजार", "सड़क", "भीड़",
    ],
    "sad": [
        "cry", "tears", "grief", "sadness", "heartbroken",
        "आँसू", "रोया", "दुख", "दर्द", "विरह",
    ],
}


def detect_scene(text: str) -> str:
    text_lower = text.lower()

    scores = {}

    for scene, keywords in SCENE_KEYWORDS.items():
        scores[scene] = sum(
            text_lower.count(keyword.lower())
            for keyword in keywords
        )

    if not scores:
        return "neutral"

    best_scene = max(scores, key=scores.get)

    if scores[best_scene] == 0:
        return "neutral"

    return best_scene


# ============================================================
# AUDIO PROCESSING
# ============================================================

def run_ffmpeg(args, timeout=None):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error"] + args

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )

    if result.returncode != 0:
        error = result.stderr.decode(
            "utf-8",
            errors="replace",
        )

        raise RuntimeError(
            f"FFmpeg failed: {error[-2000:]}"
        )


def get_duration(path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    if result.returncode != 0:
        return 0.0

    try:
        return float(result.stdout.decode().strip())
    except (ValueError, TypeError):
        return 0.0


def create_ambience(
    scene: str,
    duration: float,
    output_path: str,
):
    """
    Generates synthetic ambience with FFmpeg.
    These are procedural effects, not real recordings.
    """

    if duration <= 0:
        duration = 1.0

    if scene == "neutral":
        return False

    # Synthetic ambience uses pink noise.
    # It will not reproduce realistic individual sound effects.
    filters = {
        "rain": "lowpass=f=6500,highpass=f=350",
        "storm": "lowpass=f=450,highpass=f=35",
        "forest": "lowpass=f=2800,highpass=f=300",
        "battle": "lowpass=f=1200,highpass=f=60",
        "mystical": "lowpass=f=1800,highpass=f=80",
        "horror": "lowpass=f=700,highpass=f=40",
        "city": "lowpass=f=2500,highpass=f=180",
        "sad": "lowpass=f=1200,highpass=f=180",
    }

    filter_chain = filters.get(scene)

    if not filter_chain:
        return False

    try:
        run_ffmpeg([
            "-f", "lavfi",
            "-i",
            (
                "anoisesrc=color=pink:"
                f"amplitude=0.35:duration={duration:.3f}"
            ),
            "-af", filter_chain,
            "-ac", "1",
            "-ar", "24000",
            "-y",
            output_path,
        ], timeout=180)

        return os.path.exists(output_path)

    except Exception:
        log.exception("Could not generate ambience.")
        return False


def apply_voice_and_speed(
    input_wav: str,
    output_wav: str,
    voice_type: str,
    speed: float,
):
    """
    Female: original model output.
    Male: lower-pitch processing as a fallback.

    Lowering pitch does not create a genuinely trained male voice.
    """

    filters = []

    if voice_type == "male":
        # Approximate male-like pitch; quality varies.
        filters.append("asetrate=24000*0.84")
        filters.append("aresample=24000")
        filters.append("atempo=1.190476")

    if speed != 1.0:
        filters.append(f"atempo={speed:.3f}")

    if not filters:
        shutil.copy2(input_wav, output_wav)
        return

    run_ffmpeg([
        "-i", input_wav,
        "-af", ",".join(filters),
        "-ac", "1",
        "-ar", "24000",
        "-y",
        output_wav,
    ], timeout=600)


def mix_ambience(
    speech_wav: str,
    scene: str,
    output_wav: str,
):
    duration = get_duration(speech_wav)

    if duration <= 0:
        shutil.copy2(speech_wav, output_wav)
        return

    with tempfile.TemporaryDirectory(prefix="levi_fx_") as temp:
        ambience_path = os.path.join(temp, "ambience.wav")

        created = create_ambience(
            scene,
            duration,
            ambience_path,
        )

        if not created:
            shutil.copy2(speech_wav, output_wav)
            return

        run_ffmpeg([
            "-i", speech_wav,
            "-i", ambience_path,
            "-filter_complex",
            (
                "[1:a]volume=0.08[bg];"
                "[0:a][bg]amix=inputs=2:"
                "duration=first:dropout_transition=2,"
                "alimiter=limit=0.95[out]"
            ),
            "-map", "[out]",
            "-ac", "1",
            "-ar", "24000",
            "-y",
            output_wav,
        ], timeout=600)


def convert_to_mp3(
    input_wav: str,
    output_mp3: str,
):
    run_ffmpeg([
        "-i", input_wav,
        "-vn",
        "-ac", "1",
        "-ar", "24000",
        "-b:a", AUDIO_BITRATE,
        "-codec:a", "libmp3lame",
        "-y",
        output_mp3,
    ], timeout=600)


def concat_mp3_files(
    mp3_files,
    output_path: str,
):
    """
    Concatenates same-format MP3 segments without re-encoding.
    """

    if not mp3_files:
        raise ValueError("No MP3 files to concatenate.")

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".txt",
        delete=False,
        encoding="utf-8",
    ) as f:
        list_path = f.name

        for path in mp3_files:
            escaped = os.path.abspath(path).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        run_ffmpeg([
            "-f", "concat",
            "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-y",
            output_path,
        ], timeout=1200)

    finally:
        try:
            os.remove(list_path)
        except OSError:
            pass


# ============================================================
# TELEGRAM HELPERS
# ============================================================

def build_voice_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👩 Female Voice",
                callback_data="voice:female",
            ),
            InlineKeyboardButton(
                "👨 Male Voice",
                callback_data="voice:male",
            ),
        ],
    ])


def build_speed_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1.0x", callback_data="speed:1.0"),
            InlineKeyboardButton("1.1x", callback_data="speed:1.1"),
            InlineKeyboardButton("1.2x", callback_data="speed:1.2"),
        ],
        [
            InlineKeyboardButton("1.3x", callback_data="speed:1.3"),
            InlineKeyboardButton("1.5x", callback_data="speed:1.5"),
            InlineKeyboardButton("1.7x", callback_data="speed:1.7"),
        ],
        [
            InlineKeyboardButton("2.0x", callback_data="speed:2.0"),
        ],
    ])


async def send_status(update, text):
    try:
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query:
            await update.callback_query.message.reply_text(text)
    except Exception:
        log.exception("Could not send status message.")


async def safe_send_document(
    context,
    chat_id,
    path,
    caption,
):
    for attempt in range(5):
        try:
            with open(path, "rb") as audio_file:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    caption=caption,
                    title=os.path.basename(path),
                    read_timeout=300,
                    write_timeout=300,
                    connect_timeout=60,
                    pool_timeout=60,
                )

            return True

        except RetryAfter as exc:
            delay = getattr(exc, "retry_after", 10)
            if not isinstance(delay, (int, float)):
                delay = 10

            await asyncio.sleep(delay + 1)

        except (TimedOut, NetworkError) as exc:
            log.warning(
                "Upload attempt %s failed: %s",
                attempt + 1,
                exc,
            )
            await asyncio.sleep(3 * (attempt + 1))

        except Exception:
            log.exception("Audio upload failed.")
            return False

    return False


# ============================================================
# /START AND /HELP
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎙️ Levithan Voice AI\n\n"
        "अपनी TXT novel भेजो।\n\n"
        "1. Bot शुरुआती 10–20 lines का demo बनाएगा।\n"
        "2. Male या Female voice चुनो।\n"
        "3. Demo सुनकर speed चुनो।\n"
        "4. पूरी audiobook बनना शुरू होगी।\n"
        "5. MP3 parts तैयार होते ही भेजे जाएँगे।\n\n"
        "Commands:\n"
        "/start - Start bot\n"
        "/cancel - Cancel current job\n"
        "/help - Help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ============================================================
# TXT UPLOAD
# ============================================================

async def handle_txt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.message
    document = message.document

    if not document:
        return

    file_name = document.file_name or "novel.txt"

    if not file_name.lower().endswith(".txt"):
        await message.reply_text(
            "कृपया TXT file भेजो।"
        )
        return

    if document.file_size and document.file_size > 100 * 1024 * 1024:
        await message.reply_text(
            "TXT file बहुत बड़ी है। पहले इसे छोटे हिस्सों में बाँटें।"
        )
        return

    user_id = update.effective_user.id

    old_job = USER_JOBS.get(user_id)

    if old_job and old_job.get("running"):
        await message.reply_text(
            "तुम्हारा पिछला audiobook job अभी चल रहा है। "
            "/cancel भेजकर उसे रोक सकते हो।"
        )
        return

    status = await message.reply_text(
        "📥 TXT डाउनलोड हो रही है..."
    )

    job_dir = tempfile.mkdtemp(prefix="levithan_job_")

    try:
        input_path = os.path.join(job_dir, "novel.txt")

        telegram_file = await context.bot.get_file(document.file_id)

        # PTB 21.x download_to_drive is asynchronous.
        await telegram_file.download_to_drive(
            custom_path=input_path
        )

        text = await asyncio.to_thread(
            read_text_file,
            input_path,
        )

        text = normalize_text(text)

        if not text:
            raise ValueError("TXT file खाली है।")

        demo_text = create_demo_text(text)

        USER_JOBS[user_id] = {
            "running": False,
            "dir": job_dir,
            "input": input_path,
            "text": text,
            "voice": "female",
            "speed": 1.0,
            "status_message_id": status.message_id,
            "chat_id": update.effective_chat.id,
        }

        await status.edit_text(
            "🎧 Demo तैयार कर रहा हूँ...\n"
            "👩 Default demo voice: Female"
        )

        demo_wav = os.path.join(job_dir, "demo.wav")
        demo_mp3 = os.path.join(job_dir, "demo.mp3")

        await asyncio.to_thread(
            synthesize_wav,
            demo_text,
            demo_wav,
        )

        await asyncio.to_thread(
            convert_to_mp3,
            demo_wav,
            demo_mp3,
        )

        ok = await safe_send_document(
            context,
            update.effective_chat.id,
            demo_mp3,
            "🎧 Novel Demo — शुरुआती lines",
        )

        if not ok:
            raise RuntimeError(
                "Demo audio Telegram पर भेजी नहीं जा सकी।"
            )

        await status.edit_text(
            "✅ Demo भेज दिया!\n\n"
            "अब narration voice चुनो।\n"
            "Female मूल मॉडल की आवाज़ है।\n"
            "Male विकल्प pitch-processed fallback है।",
            reply_markup=build_voice_keyboard(),
        )

    except Exception as exc:
        log.exception("TXT processing failed.")

        try:
            await status.edit_text(
                "❌ TXT/demo processing failed.\n\n"
                f"Error: {str(exc)[:800]}"
            )
        except Exception:
            pass

        if user_id in USER_JOBS:
            USER_JOBS.pop(user_id, None)

        shutil.rmtree(job_dir, ignore_errors=True)


# ============================================================
# VOICE SELECTION
# ============================================================

async def voice_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    job = USER_JOBS.get(user_id)

    if not job:
        await query.message.reply_text(
            "पहले TXT file भेजो।"
        )
        return

    voice = query.data.split(":", 1)[1]

    if voice not in ("male", "female"):
        return

    job["voice"] = voice

    label = "👨 Male" if voice == "male" else "👩 Female"

    await query.message.reply_text(
        f"{label} voice चुनी गई।\n"
        "अब narration speed चुनो:",
        reply_markup=build_speed_keyboard(),
    )


# ============================================================
# AUDIOBOOK GENERATION
# ============================================================

async def generate_audiobook(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    speed: float,
):
    user_id = update.effective_user.id
    job = USER_JOBS.get(user_id)

    if not job:
        await send_status(
            update,
            "TXT file नहीं मिली। पहले novel भेजो।"
        )
        return

    if job.get("running"):
        await send_status(
            update,
            "तुम्हारा audiobook पहले से बन रहा है।"
        )
        return

    job["running"] = True
    job["speed"] = speed
    job["cancelled"] = False

    chat_id = job["chat_id"]
    job_dir = job["dir"]

    start_time = time.monotonic()

    status = await context.bot.send_message(
        chat_id=chat_id,
        text=(
            "🚀 Audiobook generation शुरू!\n"
            f"Voice: {job['voice']}\n"
            f"Speed: {speed}x\n"
            "MP3 parts तैयार होते ही भेजे जाएँगे।"
        ),
    )

    chunks = split_text(job["text"])

    if not chunks:
        job["running"] = False
        await status.edit_text("TXT में readable text नहीं मिला।")
        return

    pending_mp3 = []
    pending_size = 0
    part_number = 0
    generated = 0

    try:
        for index, chunk in enumerate(chunks, start=1):
            if job.get("cancelled"):
                await status.edit_text(
                    "⏹️ Audiobook generation cancel कर दी गई।"
                )
                return

            chunk_dir = os.path.join(
                job_dir,
                f"chunk_{index:06d}",
            )

            os.makedirs(chunk_dir, exist_ok=True)

            raw_wav = os.path.join(chunk_dir, "raw.wav")
            voice_wav = os.path.join(chunk_dir, "voice.wav")
            mixed_wav = os.path.join(chunk_dir, "mixed.wav")
            chunk_mp3 = os.path.join(chunk_dir, "chunk.mp3")

            # Generate speech.
            await asyncio.to_thread(
                synthesize_wav,
                chunk,
                raw_wav,
            )

            # Apply voice processing and selected speed.
            await asyncio.to_thread(
                apply_voice_and_speed,
                raw_wav,
                voice_wav,
                job["voice"],
                speed,
            )

            # Detect a scene and create subtle synthetic ambience.
            scene = detect_scene(chunk)

            await asyncio.to_thread(
                mix_ambience,
                voice_wav,
                scene,
                mixed_wav,
            )

            # Convert to a compact MP3 segment.
            await asyncio.to_thread(
                convert_to_mp3,
                mixed_wav,
                chunk_mp3,
            )

            size = os.path.getsize(chunk_mp3)

            pending_mp3.append(chunk_mp3)
            pending_size += size
            generated += 1

            # Send a part when it approaches the configured size.
            if pending_size >= TARGET_PART_SIZE_BYTES:
                part_number += 1

                part_path = os.path.join(
                    job_dir,
                    f"part_{part_number:04d}.mp3",
                )

                await asyncio.to_thread(
                    concat_mp3_files,
                    pending_mp3,
                    part_path,
                )

                # Safety check before uploading.
                if os.path.getsize(part_path) > MAX_AUDIO_SIZE_BYTES:
                    raise RuntimeError(
                        "MP3 part exceeded the configured size limit. "
                        "Reduce TARGET_PART_SIZE_BYTES."
                    )

                sent = await safe_send_document(
                    context,
                    chat_id,
                    part_path,
                    f"🎧 Audiobook Part {part_number}",
                )

                if not sent:
                    raise RuntimeError(
                        f"Telegram upload failed for part {part_number}."
                    )

                pending_mp3.clear()
                pending_size = 0

                elapsed = time.monotonic() - start_time

                await status.edit_text(
                    f"🎙️ Audiobook generation running\n"
                    f"Progress: {index}/{len(chunks)} chunks\n"
                    f"Parts sent: {part_number}\n"
                    f"Elapsed: {int(elapsed)} seconds"
                )

            # Cosmetic upload action.
            try:
                await context.bot.send_chat_action(
                    chat_id=chat_id,
                    action=ChatAction.UPLOAD_DOCUMENT,
                )
            except Exception:
                pass

        # Send the remaining MP3 segments.
        if pending_mp3:
            part_number += 1

            part_path = os.path.join(
                job_dir,
                f"part_{part_number:04d}.mp3",
            )

            await asyncio.to_thread(
                concat_mp3_files,
                pending_mp3,
                part_path,
            )

            if os.path.getsize(part_path) > MAX_AUDIO_SIZE_BYTES:
                raise RuntimeError(
                    "Final MP3 part exceeded the configured size limit."
                )

            sent = await safe_send_document(
                context,
                chat_id,
                part_path,
                f"🎧 Audiobook Part {part_number}",
            )

            if not sent:
                raise RuntimeError(
                    "Final MP3 upload failed."
                )

        elapsed = time.monotonic() - start_time

        await status.edit_text(
            "✅ Audiobook generation completed!\n\n"
            f"Voice: {job['voice']}\n"
            f"Speed: {speed}x\n"
            f"MP3 parts sent: {part_number}\n"
            f"Text chunks processed: {generated}\n"
            f"Elapsed: {int(elapsed)} seconds"
        )

    except Exception as exc:
        log.exception("Audiobook generation failed.")

        await status.edit_text(
            "❌ Audiobook generation failed.\n\n"
            f"Error: {str(exc)[:800]}"
        )

    finally:
        job["running"] = False


# ============================================================
# SPEED SELECTION
# ============================================================

async def speed_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query
    await query.answer()

    try:
        speed = float(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await query.message.reply_text("Invalid speed.")
        return

    if speed not in (1.0, 1.1, 1.2, 1.3, 1.5, 1.7, 2.0):
        await query.message.reply_text("Unsupported speed.")
        return

    user_id = update.effective_user.id
    job = USER_JOBS.get(user_id)

    if not job:
        await query.message.reply_text(
            "पहले TXT file भेजो।"
        )
        return

    if job.get("running"):
        await query.message.reply_text(
            "Audiobook पहले से बन रही है।"
        )
        return

    await query.message.reply_text(
        f"⚡ Speed {speed}x चुनी गई।\n"
        "Audiobook generation शुरू हो रही है..."
    )

    # Generate in the background so callback processing can continue.
    asyncio.create_task(
        generate_audiobook(
            update,
            context,
            speed,
        )
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    user_id = update.effective_user.id
    job = USER_JOBS.get(user_id)

    if not job:
        await update.message.reply_text(
            "कोई active job नहीं है।"
        )
        return

    job["cancelled"] = True

    await update.message.reply_text(
        "⏹️ Cancel request भेज दी गई है।"
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):
    log.error(
        "Telegram update failed.",
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    check_dependencies()

    # Fail early if the model cannot be loaded.
    load_tts_model()

    builder = Application.builder().token(BOT_TOKEN)

    if LOCAL_API_URL:
        builder = builder.base_url(
            f"{LOCAL_API_URL}/bot"
        )
        builder = builder.base_file_url(
            f"{LOCAL_API_URL}/file/bot"
        )

    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("cancel", cancel_command))

    application.add_handler(
        CallbackQueryHandler(
            voice_callback,
            pattern=r"^voice:(male|female)$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            speed_callback,
            pattern=r"^speed:(1\.0|1\.1|1\.2|1\.3|1\.5|1\.7|2\.0)$",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_txt,
        )
    )

    application.add_error_handler(error_handler)

    log.info("Levithan Voice AI is starting...")
    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
