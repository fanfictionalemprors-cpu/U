#!/usr/bin/env python3
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
import unicodedata
from pathlib import Path

import edge_tts
from gtts import gTTS
from pypdf import PdfReader
from docx import Document
from ebooklib import epub, ITEM_DOCUMENT
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import (
    Application, ApplicationHandlerStop, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, TypeHandler, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("tts-bot")

BOT_TOKEN = os.environ["BOT_TOKEN"]
LOCAL_BOT_API = os.getenv("LOCAL_BOT_API", "0").lower() in {"1", "true", "yes"}
LOCAL_BOT_API_URL = os.getenv("LOCAL_BOT_API_URL", "http://127.0.0.1:8081").rstrip("/")
LOCAL_API_CONTAINER = os.getenv("LOCAL_API_CONTAINER", "telegram-bot-api")
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "1900" if LOCAL_BOT_API else "50"))
MAX_INPUT_MB = int(os.getenv("MAX_INPUT_MB", "1900" if LOCAL_BOT_API else "20"))
SEND_PART_MB = int(os.getenv("SEND_PART_MB", "1800" if LOCAL_BOT_API else "45"))
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "20000000"))
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "2500"))
TTS_CONCURRENCY = max(1, min(8, int(os.getenv("TTS_CONCURRENCY", "6"))))
MAX_QUEUE = max(1, int(os.getenv("MAX_QUEUE", "8")))
PROGRESS_EDIT_SECONDS =float(os.getenv("PROGRESS_EDIT_SECONDS", "3"))
WORK_ROOT = Path(os.getenv("TTS_WORKDIR", "/tmp/telegram-tts-work"))
WORK_ROOT.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", str(Path(__file__).resolve().parent / "bot_data" / "settings.json")))
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))}

GTTS_SAMPLE_RATE = 24000          # gTTS mp3 output is 24 kHz
EDGE_BYTES_PER_SEC = 6000         # Edge mp3 is 48 kbps -> used for cheap duration estimates
SUPPORTED_EXT = {".txt", ".text", ".md", ".pdf", ".docx", ".epub"}

# key -> (Edge short name, label)
VOICES = {
    # Hindi
    "madhur": ("hi-IN-MadhurNeural", "🇮🇳 Hindi Male — Madhur"),
    "swara": ("hi-IN-SwaraNeural", "🇮🇳 Hindi Female — Swara"),
    # English (US)
    "guy": ("en-US-GuyNeural", "🇺🇸 English Male — Guy"),
    "jenny": ("en-US-JennyNeural", "🇺🇸 English Female — Jenny"),
    "aria": ("en-US-AriaNeural", "🇺🇸 English Female — Aria"),
    "ava": ("en-US-AvaNeural", "🇺🇸 English Female — Ava"),
    "emma": ("en-US-EmmaNeural", "🇺🇸 English Female — Emma"),
    "andrew": ("en-US-AndrewNeural", "🇺🇸 English Male — Andrew"),
    "brian": ("en-US-BrianNeural", "🇺🇸 English Male — Brian"),
    "christopher": ("en-US-ChristopherNeural", "🇺🇸 English Male — Christopher"),
    # English (UK / India / Australia)
    "ryan": ("en-GB-RyanNeural", "🇬🇧 English Male — Ryan"),
    "sonia": ("en-GB-SoniaNeural", "🇬🇧 English Female — Sonia"),
    "neerja": ("en-IN-NeerjaNeural", "🇮🇳 English Female — Neerja"),
    "prabhat": ("en-IN-PrabhatNeural", "🇮🇳 English Male — Prabhat"),
    "natasha": ("en-AU-NatashaNeural", "🇦🇺 English Female — Natasha"),
    # Multilingual (read Hindi + English mixed text far better)
    "avam": ("en-US-AvaMultilingualNeural", "🌐 Multilingual Female — Ava"),
    "andrewm": ("en-US-AndrewMultilingualNeural", "🌐 Multilingual Male — Andrew"),
    "emmam": ("en-US-EmmaMultilingualNeural", "🌐 Multilingual Female — Emma"),
    "brianm": ("en-US-BrianMultilingualNeural", "🌐 Multilingual Male — Brian"),
    # Other Indian languages
    "bashkar": ("bn-IN-BashkarNeural", "🇮🇳 Bengali Male — Bashkar"),
    "tanishaa": ("bn-IN-TanishaaNeural", "🇮🇳 Bengali Female — Tanishaa"),
    "pallavi": ("ta-IN-PallaviNeural", "🇮🇳 Tamil Female — Pallavi"),
    "valluvar": ("ta-IN-ValluvarNeural", "🇮🇳 Tamil Male — Valluvar"),
    "mohan": ("te-IN-MohanNeural", "🇮🇳 Telugu Male — Mohan"),
    "shruti": ("te-IN-ShrutiNeural", "🇮🇳 Telugu Female — Shruti"),
    "aarohi": ("mr-IN-AarohiNeural", "🇮🇳 Marathi Female — Aarohi"),
    "manohar": ("mr-IN-ManoharNeural", "🇮🇳 Marathi Male — Manohar"),
    "dhwani": ("gu-IN-DhwaniNeural", "🇮🇳 Gujarati Female — Dhwani"),
    "niranjan": ("gu-IN-NiranjanNeural", "🇮🇳 Gujarati Male — Niranjan"),
    "sapna": ("kn-IN-SapnaNeural", "🇮🇳 Kannada Female — Sapna"),
    "gagan": ("kn-IN-GaganNeural", "🇮🇳 Kannada Male — Gagan"),
    "sobhana": ("ml-IN-SobhanaNeural", "🇮🇳 Malayalam Female — Sobhana"),
    "midhun": ("ml-IN-MidhunNeural", "🇮🇳 Malayalam Male — Midhun"),
    "gul": ("ur-IN-GulNeural", "🇮🇳 Urdu Female — Gul"),
    "salman": ("ur-IN-SalmanNeural", "🇮🇳 Urdu Male — Salman"),
}
VOICE_PATTERN = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")
DEMO_TEXT = {
    "hi": "नमस्ते! मैं आपकी ऑडियोबुक पढ़ने के लिए तैयार हूँ। यह मेरी आवाज़ का एक छोटा सा डेमो है।",
    "en": "Hello! I'm ready to read your audiobook. This is a short demo of my voice.",
    "bn": "নমস্কার! আমি আপনার অডিওবুক পড়ার জন্য প্রস্তুত। এটি আমার কণ্ঠের একটি ছোট ডেমো।",
    "ta": "வணக்கம்! உங்கள் ஆடியோ புத்தகத்தைப் படிக்க நான் தயார். இது என் குரலின் ஒரு சிறிய மாதிரி.",
    "te": "నమస్కారం! మీ ఆడియోబుక్ చదవడానికి నేను సిద్ధంగా ఉన్నాను. ఇది నా గొంతు యొక్క చిన్న డెమో.",
    "mr": "नमस्कार! मी तुमचे ऑडिओबुक वाचण्यासाठी तयार आहे. हा माझ्या आवाजाचा एक छोटासा डेमो आहे.",
    "gu": "નમસ્તે! હું તમારું ઓડિયોબુક વાંચવા માટે તૈયાર છું. આ મારા અવાજનો એક નાનો ડેમો છે.",
    "kn": "ನಮಸ್ಕಾರ! ನಿಮ್ಮ ಆಡಿಯೋಬುಕ್ ಓದಲು ನಾನು ಸಿದ್ಧ. ಇದು ನನ್ನ ಧ್ವನಿಯ ಒಂದು ಚಿಕ್ಕ ಡೆಮೊ.",
    "ml": "നമസ്കാരം! നിങ്ങളുടെ ഓഡിയോബുക്ക് വായിക്കാൻ ഞാൻ തയ്യാറാണ്. ഇത് എന്റെ ശബ്ദത്തിന്റെ ഒരു ചെറിയ ഡെമോ ആണ്.",
    "ur": "السلام علیکم! میں آپ کی آڈیو بک پڑھنے کے لیے تیار ہوں۔ یہ میری آواز کا ایک مختصر ڈیمو ہے۔",
}
MULTI_DEMO = ("नमस्ते! Hello! I can read Hindi and English together. यह मेरी आवाज़ का डेमो है।")


