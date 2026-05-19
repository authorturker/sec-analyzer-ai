"""
SEC Analyzer Bot v2.5 — Telegram (multi-language)

Single-codebase replacement for bot_en.py + bot_tr.py.
- i18n: lang/en.json (default) + lang/tr.json, switch with /setlang.
- Thread-safe state (raw filing store, status dict).
- scan_ticker split into pure-ish helpers (fetch / analyze / render / send).
- Includes all v2.4 features:
    8.  Connection health monitoring + /status
    9.  Smart retry with exponential backoff
    12. Markdown report export + inline original document button
"""

import copy, time, json, logging, hashlib, threading, io, uuid
from datetime import datetime, timedelta
from pathlib import Path

import re
import sys
import requests
from edgar import Company, set_identity

# Flask — for webhook mode (optional)
try:
    from flask import Flask as _Flask, request as _flask_req
    FLASK_OK = True
except ImportError:
    FLASK_OK = False

# yfinance — for /checkprice and /checknews (optional)
try:
    import yfinance as yf
    YF_OK = True
except ImportError:
    YF_OK = False

from config import (
    EDGAR_IDENTITY, OPENROUTER_API_KEY,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

# ─── Paths ────────────────────────────────────────────────
BASE_DIR    = Path.home() / "sec-analyzer"
OUTPUT_DIR  = BASE_DIR / "reports"
CACHE_FILE  = BASE_DIR / "cache.json"
CONFIG_FILE = BASE_DIR / "bot_config.json"
PREV_DIR    = BASE_DIR / "previous_filings"
WEEKLY_LOG  = BASE_DIR / "weekly_log.json"
SENT_HIST   = BASE_DIR / "sentiment_history.json"
PRICE_CACHE = BASE_DIR / "price_cache.json"
LANG_DIR    = Path(__file__).resolve().parent / "lang"
for d in [OUTPUT_DIR, PREV_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ─── Logging ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(OUTPUT_DIR / "bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ─── Threading primitives ─────────────────────────────────
_cfg_lock    = threading.RLock()    # reentrant: mutate_cfg's fn may read inside
_cache_lock  = threading.Lock()
_wlog_lock   = threading.Lock()
_status_lock  = threading.Lock()
_raw_filings_lock    = threading.Lock()
_lang_lock   = threading.Lock()
_sent_lock   = threading.Lock()     # sentiment_history.json
_price_lock  = threading.Lock()     # price_cache.json
_stop_event  = threading.Event()

# ─── Status dict (thread-safe via _status_lock) ────────────
_status = {
    "started":          datetime.now().isoformat(),
    "last_update":      None,
    "tg_errors":        0,
    "or_errors":        0,
    "last_scan":        None,
    "last_alarm":       None,
    "total_analyzed":   0,
}

def status_set(**kwargs):
    with _status_lock:
        _status.update(kwargs)

def status_inc(key: str, by: int = 1):
    with _status_lock:
        _status[key] = _status.get(key, 0) + by

def status_reset_zero(key: str):
    with _status_lock:
        _status[key] = 0

def status_snapshot() -> dict:
    with _status_lock:
        return dict(_status)

def last_scan_dt() -> datetime | None:
    """Return the last scan time as a datetime (or None) from _status."""
    iso = status_snapshot().get("last_scan")
    if not iso: return None
    try:
        return datetime.fromisoformat(iso)
    except Exception:
        return None

# ─── Raw filing store (thread-safe via _raw_filings_lock) ─────────
# Used by inline "view original filing" button callbacks.
# Python 3.7+ dicts preserve insertion order, so eviction is FIFO.
_raw_filings: dict = {}    # uuid_key → {"ticker", "form", "tarih", "metin"}

def _raw_cap() -> int:
    """Return the configured cap on _raw_filings entries (0 = unlimited)."""
    return int(get_cfg().get("raw_max", 0) or 0)

def store_raw_filing(ticker: str, form: str, date_str: str, text: str) -> str:
    """Store a raw filing and return its short uuid key.

    If raw_max > 0, oldest entries are evicted FIFO to keep the store
    within that bound. raw_max = 0 means unlimited (default).
    """
    raw_key = uuid.uuid4().hex[:16]
    cap = _raw_cap()
    with _raw_filings_lock:
        _raw_filings[raw_key] = {
            "ticker": ticker, "form": form,
            "tarih": date_str, "metin": text,
        }
        if cap > 0:
            while len(_raw_filings) > cap:
                # Pop oldest (first inserted) — FIFO eviction.
                _raw_filings.pop(next(iter(_raw_filings)))
    return raw_key

def get_raw_filing(raw_key: str) -> dict | None:
    with _raw_filings_lock:
        return _raw_filings.get(raw_key)

# Webhook state (set by main())
_webhook_active = False

# ─── Supported forms (codes only — descriptions come from lang) ─
FORMS = [
    "10-K", "10-Q", "8-K", "4", "144",
    "SC 13G", "SC 13D",
    "S-1", "424B4",
    "20-F", "6-K",
    "DEF 14A", "11-K",
]
DEFAULT_FORMS = ["10-K", "10-Q", "8-K", "4"]
DEFAULT_LANG    = "en"

def _discover_langs() -> list:
    """Return sorted list of language codes from lang/*.json filenames."""
    try:
        codes = sorted(p.stem for p in LANG_DIR.glob("*.json"))
        return codes if codes else [DEFAULT_LANG]
    except Exception as e:
        log.error(f"Lang discovery failed: {e}")
        return [DEFAULT_LANG]

SUPPORTED_LANGS = _discover_langs()

# ─── EDGAR identity validation ────────────────────────────
# SEC requires a User-Agent of form "Name email@domain.com".
_EDGAR_IDENTITY_RE = re.compile(
    r"^.+\s+[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$"
)
_EDGAR_PLACEHOLDER = "Your Name yourname@email.com"

def validate_edgar_identity(identity: str) -> tuple[bool, str]:
    """Returns (ok, message). Reject placeholder and bad formats."""
    s = (identity or "").strip()
    if not s:
        return False, "EDGAR_IDENTITY is empty in config.py."
    if s == _EDGAR_PLACEHOLDER:
        return False, ("EDGAR_IDENTITY is still the placeholder. "
                       "Edit config.py and set it to 'Your Name your@email.com'.")
    if not _EDGAR_IDENTITY_RE.match(s):
        return False, ("EDGAR_IDENTITY must be 'Name email@domain.com' "
                       f"(got: {s!r}).")
    return True, "ok"

# ─── Config (in-memory cache + atomic mutation) ───────────
# Source of truth lives in `_cfg_cache`. Disk is the persistence layer,
# read once on startup and written through on every change.
# All access goes through `get_cfg()` (read snapshot) or `mutate_cfg(fn)`
# (atomic read-modify-write under _cfg_lock) — no more TOCTOU races.

_CFG_DEFAULTS = {
    "tickers":         [],
    "default_forms":   DEFAULT_FORMS,
    "model":           "openrouter/free",
    "days_lookback":   35,
    "max_chars":       10000,
    "first_run":       True,
    "schedule":        None,
    "alarm_on":        False,
    "weekly_digest":   True,
    "custom_prompts":  {},
    "language":        DEFAULT_LANG,
    "raw_max":         0,           # 0 = unlimited raw-filing cache
    "cache_max_age_days": 365,      # auto-prune cache entries older than this
    "groups":          {},          # named ticker groups: {"tech": ["AAPL", "MSFT"]}
    "price_action_enabled":   True, # show price change after filing date
    "price_lookforward_days": 5,    # days after filing for price change measurement
}
_cfg_cache: dict | None = None

def _apply_defaults(cfg: dict) -> dict:
    """Fill missing keys with deep-copied defaults (mutating cfg in place)."""
    for k, v in _CFG_DEFAULTS.items():
        cfg.setdefault(k, copy.deepcopy(v))
    return cfg

def _load_cfg_locked() -> dict:
    """Caller must hold _cfg_lock. Returns the live cache (initialized on first call)."""
    global _cfg_cache
    if _cfg_cache is None:
        try:
            data = json.loads(CONFIG_FILE.read_text())
        except Exception:
            data = {}
        _cfg_cache = _apply_defaults(data if isinstance(data, dict) else {})
    return _cfg_cache

def _save_cfg_locked():
    """Caller must hold _cfg_lock. Persist cache to disk."""
    if _cfg_cache is not None:
        CONFIG_FILE.write_text(json.dumps(_cfg_cache, indent=2))

def get_cfg() -> dict:
    """Return a deep copy of the config. Safe to read or mutate locally
    without affecting the canonical state (use mutate_cfg to persist changes)."""
    with _cfg_lock:
        return copy.deepcopy(_load_cfg_locked())

def mutate_cfg(fn) -> dict:
    """
    Atomic read-modify-write. `fn` receives the live cache dict and may
    mutate it in place; the new state is persisted before the lock releases.
    Returns a deep copy of the new state for inspection.

    Usage:
        mutate_cfg(lambda c: c["tickers"].append("AAPL"))
    """
    with _cfg_lock:
        cfg = _load_cfg_locked()
        fn(cfg)
        _save_cfg_locked()
        return copy.deepcopy(cfg)

def update_cfg(**kwargs) -> dict:
    """Set top-level keys atomically. Convenience wrapper over mutate_cfg."""
    return mutate_cfg(lambda c: c.update(kwargs))

# ─── i18n ─────────────────────────────────────────────────
_lang_cache: dict = {}        # code → full strings dict
_current_lang: str | None = None   # cached active language code

def _load_lang(code: str) -> dict:
    """Load and cache lang/<code>.json. Empty dict on failure."""
    with _lang_lock:
        if code in _lang_cache:
            return _lang_cache[code]
        p = LANG_DIR / f"{code}.json"
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Lang load failed ({code}): {e}")
            data = {}
        _lang_cache[code] = data
        return data

def get_lang() -> str:
    """Return active language code, cached."""
    global _current_lang
    if _current_lang is None:
        _current_lang = get_cfg().get("language", DEFAULT_LANG)
        if _current_lang not in SUPPORTED_LANGS:
            _current_lang = DEFAULT_LANG
    return _current_lang

def set_lang(code: str):
    """Update active language (memory + config)."""
    global _current_lang
    _current_lang = code
    update_cfg(language=code)

def lang_meta() -> dict:
    return _load_lang(get_lang()).get("_meta", {})

def t(key: str, **kwargs) -> str:
    """Lookup translated string. Falls back to English, then to the key itself."""
    code = get_lang()
    s = _load_lang(code).get(key)
    if s is None and code != DEFAULT_LANG:
        s = _load_lang(DEFAULT_LANG).get(key)
    if s is None:
        s = key
    try:
        return s.format(**kwargs) if kwargs else s
    except (KeyError, IndexError) as e:
        log.error(f"t({key}) format error: {e}")
        return s

def form_desc(form: str) -> str:
    """Localized description for a form code."""
    return t(f"form_desc_{form}")

# ─── Cache ────────────────────────────────────────────────
# Cache layout: {md5(ticker_form_date): {"at": iso_timestamp}}.
# `cache_max_age_days` (default 365) caps how long an entry survives;
# expired entries are pruned at startup by `prune_cache_expired()`.
CACHE_DEFAULT_MAX_AGE_DAYS = 365

def load_cache() -> dict:
    with _cache_lock:
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            return {}

def save_cache(c: dict):
    with _cache_lock:
        CACHE_FILE.write_text(json.dumps(c, indent=2))

def cache_key(*args) -> str:
    return hashlib.md5("_".join(str(a) for a in args).encode()).hexdigest()

def is_new_in_cache(ob, *args) -> bool:
    return cache_key(*args) not in ob

def mark_processed(ob, *args):
    ob[cache_key(*args)] = {"at": datetime.now().isoformat()}

# ─── Price action (E1) ────────────────────────────────────
# Uses Stooq's free daily CSV endpoint — no API key, no extra deps.
# Layout of price_cache.json:
#   {"AAPL_2026-04-01_5": {start_date, start_close, end_date, end_close, pct}}
# Cache key bakes in the lookforward so changing the config never serves stale.

_STOOQ_URL = "https://stooq.com/q/d/l/?s={ticker}.us&i=d&d1={d1}&d2={d2}"

def _parse_stooq_csv(text: str) -> list:
    """PURE: parse Stooq daily CSV → sorted list of (YYYY-MM-DD, close)."""
    if not text or text.strip().lower().startswith("no data"):
        return []
    lines = text.strip().split("\n")
    if len(lines) < 2:
        return []
    rows = []
    for ln in lines[1:]:
        cols = ln.split(",")
        if len(cols) < 5:
            continue
        try:
            close = float(cols[4])
        except (ValueError, IndexError):
            continue
        rows.append((cols[0], close))
    rows.sort()
    return rows

def _compute_price_change(rows: list, filing_date: str,
                          lookforward_days: int) -> dict | None:
    """
    PURE: given parsed Stooq rows, find close on/after filing_date and close on
    or after filing_date + lookforward_days. Return change dict or None.
    """
    if not rows:
        return None
    try:
        filing_dt = datetime.fromisoformat(filing_date)
    except Exception:
        return None
    target_str = (filing_dt + timedelta(days=lookforward_days)).strftime("%Y-%m-%d")
    start = next(((d, c) for d, c in rows if d >= filing_date), None)
    if start is None:
        return None
    end = next(((d, c) for d, c in rows if d >= target_str), None)
    if end is None:
        end = rows[-1]
    if end[0] == start[0]:
        return None
    pct = (end[1] - start[1]) / start[1] * 100.0
    return {
        "start_date":  start[0], "start_close": start[1],
        "end_date":    end[0],   "end_close":   end[1],
        "pct":         pct,
    }

def _format_price_snippet(change: dict | None) -> str:
    """PURE: render the i18n price snippet line. Empty if no data."""
    if not change:
        return ""
    emoji = "📈" if change["pct"] >= 0 else "📉"
    return t("price_action_line",
             emoji=emoji,
             pct=f"{change['pct']:+.2f}",
             start_date=change["start_date"],
             end_date=change["end_date"])

def fetch_stooq_daily(ticker: str, start_date: str, end_date: str) -> str | None:
    """IO: fetch Stooq daily CSV for a ticker over a date range."""
    url = _STOOQ_URL.format(
        ticker=ticker.lower(),
        d1=start_date.replace("-", ""),
        d2=end_date.replace("-", ""),
    )
    try:
        r = requests.get(url, timeout=15)
        if not r.ok:
            return None
        return r.text
    except Exception as e:
        log.error(f"Stooq fetch {ticker}: {e}")
        return None

def load_price_cache() -> dict:
    with _price_lock:
        try:
            return json.loads(PRICE_CACHE.read_text())
        except Exception:
            return {}

def save_price_cache(data: dict):
    with _price_lock:
        PRICE_CACHE.write_text(json.dumps(data, indent=2))

def compute_price_snippet(ticker: str, filing_date: str) -> str:
    """
    IO: orchestrate cache → fetch → parse → format. Returns "" on disable,
    cache-miss-with-fetch-fail, or unparseable response.
    """
    cfg = get_cfg()
    if not cfg.get("price_action_enabled", True):
        return ""
    try:
        lookforward = int(cfg.get("price_lookforward_days", 5))
    except (ValueError, TypeError):
        lookforward = 5

    cache = load_price_cache()
    cache_k = f"{ticker}_{filing_date}_{lookforward}"
    if cache_k in cache:
        return _format_price_snippet(cache[cache_k])

    # Pad the window so we still find data even if filing fell on a weekend.
    try:
        end_dt = datetime.fromisoformat(filing_date) + timedelta(days=lookforward + 14)
    except Exception:
        return ""
    csv = fetch_stooq_daily(ticker, filing_date, end_dt.strftime("%Y-%m-%d"))
    if not csv:
        return ""
    change = _compute_price_change(_parse_stooq_csv(csv), filing_date, lookforward)
    if not change:
        return ""
    cache[cache_k] = change
    save_price_cache(cache)
    return _format_price_snippet(change)

# ─── /checkprice + /checknews (E5, yfinance-backed) ───────
# yfinance is optional — if not installed, both commands return a clear
# error message instead of crashing. All formatting helpers are PURE
# so they can be unit-tested with synthetic data (no network).

def _format_price_check(ticker: str, days: int, rows: list) -> str:
    """
    PURE: render the /checkprice block.
    rows: list[(date_str, open, high, low, close)] sorted ascending.
    """
    if not rows:
        return t("checkprice_no_data", ticker=ticker, days=days)
    first = rows[0]
    last  = rows[-1]
    start_open = first[1]
    end_close  = last[4]
    pct = ((end_close - start_open) / start_open * 100.0) if start_open else 0.0
    emoji = "📈" if pct >= 0 else "📉"
    high_all = max(r[2] for r in rows)
    low_all  = min(r[3] for r in rows)
    return t("checkprice_block",
             ticker=ticker, days=days, count=len(rows),
             emoji=emoji, pct=f"{pct:+.2f}",
             start_date=first[0], start_open=f"{start_open:.2f}",
             end_date=last[0],    end_close=f"{end_close:.2f}",
             high=f"{high_all:.2f}", low=f"{low_all:.2f}")

def _news_extract(item: dict) -> dict:
    """PURE: normalize one yfinance news item into a flat dict."""
    c = (item or {}).get("content") or {}
    canonical = (c.get("canonicalUrl") or {}).get("url")
    fallback  = (c.get("clickThroughUrl") or {}).get("url")
    return {
        "title":     c.get("title")    or "(no title)",
        "url":       canonical or fallback or "",
        "provider":  (c.get("provider") or {}).get("displayName", ""),
        "date":      (c.get("pubDate")  or "")[:10],   # YYYY-MM-DD
    }

def _format_news_list(ticker: str, items: list, count: int) -> str:
    """PURE: render the /checknews block. items: raw yfinance news list."""
    if not items:
        return t("checknews_no_data", ticker=ticker)
    items = items[:count]
    lines = [t("checknews_header", ticker=ticker, count=len(items))]
    for raw in items:
        n = _news_extract(raw)
        lines.append(t("checknews_item",
                       title=n["title"], url=n["url"],
                       provider=n["provider"], date=n["date"]))
    return "\n".join(lines)

def fetch_yfinance_history(ticker: str, days: int) -> list | None:
    """IO: returns rows sorted ascending or None on failure."""
    if not YF_OK:
        return None
    try:
        h = yf.Ticker(ticker).history(period=f"{days}d")
        if h is None or h.empty:
            return []
        rows = []
        for idx, r in h.iterrows():
            rows.append((
                idx.strftime("%Y-%m-%d"),
                float(r["Open"]), float(r["High"]),
                float(r["Low"]),  float(r["Close"]),
            ))
        return rows
    except Exception as e:
        log.error(f"yfinance history {ticker}: {e}")
        return None

def fetch_yfinance_news(ticker: str) -> list | None:
    """IO: returns raw news list or None on failure."""
    if not YF_OK:
        return None
    try:
        return yf.Ticker(ticker).news or []
    except Exception as e:
        log.error(f"yfinance news {ticker}: {e}")
        return None

def cmd_checkprice(parts: list):
    """Usage: /checkprice TICKER [days]   — default 7 days, range 1-365."""
    if len(parts) < 2:
        tg(t("checkprice_usage")); return
    if not YF_OK:
        tg(t("yfinance_missing", cmd="/checkprice")); return
    ticker = parts[1].upper().strip()
    days = 7
    if len(parts) >= 3:
        try:
            n = int(parts[2])
            if 1 <= n <= 365:
                days = n
        except ValueError:
            pass
    rows = fetch_yfinance_history(ticker, days)
    if rows is None:
        tg(t("checkprice_fetch_error", ticker=ticker)); return
    tg(_format_price_check(ticker, days, rows))

def cmd_checknews(parts: list):
    """Usage: /checknews TICKER [count]   — default 5 headlines, max 20."""
    if len(parts) < 2:
        tg(t("checknews_usage")); return
    if not YF_OK:
        tg(t("yfinance_missing", cmd="/checknews")); return
    ticker = parts[1].upper().strip()
    count = 5
    if len(parts) >= 3:
        try:
            n = int(parts[2])
            if 1 <= n <= 20:
                count = n
        except ValueError:
            pass
    items = fetch_yfinance_news(ticker)
    if items is None:
        tg(t("checknews_fetch_error", ticker=ticker)); return
    tg(_format_news_list(ticker, items, count))

# ─── Sentiment history (E3) ───────────────────────────────
# Layout: {"AAPL": [{"date": "YYYY-MM-DD", "label": "bullish|bearish|neutral|unknown",
#                    "emoji": "📈", "raw": "<full signal line>"}], ...}
# Used by /sentiment trend to compare current vs N-day-ago sentiment.

_SENT_LABELS = {
    "bullish":  {"emoji": "📈", "needles": ("📈", "bullish", "yükseliş", "yukselis")},
    "bearish":  {"emoji": "📉", "needles": ("📉", "bearish", "düşüş", "dusus")},
    "neutral":  {"emoji": "➡️", "needles": ("➡️", "neutral", "nötr", "notr")},
}

def parse_sentiment_signal(raw: str) -> tuple[str, str]:
    """Extract (label, emoji) from a one-line LLM sentiment signal.

    label is one of: 'bullish', 'bearish', 'neutral', 'unknown'.
    Robust against language and ordering — uses needle search, not regex.
    """
    lower = raw.lower()
    for label, info in _SENT_LABELS.items():
        for needle in info["needles"]:
            if needle.lower() in lower:
                return label, info["emoji"]
    return "unknown", "❔"

def load_sentiment_history() -> dict:
    with _sent_lock:
        try:
            return json.loads(SENT_HIST.read_text())
        except Exception:
            return {}

def save_sentiment_history(data: dict):
    with _sent_lock:
        SENT_HIST.write_text(json.dumps(data, indent=2, ensure_ascii=False))

def append_sentiment(ticker: str, raw: str, on_date: str | None = None):
    """Append one signal observation for a ticker. on_date defaults to today (YYYY-MM-DD)."""
    label, emoji = parse_sentiment_signal(raw)
    on_date = on_date or datetime.now().strftime("%Y-%m-%d")
    data = load_sentiment_history()
    data.setdefault(ticker, []).append({
        "date": on_date, "label": label, "emoji": emoji, "raw": raw.strip(),
    })
    save_sentiment_history(data)

def prune_cache_expired(max_age_days: int | None = None) -> int:
    """Remove cache entries older than `max_age_days`. Returns count pruned.

    Pure-ish: only touches CACHE_FILE through the cache locks. Safe to call
    at startup; no-op when cache is empty or all entries are fresh.
    """
    if max_age_days is None:
        max_age_days = int(get_cfg().get("cache_max_age_days",
                                        CACHE_DEFAULT_MAX_AGE_DAYS))
    if max_age_days <= 0:
        return 0
    cutoff = datetime.now() - timedelta(days=max_age_days)
    cache  = load_cache()
    if not cache:
        return 0
    to_drop = []
    for k, v in cache.items():
        iso = (v or {}).get("at")
        if not iso:
            continue
        try:
            if datetime.fromisoformat(iso) < cutoff:
                to_drop.append(k)
        except Exception:
            # Corrupt timestamp — drop it
            to_drop.append(k)
    if to_drop:
        for k in to_drop:
            cache.pop(k, None)
        save_cache(cache)
    return len(to_drop)

# ─── Weekly log (for digest / report) ─────────────────────
def log_weekly(ticker: str, form: str, date_str: str, analysis: str):
    """Append a full analysis to the weekly log. Truncation happens at digest time."""
    with _wlog_lock:
        try:
            data = json.loads(WEEKLY_LOG.read_text()) if WEEKLY_LOG.exists() else []
        except Exception:
            data = []
        data.append({
            "ticker": ticker, "form": form, "tarih": date_str,
            "analiz": analysis,
            "ekleme": datetime.now().isoformat(),
        })
        WEEKLY_LOG.write_text(json.dumps(data, indent=2))

def get_weekly_log() -> list:
    with _wlog_lock:
        try:
            return json.loads(WEEKLY_LOG.read_text()) if WEEKLY_LOG.exists() else []
        except Exception:
            return []

def clear_weekly_log():
    with _wlog_lock:
        WEEKLY_LOG.write_text(json.dumps([], indent=2))

# ─── Previous filing storage (for risk diff) ──────────────
def prev_path(ticker: str, form: str) -> Path:
    return PREV_DIR / f"{ticker}_{form.replace(' ','_')}.txt"

def save_prev(ticker: str, form: str, text: str):
    try:
        prev_path(ticker, form).write_text(text[:50000], encoding="utf-8")
    except Exception as e:
        log.error(f"save_prev error {ticker} {form}: {e}")

def load_prev(ticker: str, form: str) -> str | None:
    p = prev_path(ticker, form)
    try:
        return p.read_text(encoding="utf-8") if p.exists() else None
    except Exception:
        return None

# ─── Telegram ─────────────────────────────────────────────
_TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg(text: str):
    """Send a Telegram message (chunked, exponential backoff, Markdown fallback)."""
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            parts.append(current); current = line
        else:
            current += "\n" + line
    if current.strip(): parts.append(current)
    for part in parts:
        for attempt in range(4):
            try:
                r = requests.post(f"{_TG}/sendMessage", json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": part.strip(),
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": True,
                }, timeout=15)
                if r.status_code == 429:
                    wait_sec = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
                    log.warning(f"Telegram flood control — waiting {wait_sec}s")
                    time.sleep(wait_sec); continue
                if r.status_code == 400:
                    # Markdown invalid — retry as plain text
                    log.warning("Markdown parse error — retrying as plain text")
                    r2 = requests.post(f"{_TG}/sendMessage", json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": part.strip(),
                        "disable_web_page_preview": True,
                    }, timeout=15)
                    if r2.ok:
                        status_reset_zero("tg_errors")
                    break
                r.raise_for_status()
                status_reset_zero("tg_errors")
                break
            except Exception as e:
                status_inc("tg_errors")
                wait_sec = min(60, 2 ** attempt * 3)
                log.error(f"Telegram (attempt {attempt+1}): {e} — waiting {wait_sec}s")
                time.sleep(wait_sec)
        time.sleep(0.3)

def tg_send_document(filename: str, content: str, caption: str = ""):
    """Send a document over Telegram (no temp file, BytesIO)."""
    url  = f"{_TG}/sendDocument"
    data = content.encode("utf-8")
    try:
        r = requests.post(url, data={
            "chat_id": TELEGRAM_CHAT_ID,
            "caption": caption[:1024] if caption else "",
            "parse_mode": "Markdown",
        }, files={
            "document": (filename, io.BytesIO(data), "text/plain"),
        }, timeout=30)
        if not r.ok:
            log.error(f"Document send error: {r.status_code} {r.text[:200]}")
    except Exception as e:
        log.error(f"tg_send_document: {e}")

def tg_answer_callback(callback_id: str, text: str = ""):
    """Acknowledge an inline-button callback (Telegram requirement)."""
    try:
        requests.post(f"{_TG}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": text,
        }, timeout=10)
    except Exception as e:
        log.error(f"answerCallbackQuery: {e}")

