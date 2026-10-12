
# Offline Hindi Female Telegram Audiobook Bot

TXT -> offline Piper Hindi female (Priyamvada) -> MP3 at 48 kbps mono. Edge TTS and gTTS are removed, so Edge 403 / Google gTTS 429 will not be used. Internet is needed only while Docker builds and downloads the model; speech synthesis itself is local.

## GitHub Actions secrets
Set repository Actions secrets `BOT_TOKEN`, `TELEGRAM_API_ID`, and `TELEGRAM_API_HASH`.

## Important
- Stop any other instance polling with the same BOT_TOKEN (for example Render) to avoid Telegram 409 Conflict.
- The existing workflow uses a scheduled GitHub-hosted runner and restarts periodically. It is not a permanent server; uploads/conversion must finish while the job is alive.
- `docker compose up -d --build` downloads the model at image-build time. First build is larger/slower. Model license and voice card are hosted by the Piper voices project; check the model's license before redistribution.
- Speed buttons 1.0x–2.0x use FFmpeg `atempo` after local synthesis.
- GitHub-hosted runner CPU is limited; offline local synthesis can be slower than hosted TTS, and a very large book may outlast a job run.
