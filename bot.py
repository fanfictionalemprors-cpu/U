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
# Edge TTS splits anything above 4096 bytes into serial requests. Devanagari = 3 bytes/char,
# so 1300 chars (~3900 bytes) is one request per chunk -> much better parallelism.
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1300"))
TTS_CONCURRENCY = max(1, min(16, int(os.getenv("TTS_CONCURRENCY", "8"))))
MAX_QUEUE = max(1, int(os.getenv("MAX_QUEUE", "8")))
PROGRESS_EDIT_SECONDS = float(os.getenv("PROGRESS_EDIT_SECONDS", "3"))
WORK_ROOT = Path(os.getenv("TTS_WORKDIR", "/tmp/telegram-tts-work"))
WORK_ROOT.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = Path(os.getenv("SETTINGS_FILE", str(Path(__file__).resolve().parent / "bot_data" / "settings.json")))
ALLOWED_USERS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_USERS", ""))}

DEFAULT_VOICE = os.getenv("DEFAULT_VOICE", "hi-IN-MadhurNeural")   # Hindi by default
GTTS_SAMPLE_RATE = 24000          # gTTS mp3 output is 24 kHz
EDGE_BYTES_PER_SEC = 6000         # Edge mp3 is 48 kbps -> used for cheap duration estimates
SUPPORTED_EXT = {".txt", ".text", ".md", ".pdf", ".docx", ".epub"}

# ───────────────────────── languages & voices ─────────────────────────

# code -> (flag, English name)
LANG_NAMES = {
    "hi": ("🇮🇳", "Hindi"), "en": ("🇺🇸", "English"), "multi": ("🌐", "Multilingual"),
    "bn": ("🇮🇳", "Bengali"), "ta": ("🇮🇳", "Tamil"), "te": ("🇮🇳", "Telugu"),
    "mr": ("🇮🇳", "Marathi"), "gu": ("🇮🇳", "Gujarati"), "kn": ("🇮🇳", "Kannada"),
    "ml": ("🇮🇳", "Malayalam"), "ur": ("🇮🇳", "Urdu"), "ne": ("🇳🇵", "Nepali"),
    "fr": ("🇫🇷", "French"), "es": ("🇪🇸", "Spanish"), "de": ("🇩🇪", "German"),
    "pt": ("🇧🇷", "Portuguese"), "it": ("🇮🇹", "Italian"), "ru": ("🇷🇺", "Russian"),
    "ja": ("🇯🇵", "Japanese"), "zh": ("🇨🇳", "Chinese"), "ko": ("🇰🇷", "Korean"),
    "ar": ("🇸🇦", "Arabic"),
}
LANG_ORDER = list(LANG_NAMES)
REGION_FLAGS = {
    "IN": "🇮🇳", "US": "🇺🇸", "GB": "🇬🇧", "AU": "🇦🇺", "NP": "🇳🇵", "FR": "🇫🇷", "ES": "🇪🇸",
    "DE": "🇩🇪", "BR": "🇧🇷", "IT": "🇮🇹", "RU": "🇷🇺", "JP": "🇯🇵", "CN": "🇨🇳", "KR": "🇰🇷", "SA": "🇸🇦",
}

