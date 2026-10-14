# 🎙️ Hindi Audiobook Telegram Bot

A Telegram bot that converts TXT novels and stories into MP3 audiobooks
using the Inaya Hindi/Hinglish female voice from Indus Pocket TTS.

## ✨ Features

- Hindi/Hinglish female TTS voice
- TXT to MP3 conversion
- Automatic text splitting
- MP3 audio output at 48 kbps
- Mono audio
- Audio parts sent individually
- Progress updates during processing
- Elapsed time and estimated remaining time
- Voice demo using /demo
- Playback speed options from 1.0x to 2.0x
- Docker-based deployment
- GitHub Actions workflow
- Telegram Local Bot API support

## 🎙️ TTS Model

Model: Indus Pocket TTS

Model repository:
https://huggingface.co/Indus-Labs/indus-pocket-tts

Voice: Inaya

The model is public, so this configuration does not require an HF_TOKEN.

The model is designed primarily for conversational Hindi and Hinglish.
Pronunciation may be less accurate for difficult literary Hindi,
uncommon words, and long descriptive passages.

## 📁 Project Structure

```text
.
├── bot.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── .gitignore
└── .github
    └── workflows
        └── bot.yml
```

## 🔑 Required Environment Variables

| Variable | Description |
|---|---|
| BOT_TOKEN | Telegram bot token from BotFather |
| TELEGRAM_API_ID | Telegram API ID |
| TELEGRAM_API_HASH | Telegram API hash |
| PART_MINUTES | Audio part configuration |
| MAX_CHARS | Approximate maximum characters per TTS chunk |
| TTS_MODEL_DIR | Local path to the downloaded TTS model |

No HF_TOKEN is required for the public model.

## 🔐 GitHub Secrets

Open:

Repository → Settings → Secrets and variables → Actions

Add these repository secrets:

- BOT_TOKEN
- TELEGRAM_API_ID
- TELEGRAM_API_HASH

Do not put real credentials in README.md or commit them to Git.

## 🚀 Deploy with GitHub Actions

1. Upload the project files to your GitHub repository.
2. Add the three required GitHub Secrets.
3. Check the Dockerfile and workflow configuration.
4. Open the Actions tab.
5. Select `Run Hindi Audiobook Bot`.
6. Click `Run workflow`.
7. Open the running workflow and inspect its logs.

The first build downloads the TTS model and may take longer than subsequent runs.

## 🐳 Run with Docker

Create a `.env` file in the project directory:

```dotenv
BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
TELEGRAM_API_ID=YOUR_TELEGRAM_API_ID
TELEGRAM_API_HASH=YOUR_TELEGRAM_API_HASH
PART_MINUTES=30
MAX_CHARS=450
```

Build and start the services:

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f bot
```

Stop the services:

```bash
docker compose down
```

## 📚 How to Use the Bot

1. Open your Telegram bot.
2. Send `/start`.
3. Send a `.txt` file.
4. Wait while the bot generates the audio.
5. Completed audio parts are sent to Telegram.

Available commands:

- `/start` — Start the bot
- `/help` — Show usage instructions
- `/demo` — Generate a voice demo
- `/speed 1.5` — Set the requested generation speed

## ⚡ Audio Speed

The intended speed range is 1.0x to 2.0x.

The bot uses FFmpeg to change audio playback speed.

## ⏱️ GitHub Actions Runtime

GitHub-hosted runners are temporary environments.

This workflow is configured to run for a limited period and then stop.
It does not provide guaranteed 24/7 uptime.

When the workflow stops, the bot becomes unavailable until another
workflow run starts.

## ⚠️ Troubleshooting

### Bot does not respond

- Verify BOT_TOKEN.
- Confirm that another polling instance is not using the same bot token.
- Inspect the GitHub Actions logs.

### Missing Telegram API credentials

Verify that TELEGRAM_API_ID and TELEGRAM_API_HASH are configured
as GitHub repository secrets.

### TTS model loading error

Inspect the Docker build logs and confirm that the public model
was downloaded successfully.

### PyTorch or torchaudio error

Check that PyTorch and audio dependencies are compatible.
Review the Docker build output for dependency conflicts.

### Audio generation fails

Inspect the bot logs. Difficult literary vocabulary may not be
pronounced correctly by the selected voice model.

## 📜 Model and Software Licensing

Review the model repository for its current model license and
attribution requirements:

https://huggingface.co/Indus-Labs/indus-pocket-tts

Review the upstream implementation and its license before modifying
or redistributing the project.

## 🔒 Security

- Never commit `.env`.
- Never publish BOT_TOKEN.
- Never publish TELEGRAM_API_HASH.
- Keep credentials in GitHub Secrets or an untracked local `.env` file.