def demo_text_for(voice):
    if "Multilingual" in voice:
        return MULTI_DEMO
    return DEMO_TEXT.get(voice.split("-", 1)[0].lower(), DEMO_TEXT["en"])

SETTINGS = {}
TASKS = {}
PROGRESS = {}
SKIPPED = {}
QUEUES = {}     # uid -> waiting jobs
CURRENT = {}    # uid -> running job
WORKERS = {}    # uid -> queue worker task
BG_TASKS = set()  # keeps fire-and-forget tasks alive


# ───────────────────────── helpers ─────────────────────────

def fmt_time(seconds):
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h {m:02d}m {sec:02d}s" if h else f"{m}m {sec:02d}s"


def fmt_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024


def progress_bar(done, total, width=12):
    total = max(1, total)
    filled = min(width, int(width * done / total))
    return "█" * filled + "░" * (width - filled)


def progress_text(stage, done, total, started, extra=""):
    elapsed = time.monotonic() - started
    rate = done / elapsed if elapsed > 0 and done > 0 else 0
    eta = (total - done) / rate if rate > 0 else 0
    pct = min(100, int(100 * done / max(1, total)))
    eta_text = fmt_time(eta) if done else "Calculating…"
    lines = [
        f"{stage} {progress_bar(done, total)} {pct}%",
        f"Progress: {done:,}/{total:,}",
        f"⏱ Used time: {fmt_time(elapsed)}",
        f"⌛ Est. time left: {eta_text}",
    ]
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def load_settings():
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            SETTINGS[int(k)] = v
    except FileNotFoundError:
        pass
    except Exception:
        log.exception("Could not load settings file; starting fresh")


def save_settings():
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SETTINGS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(SETTINGS, ensure_ascii=False), encoding="utf-8")
        tmp.replace(SETTINGS_FILE)
    except Exception:
        log.exception("Could not save settings")


def cfg(uid):
    s = SETTINGS.setdefault(uid, {})
    s.setdefault("voice", os.getenv("DEFAULT_VOICE", "hi-IN-MadhurNeural"))
    s.setdefault("rate", float(os.getenv("DEFAULT_RATE", "1.1")))
    s.setdefault("pitch", int(os.getenv("DEFAULT_PITCH", "0")))
    s.setdefault("engine", os.getenv("DEFAULT_ENGINE", "edge").lower())
    s.setdefault("name", "Audiobook")
    return s


def safe_name(value):
    value = Path(value or "Audiobook").stem
    # keep letters/digits in any script (incl. Hindi matras) plus a few safe symbols
    value = "".join(
        ch for ch in value
        if ch.isalnum() or unicodedata.category(ch).startswith("M") or ch in " _().-"
    )
    value = re.sub(r"\s+", " ", value).strip(" ._-")
    return (value or "Audiobook")[:100]


def edge_rate(rate):
    return f"{round((rate - 1.0) * 100):+d}%"


def voice_label(voice):
    return next((v[1] for v in VOICES.values() if v[0] == voice), voice)


async def safe_edit(target, text, markup=None):
    """Edit a message, ignoring 'message is not modified' and flood errors."""
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramError:
        pass