# key, Edge voice, gender, display name
_VOICE_TABLE = [
    # Hindi
    ("madhur", "hi-IN-MadhurNeural", "M", "Madhur"),
    ("swara", "hi-IN-SwaraNeural", "F", "Swara"),
    # English
    ("guy", "en-US-GuyNeural", "M", "Guy"),
    ("jenny", "en-US-JennyNeural", "F", "Jenny"),
    ("aria", "en-US-AriaNeural", "F", "Aria"),
    ("ava", "en-US-AvaNeural", "F", "Ava"),
    ("emma", "en-US-EmmaNeural", "F", "Emma"),
    ("michelle", "en-US-MichelleNeural", "F", "Michelle"),
    ("andrew", "en-US-AndrewNeural", "M", "Andrew"),
    ("brian", "en-US-BrianNeural", "M", "Brian"),
    ("christopher", "en-US-ChristopherNeural", "M", "Christopher"),
    ("roger", "en-US-RogerNeural", "M", "Roger"),
    ("ryan", "en-GB-RyanNeural", "M", "Ryan"),
    ("thomas", "en-GB-ThomasNeural", "M", "Thomas"),
    ("sonia", "en-GB-SoniaNeural", "F", "Sonia"),
    ("libby", "en-GB-LibbyNeural", "F", "Libby"),
    ("neerja", "en-IN-NeerjaNeural", "F", "Neerja"),
    ("prabhat", "en-IN-PrabhatNeural", "M", "Prabhat"),
    ("natasha", "en-AU-NatashaNeural", "F", "Natasha"),
    ("william", "en-AU-WilliamNeural", "M", "William"),
    # Multilingual (read mixed-language text far better)
    ("avam", "en-US-AvaMultilingualNeural", "F", "Ava"),
    ("andrewm", "en-US-AndrewMultilingualNeural", "M", "Andrew"),
    ("emmam", "en-US-EmmaMultilingualNeural", "F", "Emma"),
    ("brianm", "en-US-BrianMultilingualNeural", "M", "Brian"),
    ("vivienne", "fr-FR-VivienneMultilingualNeural", "F", "Vivienne"),
    ("remy", "fr-FR-RemyMultilingualNeural", "M", "Remy"),
    ("seraphina", "de-DE-SeraphinaMultilingualNeural", "F", "Seraphina"),
    ("florian", "de-DE-FlorianMultilingualNeural", "M", "Florian"),
    # Other Indian languages
    ("bashkar", "bn-IN-BashkarNeural", "M", "Bashkar"),
    ("tanishaa", "bn-IN-TanishaaNeural", "F", "Tanishaa"),
    ("pallavi", "ta-IN-PallaviNeural", "F", "Pallavi"),
    ("valluvar", "ta-IN-ValluvarNeural", "M", "Valluvar"),
    ("mohan", "te-IN-MohanNeural", "M", "Mohan"),
    ("shruti", "te-IN-ShrutiNeural", "F", "Shruti"),
    ("aarohi", "mr-IN-AarohiNeural", "F", "Aarohi"),
    ("manohar", "mr-IN-ManoharNeural", "M", "Manohar"),
    ("dhwani", "gu-IN-DhwaniNeural", "F", "Dhwani"),
    ("niranjan", "gu-IN-NiranjanNeural", "M", "Niranjan"),
    ("sapna", "kn-IN-SapnaNeural", "F", "Sapna"),
    ("gagan", "kn-IN-GaganNeural", "M", "Gagan"),
    ("sobhana", "ml-IN-SobhanaNeural", "F", "Sobhana"),
    ("midhun", "ml-IN-MidhunNeural", "M", "Midhun"),
    ("gul", "ur-IN-GulNeural", "F", "Gul"),
    ("salman", "ur-IN-SalmanNeural", "M", "Salman"),
    ("hemkala", "ne-NP-HemkalaNeural", "F", "Hemkala"),
    ("sagar", "ne-NP-SagarNeural", "M", "Sagar"),
    # World languages
    ("denise", "fr-FR-DeniseNeural", "F", "Denise"),
    ("henri", "fr-FR-HenriNeural", "M", "Henri"),
    ("elvira", "es-ES-ElviraNeural", "F", "Elvira"),
    ("alvaro", "es-ES-AlvaroNeural", "M", "Alvaro"),
    ("katja", "de-DE-KatjaNeural", "F", "Katja"),
    ("conrad", "de-DE-ConradNeural", "M", "Conrad"),
    ("francisca", "pt-BR-FranciscaNeural", "F", "Francisca"),
    ("antonio", "pt-BR-AntonioNeural", "M", "Antonio"),
    ("elsa", "it-IT-ElsaNeural", "F", "Elsa"),
    ("diego", "it-IT-DiegoNeural", "M", "Diego"),
    ("svetlana", "ru-RU-SvetlanaNeural", "F", "Svetlana"),
    ("dmitry", "ru-RU-DmitryNeural", "M", "Dmitry"),
    ("nanami", "ja-JP-NanamiNeural", "F", "Nanami"),
    ("keita", "ja-JP-KeitaNeural", "M", "Keita"),
    ("xiaoxiao", "zh-CN-XiaoxiaoNeural", "F", "Xiaoxiao"),
    ("yunxi", "zh-CN-YunxiNeural", "M", "Yunxi"),
    ("sunhi", "ko-KR-SunHiNeural", "F", "SunHi"),
    ("injoon", "ko-KR-InJoonNeural", "M", "InJoon"),
    ("zariyah", "ar-SA-ZariyahNeural", "F", "Zariyah"),
    ("hamed", "ar-SA-HamedNeural", "M", "Hamed"),
]
VOICE_PATTERN = re.compile(r"^[a-z]{2,3}-[A-Z]{2}-[A-Za-z]+Neural$")