def build_inline_button(raw_key: str) -> dict:
    """Returns inline keyboard with 'view original filing' button."""
    return {
        "inline_keyboard": [[{
            "text": t("view_original_button"),
            "callback_data": f"raw:{raw_key}",
        }]]
    }

def tg_with_button(text: str, raw_key: str):
    """Send a message together with an inline button on the last chunk."""
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            parts.append(current); current = line
        else:
            current += "\n" + line
    if current.strip(): parts.append(current)

    for i, part in enumerate(parts):
        payload: dict = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": part.strip(),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if i == len(parts) - 1:
            payload["reply_markup"] = build_inline_button(raw_key)
        try:
            r = requests.post(f"{_TG}/sendMessage", json=payload, timeout=15)
            if r.status_code == 400:
                payload.pop("parse_mode", None)
                r2 = requests.post(f"{_TG}/sendMessage", json=payload, timeout=15)
                r2.raise_for_status()
            else:
                r.raise_for_status()
        except Exception as e:
            log.error(f"tg_with_button: {e}")
        time.sleep(0.3)

def send_raw_filing(callback_id: str, raw_key: str):
    """Send the stored raw filing as a .txt document."""
    tg_answer_callback(callback_id, t("preparing_document"))
    entry_data = get_raw_filing(raw_key)
    if not entry_data:
        tg(t("raw_filing_not_found"))
        return
    filename = (f"{entry_data['ticker']}_{entry_data['form'].replace(' ','_')}"
                 f"_{entry_data['tarih']}.txt")
    caption  = t("raw_filing_caption",
                  ticker=entry_data['ticker'], form=entry_data['form'], date=entry_data['tarih'])
    tg_send_document(filename, entry_data["metin"], caption)
    log.info(f"Raw filing sent: {filename}")

