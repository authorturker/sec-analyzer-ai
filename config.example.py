# config.example.py — Copy this to config.py and fill in your values
# cp config.example.py config.py

import os

# ─── EDGAR Identity (required) ────────────────────────────
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
DAYS_LOOKBACK = 35

# ─── Telegram ─────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"

# ─── File Paths ───────────────────────────────────────────
_BASE      = os.path.expanduser("~/sec-analyzer")
OUTPUT_DIR = f"{_BASE}/reports"
CACHE_FILE = f"{_BASE}/analyzed_cache.json"

# ─── Analysis Depth ───────────────────────────────────────
MAX_CHARS = 10000