def lang_of(voice):
    """'hi-IN-MadhurNeural' -> 'hi'; any *MultilingualNeural voice -> 'multi'."""
    if "Multilingual" in voice:
        return "multi"
    return voice.split("-", 1)[0].lower()


def _build_voices():
    voices, genders = {}, {}
    for key, name, gender, short in _VOICE_TABLE:
        lang = lang_of(name)
        parts = name.split("-")
        flag = "🌐" if lang == "multi" else REGION_FLAGS.get(parts[1] if len(parts) > 1 else "", "🌐")
        label = f"{flag} {LANG_NAMES.get(lang, ('', lang))[1]} {'Female' if gender == 'F' else 'Male'} — {short}"
        voices[key] = (name, label)
        genders[name] = gender
    return voices, genders


VOICES, VOICE_GENDER = _build_voices()
# Indian English is the natural companion of a Hindi voice, so prefer it over US English.
LANG_PREF = {("en", "M"): "en-IN-PrabhatNeural", ("en", "F"): "en-IN-NeerjaNeural"}


def gender_of(voice):
    return VOICE_GENDER.get(voice, "M")


def voice_for_lang(lang, gender="M"):
    pref = LANG_PREF.get((lang, gender))
    if pref:
        return pref
    first_any = None
    for name, _ in VOICES.values():
        if lang_of(name) != lang:
            continue
        if VOICE_GENDER[name] == gender:
            return name
        first_any = first_any or name
    return first_any


def voices_of_lang(lang):
    return [(k, n, l) for k, (n, l) in VOICES.items() if lang_of(n) == lang]


def voice_label(voice):
    return next((v[1] for v in VOICES.values() if v[0] == voice), voice)


def resolve_voice(arg):
    key = arg.lower()
    if key in VOICES:
        return VOICES[key][0]
    if VOICE_PATTERN.match(arg):
        return arg
    return None


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
    "ne": "नमस्ते! म तपाईंको अडियोबुक पढ्न तयार छु। यो मेरो आवाजको छोटो डेमो हो।",
    "fr": "Bonjour ! Je suis prêt à lire votre livre audio. Voici un court aperçu de ma voix.",
    "es": "¡Hola! Estoy listo para leer tu audiolibro. Esta es una breve demostración de mi voz.",
    "de": "Hallo! Ich bin bereit, Ihr Hörbuch vorzulesen. Das ist eine kurze Hörprobe meiner Stimme.",
    "pt": "Olá! Estou pronto para ler o seu audiolivro. Esta é uma breve demonstração da minha voz.",
    "it": "Ciao! Sono pronto a leggere il tuo audiolibro. Questa è una breve dimostrazione della mia voce.",
    "ru": "Здравствуйте! Я готов прочитать вашу аудиокнигу. Это короткая демонстрация моего голоса.",
    "ja": "こんにちは！あなたのオーディオブックを読む準備ができています。これは私の声の短いデモです。",
    "zh": "你好！我已经准备好为你朗读有声书了。这是我声音的简短演示。",
    "ko": "안녕하세요! 오디오북을 읽을 준비가 되었습니다. 제 목소리의 짧은 데모입니다.",
    "ar": "مرحبًا! أنا جاهز لقراءة كتابك الصوتي. هذا عرض قصير لصوتي.",
}
MULTI_DEMO = "नमस्ते! Hello! I can read Hindi and English together. यह मेरी आवाज़ का डेमो है।"


def demo_text_for(voice):
    lang = lang_of(voice)
    if lang == "multi":
        return MULTI_DEMO
    return DEMO_TEXT.get(lang, DEMO_TEXT["en"])


