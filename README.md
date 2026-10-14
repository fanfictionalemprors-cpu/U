# Indic Parler-TTS Hindi Female Telegram Audiobook Bot

This version uses AI4Bharat Indic Parler-TTS and adds a `/demo` command
plus a Demo Voice button.

Features:
- Hindi female voice description based on Divya
- TXT upload and MP3 output
- Playback speed options from 1.0x to 2.0x
- Progress updates, duration, and ETA
- 48 kbps mono MP3 parts
- Local Telegram Bot API support

## Hugging Face setup

1. Open https://huggingface.co/ai4bharat/indic-parler-tts
2. Sign in and accept any required access conditions.
3. Create a Hugging Face read access token.
4. Add `HF_TOKEN` in GitHub repository Settings ->
   Secrets and variables -> Actions.

Also configure:
- BOT_TOKEN
- TELEGRAM_API_ID
- TELEGRAM_API_HASH

## Run

Upload all project files, then open GitHub Actions and run the workflow.

In Telegram:
1. Send `/start`
2. Tap Demo Voice
3. Upload a TXT file
4. Select speed from 1.0x to 2.0x

The first model download may take several minutes. CPU synthesis can be
slow for long books, so test the demo before uploading a large TXT file.

Do not commit `.env` or share access tokens.
Stop other processes using the same Telegram bot token to avoid
409 Conflict errors.
