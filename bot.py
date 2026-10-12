"""
Telegram TTS Audiobook Bot
--------------------------
.txt file bhejo -> voice choose karo -> audiobook parts (mp3) wapas milenge.

Setup:
    pip install python-telegram-bot==21.* edge-tts
    ffmpeg + ffprobe install hona chahiye (sudo apt install ffmpeg)

Run:
    export BOT_TOKEN="123456:ABC..."   # @BotFather se token
    python tts_audiobook_bot.py

Audio: 48 kbps mono mp3.
  - Normal mode: har part max 50 MB (size ke hisaab se auto split).
  - Local Bot API mode (LOCAL_API_URL set): 2 GB tak, default part = 180 min.

Optional env:
    LOCAL_API_URL=http://localhost:8081   # local telegram-bot-api server
    PART_MINUTES=180                      # sirf local mode me use hota hai
"""
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

BOT_TOKEN = os.getenv("BOT_TOKEN", "PASTE_TOKEN_HERE")
LOCAL_API = os.getenv("LOCAL_API_URL", "").rstrip("/")
BITRATE_KBPS = 48
SAFETY = 0.97                                     # 3% margin (tags/headers)
if LOCAL_API:
    MAX_PART_BYTES = 2000 * 1000 * 1000           # local server: 2 GB upload
    MAX_DOWNLOAD_BYTES = 2000 * 1000 * 1000
else:
    MAX_PART_BYTES = 50 * 1000 * 1000             # cloud API: 50 MB upload
    MAX_DOWNLOAD_BYTES = 20 * 1000 * 1000         # cloud API: 20 MB download
# 48 kbps = 6000 bytes/sec -> 50 MB ~ 8083 sec (~2h 14m)
MAX_PART_SECONDS = MAX_PART_BYTES * SAFETY / (BITRATE_KBPS * 1000 / 8)
if LOCAL_API:
    MAX_PART_SECONDS = min(MAX_PART_SECONDS, int(os.getenv("PART_MINUTES", "180")) * 60)
CHUNK_CHARS = int(os.getenv("CHUNK_CHARS", "1500"))  # smaller requests are more reliable
CONCURRENCY = int(os.getenv("TTS_CONCURRENCY", "2"))  # avoid Edge TTS throttling
TTS_RETRIES = int(os.getenv("TTS_RETRIES", "7"))
PERFORMER = "AudioBook Pro"