# ── script detection (auto language) ──
_SCRIPT_RANGES = [
    ("deva", 0x0900, 0x097F), ("beng", 0x0980, 0x09FF), ("gujr", 0x0A80, 0x0AFF),
    ("taml", 0x0B80, 0x0BFF), ("telu", 0x0C00, 0x0C7F), ("knda", 0x0C80, 0x0CFF),
    ("mlym", 0x0D00, 0x0D7F), ("arab", 0x0600, 0x06FF), ("cyrl", 0x0400, 0x04FF),
    ("cjk", 0x3040, 0x30FF), ("cjk", 0x4E00, 0x9FFF), ("hang", 0xAC00, 0xD7AF),
]
# script -> (languages whose voices can read it, language to switch to)
_SCRIPT_LANGS = {
    "deva": ({"hi", "mr", "ne"}, "hi"), "beng": ({"bn"}, "bn"), "gujr": ({"gu"}, "gu"),
    "taml": ({"ta"}, "ta"), "telu": ({"te"}, "te"), "knda": ({"kn"}, "kn"),
    "mlym": ({"ml"}, "ml"), "arab": ({"ar", "ur"}, "ur"), "cyrl": ({"ru"}, "ru"),
    "cjk": ({"zh", "ja"}, "zh"), "hang": ({"ko"}, "ko"),
    "latn": ({"en", "fr", "es", "de", "pt", "it"}, "en"),
}


def auto_voice(text, voice, threshold=0.7):
    """If a chunk is clearly written in a script the current voice can't read
    (e.g. an English paragraph inside a Hindi book), pick a matching voice with the
    same gender. Mixed chunks and Multilingual voices are left untouched."""
    if "Multilingual" in voice:
        return voice
    counts, kana, total = {}, 0, 0
    for ch in text[:1500]:
        if not ch.isalpha():
            continue
        cp = ord(ch)
        total += 1
        if cp < 0x250:
            counts["latn"] = counts.get("latn", 0) + 1
            continue
        for name, lo, hi in _SCRIPT_RANGES:
            if lo <= cp <= hi:
                counts[name] = counts.get(name, 0) + 1
                if 0x3040 <= cp <= 0x30FF:
                    kana += 1
                break
    if not total or not counts:
        return voice
    script, n = max(counts.items(), key=lambda kv: kv[1])
    if n / total < threshold:
        return voice
    compatible, target = _SCRIPT_LANGS[script]
    if lang_of(voice) in compatible:
        return voice
    if script == "cjk" and kana:
        target = "ja"
    return voice_for_lang(target, gender_of(voice)) or voice


SETTINGS = {}
TASKS = {}
PROGRESS = {}
SKIPPED = {}
LANGSTAT = {}   # uid -> {lang: chunks}
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
    s.setdefault("voice", DEFAULT_VOICE)
    s.setdefault("rate", float(os.getenv("DEFAULT_RATE", "1.1")))
    s.setdefault("pitch", int(os.getenv("DEFAULT_PITCH", "0")))
    s.setdefault("engine", os.getenv("DEFAULT_ENGINE", "edge").lower())
    s.setdefault("name", "Audiobook")
    s.setdefault("auto", os.getenv("DEFAULT_AUTO", "1").lower() in {"1", "true", "yes", "on"})
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


def _mostly_ascii(seg):
    return bool(seg) and sum(1 for c in seg if c < "\x80") > 0.85 * len(seg)


def split_text(text, limit=CHUNK_CHARS):
    """Index-based splitter (linear time) that keeps sentence punctuation.
    ASCII-heavy (Latin) text takes 1 byte/char, so those chunks are made 2x longer."""
    text = clean_text(text)
    n = len(text)
    i = 0
    chunks = []
    groups = (("\n",), ("। ", ". ", "! ", "? ", "।\n", ".\n"), ("; ", ", ", " "))
    while i < n:
        lim = limit * 2 if _mostly_ascii(text[i:i + limit]) else limit
        end = min(n, i + lim)
        cut = end
        if end < n:
            window = text[i:end]
            for group in groups:
                best = -1
                for sep in group:
                    p = window.rfind(sep)
                    if p >= lim // 2:
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


def devanagari_ratio(text):
    letters = [c for c in text if c.isalpha()]
    return sum(1 for c in letters if "ऀ" <= c <= "ॿ") / len(letters) if letters else 0.0


