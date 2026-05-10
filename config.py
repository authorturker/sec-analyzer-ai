# config.py — SEC Analyzer
# Edit with: nano ~/sec-analyzer/config.py

import os

# ─── EDGAR Identity (required) ────────────────────────────
# SEC requires a name and email in the User-Agent header.
EDGAR_IDENTITY = "Your Name yourname@email.com"

# ─── OpenRouter API ───────────────────────────────────────
# Free key at: openrouter.ai → Keys → Create Key
OPENROUTER_API_KEY = "sk-or-v1-YOUR_KEY_HERE"

# Recommended free models:
#   meta-llama/llama-3.3-70b-instruct:free
#   google/gemma-3-27b-it:free
#   deepseek/deepseek-chat-v3-0324:free
OPENROUTER_MODEL = "openrouter/free"

# ─── Tracked Tickers ──────────────────────────────────────
TICKERS = [
    "TICKER1",
    "TICKER2",
    "TICKER3",
    "TICKER4",
    "ETC...",
]

# ─── Filing Types ─────────────────────────────────────────
FILING_TYPES = ["10-K", "10-Q", "8-K"]

# ─── Lookback Window ──────────────────────────────────────
# How many days back to search for new filings.
DAYS_LOOKBACK = 35

# ─── Telegram ─────────────────────────────────────────────
# Bot token : @BotFather → /newbot
# Chat ID   : @userinfobot → /start
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"

# ─── File Paths ───────────────────────────────────────────
_BASE      = os.path.expanduser("~/sec-analyzer")
OUTPUT_DIR = f"{_BASE}/reports"
CACHE_FILE = f"{_BASE}/analyzed_cache.json"

# ─── Analysis Depth ───────────────────────────────────────
# Characters sent to the model per filing section.
# Free models handle 10000–12000 comfortably.
# Raise to 16000 if you want deeper analysis and the model supports it.
MAX_CHARS = 10000