_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def clean_text(text):
    text = re.sub(r"\r\n?", "\n", text)
    text = _CTRL.sub(" ", text)
    # soft hyphen, zero-width space/marks, BOM, word joiner, bidi marks (keep ZWJ/ZWNJ for Indic scripts)
    text = re.sub(r"[\u00ad\u200b\u200e\u200f\u2060\ufeff\u202a-\u202e]", "", text)
    # emoji / pictographs / private-use symbols are unspeakable and can make Edge return no audio
    text = re.sub(r"[\U0001F000-\U0001FAFF\u2600-\u27bf\ue000-\uf8ff\ufe0f]", " ", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)      # PDF hyphenation
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_text(path):
    ext = path.suffix.lower()
    if ext in {".txt", ".text", ".md"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        reader = PdfReader(str(path))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    if ext == ".docx":
        doc = Document(str(path))
        return "\n".join(p.text for p in doc.paragraphs)
    if ext == ".epub":
        book = epub.read_epub(str(path))
        parts = []
        for item in book.get_items():
            if item.get_type() == ITEM_DOCUMENT:
                soup = BeautifulSoup(item.get_content(), "html.parser")
                parts.append(soup.get_text(" ", strip=True))
        return "\n\n".join(parts)
    raise ValueError("Supported formats: TXT, MD, PDF, DOCX, EPUB.")


def split_text(text, limit=CHUNK_CHARS):
    """Index-based splitter (linear time) that keeps sentence punctuation."""
    text = clean_text(text)
    n = len(text)
    i = 0
    chunks = []
    groups = (("\n",), ("। ", ". ", "! ", "? ", "।\n", ".\n"), ("; ", ", ", " "))
    while i < n:
        end = min(n, i + limit)
        cut = end
        if end < n:
            window = text[i:end]
            for group in groups:
                best = -1
                for sep in group:
                    p = window.rfind(sep)
                    if p >= limit // 2:
                        best = max(best, p + max(1, len(sep.rstrip())))
                if best > 0:
                    cut = i + best
                    break
        chunk = text[i:cut].strip()
        if chunk and re.search(r"\w", chunk):      # skip chunks with nothing speakable
            chunks.append(chunk)
        i = cut
    return chunks


def concat_line(path):
    return "file '" + path.as_posix().replace("'", "'\\''") + "'\n"


async def run_ffmpeg(*args):
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    return proc.returncode, err.decode(errors="replace")[-800:]


# ───────────────────────── TTS engines ─────────────────────────

async def make_edge(text, path, s, attempts=3):
    """Retry transient Edge TTS failures and reject empty audio."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            path.unlink(missing_ok=True)
            tts = edge_tts.Communicate(
                text=text, voice=s["voice"], rate=edge_rate(s["rate"]),
                pitch=f"{s['pitch']:+d}Hz",
            )
            await tts.save(str(path))
            if path.is_file() and path.stat().st_size > 256:
                return
            raise RuntimeError("Edge TTS returned no audio or an empty audio file.")
        except Exception as exc:
            last_error = exc
            path.unlink(missing_ok=True)
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
    raise RuntimeError(
        f"Edge TTS failed after {attempts} attempts ({type(last_error).__name__}: {last_error}). "
        "Try /engine google, or retry later if the TTS service is temporarily unavailable."
    ) from last_error


def google_lang(voice):
    lang = voice.split("-", 1)[0].lower()
    tld = "co.in" if voice.startswith(("en-IN", "hi-", "bn-", "ta-", "te-", "mr-", "gu-", "kn-", "ml-", "ur-")) else "com"
    if voice.startswith("en-GB"):
        tld = "co.uk"
    elif voice.startswith("en-AU"):
        tld = "com.au"
    return lang, tld


async def make_google(text, path, s):
    lang, tld = google_lang(s["voice"])
    await asyncio.to_thread(gTTS(text=text, lang=lang, tld=tld, slow=False).save, str(path))
    # Google TTS has no native rate/pitch controls -> FFmpeg post-processing.
    if s["rate"] != 1.0 or s["pitch"] != 0:
        adjusted = path.with_name(path.stem + "_adjusted.mp3")
        filters_ = []
        tempo = s["rate"]
        if s["pitch"] != 0:
            factor = 2 ** (s["pitch"] / 100)          # ±20 -> about ±15 % pitch shift
            filters_ += [f"asetrate={GTTS_SAMPLE_RATE * factor:.2f}", f"aresample={GTTS_SAMPLE_RATE}"]
            tempo = tempo / factor                    # asetrate also changes speed; compensate
        tempo = min(2.0, max(0.5, tempo))
        filters_.append(f"atempo={tempo:.6f}")
        code, err = await run_ffmpeg("-i", str(path), "-filter:a", ",".join(filters_), "-y", str(adjusted))
        if code:
            adjusted.unlink(missing_ok=True)
            raise RuntimeError("Google audio processing failed: " + err)
        path.unlink(missing_ok=True)
        adjusted.rename(path)


async def audio_metadata(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration,size",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError("Could not read audio metadata: " + err.decode(errors="replace")[-500:])
    vals = out.decode(errors="replace").strip().splitlines()
    duration = float(vals[0]) if vals else 0.0
    size = int(vals[1]) if len(vals) > 1 and vals[1].isdigit() else path.stat().st_size
    return duration, size


async def make_silence(path, seconds=0.4):
    """Short silent mp3 (matches Edge's 24 kHz mono 48 kbps) used for unspeakable fragments."""
    code, err = await run_ffmpeg(
        "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono", "-t", str(seconds),
        "-c:a", "libmp3lame", "-b:a", "48k", "-y", str(path),
    )
    if code or not path.is_file():
        raise RuntimeError("Could not create silence: " + err)


async def edge_robust(text, out, folder, tag, s, skipped, depth=0):
    """Speak `text`; if Edge returns no audio, split in halves and retry recursively.
    A fragment that still cannot be spoken (symbols, page junk) becomes brief silence
    so one bad piece never kills a whole audiobook."""
    try:
        # Fast path: one retry at the top level, a single try on split fragments,
        # so a bad piece costs seconds, not minutes.
        await make_edge(text, out, s, attempts=1 if depth else 2)
        return
    except Exception as exc:
        if len(text) > 200 and depth < 4:
            mid = len(text) // 2
            cut = max(text.rfind(". ", 0, mid + 200), text.rfind("। ", 0, mid + 200), text.rfind(" ", 0, mid))
            if cut < len(text) // 4 or cut > len(text) * 3 // 4:
                cut = mid
            parts = [t for t in (text[:cut].strip(), text[cut:].strip()) if t]
            subpaths = []
            try:
                for j, part in enumerate(parts, 1):
                    sp = folder / f"{tag}_{depth}_{j}.mp3"
                    if re.search(r"\w", part):
                        await edge_robust(part, sp, folder, f"{tag}_{depth}_{j}", s, skipped, depth + 1)
                    else:
                        await make_silence(sp)
                    subpaths.append(sp)
                lst = folder / f"{tag}_{depth}.txt"
                lst.write_text("".join(concat_line(x) for x in subpaths), encoding="utf-8")
                code, err = await run_ffmpeg("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-y", str(out))
                lst.unlink(missing_ok=True)
                if code or not out.is_file() or out.stat().st_size <= 256:
                    raise RuntimeError("Could not join split audio: " + err) from exc
                return
            finally:
                for sp in subpaths:
                    sp.unlink(missing_ok=True)
        # Tiny fragment Edge refuses to speak: skip it with a short pause.
        log.warning("Skipping unspeakable fragment (%s): %r", type(exc).__name__, text[:80])
        try:
            chunk_no = int(tag[1:7])
        except ValueError:
            chunk_no = 0
        skipped.append({"chunk": chunk_no, "text": text[:200]})
        await make_silence(out)


async def generate_audio(text, folder, s, status, uid, started):
    """Generate chunks concurrently, preserving order in the final audio."""
    chunks = split_text(text)
    if not chunks:
        raise ValueError("No readable text found.")

    semaphore = asyncio.Semaphore(TTS_CONCURRENCY)
    pieces = [folder / f"chunk_{i:06d}.mp3" for i in range(1, len(chunks) + 1)]
    state = {"done": 0, "bytes": 0, "last_edit": 0.0}
    skipped = []
    total = len(chunks)
    SKIPPED[uid] = {"items": skipped, "total": total}
    status_lock = asyncio.Lock()
    PROGRESS[uid] = {
        "stage": "Generating audio", "done": 0, "total": total, "started": started,
        "audio_duration": 0.0, "audio_bytes": 0, "concurrency": TTS_CONCURRENCY,
    }

    async def generate_one(index, chunk):
        out = pieces[index]
        async with semaphore:
            if s["engine"] == "google":
                await make_google(chunk, out, s)
            else:
                await edge_robust(chunk, out, folder, f"c{index + 1:06d}", s, skipped)

            if not out.is_file() or out.stat().st_size <= 256:
                raise RuntimeError(f"TTS produced no audio for chunk {index + 1}/{total}.")
            size = out.stat().st_size                 # no ffprobe per chunk: much faster
            async with status_lock:
                state["done"] += 1
                state["bytes"] += size
                done, total_bytes = state["done"], state["bytes"]
                est_duration = total_bytes / EDGE_BYTES_PER_SEC
                PROGRESS[uid] = {
                    "stage": "Generating audio", "done": done, "total": total, "started": started,
                    "audio_duration": est_duration, "audio_bytes": total_bytes,
                    "concurrency": TTS_CONCURRENCY,
                }
                now = time.monotonic()
                if done == total or now - state["last_edit"] >= PROGRESS_EDIT_SECONDS:
                    state["last_edit"] = now
                    extra = (
                        f"🎧 Audio made: {done}/{total} chunks\n"
                        f"⏱ Audio duration made: ~{fmt_time(est_duration)}\n"
                        f"📦 Generated audio size: {fmt_size(total_bytes)}\n"
                        f"⚡ Parallel workers: {TTS_CONCURRENCY}"
                    )
                    # fire-and-forget: a slow Telegram call must never hold up a TTS worker
                    edit_task = asyncio.create_task(
                        safe_edit(status, progress_text("🎙️ Generating", done, total, started, extra)))
                    BG_TASKS.add(edit_task)
                    edit_task.add_done_callback(BG_TASKS.discard)

    tasks =[asyncio.create_task(generate_one(i, chunk)) for i, chunk in enumerate(chunks)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    concat = folder / "concat.txt"
    concat.write_text("".join(concat_line(x) for x in pieces), encoding="utf-8")
    merged = folder / "merged.mp3"
    code, err = await run_ffmpeg("-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-y", str(merged))
    if code or not merged.is_file() or merged.stat().st_size <= 256:
        raise RuntimeError("FFmpeg merge produced no audio: " + err)
    for piece in pieces:                              # free disk early
        piece.unlink(missing_ok=True)
    return merged


async def split_audio(source, folder):
    max_bytes = SEND_PART_MB * 1024 * 1024
    if source.stat().st_size <= max_bytes:
        return [source]
    duration, _ = await audio_metadata(source)
    if duration <= 0:
        raise RuntimeError("Could not determine audio duration.")
    segment = max(20, duration * max_bytes / source.stat().st_size * 0.80)
    for _ in range(10):
        for old in folder.glob("part_*.mp3"):
            old.unlink(missing_ok=True)
        code, err = await run_ffmpeg(
            "-i", str(source), "-f", "segment", "-segment_time", str(segment),
            "-c", "copy", "-reset_timestamps", "1", "-y", str(folder / "part_%03d.mp3"),
        )
        if code:
            raise RuntimeError("Audio split failed: " + err)
        parts = sorted(folder.glob("part_*.mp3"))
        if parts and all(p.stat().st_size <= max_bytes for p in parts):
            return parts
        segment *= 0.65
    raise RuntimeError("Could not split audio under SEND_PART_MB; lower the value.")


# ───────────────────────── commands ─────────────────────────

async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS:
        user = update.effective_user
        if not user or user.id not in ALLOWED_USERS:
            raise ApplicationHandlerStop


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎧 TTS Bot ready!\nSend TXT, MD, PDF, DOCX or EPUB — or just type/paste text.\n"
        "Use /settings for voice, rate, pitch and filename, or /voices to see every voice."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — Start\n/help — Help\n/settings — Settings menu\n"
        "/voice <name> — Set voice (or /voice for a menu)\n/voices — List all voices\n"
        "/rate 1.5 — Rate (0.5–2.0)\n/speed 1.5 — Rate alias\n"
        "/pitch 5 — Pitch (-20 to +20 Hz)\n/engine edge|google — Engine\n"
        "/name My Audiobook — Output filename\n/demo [voice|all] — Voice demo\n"
        "/status — Progress\n/queue — Show queue\n/cancel — Cancel running job\n"
        "/clearqueue — Remove waiting jobs\n/cancelall — Cancel everything\n\n"
        f"Send a file, or plain text, to convert it. You can send up to {MAX_QUEUE} files at once; "
        "they are converted one after another."
    )


async def voices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [f"/voice {key} — {label}" for key, (_, label) in VOICES.items()]
    text = ("🎙️ Available voices\n\n" + "\n".join(lines)
            + "\n\nHear one: /demo <name> (e.g. /demo madhur) · all: /demo all"
            + "\nYou can also pass any full Edge name, e.g. /voice fr-FR-DeniseNeural")
    for i in range(0, len(text), 3900):                # Telegram limit is 4096 chars
        await update.effective_message.reply_text(text[i:i + 3900])


def settings_text(uid):
    s = cfg(uid)
    return (
        "⚙️ TTS Settings\n\n"
        f"Voice: {voice_label(s['voice'])}\nRate: {s['rate']:.2f}x\nPitch: {s['pitch']:+d} Hz\n"
        f"Engine: {s['engine']}\nAudio name: {s['name']}"
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Voice", callback_data="menu:voice"),
         InlineKeyboardButton("Rate", callback_data="menu:rate")],
        [InlineKeyboardButton("Pitch", callback_data="menu:pitch"),
         InlineKeyboardButton("Engine", callback_data="menu:engine")],
        [InlineKeyboardButton("Audio name help", callback_data="menu:name"),
         InlineKeyboardButton("Refresh", callback_data="menu:refresh")],
    ])