def google_lang(voice):
    lang = lang_of(voice)
    if lang == "multi":
        lang = "en"
    parts = voice.split("-")
    region = parts[1] if len(parts) > 1 else ""
    tld = {"IN": "co.in", "GB": "co.uk", "AU": "com.au"}.get(region, "com")
    if lang == "zh":
        lang = "zh-CN"
    return lang, tld


async def make_google(text, path, s):
    lang, tld = google_lang(s["voice"])
    if lang_of(s["voice"]) == "multi" and devanagari_ratio(text) > 0.5:
        lang, tld = "hi", "co.in"
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


def split_half(text):
    """Split near the middle at a sentence end or a space — never inside a word
    (cutting Hindi mid-word leaves broken letters that Edge refuses to read)."""
    n = len(text)
    mid = n // 2
    best = None
    for seps in (("।", ".", "!", "?", "\n"), (" ",)):
        cands = []
        for sep in seps:
            for p in (text.rfind(sep, 0, mid + 1), text.find(sep, mid)):
                if p > 0:
                    cands.append(p + len(sep))
        cands = [c for c in cands if n // 5 <= c <= n * 4 // 5]
        if cands:
            best = min(cands, key=lambda c: abs(c - mid))
            break
    if best is None:
        return None
    a, b = text[:best].strip(), text[best:].strip()
    return (a, b) if a and b else None


async def edge_robust(text, out, folder, tag, s, skipped, depth=0):
    """Speak `text` with Edge. If Edge returns no audio: retry, split at a sentence/word
    boundary and retry the halves, and as a last resort read the fragment with Google TTS.
    Silence is used only if every engine fails, so no story text is silently dropped."""
    try:
        await make_edge(text, out, s, attempts=2 if depth else 3)
        return
    except Exception as exc:
        halves = split_half(text) if (len(text) > 120 and depth < 5) else None
        if halves:
            parts = list(halves)
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
        try:
            chunk_no = int(tag[1:7])
        except ValueError:
            chunk_no = 0
        # Edge refuses this fragment: read it with Google TTS instead of dropping it.
        try:
            gs = dict(s)
            if devanagari_ratio(text) > 0.5:
                gs["voice"] = "hi-IN-MadhurNeural"      # only used to pick Google's language
            await make_google(text, out, gs)
            if out.is_file() and out.stat().st_size > 256:
                log.warning("Edge failed (%s); fragment read with Google TTS: %r", type(exc).__name__, text[:60])
                skipped.append({"chunk": chunk_no, "text": text[:200], "mode": "google"})
                return
        except Exception as gexc:
            log.warning("Google fallback failed too: %s", gexc)
        log.warning("Skipping unspeakable fragment (%s): %r", type(exc).__name__, text[:80])
        skipped.append({"chunk": chunk_no, "text": text[:200], "mode": "silent"})
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
    langstat = {}
    total = len(chunks)
    SKIPPED[uid] = {"items": skipped, "total": total}
    LANGSTAT[uid] = langstat
    status_lock = asyncio.Lock()
    PROGRESS[uid] = {
        "stage": "Generating audio", "done": 0, "total": total, "started": started,
        "audio_duration": 0.0, "audio_bytes": 0, "concurrency": TTS_CONCURRENCY,
    }

    async def generate_one(index, chunk):
        out = pieces[index]
        cs = s
        if s.get("auto"):
            v = auto_voice(chunk, s["voice"])
            if v != s["voice"]:
                cs = {**s, "voice": v}
        lang = lang_of(cs["voice"])
        langstat[lang] = langstat.get(lang, 0) + 1
        async with semaphore:
            if cs["engine"] == "google":
                await make_google(chunk, out, cs)
            else:
                await edge_robust(chunk, out, folder, f"c{index + 1:06d}", cs, skipped)

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

    tasks = [asyncio.create_task(generate_one(i, chunk)) for i, chunk in enumerate(chunks)]
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
        "🎧 TTS Bot ready! (default: Hindi — Madhur)\n"
        "Send TXT, MD, PDF, DOCX or EPUB — or just type/paste text.\n"
        "Use /settings for voice, language, rate, pitch and filename, or /voices to see every voice."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "/start — Start\n/help — Help\n/settings — Settings menu\n"
        "/lang [code] — Pick language (e.g. /lang hi, /lang en, /lang fr)\n"
        "/auto on|off — Auto-switch voice when a paragraph is in another language\n"
        "/voice <name> — Set voice (or /voice for a menu)\n/voices [lang] — List voices (e.g. /voices hi)\n"
        "/rate 1.5 — Rate (0.5–2.0)\n/speed 1.5 — Rate alias\n"
        "/pitch 5 — Pitch (-20 to +20 Hz)\n/engine edge|google — Engine\n"
        "/name My Audiobook — Output filename\n/demo [voice|lang|all] — Voice demo\n"
        "/status — Progress\n/queue — Show queue\n/cancel — Cancel running job\n"
        "/clearqueue — Remove waiting jobs\n/cancelall — Cancel everything\n\n"
        f"Send a file, or plain text, to convert it. You can send up to {MAX_QUEUE} files at once; "
        "they are converted one after another."
    )


