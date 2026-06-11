# config.py — SEC Analyzer
#
# Thin secrets loader. Real values live in a .env file (git-ignored) — see
# .env.example for the template. bot.py keeps importing from here unchanged:
#     from config import EDGAR_IDENTITY, OPENROUTER_API_KEY, ...
#
# Setup (one-time):
#     cp .env.example .env      # then edit .env with your real values
#
# Everything else (tickers, forms, model, schedule, language, custom
# prompts, …) is managed live via Telegram commands and persisted in
# ~/sec-analyzer/bot_config.json — no code edits required.

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    # Load the .env sitting next to this file (works regardless of CWD).
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    # python-dotenv not installed — fall back to the process environment.
    # The startup health check in bot.py will flag any missing values.
    pass

# Required secrets — the bot refuses to start if any of the three below are
# missing or invalid. OPENROUTER_API_KEY and TELEGRAM_CHAT_ID are legacy
# migration sources: they are imported once into bot_config.json (J1) and are
# not required at startup thereafter.
EDGAR_IDENTITY     = os.getenv("EDGAR_IDENTITY",     "Your Name yourname@email.com")
MASTER_CHAT_ID     = os.getenv("MASTER_CHAT_ID",     "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
# Legacy — kept as migration sources; not required after first run.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