def voice_keyboard():
    # One row per voice: [select voice] [🔊 hear demo]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"voice:{key}"),
         InlineKeyboardButton("🔊 Demo", callback_data=f"demo:{key}")]
        for key, (_, label) in VOICES.items()
    ]
    rows.append([InlineKeyboardButton("🔊 Demo ALL voices", callback_data="demo:all")])
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="menu:refresh")])
    return InlineKeyboardMarkup(rows)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        settings_text(update.effective_user.id), reply_markup=settings_keyboard()
    )


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        arg = context.args[0]
        key = arg.lower()
        if key in VOICES:
            cfg(uid)["voice"] = VOICES[key][0]
            save_settings()
            await update.effective_message.reply_text(f"Voice set: {VOICES[key][1]}")
        elif VOICE_PATTERN.match(arg):
            cfg(uid)["voice"] = arg
            save_settings()
            await update.effective_message.reply_text(f"Voice set: {arg}")
        else:
            await update.effective_message.reply_text("Unknown voice. Use /voices to see the list.")
        return
    await update.effective_message.reply_text("Choose voice:", reply_markup=voice_keyboard())


async def rate_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current rate: {s['rate']:.2f}x. Use /rate 1.5 (0.5–2.0).")
        return
    try:
        value = float(context.args[0])
        if not 0.5 <= value <= 2.0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Rate must be 0.5–2.0, e.g. /rate 1.5")
        return
    s["rate"] = value
    save_settings()
    await update.effective_message.reply_text(f"Rate set to {value:.2f}x.")