async def voices_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    only = context.args[0].lower() if context.args else None
    if only and only not in LANG_NAMES:
        await update.effective_message.reply_text(
            "Unknown language. Codes: " + ", ".join(LANG_ORDER))
        return
    lines = []
    for lang in LANG_ORDER:
        if only and lang != only:
            continue
        vs = voices_of_lang(lang)
        if not vs:
            continue
        flag, name = LANG_NAMES[lang]
        lines.append(f"\n{flag} {name} ({lang})")
        lines += [f"/voice {k} — {l}" for k, _, l in vs]
    text = ("🎙️ Available voices" + "\n".join([""] + lines)
            + "\n\nHear one: /demo <name> · a whole language: /demo hi · all: /demo all"
            + "\nAny full Edge name also works, e.g. /voice fr-CA-SylvieNeural")
    for i in range(0, len(text), 3900):                # Telegram limit is 4096 chars
        await update.effective_message.reply_text(text[i:i + 3900])


def settings_text(uid):
    s = cfg(uid)
    return (
        "⚙️ TTS Settings\n\n"
        f"Voice: {voice_label(s['voice'])}\nRate: {s['rate']:.2f}x\nPitch: {s['pitch']:+d} Hz\n"
        f"Engine: {s['engine']}\nAuto language switch: {'ON' if s['auto'] else 'OFF'}\n"
        f"Audio name: {s['name']}"
    )


def settings_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Voice / Language", callback_data="menu:voice"),
         InlineKeyboardButton("🔀 Auto language", callback_data="auto:toggle")],
        [InlineKeyboardButton("Rate", callback_data="menu:rate"),
         InlineKeyboardButton("Pitch", callback_data="menu:pitch")],
        [InlineKeyboardButton("Engine", callback_data="menu:engine"),
         InlineKeyboardButton("Audio name help", callback_data="menu:name")],
        [InlineKeyboardButton("Refresh", callback_data="menu:refresh")],
    ])


def lang_keyboard():
    buttons = [InlineKeyboardButton(f"{LANG_NAMES[l][0]} {LANG_NAMES[l][1]}", callback_data=f"vl:{l}")
               for l in LANG_ORDER]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton("⬅ Back", callback_data="menu:refresh")])
    return InlineKeyboardMarkup(rows)


def voice_keyboard(lang):
    # One row per voice: [select voice] [🔊 hear demo]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"voice:{key}"),
         InlineKeyboardButton("🔊 Demo", callback_data=f"demo:{key}")]
        for key, _, label in voices_of_lang(lang)
    ]
    rows.append([InlineKeyboardButton(f"🔊 Demo all {LANG_NAMES[lang][1]}", callback_data=f"demol:{lang}")])
    rows.append([InlineKeyboardButton("⬅ Languages", callback_data="menu:voice")])
    return InlineKeyboardMarkup(rows)


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        settings_text(update.effective_user.id), reply_markup=settings_keyboard()
    )


