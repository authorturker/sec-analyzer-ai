# config.py — SEC Analyzer
# Only secrets live here. Everything else (tickers, forms, model, schedule,
# language, custom prompts, …) is managed live via Telegram commands and
# persisted in ~/sec-analyzer/bot_config.json — no code edits required.

# ─── EDGAR Identity (required) ────────────────────────────
# SEC requires "Name email@domain.com" in the User-Agent header.
# The bot validates this at startup and refuses to run on the placeholder.
EDGAR_IDENTITY = "Your Name yourname@email.com"

# ─── OpenRouter API (required) ────────────────────────────
# Free key at: openrouter.ai → Keys → Create Key
OPENROUTER_API_KEY = "sk-or-v1-YOUR_KEY_HERE"

# ─── Telegram (required) ──────────────────────────────────
# Bot token : @BotFather → /newbot
# Chat ID   : @userinfobot → /start
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"