async def pitch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current pitch: {s['pitch']:+d} Hz. Use /pitch -5 (-20 to +20).")
        return
    try:
        value = int(context.args[0])
        if not -20 <= value <= 20:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Pitch must be an integer from -20 to +20.")
        return
    s["pitch"] = value
    save_settings()
    await update.effective_message.reply_text(f"Pitch set to {value:+d} Hz.")


async def engine_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Engine: {s['engine']}. Use /engine edge or /engine google.")
        return
    engine = context.args[0].lower()
    if engine not in {"edge", "google"}:
        await update.effective_message.reply_text("Use /engine edge or /engine google.")
        return
    s["engine"] = engine
    save_settings()
    await update.effective_message.reply_text(f"Engine set to {engine}.")


async def name_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if not context.args:
        await update.effective_message.reply_text(f"Current name: {s['name']}\nUse /name My Audiobook")
        return
    s["name"] = safe_name(" ".join(context.args))
    save_settings()
    await update.effective_message.reply_text(f"Audio name set to {s['name']}.mp3")


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    task = TASKS.get(uid)
    p = PROGRESS.get(uid)
    if not task or task.done():
        await update.effective_message.reply_text("No active conversion.")
    elif p:
        extra = f"📋 Queue: job 1 of {queue_size(update.effective_user.id)} — {CURRENT.get(update.effective_user.id, {}).get('label', '')}\n"
        if p.get("audio_duration") is not None:
            extra += (f"🎧 Audio duration made: ~{fmt_time(p.get('audio_duration', 0))}\n"
                      f"📦 Generated audio size: {fmt_size(p.get('audio_bytes', 0))}\n"
                      f"⚡ Parallel workers: {p.get('concurrency', TTS_CONCURRENCY)}")
        await update.effective_message.reply_text(
            progress_text(p["stage"], p["done"], p["total"], p.get("started", time.monotonic()), extra))
    else:
        await update.effective_message.reply_text("Conversion running.")


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    task = TASKS.get(update.effective_user.id)
    if not task or task.done():
        await update.effective_message.reply_text("No active conversion.")
        return
    task.cancel()
    waiting = len(QUEUES.get(update.effective_user.id, []))
    await update.effective_message.reply_text(
        "Cancellation requested; temporary files will be cleared."
        + (f"\nNext in queue starts automatically ({waiting} waiting). Use /cancelall to stop everything." if waiting else ""))


async def send_demo(message, uid, voice):
    """Synthesize and send a demo of one voice using the user's rate/pitch/engine."""
    folder = Path(tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT))
    try:
        s = cfg(uid).copy()
        s["voice"] = voice
        output = folder / f"demo_{safe_name(voice)}.mp3"
        text = demo_text_for(voice)
        if s["engine"] == "google":
            await make_google(text, output, s)
        else:
            await make_edge(text, output, s)
        with output.open("rb") as audio:
            await message.reply_audio(
                audio=audio, filename=output.name, title=voice_label(voice)[:64],
                caption=f"🎙️ {voice_label(voice)}",
            )
        return True
    except Exception as exc:
        log.exception("Demo failed for %s", voice)
        err = str(exc).replace(BOT_TOKEN, "[REDACTED]")[:300]
        await message.reply_text(f"Demo failed for {voice}: {type(exc).__name__}: {err}")
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def resolve_voice(arg):
    key = arg.lower()
    if key in VOICES:
        return VOICES[key][0]
    if VOICE_PATTERN.match(arg):
        return arg
    return None


async def demo_all(message, uid):
    """Send a demo for every voice, a few at a time to stay within rate limits."""
    voices = [v for v, _ in VOICES.values()]
    sem = asyncio.Semaphore(TTS_CONCURRENCY)
    status = await message.reply_text(f"🔊 Generating {len(voices)} demos…")
    done = 0

    async def one(voice):
        nonlocal done
        async with sem:
            data = await _demo_file(uid, voice)
        return voice, data

    async def _demo_file(uid_, voice):
        folder = Path(tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT))
        try:
            s = cfg(uid_).copy()
            s["voice"] = voice
            out = folder / f"demo_{safe_name(voice)}.mp3"
            text = demo_text_for(voice)
            if s["engine"] == "google":
                await make_google(text, out, s)
            else:
                await make_edge(text, out, s)
            return out.read_bytes()
        except Exception as exc:
            log.warning("Demo failed for %s: %s", voice, exc)
            return None
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    tasks = [asyncio.create_task(one(v)) for v in voices]
    failed = []
    try:
        for voice, task in zip(voices, tasks):          # send in the listed order
            _, data = await task
            done += 1
            if data is None:
                failed.append(voice)
            else:
                await message.reply_audio(
                    audio=data, filename=f"demo_{safe_name(voice)}.mp3",
                    title=voice_label(voice)[:64], caption=f"🎙️ {voice_label(voice)}",
                )
            await safe_edit(status, f"🔊 Demos: {done}/{len(voices)}")
    finally:
        for t in tasks:
            t.cancel()
    summary = f"✅ Sent {len(voices) - len(failed)}/{len(voices)} demos."
    if failed:
        summary += "\n⚠ Failed: " + ", ".join(failed)
    await safe_edit(status, summary)