async def lang_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = cfg(uid)
    if not context.args:
        await update.effective_message.reply_text(
            "🌐 Choose language (or /lang hi, /lang en …):", reply_markup=lang_keyboard())
        return
    code = context.args[0].lower()
    if code not in LANG_NAMES:
        await update.effective_message.reply_text("Unknown language. Codes: " + ", ".join(LANG_ORDER))
        return
    voice = voice_for_lang(code, gender_of(s["voice"]))
    if not voice:
        await update.effective_message.reply_text("No voice available for that language.")
        return
    s["voice"] = voice
    save_settings()
    await update.effective_message.reply_text(f"Voice set: {voice_label(voice)}\nChange it with /voices {code}")


async def auto_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = cfg(update.effective_user.id)
    if context.args:
        v = context.args[0].lower()
        if v in {"on", "1", "yes"}:
            s["auto"] = True
        elif v in {"off", "0", "no"}:
            s["auto"] = False
        else:
            await update.effective_message.reply_text("Use /auto on or /auto off.")
            return
        save_settings()
    await update.effective_message.reply_text(
        f"Auto language switch: {'ON' if s['auto'] else 'OFF'}\n"
        "ON = a paragraph written in a different script (e.g. English inside a Hindi book) "
        "is read by a matching voice of the same gender. Multilingual voices are never switched.")


async def voice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        voice = resolve_voice(context.args[0])
        if not voice:
            await update.effective_message.reply_text("Unknown voice. Use /voices to see the list.")
            return
        cfg(uid)["voice"] = voice
        save_settings()
        await update.effective_message.reply_text(f"Voice set: {voice_label(voice)}")
        return
    await update.effective_message.reply_text("Choose language:", reply_markup=lang_keyboard())


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
        extra = f"📋 Queue: job 1 of {queue_size(uid)} — {CURRENT.get(uid, {}).get('label', '')}\n"
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


# ───────────────────────── demos ─────────────────────────

async def demo_bytes(uid, voice):
    """Synthesize a short demo of one voice with the user's rate/pitch/engine."""
    folder = Path(tempfile.mkdtemp(prefix="tts-demo-", dir=WORK_ROOT))
    try:
        s = cfg(uid).copy()
        s["voice"] = voice
        out = folder / f"demo_{safe_name(voice)}.mp3"
        text = demo_text_for(voice)
        if s["engine"] == "google":
            await make_google(text, out, s)
        else:
            await make_edge(text, out, s)
        return out.read_bytes()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


async def send_demo(message, uid, voice):
    try:
        data = await demo_bytes(uid, voice)
        await message.reply_audio(
            audio=data, filename=f"demo_{safe_name(voice)}.mp3",
            title=voice_label(voice)[:64], caption=f"🎙️ {voice_label(voice)}",
        )
        return True
    except Exception as exc:
        log.exception("Demo failed for %s", voice)
        err = str(exc).replace(BOT_TOKEN, "[REDACTED]")[:300]
        await message.reply_text(f"Demo failed for {voice}: {type(exc).__name__}: {err}")
        return False


async def demo_all(message, uid, voices=None):
    """Send a demo for many voices, a few at a time to stay within rate limits."""
    voices = voices or [v for v, _ in VOICES.values()]
    sem = asyncio.Semaphore(TTS_CONCURRENCY)
    status = await message.reply_text(f"🔊 Generating {len(voices)} demos…")
    done = 0

    async def one(voice):
        async with sem:
            try:
                return await demo_bytes(uid, voice)
            except Exception as exc:
                log.warning("Demo failed for %s: %s", voice, exc)
                return None

    tasks = [asyncio.create_task(one(v)) for v in voices]
    failed = []
    try:
        for voice, task in zip(voices, tasks):          # send in the listed order
            data = await task
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
        low = arg.lower()
        if low == "all":
            await demo_all(message, uid)
            return
        if low not in VOICES and low in LANG_NAMES:
            await demo_all(message, uid, [n for _, n, _ in voices_of_lang(low)])
            return
        voice = resolve_voice(arg)
        if not voice:
            await message.reply_text("Unknown voice. Use /voices, /demo <name>, /demo <lang> or /demo all.")
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
    elif data in {"menu:voice", "menu:lang"}:
        await edit("🌐 Choose language:", lang_keyboard())
    elif data.startswith("vl:"):
        lang = data.split(":", 1)[1]
        if lang in LANG_NAMES:
            await edit(f"{LANG_NAMES[lang][0]} {LANG_NAMES[lang][1]} voices (🔊 Demo plays a sample):",
                       voice_keyboard(lang))
    elif data == "auto:toggle":
        s["auto"] = not s.get("auto", True)
        save_settings()
        await edit(settings_text(uid), settings_keyboard())
    elif data == "demo:all":
        if msg:
            await demo_all(msg, uid)
    elif data.startswith("demol:"):
        lang = data.split(":", 1)[1]
        if lang in LANG_NAMES and msg:
            await demo_all(msg, uid, [n for _, n, _ in voices_of_lang(lang)])
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
    items = sorted((x for x in info.get("items", []) if x.get("mode", "silent") == "silent"),
                   key=lambda x: x["chunk"])
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


