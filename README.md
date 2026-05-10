# 📊 SEC Analyzer Bot

A Telegram bot that monitors SEC filings and insider trading activity for a configurable list of tickers. Triggered by natural language commands — no scheduled jobs, no cron, no cloud infrastructure required. Runs on Android (Termux) or any Linux machine.

## Features

- **SEC Filings** — Monitors 10-K (annual), 10-Q (quarterly), and 8-K (current events) for each tracked ticker.
- **Insider Trading** — Fetches and analyzes Form 4 filings (executive buy/sell transactions) from the last 30 days.
- **Smart Caching** — Never re-analyzes a filing already processed; silent when nothing is new.
- **OpenRouter LLM** — Uses OpenRouter free tier (Llama 3.3 70B by default, 128K context) for structured investor-focused summaries.
- **Telegram Interface** — Fully command-driven; responds only to your own chat ID.
- **Lightweight** — Runs on a mid-range Android phone (tested on Poco F5 via Termux).

## How It Works

```text
You send "Any news?" on Telegram
          ↓
Bot fetches latest filings via edgartools (SEC EDGAR)
          ↓
New filings are extracted, key sections isolated
          ↓
OpenRouter LLM analyzes each filing with structured prompts
          ↓
Analysis report sent back via Telegram
          ↓
Filing cached — won't be processed again
```

## Commands

| Message | Action |
|---|---|
| Any news? · Check · Scan · /sec | Scan 10-K, 10-Q, 8-K for all tickers |
| Insider · /insider | Analyze Form 4 insider transactions (last 30 days) |
| Check all · /all | Run SEC + Insider scan together |
| /help | Show command reference |

## Requirements

- Python 3.10+
- Packages: edgartools, requests, tzdata [Big thanks to edgartools team, you can reach original repo here](https://github.com/dgunning/edgartools)
- [OpenRouter API key — free tier, no credit card required](https://openrouter.ai)
- A Telegram bot token via [@BotFather](https://t.me/BotFather)
- Your Telegram user ID via [@userinfobot](https://t.me/userinfobot)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/authorturker/sec-analyzer-ai.git
cd sec-analyzer-bot
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install edgartools requests tzdata
```

### 4. Configure

```bash
cp config.example.py config.py
nano config.py
```

### 5. Run

```bash
python bot.py
```

## Configuration

Edit `config.py` before running. The only required fields:

```python
EDGAR_IDENTITY     = "Your Name yourname@email.com"  # Required by SEC
OPENROUTER_API_KEY = "sk-or-v1-..."                  # From openrouter.ai
TELEGRAM_BOT_TOKEN = "123456:ABC..."                 # From @BotFather
TELEGRAM_CHAT_ID   = "123456789"                    # From @userinfobot
TICKERS = ["TICKER1", "TICKER2", "TICKER3", "TICKER4", "ETC..."]  # Tickers to track
```

## Switching Models

The default model is `openrouter/free`. To switch:

```python
OPENROUTER_MODEL = "google/gemma-3-27b-it:free"
# or
OPENROUTER_MODEL = "deepseek/deepseek-chat-v3-0324:free"
```

All free models on OpenRouter reset daily at UTC 00:00. A full scan of 8 tickers generates ~25–40 API calls — comfortably within the 50 requests/day free limit.

## Running on Android (Termux)

Install Termux from F-Droid — not the Play Store version — then:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python python-pip tmux
pip install edgartools requests tzdata
```

Keep the bot alive with tmux:

```bash
tmux new -s sec
python bot.py
```

```text
# Ctrl+B then D to detach
```

## Project Structure

```text
sec-analyzer-bot/

├── bot.py              # Main bot — polling, SEC & insider scan logic
├── config.py           # Your keys and settings (not committed to git)
├── config.example.py   # Template — copy to config.py
└── reports/            # Auto-created at runtime
    ├── bot.log
    └── analyzed_cache.json
```

## Analysis Output Example

🏢 MU — 10-Q  
📅 2026-04-03

📊 Quarter Performance
- Revenue: $8.7B (+18% YoY) — strong DRAM demand
- Gross margin: 34.2% → 38.1% (HBM contribution)

🔑 Key Messages from Management
- AI server demand tracking above expectations
- HBM3E capacity expansion on track for Q3

⚠️ Notable Changes
- NAND pricing pressure continues
- China export restrictions remain an overhang

👀 3 Factors to Watch
1. HBM ramp cadence and yield rates
2. PC/mobile DRAM pricing recovery
3. Further export control developments

## Cost

**$0.** OpenRouter free tier provides 50 requests/day per model (resets UTC 00:00). Loading $10 credit unlocks 1,000 requests/day — the credit is only drawn when using paid models, not free ones.

## Troubleshooting

| Error | Fix |
|---|---|
| 403 Forbidden from SEC | Check EDGAR_IDENTITY — must be "Name email@domain.com" |
| No time zone found with key UTC | Run `pip install tzdata` |
| 429 Too Many Requests | Rate limit hit — bot retries automatically; switch model in config if persistent |
| Analysis shows ⚠️ Analysis unavailable | API timeout — retry or switch to a different free model |

## Disclaimer

This tool is for informational purposes only. Nothing it produces constitutes investment advice. Always do your own research before making investment decisions.

## License

MIT