async def demo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    uid = update.effective_user.id
    if context.args:
        arg = context.args[0]
        if arg.lower() == "all":
            await demo_all(message, uid)
            return
        voice = resolve_voice(arg)
        if not voice:
            await message.reply_text("Unknown voice. Use /voices, /demo <name> or /demo all.")
            return
    else:
        voice = cfg(uid)["voice"]
    await send_demo(message, uid, voice)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    s = cfg(uid)
    data = q.data or ""
    msg = q.message

    async def edit(text, markup=None):
        try:
            await q.edit_message_text(text, reply_markup=markup)
        except TelegramError:
            pass

    if data == "menu:refresh":
        await edit(settings_text(uid), settings_keyboard())
    elif data == "menu:voice":
        await edit("Choose voice (🔊 Demo plays a sample):", voice_keyboard())
    elif data == "demo:all":
        if msg:
            await demo_all(msg, uid)
    elif data.startswith("demo:"):
        key = data.split(":", 1)[1]
        if key in VOICES and msg:
            await send_demo(msg, uid, VOICES[key][0])
    elif data == "menu:rate":
        kb = [
            [InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate:{v:.1f}") for v in (0.8, 1.0, 1.1)],
            [InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate:{v:.1f}") for v in (1.2, 1.3, 1.5)],
            [InlineKeyboardButton(f"{v:.1f}x", callback_data=f"rate:{v:.1f}") for v in (1.7, 1.8, 2.0)],
        ]
        await edit("Choose rate:", InlineKeyboardMarkup(kb))
    elif data == "menu:pitch":
        kb = [
            [InlineKeyboardButton(f"{v:+d} Hz", callback_data=f"pitch:{v}") for v in (-20, -10, -5, 0)],
            [InlineKeyboardButton(f"{v:+d} Hz", callback_data=f"pitch:{v}") for v in (5, 10, 15, 20)],
        ]
        await edit("Choose pitch:", InlineKeyboardMarkup(kb))
    elif data == "menu:engine":
        kb = [[InlineKeyboardButton("Edge TTS", callback_data="engine:edge")],
              [InlineKeyboardButton("Google TTS", callback_data="engine:google")]]
        await edit("Choose engine:", InlineKeyboardMarkup(kb))
    elif data == "menu:name":
        await edit("Set the filename in chat using:\n/name My Audiobook")
    elif data.startswith("voice:"):
        key = data.split(":", 1)[1]
        if key in VOICES:
            s["voice"] = VOICES[key][0]
            save_settings()
            await edit(f"Voice set: {VOICES[key][1]}")
    elif data.startswith("rate:"):
        try:
            s["rate"] = min(2.0, max(0.5, float(data.split(":", 1)[1])))
        except ValueError:
            return
        save_settings()
        await edit(f"Rate set to {s['rate']:.1f}x")
    elif data.startswith("pitch:"):
        try:
            s["pitch"] = min(20, max(-20, int(data.split(":", 1)[1])))
        except ValueError:
            return
        save_settings()
        await edit(f"Pitch set to {s['pitch']:+d} Hz")
    elif data.startswith("engine:"):
        engine = data.split(":", 1)[1]
        if engine in {"edge", "google"}:
            s["engine"] = engine
            save_settings()
            await edit(f"Engine set to {s['engine']}")


# ───────────────────────── file download (Local Bot API logic unchanged) ─────────────────────────

async def download_input_file(context, file_id, input_path):
    """Download input from Telegram, robustly resolving Local Bot API paths."""
    from urllib.parse import urlsplit, unquote

    tg_file = await context.bot.get_file(file_id)
    returned_path = str(tg_file.file_path or "")
    destination = Path(input_path)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if not LOCAL_BOT_API:
        await tg_file.download_to_drive(custom_path=str(destination))
        if not destination.is_file() or destination.stat().st_size == 0:
            raise RuntimeError("Telegram download returned an empty file.")
        return destination

    if not returned_path:
        raise FileNotFoundError("Local Bot API getFile returned an empty path.")

    candidates = []
    if os.path.isabs(returned_path):
        candidates.append(Path("/" + returned_path.lstrip("/")))
    else:
        parsed = urlsplit(returned_path)
        url_path = unquote(parsed.path) if parsed.scheme in {"http", "https"} else returned_path
        marker = "/file/bot"
        marker_pos = url_path.find(marker)
        if marker_pos >= 0:
            after_marker = url_path[marker_pos + len(marker):]
            slash_pos = after_marker.find("/")
            if slash_pos >= 0:
                candidates.append(Path("/" + after_marker[slash_pos:].lstrip("/")))
        elif url_path.startswith("/"):
            candidates.append(Path("/" + url_path.lstrip("/")))

    for source_path in candidates:
        try:
            if source_path.is_file():
                shutil.copyfile(source_path, destination)
                if destination.is_file() and destination.stat().st_size > 0:
                    log.info("Input copied from shared Local Bot API storage.")
                    return destination
        except OSError:
            pass

    async def run_docker(*args):
        """Run Docker without sudo first, then try sudo for runner permissions."""
        last_error = b""
        for prefix in (("docker",), ("sudo", "docker")):
            try:
                proc = await asyncio.create_subprocess_exec(
                    *prefix, *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                if proc.returncode == 0:
                    return stdout, b""
                last_error = stderr or stdout
            except FileNotFoundError as exc:
                last_error = str(exc).encode()
        return b"", last_error

    last_err = b""
    for source_path in candidates:
        _, last_err = await run_docker(
            "cp", f"{LOCAL_API_CONTAINER}:{source_path}", str(destination)
        )
        if destination.is_file() and destination.stat().st_size > 0:
            log.info("Input copied from Local Bot API container using returned path.")
            return destination
        destination.unlink(missing_ok=True)

    # Local Bot API can return only /documents/file_0.txt, although the file
    # is actually stored under /var/lib/telegram-bot-api/<token>/documents/.
    # Search inside the container rather than guessing that path.
    basename = Path(returned_path.split("?", 1)[0]).name
    if not basename or basename in {".", ".."}:
        raise RuntimeError("Local Bot API returned an invalid uploaded-file path.")

    # Run the search as root and restrict it to likely file-storage locations.
    # The Local Bot API container may not permit its default user to traverse
    # /proc, /sys, or protected directories; those permission errors must not
    # prevent locating the uploaded document in its data volume.
    search_script = (
        "for d in /var/lib/telegram-bot-api /documents /tmp; do "
        "if [ -d \"$d\" ]; then "
        "f=$(find \"$d\" -type f -name \"$1\" -print -quit 2>/dev/null); "
        "if [ -n \"$f\" ]; then printf '%s\\n' \"$f\"; exit 0; fi; "
        "fi; done; exit 1"
    )
    found, find_err = await run_docker(
        "exec", "-u", "0", LOCAL_API_CONTAINER,
        "sh", "-lc", search_script, "sh", basename
    )
    found_paths = []
    if found:
        for line in found.decode("utf-8", errors="replace").splitlines():
            path = line.strip()
            if path.startswith("/") and path not in found_paths:
                found_paths.append(path)

    copy_errors = []
    for actual_path in found_paths:
        _, copy_err = await run_docker(
            "cp", f"{LOCAL_API_CONTAINER}:{actual_path}", str(destination)
        )
        if destination.is_file() and destination.stat().st_size > 0:
            log.info("Input copied from Local Bot API container after path lookup.")
            return destination
        destination.unlink(missing_ok=True)
        if copy_err:
            copy_errors.append(copy_err.decode("utf-8", errors="replace"))

    details = copy_errors[-1] if copy_errors else last_err.decode("utf-8", errors="replace")
    details = details[-500:].replace(BOT_TOKEN, "[REDACTED]")
    if find_err:
        details = (details + " " + find_err.decode("utf-8", errors="replace")[-250:])
    raise RuntimeError(
        f"Could not locate uploaded file '{basename}' inside Local Bot API container "
        f"'{LOCAL_API_CONTAINER}'. The returned path and container search both failed. {details}"
    )


# ───────────────────────── conversion job ─────────────────────────

async def send_skipped_report(message, uid, duration):
    """After the mp3 is sent, list every fragment that could not be spoken."""
    info = SKIPPED.get(uid) or {}
    items = sorted(info.get("items", []), key=lambda x: x["chunk"])
    if not items:
        return
    total = max(1, info.get("total", 1))
    lines = [f"⚠ {len(items)} fragment(s) were NOT spoken (0.4 s silence in their place).",
             "Time is approximate position in the audio.\n"]
    for n, item in enumerate(items, 1):
        pos = fmt_time((item["chunk"] - 1) / total * duration) if item["chunk"] else "?"
        shown = item["text"] if item["text"].strip() else "(blank / invisible characters)"
        shown = " ".join(shown.split())[:150]
        lines.append(f"{n}. ~{pos} · chunk {item['chunk']}/{total}\n   {shown}")
    report = "\n".join(lines)
    try:
        if len(report) <= 3800:
            await message.reply_text(report)
        else:
            await message.reply_document(
                document=report.encode("utf-8"), filename="skipped_fragments.txt",
                caption=f"⚠ {len(items)} fragment(s) were not spoken — full list attached.")
    except TelegramError:
        log.exception("Could not send skipped-fragment report")


async def run_job(update, context, filename=None, text=None, settings=None):
    """Convert an uploaded file (filename) or pasted text (text) into audio."""
    message = update.effective_message
    uid = update.effective_user.id
    folder = Path(tempfile.mkdtemp(prefix=f"user-{uid}-", dir=WORK_ROOT))
    started = time.monotonic()
    status = await message.reply_text("📥 Preparing…\n⏱ Used time: 0m 00s")
    try:
        if text is None:
            await safe_edit(status, "📥 Downloading file…\n⏱ Used time: 0m 00s")
            input_path = folder / Path(filename).name
            await download_input_file(context, message.document.file_id, input_path)
            await safe_edit(status, progress_text(
                "📖 Extracting text", 0, 1, started, f"📄 Input size: {fmt_size(input_path.stat().st_size)}"))
            text = await asyncio.to_thread(extract_text, input_path)   # don't block the event loop
            input_path.unlink(missing_ok=True)
            display_name = Path(filename).stem
        else:
            display_name = "Text"
        if not text.strip():
            raise ValueError("No readable text found; scanned PDFs need OCR.")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"Text exceeds {MAX_TEXT_CHARS:,} characters.")

        s = dict(settings) if settings else cfg(uid).copy()
        merged = await generate_audio(text, folder, s, status, uid, started)
        duration, size_bytes = await audio_metadata(merged)
        await safe_edit(status, progress_text(
            "✂️ Preparing audio", 1, 1, started,
            f"🎧 Audio duration: {fmt_time(duration)}\n📦 File size: {fmt_size(size_bytes)}\n🎙️ Audio made: 1 merged file"))
        parts = await split_audio(merged, folder)
        base = safe_name(s["name"] if s["name"] != "Audiobook" else display_name)
        PROGRESS[uid] = {"stage": "📤 Sending audio", "done": 0, "total": len(parts), "started": started}
        for i, part in enumerate(parts, 1):
            if part.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                raise RuntimeError("Part exceeds MAX_UPLOAD_MB; lower SEND_PART_MB.")
            outname = f"{base}.mp3" if len(parts) == 1 else f"{base}_part_{i:03d}.mp3"
            try:
                part_duration, part_size = await audio_metadata(part)
            except Exception:
                part_duration, part_size = 0, part.stat().st_size
            details = ((f"🎧 Audio duration: {fmt_time(part_duration)}\n" if part_duration else "")
                       + f"📦 File size: {fmt_size(part_size)}\n🎙️ Audio made: {i}/{len(parts)} files")
            await safe_edit(status, progress_text("📤 Sending", i - 1, len(parts), started, details))
            with part.open("rb") as audio:
                await context.bot.send_audio(
                    chat_id=update.effective_chat.id, audio=audio,
                    filename=outname, title=Path(outname).stem[:64],
                    duration=int(part_duration) or None,
                    read_timeout=600, write_timeout=600, connect_timeout=60, pool_timeout=60,
                )
            if part != merged:
                part.unlink(missing_ok=True)
            PROGRESS[uid] = {"stage": "📤 Sending audio", "done": i, "total": len(parts), "started": started}
        elapsed = time.monotonic() - started
        await safe_edit(status,
            f"✅ Conversion complete!\n{progress_bar(len(parts), len(parts))} 100%\n"
            f"⏱ Used time: {fmt_time(elapsed)}\n"
            f"🎧 Audio duration: {fmt_time(duration)}\n"
            f"📦 Total audio size: {fmt_size(size_bytes)}\n"
            f"🎙️ Audio made: {len(parts)} file(s) sent."
            + (f"\n⚠ {len(SKIPPED[uid]['items'])} unspeakable fragment(s) were skipped (list below)."
               if SKIPPED.get(uid, {}).get("items") else "")
        )
        await send_skipped_report(message, uid, duration)
    except asyncio.CancelledError:
        await safe_edit(status, "⛔ Conversion cancelled; clearing temporary files.")
        raise
    except Exception as exc:
        log.exception("Conversion failed")
        err = str(exc).replace(BOT_TOKEN, "[REDACTED]")[:700]
        err_text = f"❌ Conversion failed: {type(exc).__name__}: {err}\n⏱ Used time: {fmt_time(time.monotonic() - started)}"
        try:
            await status.edit_text(err_text)
        except TelegramError:
            try:
                await message.reply_text(err_text)
            except TelegramError:
                pass
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        PROGRESS.pop(uid, None)
        SKIPPED.pop(uid, None)


# ───────────────────────── per-user job queue ─────────────────────────

def queue_size(uid):
    """Running job (if any) + waiting jobs."""
    return len(QUEUES.get(uid, [])) + (1 if uid in CURRENT else 0)


async def queue_worker(uid):
    """Process this user's jobs one at a time, in order."""
    try:
        while QUEUES.get(uid):
            job = QUEUES[uid].pop(0)
            CURRENT[uid] = job
            task = asyncio.create_task(run_job(
                job["update"], job["context"], filename=job["filename"],
                text=job["text"], settings=job["settings"]))
            TASKS[uid] = task
            try:
                await asyncio.wait({task})                  # doesn't raise if the job is cancelled
            except asyncio.CancelledError:
                task.cancel()
                raise
            finally:
                TASKS.pop(uid, None)
                CURRENT.pop(uid, None)
            if not task.cancelled() and task.exception():
                log.error("Job crashed", exc_info=task.exception())
    finally:
        if WORKERS.get(uid) is asyncio.current_task():
            WORKERS.pop(uid, None)
        if not QUEUES.get(uid):
            QUEUES.pop(uid, None)


async def enqueue(update, context, label, filename=None, text=None):
    message = update.effective_message
    uid = update.effective_user.id
    if queue_size(uid) >= MAX_QUEUE:
        await message.reply_text(f"📋 Queue is full ({MAX_QUEUE} jobs). Wait for one to finish, or use /queue and /clearqueue.")
        return
    job = {
        "update": update, "context": context, "filename": filename, "text": text,
        "settings": cfg(uid).copy(),                         # voice/rate/etc. as set when queued
        "label": label, "mid": message.message_id,
    }
    pending = QUEUES.setdefault(uid, [])
    # Files sent together can be handled out of order; keep them in message order.
    pos = len(pending)
    while pos > 0 and pending[pos - 1]["mid"] > job["mid"]:
        pos -= 1
    pending.insert(pos, job)
    number = pos + 1 + (1 if uid in CURRENT else 0)
    if number == 1:
        await message.reply_text(f"📋 Added: {label}\nStarting now.")
    else:
        await message.reply_text(f"📋 Queued #{number} of {queue_size(uid)}: {label}\nUse /queue to see the list.")
    worker = WORKERS.get(uid)
    if not worker or worker.done():
        WORKERS[uid] = asyncio.create_task(queue_worker(uid))


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    filename = message.document.file_name or "input.txt"
    if Path(filename).suffix.lower() not in SUPPORTED_EXT:
        await message.reply_text(f"{filename}: unsupported. Supported: TXT, MD, PDF, DOCX, EPUB.")
        return
    if message.document.file_size and message.document.file_size > MAX_INPUT_MB * 1024 * 1024:
        await message.reply_text(f"{filename}: exceeds the {MAX_INPUT_MB} MB limit.")
        return
    await enqueue(update, context, filename[:60], filename=filename)


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    text = message.text or ""
    if not re.search(r"\w", text):
        return
    snippet = " ".join(text.split())[:30]
    await enqueue(update, context, f"Text “{snippet}…”", text=text)


async def queue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    running = CURRENT.get(uid)
    pending = QUEUES.get(uid, [])
    if not running and not pending:
        await update.effective_message.reply_text("Queue is empty. Send up to "
                                                  f"{MAX_QUEUE} files and they will be converted one by one.")
        return
    lines = [f"📋 Queue ({queue_size(uid)}/{MAX_QUEUE})"]
    n = 1
    if running:
        lines.append(f"{n}. ▶️ {running['label']} — running")
        n += 1
    for job in pending:
        lines.append(f"{n}. ⏳ {job['label']}")
        n += 1
    lines.append("\n/cancel — stop the running job (next one starts)\n/clearqueue — remove waiting jobs\n/cancelall — stop everything")
    await update.effective_message.reply_text("\n".join(lines))


async def clearqueue_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    count = len(QUEUES.get(uid, []))
    if count:
        QUEUES[uid].clear()
    await update.effective_message.reply_text(f"🧹 Removed {count} waiting job(s)." if count else "No waiting jobs.")


async def cancelall_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    count = len(QUEUES.get(uid, []))
    if count:
        QUEUES[uid].clear()
    task = TASKS.get(uid)
    if task and not task.done():
        task.cancel()
    await update.effective_message.reply_text(f"⛔ Cancelled the running job and removed {count} waiting job(s).")


async def error_handler(update, context):
    log.error("Unhandled bot error", exc_info=context.error)


def cleanup_stale_workdirs():
    """Remove leftovers from a previous crashed run."""
    for old in WORK_ROOT.glob("*"):
        shutil.rmtree(old, ignore_errors=True)


def main():
    load_settings()
    cleanup_stale_workdirs()
    builder = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    if LOCAL_BOT_API:
        builder = (
            builder
            .base_url(f"{LOCAL_BOT_API_URL}/bot")
            .base_file_url(f"{LOCAL_BOT_API_URL}/file/bot")
            .local_mode(True)
        )
        log.info("Using Local Bot API at %s", LOCAL_BOT_API_URL)

    app = builder.build()
    app.add_handler(TypeHandler(Update, gate), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("settings", settings_cmd))
    app.add_handler(CommandHandler("voice", voice_cmd))
    app.add_handler(CommandHandler("voices", voices_cmd))
    app.add_handler(CommandHandler("rate", rate_cmd))
    app.add_handler(CommandHandler("speed", rate_cmd))
    app.add_handler(CommandHandler("pitch", pitch_cmd))
    app.add_handler(CommandHandler("engine", engine_cmd))
    app.add_handler(CommandHandler("name", name_cmd))
    app.add_handler(CommandHandler("demo", demo_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("queue", queue_cmd))
    app.add_handler(CommandHandler("clearqueue", clearqueue_cmd))
    app.add_handler(CommandHandler("cancelall", cancelall_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(error_handler)

    log.info("Bot starting; local API=%s; target part size=%s MB; workers=%s",
             LOCAL_BOT_API, SEND_PART_MB, TTS_CONCURRENCY)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