def send_md_analysis(ticker: str, form: str, date_str: str,
                     analysis: str, diff: str):
    """Send analysis as a .md file."""
    content = f"# {ticker} — {form} ({date_str})\n\n{analysis}\n"
    if diff:
        content += f"\n{t('md_diff_header')}\n\n{diff}\n"
    filename = f"{ticker}_{form.replace(' ','_')}_{date_str}_analysis.md"
    caption  = t("md_caption", ticker=ticker, form=form, date=date_str)
    tg_send_document(filename, content, caption)

# ─── Webhook mode ─────────────────────────────────────────
def register_webhook(url: str) -> bool:
    try:
        r = requests.post(f"{_TG}/setWebhook", json={
            "url": url,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=15)
        r.raise_for_status()
        log.info(f"Webhook registered: {url}")
        return True
    except Exception as e:
        log.error(f"Webhook register error: {e}")
        return False

def delete_webhook() -> bool:
    try:
        r = requests.post(f"{_TG}/deleteWebhook", timeout=15)
        r.raise_for_status()
        log.info("Webhook deleted — switched to polling.")
        return True
    except Exception as e:
        log.error(f"Webhook delete error: {e}")
        return False

def start_webhook_server(port: int, handle_update):
    if not FLASK_OK:
        log.error("Flask not installed. Run `pip install flask`.")
        return
    app = _Flask(__name__)

    @app.route("/webhook", methods=["POST"])
    def webhook_al():
        update = _flask_req.get_json(force=True)
        if update:
            handle_update(update)
        return "ok", 200

    log.info(f"Webhook server starting on port {port}...")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

def cmd_setwebhook(parts: list) -> str:
    if len(parts) < 2:
        return t("setwebhook_usage")
    url = parts[1].rstrip("/") + "/webhook"
    result = register_webhook(url)
    if result:
        update_cfg(webhook_url=url, webhook_aktif=True)
        return t("webhook_registered", url=url)
    return t("webhook_register_failed")

def cmd_delwebhook() -> str:
    delete_webhook()
    update_cfg(webhook_url=None, webhook_aktif=False)
    return t("webhook_deleted")

def get_updates(offset: int) -> list:
    """getUpdates — exponential backoff retry."""
    for attempt in range(4):
        try:
            r = requests.get(f"{_TG}/getUpdates",
                             params={"offset": offset, "timeout": 30}, timeout=35)
            r.raise_for_status()
            status_set(last_update=datetime.now().isoformat())
            status_reset_zero("tg_errors")
            return r.json().get("result", [])
        except Exception as e:
            status_inc("tg_errors")
            wait_sec = min(120, 5 * 2 ** attempt)
            log.error(f"getUpdates (attempt {attempt+1}): {e} — waiting {wait_sec}s")
            time.sleep(wait_sec)
    return []

# ─── OpenRouter LLM ───────────────────────────────────────
_OR  = "https://openrouter.ai/api/v1/chat/completions"
_ORH = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/sec-analyzer-bot",
    "X-Title": "SEC Analyzer Bot",
}