def result_notes(uid):
    """Extra lines for the final status message: fallbacks, skipped fragments, languages used."""
    items = SKIPPED.get(uid, {}).get("items", [])
    notes = ""
    n_google = sum(1 for x in items if x.get("mode") == "google")
    n_silent = sum(1 for x in items if x.get("mode", "silent") == "silent")
    if n_google:
        notes += f"\n🔁 {n_google} fragment(s) Edge refused were read with Google voice."
    if n_silent:
        notes += f"\n⚠ {n_silent} fragment(s) could NOT be spoken (list below)."
    langs = LANGSTAT.get(uid, {})
    if len(langs) > 1:
        used = ", ".join(f"{LANG_NAMES.get(l, ('', l))[1]} ×{n}"
                         for l, n in sorted(langs.items(), key=lambda kv: -kv[1]))
        notes += f"\n🌐 Voices used (chunks): {used}"
    return notes


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
        await safe_edit(
            status,
            f"✅ Conversion complete!\n{progress_bar(len(parts), len(parts))} 100%\n"
            f"⏱ Used time: {fmt_time(elapsed)}\n"
            f"🎧 Audio duration: {fmt_time(duration)}\n"
            f"📦 Total audio size: {fmt_size(size_bytes)}\n"
            f"🎙️ Audio made: {len(parts)} file(s) sent."
            + result_notes(uid),
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
        LANGSTAT.pop(uid, None)


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


def wait_for_bot_api(max_wait=300):
    """Block until the (local) Bot API answers getMe. Its port opens before it has
    finished connecting to Telegram, and PTB's default 5 s timeout then kills startup."""
    import json as _json
    import urllib.error
    import urllib.request

    url = f"{LOCAL_BOT_API_URL}/bot{BOT_TOKEN}/getMe"
    deadline = time.monotonic() + max_wait
    attempt = 0
    while True:
        attempt += 1
        pause = 3
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                if resp.status == 200:
                    log.info("Local Bot API is ready (attempt %s).", attempt)
                    return
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 404):
                raise SystemExit("Bot API rejected BOT_TOKEN (401/404). Check the BOT_TOKEN secret.")
            if exc.code == 429:
                try:
                    pause = int(_json.loads(exc.read()).get("parameters", {}).get("retry_after", 5)) + 1
                except Exception:
                    pause = 5
            log.warning("Bot API getMe returned HTTP %s (attempt %s).", exc.code, attempt)
        except Exception as exc:                      # timeout / connection refused while it starts
            log.info("Waiting for Bot API… (%s, attempt %s)", type(exc).__name__, attempt)
        if time.monotonic() > deadline:
            log.warning("Bot API still not answering after %ss; trying to start anyway.", max_wait)
            return
        time.sleep(min(pause, 60))


def main():
    load_settings()
    cleanup_stale_workdirs()
    if LOCAL_BOT_API:
        wait_for_bot_api()
    builder = (
        Application.builder().token(BOT_TOKEN).concurrent_updates(True)
        .connect_timeout(30).read_timeout(30).write_timeout(30).pool_timeout(30)
        .get_updates_connect_timeout(30).get_updates_read_timeout(40)
        .get_updates_write_timeout(30).get_updates_pool_timeout(30)
    )
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
    app.add_handler(CommandHandler("lang", lang_cmd))
    app.add_handler(CommandHandler("auto", auto_cmd))
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

    log.info("Bot starting; local API=%s; target part size=%s MB; workers=%s; default voice=%s",
             LOCAL_BOT_API, SEND_PART_MB, TTS_CONCURRENCY, DEFAULT_VOICE)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
