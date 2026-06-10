# 📊 SEC Analyzer Bot

<div align="center">

**A Telegram bot that monitors SEC filings and insider trading for a configurable watchlist.**

Single-file Python, two-language UI, no cloud required. Runs on Android (Termux) or any Linux box.

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://python.org)
[![OpenRouter](https://img.shields.io/badge/LLM-OpenRouter%20Free-6c47ff)](https://openrouter.ai)
[![Telegram](https://img.shields.io/badge/Interface-Telegram-2CA5E0?logo=telegram&logoColor=white)](https://telegram.org)
[![Tests](https://img.shields.io/badge/tests-477%20passing-success)](#-tests)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

</div>

---

## ✨ Features

| | |
|---|---|
| 📄 **13 Form Types** | 10-K, 10-Q, 8-K, Form 4, SC 13G/D, S-1, DEF 14A, and more |
| 🌐 **Two languages** | English + Turkish UI, switch live with `/setlang en` / `/setlang tr` |
| 🔐 **Insider Trading** | Form 4 analysis with portfolio-wide sentiment score |
| 📊 **Sentiment trend** | `/sentiment trend [days]` — compare current vs. N-day-ago insider mood |
| 🆚 **Side-by-side compare** | `/compare AAPL MSFT 10-K` — single LLM call, four-axis comparison |
| 🗂 **Watchlist groups** | `/addgroup tech AAPL MSFT NVDA` → `/scangroup tech` |
| 📈 **Price action** | Filing date → +N day stock change appended automatically (free, no API key) |
| 💹 **On-demand prices** | `/checkprice AAPL [days]` — last-N-day window summary (yfinance) |
| 📰 **Yahoo Finance news** | `/checknews AAPL [count]` — latest headlines with publisher links (yfinance) |
| 🧠 **Smart Caching** | Never re-analyzes a filing already processed; old entries auto-pruned |
| 🤖 **Guided Setup** | First-run wizard, starts with language picker — no config file editing needed |
| ⚙️ **Full Telegram Control** | Manage tickers, forms, model, language, schedule, prompts from chat |
| ⏰ **Auto Scheduling** | Daily auto-scan at a time you set + hourly filing alarm (probe-only) |
| 📊 **Weekly Digest** | Sunday summary of everything analyzed that week |
| 🔍 **Filing Diff** | Risk-factor comparison between current and previous 10-K / 10-Q |
| ✏️ **Custom Prompts** | Override the analysis prompt per form type |
| 📄 **Inline Original** | Button after each analysis to receive the raw filing as `.txt` |
| 📋 **Markdown Reports** | `/report` sends this week's full analyses as a `.md` file |
| 🔔 **Webhook Mode** | Optional — faster response, lower battery use |
| 📡 **Health Monitoring** | `/status` shows uptime, error counts, last scan/alarm times |
| 🧾 **Grounded Analysis** | 10-K/10-Q/20-F analyses are grounded in audited XBRL facts injected into the prompt |
| ✅ **Numeric Verification** | Figures in the LLM output are checked against XBRL facts and the filing text; unverifiable ones are flagged with ⚠️ |
| 🔎 **Keyword Alerts** | Watch any phrase across ALL EDGAR filings (`/addword`); hourly full-text search alert, no LLM cost |
| 💼 **Portfolio P&L** | Track positions with `/addpos`; `/pnl` shows unrealized profit/loss per ticker (Stooq, no API key) |
| 🧪 **Tested** | 477 pytest tests for pure helpers, i18n, config cache, thread safety |
| ⚡ **OpenRouter Free LLM** | openrouter/free, $0 cost |
| 📱 **Lightweight** | Single 1.8k-line `bot.py`, runs on a mid-range Android phone via Termux |

---

## 🗂 Project Structure

```text
sec-analyzer/
├── bot.py                       # Main bot — everything in one file
├── config.py                    # Thin loader — reads secrets from .env
├── .env.example                 # Secrets template — copy to .env, fill in
├── requirements.txt             # pip dependencies
├── .gitignore
├── lang/
│   ├── en.json                  # English UI strings (default)
│   └── tr.json                  # Turkish UI strings
├── tests/                       # 477 pytest tests
│   ├── conftest.py
│   ├── test_alarm_buttons.py    # Interactive alarm + on-demand .md button
│   ├── test_cfg.py              # Config cache + atomic mutate
│   ├── test_checkprice_news.py  # /checkprice + /checknews formatters
│   ├── test_compare.py          # /compare prompt builder
│   ├── test_groups.py           # Watchlist groups
│   ├── test_hotfixes.py         # Atomic JSON IO, wizard guard, digest week
│   ├── test_i18n.py             # Language loader, t(), fallback
│   ├── test_portfolio.py        # Portfolio P&L
│   ├── test_price.py            # Stooq parsing + price snippet
│   ├── test_probe.py            # Alarm probe — whole-watchlist hit list
│   ├── test_pure.py             # render, extract_section, build_prompt, …
│   ├── test_sentiment.py        # Sentiment parse + trend rendering
│   ├── test_startup_company.py  # Startup checks + Company cache
│   ├── test_state.py            # Locks, raw store, cache TTL
│   ├── test_watchwords.py       # EDGAR full-text watchword alarm
│   ├── test_xbrl_facts.py       # XBRL fact extraction + facts block
│   └── test_verify.py           # Numeric claim verification
└── README.md
```

Runtime files (created automatically under `~/sec-analyzer/`, **not** in the repo):

```text
~/sec-analyzer/
├── bot_config.json         # Live settings managed via Telegram
├── cache.json              # Analyzed filings cache
├── weekly_log.json         # Buffer for digest + /report
├── sentiment_history.json  # /sentiment trend history
├── price_cache.json        # Stooq price snippets cache
├── watchword_seen.json     # Watchword dedup state
├── previous_filings/       # For risk-factor diff
└── reports/
    └── bot.log
```

---

## 🚀 Quick Start

```bash
git clone https://github.com/authorturker/sec-analyzer-ai.git
cd sec-analyzer-ai
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # then edit .env with your 4 keys
python bot.py
```

On first run the bot launches a bilingual wizard — pick a language, choose default form types, add tickers, done.

---

## ⚙️ Configuration

Secrets live in a `.env` file (git-ignored). Copy the template and fill in **four** values:

```bash
cp .env.example .env
```

```ini
EDGAR_IDENTITY=Your Name yourname@email.com   # Required by SEC
OPENROUTER_API_KEY=sk-or-v1-...               # From openrouter.ai
TELEGRAM_BOT_TOKEN=123456:ABC...              # From @BotFather
TELEGRAM_CHAT_ID=123456789                    # From @userinfobot
```

`config.py` is a thin loader that reads these via `python-dotenv` — you never edit `config.py` itself. At startup the bot runs a health check and refuses to run if any of the four is missing, malformed, or still a placeholder.

Everything else — tickers, default forms, model, schedule, language, custom prompts, webhook URL, price-action lookforward, raw-filing cap (default 100), cache TTL — is managed live via Telegram commands and stored in `~/sec-analyzer/bot_config.json`.

---

## 💬 Commands
### Scans
| Command | Action |
|---|---|
| `Any news?` · `Check` · `/sec` | Scan watchlist with default forms |
| `Insider` · `/insider` | Form 4 only across watchlist |
| `Check all` · `/all` | SEC + Insider combined |
| `/sentiment` | Portfolio-wide insider sentiment score |
| `/sentiment trend [days]` | Compare current sentiment vs. N days ago (default 30) |
| `/scanticker AAPL` | Scan single ticker, default forms, not added to watchlist |
| `/scanticker AAPL 10-K 4` | Scan single ticker with specific forms |
| `/compare AAPL MSFT [FORM]` | Side-by-side comparison (default form: 10-K) |
| `/checkprice AAPL [days]` | Last-N-day price summary — change, open/close, high/low (default 7) |
| `/checknews AAPL [count]` | Recent Yahoo Finance headlines + publisher links (default 5, max 20) |

### Ticker Management
| Command | Action |
|---|---|
| `/addticker AAPL` | Add ticker to watchlist |
| `/addticker AAPL MSFT NVDA` | Bulk add |
| `/removeticker AAPL` | Remove ticker |
| `/listtickers` | Show full watchlist |

### Groups
| Command | Action |
|---|---|
| `/addgroup tech AAPL MSFT NVDA` | Create or replace a named group |
| `/removegroup tech` | Delete a group |
| `/listgroups` | Show all groups and their members |
| `/scangroup tech` | Scan a group with default forms |
| `/scangroup tech 10-K 4` | Scan a group with specific forms |

### Form Management
| Command | Action |
|---|---|
| `/listforms` | All 13 supported forms + which are active |
| `/addform SC 13G` | Add form to default scan |
| `/removeform 8-K` | Remove form from default scan |

### Custom Prompts
| Command | Action |
|---|---|
| `/setprompt 10-K <text>` | Override analysis prompt for a form type |
| `/getprompt 10-K` | Show current custom prompt |
| `/resetprompt 10-K` | Revert to default prompt |
| `/listprompts` | Show all active custom prompts |

### Reports
| Command | Action |
|---|---|
| `/report` | Send this week's full analyses as a `.md` file |
| *(inline button after analysis)* | Receive the raw filing as a `.txt` |

### Scheduling & Alerts
| Command | Action |
|---|---|
| `/setschedule 08:00` | Auto-scan daily at 08:00 |
| `/setschedule off` | Disable auto-scan |
| `/alarm` | Enable hourly filing alarm (probe only — no LLM, no cache write) |
| `/alarm off` | Disable alarm |
| `/digest` | Enable weekly Sunday digest |
| `/digest now` | Send digest immediately |
| `/digest off` | Disable weekly digest |

### Watchwords
| Command | Action |
|---|---|
| `/addword <phrase>` | Watch a phrase across all EDGAR filings (max 10) |
| `/removeword <phrase>` | Stop watching a phrase |
| `/listwords` | Show all watched phrases |

### Portfolio
| Command | Action |
|---|---|
| `/addpos TICKER QTY PRICE [DATE]` | Add a lot; fractional shares OK, max 50 lots |
| `/removepos TICKER` | Remove all lots for a ticker |
| `/pnl` | Unrealized P&L summary (delayed/end-of-day prices from Stooq) |

### Language & Webhook
| Command | Action |
|---|---|
| `/setlang en` / `/setlang tr` | Switch UI + LLM-output language |
| `/setwebhook <url>` | Switch to webhook mode (requires Flask + public URL) |
| `/delwebhook` | Switch back to polling mode |

### Settings & Status
| Command | Action |
|---|---|
| `/settings` | Show all current settings |
| `/status` | Bot uptime, error counts, last scan time |
| `/setmodel <model>` | Switch LLM model |
| `/setlookback 60` | Set lookback window in days (1–365) |
| `/setchars 15000` | Set max characters per section (1000–50000) |
| `/setrawmax 500` | Cap in-memory raw-filing cache (0 = unlimited) |
| `/priceaction on` / `off` | Toggle the per-filing price-action snippet |
| `/setlookforward 5` | Days after filing for price change (1–90) |

---

## 📋 Supported Form Types

| Form | Description |
|---|---|
| `10-K` | Annual report |
| `10-Q` | Quarterly report |
| `8-K` | Current events / material events |
| `4` | Insider buy/sell transactions |
| `144` | Restricted stock sale notice |
| `SC 13G` | Passive major shareholder (>5%) |
| `SC 13D` | Active / activist major shareholder |
| `S-1` | IPO registration statement |
| `424B4` | Prospectus |
| `20-F` | Foreign company annual report |
| `6-K` | Foreign company current report |
| `DEF 14A` | Proxy / shareholder vote statement |
| `11-K` | Employee retirement plan report |

Each form has its own form-specific prompt — Form 4 triggers insider-sentiment analysis, S-1 triggers IPO attractiveness scoring, DEF 14A triggers proxy-vote analysis, and so on. Override per form with `/setprompt`.

---

## 📱 Running on Android (Termux)

Install [Termux from F-Droid](https://f-droid.org/packages/com.termux/) — not the Play Store — then:

```bash
pkg update -y && pkg upgrade -y
pkg install -y python git tmux
git clone https://github.com/authorturker/sec-analyzer-ai.git
cd sec-analyzer-ai
pip install -r requirements.txt
cp .env.example .env && nano .env
```

Run in background with tmux:

```bash
tmux new -s sec
python bot.py
# Ctrl+B then D to detach
```

---

## 🔔 Webhook Mode (optional)

Webhook mode requires a publicly accessible HTTPS URL and Flask. On Android use [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) or [ngrok](https://ngrok.com):

```bash
pip install flask
pkg install cloudflared       # Termux
cloudflared tunnel --url localhost:5050
```

Register the tunnel URL from Telegram:

```
/setwebhook https://your-tunnel-url.trycloudflare.com
```

Restart the bot, and it switches to webhook delivery. Revert with `/delwebhook` and restart.

---

## 📊 Analysis Output Example

```text
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

📊 Risk Factor Changes (vs previous 10-Q)
➕ Added: "Increased competition from Chinese DRAM manufacturers..."
➖ Removed: "Supply chain disruptions related to legacy node capacity..."

⚠️ Could not verify against filing data: $110B, 24.6%

📈 Price action: +3.42% (2026-04-03 → 2026-04-10)

────────────────────────────
[📄 View original filing]
```

---

## 🧪 Tests

```bash
python -m pytest tests/ -q
```

```
477 passed in <1s
```

The suite covers the pure helpers (`render_filing_message`, `extract_section`, `build_prompt`, `_parse_stooq_csv`, `_compute_price_change`, `parse_sentiment_signal`, `build_trend_lines`, `build_compare_prompt`, `_format_price_check`, `_news_extract`, `_format_news_list`, `_md_escape`, `_normalize_xbrl_facts`, `format_facts_block`, `_extract_numeric_claims`, `_parse_facts_block`, `verify_numeric_claims`, …), the i18n loader (key parity, fallbacks, language switching, LLM-language hint), the config layer (snapshot isolation, atomic mutate, race protection), the thread-safe state stores, and the alarm probe (proves the hourly alarm makes no LLM calls and no cache writes). Network IO is not exercised — tests run offline.

---

## 💰 Cost

**$0.** OpenRouter free tier — 50 requests/day per model. Loading $10 credit raises the limit to 1,000/day (only drawn on paid models). Stooq price data is also free.

---

## 🔧 Troubleshooting

| Error | Fix |
|---|---|
| `EDGAR identity invalid` / startup check failed | Edit `.env` and set the flagged value (e.g. `EDGAR_IDENTITY=Real Name your@email.com`) |
| `403 Forbidden` from SEC | Same as above — SEC requires a real-looking identity |
| `No time zone found with key UTC` | `pip install tzdata` (already in `requirements.txt`) |
| `429 Too Many Requests` | Free-tier daily limit — switch model with `/setmodel` |
| `⚠️ Analysis unavailable` | OpenRouter timeout/5xx — bot retries with exponential backoff |
| `409 Conflict` on getUpdates | Two bot instances running — `pkill -f bot.py` then restart |
| `400 Bad Request` on sendMessage | Markdown parse error — bot automatically retries as plain text |
| Bot goes silent after network error | Reconnects automatically; check `/status` for error count |
| Webhook not receiving updates | Verify public HTTPS URL; run `/delwebhook` to fall back to polling |
| Wizard shows English even though I want Turkish | Step 0 of the wizard is bilingual — type `/lang tr` first |
| `/checkprice` or `/checknews` says "yfinance required" | `pip install yfinance` (optional dep, ~50 MB) |
| Alarm fires but `/check` finds nothing | Pre-v2.5 bug — update to current release (alarm is now probe-only) |
| `⚠️ Could not verify against filing data: …` appears in an analysis | The flagged figure was not found in the filing's audited XBRL data or text — treat it with caution; it may be LLM-derived (e.g., a computed total or segment share). Not necessarily wrong, just unverifiable. |
| `/pnl` shows `n/a` for a ticker | Stooq has no data for it (delisted or non-US listing) — the total skips that row |
| Watchword alert fires but nothing analyzed | By design: keyword alerts are probe-only (no LLM); run `/scanticker` on the company if you want an analysis |

---

## 📝 Release Notes

### v2.7
- **Keyword alerts (watchwords):** EDGAR full-text search (EFTS) monitoring via `/addword`; checks hourly alongside the existing filing alarm; probe-only (no LLM, no cache writes); accession-based dedup per phrase with a 200-entry FIFO cap; max 10 phrases; SEC courtesy intervals respected. State persisted in `watchword_seen.json`.
- **Portfolio P&L:** Lot-based position tracking via `/addpos`; weighted average cost across multiple lots of the same ticker; `/pnl` shows unrealized profit/loss per position using delayed/end-of-day prices from Stooq (free, no API key required); rows with unavailable prices show `n/a` and are excluded from the total — the command does not fail. Unrealized P&L only — realized P&L, dividends, and non-USD positions are out of scope.
- **+65 tests (477 total).**

### v2.6
- **Grounded Analysis:** For 10-K, 10-Q, and 20-F filings, audited XBRL facts (9 us-gaap concepts: Revenues, GrossProfit, OperatingIncomeLoss, NetIncomeLoss, EarningsPerShareDiluted, Cash, Assets, Liabilities, StockholdersEquity, plus derived gross margin %) are pulled from the filing and injected into the prompt with the instruction to use only these figures for numeric financial claims. When XBRL is unavailable the behaviour is identical to prior versions.
- **Numeric verification:** A deterministic, LLM-free post-check scans every B/M-scale monetary claim and percentage in the analysis against the XBRL facts block and the raw filing text (2% relative tolerance for money, ±1.0 point for percentages). Claims that cannot be verified against either source are listed at the end of the message with ⚠️ (at most 5 per analysis). Ranges, bare years, and EPS-scale figures are intentionally excluded from scanning to minimise false positives.
- **edgartools pin `<6.0`** — validated against v5.36.
- **+79 tests (412 total).**

### v2.5
- **🐛 Fix: alarm probe-only.** The hourly `/alarm` previously called the full scan pipeline in quiet mode — it silently consumed LLM quota, wrote to the cache, and announced an alert; then `/check` would return "no new filings" because the cache was already populated. The alarm now uses `probe_new_filings_for_watchlist()` which only asks EDGAR whether anything new exists and short-circuits on the first hit. Zero LLM calls, zero writes — the user runs `/check` manually after the alert.
- **Multi-language UI** — single `bot.py` + `lang/en.json` + `lang/tr.json`, switch with `/setlang`. Replaces the old `bot_en.py` / `bot_tr.py` split.
- **`/sentiment trend [days]`** — compare insider mood vs. N days ago, persistent history in `sentiment_history.json`.
- **`/compare AAPL MSFT [FORM]`** — side-by-side LLM comparison of two tickers' latest filings.
- **`/checkprice TICKER [days]`** — on-demand price summary via yfinance (optional dep). Default 7 days.
- **`/checknews TICKER [count]`** — recent Yahoo Finance headlines + publisher direct links via yfinance.
- **Watchlist groups** — `/addgroup`, `/removegroup`, `/listgroups`, `/scangroup`.
- **Price action snippet** — every filing analysis gets `📈 +3.4% (filing-date → +5d)` from Stooq (free, no API key). Toggle with `/priceaction`, tune with `/setlookforward`.
- **Wizard step 0 — language picker** — first-run setup now starts bilingual.
- **In-memory config cache + atomic `mutate_cfg`** — no more TOCTOU races on concurrent edits.
- **Thread-safe `_raw_filings` and `_status`** stores with helper accessors.
- **`scan_ticker` refactored** into `fetch_new_filings` / `analyze_filing` / `render_filing_message` / `send_filing_result` — testable, single-responsibility.
- **Startup validation** of `EDGAR_IDENTITY` (placeholder/empty/format) — bot refuses to run with bad identity.
- **Cache TTL** — entries older than `cache_max_age_days` (default 365) auto-pruned at startup.
- **Raw-filing cap** — `/setrawmax N` enables FIFO eviction for long-running bots.
- **MarkdownV2-safe escape** in digest snippets — characters like `*` and `_` no longer get stripped.
- **231 pytest tests** — pure helpers, i18n, config cache, thread safety, sentiment, compare, groups, price, alarm probe.
- **Auto-discover languages** — drop `lang/<code>.json` and it's picked up.
- **`build_prompt`** simplified to a dict-dispatch with form aliases.

### v2.4
- Inline original-document button, Markdown report export, webhook mode, Markdown parse fallback.

### v2.3
- Connection health monitoring + `/status`, exponential backoff on all API calls.

### v2.2
- Weekly digest, custom analysis prompts, portfolio insider sentiment.

### v2.1
- Auto scheduling, hourly filing alarm, risk-factor diff.

### v2.0
- First-run wizard, 13 form types, form-specific prompts, Telegram settings management.

### v1.x
- Initial release, OpenRouter migration, insider trading scan, smart caching.

---

## 📚 Support the Project

If you want to support this work, you can buy me a coffee.

![Bitcoin](https://img.shields.io/badge/bitcoin-2F3134?style=for-the-badge&logo=bitcoin&logoColor=white) : 178hyCd89p2QQnyUCL5y6hpzyJqu7QHz34

![Ethereum](https://img.shields.io/badge/Ethereum-3C3C3D?style=for-the-badge&logo=Ethereum&logoColor=white) : 0xf886b701d0abC89c2f59a8F98d1edF739D4b39a2

![Solana](https://img.shields.io/badge/solana-%239945FF.svg?style=for-the-badge&logo=solana&logoColor=white) : MXpoKvp1ZojjZ1fXYhgLCYfUo3R9U43jiCF8cEA1q1Y

---

## ⚠️ Disclaimer

This tool is for informational purposes only. Nothing it produces constitutes investment advice. Always do your own research before making investment decisions. The numeric verification feature only scans B/M-scale monetary figures and percentages — the absence of a ⚠️ flag does not guarantee the accuracy of any figure in the analysis. P&L figures are informational estimates based on free delayed data; not suitable for tax or trading decisions.

---

## 📄 License

MIT