def system_message() -> str:
    """Build the LLM system message, including the active response language."""
    lang_name = lang_meta().get("llm_response_language", "English")
    return (
        "You are an experienced financial analyst specializing in SEC filings. "
        "Analyze documents from an investor's perspective. Be concise, structured, "
        "use bullet points. Highlight key risks and opportunities. Use emojis. "
        f"Respond in {lang_name}."
    )

def llm(istem: str, model: str) -> str:
    """OpenRouter LLM call — exponential backoff retry."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_message()},
            {"role": "user",   "content": istem},
        ],
        "max_tokens": 1200, "temperature": 0.2,
    }
    for attempt in range(4):
        try:
            r = requests.post(_OR, headers=_ORH, json=payload, timeout=120)
            if r.status_code == 429:
                wait_sec = min(180, 60 * (attempt + 1))
                log.warning(f"Rate limit — waiting {wait_sec}s"); time.sleep(wait_sec); continue
            if r.status_code in (500, 502, 503, 504):
                wait_sec = min(60, 10 * 2 ** attempt)
                log.warning(f"OpenRouter {r.status_code} — waiting {wait_sec}s"); time.sleep(wait_sec); continue
            r.raise_for_status()
            status_reset_zero("or_errors")
            return r.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.Timeout:
            wait_sec = min(60, 15 * (attempt + 1))
            log.error(f"LLM timeout (attempt {attempt+1}) — waiting {wait_sec}s"); time.sleep(wait_sec)
        except Exception as e:
            status_inc("or_errors")
            wait_sec = min(60, 10 * 2 ** attempt)
            log.error(f"LLM error (attempt {attempt+1}): {e} — waiting {wait_sec}s"); time.sleep(wait_sec)
    return t("analysis_unavailable")

# ─── Section extraction ───────────────────────────────────
_SECTION_KEYWORDS = {
    "10-K": ["item 1.", "item 1a.", "item 7.", "item 8."],
    "10-Q": ["item 1.", "item 2.", "item 3."],
}

def extract_section(text: str, form: str, max_k: int) -> str:
    kw = _SECTION_KEYWORDS.get(form, [])
    if not kw: return text[:max_k]
    lines, active, chars, output = text.split("\n"), False, 0, []
    for line in lines:
        if any(line.lower().strip().startswith(k) for k in kw): active = True
        if active:
            output.append(line); chars += len(line)
            if chars >= max_k: output.append("[text truncated]"); break
    return "\n".join(output) if output else text[:max_k]

# ─── Filing diff ──────────────────────────────────────────
def _risk_section(text: str) -> str:
    lines = text.lower().split("\n")
    active, output, chars = False, [], 0
    for s in lines:
        if "item 1a" in s or "risk factor" in s: active = True
        if active:
            output.append(s); chars += len(s)
            if chars > 8000: break
        if active and ("item 1b" in s or "item 2." in s): break
    return "\n".join(output)

def diff_analysis(ticker: str, form: str, yeni_metin: str, model: str,
                  previous: str | None = None) -> str:
    if form not in ("10-K", "10-Q"): return ""
    if previous is None:
        previous = load_prev(ticker, form)
    if not previous: return ""
    previous_risk = _risk_section(previous)
    yeni_risk   = _risk_section(yeni_metin)
    if not previous_risk or not yeni_risk: return ""
    if previous_risk[:500] == yeni_risk[:500]:
        return t("no_significant_risk_changes")
    return llm(
        f"{ticker} — {form} Risk Factor Comparison\n\n"
        f"PREVIOUS FILING:\n{previous_risk[:4000]}\n\n"
        f"NEW FILING:\n{yeni_risk[:4000]}\n\n"
        "Compare:\n"
        "1. ➕ Newly added risk factors\n"
        "2. ➖ Removed risk factors\n"
        "3. ✏️ Materially modified language\n"
        "4. 🎯 Significance for investors",
        model
    )

# ─── Analysis prompts (English; LLM responds in active language) ───
# Form-specific prompt templates. Custom prompts override these.
# To add or tweak a form, edit one entry — no if/elif gymnastics.
_PROMPT_DEFAULT = (
    "Analyze:\n"
    "1. 📣 What happened? (summary)\n"
    "2. 📈 Impact on stock / business\n"
    "3. 🚨 Does it require urgent attention?"
)
PROMPTS: dict[str, str] = {
    "10-K": (
        "Analyze:\n"
        "1. 📌 Business model and competitive advantage\n"
        "2. ⚠️ Top 3 critical risks\n"
        "3. 💰 Financial highlights (revenue, margins, growth)\n"
        "4. 🔭 Management's 12-month outlook\n"
        "5. 🎯 Investor decision: BUY / HOLD / CAUTION"
    ),
    "10-Q": (
        "Analyze:\n"
        "1. 📊 Quarter performance (compare with previous)\n"
        "2. 🔑 3 key messages from management\n"
        "3. ⚠️ Notable changes\n"
        "4. 👀 3 factors to watch for the next quarter"
    ),
    "4": (
        "Analyze:\n"
        "1. 👤 Who transacted? (name and position)\n"
        "2. 📈 Buy or sell? Volume and estimated value\n"
        "3. 🔍 Insider sentiment: Bullish / Bearish / Neutral\n"
        "4. 💡 What it means for investors"
    ),
    "SC 13G": (
        "Analyze:\n"
        "1. 🏦 Who acquired a major stake?\n"
        "2. 📊 Ownership percentage and change\n"
        "3. 🎯 Passive or active (activist)?\n"
        "4. 💡 Implications for retail investors"
    ),
    "S-1": (
        "Analyze:\n"
        "1. 📌 Company summary and IPO rationale\n"
        "2. 💰 Use of proceeds\n"
        "3. ⚠️ Material risk factors\n"
        "4. 🎯 Investor attractiveness: HIGH / MEDIUM / LOW"
    ),
    "DEF 14A": (
        "Analyze:\n"
        "1. 🗳️ Key votes on the agenda\n"
        "2. 💼 Executive compensation — reasonable?\n"
        "3. ⚠️ Controversial proposals?\n"
        "4. 💡 Recommended stance for shareholders"
    ),
}
# Aliases — share the same prompt for closely related forms.
PROMPTS["144"]    = PROMPTS["4"]
PROMPTS["SC 13D"] = PROMPTS["SC 13G"]
PROMPTS["424B4"]  = PROMPTS["S-1"]

def build_prompt(ticker: str, form: str, date_str: str, body: str,
                 custom_prompt: str = "") -> str:
    base = f"{ticker} — {form} ({date_str})\n\n{body}\n\n"
    return base + (custom_prompt or PROMPTS.get(form, _PROMPT_DEFAULT))

# ═══════════════════════════════════════════════════════════
# Refactored scan pipeline — small composable functions.
# Each step has a clear input/output contract for testability.
# ═══════════════════════════════════════════════════════════

def fetch_new_filings(ticker: str, forms: list, lookback_days: int,
                      cache_dict: dict | None = None,
                      use_cache: bool = False,
                      quiet: bool = False,
                      *,
                      n_latest: int = 1,
                      max_chars_per: int | None = None) -> list:
    """
    IO: fetch the latest filings from SEC EDGAR for `ticker` × `formlar`,
    filter by lookback window and (optionally) the cache.

    Args:
        ticker:           Stock ticker symbol.
        formlar:          List of form codes to fetch.
        lookback_days:    Filter out filings older than this many days.
        cache_dict:         Cache dict (used only when use_cache=True).
        use_cache:  If True, skip filings already present in cache.
        quiet:           If True, suppress error messages sent to chat.
        n_latest:         How many recent filings per form to fetch (kw-only).
        max_chars_per:    Optional per-filing text truncation (kw-only).

    Returns: list of (form, date_str, raw_text) tuples, in fetch order.
             Empty list on miss or hard failure. set_identity is called once
             at startup in main(); not re-called here.
    """
    cutoff = datetime.now() - timedelta(days=lookback_days)
    found: list = []

    for edgar_deneme in range(3):
        try:
            company = Company(ticker)
            for form in forms:
                for form_deneme in range(3):
                    try:
                        result = company.get_filings(form=form).latest(n_latest)
                        if not result: break
                        items = (list(result) if hasattr(result, "__iter__")
                                 else [result])
                        for f in items:
                            d = f.filing_date
                            if hasattr(d, "date"): d = d.date()
                            ds = str(d)
                            if datetime.combine(d, datetime.min.time()) < cutoff:
                                continue
                            if (use_cache and cache_dict is not None
                                    and not is_new_in_cache(cache_dict, ticker, form, ds)):
                                continue
                            text = f.text()
                            if max_chars_per is not None:
                                text = text[:max_chars_per]
                            found.append((form, ds, text))
                            time.sleep(0.5)
                        break
                    except Exception as e:
                        wait_sec = min(30, 5 * 2 ** form_deneme)
                        log.error(f"{ticker} {form} (attempt {form_deneme+1}): {e} — waiting {wait_sec}s")
                        time.sleep(wait_sec)
            return found
        except Exception as e:
            wait_sec = min(60, 10 * 2 ** edgar_deneme)
            log.error(f"{ticker} Company (attempt {edgar_deneme+1}): {e} — waiting {wait_sec}s")
            if edgar_deneme == 2:
                if not quiet:
                    tg(t("ticker_not_found", ticker=ticker, error=str(e)))
                return []
            time.sleep(wait_sec)
    return []


def analyze_filing(ticker: str, form: str, date_str: str, text: str,
                   max_chars: int, model: str,
                   custom_prompts: dict) -> tuple[str, str]:
    """
    IO: call the LLM. Pure with respect to caller state.

    Returns: (analysis, diff). diff is "" for non-10-K/10-Q or no prior filing.
    """
    body = extract_section(text, form, max_chars)
    custom  = custom_prompts.get(form, "")
    analysis = llm(build_prompt(ticker, form, date_str, body, custom), model)
    diff   = diff_analysis(ticker, form, text, model)
    return analysis, diff


def render_filing_message(ticker: str, form: str, date_str: str,
                          analysis: str, diff: str,
                          price_snippet: str = "") -> str:
    """
    PURE: build the Telegram message body for a single filing analysis.
    Optional `price_snippet` (from E1 price action) is appended above the
    separator if non-empty. No IO, no globals — easy to unit-test.
    """
    msg = f"{t('analysis_msg_header', ticker=ticker, form=form, date=date_str)}\n\n{analysis}"
    if diff:
        msg += f"\n\n{t('risk_factor_changes_header')}\n{diff}"
    if price_snippet:
        msg += f"\n\n{price_snippet}"
    msg += f"\n\n{'─'*28}"
    return msg


def send_filing_result(ticker: str, form: str, date_str: str, text: str,
                       analysis: str, diff: str, save_to_cache: bool, quiet: bool):
    """
    IO: persist artifacts and notify the user.
    Steps: store raw + previous, send message+button, send .md file,
    weekly log, update cache, bump counter.
    """
    save_prev(ticker, form, text)
    raw_key = store_raw_filing(ticker, form, date_str, text)

    if not quiet:
        # Optional E1 price action — empty string on disable/failure (silent).
        price_snippet = compute_price_snippet(ticker, date_str)
        message = render_filing_message(ticker, form, date_str,
                                        analysis, diff, price_snippet)
        tg_with_button(message, raw_key)
        send_md_analysis(ticker, form, date_str, analysis, diff)

    log_weekly(ticker, form, date_str, analysis)

    if save_to_cache:
        ob = load_cache()
        mark_processed(ob, ticker, form, date_str)
        save_cache(ob)
    status_inc("total_analyzed")


def scan_ticker(ticker: str, forms: list,
                use_cache: bool, save_to_cache: bool,
                quiet: bool = False) -> bool:
    """
    Top-level orchestration: fetch → analyze → send for one ticker.
    Returns True if at least one new filing was processed.
    """
    cfg      = get_cfg()
    cache_dict = load_cache()

    found = fetch_new_filings(
        ticker, forms, cfg["days_lookback"],
        cache_dict, use_cache, quiet,
    )

    if not found:
        if not quiet: tg(t("no_new_filings", ticker=ticker))
        return False

    if not quiet:
        tg(t("new_filings_found", ticker=ticker, count=len(found)))

    for form, date_str, text in found:
        analysis, diff = analyze_filing(
            ticker, form, date_str, text,
            cfg["max_chars"], cfg["model"], cfg["custom_prompts"],
        )
        send_filing_result(ticker, form, date_str, text,
                           analysis, diff, save_to_cache, quiet)
        time.sleep(5)

    status_set(last_scan=datetime.now().isoformat())
    return True

# ─── Top-level scan commands ──────────────────────────────
def cmd_sec(form_override=None, quiet=False):
    cfg    = get_cfg()
    items  = cfg["tickers"]
    forms = form_override or cfg["default_forms"]
    if not items:
        if not quiet: tg(t("watchlist_empty_with_hint"))
        return False
    if not quiet:
        tg(t("sec_scan_started",
             tickers="  ".join(items), forms="  ".join(forms)))
    any_found = False
    for ticker in items:
        if scan_ticker(ticker, forms, True, True, quiet): any_found = True
        time.sleep(2)
    if not quiet:
        tg(t("scan_complete"))
    status_set(last_scan=datetime.now().isoformat())
    return any_found

def cmd_insider(quiet=False):
    cfg   = get_cfg()
    items = cfg["tickers"]
    if not items:
        if not quiet: tg(t("watchlist_empty"))
        return False
    if not quiet: tg(t("insider_scan_started"))
    any_found = False
    for ticker in items:
        if scan_ticker(ticker, ["4"], True, True, quiet): any_found = True
        time.sleep(2)
    if not quiet: tg(t("insider_scan_complete"))
    return any_found

def cmd_scanticker(parts: list):
    if len(parts) < 2:
        tg(t("scanticker_usage")); return
    ticker = parts[1].upper()
    forms: list = []
    if len(parts) >= 3:
        for f in [p.upper() for p in parts[2:]]:
            m = _match_form(f)
            if m: forms.append(m)
            else: tg(t("unknown_form_skipped", form=f))
        if not forms: tg(t("no_valid_forms")); return
    else:
        forms = get_cfg()["default_forms"]
    tg(t("on_demand_scan", ticker=ticker, forms="  ".join(forms)))
    scan_ticker(ticker, forms, False, False, False)
    tg(t("on_demand_complete", ticker=ticker))

# ─── /compare — side-by-side ticker comparison (E4) ───────
def build_compare_prompt(ticker_a: str, ticker_b: str, form: str,
                         text_a: str, text_b: str) -> str:
    """PURE: build the comparative-analysis prompt for two filings."""
    return (
        f"Compare two SEC filings for {ticker_a} and {ticker_b} — same form: {form}.\n\n"
        f"=== {ticker_a} {form} ===\n{text_a}\n\n"
        f"=== {ticker_b} {form} ===\n{text_b}\n\n"
        "Produce a single side-by-side comparison covering:\n"
        "1. 📊 Business momentum (growth, margins) — which is stronger?\n"
        "2. ⚠️ Key risks — overlapping vs. distinct\n"
        "3. 💰 Capital allocation / balance-sheet posture\n"
        "4. 🎯 Relative attractiveness for an investor"
    )

def cmd_compare(parts: list):
    """Usage: /compare TICKER_A TICKER_B [FORM]   — FORM defaults to 10-K."""
    if len(parts) < 3:
        tg(t("compare_usage")); return
    ticker_a = parts[1].upper().strip()
    ticker_b = parts[2].upper().strip()
    if ticker_a == ticker_b:
        tg(t("compare_same_ticker")); return
    # Optional form (4th arg, possibly multi-word like "SC 13G")
    form = "10-K"
    if len(parts) >= 4:
        m = _match_form(" ".join(parts[3:]).upper())
        if m: form = m
        else:
            tg(t("unknown_form_named", form=" ".join(parts[3:])))
            return

    cfg = get_cfg()
    tg(t("compare_started", a=ticker_a, b=ticker_b, form=form))

    def _fetch_one(tk: str) -> tuple[str, str] | None:
        rows = fetch_new_filings(tk, [form],
                                 lookback_days=400,
                                 quiet=True,
                                 n_latest=1,
                                 max_chars_per=cfg["max_chars"])
        if not rows: return None
        f, d, txt = rows[0]
        return d, txt

    res_a = _fetch_one(ticker_a)
    res_b = _fetch_one(ticker_b)
    if not res_a:
        tg(t("compare_missing", ticker=ticker_a, form=form)); return
    if not res_b:
        tg(t("compare_missing", ticker=ticker_b, form=form)); return

    date_a, text_a = res_a
    date_b, text_b = res_b
    summary = llm(
        build_compare_prompt(ticker_a, ticker_b, form,
                             extract_section(text_a, form, cfg["max_chars"]),
                             extract_section(text_b, form, cfg["max_chars"])),
        cfg["model"],
    )
    tg(t("compare_header",
         a=ticker_a, date_a=date_a,
         b=ticker_b, date_b=date_b,
         form=form,
         sep="─" * 28,
         summary=summary))

# ─── Weekly digest ────────────────────────────────────────
# Telegram classic Markdown reserves: \ _ * ` [
# Backslash MUST be escaped first, otherwise subsequent replacements
# would double-escape the backslash chars added by them.
_MD_ESCAPE_CHARS = ("\\", "_", "*", "`", "[")

def _md_escape(text: str) -> str:
    """Escape Markdown special chars so user-supplied text renders verbatim.

    Replaces the older _clean_md (which stripped these chars and lost data
    like 'AAPL *up* 15%' → 'AAPL up 15%'). Escaping preserves the original
    text: 'AAPL *up* 15%' → 'AAPL \\*up\\* 15%'.
    """
    for ch in _MD_ESCAPE_CHARS:
        text = text.replace(ch, "\\" + ch)
    return text

def send_weekly_digest():
    data = get_weekly_log()
    if not data:
        log.info(t("digest_no_data")); return

    week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")
    today_str      = datetime.now().strftime("%d.%m.%Y")

    lines = [t("digest_title_block",
                  start=week_start, end=today_str,
                  count=len(data), sep="─" * 28)]

    by_ticker: dict = {}
    for entry in data:
        by_ticker.setdefault(entry["ticker"], []).append(entry)

    for ticker, entries in by_ticker.items():
        lines.append(f"\n🏢 *{ticker}*")
        for k in entries:
            snippet = _md_escape(k["analiz"][:120].replace("\n", " "))
            lines.append(f"  • {k['form']} ({k['tarih']}): {snippet}...")

    tg("\n".join(lines))
    clear_weekly_log()
    log.info("Weekly digest sent, log cleared.")

def cmd_digest(parts: list) -> str:
    if len(parts) >= 2 and parts[1].lower() == "off":
        update_cfg(weekly_digest=False)
        return t("digest_disabled")
    if len(parts) >= 2 and parts[1].lower() == "now":
        send_weekly_digest()
        return ""
    update_cfg(weekly_digest=True)
    return t("digest_enabled")

# ─── Custom prompts ───────────────────────────────────────
def _match_form(raw: str) -> str | None:
    return next((k for k in FORMS if k.upper() == raw or
                 k.replace(" ","").upper() == raw.replace(" ","")), None)

def cmd_setprompt(parts: list) -> str:
    if len(parts) < 3:
        return t("setprompt_usage")
    m = _match_form(parts[1].upper())
    if not m: return t("unknown_form_named", form=parts[1])
    prompt = " ".join(parts[2:])
    mutate_cfg(lambda c: c["custom_prompts"].update({m: prompt}))
    return t("prompt_saved", form=m, prompt=prompt)

def cmd_getprompt(parts: list) -> str:
    if len(parts) < 2: return t("getprompt_usage")
    m = _match_form(parts[1].upper())
    if not m: return t("unknown_form_named", form=parts[1])
    prompt = get_cfg()["custom_prompts"].get(m)
    if not prompt:
        return t("no_custom_prompt", form=m)
    return t("custom_prompt_show", form=m, prompt=prompt)

def cmd_resetprompt(parts: list) -> str:
    if len(parts) < 2: return t("resetprompt_usage")
    m = _match_form(parts[1].upper())
    if not m: return t("unknown_form_named", form=parts[1])
    mutate_cfg(lambda c: c["custom_prompts"].pop(m, None))
    return t("prompt_reset", form=m)

def cmd_listprompts() -> str:
    custom = get_cfg()["custom_prompts"]
    if not custom:
        return t("listprompts_empty")
    lines = [t("listprompts_title")]
    for form, prompt in custom.items():
        lines.append(f"*{form}:* {prompt[:80]}{'...' if len(prompt)>80 else ''}")
    return "\n".join(lines)

# ─── Portfolio insider sentiment ──────────────────────────
def cmd_sentiment():
    cfg   = get_cfg()
    items = cfg["tickers"]
    if not items: tg(t("watchlist_empty")); return

    tg(t("sentiment_started"))

    # Reuse fetch_new_filings — pulls last 5 Form 4 filings per ticker,
    # 30-day window, no cache, suppressed user-facing errors.
    ticker_data = []
    for ticker in items:
        rows = fetch_new_filings(
            ticker, ["4"], lookback_days=30,
            quiet=True,
            n_latest=5, max_chars_per=3000,
        )
        if rows:
            texts = [text for _, _, text in rows]
            ticker_data.append((ticker, "\n---\n".join(texts)))

    if not ticker_data:
        tg(t("sentiment_no_transactions")); return

    signals = []
    today_iso = datetime.now().strftime("%Y-%m-%d")
    for ticker, texts in ticker_data:
        signal = llm(
            f"{ticker} — Last 30 days Form 4 transactions:\n{texts[:6000]}\n\n"
            "Summarize in a single line:\n"
            "Format: EMOJI SENTIMENT (Bullish/Bearish/Neutral) — 1-sentence reason\n"
            "Emoji: 📈 Bullish, 📉 Bearish, ➡️ Neutral",
            cfg["model"]
        )
        signals.append(f"*{ticker}*: {signal}")
        # Persist for /sentiment trend comparisons.
        try:
            append_sentiment(ticker, signal, on_date=today_iso)
        except Exception as e:
            log.error(f"append_sentiment {ticker}: {e}")
        time.sleep(3)

    portfolio = llm(
        "Based on the following insider signals, give a portfolio-wide assessment:\n\n"
        + "\n".join(signals)
        + "\n\nWhat is the insider sentiment across the portfolio? "
        "Are there any standout warnings or opportunities?",
        cfg["model"]
    )

    tg(t("sentiment_score_header",
         count=len(ticker_data),
         signals="\n".join(signals),
         sep="─" * 28,
         summary=portfolio))

# ─── Sentiment trend (E3) ─────────────────────────────────
_SHIFT_ARROWS = {
    ("bearish", "bullish"): "📉→📈",
    ("neutral", "bullish"): "➡️→📈",
    ("bullish", "bearish"): "📈→📉",
    ("neutral", "bearish"): "➡️→📉",
    ("bullish", "neutral"): "📈→➡️",
    ("bearish", "neutral"): "📉→➡️",
}

def _trend_label(prev_label: str, latest_label: str) -> str:
    """Return a localized one-word shift description."""
    if prev_label == latest_label:
        return t("trend_no_change")
    if prev_label == "unknown" or latest_label == "unknown":
        return t("trend_changed")
    key = (prev_label, latest_label)
    arrow = _SHIFT_ARROWS.get(key, "")
    if latest_label == "bullish":
        return f"{arrow} {t('trend_shift_bullish')}".strip()
    if latest_label == "bearish":
        return f"{arrow} {t('trend_shift_bearish')}".strip()
    return f"{arrow} {t('trend_shift_neutral')}".strip()

def build_trend_lines(history: dict, days: int,
                      ref_date: datetime | None = None) -> list[str]:
    """
    PURE: from sentiment_history dict, build trend output lines.
    For each ticker (sorted) compares the most recent entry to the most
    recent entry that is at least `days` days older.

    `ref_date` defaults to datetime.now() — overridable for tests.
    """
    now = ref_date or datetime.now()
    cutoff = now - timedelta(days=days)
    lines: list[str] = []
    for ticker in sorted(history.keys()):
        entries = history[ticker]
        if not entries:
            continue
        # Sort by date ascending; latest is the last entry.
        sorted_entries = sorted(entries, key=lambda e: e.get("date", ""))
        latest = sorted_entries[-1]
        # Find the most recent entry strictly older than `cutoff`.
        prev = None
        for e in reversed(sorted_entries[:-1]):
            try:
                ed = datetime.fromisoformat(e.get("date", ""))
            except Exception:
                continue
            if ed <= cutoff:
                prev = e
                break
        if prev is None:
            lines.append(t("sentiment_trend_no_history",
                           ticker=ticker,
                           emoji=latest["emoji"],
                           label=latest["label"]))
        else:
            shift = _trend_label(prev["label"], latest["label"])
            lines.append(t("sentiment_trend_line",
                           ticker=ticker,
                           latest_emoji=latest["emoji"],
                           latest_label=latest["label"],
                           prev_emoji=prev["emoji"],
                           prev_label=prev["label"],
                           prev_date=prev["date"],
                           shift=shift))
    return lines

def cmd_sentiment_trend(parts: list):
    """Usage: /sentiment trend [days]  — default 30 days lookback."""
    days = 30
    if len(parts) >= 3:
        try:
            n = int(parts[2])
            if 1 <= n <= 730:
                days = n
        except ValueError:
            pass
    history = load_sentiment_history()
    if not history:
        tg(t("sentiment_trend_no_data"))
        return
    lines = build_trend_lines(history, days)
    if not lines:
        tg(t("sentiment_trend_no_data"))
        return
    tg(t("sentiment_trend_title", days=days) + "\n\n" + "\n".join(lines))

# ─── Ticker management ────────────────────────────────────
def cmd_addticker(parts: list) -> str:
    if len(parts) < 2:
        return t("addticker_usage")
    added, already_in = [], []
    def _add(c):
        for raw in parts[1:]:
            ticker = raw.upper().strip()
            if ticker in c["tickers"]: already_in.append(ticker)
            else: c["tickers"].append(ticker); added.append(ticker)
    mutate_cfg(_add)
    lines = []
    if added:   lines.append(t("ticker_added",  tickers="  ".join(f"`{x}`" for x in added)))
    if already_in: lines.append(t("ticker_already", tickers="  ".join(f"`{x}`" for x in already_in)))
    return "\n".join(lines)

def cmd_removeticker(parts: list) -> str:
    if len(parts) < 2: return t("removeticker_usage")
    removed, not_found = [], []
    def _remove(c):
        for raw in parts[1:]:
            ticker = raw.upper().strip()
            if ticker in c["tickers"]: c["tickers"].remove(ticker); removed.append(ticker)
            else: not_found.append(ticker)
    mutate_cfg(_remove)
    lines = []
    if removed:  lines.append(t("ticker_removed",       tickers="  ".join(f"`{x}`" for x in removed)))
    if not_found: lines.append(t("ticker_not_found_list", tickers="  ".join(f"`{x}`" for x in not_found)))
    return "\n".join(lines)

def cmd_listtickers() -> str:
    cfg = get_cfg(); lst = cfg["tickers"]
    if not lst: return t("listtickers_empty")
    return t("listtickers_title",
             count=len(lst),
             lines="\n".join(f"  • `{x}`" for x in lst))

# ─── Watchlist groups (E2) ────────────────────────────────
# Groups are named subsets of tickers, persisted in cfg["groups"].
# They are independent of the main watchlist — tickers can be in groups
# without being in the main watchlist (useful for ad-hoc scanning).
_GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

def _valid_group_name(s: str) -> bool:
    return bool(_GROUP_NAME_RE.match(s))

def cmd_addgroup(parts: list) -> str:
    """Usage: /addgroup NAME TICKER [TICKER...]"""
    if len(parts) < 3:
        return t("addgroup_usage")
    name = parts[1].strip()
    if not _valid_group_name(name):
        return t("group_name_invalid", name=name)
    tickers = [p.upper().strip() for p in parts[2:]]
    def _add(c):
        c["groups"][name] = sorted(set(tickers))
    mutate_cfg(_add)
    return t("group_added", name=name,
             tickers="  ".join(f"`{x}`" for x in sorted(set(tickers))))

def cmd_removegroup(parts: list) -> str:
    """Usage: /removegroup NAME"""
    if len(parts) < 2:
        return t("removegroup_usage")
    name = parts[1].strip()
    missing = {"flag": False}
    def _rm(c):
        if name not in c["groups"]:
            missing["flag"] = True
        else:
            c["groups"].pop(name, None)
    mutate_cfg(_rm)
    if missing["flag"]:
        return t("group_not_found", name=name)
    return t("group_removed", name=name)

def cmd_listgroups() -> str:
    """Show all defined groups."""
    groups = get_cfg().get("groups", {})
    if not groups:
        return t("listgroups_empty")
    lines = [t("listgroups_title", count=len(groups))]
    for name in sorted(groups.keys()):
        members = groups[name]
        members_str = "  ".join(f"`{x}`" for x in members) if members else "_empty_"
        lines.append(f"*{name}* ({len(members)}): {members_str}")
    return "\n".join(lines)

def cmd_scangroup(parts: list):
    """Usage: /scangroup NAME [FORM...]"""
    if len(parts) < 2:
        tg(t("scangroup_usage")); return
    name = parts[1].strip()
    cfg = get_cfg()
    group = cfg.get("groups", {}).get(name)
    if group is None:
        tg(t("group_not_found", name=name)); return
    if not group:
        tg(t("group_empty", name=name)); return

    # Optional form list
    forms: list = []
    if len(parts) >= 3:
        for f in [p.upper() for p in parts[2:]]:
            m = _match_form(f)
            if m: forms.append(m)
            else: tg(t("unknown_form_skipped", form=f))
        if not forms: tg(t("no_valid_forms")); return
    else:
        forms = cfg["default_forms"]

    tg(t("scangroup_started",
         name=name, count=len(group),
         tickers="  ".join(group),
         forms="  ".join(forms)))
    for ticker in group:
        scan_ticker(ticker, forms, True, True, False)
        time.sleep(2)
    tg(t("scan_complete"))
    status_set(last_scan=datetime.now().isoformat())

# ─── Form management ──────────────────────────────────────
def cmd_listforms() -> str:
    cfg   = get_cfg(); active = cfg["default_forms"]
    categories = [
        (t("listforms_cat_periodic"),  ["10-K","10-Q","8-K"]),
        (t("listforms_cat_insider"),   ["4","144","SC 13G","SC 13D"]),
        (t("listforms_cat_offerings"), ["S-1","424B4"]),
        (t("listforms_cat_foreign"),   ["20-F","6-K"]),
        (t("listforms_cat_other"),     ["DEF 14A","11-K"]),
    ]
    lines = [t("listforms_title")]
    for cat, fl in categories:
        lines.append(f"*{cat}*")
        for f in fl:
            lines.append(f"  {'✅' if f in active else '  '} `{f}` — {form_desc(f)}")
        lines.append("")
    lines.append(t("listforms_active", forms="  ".join(active)))
    lines.append(t("listforms_footer"))
    return "\n".join(lines)

def cmd_addform(parts: list) -> str:
    if len(parts) < 2: return t("addform_usage")
    m = _match_form(" ".join(parts[1:]).upper())
    if not m: return t("unknown_form")
    # Check + mutate atomically; if already present, skip persist.
    already = {"flag": False}
    def _add(c):
        if m in c["default_forms"]:
            already["flag"] = True
        else:
            c["default_forms"].append(m)
    mutate_cfg(_add)
    if already["flag"]: return t("form_already_active", form=m)
    return t("form_added", form=m)

def cmd_removeform(parts: list) -> str:
    if len(parts) < 2: return t("removeform_usage")
    m = _match_form(" ".join(parts[1:]).upper())
    if not m: return t("unknown_form_short")
    missing = {"flag": False}
    def _rm(c):
        if m not in c["default_forms"]:
            missing["flag"] = True
        else:
            c["default_forms"].remove(m)
    mutate_cfg(_rm)
    if missing["flag"]: return t("form_not_in_default", form=m)
    return t("form_removed", form=m)

# ─── Settings, status, language ───────────────────────────
def cmd_settings() -> str:
    cfg = get_cfg()
    ticker_list = ""
    if cfg["tickers"]:
        ticker_list = " — `" + "  ".join(cfg["tickers"]) + "`"
    return t("settings_block",
             model=cfg['model'],
             lookback=cfg['days_lookback'],
             max_chars=cfg['max_chars'],
             forms="  ".join(cfg['default_forms']),
             ticker_count=len(cfg['tickers']),
             ticker_list=ticker_list,
             language=get_lang(),
             schedule=cfg.get('schedule') or t("label_off"),
             alarm=t("label_on") if cfg.get('alarm_on') else t("label_off"),
             digest=t("label_on") if cfg.get('weekly_digest') else t("label_off"),
             prompt_count=len(cfg.get('custom_prompts', {})))

def cmd_status() -> str:
    snap = status_snapshot()
    now    = datetime.now()
    baslangi = datetime.fromisoformat(snap["started"])
    sure     = now - baslangi
    saat     = int(sure.total_seconds() // 3600)
    dakika   = int((sure.total_seconds() % 3600) // 60)

    def time_format(iso: str | None) -> str:
        if not iso: return t("label_dash")
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")

    cfg = get_cfg()
    tg_hata = snap["tg_errors"]
    or_hata = snap["or_errors"]
    return t("status_block",
             uptime=t("uptime_format", hours=saat, minutes=dakika),
             last_update=time_format(snap["last_update"]),
             last_scan=time_format(snap["last_scan"]),
             last_alarm=time_format(snap["last_alarm"]),
             total_analyzed=snap["total_analyzed"],
             tg_errors=("✅ 0" if tg_hata == 0 else f"⚠️ {tg_hata}"),
             or_errors=("✅ 0" if or_hata == 0 else f"⚠️ {or_hata}"),
             language=get_lang(),
             schedule=cfg.get('schedule') or t("label_off"),
             alarm=t("label_on") if cfg.get('alarm_on') else t("label_off"),
             digest=t("label_on") if cfg.get('weekly_digest') else t("label_off"),
             ticker_count=len(cfg['tickers']))

def cmd_setlang(parts: list) -> str:
    if len(parts) < 2:
        return t("setlang_usage",
                 current=get_lang(),
                 supported=", ".join(SUPPORTED_LANGS))
    code = parts[1].lower().strip()
    if code not in SUPPORTED_LANGS:
        return t("lang_unknown", lang=code, supported=", ".join(SUPPORTED_LANGS))
    set_lang(code)
    return t("lang_set", lang=code, name=lang_meta().get("name", code))

def cmd_setmodel(parts: list) -> str:
    if len(parts) < 2:
        return t("setmodel_usage")
    update_cfg(model=" ".join(parts[1:]))
    return t("model_set", model=" ".join(parts[1:]))

def cmd_setlookback(parts: list) -> str:
    if len(parts) < 2: return t("setlookback_usage")
    try:
        n = int(parts[1])
        if not 1 <= n <= 365: raise ValueError
        update_cfg(days_lookback=n)
        return t("lookback_set", n=n)
    except ValueError:
        return t("lookback_invalid")

def cmd_setchars(parts: list) -> str:
    if len(parts) < 2: return t("setchars_usage")
    try:
        n = int(parts[1])
        if not 1000 <= n <= 50000: raise ValueError
        update_cfg(max_chars=n)
        return t("chars_set", n=n)
    except ValueError:
        return t("chars_invalid")

def cmd_setrawmax(parts: list) -> str:
    """Cap the in-memory raw-filing store. 0 = unlimited."""
    if len(parts) < 2:
        return t("setrawmax_usage", current=get_cfg().get("raw_max", 0))
    try:
        n = int(parts[1])
        if n < 0 or n > 100000: raise ValueError
        update_cfg(raw_max=n)
        return t("rawmax_set", n=n) if n > 0 else t("rawmax_unlimited")
    except ValueError:
        return t("rawmax_invalid")

def cmd_priceaction(parts: list) -> str:
    """Toggle the E1 price action snippet under each filing analysis."""
    if len(parts) < 2:
        cur = "on" if get_cfg().get("price_action_enabled", True) else "off"
        return t("priceaction_usage", current=cur)
    val = parts[1].lower()
    if val == "off":
        update_cfg(price_action_enabled=False)
        return t("priceaction_disabled")
    if val == "on":
        update_cfg(price_action_enabled=True)
        return t("priceaction_enabled")
    return t("priceaction_usage", current="on" if get_cfg().get("price_action_enabled", True) else "off")

def cmd_setlookforward(parts: list) -> str:
    """Days after filing for price change measurement (1-90)."""
    if len(parts) < 2:
        return t("setlookforward_usage", current=get_cfg().get("price_lookforward_days", 5))
    try:
        n = int(parts[1])
        if not 1 <= n <= 90: raise ValueError
        update_cfg(price_lookforward_days=n)
        return t("lookforward_set", n=n)
    except ValueError:
        return t("lookforward_invalid")

def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        h, m = s.split(":"); return int(h), int(m)
    except Exception:
        return -1, -1

def cmd_setschedule(parts: list) -> str:
    if len(parts) < 2:
        return t("setschedule_usage")
    value = parts[1].lower()
    if value == "off":
        update_cfg(schedule=None)
        return t("schedule_disabled")
    sh, sd = _parse_hhmm(value)
    if sh < 0 or not (0 <= sh <= 23 and 0 <= sd <= 59):
        return t("schedule_invalid")
    update_cfg(schedule=value)
    return t("schedule_set", time=value)

def cmd_alarm(parts: list) -> str:
    if len(parts) >= 2 and parts[1].lower() == "off":
        update_cfg(alarm_on=False)
        return t("alarm_disabled")
    update_cfg(alarm_on=True)
    return t("alarm_enabled")

# ─── Background thread ────────────────────────────────────
def background_thread():
    log.info("Background thread started.")
    last_alarm_check = datetime.now()
    last_digest_day    = -1

    while not _stop_event.is_set():
        now = datetime.now()
        cfg   = get_cfg()

        # Auto schedule
        schedule_str = cfg.get("schedule")
        if schedule_str:
            sh, sd = _parse_hhmm(schedule_str)
            if sh >= 0 and now.hour == sh and now.minute == sd:
                last = last_scan_dt()
                if last is None or (now - last).total_seconds() > 90:
                    log.info(f"Scheduled scan: {schedule_str}")
                    tg(t("scheduled_scan_starting", time=schedule_str))
                    cmd_sec(quiet=False)

        # Hourly alarm
        if cfg.get("alarm_on"):
            if (now - last_alarm_check).total_seconds() >= 3600:
                log.info("Alarm: hourly check")
                any_found = cmd_sec(quiet=True)
                if any_found:
                    tg(t("alert_new_filing"))
                last_alarm_check = now
                status_set(last_alarm=now.isoformat())

        # Weekly digest (Sunday 09:00)
        if cfg.get("weekly_digest"):
            if (now.weekday() == 6 and now.hour == 9
                    and now.minute == 0 and last_digest_day != now.day):
                log.info("Sending weekly digest.")
                send_weekly_digest()
                last_digest_day = now.day

        time.sleep(60)

    log.info("Background thread stopped.")

# ─── First-run wizard ─────────────────────────────────────
WIZARD: dict = {}

def start_wizard():
    # Step 0: language. Until the user picks, all UI is bilingual.
    WIZARD["step"] = "lang"
    tg(t("wizard_lang_menu"))

def _advance_to_forms_step():
    """Move from Step 0/1 to Step 2 (forms). Sends localized welcome + form menu."""
    WIZARD["step"] = "forms"
    tg(t("wizard_welcome") + t("wizard_form_menu"))

def wizard_handle(text: str, parts: list) -> bool:
    step = WIZARD.get("step")
    if not step: return False

    # Step 0 — language picker (bilingual UI)
    if step == "lang":
        if parts and parts[0].lower() == "/lang" and len(parts) >= 2:
            code = parts[1].lower().strip()
            if code in SUPPORTED_LANGS:
                set_lang(code)
                _advance_to_forms_step()
                return True
        tg(t("wizard_lang_unknown"))
        return True

    if step == "forms":
        if text == "/usedefaults":
            update_cfg(default_forms=DEFAULT_FORMS, first_run=False)
            WIZARD["step"] = "tickers"
            tg(t("wizard_forms_set", forms="  ".join(DEFAULT_FORMS))
               + t("wizard_ticker_menu"))
            return True
        if parts[0].lower() == "/setforms" and len(parts) >= 2:
            valid = []
            for f in [p.upper() for p in parts[1:]]:
                m = _match_form(f)
                if m: valid.append(m)
                else: tg(t("unknown_form_skipped", form=f))
            if not valid:
                tg(t("wizard_no_valid_forms") + t("wizard_form_menu"))
                return True
            update_cfg(default_forms=valid, first_run=False)
            WIZARD["step"] = "tickers"
            tg(t("wizard_forms_set", forms="  ".join(valid))
               + t("wizard_ticker_menu"))
            return True
        tg(t("wizard_use_default_or_setforms"))
        return True
    if step == "tickers":
        if text == "/skip":
            WIZARD.pop("step", None)
            tg(t("wizard_complete"))
            return True
        if parts[0].lower() == "/addticker" and len(parts) >= 2:
            result = cmd_addticker(parts)
            WIZARD.pop("step", None)
            tg(result + "\n\n" + t("wizard_complete"))
            return True
        tg(t("wizard_use_addticker_or_skip"))
        return True
    return False

# ─── Help & report ────────────────────────────────────────
def cmd_report():
    """Send this week's analyses as a single .md file."""
    data = get_weekly_log()
    if not data:
        tg(t("report_no_data"))
        return

    today_str      = datetime.now().strftime("%d.%m.%Y")
    week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")

    lines = [t("report_md_title",
                  start=week_start, end=today_str,
                  count=len(data),
                  generated=datetime.now().strftime('%d.%m.%Y %H:%M')),
                ""]

    by_ticker: dict = {}
    for entry in data:
        by_ticker.setdefault(entry["ticker"], []).append(entry)

    for ticker, entries in by_ticker.items():
        lines.append(f"## 🏢 {ticker}")
        lines.append("")
        for k in entries:
            lines.append(f"### {k['form']} — {k['tarih']}")
            lines.append("")
            lines.append(k["analiz"])
            lines.append("")
            lines.append("---")
            lines.append("")

    lines.append(t("report_footer"))

    content   = "\n".join(lines)
    filename = f"sec_report_{datetime.now().strftime('%Y%m%d')}.md"
    tg_send_document(filename, content, t("report_caption", date=today_str))