VOICES = {
    "hi_m": ("🇮🇳 Hindi - Madhur (M)", "hi-IN-MadhurNeural"),
    "hi_f": ("🇮🇳 Hindi - Swara (F)", "hi-IN-SwaraNeural"),
    "en_m": ("🇬🇧 English - Guy (M)", "en-US-GuyNeural"),
    "en_f": ("🇺🇸 English - Aria (F)", "en-US-AriaNeural"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ttsbot")


# ---------------------------------------------------------------- helpers
def fmt_time(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600}h {(sec % 3600) // 60}m {sec % 60}s"


def fmt_clock(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def progress_bar(pct: float, width: int = 12) -> str:
    pct = max(0, min(100, int(pct)))
    filled = round(width * pct / 100)
    return "█" * filled + "░" * (width - filled)


def split_text(text: str, limit: int = CHUNK_CHARS) -> list[str]:
    """Sentence boundary pe text ko chunks me todta hai."""
    text = re.sub(r"[ \t]+", " ", text)
    sentences = re.split(r"(?<=[।.!?\n])\s*", text)
    chunks, cur = [], ""
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        while len(s) > limit:  # bahut lambi line
            cut = s.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(s[:cut])
            s = s[cut:].strip()
        if len(cur) + len(s) + 1 > limit:
            chunks.append(cur)
            cur = s
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c.strip()]


async def run(*cmd: str) -> str:
    p = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await p.communicate()
    if p.returncode != 0:
        raise RuntimeError(err.decode(errors="ignore")[-500:])
    return out.decode().strip()


async def audio_duration(path: Path) -> float:
    out = await run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
    )
    return float(out)


async def tts_chunk(text: str, voice: str, out: Path, sem: asyncio.Semaphore):
    """Generate one chunk with bounded concurrency, cleanup, and exponential backoff."""
    async with sem:
        last_error = None
        for attempt in range(1, TTS_RETRIES + 1):
            # Remove any partial/corrupt output left by a failed attempt.
            out.unlink(missing_ok=True)
            try:
                communicate = edge_tts.Communicate(text, voice)
                await communicate.save(str(out))
                if out.exists() and out.stat().st_size > 256:
                    return
                raise RuntimeError("TTS returned an empty or incomplete audio file")
            except Exception as e:
                last_error = e
                out.unlink(missing_ok=True)
                delay = min(45, 3 * (2 ** (attempt - 1))) + random.uniform(0, 1.5)
                log.warning(
                    "TTS chunk failed (attempt %s/%s, voice=%s, chars=%s): %s; retrying in %.1fs",
                    attempt, TTS_RETRIES, voice, len(text), repr(e), delay
                )
                if attempt < TTS_RETRIES:
                    await asyncio.sleep(delay)
        raise RuntimeError(
            f"TTS chunk failed after {TTS_RETRIES} attempts. "
            f"Voice={voice}, chars={len(text)}, last_error={last_error!r}"
        )


async def concat_mp3(files: list[Path], out: Path):
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{f.as_posix()}'\n" for f in files))
    await run(
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
        "-c:a", "libmp3lame", "-b:a", f"{BITRATE_KBPS}k", "-ac", "1", "-ar", "24000",
        str(out),
    )
    lst.unlink(missing_ok=True)


# ---------------------------------------------------------------- handlers
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Namaste! Mujhe ek <b>.txt</b> file bhejo, main uska audiobook bana dunga.\n\n"
        "Steps:\n1️⃣ .txt file bhejo\n2️⃣ Voice choose karo\n3️⃣ Audiobook parts milenge 🎧",
        parse_mode="HTML",
    )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("❌ Sirf .txt file bhejo.")
        return
    if doc.file_size and doc.file_size > MAX_DOWNLOAD_BYTES:
        await update.message.reply_text(
            f"❌ File {MAX_DOWNLOAD_BYTES // 1_000_000} MB se badi hai (Telegram limit)."
        )
        return

    work = Path(tempfile.mkdtemp(prefix="tts_"))
    path = work / "input.txt"
    f = await ctx.bot.get_file(doc.file_id)
    await f.download_to_drive(path)

    ctx.user_data["job"] = {"dir": work, "path": path, "name": Path(doc.file_name).stem}
    kb = [[InlineKeyboardButton(label, callback_data=f"v:{k}")] for k, (label, _) in VOICES.items()]
    await update.message.reply_text(
        f"📘 <b>{doc.file_name}</b> mil gayi.\nVoice choose karo:",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML",
    )


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    job = ctx.user_data.get("job")
    if not job:
        await q.edit_message_text("File expire ho gayi, dobara bhejo.")
        return
    ctx.user_data["job"] = None
    voice_key = q.data.split(":", 1)[1]
    voice = VOICES[voice_key][1]
    status = await q.edit_message_text("⏳ Shuru kar raha hoon...")
    ctx.application.create_task(process(status, ctx, job, voice, q.message.chat_id))


async def process(status, ctx, job, voice: str, chat_id: int):
    work: Path = job["dir"]
    name: str = job["name"]
    try:
        raw = job["path"].read_bytes()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("utf-16", errors="ignore")

        chunks = split_text(text)
        total = len(chunks)
        if not total:
            await status.edit_text("❌ File khaali hai.")
            return

        # ---- 1) TTS (parallel)
        sem = asyncio.Semaphore(CONCURRENCY)
        files = [work / f"c{i:05d}.mp3" for i in range(total)]
        done = 0
        started_at = time.monotonic()
        last_update = -1

        async def worker(i):
            nonlocal done
            await tts_chunk(chunks[i], voice, files[i], sem)
            done += 1

        tasks = [asyncio.create_task(worker(i)) for i in range(total)]
        while not all(t.done() for t in tasks):
            await asyncio.sleep(3)
            pct = done * 100 // total
            if pct != last_update:
                last_update = pct
                elapsed = time.monotonic() - started_at
                eta = (elapsed / done * (total - done)) if done else 0
                # Estimate final MP3 duration from completed chunks and source text.
                try:
                    completed_durations = [await audio_duration(files[i]) for i in range(total) if files[i].exists() and files[i].stat().st_size > 256]
                    completed_audio = sum(completed_durations)
                except Exception:
                    completed_audio = 0
                estimated_audio = (completed_audio / done * total) if done and completed_audio else 0
                msg = (
                    f"🎙 <b>Audiobook ban raha hai</b>\n"
                    f"{progress_bar(pct)} <b>{pct}%</b> ({done}/{total} chunks)\n"
                    f"🎧 MP3 duration: {fmt_clock(completed_audio)} complete"
                    + (f" | Estimated total: ~{fmt_clock(estimated_audio)}\n" if estimated_audio else "\n")
                    + f"⏱ Elapsed: {fmt_clock(elapsed)} | ETA: {fmt_clock(eta)}"
                )
                try:
                    await status.edit_text(msg, parse_mode="HTML")
                except Exception:
                    pass
        for t in tasks:
            t.result()  # error raise kare agar koi fail hua

        # ---- 2) parts me group karo
        await status.edit_text(
            f"🔧 <b>TTS complete!</b>\n{progress_bar(100)} <b>100%</b>\n"
            f"🎧 MP3 duration calculate kar raha hoon...\n"
            f"⏱ Elapsed: {fmt_clock(time.monotonic() - started_at)}",
            parse_mode="HTML",
        )
        durs = [await audio_duration(f) for f in files]
        limit = MAX_PART_SECONDS
        parts, cur, cur_d = [], [], 0.0
        for f, d in zip(files, durs):
            if cur and cur_d + d > limit:
                parts.append((cur, cur_d))
                cur, cur_d = [], 0.0
            cur.append(f)
            cur_d += d
        if cur:
            parts.append((cur, cur_d))

        # ---- 3) merge + send
        n = len(parts)
        total_audio_duration = sum(d for _, d in parts)
        for idx, (group, dur) in enumerate(parts, 1):
            pct = (idx - 1) * 100 // n
            elapsed = time.monotonic() - started_at
            eta = (elapsed / (idx - 1) * (n - idx + 1)) if idx > 1 else 0
            await status.edit_text(
                f"📤 <b>Audiobook parts send ho rahe hain</b>\n"
                f"{progress_bar(pct)} <b>{pct}%</b> (Part {idx - 1}/{n})\n"
                f"🎧 Total MP3 duration: {fmt_clock(total_audio_duration)}\n"
                f"📀 Current part: {fmt_clock(dur)}\n"
                f"⏱ Elapsed: {fmt_clock(elapsed)} | ETA: {fmt_clock(eta)}",
                parse_mode="HTML",
            )
            out = work / f"{name} - Part {idx}.mp3"
            await concat_mp3(group, out)
            if out.stat().st_size > MAX_PART_BYTES:
                raise RuntimeError(f"Part {idx} size limit se bada ban gaya")
            caption = f"{name}\nAudiobook - part {idx}\n{fmt_time(dur)} | {voice}"
            with open(out, "rb") as fh:
                await ctx.bot.send_audio(
                    chat_id,
                    audio=fh,
                    title=f"{name} - Part {idx}",
                    performer=PERFORMER,
                    duration=int(dur),
                    caption=caption,
                    filename=out.name,
                    read_timeout=900,
                    write_timeout=900,
                    connect_timeout=60,
                )
            out.unlink(missing_ok=True)

        await status.edit_text(
            f"✅ <b>Audiobook complete!</b>\n{progress_bar(100)} <b>100%</b>\n"
            f"🎧 Total MP3 duration: {fmt_clock(total_audio_duration)}\n"
            f"📀 MP3 parts: {n}\n"
            f"⏱ Total processing time: {fmt_clock(time.monotonic() - started_at)}",
            parse_mode="HTML",
        )
    except Exception as e:
        log.exception("job failed")
        await status.edit_text(f"❌ Error: {e}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    builder = Application.builder().token(BOT_TOKEN).concurrent_updates(True)
    if LOCAL_API:
        builder = (
            builder.base_url(f"{LOCAL_API}/bot")
            .base_file_url(f"{LOCAL_API}/file/bot")
            .local_mode(True)
        )
        log.info("Local Bot API mode: %s", LOCAL_API)
    app = builder.build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(CallbackQueryHandler(on_voice, pattern=r"^v:"))
    log.info("Bot chal raha hai...")
    app.run_polling()


if __name__ == "__main__":
    main()
