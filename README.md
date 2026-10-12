# TTS Audiobook Bot (runs on GitHub Actions)

Telegram bot: .txt file -> audiobook (48 kbps mp3 parts) using Edge TTS.

## Deploy on GitHub (free)
1. Repo PUBLIC rakho (public repo me Actions minutes free hain).
2. Bot ko cloud API se logout karo (ek baar):
   curl https://api.telegram.org/bot<TOKEN>/logOut
3. Repo > Settings > Secrets and variables > Actions > New repository secret:
   BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH
4. Repo > Settings > Actions > General > Workflow permissions:
   "Read and write permissions" ON
5. Actions tab > "Run Bot" > Run workflow. Bas.

Har run ~5h45m chalta hai aur apne aap next run queue kar deta hai.
Restart ke time ~1-2 min bot offline rehta hai, aur chal rahi conversion beech me rukk sakti hai.