def help_msg() -> str:
    cfg = get_cfg()
    return t("help_block",
             forms="  ".join(cfg['default_forms']),
             ticker_count=len(cfg['tickers']),
             language=get_lang())

# ─── Update handler (polling and webhook) ─────────────────
def _process_update(upd: dict):
    global _webhook_active

    # Callback query (inline button)
    cq = upd.get("callback_query")
    if cq:
        chat_id = str(cq.get("from", {}).get("id", ""))
        if chat_id == str(TELEGRAM_CHAT_ID):
            data = cq.get("data", "")
            if data.startswith("raw:"):
                send_raw_filing(cq["id"], data[4:])
            else:
                tg_answer_callback(cq["id"])
        return

    # Normal message
    msg     = upd.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    raw     = msg.get("text", "").strip()
    text   = raw.lower()
    parts = raw.split()
    komut   = parts[0].lower() if parts else ""

    if chat_id != str(TELEGRAM_CHAT_ID): return
    if wizard_handle(text, parts):   return

    # Tickers
    if   komut == "/addticker":      tg(cmd_addticker(parts))
    elif komut == "/removeticker":   tg(cmd_removeticker(parts))
    elif komut == "/listtickers":    tg(cmd_listtickers())
    # Groups
    elif komut == "/addgroup":       tg(cmd_addgroup(parts))
    elif komut == "/removegroup":    tg(cmd_removegroup(parts))
    elif komut == "/listgroups":     tg(cmd_listgroups())
    elif komut == "/scangroup":      cmd_scangroup(parts)
    # Forms
    elif komut == "/listforms":      tg(cmd_listforms())
    elif komut == "/addform":        tg(cmd_addform(parts))
    elif komut == "/removeform":     tg(cmd_removeform(parts))
    # Custom prompts
    elif komut == "/setprompt":      tg(cmd_setprompt(parts))
    elif komut == "/getprompt":      tg(cmd_getprompt(parts))
    elif komut == "/resetprompt":    tg(cmd_resetprompt(parts))
    elif komut == "/listprompts":    tg(cmd_listprompts())
    # Schedule, alarm, digest
    elif komut == "/setschedule":    tg(cmd_setschedule(parts))
    elif komut == "/alarm":          tg(cmd_alarm(parts))
    elif komut == "/digest":
        r = cmd_digest(parts)
        if r: tg(r)
    # Report
    elif komut == "/report":         cmd_report()
    # Webhook
    elif komut == "/setwebhook":     tg(cmd_setwebhook(parts))
    elif komut == "/delwebhook":     tg(cmd_delwebhook())
    # Settings, status, language
    elif komut == "/settings":       tg(cmd_settings())
    elif komut == "/status":         tg(cmd_status())
    elif komut == "/setlang":        tg(cmd_setlang(parts))
    elif komut == "/setmodel":       tg(cmd_setmodel(parts))
    elif komut == "/setlookback":    tg(cmd_setlookback(parts))
    elif komut == "/setchars":       tg(cmd_setchars(parts))
    elif komut == "/setrawmax":      tg(cmd_setrawmax(parts))
    elif komut == "/priceaction":    tg(cmd_priceaction(parts))
    elif komut == "/setlookforward": tg(cmd_setlookforward(parts))
    # On-demand scan
    elif komut == "/scanticker":     cmd_scanticker(parts)
    elif komut == "/compare":        cmd_compare(parts)
    elif komut == "/checkprice":     cmd_checkprice(parts)
    elif komut == "/checknews":      cmd_checknews(parts)
    # Help
    elif text in ["/start", "/help"]:
        tg(help_msg())
    # Sentiment
    elif any(s in text for s in ["/sentiment", "sentiment", "sentiment score"]):
        if len(parts) >= 2 and parts[1].lower() == "trend":
            cmd_sentiment_trend(parts)
        else:
            cmd_sentiment()
    # Combined scan
    elif any(s in text for s in ["check all","scan all","everything","/all"]):
        cmd_sec(); cmd_insider()
    # Insider only
    elif any(s in text for s in ["insider","form4","/insider","/form4"]):
        cmd_insider()
    # Default scan
    elif any(s in text for s in ["any news","check","scan","sec",
                                   "filings","/sec","/check","/scan"]):
        cmd_sec()


