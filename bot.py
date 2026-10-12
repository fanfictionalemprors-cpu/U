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
        path_text =
