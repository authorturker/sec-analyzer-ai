# 📊 SEC Analyzer Bot

<div align="center">

**A Telegram bot that monitors SEC filings and insider trading for a configurable watchlist.**

Single-file Python, two-language UI, no cloud required. Runs on Android (Termux) or any Linux box.

[![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python&logoColor=white)](https://python.org)
[![OpenRouter](https://img.shields.io/badge/LLM-OpenRouter%20Free-6c47ff)](https://openrouter.ai)
[![Telegram](https://img.shields.io/badge/Interface-Telegram-2CA5E0?logo=telegram&logoColor=white)](https://telegram.org)
[![CI](https://github.com/authorturker/sec-analyzer-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/authorturker/sec-analyzer-ai/actions/workflows/ci.yml)
[![Tests](https://img.shields.io/badge/tests-874%20passing-success)](#-tests)
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
| 📑 **Financial Sheets** | `/sheet AAPL` — income statement + balance sheet + cash flow from EDGAR |
| 🧠 **Smart Caching** | Never re-analyzes a filing already processed; old entries auto-pruned |
| 🤖 **Guided Setup** | First-run wizard, starts with language picker — no config file editing needed |
| ⚙️ **Full Telegram Control** | Manage tickers, forms, model, language, schedule, prompts from chat |
| ⏰ **Auto Scheduling** | Daily auto-scan at a time you set + hourly filing alarm (probe-only) |
| 📊 **Weekly Digest** | Sunday summary of everything analyzed that week |
| 🔍 **Filing Diff** | Risk-factor comparison between current and previous 10-K / 10-Q |
| ✏️ **Custom Prompts** | Override the analysis prompt per form type |
| 📄 **Inline Original** | Button after each analysis to receive the raw filing as `.txt` |
| 📋 **Markdown Reports** | `/report` sends this week's full analyses as a `.md` file |
| 📤 **CSV Export** | `/export` sends the weekly log as a downloadable CSV file |
| 🔔 **Webhook Mode** | Optional — faster response, lower battery use |
| 📡 **Health Monitoring** | `/status` shows uptime, error counts, last scan/alarm times |
| 🧾 **Grounded Analysis** | 10-K/10-Q/20-F analyses are grounded in audited XBRL facts injected into the prompt |
| ✅ **Numeric Verification** | Figures in the LLM output are checked against XBRL facts and the filing text; unverifiable ones are flagged with ⚠️ |
| 🔎 **Keyword Alerts** | Watch any phrase across ALL EDGAR filings (`/addword`); hourly full-text search alert, no LLM cost |
| 💼 **Portfolio P&L** | Track positions with `/addpos`; `/pnl` shows unrealized profit/loss per ticker (yfinance, optional dep) |
| 👥 **Multi-Chat** | Share the bot with up to 5 chats (`/addchat`); each user has their own tickers, groups, watchwords, alarm, model, API keys, digest — alerts and scans are per-user |
| 🧠 **Multi-LLM** | OpenRouter · Gemini · Anthropic · Groq · DeepSeek — add keys with `/addapi`; auto-failover across providers |
| 📴 **No-AI Mode** | Without any LLM key the bot still runs: delivers raw filing text with a clear ⚠️ label; never goes silent |
| 📈 **Portfolio History** | Daily portfolio-value snapshots (up to 730 days); `/pnl` shows 1d / 7d / 30d raw-value delta |
| 📊 **Grounded Analysis** | 10-K/10-Q/20-F analyses are grounded in audited XBRL facts from EDGAR |
| 🧪 **Tested** | 720 pytest tests for pure helpers, i18n, config cache, thread safety |
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
├── Dockerfile                   # Container image (python:3.12-slim, yfinance included)
├── .dockerignore
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml               # GitHub Actions CI (Python 3.10–3.14 matrix)
├── lang/
│   ├── en.json                  # English UI strings (default)
│   └── tr.json                  # Turkish UI strings
├── tests/                       # 720 pytest tests (+ 6 opt-in live network tests)
│   ├── conftest.py
│   ├── test_alarm_buttons.py    # Interactive alarm + on-demand .md button
│   ├── test_bootstrap.py        # Minimal .env bootstrap, master-user init, env migration
│   ├── test_cfg.py              # Config cache + atomic mutate
│   ├── test_checkprice_news.py  # /checkprice + /checknews formatters
│   ├── test_groups.py           # Watchlist groups
│   ├── test_hotfixes.py         # Atomic JSON IO, wizard guard, digest week
│   ├── test_i18n.py             # Language loader, t(), fallback
│   ├── test_k2_command_surface.py # Command surface inventory; dispatcher↔help parity guard
│   ├── test_k3_pnl_visual.py   # /pnl monospace table renderer (_pnl_table)
│   ├── test_k31_qty_col.py     # QTY column clamping formatter (_fmt_qty_col)
│   ├── test_multichat.py        # Multi-chat auth, migration, broadcast
│   ├── test_multi_llm.py        # Multi-LLM provider abstraction, /addapi, /apis, retry
│   ├── test_network.py          # Opt-in live endpoint tests (--network flag)
│   ├── test_no_ai_mode.py       # No-AI mode — raw text delivery, daily reminder gate
│   ├── test_portfolio.py        # Portfolio P&L
│   ├── test_portfolio_history.py # Daily value snapshots, 1d/7d/30d delta helpers
│   ├── test_price.py            # Price compute helpers + snippet formatting
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
├── price_cache.json        # Filing price-action snippets cache
├── portfolio_history.json  # Daily portfolio-value snapshots for /pnl delta
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
cp .env.example .env    # then edit .env with your 3 required keys
python bot.py
```

On first run the bot sends a welcome message and launches a setup wizard: pick a language first (step 0 is bilingual), then add API keys one by one (type `/skip` to skip any), then choose default form types, then add tickers — done.

---

## ⚙️ Configuration

Secrets live in a `.env` file (git-ignored). Only **three** values are required to start:

```bash
cp .env.example .env
```

```ini
TELEGRAM_BOT_TOKEN=123456:ABC...              # From @BotFather
MASTER_CHAT_ID=123456789                      # Your Telegram chat ID (from @userinfobot)
EDGAR_IDENTITY=Your Name yourname@email.com   # Required by SEC (any real name + email)
```

`config.py` is a thin loader that reads these via `python-dotenv` — you never edit `config.py` itself. At startup the bot validates all three and refuses to run if any is missing or malformed.

**LLM API keys are not required to start.** Without a key the bot runs in No-AI Mode — it fetches and delivers raw filing text with a clear ⚠️ label so you always know what you are reading. Add your first key from Telegram at any time:

```
/addapi openrouter sk-or-v1-...
/addapi gemini AIza...
/addapi anthropic sk-ant-...
/addapi groq gsk_...
```

The key message is deleted from Telegram immediately after saving. You can add multiple providers; the bot tries them in order and falls back automatically if one fails.

**Migrating from an older `.env`:** if your `.env` contains `OPENROUTER_API_KEY` or `TELEGRAM_CHAT_ID`, the bot reads them on first start and migrates them into `bot_config.json` automatically — no manual action required. You can then remove those lines from `.env`.

Everything else — tickers, default forms, model, schedule, language, custom prompts, webhook URL, price-action lookforward, raw-filing cap (default 100), cache TTL — is managed live via Telegram commands and stored in `~/sec-analyzer/bot_config.json`.

**Multi-chat:** authorized chats are stored as a `chat_ids` list in `bot_config.json`. The first element is the admin; only the admin can run `/addchat`, `/removechat`, and `/listchats`. Proactive messages (alerts, scheduled scans) broadcast to all authorized chats; command replies go only to the chat that sent the command. Each authorized chat has its own independent settings: tickers, groups, watchwords, portfolio, API keys, alarm, model, schedule, price action, custom prompts, and weekly digest. Language and wizard settings are shared globally. Maximum 5 authorized chats.

---

## 💬 Commands
### Scans
| Command | Action |
|---|---|
| `/scan` | Scan watchlist with default forms |
| `/insider` | Form 4 only across watchlist |
| `/all` | SEC + Insider combined |
| `/sentiment` | Portfolio-wide insider sentiment score |
| `/sentiment trend [days]` | Compare current sentiment vs. N days ago (default 30) |
| `/scanticker AAPL` | Scan single ticker, default forms, not added to watchlist |
| `/scanticker AAPL 10-K 4` | Scan single ticker with specific forms |
| `/compare AAPL MSFT [FORM]` | Side-by-side comparison (default form: 10-K) |
| `/checkprice AAPL [days]` | Last-N-day price summary — change, open/close, high/low (default 7) |
| `/checknews AAPL [count]` | Recent Yahoo Finance headlines + publisher links (default 5, max 20) |
| `/sheet AAPL` | Financial statements from EDGAR (default: last 4 years, yearly) |
| `/sheet AAPL 2020-2023 yearly` | Financial statements for a specific year range |
| `/sheet AAPL quarterly` | Quarterly financial statements |
| `/fulltext AAPL` | Send latest filing as a `.md` file |
| `/fulltext AAPL 10-Q` | Send specific form type as `.md` |
| `/fulltext AAPL` | Send latest filing as a `.md` file |
| `/search Apple` | Search SEC database by company name |
| `/company AAPL` | Show company info from EDGAR |

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
| `/export` | Send the weekly log as a downloadable CSV file |
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

### API Keys
| Command | Action |
|---|---|
| `/addapi <provider> [key]` | Add or update a provider API key — send in a **private chat** (key is deleted from Telegram immediately); providers: `openrouter` `gemini` `anthropic` `groq` `deepseek` |
| `/apis` | Show all configured providers (masked keys) and the active LLM provider |
| `/setapi <provider>` | Set the preferred LLM provider (used first; others as fallback) |
| `/delapi <provider>` | Remove a provider's key |

### Portfolio
| Command | Action |
|---|---|
| `/addpos TICKER QTY PRICE [DATE]` | Add a lot; fractional shares OK, max 50 lots |
| `/removepos TICKER` | Remove all lots for a ticker |
| `/pnl` | Unrealized P&L summary (delayed prices via yfinance) with 1d / 7d / 30d raw-value delta |

### Multi-chat (admin only)
| Command | Action |
|---|---|
| `/addchat <id>` | Authorize a chat (max 5); group IDs are negative, e.g. `/addchat -1001234567890` |
| `/removechat <id>` | Remove a chat from the authorized list (admin cannot remove itself) |
| `/listchats` | Show all authorized chats; ⭐ marks the admin |

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

## 🧾 Optional Data Sources

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

## 🐳 Docker

Build and run the bot in a container (yfinance included — all features available):

```bash
docker build -t sec-analyzer .
docker run --env-file .env sec-analyzer
```

**Persist state across restarts** — without a volume mount the dedup/cache JSONs reset on every container restart. Mount a host directory:

```bash
docker run --env-file .env \
  -v /path/to/data:/root/sec-analyzer \
  sec-analyzer
```

> **Note:** Secrets in `.env` are never baked into the image (`.dockerignore` excludes `.env`). Always use `--env-file` at runtime.

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

## 📊 Analysis Output Examples

### SEC Filing Analysis

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

### `/pnl` Portfolio P&L

```text
📊 Portföy K/Z

TICKER   QTY    LAST    VALUE    P&L%
-------------------------------------
AAPL      15 $196.45   $2,947  +21.2%
MSFT       5 $425.20   $2,126  +11.9%
META       8 $480.00   $3,840   -7.7%
DELIST     3     n/a      n/a     n/a

Toplam: $8,913  📈 +421 (+5.1%)
1 pozisyon hariç tutuldu (fiyat alınamadı)
Δ 1d: 📈 +$42.10 (+0.47%)  ·  7d: n/a  ·  30d: n/a
```

---

## 🧪 Tests

```bash
python -m pytest tests/ -q
```

```
783 passed, 6 skipped in <3s
```

The suite covers the pure helpers (`render_filing_message`, `extract_section`, `build_prompt`, `_compute_price_change`, `parse_sentiment_signal`, `build_trend_lines`, `build_compare_prompt`, `_format_price_check`, `_news_extract`, `_format_news_list`, `_md_escape`, `_normalize_xbrl_facts`, `format_facts_block`, `_extract_numeric_claims`, `_parse_facts_block`, `verify_numeric_claims`, `_portfolio_history_delta`, `_pnl_table`, `_fmt_qty_col`, `_should_run_scheduled_scan`, `_digest_top_movers`, `_daily_news_fresh`, `_format_daily_news`, `_watchword_analyzable_hits`), the i18n loader (key parity, fallbacks, language switching, LLM-language hint), the config layer (snapshot isolation, atomic mutate, race protection), the per-chat config lock-discipline (TOCTOU flip, single-acquire proof), the chat-data purge (`_purge_chat_data` idempotency, re-add freshness), the per-chat scheduled-scan dedup (multi-chat gate, 90s boundary), the version label single-source (render correctness, placeholder leak), the multi-LLM provider abstraction (provider chain, key masking, retry logic), the No-AI mode (raw text delivery, short-circuit, daily reminder gate), the portfolio-history delta helpers, the command surface inventory (dispatcher↔help parity), and the alarm probe (proves the hourly alarm makes no LLM calls and no cache writes). Network IO is not exercised — tests run offline. The 6 skipped tests are opt-in live endpoint smoke tests; run them with `--network -m network`.

---

## 💰 Cost

**$0.** OpenRouter free tier — 50 requests/day per model. Loading $10 credit raises the limit to 1,000/day (only drawn on paid models). Price data via yfinance is also free (delayed/end-of-day).

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
| Wizard shows English even though I want Turkish | Language selection is the very first wizard step — both English and Turkish options are shown; just pick your language and the rest of the wizard continues in it |
| `/checkprice` or `/checknews` says "yfinance required" | `pip install yfinance` (optional dep, ~50 MB) |
| Alarm fires but `/check` finds nothing | Pre-v2.5 bug — update to current release (alarm is now probe-only) |
| `⚠️ Could not verify against filing data: …` appears in an analysis | The flagged figure was not found in the filing's audited XBRL data or text — treat it with caution; it may be LLM-derived (e.g., a computed total or segment share). Not necessarily wrong, just unverifiable. |
| `/pnl` shows `n/a` for a ticker | yfinance has no data for it (delisted or non-US listing) — the total skips that row; also check `pip install yfinance` |
| Watchword alert fires but nothing analyzed | By design: keyword alerts are probe-only (no LLM); run `/scanticker` on the company if you want an analysis |

---

## 📝 Release Notes

### v4.8

- **Digest top-movers line** — weekly digest P&L now includes best/worst performing position (% basis) derived from existing `priced` data; no extra IO.
- **`/dailynews on/off`** — daily (08:00) news feed for watchlist tickers; independent per-chat toggle; reuses existing yfinance news layer.
- **Watchword analyze button** — EFTS watchword alerts now include inline "analyze" buttons for valid-ticker hits; reuses existing alarm button infrastructure; CIK/adsh-only hits shown as text without buttons.
- **Test count:** 783 passing, 6 skipped (789 collected).

### v4.7

- **DEF 14A proxy analysis:** when a DEF 14A filing is encountered during scan, the bot extracts proxy data directly from EDGAR using edgartools (CEO name, total compensation, executive comp table, pay vs performance, company/peer TSR) and sends it to the user. LLM analysis is skipped for proxy statements — structured data is shown instead.
- **`/fulltext TICKER [FORM]`** — new command to send the latest filing as a `.md` file. Uses `filing.markdown()` from edgartools. Default form: 10-K.
- **DeepSeek AI provider added:** DeepSeek is now available as an LLM provider alongside OpenRouter, Groq, Anthropic, and Gemini. Models: `deepseek-v4-flash` (default), `deepseek-v4-pro`. Uses OpenAI-compatible API at `api.deepseek.com`. Add with `/addapi deepseek <key>`.
- **`_collect_8k_text` updated:** now uses `filing.markdown()` instead of `filing.text()` for richer formatting with tables and headers.
- **Per-chat config lock-discipline hardened** — `get_chat_cfg`/`mutate_chat_cfg`/`init_chat_config` now run under a single `_cfg_lock` (RLock) hold; TOCTOU windows closed.
- **Chat-removal data purge** — `/removechat` now deletes `chat_<id>.json` + `weekly_log_<id>.json`; plaintext api_keys leak and re-add stale-data resurrection fixed.
- **Per-chat scheduled-scan dedup** — same-schedule multi-chat scans no longer suppress each other (global gate moved to per-chat `last_sched_scan` dict).
- **Version label single-sourced** — all user-facing `v4.7` labels fed from `__version__` via `{version}` placeholder; no hardcoded version in lang files.
- **Test count:** 749 passing, 6 skipped (755 collected).

### v4.6

- **Fiscal AI and Twelve Data removed:** the bot now uses EDGAR XBRL as the sole financial data source. All Fiscal AI and Twelve Data API code, config keys, and lang strings have been removed.
- **`/setsource` and `/setdataapi` removed:** no longer needed — EDGAR XBRL is the only data source.
- **`/sheet` rewritten with edgartools:** now uses `company.get_financials()` from edgartools to fetch income statement, balance sheet, and cash flow data directly from EDGAR. Shows revenue, gross profit, operating income, net income, EPS, cash, assets, liabilities, equity, and cash flow items. No API key required.
- **`facts_source` config removed:** removed from both global and per-chat config.
- **`_data_source_chain()` and `_data_source_label()` removed:** no longer needed.
- **`/settings` simplified:** removed "Default Data" line (always EDGAR XBRL).
- **`/apis` simplified:** removed data providers section (fiscalai, twelvedata no longer listed).
- **Test files removed:** `test_fiscal_ai.py`, `test_l1_twelvedata.py`, `test_k4_setsource.py` no longer applicable.
- **Test count:** 720 passing, 6 skipped.

### v4.5

- **Per-user config isolation expanded:** each authorized chat now has its own independent settings for: `tickers`, `portfolio`, `custom_prompts`, `default_forms`, `alarm_on`, `price_action_enabled`, `model`, `schedule`, `api_keys`, `groups`, `weekly_digest`, and `watchwords`. Previously groups, weekly digest, and watchwords were shared globally.
- **Per-user API keys:** `/addapi` is no longer admin-only — each user can add their own LLM provider keys independently.
- **Per-user background scans:** the background thread now iterates over each authorized chat independently — scheduled scans, hourly alarms, watchword alerts, and weekly digests run per-user using that user's tickers, forms, and preferences. Messages are sent only to the relevant chat, not broadcast.
- **Per-chat weekly log:** digest, `/report`, and `/export` now use per-chat log files — each user sees only their own analyzed filings.
- **`/help` and `/status` fix:** these commands now show the requesting user's own ticker count, forms, and settings instead of the master user's.
- **`/scan` model fix:** scan now uses the requesting user's model setting instead of the global one.
- **`/setmodel` fix:** now correctly saves to per-chat config instead of global config.
- **`/addapi` access fix:** no longer restricted to admin only — all authorized users can manage their own API keys.
- **edgartools `.latest(1)` fix:** `company.get_filings(form).latest(1)` now correctly wraps a single `Filing` object in a list, fixing `'EntityFilling' object is not iterable` errors during scans.
- **Background alarm fix:** `format_alarm_alert` (undefined function) replaced with `send_alarm_alert` in the background thread.
- **Test count:** 720 passing, 6 skipped.

### v4.4

- **OpenRouter model alternatives:** `openrouter/free` and `openrouter/owl-alpha` added to the selectable model list.
- **`/sheet TICKER [year-range] [quarterly|yearly]`** — displays income statement, balance sheet, and cash flow data from EDGAR via edgartools. Default: last 4 years, yearly. Shows revenue, gross profit, operating income, net income, EPS, cash, assets, liabilities, equity, and cash flow items. No API key required.
- **`/digest now` fix:** digest now sends even when no filings were analyzed that week — still shows the P&L summary. Empty weeks display "no new filings" instead of silent no-op.
- **Digest P&L intervals:** weekly digest P&L block now shows time-based deltas: 1W, 6M, YTD, 1Y (in addition to the total line).
- **`/settings` output enhanced:** two new lines above the model: "Default AI" (active LLM provider) and "Default Data" (active grounding data source).
- **Command simplification:** natural-language keyword triggers reduced to slash-only commands:
  - `/scan` replaces `Any news?` · `Check` · `/sec` · `check` · `scan` · `sec` · `filings`
  - `/all` replaces `Check all` · `check all` · `scan all` · `everything`
  - `/insider` replaces `Insider` · `insider` · `form4` · `/form4`
  - `/sentiment` replaces `sentiment` · `sentiment score`
- **Wizard deduplication:** startup wizard messages now send only to the master chat instead of broadcasting to all authorized chats. Prevents duplicate language-selection messages.
- **Welcome message:** API-key step now includes a `/skip` hint so users know they can continue without adding a key.
- **Help block visual upgrade:** all section headers now have descriptive emojis (🔍 📊 📑 🏢 📂 📋 🔍 💹 ✏️ ⏰ 📄 🔑 💬 🌐 🔗 ⚙️).
- **`_BOT_COMMANDS` menu:** `/scan` and `/all` added to the Telegram `/` command menu (replacing the old `check` entry).
- **README command table updated:** simplified command table reflects the new slash-only triggers.

### v4.3

- **External audit triage (12 LLM auditors, 32 recommendations):** 8 accepted, 24 rejected. Key rejections: single-file architecture (deliberate Termux choice), async/SQLite/Strategy (over-engineering for single-user bot). Key acceptances implemented below.
- **`_facts_dict` FactsView fix (M1.1):** edgartools v3's FactsView path now iterates via `get_facts()` instead of silently returning None. XBRL grounding was disabled for v3 users.
- **`cmd_digest now` UX fix (M1.2):** `/digest now` now sends a confirmation message instead of silent success.
- **`get_cfg()` → `get_cfg_value()` optimization (M1.3):** 10 hot-path functions no longer trigger `copy.deepcopy()` on every call. `_is_authorized` now uses O(1) set lookup.
- **Command dispatch dict refactor (M1.4):** 42 commands moved from `elif` chain to `_CMDS` dict — single registration point.
- **Negative cache TTL (M1.5):** memo caches now expire `None` entries after 1 hour. Transient API failures no longer cause permanent data loss.
- **`retry()` on_error logging (M1.6):** Silent exception swallowing in `on_error` callbacks replaced with `log.debug`.
- **`_pending_api_key` lazy cleanup (M1.7):** Expired pending entries purged on each update processing cycle.
- **+1 i18n key** (`digest_sent` EN+TR).

### v4.2

- **Data-source chain architecture removed:** the `_data_source_chain()`, `_data_source_label()`, and `/setsource` command have been removed. EDGAR XBRL is now the sole financial data source. `/sheet` uses edgartools `Company.get_financials()` directly.

### v4.1

- **Setup wizard reordering (K1):** language selection is now the very first wizard step (step 0 is bilingual). The wizard then loops through API key entry (`/skip` to skip any provider), then form types, then tickers. The `wizard_step` config field makes the wizard restart-safe — a crash or restart resumes at the correct step. The stale `wizard_welcome` message was removed.
- **Command surface (K2):** a full inventory audit found 6 command groups missing from the help block (watchwords, portfolio, API keys, chats, `/export`, `/setrawmax`). All are now documented. `/settings` output gained three new lines: active LLM provider, all configured provider names, and the active grounding data source. A dispatcher↔help parity regression test (`test_k2_command_surface.py`) guards against future gaps.
- **`/pnl` monospace table (K3 + K3.1):** `/pnl` now renders a fixed-width table (`_pnl_table`) with aligned TICKER / QTY / LAST / VALUE / P&L% columns. The emoji summary block is separate from the table. QTY values ≥1 M render as `1.2M`; values ≥10 k render as `12.3k` — the column never overflows (`_fmt_qty_col`).
- **User-selectable facts source (K4):** the `/setsource` command and `facts_source` config have been removed. EDGAR XBRL is now the sole grounding data source.
- **+90 tests** (822 total, 7 opt-in network smoke tests).

### v4.0
> Note: the v3.x range was skipped. Leftover version labels carried forward from v2.x development had left the codebase self-identifying as an earlier version string; this release unifies all labels at v4.0 to eliminate the ambiguity.

- **Minimal `.env` bootstrap:** only three environment variables are required to start (`TELEGRAM_BOT_TOKEN`, `MASTER_CHAT_ID`, `EDGAR_IDENTITY`). LLM API keys are no longer required at launch. Existing `.env` files with `OPENROUTER_API_KEY` or `TELEGRAM_CHAT_ID` are migrated automatically on first start.
- **Multi-LLM provider abstraction:** OpenRouter, Gemini, Anthropic, and Groq are supported via pure-HTTP adapters (no new required packages). Add keys with `/addapi <provider>` in a private chat — the key message is deleted from Telegram immediately after saving. The bot tries providers in order and fails over automatically if one is unreachable. View configured keys (masked) with `/apis`; switch preference with `/setapi`.
- **No-AI Mode:** when no LLM key is configured or all providers fail, the bot delivers raw filing text with a clear ⚠️ label rather than going silent. A daily reminder prompts the admin to add a key. The filing-fetch and grounding pipeline still run — only the LLM call is skipped.
- **Portfolio value history:** daily portfolio-value snapshots are recorded to `portfolio_history.json` (up to 730 entries). `/pnl` now shows a Σ line with 1-day, 7-day, and 30-day raw-value deltas. Note: the Δ columns reflect raw total-value change and are not time-weighted — adding a position produces an apparent positive delta.
- **Fiscal AI and Twelve Data removed:** these features were subsequently removed in v4.6. EDGAR XBRL is the sole financial data source.
- **+229 tests** (732 total, 7 opt-in network smoke tests).

### v2.9
- **Multi-Chat (Model A):** Share one bot instance with up to 5 authorized Telegram chats. Proactive messages — scheduled scans, filing alerts, watchword alarms, digest — broadcast to all authorized chats. Command replies go only to the chat that sent the command. All authorized chats share the same watchlist, language, and settings (Model B with per-chat isolation is out of scope).
- **Admin commands:** `/addchat <id>` authorizes a new chat (Telegram group IDs are negative, e.g. `/addchat -1001234567890`); `/removechat <id>` removes one (admin cannot remove itself); `/listchats` shows all authorized chats with ⭐ marking the admin. Maximum 5 chats.
- **Automatic migration:** existing single-chat configs (`TELEGRAM_CHAT_ID` env var or legacy `chat_id` config key) are migrated to the new `chat_ids` list on first startup — no user action required.
- **Security:** unauthorized chats are ignored silently; the bot's existence is not revealed to unknown callers.
- **+31 tests** (`test_multichat.py`: migration, auth, broadcast isolation, context routing, admin commands, i18n parity) — **503 total**.

### v2.8
- **Git repository + MIT license:** Project initialized as a git repository with `.gitignore` covering runtime state, secrets, and internal orchestration files.
- **GitHub Actions CI:** `.github/workflows/ci.yml` runs `pytest tests/ -q` across Python 3.10, 3.11, 3.12, 3.13, and 3.14 on every push and pull request (fail-fast disabled, 10-minute timeout). CI badge added to README — badge will show green once the first push reaches GitHub.
- **Opt-in live network smoke tests:** `tests/test_network.py` adds 6 end-to-end tests against real endpoints (EDGAR, EFTS, yfinance). Skipped in all normal and CI runs; run manually with `python -m pytest tests/ -q --network -m network`. First run caught a price-endpoint HTTP 404 that prompted the price-source migration below.
- **Price source migrated to yfinance.** The previous daily CSV price endpoint was found unreachable during live smoke testing. Price data (filing price-action snippet + `/pnl`) now uses yfinance — already an optional dep for `/checkprice` and `/checknews`. yfinance remains optional — when absent, `/pnl` returns a clear message instead of silent `n/a`. Old price-fetching code removed entirely (no dead code).
- **Docker support:** `Dockerfile` (python:3.12-slim, yfinance included) and `.dockerignore` added. Run with `docker run --env-file .env sec-analyzer`; mount a volume for state persistence.
- **472 tests** (−5 deleted price-source parser tests, +6 opt-in network tests).

### v2.7
- **Keyword alerts (watchwords):** EDGAR full-text search (EFTS) monitoring via `/addword`; checks hourly alongside the existing filing alarm; probe-only (no LLM, no cache writes); accession-based dedup per phrase with a 200-entry FIFO cap; max 10 phrases; SEC courtesy intervals respected. State persisted in `watchword_seen.json`.
- **Portfolio P&L:** Lot-based position tracking via `/addpos`; weighted average cost across multiple lots of the same ticker; `/pnl` shows unrealized profit/loss per position using delayed/end-of-day prices from Stooq (free, no API key required); rows with unavailable prices show `n/a` and are excluded from the total — the command does not fail. Unrealized P&L only — realized P&L, dividends, and non-USD positions are out of scope.
- **+65 tests.**

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
- **Price action snippet** — every filing analysis gets `📈 +3.4% (filing-date → +5d)` via yfinance (optional dep). Toggle with `/priceaction`, tune with `/setlookforward`.
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