def main():
    global _webhook_active
    log.info("Bot v2.5 started.")

    # Validate EDGAR identity once at startup, then register with edgartools.
    ok, msg = validate_edgar_identity(EDGAR_IDENTITY)
    if not ok:
        log.error(f"EDGAR identity invalid: {msg}")
        sys.stderr.write(f"\n❌ {msg}\n")
        sys.exit(1)
    set_identity(EDGAR_IDENTITY)
    log.info(f"EDGAR identity registered: {EDGAR_IDENTITY}")

    # Startup cache cleanup — drop entries older than cache_max_age_days.
    pruned = prune_cache_expired()
    if pruned:
        log.info(f"Pruned {pruned} expired cache entries.")

    # Prime the lang cache early
    _ = get_lang()

    bg = threading.Thread(target=background_thread, daemon=True)
    bg.start()

    cfg = get_cfg()
    if cfg.get("first_run", True):
        start_wizard()
    else:
        tg(t("bot_active"))

    # Webhook or polling mode
    webhook_url = cfg.get("webhook_url")
    if cfg.get("webhook_aktif") and webhook_url and FLASK_OK:
        _webhook_active = True
        port = cfg.get("webhook_port", 5050)
        log.info(f"Webhook mode — port {port}, URL: {webhook_url}")
        tg(t("webhook_active", port=port))

        def flask_thread():
            start_webhook_server(port, _process_update)

        ft = threading.Thread(target=flask_thread, daemon=True)
        ft.start()
        _stop_event.wait()
    else:
        # Polling mode (default)
        _webhook_active = False
        if cfg.get("webhook_aktif") and not FLASK_OK:
            tg(t("webhook_flask_missing"))

        offset = 0
        while not _stop_event.is_set():
            updates = get_updates(offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                _process_update(upd)
            time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopping...")
        _stop_event.set()
