"""
SEC Analyzer Bot v4.9 — Telegram (multi-language)

Single-codebase replacement for bot_en.py + bot_tr.py.
- i18n: lang/en.json (default) + lang/tr.json, switch with /setlang.
- Thread-safe state (raw filing store, status dict).
- scan_ticker split into pure-ish helpers (fetch / analyze / render / send).
- 8-K EX-99.* exhibit collection, full network I/O hardening, 327 tests.
"""

__version__ = "4.9"

import copy, csv, os, time, json, logging, hashlib, threading, io, uuid
from datetime import datetime, timedelta, date
from pathlib import Path

import re
import sys
import requests
from edgar import Company, set_identity, find

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
    MASTER_CHAT_ID,
)

# ─── Paths ────────────────────────────────────────────────
BASE_DIR    = Path.home() / "sec-analyzer"
OUTPUT_DIR  = BASE_DIR / "reports"
CACHE_FILE  = BASE_DIR / "cache.json"
CONFIG_FILE = BASE_DIR / "bot_config.json"
CHAT_DIR    = BASE_DIR / "chats"
CHAT_DIR.mkdir(parents=True, exist_ok=True)
PREV_DIR    = BASE_DIR / "previous_filings"
WEEKLY_LOG  = BASE_DIR / "weekly_log.json"
SENT_HIST   = BASE_DIR / "sentiment_history.json"
PRICE_CACHE     = BASE_DIR / "price_cache.json"
WATCHWORD_SEEN      = BASE_DIR / "watchword_seen.json"
PORTFOLIO_HISTORY   = BASE_DIR / "portfolio_history.json"   # J4 daily value log
LANG_DIR        = Path(__file__).resolve().parent / "lang"
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
_alarm_lock      = threading.Lock()     # _pending_alarms (interactive alarm)
_watchword_lock  = threading.Lock()     # watchword_seen.json dedup state (G1)
_phistory_lock   = threading.Lock()     # portfolio_history.json (J4)
_fiscal_memo_lock = threading.Lock()    # no-op — kept for compat (Fiscal AI removed)
_twelve_memo_lock = threading.Lock()    # no-op — kept for compat (Twelve Data removed)
_stop_event  = threading.Event()
_ctx         = threading.local()    # per-thread reactive context (I1): _ctx.chat_id

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
    """Return the configured cap on _raw_filings entries (0 = unlimited).

    Uses get_cfg_value() — single key read under _cfg_lock, no deep-copy.
    _raw_filings itself is guarded by _raw_lock (distinct from _cfg_lock),
    so callers must not hold _raw_lock when calling this.
    """
    return int(get_cfg_value("raw_max", 0) or 0)

def store_raw_filing(ticker: str, form: str, date_str: str, text: str,
                     analysis: str = "", diff: str = "") -> str:
    """Store a raw filing (+ its analysis) and return a short uuid key.

    The entry feeds two inline buttons on the analysis message:
      * 'view original' — the raw filing text (`metin`)
      * '.md report'    — the analysis rendered as Markdown (`analysis`/`diff`)

    If raw_max > 0 (default 100), oldest entries are evicted FIFO to keep
    the store within that bound. raw_max = 0 means unlimited.
    """
    raw_key = uuid.uuid4().hex[:16]
    cap = _raw_cap()
    with _raw_filings_lock:
        _raw_filings[raw_key] = {
            "ticker": ticker, "form": form,
            "tarih": date_str, "metin": text,
            "analysis": analysis, "diff": diff,
        }
        if cap > 0:
            while len(_raw_filings) > cap:
                # Pop oldest (first inserted) — FIFO eviction.
                _raw_filings.pop(next(iter(_raw_filings)))
    return raw_key

def get_raw_filing(raw_key: str) -> dict | None:
    with _raw_filings_lock:
        return _raw_filings.get(raw_key)

# ─── Pending alarm hits (Item 4) ──────────────────────────
# The interactive alarm lists new filings with inline buttons. Telegram
# callback_data is capped at 64 bytes, so the hit list is parked here under
# a short token; buttons reference it by token + index. In-memory only —
# a bot restart invalidates outstanding alert buttons (they say "expired").
_pending_alarms: dict = {}     # token → {"hits": [(ticker,form,date)], "done": set()}

def register_alarm_hits(hits: list) -> str:
    """Park an alarm hit list under a fresh token (FIFO-capped at 50)."""
    token = uuid.uuid4().hex[:12]
    with _alarm_lock:
        _pending_alarms[token] = {"hits": list(hits), "done": set()}
        while len(_pending_alarms) > 50:
            _pending_alarms.pop(next(iter(_pending_alarms)))
    return token

def get_alarm_hits(token: str) -> dict | None:
    with _alarm_lock:
        entry = _pending_alarms.get(token)
        if entry is None:
            return None
        # Return a shallow copy so callers can read without holding the lock.
        return {"hits": list(entry["hits"]), "done": set(entry["done"])}

def mark_alarm_done(token: str, idx: int):
    with _alarm_lock:
        entry = _pending_alarms.get(token)
        if entry is not None:
            entry["done"].add(idx)

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

_CHAT_MAX = 5   # max authorized chat IDs (I1)

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
    "raw_max":         100,         # FIFO cap on raw-filing store (0 = unlimited)
    "cache_max_age_days": 365,      # auto-prune cache entries older than this
    "groups":          {},          # named ticker groups: {"tech": ["AAPL", "MSFT"]}
    "price_action_enabled":   True, # show price change after filing date
    "price_lookforward_days": 5,    # days after filing for price change measurement
    "watchwords":             [],   # EDGAR full-text keyword phrases (G1); max 10
    "portfolio":              [],   # unrealized P&L lots (G2); max 50
    "chat_ids":               [],   # authorized chat ID list (I1); first = admin
    "openrouter_api_key":     "",   # migrated from .env (J1); superseded by api_keys (J2)
    "env_imported":           False, # one-time .env migration guard (J1)
    "api_keys":               {},   # {provider: key} (J2)
    "default_provider":       "",   # active LLM provider name (J2)
    "no_keys_warned_date":    "",   # YYYY-MM-DD of last NO_KEYS reminder (J3 spam gate)
    "wizard_step":             "",   # active wizard step: "lang"|"api"|"forms"|"tickers"|"" (K1)
    "rich_format":            True,   # Bot API 10.1 rich messages (O1); per-chat opt-out via /setrich off
}
_cfg_cache: dict | None = None

def _apply_defaults(cfg: dict) -> dict:
    """Fill missing keys with deep-copied defaults (mutating cfg in place)."""
    for k, v in _CFG_DEFAULTS.items():
        cfg.setdefault(k, copy.deepcopy(v))
    return cfg

# ─── JSON IO — atomic write + corruption-safe read ────────
# Every runtime JSON file (config, cache, price cache, sentiment history,
# weekly log) goes through these two helpers:
#   * _atomic_write_json — writes a .tmp sibling then os.replace()s it into
#     place. os.replace is atomic on POSIX, so a crash mid-write can never
#     leave a half-written, unparseable file.
#   * _read_json — on a JSONDecodeError, backs the bad file up to
#     <name>.corrupt and logs it, instead of silently returning {} (which
#     would let the next save overwrite — and permanently destroy — the
#     corrupted-but-possibly-recoverable data).
# Callers still hold their own locks; these helpers do no locking.

def _atomic_write_json(path: Path, data) -> None:
    """Write `data` as JSON atomically (temp file + os.replace).

    Uses a per-thread unique .tmp name so concurrent calls (even without an
    external lock) cannot clobber each other's temp files.
    """
    tmp = path.parent / (f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        log.error(f"_atomic_write_json failed for {path.name}: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

def _read_json(path: Path, default):
    """Read JSON from `path`. Missing → default. Corrupt → back up + default."""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        backup = path.parent / (path.name + ".corrupt")
        try:
            path.replace(backup)
            log.error(f"Corrupt JSON: {path.name} → backed up as {backup.name} ({e})")
        except OSError as be:
            log.error(f"Corrupt JSON: {path.name} — backup failed: {be}")
        return default
    except OSError as e:
        log.error(f"Cannot read {path.name}: {e}")
        return default

def _load_cfg_locked() -> dict:
    """Caller must hold _cfg_lock. Returns the live cache (initialized on first call)."""
    global _cfg_cache
    if _cfg_cache is None:
        data = _read_json(CONFIG_FILE, {})
        _cfg_cache = _apply_defaults(data if isinstance(data, dict) else {})
    return _cfg_cache

def _save_cfg_locked():
    """Caller must hold _cfg_lock. Persist cache to disk."""
    if _cfg_cache is not None:
        _atomic_write_json(CONFIG_FILE, _cfg_cache)

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

def get_cfg_value(key: str, default=None):
    """Read a single top-level config key under _cfg_lock — no deep-copy overhead."""
    with _cfg_lock:
        return _load_cfg_locked().get(key, default)

# ─── Per-chat config (isolation for /addchat users) ───────
# Per-chat files store user-specific data: tickers, portfolio, custom_prompts,
# and per-user preferences (alarm, model, schedule, etc.).
# Global config stores shared state: language, default provider names.

_CHAT_PER_USER_KEYS = {"tickers", "portfolio", "custom_prompts", "default_forms",
                       "alarm_on", "price_action_enabled", "model",
                       "schedule", "api_keys", "groups", "weekly_digest",
                       "daily_news", "watchwords", "rich_format"}
_CHAT_DEFAULTS = {
    "tickers": [],
    "portfolio": [],
    "custom_prompts": {},
    "default_forms": DEFAULT_FORMS,
    "alarm_on": False,
    "price_action_enabled": True,
    "model": "openrouter/auto",
    "schedule": None,
    "api_keys": {},
    "groups": {},
    "weekly_digest": True,
    "daily_news": False,
    "watchwords": [],
    "rich_format": True,
}

def _chat_cfg_path(chat_id: str) -> Path:
    return CHAT_DIR / f"chat_{chat_id}.json"

def _load_chat_cfg(chat_id: str) -> dict:
    """Load per-chat config, creating with defaults if missing."""
    path = _chat_cfg_path(chat_id)
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    for k, v in _CHAT_DEFAULTS.items():
        data.setdefault(k, copy.deepcopy(v))
    return data

def _save_chat_cfg(chat_id: str, cfg: dict):
    _atomic_write_json(_chat_cfg_path(chat_id), cfg)

def _has_chat_cfg(chat_id: str) -> bool:
    return _chat_cfg_path(chat_id).exists()

def get_chat_cfg() -> dict:
    """Return per-chat config merged with global config.

    Per-chat settings (tickers, portfolio, model, alarm, etc.) override globals.
    Global-only settings (wizard_step, first_run, chat_ids, etc.) always come from global.
    """
    with _cfg_lock:
        global_cfg = _load_cfg_locked()
        cid = getattr(_ctx, "chat_id", None)
        if cid and _has_chat_cfg(cid):
            chat_cfg = _load_chat_cfg(cid)
            merged = copy.deepcopy(global_cfg)
            for k in _CHAT_PER_USER_KEYS:
                if k in chat_cfg:
                    merged[k] = copy.deepcopy(chat_cfg[k])
            return merged
        return copy.deepcopy(global_cfg)

def mutate_chat_cfg(fn) -> dict:
    """Atomic read-modify-write that routes changes correctly.

    Per-chat keys (tickers, model, alarm, etc.) go to the per-chat file.
    Global keys (wizard_step, chat_ids, etc.) go to the global config.
    Returns merged config snapshot.
    """
    with _cfg_lock:
        cid = getattr(_ctx, "chat_id", None)
        if cid and _has_chat_cfg(cid):
            chat_cfg = _load_chat_cfg(cid)
            global_cfg = _load_cfg_locked()
            merged = copy.deepcopy(global_cfg)
            for k in _CHAT_PER_USER_KEYS:
                if k in chat_cfg:
                    merged[k] = copy.deepcopy(chat_cfg[k])
            fn(merged)
            for k in _CHAT_PER_USER_KEYS:
                if merged.get(k) != chat_cfg.get(k):
                    chat_cfg[k] = copy.deepcopy(merged[k])
            _save_chat_cfg(cid, chat_cfg)
            for k, v in merged.items():
                if k not in _CHAT_PER_USER_KEYS and global_cfg.get(k) != v:
                    global_cfg[k] = v
            _save_cfg_locked()
            return copy.deepcopy(merged)
        return mutate_cfg(fn)

def init_chat_config(chat_id: str):
    """Create per-chat config for a newly added user (empty defaults)."""
    with _cfg_lock:
        path = _chat_cfg_path(chat_id)
        if not path.exists():
            _save_chat_cfg(chat_id, copy.deepcopy(_CHAT_DEFAULTS))

def _purge_chat_data(chat_id: str) -> None:
    """Delete all per-chat artifacts for a deauthorized chat. Idempotent."""
    with _cfg_lock:
        try:
            _chat_cfg_path(chat_id).unlink(missing_ok=True)
        except OSError as e:
            log.error(f"Failed to delete chat config for {chat_id}: {e}")
    with _wlog_lock:
        try:
            _weekly_log_path(chat_id).unlink(missing_ok=True)
        except OSError as e:
            log.error(f"Failed to delete weekly log for {chat_id}: {e}")

# ─── Chat ID authorization helpers (I1) ──────────────────

def _is_valid_chat_id(v) -> bool:
    """Return True if v is a non-empty string that parses as a non-zero integer."""
    try:
        return bool(v) and int(str(v)) != 0
    except (ValueError, TypeError):
        return False

def _is_authorized(chat_id: str) -> bool:
    """Return True if chat_id is in the authorized chat_ids list."""
    ids = get_cfg_value("chat_ids", [])
    return str(chat_id) in {str(c) for c in ids}

def _is_admin(chat_id: str) -> bool:
    """Return True if chat_id is the first (admin) entry in chat_ids."""
    ids = get_cfg_value("chat_ids", [])
    return bool(ids) and str(chat_id) == str(ids[0])

def _migrate_chat_ids():
    """Startup: ensure chat_ids list exists.

    Migration order:
    1. chat_ids already present → idempotent; remove stale scalar key if any.
    2. Legacy 'chat_id' scalar key in config → promote to list.
    3. TELEGRAM_CHAT_ID env var valid → bootstrap list.
    4. Nothing available → leave empty; first incoming chat will fill it via wizard.
    """
    def _do(c: dict):
        if c.get("chat_ids"):                                 # already migrated
            c.pop("chat_id", None)                           # clean stale key
            return
        legacy = c.pop("chat_id", None)
        seed = legacy or (
            TELEGRAM_CHAT_ID
            if TELEGRAM_CHAT_ID and TELEGRAM_CHAT_ID != "YOUR_CHAT_ID"
            else None
        )
        if seed:
            c["chat_ids"] = [str(seed)]
    mutate_cfg(_do)

def _ensure_master_in_chat_ids():
    """Guarantee MASTER_CHAT_ID is always chat_ids[0].

    The cap (_CHAT_MAX) can never exclude the master chat. Idempotent.
    """
    if not _is_valid_chat_id(MASTER_CHAT_ID):
        return
    master = str(MASTER_CHAT_ID)
    def _do(c: dict):
        ids = [str(i) for i in c.get("chat_ids", [])]
        ids = [i for i in ids if i != master]   # remove master from wherever it is
        c["chat_ids"] = [master] + ids           # prepend master
    mutate_cfg(_do)

def _import_legacy_env() -> list[str]:
    """One-time migration of legacy .env values into bot_config.json.

    Imports OPENROUTER_API_KEY and TELEGRAM_CHAT_ID if they are valid in the
    environment and not already present in config. Guarded by the
    ``env_imported`` config flag — idempotent on subsequent restarts.

    Returns list of key names that were imported (empty if nothing was done).
    """
    if get_cfg_value("env_imported", False):
        return []

    imported: list[str] = []

    def _do(c: dict):
        # Migrate OPENROUTER_API_KEY if valid and not already stored
        if (OPENROUTER_API_KEY
                and OPENROUTER_API_KEY != "sk-or-v1-YOUR_KEY_HERE"
                and len(OPENROUTER_API_KEY) >= 10
                and not c.get("openrouter_api_key")):
            c["openrouter_api_key"] = OPENROUTER_API_KEY
            imported.append("OPENROUTER_API_KEY")

        # Migrate TELEGRAM_CHAT_ID if valid and not already in chat_ids
        if _is_valid_chat_id(TELEGRAM_CHAT_ID):
            ids = [str(i) for i in c.get("chat_ids", [])]
            if str(TELEGRAM_CHAT_ID) not in ids:
                c["chat_ids"] = ids + [str(TELEGRAM_CHAT_ID)]
                imported.append("TELEGRAM_CHAT_ID")

        c["env_imported"] = True

    mutate_cfg(_do)
    return imported

def _migrate_openrouter_key():
    """Idempotent: move legacy openrouter_api_key → api_keys['openrouter'] (J2).

    Sets default_provider='openrouter' if no default is set and the key exists.
    Does NOT remove openrouter_api_key from config — _apply_defaults repopulates
    it as "" anyway, and _get_provider_key() checks api_keys first.
    """
    def _do(c: dict):
        legacy = c.get("openrouter_api_key", "")
        api_keys = c.setdefault("api_keys", {})
        if legacy and not api_keys.get("openrouter"):
            api_keys["openrouter"] = legacy
        if api_keys.get("openrouter") and not c.get("default_provider"):
            c["default_provider"] = "openrouter"
    mutate_cfg(_do)

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
        _current_lang = get_cfg_value("language", DEFAULT_LANG)
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

# ─── Error classification (U3) ────────────────────────────
# Raw exception strings are noisy and occasionally leak internals into the
# chat (the old code sent `str(e)` straight to the user). classify_error
# folds an exception into a small set of categories so the user sees a
# calm, actionable message — while the full error still goes to the log.
def classify_error(exc) -> str:
    """PURE: map an exception to a coarse category.

    Returns one of: 'timeout', 'rate_limit', 'network', 'not_found',
    'unknown'. Inspects both the exception type and its message text, so it
    works whether the caller raised a typed requests exception or a bare
    Exception wrapping an HTTP/SDK error.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        return "timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "network"
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        return "rate_limit"
    if "timeout" in msg or "timed out" in msg:
        return "timeout"
    if any(s in msg for s in ("not found", "no matching", "no cik",
                              "unknown ticker", "invalid ticker", "404")):
        return "not_found"
    if any(s in msg for s in ("connection", "network", "unreachable",
                              "name resolution", "getaddrinfo", "dns",
                              "temporary failure")):
        return "network"
    return "unknown"

def friendly_fetch_error(ticker: str, exc) -> str:
    """Localized, user-facing message for an EDGAR fetch failure — the raw
    exception is kept out of the chat and only the category is shown."""
    return t(f"fetch_error_{classify_error(exc)}", ticker=ticker)

# ─── Retry / backoff (R5) ─────────────────────────────────
# A single home for the exponential-backoff math and the generic retry
# loop. Before R5 the formula `min(cap, base * 2**attempt)` was inlined in
# six places with slightly different constants; now _backoff is the one
# source of truth and retry() wraps the plain "try N times" loops.
def _backoff(attempt: int, base: int, cap: int) -> int:
    """PURE: exponential backoff delay — base * 2**attempt, capped at cap."""
    return min(cap, base * (2 ** attempt))

def retry(fn, *, attempts: int = 4, base: int = 5, cap: int = 120,
          label: str = "operation", on_error=None):
    """
    Call `fn()` up to `attempts` times with exponential backoff between
    failed attempts. Returns fn()'s value on the first success, or None if
    every attempt raised.

    `fn` signals a retryable failure by raising. `on_error(exc, attempt)`,
    when given, runs after each failure (e.g. to bump an error counter).
    The wait after attempt i is _backoff(i, base, cap) seconds; the final
    attempt is not followed by a sleep.
    """
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as e:
            if on_error is not None:
                try:
                    on_error(e, attempt)
                except Exception as cb_err:
                    log.debug(f"retry on_error callback failed: {cb_err}")
            last = attempt == attempts - 1
            wait = _backoff(attempt, base, cap)
            log.error(f"{label} (attempt {attempt+1}/{attempts}): {e}"
                      + ("" if last else f" — waiting {wait}s"))
            if not last:
                time.sleep(wait)
    return None

# ─── Cache ────────────────────────────────────────────────
# Cache layout: {md5(ticker_form_date): {"at": iso_timestamp}}.
# `cache_max_age_days` (default 365) caps how long an entry survives;
# expired entries are pruned at startup by `prune_cache_expired()`.
CACHE_DEFAULT_MAX_AGE_DAYS = 365

def load_cache() -> dict:
    with _cache_lock:
        data = _read_json(CACHE_FILE, {})
        return data if isinstance(data, dict) else {}

def save_cache(c: dict):
    with _cache_lock:
        _atomic_write_json(CACHE_FILE, c)

def cache_key(*args) -> str:
    return hashlib.md5("_".join(str(a) for a in args).encode()).hexdigest()

def is_new_in_cache(ob, *args) -> bool:
    return cache_key(*args) not in ob

def mark_processed(ob, *args):
    ob[cache_key(*args)] = {"at": datetime.now().isoformat()}

# ─── Price action (E1) ────────────────────────────────────
# Uses yfinance (optional dep — same as /checkprice and /checknews).
# When yfinance is absent, price snippets are silently omitted.
# Layout of price_cache.json:
#   {"AAPL_2026-04-01_5": {start_date, start_close, end_date, end_close, pct}}
# Cache key bakes in the lookforward so changing the config never serves stale.

def _compute_price_change(rows: list, filing_date: str,
                          lookforward_days: int) -> dict | None:
    """
    PURE: given (date, close) rows, find close on/after filing_date and close on
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

def load_price_cache() -> dict:
    with _price_lock:
        data = _read_json(PRICE_CACHE, {})
        return data if isinstance(data, dict) else {}

def save_price_cache(data: dict):
    with _price_lock:
        _atomic_write_json(PRICE_CACHE, data)

def compute_price_snippet(ticker: str, filing_date: str) -> str:
    """
    IO: orchestrate cache → fetch → parse → format. Returns "" on disable,
    cache-miss-with-fetch-fail, or unparseable response.
    """
    cfg = get_chat_cfg()
    if not cfg.get("price_action_enabled", True):
        return ""
    try:
        lookforward = int(cfg.get("price_lookforward_days", 5))
    except (ValueError, TypeError):
        lookforward = 5

    if not YF_OK:
        return ""

    cache = load_price_cache()
    cache_k = f"{ticker}_{filing_date}_{lookforward}"
    if cache_k in cache:
        return _format_price_snippet(cache[cache_k])

    # Fetch enough history to cover from filing_date to filing_date + lookforward.
    # yfinance history() is relative to today, so compute calendar days needed.
    try:
        filing_dt = datetime.fromisoformat(filing_date)
    except Exception:
        return ""
    days_needed = max(30, (datetime.now() - filing_dt).days + lookforward + 20)
    rows_raw = fetch_yfinance_history(ticker, days_needed)
    if not rows_raw:
        return ""
    # _compute_price_change expects [(date_str, close), ...] pairs.
    rows = [(r[0], r[4]) for r in rows_raw]
    change = _compute_price_change(rows, filing_date, lookforward)
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

def _format_news_list_rich(ticker: str, items: list, count: int) -> str:
    """PURE: GFM-rich counterpart to _format_news_list for sendRichMessage."""
    if not items:
        return ""
    items = items[:count]
    lines = [t("checknews_rich_header", ticker=ticker, count=len(items))]
    for raw in items:
        n = _news_extract(raw)
        lines.append(t("checknews_rich_item",
                       title=n["title"], url=n["url"],
                       provider=n["provider"], date=n["date"]))
    return "\n".join(lines)


def fetch_yfinance_history(ticker: str, days: int) -> list | None:
    """IO: returns rows sorted ascending or None on failure."""
    if not YF_OK:
        return None
    def _call():
        h = yf.Ticker(ticker).history(period=f"{days}d")
        if h is None or h.empty:
            return []
        return [
            (idx.strftime("%Y-%m-%d"),
             float(r["Open"]), float(r["High"]),
             float(r["Low"]),  float(r["Close"]))
            for idx, r in h.iterrows()
        ]
    return retry(_call, attempts=3, base=5, cap=60,
                 label=f"yfinance history {ticker}",
                 on_error=lambda e, a: status_inc("yf_errors"))

def fetch_yfinance_news(ticker: str) -> list | None:
    """IO: returns raw news list or None on failure."""
    if not YF_OK:
        return None
    return retry(lambda: yf.Ticker(ticker).news or [],
                 attempts=3, base=5, cap=60,
                 label=f"yfinance news {ticker}",
                 on_error=lambda e, a: status_inc("yf_errors"))

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
    body = _format_news_list(ticker, items, count)
    rich = _format_news_list_rich(ticker, items, count)
    tg(body, rich_md=rich or None)

# ─── Daily news digest (N2) ─────────────────────────────

def _daily_news_fresh(items: list, today_iso: str, count: int) -> list:
    """PURE: filter raw yfinance news to today's date, normalized, max count."""
    result = []
    for raw in items:
        n = _news_extract(raw)
        if n["date"] == today_iso:
            result.append(n)
        if len(result) >= count:
            break
    return result

def _format_daily_news(news_by_ticker: dict, today_iso: str, per_ticker: int = 3) -> str:
    """PURE: build daily news digest body from {ticker: raw_items} map.

    Returns '' if no ticker has fresh news today.
    """
    sections = []
    for ticker, raw_items in news_by_ticker.items():
        fresh = _daily_news_fresh(raw_items or [], today_iso, per_ticker)
        if fresh:
            sections.append(t("dailynews_ticker", ticker=ticker))
            for n in fresh:
                sections.append(t("dailynews_item",
                                  title=n["title"], url=n["url"],
                                  provider=n["provider"], date=n["date"]))
    if not sections:
        return ""
    header = t("dailynews_header", date=today_iso)
    return header + "\n" + "\n".join(sections)

def _format_daily_news_rich(news_by_ticker: dict, today_iso: str, per_ticker: int = 3) -> str:
    """PURE: GFM-rich counterpart to _format_daily_news for sendRichMessage."""
    sections = []
    for ticker, raw_items in news_by_ticker.items():
        fresh = _daily_news_fresh(raw_items or [], today_iso, per_ticker)
        if fresh:
            sections.append(t("dailynews_rich_ticker", ticker=ticker))
            for n in fresh:
                sections.append(t("dailynews_rich_item",
                                  title=n["title"], url=n["url"],
                                  provider=n["provider"], date=n["date"]))
    if not sections:
        return ""
    header = t("dailynews_rich_header", date=today_iso)
    return header + "\n" + "\n".join(sections)


def send_daily_news() -> bool:
    """IO: send daily news digest for current chat. Returns True if sent."""
    if not YF_OK:
        return False
    cfg = get_chat_cfg()
    tickers = cfg.get("tickers", [])
    if not tickers:
        return False
    today_iso = date.today().isoformat()
    news_by_ticker: dict = {}
    for tk in tickers:
        news_by_ticker[tk] = fetch_yfinance_news(tk) or []
        time.sleep(0.5)
    body = _format_daily_news(news_by_ticker, today_iso)
    if body:
        rich_body = _format_daily_news_rich(news_by_ticker, today_iso)
        tg(body, rich_md=rich_body or None)
        return True
    return False

def cmd_dailynews(parts: list) -> str:
    """Usage: /dailynews [on|off|now]"""
    if len(parts) >= 2 and parts[1].lower() == "off":
        mutate_chat_cfg(lambda c: c.update({"daily_news": False}))
        return t("dailynews_disabled")
    if len(parts) >= 2 and parts[1].lower() == "now":
        sent = send_daily_news()
        return t("dailynews_sent") if sent else t("dailynews_none")
    mutate_chat_cfg(lambda c: c.update({"daily_news": True}))
    return t("dailynews_enabled")

def cmd_setrich(parts: list) -> str:
    """Usage: /setrich [on|off] — toggle rich-message formatting for this chat."""
    if len(parts) >= 2 and parts[1].lower() == "off":
        mutate_chat_cfg(lambda c: c.update({"rich_format": False}))
        return t("rich_disabled")
    mutate_chat_cfg(lambda c: c.update({"rich_format": True}))
    return t("rich_enabled")

def cmd_richtest():
    """Admin/master-gated: send a canonical rich-markdown sample end-to-end so
    the user can visually confirm rich rendering in their real client. If the
    rich transport reports failure, reply that rich is unsupported."""
    cid = getattr(_ctx, "chat_id", None)
    if not cid or not _is_admin(cid):
        tg(t("unauthorized_admin"))
        return
    if not _tg_send_rich_to(cid, t("rich_test_sample")):
        tg(t("rich_unsupported"))

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
        data = _read_json(SENT_HIST, {})
        return data if isinstance(data, dict) else {}

def save_sentiment_history(data: dict):
    with _sent_lock:
        _atomic_write_json(SENT_HIST, data)

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
_WLOG_CAP = 500   # keep last N entries; independent of raw_max config

def _weekly_log_path(chat_id: str | None = None) -> Path:
    """Return per-chat weekly log path (or global fallback for background threads)."""
    if chat_id:
        return CHAT_DIR / f"weekly_log_{chat_id}.json"
    return WEEKLY_LOG

def log_weekly(ticker: str, form: str, date_str: str, analysis: str):
    """Append a full analysis to the weekly log. Truncation happens at digest time."""
    cid = getattr(_ctx, "chat_id", None)
    path = _weekly_log_path(cid)
    with _wlog_lock:
        data = _read_json(path, [])
        if not isinstance(data, list):
            data = []
        data.append({
            "ticker": ticker, "form": form, "tarih": date_str,
            "analiz": analysis,
            "ekleme": datetime.now().isoformat(),
        })
        data = data[-_WLOG_CAP:]
        _atomic_write_json(path, data)

def get_weekly_log() -> list:
    cid = getattr(_ctx, "chat_id", None)
    path = _weekly_log_path(cid)
    with _wlog_lock:
        data = _read_json(path, [])
        return data if isinstance(data, list) else []

def clear_weekly_log():
    cid = getattr(_ctx, "chat_id", None)
    path = _weekly_log_path(cid)
    with _wlog_lock:
        _atomic_write_json(path, [])

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
_TG_LIMIT = 4000

# ─── Rich message transport (Bot API 10.1 — Wave O / O1) ──
# Lazy, cached, startup-spam-free capability flag for `sendRichMessage`:
#   None  = unknown (never attempted yet) — first attempt decides
#   True  = this process has confirmed rich support
#   False = this process learned rich is unsupported — never re-attempt
# Only callers passing an explicit `rich_md` ever exercise the rich path; ALL
# production callers in O1 pass rich_md=None, so the legacy path is byte-identical.
_RICH_CAP: bool | None = None

def _classify_rich_error(status: int, body: str) -> str:
    """PURE: classify a failed sendRichMessage response into a fallback action.

    Returns one of:
      "unsupported" — the API/method does not exist (404, "method not found",
                      "unknown method") → cap goes permanently False.
      "content"     — 400 markdown/parse/content error for THIS message →
                      fall back for this message only; cap unchanged.
      "transient"   — 429 / 5xx / network / empty body → retry with backoff.
    Deterministic; offline-testable with synthetic (status, body).
    """
    low = (body or "").lower()
    if status == 404 or "method not found" in low or "unknown method" in low:
        return "unsupported"
    if status == 429 or 500 <= status <= 599:
        return "transient"
    if status == 400:
        return "content"
    if status == 0 or status is None:
        return "transient"
    # Any other 4xx with a content-ish body is treated as a content error;
    # anything else (unexpected) is transient so we retry rather than give up.
    return "content" if 400 <= status < 500 else "transient"

def _rich_enabled(chat_id: str) -> bool:
    """Per-chat gate: that chat's rich_format pref (default True) AND the
    process-level cap is not known-False (None/unknown still allowed to try)."""
    if _RICH_CAP is False:
        return False
    return bool(get_chat_cfg().get("rich_format", True))

def _tg_send_rich_to(chat_id: str, markdown: str, keyboard: dict | None = None) -> bool:
    """Transport primitive: POST `markdown` to sendRichMessage. Returns True on
    success, False on any failure. NEVER raises — the caller decides on fallback.

    Honors the lazy capability flag, classifies failures, retries transient
    errors with the same 4-attempt/429 logic as _tg_to.
    """
    global _RICH_CAP
    if _RICH_CAP is False:
        return False
    payload: dict = {
        "chat_id": chat_id,
        "rich_message": {"markdown": markdown},
        "disable_notification": False,
    }
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    for attempt in range(4):
        try:
            r = requests.post(f"{_TG}/sendRichMessage", json=payload, timeout=15)
            if 200 <= r.status_code < 300:
                _RICH_CAP = True
                status_reset_zero("tg_errors")
                return True
            body = ""
            try:
                body = r.text or ""
            except Exception:
                body = ""
            kind = _classify_rich_error(r.status_code, body)
            if kind == "unsupported":
                _RICH_CAP = False
                return False
            if kind == "content":
                return False
            # transient — backoff and retry
            if r.status_code == 429:
                wait_sec = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
            else:
                wait_sec = _backoff(attempt, 3, 60)
            log.warning(f"sendRichMessage transient ({r.status_code}) — waiting {wait_sec}s")
            time.sleep(wait_sec)
        except Exception as e:
            status_inc("tg_errors")
            wait_sec = _backoff(attempt, 3, 60)
            log.error(f"sendRichMessage (attempt {attempt+1}): {e} — waiting {wait_sec}s")
            time.sleep(wait_sec)
    return False

_RICH_DRAFT_CAP: bool | None = None


def _is_private_chat(chat_id: str) -> bool:
    """PURE: True if chat_id looks like a private (positive) Telegram chat."""
    try:
        return int(chat_id) > 0
    except (ValueError, TypeError):
        return False


def _thinking_draft_md(ticker: str, form: str) -> str:
    """PURE: GFM rich-markdown with <tg-thinking> block for sendRichMessageDraft."""
    return t("filing_thinking_draft", ticker=ticker, form=form)


def _tg_send_rich_draft_to(chat_id: str, markdown: str, draft_id: int) -> bool:
    """Transport primitive: POST to sendRichMessageDraft. Returns True on
    success, False on failure. NEVER raises — caller decides on fallback.

    Uses _classify_rich_error (shared with _tg_send_rich_to) but writes
    ONLY to _RICH_DRAFT_CAP — _RICH_CAP is never touched (isolation).
    """
    global _RICH_DRAFT_CAP
    if _RICH_DRAFT_CAP is False:
        return False
    payload: dict = {
        "chat_id": int(chat_id),
        "draft_id": draft_id,
        "rich_message": {"markdown": markdown},
    }
    for attempt in range(4):
        try:
            r = requests.post(f"{_TG}/sendRichMessageDraft", json=payload, timeout=15)
            if 200 <= r.status_code < 300:
                _RICH_DRAFT_CAP = True
                status_reset_zero("tg_errors")
                return True
            body = ""
            try:
                body = r.text or ""
            except Exception:
                body = ""
            kind = _classify_rich_error(r.status_code, body)
            if kind == "unsupported":
                _RICH_DRAFT_CAP = False
                return False
            if kind == "content":
                return False
            if r.status_code == 429:
                wait_sec = int(r.headers.get("Retry-After", 5 * (attempt + 1)))
            else:
                wait_sec = _backoff(attempt, 3, 60)
            log.warning(f"sendRichMessageDraft transient ({r.status_code}) — waiting {wait_sec}s")
            time.sleep(wait_sec)
        except Exception as e:
            status_inc("tg_errors")
            wait_sec = _backoff(attempt, 3, 60)
            log.error(f"sendRichMessageDraft (attempt {attempt+1}): {e} — waiting {wait_sec}s")
            time.sleep(wait_sec)
    return False


def _maybe_post_thinking_draft(ticker: str, form: str, quiet: bool) -> bool:
    """Best-effort: show a <tg-thinking> draft before LLM analysis.

    Returns True if a draft was posted (best-effort; never raises).
    Gate: quiet=False, _ctx.chat_id set, private-only, _RICH_DRAFT_CAP not False,
    _rich_enabled.
    """
    if quiet:
        return False
    cid = getattr(_ctx, "chat_id", None)
    if not cid:
        return False
    if not _is_private_chat(cid):
        return False
    if _RICH_DRAFT_CAP is False:
        return False
    if not _rich_enabled(cid):
        return False
    draft_id = (abs(hash((ticker, form))) % 2_000_000_000) + 1
    return _tg_send_rich_draft_to(cid, _thinking_draft_md(ticker, form), draft_id)


def _split_message(text: str, limit: int = _TG_LIMIT) -> list:
    """PURE: split text into Telegram-safe chunks (≤ limit chars).

    Prefers newline boundaries; hard-splits any single line that itself
    exceeds the limit so it never produces an oversized chunk.
    """
    parts: list = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            # flush current buffer first
            if current.strip():
                parts.append(current)
                current = ""
            # hard-split the oversized line
            for i in range(0, len(line), limit):
                parts.append(line[i : i + limit])
        elif current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = (current + "\n" + line) if current else line
    if current.strip():
        parts.append(current)
    return parts

def _tg_to(chat_id: str, text: str, rich_md: str | None = None):
    """Primitive: send `text` to one specific Telegram chat (chunked, backoff, Markdown fallback).

    When `rich_md` is provided AND the chat has rich enabled AND the rich
    transport succeeds, the message is delivered via sendRichMessage and we
    return early. When `rich_md is None` (every production caller in O1) the
    rich path is NOT attempted at all and the legacy path below runs unchanged.
    """
    if rich_md is not None and _rich_enabled(chat_id) and _tg_send_rich_to(chat_id, rich_md):
        return                       # rich delivered → done
    for part in _split_message(text):
        sent = False
        for attempt in range(4):
            try:
                r = requests.post(f"{_TG}/sendMessage", json={
                    "chat_id": chat_id,
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
                        "chat_id": chat_id,
                        "text": part.strip(),
                        "disable_web_page_preview": True,
                    }, timeout=15)
                    if r2.ok:
                        status_reset_zero("tg_errors")
                        sent = True
                    break
                r.raise_for_status()
                status_reset_zero("tg_errors")
                sent = True
                break
            except Exception as e:
                status_inc("tg_errors")
                wait_sec = _backoff(attempt, 3, 60)
                log.error(f"Telegram (attempt {attempt+1}): {e} — waiting {wait_sec}s")
                time.sleep(wait_sec)
        # [H7] A message that never reached Telegram used to vanish silently
        # (only a log.error per attempt). Now total failure is loud: a
        # CRITICAL log line + a stderr write so a Termux operator watching
        # the console actually sees that delivery failed.
        if not sent:
            log.critical(f"Telegram message NOT delivered after 4 attempts: "
                         f"{part.strip()[:80]!r}")
            sys.stderr.write("[CRITICAL] Telegram delivery failed — "
                             "message lost. Check network / bot token.\n")
        time.sleep(0.3)

def broadcast(text: str, rich_md: str | None = None):
    """Send `text` to ALL authorized chat_ids (proactive/background messages).
    Each chat's failure is isolated — one blocked bot does not stop the rest."""
    for cid in get_cfg_value("chat_ids", []):
        try:
            _tg_to(str(cid), text, rich_md=rich_md)
        except Exception as e:
            log.debug(f"broadcast to {cid}: {e}")

def tg(text: str, rich_md: str | None = None):
    """Send a Telegram message.

    Reactive context (inside handle_update): sends only to the requesting chat.
    No context (background thread / startup): broadcasts to all chat_ids.
    """
    cid = getattr(_ctx, "chat_id", None)
    if cid:
        _tg_to(cid, text, rich_md=rich_md)
    else:
        broadcast(text, rich_md=rich_md)

def _tg_send_document_to(chat_id: str, filename: str, content: str, caption: str = ""):
    """Primitive: send a document to one specific chat."""
    url = f"{_TG}/sendDocument"
    raw = content.encode("utf-8")
    def _call():
        r = requests.post(url, data={
            "chat_id": chat_id,
            "caption": caption[:1024] if caption else "",
            "parse_mode": "Markdown",
        }, files={
            "document": (filename, io.BytesIO(raw), "text/plain"),
        }, timeout=30)
        r.raise_for_status()
    retry(_call, attempts=3, base=5, cap=30, label=f"tg_send_document:{filename}",
          on_error=lambda e, a: status_inc("tg_errors"))

def tg_send_document(filename: str, content: str, caption: str = ""):
    """Send a document to the reactive context chat, or broadcast if no context."""
    cid = getattr(_ctx, "chat_id", None)
    chats = [cid] if cid else [str(c) for c in get_cfg_value("chat_ids", [])]
    for chat in chats:
        try:
            _tg_send_document_to(str(chat), filename, content, caption)
        except Exception as e:
            log.debug(f"tg_send_document to {chat}: {e}")

def tg_answer_callback(callback_id: str, text: str = ""):
    """Acknowledge an inline-button callback (Telegram requirement)."""
    try:
        requests.post(f"{_TG}/answerCallbackQuery", json={
            "callback_query_id": callback_id,
            "text": text,
        }, timeout=10)
    except Exception as e:
        log.error(f"answerCallbackQuery: {e}")

# ─── Bot command menu (U4) ────────────────────────────────
_BOT_COMMANDS = [
    ("scan",         "cmd_desc_scan"),
    ("all",          "cmd_desc_all"),
    ("insider",      "cmd_desc_insider"),
    ("scanticker",   "cmd_desc_scanticker"),
    ("compare",      "cmd_desc_compare"),
    ("sentiment",    "cmd_desc_sentiment"),
    ("checkprice",   "cmd_desc_checkprice"),
    ("checknews",    "cmd_desc_checknews"),
    ("dailynews",    "cmd_desc_dailynews"),
    ("setrich",      "cmd_desc_setrich"),
    ("sheet",        "cmd_desc_sheet"),
    ("fulltext",     "cmd_desc_fulltext"),
    ("search",       "cmd_desc_search"),
    ("company",      "cmd_desc_company"),
    ("listtickers",  "cmd_desc_listtickers"),
    ("addticker",    "cmd_desc_addticker"),
    ("removeticker", "cmd_desc_removeticker"),
    ("listforms",    "cmd_desc_listforms"),
    ("addpos",       "cmd_desc_addpos"),
    ("removepos",    "cmd_desc_removepos"),
    ("pnl",          "cmd_desc_pnl"),
    ("addword",      "cmd_desc_addword"),
    ("removeword",   "cmd_desc_removeword"),
    ("listwords",    "cmd_desc_listwords"),
    ("report",       "cmd_desc_report"),
    ("export",       "cmd_desc_export"),
    ("settings",     "cmd_desc_settings"),
    ("status",       "cmd_desc_status"),
    ("help",         "cmd_desc_help"),
]

def register_bot_commands():
    """Call Telegram setMyCommands so the '/' menu is populated."""
    commands = [{"command": cmd, "description": t(key)} for cmd, key in _BOT_COMMANDS]
    try:
        r = requests.post(f"{_TG}/setMyCommands", json={"commands": commands}, timeout=10)
        if r.ok:
            log.info("setMyCommands: OK")
        else:
            log.warning(f"setMyCommands: {r.status_code} {r.text[:120]}")
    except Exception as e:
        log.warning(f"setMyCommands failed: {e}")

def build_inline_button(raw_key: str) -> dict:
    """Inline keyboard for an analysis message: view raw filing + get .md."""
    return {
        "inline_keyboard": [[
            {"text": t("view_original_button"), "callback_data": f"raw:{raw_key}"},
            {"text": t("md_report_button"),     "callback_data": f"md:{raw_key}"},
        ]]
    }

def _tg_with_keyboard_to(chat_id: str, text: str, keyboard: dict | None, rich_md: str | None = None):
    """Primitive: send a (chunked) message with keyboard to one specific chat.

    When `rich_md` is provided AND the chat has rich enabled AND the rich
    transport succeeds (keyboard threaded through), we return early. When
    `rich_md is None` (every production caller in O1) the rich path is NOT
    attempted and the legacy keyboard path below runs unchanged.
    """
    if rich_md is not None and _rich_enabled(chat_id) and _tg_send_rich_to(chat_id, rich_md, keyboard):
        return                       # rich delivered → done
    parts, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 4000:
            parts.append(current); current = line
        else:
            current += "\n" + line
    if current.strip(): parts.append(current)

    for i, part in enumerate(parts):
        payload: dict = {
            "chat_id": chat_id,
            "text": part.strip(),
            "parse_mode": "Markdown",
            "disable_web_page_preview": True,
        }
        if i == len(parts) - 1 and keyboard is not None:
            payload["reply_markup"] = keyboard
        def _send(p=dict(payload)):
            r = requests.post(f"{_TG}/sendMessage", json=p, timeout=15)
            if r.status_code == 400:           # Markdown parse error — plain fallback
                p2 = {k: v for k, v in p.items() if k != "parse_mode"}
                r2 = requests.post(f"{_TG}/sendMessage", json=p2, timeout=15)
                r2.raise_for_status()
            else:
                r.raise_for_status()
        retry(_send, attempts=3, base=3, cap=30, label="tg_with_keyboard",
              on_error=lambda e, a: status_inc("tg_errors"))
        time.sleep(0.3)

def tg_with_keyboard(text: str, keyboard: dict | None, rich_md: str | None = None):
    """Send message+keyboard to the reactive context chat, or broadcast if no context."""
    cid = getattr(_ctx, "chat_id", None)
    chats = [cid] if cid else [str(c) for c in get_cfg_value("chat_ids", [])]
    for chat in chats:
        try:
            _tg_with_keyboard_to(str(chat), text, keyboard, rich_md=rich_md)
        except Exception as e:
            log.debug(f"tg_with_keyboard to {chat}: {e}")

def tg_with_button(text: str, raw_key: str, rich_md: str | None = None):
    """Send an analysis message with its inline buttons (view raw + .md)."""
    tg_with_keyboard(text, build_inline_button(raw_key), rich_md=rich_md)

def tg_edit_markup(chat_id, message_id, keyboard: dict | None):
    """editMessageReplyMarkup — replace, or (keyboard=None) remove, a message's
    inline keyboard. Used to retire alarm buttons once they are acted on."""
    if chat_id is None or message_id is None:
        return
    payload: dict = {"chat_id": chat_id, "message_id": message_id}
    if keyboard is not None:
        payload["reply_markup"] = keyboard
    # keyboard is None → reply_markup omitted → Telegram strips the keyboard.
    try:
        requests.post(f"{_TG}/editMessageReplyMarkup", json=payload, timeout=15)
    except Exception as e:
        log.error(f"tg_edit_markup: {e}")

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

def send_md_for_key(callback_id: str, raw_key: str):
    """Send the stored analysis as a .md file — triggered by the '.md' button.

    The .md is no longer pushed automatically after every analysis; it is
    produced on demand here from the data parked in the raw-filing store.
    """
    tg_answer_callback(callback_id, t("preparing_document"))
    entry_data = get_raw_filing(raw_key)
    if not entry_data:
        tg(t("raw_filing_not_found"))
        return
    send_md_analysis(entry_data["ticker"], entry_data["form"],
                     entry_data["tarih"],
                     entry_data.get("analysis", ""),
                     entry_data.get("diff", ""))

# ─── Interactive alarm alert (Item 4) ─────────────────────
def build_alarm_keyboard(token: str, hits: list, done: set) -> dict | None:
    """
    PURE: build the alarm message's inline keyboard.

    One [🔍 TICKER FORM] button per not-yet-analyzed hit, plus an
    [🔍 Analyze all] button when more than one remains. Returns None when
    every hit is done — the caller then strips the keyboard entirely.
    """
    remaining = [i for i in range(len(hits)) if i not in done]
    if not remaining:
        return None
    rows = []
    for i in remaining:
        ticker, form, _date = hits[i]
        rows.append([{
            "text": t("alarm_btn_one", ticker=ticker, form=form),
            "callback_data": f"analyze:{token}:{i}",
        }])
    if len(remaining) > 1:
        rows.append([{
            "text": t("alarm_btn_all"),
            "callback_data": f"analyzeall:{token}",
        }])
    return {"inline_keyboard": rows}

def send_alarm_alert(hits: list):
    """Send the interactive new-filing alert: a line per filing + buttons."""
    token = register_alarm_hits(hits)
    lines = [t("alarm_alert_header", count=len(hits))]
    for ticker, form, date_str in hits:
        lines.append(t("alarm_alert_line", ticker=ticker, form=form, date=date_str))
    tg_with_keyboard("\n".join(lines),
                     build_alarm_keyboard(token, hits, set()))

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
            # [H7] Same per-update crash guard as the polling loop.
            try:
                handle_update(update)
            except Exception as e:
                log.exception(f"Webhook update processing failed: {e}")
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
    """getUpdates — exponential backoff via retry()."""
    def _call():
        r = requests.get(f"{_TG}/getUpdates",
                         params={"offset": offset, "timeout": 30}, timeout=35)
        r.raise_for_status()
        status_set(last_update=datetime.now().isoformat())
        status_reset_zero("tg_errors")
        return r.json().get("result", [])
    result = retry(_call, attempts=4, base=5, cap=120, label="getUpdates",
                   on_error=lambda e, a: status_inc("tg_errors"))
    return result if result is not None else []

# ─── OpenRouter LLM ───────────────────────────────────────
_OR  = "https://openrouter.ai/api/v1/chat/completions"
_ORH = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/sec-analyzer-bot",
    "X-Title": "SEC Analyzer Bot",
}

def _get_or_headers() -> dict:
    """Legacy header builder — superseded by _get_provider_key() in J2.
    Kept for reference; llm() no longer uses it directly.
    """
    key = get_cfg_value("openrouter_api_key") or OPENROUTER_API_KEY
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type":  "application/json",
        "HTTP-Referer":  "https://github.com/sec-analyzer-bot",
        "X-Title":       "SEC Analyzer Bot",
    }

# ─── Multi-LLM provider registry (J2) ─────────────────────
# Pure HTTP for all providers (Termux principle — no SDKs).
# Each entry defines: endpoint template, default model, wire type.
_PROVIDERS: dict = {
    "openrouter": {
        "endpoint": "https://openrouter.ai/api/v1/chat/completions",
        "model":    "openrouter/auto",
        "type":     "openai",
    },
    "groq": {
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        "model":    "llama-3.3-70b-versatile",
        "type":     "openai",
    },
    "anthropic": {
        "endpoint": "https://api.anthropic.com/v1/messages",
        "model":    "claude-haiku-4-5-20251001",
        "type":     "anthropic",
    },
    "gemini": {
        "endpoint": "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        "model":    "gemini-2.0-flash",
        "type":     "gemini",
    },
    "deepseek": {
        "endpoint": "https://api.deepseek.com/chat/completions",
        "model":    "deepseek-v4-flash",
        "type":     "openai",
    },
}

# Available models per provider (user-selectable via /setapi)
_PROVIDER_MODELS: dict[str, list[str]] = {
    "openrouter": [
        "openrouter/auto",
        "openrouter/free",
        "openrouter/owl-alpha",
        "meta-llama/llama-3.3-70b-instruct:free",
        "google/gemma-3-27b-it:free",
        "deepseek/deepseek-chat-v3-0324:free",
    ],
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
    ],
    "anthropic": [
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-20250514",
    ],
    "gemini": [
        "gemini-2.0-flash",
        "gemini-2.5-flash-preview-04-17",
    ],
    "deepseek": [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ],
}

# Sentinel returned by _llm_one on 401 (invalid key) — distinct from None (other failure).
_AUTH_FAIL = object()

# Pending API-key intake: {chat_id: {"provider": str, "expires": float}}
_pending_api_key: dict = {}
_pending_lock = threading.Lock()

# Last failed LLM prompt for retry button: {chat_id: {"istem": str, "model": str, "remaining": list}}
_retry_prompt: dict = {}
_retry_lock = threading.Lock()


def _mask_key(key: str) -> str:
    """Return masked representation for display: first 4 chars + '…'."""
    if not key:
        return "…"
    return key[:4] + "…"

# Provider-specific key prefix expectations (non-fatal — advisory warning)
_PROVIDER_KEY_PREFIXES: dict[str, str] = {
    "openrouter": "sk-or-v1-",
    "anthropic":  "sk-ant-",
    "groq":       "gsk_",
    "deepseek":   "ds-",
}

def _validate_provider_key(provider: str, key: str) -> str | None:
    """Warn if key doesn't match expected prefix for provider. Returns i18n msg or None."""
    expected = _PROVIDER_KEY_PREFIXES.get(provider)
    if expected and not key.startswith(expected):
        return t("addapi_key_prefix_warn",
                 provider=provider, expected=expected, got=key[:6] + "…")
    return None


def _get_provider_key(provider: str) -> str:
    """Return the API key for *provider*.

    Priority (highest first):
      1. cfg['api_keys'][provider]  — set via /addapi or migration
      2. cfg['openrouter_api_key']  — legacy pre-J2 config (openrouter only)
      3. OPENROUTER_API_KEY env var — very legacy (openrouter only)
    """
    cfg = get_chat_cfg()
    key = cfg.get("api_keys", {}).get(provider, "")
    if not key and provider == "openrouter":
        key = cfg.get("openrouter_api_key", "") or OPENROUTER_API_KEY
    return key or ""


def _ordered_providers() -> list[str]:
    """Return provider names that have a key, default-provider first.

    Falls back to _PROVIDERS insertion order for ties.
    """
    cfg = get_chat_cfg()
    default = cfg.get("default_provider", "")
    available = []
    for p in _PROVIDERS:
        if _get_provider_key(p):
            available.append(p)
    if not available:
        return []
    if default in available:
        return [default] + [p for p in available if p != default]
    return available


# ─── Response parsers (pure — easy to unit-test) ──────────

def _parse_openai_resp(body: dict) -> str:
    """Extract content from an OpenAI-compatible chat/completions response."""
    choices = body.get("choices") or []
    return (choices[0].get("message", {}).get("content", "") if choices else "").strip()


def _parse_anthropic_resp(body: dict) -> str:
    """Extract content from an Anthropic Messages API response."""
    content_list = body.get("content") or []
    return (content_list[0].get("text", "") if content_list else "").strip()


def _parse_gemini_resp(body: dict) -> str:
    """Extract content from a Gemini generateContent response."""
    candidates = body.get("candidates") or []
    if not candidates:
        return ""
    parts_list = candidates[0].get("content", {}).get("parts") or []
    return (parts_list[0].get("text", "") if parts_list else "").strip()


def _get_provider_model(provider: str) -> str:
    """Return the active model for a provider: config override → _PROVIDERS default."""
    stored = get_chat_cfg().get("provider_models", {}).get(provider)
    if stored:
        return stored
    return _PROVIDERS.get(provider, {}).get("model", "")


def _llm_one(istem: str, model: str, provider: str):
    """Single-attempt LLM call to *provider*.

    Returns:
      str        — content on success
      _AUTH_FAIL — 401 (invalid key)
      None       — any other failure (429, 5xx, timeout, parse error)

    No retries; caller handles fallback / backoff.
    Keys are never logged in full.
    """
    prov = _PROVIDERS.get(provider)
    if prov is None:
        return None
    key = _get_provider_key(provider)
    if not key:
        return None

    ptype = prov["type"]
    p_model = _get_provider_model(provider) or model

    if ptype == "openai":
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        if provider == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/sec-analyzer-bot"
            headers["X-Title"]      = "SEC Analyzer Bot"
        payload  = {
            "model": p_model,
            "messages": [
                {"role": "system", "content": system_message()},
                {"role": "user",   "content": istem},
            ],
            "max_tokens": 1200, "temperature": 0.2,
        }
        endpoint = prov["endpoint"]
        parser   = _parse_openai_resp

    elif ptype == "anthropic":
        headers  = {
            "x-api-key":          key,
            "anthropic-version":  "2023-06-01",
            "Content-Type":       "application/json",
        }
        payload  = {
            "model":      p_model,
            "max_tokens": 1200,
            "system":     system_message(),
            "messages":   [{"role": "user", "content": istem}],
        }
        endpoint = prov["endpoint"]
        parser   = _parse_anthropic_resp

    elif ptype == "gemini":
        headers  = {"Content-Type": "application/json"}
        payload  = {
            "systemInstruction": {"parts": [{"text": system_message()}]},
            "contents": [{"parts": [{"text": istem}]}],
            "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.2},
        }
        endpoint = prov["endpoint"].format(model=p_model) + f"?key={key}"
        parser   = _parse_gemini_resp

    else:
        return None

    try:
        r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
        if r.status_code == 401:
            log.warning(f"LLM {provider}: 401 — key {_mask_key(key)} invalid")
            return _AUTH_FAIL
        if r.status_code in (429, 500, 502, 503, 504):
            log.warning(f"LLM {provider}: {r.status_code}")
            return None
        r.raise_for_status()
        content = parser(r.json())
        if not content:
            log.warning(f"LLM {provider}: empty response")
            return None
        status_reset_zero("or_errors")
        return content
    except requests.exceptions.Timeout:
        log.error(f"LLM {provider}: timeout")
        return None
    except Exception as e:
        status_inc("or_errors")
        log.error(f"LLM {provider}: {e}")
        return None

def _tg_delete_msg(chat_id: str, message_id):
    """Best-effort Telegram deleteMessage; failure logged at debug only."""
    if not message_id:
        return
    try:
        requests.post(f"{_TG}/deleteMessage", json={
            "chat_id":    chat_id,
            "message_id": message_id,
        }, timeout=10)
    except Exception as e:
        log.debug(f"deleteMessage {chat_id}/{message_id}: {e}")


def _handle_pending_key(chat_id: str, key_text: str, msg: dict):
    """Process an API key message during a pending /addapi flow.

    Pops the pending entry, validates key, deletes the key message from
    Telegram (best-effort), and persists the key to config (per-user).
    """
    with _pending_lock:
        entry = _pending_api_key.pop(chat_id, None)
    if entry is None or time.time() > entry["expires"]:
        return
    provider = entry["provider"]
    if not key_text or len(key_text) < 8:
        tg(t("addapi_invalid_key_short"))
        return
    _tg_delete_msg(chat_id, msg.get("message_id"))
    _ctx.chat_id = chat_id
    try:
        def _do(c: dict):
            c.setdefault("api_keys", {})[provider] = key_text
            if not c.get("default_provider"):
                c["default_provider"] = provider
        mutate_chat_cfg(_do)
    finally:
        _ctx.chat_id = None
    tg(t("addapi_saved", provider=provider, masked_key=_mask_key(key_text)))
    prefix_warn = _validate_provider_key(provider, key_text)
    if prefix_warn:
        tg(prefix_warn)
    if WIZARD.get("step") == "api":
        tg(t("wizard_api_more"))


def _handle_retry_callback(cq: dict):
    """Handle the 'retry' inline-button press.

    Pops the stored retry entry and tries remaining providers in order.
    If all fail, sends t("analysis_unavailable").
    _ctx.chat_id is already set by _process_update's callback branch.
    """
    with _retry_lock:
        entry = _retry_prompt.pop(str(cq.get("from", {}).get("id", "")), None)
    if entry is None:
        tg_answer_callback(cq["id"], t("llm_retry_expired"))
        return
    tg_answer_callback(cq["id"])
    for provider in entry.get("remaining", []):
        result = _llm_one(entry["istem"], entry["model"], provider)
        if isinstance(result, str):
            tg(result)
            return
        if result is _AUTH_FAIL:
            tg(t("llm_key_invalid", provider=provider))
    tg(t("analysis_unavailable"))


def system_message() -> str:
    """Build the LLM system message, including the active response language."""
    lang_name = lang_meta().get("llm_response_language", "English")
    return (
        "You are an experienced financial analyst specializing in SEC filings. "
        "Analyze documents from an investor's perspective. Be concise, structured, "
        "use bullet points. Highlight key risks and opportunities. Use emojis. "
        f"Respond in {lang_name}."
    )

_LLM_MAX_PROMPT = 30000      # generous cap — avoids false-positives on multi-source prompts
_RAW_TEXT_INLINE_LIMIT = 3500  # chars; above this → sendDocument (J3)
_RAW_TEXT_FILE_MAX = 200_000   # 200 KB byte cap for sendDocument content (J3)


def ai_enabled() -> bool:
    """True if at least one LLM provider has an API key configured.

    Pure: reads config only, no network calls.
    """
    return bool(_ordered_providers())


def _deliver_raw_text(body: str, ticker: str, form: str, date_str: str, warn_key: str):
    """Deliver raw filing text when AI is unavailable (J3).

    ≤3500 chars → tg() inline.  Longer → sendDocument .txt.
    sendDocument failure → inline fallback with first 3500 chars.
    No parse_mode: avoids Markdown errors on raw filing text.
    """
    warning = t(warn_key)
    safe_body = (body or "").strip()

    cid = getattr(_ctx, "chat_id", None)
    chats = [cid] if cid else [str(c) for c in get_cfg_value("chat_ids", [])]

    if not safe_body:
        tg(warning)
        return

    if len(safe_body) <= _RAW_TEXT_INLINE_LIMIT:
        for chat in chats:
            _tg_to(chat, f"{warning}\n\n{safe_body}")
        return

    content = safe_body[:_RAW_TEXT_FILE_MAX]
    if len(safe_body) > _RAW_TEXT_FILE_MAX:
        content += "\n[truncated]"
    filename = f"{ticker}_{form}_{date_str}.txt"

    for chat in chats:
        success = False
        try:
            r = requests.post(
                f"{_TG}/sendDocument",
                data={
                    "chat_id":    chat,
                    "caption":    warning[:1024],
                },
                files={"document": (filename, io.BytesIO(content.encode("utf-8")), "text/plain")},
                timeout=30,
            )
            success = r.ok
            if not r.ok:
                log.debug(f"_deliver_raw_text sendDocument {chat}: {r.status_code}")
        except Exception as e:
            log.debug(f"_deliver_raw_text sendDocument {chat}: {e}")
        if not success:
            _tg_to(chat, f"{warning}\n\n{safe_body[:_RAW_TEXT_INLINE_LIMIT]}")


def _check_no_keys_reminder():
    """Send a NO_KEYS reminder at most once per calendar day (spam gate, J3).

    Writes today's date to cfg['no_keys_warned_date'] after sending.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    cfg = get_cfg()
    if cfg.get("no_keys_warned_date", "") == today:
        return
    mutate_cfg(lambda c: c.update({"no_keys_warned_date": today}))
    tg(t("no_ai_reminder"))


def llm(istem: str, model: str) -> str | None:
    """Multi-provider LLM call with proactive/reactive fallback asymmetry (J2).

    Returns None immediately (no HTTP) when no API keys are configured (J3 NO_KEYS).
    Callers that have source text should deliver raw text on None return.

    Proactive (_ctx.chat_id is None):
        Try providers in order, silently. All fail → t("analysis_unavailable").
    Reactive (_ctx.chat_id set):
        Try default provider only. Fail → offer retry inline button (next provider).
        Return t("analysis_unavailable") immediately (backward-compat — callers
        embed the return value in strings).
    model is forwarded to openrouter only; other providers use their own default.
    """
    if not ai_enabled():
        return None   # NO_KEYS: zero HTTP attempts (J3)

    if len(istem) > _LLM_MAX_PROMPT:
        log.warning(f"LLM prompt clamped: {len(istem)} → {_LLM_MAX_PROMPT} chars")
        istem = istem[:_LLM_MAX_PROMPT]

    providers = _ordered_providers()
    if not providers:
        return t("analysis_unavailable")

    chat_id = getattr(_ctx, "chat_id", None)

    if chat_id:
        # Reactive: try the default (first) provider only.
        provider = providers[0]
        result = _llm_one(istem, model, provider)
        if isinstance(result, str):
            with _retry_lock:
                _retry_prompt.pop(chat_id, None)
            return result
        if result is _AUTH_FAIL:
            tg(t("llm_key_invalid", provider=provider))
        # Offer retry button if a next provider exists.
        if len(providers) > 1:
            next_prov = providers[1]
            with _retry_lock:
                _retry_prompt[chat_id] = {
                    "istem":     istem,
                    "model":     model,
                    "remaining": providers[1:],
                }
            keyboard = {"inline_keyboard": [[{
                "text":          t("llm_retry_button", provider=next_prov),
                "callback_data": "retry",
            }]]}
            tg_with_keyboard(t("llm_retry_offer", provider=next_prov), keyboard)
        return t("analysis_unavailable")

    # Proactive: silent fallback chain through all available providers.
    for provider in providers:
        result = _llm_one(istem, model, provider)
        if isinstance(result, str):
            return result
        if result is _AUTH_FAIL:
            log.warning(f"LLM {provider}: auth fail (proactive — skipping)")
    return t("analysis_unavailable")

# ─── XBRL facts (F1) ─────────────────────────────────────

# Concepts used by format_facts_block for ordered display.
# Names match what edgartools Company Facts API returns.
_XBRL_DISPLAY_ORDER: list[str] = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "GrossProfit",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
    "CashAndCashEquivalentsAtCarryingValue",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
]

# Forms that carry XBRL data (trigger facts fetch).
_XBRL_FORMS: frozenset[str] = frozenset({"10-K", "10-Q", "20-F"})

# Grounding instruction injected into the prompt when XBRL facts are available.
_GROUNDING_INSTRUCTION = (
    "For every numeric financial claim (revenue, profit, margins, EPS, balance-sheet items) "
    "use ONLY the audited XBRL figures above. If a figure is not listed above, describe it "
    "qualitatively or quote the filing text verbatim — never estimate, extrapolate, or "
    "compute new numbers."
)


def _fmt_xbrl_value(concept: str, value: float, unit: str | None) -> str:
    """Format a single numeric XBRL value for human display."""
    abs_val = abs(value)
    sign = "-" if value < 0 else ""

    # Currency prefix: $ for USD/unspecified, else ISO code
    u_upper = (unit or "USD").upper()
    if "USD" in u_upper:
        prefix = "$"
    elif u_upper.startswith("ISO4217:"):
        prefix = u_upper.removeprefix("ISO4217:") + " "
    else:
        # Covers division units (USD/shares) and other codes
        prefix = "$"

    if concept == "EarningsPerShareDiluted":
        # EPS: raw value, no magnitude scaling
        return f"{sign}{prefix}{abs_val:.2f}"

    # Scale to B / M / K
    if abs_val >= 1e9:
        scaled = f"{abs_val / 1e9:.2f}B"
    elif abs_val >= 1e6:
        scaled = f"{abs_val / 1e6:.1f}M"
    elif abs_val >= 1e3:
        scaled = f"{abs_val / 1e3:.1f}K"
    else:
        scaled = f"{abs_val:.0f}"

    return f"{sign}{prefix}{scaled}"


def format_facts_block(facts: dict) -> str:
    """Format financial facts for LLM grounding.

    Accepts either:
      - New format: {"latest": {concept: (val, unit, date)}, "years": {concept: [(fy, val), ...]}}
      - Legacy format: {concept: (val, unit, date)} — for backward compat

    Returns ≤600-char text block. Empty string when facts is empty.
    """
    if not facts:
        return ""

    # Unwrap new format
    if "latest" in facts:
        latest = facts["latest"]
        years_data = facts.get("years", {})
    else:
        latest = facts
        years_data = {}

    # Header
    ends = [v[2] for v in latest.values() if v[2]]
    header_date = max(ends) if ends else "unknown"
    header = f"AUDITED XBRL FACTS (period ending {header_date}):"
    lines = [header]

    SHORT_NAMES = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": "Revenue",
    }

    for concept in _XBRL_DISPLAY_ORDER:
        if concept not in latest:
            continue
        val, unit, _end = latest[concept]
        if val is None:
            continue

        short = SHORT_NAMES.get(concept, concept)

        # Multi-year line: Revenue: FY2025 $416.2B · FY2024 $391.0B (+6.4%)
        if concept in years_data and len(years_data[concept]) >= 2:
            yr_parts = []
            for fy_label, yr_val in years_data[concept]:
                yr_parts.append(f"{fy_label} {_fmt_xbrl_value(concept, yr_val, unit)}")
            # YoY change
            vals = [v for _, v in years_data[concept]]
            if len(vals) >= 2 and vals[-2] != 0:
                yoy = (vals[-1] - vals[-2]) / abs(vals[-2]) * 100
                yr_parts.append(f"({yoy:+.1f}% YoY)")
            lines.append(f"  {short}: {' · '.join(yr_parts)}")
        else:
            lines.append(f"  {short}: {_fmt_xbrl_value(concept, val, unit)}")

    # Derived: gross margin
    rev_key = "RevenueFromContractWithCustomerExcludingAssessedTax"
    if rev_key in latest and "GrossProfit" in latest:
        rev = latest[rev_key][0]
        gp = latest["GrossProfit"][0]
        if rev is not None and gp is not None and rev != 0.0:
            margin = gp / rev * 100.0
            lines.append(f"  gross_margin_pct: {margin:.1f}%")

    block = "\n".join(lines)
    if len(block) > 600:
        block = block[:597] + "…"
    return block


def fetch_company_overview(ticker: str) -> str:
    """Fetch brief company overview via edgartools to_llm_context().

    Returns a short text block for LLM context, or "" on failure.
    """
    try:
        company = get_company(ticker)
        facts = company.get_facts()
        ctx = facts.to_llm_context()
        info = ctx.get("company", {})
        metrics = ctx.get("key_metrics", {})
        name = info.get("name", ticker)
        cik = info.get("cik", "")
        total_facts = info.get("total_facts", 0)
        lines = [f"COMPANY OVERVIEW: {name} (CIK: {cik})"]
        if metrics:
            for k, v in list(metrics.items())[:5]:
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    except Exception as e:
        log.debug("fetch_company_overview(%s): %s", ticker, e)
        return ""


def fetch_facts_context(ticker: str) -> str:
    """Fetch token-efficient financial context via edgartools to_context().

    Uses income_statement, balance_sheet, and cashflow_statement with
    'standard' detail level (~300 tokens each). Returns concatenated
    context blocks for LLM grounding. Falls back to fetch_company_facts()
    if to_context() is unavailable.
    """
    try:
        company = get_company(ticker)
        financials = company.get_financials()
        if financials is None:
            return ""

        parts = []
        for stmt_name in ["income_statement", "balance_sheet", "cashflow_statement"]:
            try:
                stmt = getattr(financials, stmt_name)()
                if stmt is not None and hasattr(stmt, "to_context"):
                    ctx = stmt.to_context("standard")
                    if ctx:
                        parts.append(ctx)
            except Exception as e:
                log.debug("fetch_facts_context %s %s: %s", ticker, stmt_name, e)

        return "\n\n".join(parts) if parts else ""

    except Exception as e:
        log.debug("fetch_facts_context(%s): %s", ticker, e)
        return ""


def fetch_notes_context(ticker: str, focus: str = "") -> str:
    """Fetch financial notes via edgartools to_context().

    Returns note summaries for LLM context. Use focus='Risk' for 10-K risk factors.
    """
    try:
        company = get_company(ticker)
        filing = company.get_filings(form="10-K").latest()
        tenk = filing.obj()
        notes = tenk.notes
        if notes is None:
            return ""
        if focus:
            return notes.to_context("minimal")[:800]
        return notes.to_context("minimal")[:500]
    except Exception as e:
        log.debug("fetch_notes_context(%s): %s", ticker, e)
        return ""


def fetch_company_facts(ticker: str) -> dict | None:
    """Fetch multi-year financial facts via edgartools Company Facts API.

    Returns a dict with two keys:
      - "latest": {concept: (value, unit, period_end)} — for verify_numeric_claims
      - "years": {concept: [(fy_label, value), ...]} — for multi-year display

    Returns None on any error/absence. Never raises.
    """
    try:
        company = get_company(ticker)
        num_years = 3

        latest: dict[str, tuple] = {}
        years: dict[str, list] = {}

        def _grab(df, concepts, unit="USD"):
            fy_cols = [c for c in df.columns if c.startswith("FY")]
            if not fy_cols:
                return
            latest_period = fy_cols[-1]
            for concept in concepts:
                if concept not in df.index:
                    continue
                # Latest value
                try:
                    val = float(df.loc[concept, latest_period])
                    latest[concept] = (val, unit, latest_period)
                except (KeyError, ValueError, TypeError):
                    pass
                # Multi-year values
                yr_vals = []
                for c in fy_cols:
                    try:
                        yr_vals.append((c, float(df.loc[concept, c])))
                    except (KeyError, ValueError, TypeError):
                        pass
                if yr_vals:
                    years[concept] = yr_vals

        # Income statement
        try:
            inc = company.income_statement(periods=num_years)
            inc_df = inc.to_dataframe()
            # Fallback aliases for revenue concept
            rev_concepts = [
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "Revenues", "SalesRevenueNet",
            ]
            rev_key = next((c for c in rev_concepts if c in inc_df.index), None)
            income_concepts = [rev_key, "GrossProfit", "OperatingIncomeLoss",
                               "NetIncomeLoss", "EarningsPerShareDiluted"]
            _grab(inc_df, [c for c in income_concepts if c])
        except Exception as e:
            log.debug("fetch_company_facts income_statement: %s", e)
            return None

        # Balance sheet
        try:
            bs = company.balance_sheet(periods=num_years)
            _grab(bs.to_dataframe(), [
                "CashAndCashEquivalentsAtCarryingValue",
                "Assets", "Liabilities", "StockholdersEquity",
            ])
        except Exception as e:
            log.debug("fetch_company_facts balance_sheet: %s", e)

        if not latest:
            return None

        return {"latest": latest, "years": years}

    except Exception as exc:
        log.debug("fetch_company_facts(%s): %s", ticker, exc)
        return None


# ─── EDGAR XBRL grounding (sole data source) ──────────────
# Fiscal AI and Twelve Data removed — only EDGAR XBRL is used.
# The _fiscal_memo_lock and _twelve_memo_lock are kept as no-ops
# for backward compatibility with existing code paths.

_fiscal_memo_lock = threading.Lock()  # no-op — kept for compat
_twelve_memo_lock = threading.Lock()  # no-op — kept for compat


def _fiscal_enabled() -> bool:
    """Always False — Fiscal AI removed. Kept for call-site compat."""
    return False


# ─── Numeric verification (F3) ────────────────────────────

_RE_NV_RANGE = re.compile(
    r'\$?[\d,]+(?:\.\d+)?\s*[-–]\s*[\d,]+(?:\.\d+)?\s*'
    r'(?:trillion|billion|million|[TBM])\b',
    re.IGNORECASE,
)
_RE_NV_MONEY = re.compile(
    r'(-?\$?(?:\d{1,3}(?:,\d{3})*|\d+)(?:\.\d+)?)'
    r'\s*(trillion|billion|million|[TBM])\b',
    re.IGNORECASE,
)
_RE_NV_PCT = re.compile(
    r'(?<!\d)(-?\d+(?:\.\d+)?)\s*(?:%|percent(?:age)?\b)',
    re.IGNORECASE,
)
_RE_NV_SRC_RAW = re.compile(r'\b(\d{1,3}(?:,\d{3}){2,}(?:\.\d+)?)\b')

_NV_SCALE: dict[str, float] = {
    't': 1e12, 'trillion': 1e12,
    'b': 1e9,  'billion':  1e9,
    'm': 1e6,  'million':  1e6,
}


def _nv_normalize_money(digits: str, scale_str: str) -> float:
    """Strip commas/$, apply billion/million/trillion multiplier."""
    val = float(digits.replace(',', '').replace('$', ''))
    return val * _NV_SCALE.get(scale_str.lower(), 1.0)


def _extract_numeric_claims(text: str) -> list[tuple[str, float, str]]:
    """
    Extract B/M-scale monetary and percentage claims from LLM analysis text.

    Returns [(raw_text, normalized_value, kind)] where kind ∈ {"money", "pct"}.
    Ranges ($390-395B), bare years (1900-2099), and scale-less bare numbers
    are skipped by design.
    """
    results: list[tuple[str, float, str]] = []

    range_spans = [(m.start(), m.end()) for m in _RE_NV_RANGE.finditer(text)]

    def _in_range(s: int, e: int) -> bool:
        return any(rs <= s and e <= re_ for rs, re_ in range_spans)

    for m in _RE_NV_MONEY.finditer(text):
        if _in_range(m.start(), m.end()):
            continue
        try:
            val = _nv_normalize_money(m.group(1), m.group(2))
        except (ValueError, TypeError):
            continue
        results.append((m.group(0).strip(), val, "money"))

    for m in _RE_NV_PCT.finditer(text):
        try:
            val = float(m.group(1))
        except (ValueError, TypeError):
            continue
        results.append((m.group(0).strip(), val, "pct"))

    return results


def _parse_facts_block(facts_block: str) -> list[tuple[float, str]]:
    """
    Parse format_facts_block() output → [(value, kind)].
    kind ∈ {"money", "pct"}.  B/M-scaled values are in full USD.
    Raw-dollar entries (EPS-scale) are kept as-is.
    """
    if not facts_block:
        return []
    results: list[tuple[float, str]] = []
    for line in facts_block.splitlines():
        if "AUDITED XBRL FACTS" in line:
            continue
        # Percentage
        m = re.search(r'(-?\d+(?:\.\d+)?)\s*%', line)
        if m:
            results.append((float(m.group(1)), "pct"))
            continue
        # B/M/T-scaled monetary
        m = re.search(r'(-?\d+(?:\.\d+)?)\s*([BMT])\b', line, re.IGNORECASE)
        if m:
            val = float(m.group(1)) * _NV_SCALE.get(m.group(2).lower(), 1.0)
            results.append((val, "money"))
            continue
        # Raw dollar (EPS-scale, no scale suffix)
        m = re.search(r'\$(-?\d+(?:\.\d+)?)\b', line)
        if m:
            results.append((float(m.group(1)), "money"))
    return results


def _source_numbers(source_text: str) -> set[float]:
    """
    Extract and normalize B/M-scale numbers from source filing text.
    Returns absolute float values in USD.
    """
    nums: set[float] = set()
    for m in _RE_NV_MONEY.finditer(source_text):
        try:
            nums.add(abs(_nv_normalize_money(m.group(1), m.group(2))))
        except (ValueError, TypeError):
            pass
    for m in _RE_NV_SRC_RAW.finditer(source_text):
        try:
            val = float(m.group(1).replace(',', ''))
            if val >= 1e7:
                nums.add(val)
        except ValueError:
            pass
    return nums


def verify_numeric_claims(
    analysis: str,
    facts_block: str,
    source_text: str,
) -> list[str]:
    """
    Return raw texts of B/M-scale monetary and percentage claims in analysis
    that cannot be verified against facts_block or source_text.

    Returns [] when facts_block is empty (non-grounded filings — no change
    to existing behaviour).  At most 5 items, in analysis order, deduped by
    raw text.
    """
    if not facts_block:
        return []

    claims = _extract_numeric_claims(analysis)
    if not claims:
        return []

    facts_vals = _parse_facts_block(facts_block)
    src_nums = _source_numbers(source_text)

    def _supported(val: float, kind: str) -> bool:
        av = abs(val)
        for fv, fkind in facts_vals:
            if fkind != kind:
                continue
            af = abs(fv)
            if kind == "money":
                if abs(av - af) / max(af, 1.0) <= 0.02:
                    return True
            else:  # pct
                if abs(av - af) <= 1.0:
                    return True
        if kind == "money":
            for sv in src_nums:
                if abs(av - sv) / max(sv, 1.0) <= 0.02:
                    return True
        return False

    unverified: list[str] = []
    seen: set[str] = set()
    for raw, val, kind in claims:
        if raw in seen:
            continue
        seen.add(raw)
        if not _supported(val, kind):
            unverified.append(raw)
        if len(unverified) >= 5:
            break

    return unverified


# ─── Markdown cleaning ────────────────────────────────────
_RE_HTML_TAG = re.compile(r'<[^>]+>')
_RE_MULTI_BLANK = re.compile(r'\n{3,}')

def _clean_markdown(text: str) -> str:
    """Strip HTML artifacts from edgartools markdown output.

    Removes <div>, <span>, etc. tags and collapses excessive blank lines.
    Markdown tables and headers are preserved.
    """
    text = _RE_HTML_TAG.sub('', text)
    text = _RE_MULTI_BLANK.sub('\n\n', text)
    return text.strip()


# ─── Section extraction ───────────────────────────────────
_SECTION_KEYWORDS = {
    "10-K": ["item 1.", "item 1a.", "item 7.", "item 8."],
    "10-Q": ["item 1.", "item 2.", "item 3."],
    # Full standard 8-K item set (SEC rules; ABS 6.0x series omitted — equity-focused bot).
    # Item 1.05 added: Material Cybersecurity Incidents (SEC rule, effective 2023).
    "8-K": [
        "item 1.01", "item 1.02", "item 1.03", "item 1.04", "item 1.05",
        "item 2.01", "item 2.02", "item 2.03", "item 2.04", "item 2.05", "item 2.06",
        "item 3.01", "item 3.02", "item 3.03",
        "item 4.01", "item 4.02",
        "item 5.01", "item 5.02", "item 5.03", "item 5.04", "item 5.05",
        "item 5.06", "item 5.07", "item 5.08",
        "item 7.01", "item 8.01", "item 9.01",
    ],
}

def extract_section(text: str, form: str, max_k: int) -> str:
    """Extract relevant sections from filing text (plain text or markdown).

    Supports both markdown headers (### Item 1.) and plain text (Item 1.).
    """
    kw = _SECTION_KEYWORDS.get(form, [])
    if not kw: return text[:max_k]
    lines, active, chars, output = text.split("\n"), False, 0, []
    for line in lines:
        stripped = line.lower().strip()
        # Match markdown headers (### Item 1.) and plain text (Item 1.)
        if any(stripped.lstrip("#").strip().startswith(k) for k in kw):
            active = True
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
    if not ai_enabled(): return ""   # NO_KEYS: skip diff entirely (J3)
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
                 custom_prompt: str = "", facts_block: str = "",
                 company_overview: str = "", notes_context: str = "") -> str:
    header = f"{ticker} — {form} ({date_str})\n\n"
    parts = []
    if company_overview:
        parts.append(company_overview)
    if facts_block:
        parts.append(facts_block + "\n" + _GROUNDING_INSTRUCTION)
    if notes_context:
        parts.append(f"FINANCIAL NOTES:\n{notes_context}")
    parts.append(body)
    content = "\n\n".join(parts)
    return header + content + "\n\n" + (custom_prompt or PROMPTS.get(form, _PROMPT_DEFAULT))

# ─── EDGAR Company cache ──────────────────────────────────
# edgartools' Company() does a CIK lookup (network) on construction.
# A Company object is just a CIK + metadata — filings are always fetched
# fresh via .get_filings(), so the object is safe to cache for the whole
# process lifetime. This turns N scans of the same ticker into 1 lookup.
_company_cache: dict = {}
_company_lock = threading.Lock()

def get_company(ticker: str):
    """Return a cached edgar Company for `ticker`, constructing it once.

    The network construction runs OUTSIDE the lock — a rare race just
    builds the object twice (both valid, last write wins). May raise on a
    bad ticker / network failure; the caller's retry loop handles that.
    Failures are never cached.
    """
    with _company_lock:
        cached = _company_cache.get(ticker)
    if cached is not None:
        return cached
    company = Company(ticker)            # network — may raise
    with _company_lock:
        _company_cache[ticker] = company
    return company

# ═══════════════════════════════════════════════════════════
# Refactored scan pipeline — small composable functions.
# Each step has a clear input/output contract for testability.
# ═══════════════════════════════════════════════════════════

def _collect_8k_text(filing) -> str:
    """Collect primary 8-K doc markdown + EX-99.* attachment bodies.

    Uses filing.markdown() for structured tables and headers.
    Falls back to primary-doc-only on AttributeError (old edgartools versions).
    Never raises — caller always gets a string (possibly empty).
    """
    try:
        primary = filing.markdown() or ""
    except Exception as e:
        log.warning(f"_collect_8k_text: filing.markdown() failed: {e}")
        return ""

    try:
        attachments = filing.attachments
    except AttributeError:
        log.warning("_collect_8k_text: filing.attachments not available — primary doc only")
        return _clean_markdown(primary)

    extra_parts: list = []
    for att in attachments:
        try:
            doc_name = att.document or ""
            if not doc_name.lower().startswith("ex-99"):
                continue
            att_text = att.markdown() or att.text()
            if not att_text:
                log.warning(f"_collect_8k_text: attachment {doc_name!r} returned empty text — skipping")
                continue
            extra_parts.append(f"\n\n--- {doc_name} ---\n{att_text}")
        except Exception as e:
            log.warning(f"_collect_8k_text: attachment fetch error ({e}) — skipping")

    return _clean_markdown(primary + "".join(extra_parts))


def fetch_new_filings(ticker: str, forms: list, lookback_days: int,
                      cache_dict: dict | None = None,
                      use_cache: bool = False,
                      quiet: bool = False,
                      *,
                      n_latest: int = 1,
                      max_chars_per: int | None = None,
                      fetch_text: bool = True) -> list:
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
            company = get_company(ticker)
            for form in forms:
                for form_deneme in range(3):
                    try:
                        result = company.get_filings(form=form).latest(n_latest)
                        if not result: break
                        if not isinstance(result, list):
                            result = [result]
                        for f in result:
                            d = f.filing_date
                            if hasattr(d, "date"): d = d.date()
                            ds = str(d)
                            if datetime.combine(d, datetime.min.time()) < cutoff:
                                continue
                            if (use_cache and cache_dict is not None
                                    and not is_new_in_cache(cache_dict, ticker, form, ds)):
                                continue
                            if fetch_text:
                                text = (_collect_8k_text(f) if form == "8-K"
                                        else _clean_markdown(f.markdown() or ""))
                                if not text:
                                    log.warning(f"{ticker} {form} {ds}: text returned empty — skipping filing")
                                    continue
                                if max_chars_per is not None:
                                    text = text[:max_chars_per]
                            else:
                                text = None
                            # XBRL grounding via edgartools to_context().
                            facts_block = ""
                            if fetch_text and form in _XBRL_FORMS:
                                facts_block = fetch_facts_context(ticker)
                            found.append((form, ds, text, facts_block))
                            time.sleep(0.5)
                        break
                    except Exception as e:
                        wait_sec = _backoff(form_deneme, 5, 30)
                        log.error(f"{ticker} {form} (attempt {form_deneme+1}): {e} — waiting {wait_sec}s")
                        time.sleep(wait_sec)
            return found
        except Exception as e:
            wait_sec = _backoff(edgar_deneme, 10, 60)
            log.error(f"{ticker} Company (attempt {edgar_deneme+1}): {e} — waiting {wait_sec}s")
            if edgar_deneme == 2:
                if not quiet:
                    tg(friendly_fetch_error(ticker, e))
                return []
            time.sleep(wait_sec)
    return []


# Form-sensitive char limits; unknown forms fall back to the global max_chars config.
# 8-K gets a higher cap because substantive items often appear well past the first 10000 chars.
_FORM_MAX_CHARS: dict = {
    "8-K": 20000,   # bumped from 15000: press releases alone run 8-12K chars
}

# /compare per-side budget. Two filings share one llm() prompt (clamp ~30000).
# 2 × 14000 = 28000 + ~500 scaffolding ≈ 28500 → safely under clamp, and wide enough
# to capture Item 1/1A and reach Item 7 (MD&A) for 10-K, Item 1/2 for 10-Q.
_COMPARE_PER_SIDE_MAX = 14000

def analyze_filing(ticker: str, form: str, date_str: str, text: str,
                   max_chars: int, model: str,
                   custom_prompts: dict,
                   facts_block: str = "") -> tuple[str | None, str | None]:
    """
    IO: call the LLM. Pure with respect to caller state.

    Returns: (analysis, diff). diff is "" for non-10-K/10-Q or no prior filing.
    facts_block is forwarded to build_prompt unchanged; empty string = no grounding.
    """
    effective_max = _FORM_MAX_CHARS.get(form, max_chars)
    body = extract_section(text, form, effective_max)
    custom  = custom_prompts.get(form, "")
    if not ai_enabled():
        # NO_KEYS: signal caller to deliver raw text; diff skipped (J3)
        return None, None
    overview = fetch_company_overview(ticker) if facts_block else ""
    notes = fetch_notes_context(ticker) if facts_block and form == "10-K" else ""
    analysis = llm(build_prompt(ticker, form, date_str, body, custom, facts_block, overview, notes), model)
    diff   = diff_analysis(ticker, form, text, model)
    return analysis, diff


def _filing_rich_md(ticker: str, form: str, date_str: str,
                    analysis: str, diff: str,
                    price_snippet: str = "",
                    unverified: list | None = None) -> str:
    """PURE: GFM-rich counterpart to render_filing_message for sendRichMessage.

    Uses ### header (GFM) instead of *bold* (legacy). Risk-factor diff is
    wrapped in <details> for collapsibility. Returns '' when content exceeds
    32768 chars (caller falls back to legacy chunking).
    """
    parts = [t("filing_rich_header", ticker=ticker, form=form, date=date_str)]
    parts.append("")
    parts.append(analysis)
    if diff:
        parts.append("")
        parts.append(t("filing_rich_risk_header"))
        parts.append("")
        summary = t("filing_rich_risk_summary")
        parts.append(f"<details>\n<summary>{summary}</summary>\n")
        parts.append(diff)
        parts.append("\n\n</details>")
    if unverified:
        parts.append("")
        parts.append(t("unverified_figures", items=", ".join(unverified)))
    if price_snippet:
        parts.append("")
        parts.append(price_snippet)
    parts.append("")
    parts.append("---")
    result = "\n".join(parts)
    if len(result) > 32768:
        return ""
    return result


def render_filing_message(ticker: str, form: str, date_str: str,
                          analysis: str, diff: str,
                          price_snippet: str = "",
                          unverified: list | None = None) -> str:
    """
    PURE: build the Telegram message body for a single filing analysis.
    Optional `price_snippet` (from E1 price action) is appended above the
    separator if non-empty.  Optional `unverified` list (from F3 numeric
    verification) is inserted before price_snippet when non-empty.
    No IO, no globals — easy to unit-test.
    """
    msg = f"{t('analysis_msg_header', ticker=ticker, form=form, date=date_str)}\n\n{analysis}"
    if diff:
        msg += f"\n\n{t('risk_factor_changes_header')}\n{diff}"
    if unverified:
        msg += f"\n\n{t('unverified_figures', items=', '.join(unverified))}"
    if price_snippet:
        msg += f"\n\n{price_snippet}"
    msg += f"\n\n{'─'*28}"
    return msg


def send_filing_result(ticker: str, form: str, date_str: str, text: str,
                       analysis: str, diff: str, save_to_cache: bool, quiet: bool,
                       unverified: list | None = None):
    """
    IO: persist artifacts and notify the user.
    Steps: store raw + analysis + previous, send message with inline
    buttons, weekly log, update cache, bump counter.

    The .md report is NOT pushed automatically — it is parked in the
    raw-filing store and delivered on demand via the message's '.md' button.
    Optional `unverified` list (F3) is forwarded to render_filing_message.
    """
    save_prev(ticker, form, text)
    raw_key = store_raw_filing(ticker, form, date_str, text, analysis, diff)

    if not quiet:
        # Optional E1 price action — empty string on disable/failure (silent).
        price_snippet = compute_price_snippet(ticker, date_str)
        message = render_filing_message(ticker, form, date_str,
                                        analysis, diff, price_snippet,
                                        unverified=unverified)
        rich_full = _filing_rich_md(ticker, form, date_str,
                                    analysis, diff, price_snippet,
                                    unverified=unverified)
        tg_with_button(message, raw_key, rich_md=rich_full or None)

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
    g_cfg    = get_cfg()
    cfg      = get_chat_cfg()
    cache_dict = load_cache()

    found = fetch_new_filings(
        ticker, forms, g_cfg["days_lookback"],
        cache_dict, use_cache, quiet,
    )

    if not found:
        if not quiet: tg(t("no_new_filings", ticker=ticker))
        return False

    if not quiet:
        tg(t("new_filings_found", ticker=ticker, count=len(found)))

    for form, date_str, text, facts_block in found:
        # DEF 14A: send proxy data directly, skip LLM analysis
        if form == "DEF 14A":
            proxy_data = analyze_def14a(ticker)
            if proxy_data:
                tg(proxy_data)
                if save_to_cache:
                    ob = load_cache()
                    mark_processed(ob, ticker, form, date_str)
                    save_cache(ob)
                time.sleep(5)
                continue
            # fallback to LLM if proxy extraction failed

        _maybe_post_thinking_draft(ticker, form, quiet)
        analysis, diff = analyze_filing(
            ticker, form, date_str, text,
            g_cfg["max_chars"], cfg["model"], cfg.get("custom_prompts", {}),
            facts_block=facts_block,
        )
        if analysis is None:
            # NO_KEYS: deliver raw text instead of LLM analysis (J3)
            _check_no_keys_reminder()
            body = extract_section(text, form, _FORM_MAX_CHARS.get(form, cfg["max_chars"]))
            _deliver_raw_text(body, ticker, form, date_str, "no_ai_no_keys")
            if save_to_cache:
                ob = load_cache()
                mark_processed(ob, ticker, form, date_str)
                save_cache(ob)
            time.sleep(5)
            continue
        unverified = verify_numeric_claims(analysis, facts_block, text)
        send_filing_result(ticker, form, date_str, text,
                           analysis, diff, save_to_cache, quiet,
                           unverified=unverified)
        time.sleep(5)

    status_set(last_scan=datetime.now().isoformat())
    return True

def probe_new_filings_for_watchlist(form_override: list | None = None) -> list:
    """
    Probe-only existence check for the hourly alarm.

    Returns a list of (ticker, form, date_str) for EVERY new (cache-filtered,
    lookback-windowed) filing across the watchlist. Empty list = nothing new.

    Side-effect-free: does NOT call the LLM, does NOT write to cache, does
    NOT touch weekly_log or previous_filings. The user analyzes on demand
    via the alert's inline buttons — keeping LLM quota under user control.

    Unlike the old short-circuit version (which returned a bool on the first
    hit), this probes the whole watchlist so the alert can name each new
    filing. That is a few extra EDGAR existence checks per hour — still no
    LLM call, still cheap.
    """
    cfg = get_cfg()
    items = cfg["tickers"]
    if not items:
        return []
    forms = form_override or cfg["default_forms"]
    cache_dict = load_cache()
    lookback = cfg["days_lookback"]
    hits: list = []
    for ticker in items:
        rows = fetch_new_filings(
            ticker, forms, lookback,
            cache_dict=cache_dict, use_cache=True,
            quiet=True,
            fetch_text=False,
        )
        for form, date_str, _text, _fb in rows:
            hits.append((ticker, form, date_str))
        time.sleep(1)
    return hits

# ─── Top-level scan commands ──────────────────────────────
def cmd_sec(form_override=None, quiet=False):
    cfg    = get_chat_cfg()
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
    cfg   = get_chat_cfg()
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
        forms = get_chat_cfg()["default_forms"]
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

def _compare_metrics_data(ticker_a: str, ticker_b: str) -> "dict | None":
    """IO: fetch Company Facts metrics for two tickers. Returns dict or None.

    Returns: {a: str, b: str, rows: [(key, va, vb)]} or None on failure.
    """
    try:
        def _get_metrics(tk):
            c = Company(tk)
            f = c.get_facts()
            return {
                "Revenue": f.get_revenue(),
                "Net Income": f.get_net_income(),
                "Total Assets": f.get_total_assets(),
            }
        m_a = _get_metrics(ticker_a)
        m_b = _get_metrics(ticker_b)
        rows = []
        for key in m_a:
            rows.append((key, m_a[key], m_b[key]))
        return {"a": ticker_a, "b": ticker_b, "rows": rows}
    except Exception as e:
        log.debug("compare metrics: %s", e)
        return None


def _compare_metrics_legacy(d: "dict | None") -> str:
    """PURE: legacy ASCII-fence metrics block. Byte-identical to old inline code."""
    if not d:
        return ""
    lines = [f"📊 *Financial Metrics Comparison*",
             f"```",
             f"{'Metric':<20s} {d['a']:>14s} {d['b']:>14s} {'Delta':>10s}"]
    for key, va, vb in d["rows"]:
        if va is not None and vb is not None and vb != 0:
            delta = (va - vb) / abs(vb) * 100
            lines.append(f"{key:<20s} ${va/1e9:>12.1f}B ${vb/1e9:>12.1f}B {delta:>+9.1f}%")
        elif va is not None:
            lines.append(f"{key:<20s} ${va/1e9:>12.1f}B {'n/a':>14s}")
    lines.append("```")
    return "\n".join(lines)


def _compare_metrics_rich(d: "dict | None") -> str:
    """PURE: GFM-native metrics table for sendRichMessage."""
    if not d:
        return ""
    lines = [t("compare_rich_metrics_header", a=d["a"], b=d["b"])]
    lines.append(f"| Metric | {d['a']} | {d['b']} | Delta |")
    lines.append("|:--|--:|--:|--:|")
    for key, va, vb in d["rows"]:
        if va is not None and vb is not None and vb != 0:
            delta = (va - vb) / abs(vb) * 100
            lines.append(f"| {key} | ${va/1e9:.1f}B | ${vb/1e9:.1f}B | {delta:+.1f}% |")
        elif va is not None:
            lines.append(f"| {key} | ${va/1e9:.1f}B | n/a | n/a |")
    return "\n".join(lines)


def _compare_rich_md(ticker_a: str, date_a: str, ticker_b: str, date_b: str,
                     form: str, summary: str, metrics_rich: str = "") -> str:
    """PURE: GFM-rich counterpart to cmd_compare output."""
    parts = [t("compare_rich_header",
               a=ticker_a, date_a=date_a, b=ticker_b, date_b=date_b, form=form)]
    parts.append("")
    parts.append(summary)
    if metrics_rich:
        parts.append("")
        parts.append(metrics_rich)
    result = "\n".join(parts)
    if len(result) > 32768:
        return ""
    return result


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

    cfg = get_chat_cfg()
    tg(t("compare_started", a=ticker_a, b=ticker_b, form=form))

    def _fetch_one(tk: str) -> tuple[str, str] | None:
        # NOTE (H7): Do NOT pass max_chars_per here. For 10-K/10-Q the body
        # often starts well past cfg["max_chars"] (cover page can be 8-12K
        # chars); pre-truncating before extract_section runs leaves no Item
        # anchors to find, so extract_section's fallback returns the cover
        # page again. Let the full text reach extract_section; section-aware
        # clamp happens at the LLM call site via _COMPARE_PER_SIDE_MAX.
        rows = fetch_new_filings(tk, [form],
                                 lookback_days=400,
                                 quiet=True,
                                 n_latest=1)
        if not rows: return None
        f, d, txt, _fb = rows[0]
        return d, txt

    res_a = _fetch_one(ticker_a)
    res_b = _fetch_one(ticker_b)
    if not res_a:
        tg(t("compare_missing", ticker=ticker_a, form=form)); return
    if not res_b:
        tg(t("compare_missing", ticker=ticker_b, form=form)); return

    date_a, text_a = res_a
    date_b, text_b = res_b
    body_a = extract_section(text_a, form, _COMPARE_PER_SIDE_MAX)
    body_b = extract_section(text_b, form, _COMPARE_PER_SIDE_MAX)

    d = _compare_metrics_data(ticker_a, ticker_b)
    metrics_block = _compare_metrics_legacy(d)

    summary = llm(
        build_compare_prompt(ticker_a, ticker_b, form, body_a, body_b),
        cfg["model"],
    )
    if summary is None:
        tg(t("no_ai_no_keys"))
        _deliver_raw_text(body_a, ticker_a, form, date_a, "no_ai_no_keys")
        _deliver_raw_text(body_b, ticker_b, form, date_b, "no_ai_no_keys")
        return
    result = t("compare_header",
               a=ticker_a, date_a=date_a,
               b=ticker_b, date_b=date_b,
               form=form,
               sep="─" * 28,
               summary=summary)
    if metrics_block:
        result += f"\n\n{metrics_block}"
    rich = _compare_rich_md(ticker_a, date_a, ticker_b, date_b, form, summary,
                            metrics_rich=_compare_metrics_rich(d))
    tg(result, rich_md=rich or None)

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

def _digest_pnl_data() -> "dict | None":
    """IO: fetch portfolio data for digest P&L. Returns dict or None if unavailable.

    Returns: {total_val, total_pnl, total_cost, t_emoji, t_pct,
              movers: (best, worst)|None, delta_parts: list[str]}
    """
    if not YF_OK:
        return None
    cfg = get_chat_cfg()
    lots = cfg.get("portfolio", [])
    if not lots:
        return None
    agg = aggregate_positions(lots)
    prices: dict[str, "float | None"] = {}
    for ticker in agg:
        prices[ticker] = fetch_last_close(ticker)
        time.sleep(0.5)
    rows = compute_pnl_rows(agg, prices)
    priced = [r for r in rows if r["last"] is not None]
    if not priced:
        return None
    total_val = sum(r["value"] for r in priced)
    total_pnl = sum(r["pnl_usd"] for r in priced)
    total_cost = sum(r["qty"] * r["avg_cost"] for r in priced)
    t_emoji = "📈" if total_pnl >= 0 else "📉"
    t_pct = _fmt_pct(total_pnl / total_cost * 100.0 if total_cost != 0 else None)
    mv = _digest_top_movers(priced)
    h = load_portfolio_history()
    from datetime import date as _date
    today_str = _date.today().isoformat()
    h_filtered = {k: v for k, v in h.items() if k != today_str}
    delta_parts = []
    if h_filtered:
        ytd_days = (_date.today() - _date(_date.today().year, 1, 1)).days
        intervals = [("1W", 7), ("6M", 182), ("YTD", ytd_days), ("1Y", 365)]
        for label, days in intervals:
            d = _compute_delta(h_filtered, total_val, days)
            delta_parts.append(f"{label}: {_format_delta(d[0] if d else None, d[1] if d else None)}")
    return {
        "total_val": total_val, "total_pnl": total_pnl,
        "total_cost": total_cost, "t_emoji": t_emoji, "t_pct": t_pct,
        "movers": mv, "delta_parts": delta_parts,
    }


def _digest_pnl_fmt_legacy(d: dict) -> str:
    """PURE: legacy-format P&L block from _digest_pnl_data dict."""
    lines = [t("digest_pnl_line",
               emoji=d["t_emoji"], value=f"${d['total_val']:,.0f}",
               pnl_usd=f"{d['total_pnl']:+,.0f}", pnl_pct=d["t_pct"])]
    if d["movers"]:
        best, worst = d["movers"]
        lines.append(t("digest_movers_line",
            up=best["ticker"], up_pct=_fmt_pct(best["pnl_pct"]),
            down=worst["ticker"], down_pct=_fmt_pct(worst["pnl_pct"])))
    if d["delta_parts"]:
        lines.append("_" + "  ·  ".join(d["delta_parts"]) + "_")
    return "\n".join(lines)


def _digest_pnl_rich(d: dict) -> str:
    """PURE: GFM-rich P&L block from _digest_pnl_data dict."""
    lines = [t("digest_rich_pnl_line",
               emoji=d["t_emoji"], value=f"${d['total_val']:,.0f}",
               pnl_usd=f"{d['total_pnl']:+,.0f}", pnl_pct=d["t_pct"])]
    if d["movers"]:
        best, worst = d["movers"]
        lines.append(t("digest_rich_movers_line",
            up=best["ticker"], up_pct=_fmt_pct(best["pnl_pct"]),
            down=worst["ticker"], down_pct=_fmt_pct(worst["pnl_pct"])))
    if d["delta_parts"]:
        lines.append("*" + "  ·  ".join(d["delta_parts"]) + "*")
    return "\n".join(lines)


def _digest_pnl_summary() -> str:
    """Compact P&L block for the weekly digest. Empty if no portfolio or no yfinance."""
    d = _digest_pnl_data()
    if not d:
        return ""
    return _digest_pnl_fmt_legacy(d)


def _digest_rich_md(data: list, week_start: str, today_str: str,
                    pnl_rich: str = "") -> str:
    """PURE: GFM-rich counterpart to send_weekly_digest body."""
    lines = [t("digest_rich_title", start=week_start, end=today_str,
               count=len(data))]
    if data:
        by_ticker: dict = {}
        for entry in data:
            by_ticker.setdefault(entry["ticker"], []).append(entry)
        for ticker, entries in by_ticker.items():
            lines.append(t("digest_rich_ticker", ticker=ticker))
            for k in entries:
                snippet = _md_escape(k["analiz"][:120].replace("\n", " "))
                lines.append(t("digest_rich_item",
                               form=k["form"], tarih=k["tarih"], snippet=snippet))
    else:
        lines.append(t("digest_no_filings"))
    if pnl_rich:
        lines.append("")
        lines.append(pnl_rich)
    return "\n".join(lines)


def send_weekly_digest():
    data = get_weekly_log()

    week_start = (datetime.now() - timedelta(days=7)).strftime("%d.%m.%Y")
    today_str      = datetime.now().strftime("%d.%m.%Y")

    lines = [t("digest_title_block",
                  start=week_start, end=today_str,
                  count=len(data), sep="─" * 28)]

    if data:
        by_ticker: dict = {}
        for entry in data:
            by_ticker.setdefault(entry["ticker"], []).append(entry)

        for ticker, entries in by_ticker.items():
            lines.append(f"\n🏢 *{ticker}*")
            for k in entries:
                snippet = _md_escape(k["analiz"][:120].replace("\n", " "))
                lines.append(f"  • {k['form']} ({k['tarih']}): {snippet}...")
    else:
        lines.append(t("digest_no_filings"))

    d = _digest_pnl_data()
    pnl_legacy = _digest_pnl_fmt_legacy(d) if d else ""
    if pnl_legacy:
        lines.append(f"\n{pnl_legacy}")

    rich = _digest_rich_md(data, week_start, today_str,
                           pnl_rich=_digest_pnl_rich(d) if d else "")
    tg("\n".join(lines), rich_md=rich or None)
    if data:
        clear_weekly_log()
    log.info("Weekly digest sent%s.", ", log cleared" if data else "")

def cmd_digest(parts: list) -> str:
    if len(parts) >= 2 and parts[1].lower() == "off":
        mutate_chat_cfg(lambda c: c.update({"weekly_digest": False}))
        return t("digest_disabled")
    if len(parts) >= 2 and parts[1].lower() == "now":
        send_weekly_digest()
        return t("digest_sent")
    mutate_chat_cfg(lambda c: c.update({"weekly_digest": True}))
    return t("digest_enabled")

# ─── Custom prompts ───────────────────────────────────────
# Dash variants found in practice: en-dash (–), em-dash (—), unicode minus (−)
_DASH_CHARS = "–—−－"  # –  —  −  －

def _normalize_form_input(raw: str) -> str:
    """Fold dash variants → ASCII hyphen, strip whitespace, uppercase."""
    for ch in _DASH_CHARS:
        raw = raw.replace(ch, "-")
    return raw.strip().upper()

def _match_form(raw: str) -> str | None:
    """Match a user-supplied string to a canonical FORMS entry.

    Matching tiers (first hit wins):
      1. Exact (after normalize): "10-K" → "10-K"
      2. Space-insensitive:       "SC13G" == "SC13G"
      3. Separator-stripped:      "10k" → "10K" == "10K" (stripped 10-K)
    """
    n = _normalize_form_input(raw)
    for k in FORMS:
        ku = k.upper()
        if ku == n:
            return k
        if ku.replace(" ", "") == n.replace(" ", ""):
            return k
    # tier 3: strip ALL separators (dashes + spaces) from both sides
    n_bare = n.replace("-", "").replace(" ", "")
    for k in FORMS:
        k_bare = k.upper().replace("-", "").replace(" ", "")
        if k_bare == n_bare:
            return k
    return None

def cmd_setprompt(parts: list) -> str:
    if len(parts) < 3:
        return t("setprompt_usage")
    m = _match_form(parts[1].upper())
    if not m: return t("unknown_form_named", form=parts[1])
    prompt = " ".join(parts[2:])
    mutate_chat_cfg(lambda c: c["custom_prompts"].update({m: prompt}))
    return t("prompt_saved", form=m, prompt=prompt)

def cmd_getprompt(parts: list) -> str:
    if len(parts) < 2: return t("getprompt_usage")
    m = _match_form(parts[1].upper())
    if not m: return t("unknown_form_named", form=parts[1])
    prompt = get_chat_cfg()["custom_prompts"].get(m)
    if not prompt:
        return t("no_custom_prompt", form=m)
    return t("custom_prompt_show", form=m, prompt=prompt)

def cmd_resetprompt(parts: list) -> str:
    if len(parts) < 2: return t("resetprompt_usage")
    m = _match_form(parts[1].upper())
    if not m: return t("unknown_form_named", form=parts[1])
    mutate_chat_cfg(lambda c: c["custom_prompts"].pop(m, None))
    return t("prompt_reset", form=m)

def cmd_listprompts() -> str:
    custom = get_chat_cfg()["custom_prompts"]
    if not custom:
        return t("listprompts_empty")
    lines = [t("listprompts_title")]
    for form, prompt in custom.items():
        lines.append(f"*{form}:* {prompt[:80]}{'...' if len(prompt)>80 else ''}")
    return "\n".join(lines)

# ─── Portfolio insider sentiment ──────────────────────────
def cmd_sentiment():
    cfg   = get_chat_cfg()
    items = cfg["tickers"]
    if not items: tg(t("watchlist_empty")); return

    tg(t("sentiment_started"))

    ticker_data = []
    for ticker in items:
        try:
            company = Company(ticker)
            filings = company.get_filings(form="4").head(5)
            summaries = []
            for f in filings:
                try:
                    obj = f.obj()
                    if hasattr(obj, 'get_ownership_summary'):
                        s = obj.get_ownership_summary()
                        # Header: name, position, activity, net change
                        activity = getattr(s, 'primary_activity', '')
                        net_chg = getattr(s, 'net_change', 0)
                        net_val = getattr(s, 'net_value', 0)
                        header = f"{s.insider_name} ({s.position}) — {activity}"
                        if net_chg:
                            sign = "+" if net_chg > 0 else ""
                            header += f" — {sign}{net_chg:,} shares (${net_val:,.0f})"
                        # Transaction details
                        txns = []
                        for tx in (s.transactions or []):
                            code = getattr(tx, 'code', '?')
                            shares = getattr(tx, 'shares', 0)
                            price = getattr(tx, 'price_per_share', 0)
                            ttype = getattr(tx, 'transaction_type', '?')
                            txns.append(
                                f"  {code} — {shares:,} shares @ ${price:.2f} ({ttype})"
                            )
                        summaries.append(header + "\n" + "\n".join(txns) if txns else header)
                except Exception as e:
                    log.debug(f"sentiment {ticker} filing: {e}")
            if summaries:
                ticker_data.append((ticker, "\n---\n".join(summaries)))
        except Exception as e:
            log.debug(f"sentiment {ticker}: {e}")

    if not ticker_data:
        tg(t("sentiment_no_transactions")); return

    signals = []
    today_iso = datetime.now().strftime("%Y-%m-%d")
    _no_keys = not ai_enabled()   # snapshot once for this run (J3)
    for ticker, texts in ticker_data:
        if _no_keys:
            # Source text exists — but /sentiment is synthesis; use n/a label (J3)
            signal = t("no_ai_signal_placeholder")
        else:
            signal = llm(
                f"{ticker} — Last 30 days Form 4 transactions:\n{texts[:6000]}\n\n"
                "Summarize in a single line:\n"
                "Format: EMOJI SENTIMENT (Bullish/Bearish/Neutral) — 1-sentence reason\n"
                "Emoji: 📈 Bullish, 📉 Bearish, ➡️ Neutral",
                cfg["model"]
            )
            if signal is None:
                signal = t("no_ai_signal_placeholder")
        signals.append(f"*{ticker}*: {signal}")
        # Persist for /sentiment trend comparisons.
        try:
            append_sentiment(ticker, signal, on_date=today_iso)
        except Exception as e:
            log.error(f"append_sentiment {ticker}: {e}")
        time.sleep(3)

    if _no_keys:
        # Synthesis: no source text — skip portfolio summary (J3)
        portfolio = t("no_ai_signal_placeholder")
    else:
        portfolio = llm(
            "Based on the following insider signals, give a portfolio-wide assessment:\n\n"
            + "\n".join(signals)
            + "\n\nWhat is the insider sentiment across the portfolio? "
            "Are there any standout warnings or opportunities?",
            cfg["model"]
        )
        if portfolio is None:
            portfolio = t("no_ai_signal_placeholder")

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
    added, already_in, invalid = [], [], []
    def _add(c):
        for raw in parts[1:]:
            ticker = raw.upper().strip()
            if not valid_ticker(ticker): invalid.append(ticker); continue
            if ticker in c["tickers"]: already_in.append(ticker)
            else: c["tickers"].append(ticker); added.append(ticker)
    mutate_chat_cfg(_add)
    lines = []
    if added:
        # Validate against EDGAR and show company names
        validated = []
        for tk in added:
            ok, name = validate_ticker_edgar(tk)
            if ok:
                validated.append(f"`{tk}` ({name})")
            else:
                validated.append(f"`{tk}` ⚠️")
                invalid.append(tk)
        # Remove invalid from config
        if invalid:
            mutate_chat_cfg(lambda c: [c["tickers"].remove(t) for t in invalid if t in c["tickers"]])
            added = [t for t in added if t not in invalid]
        if validated:
            lines.append(t("ticker_added", tickers="  ".join(validated)))
    if already_in: lines.append(t("ticker_already", tickers="  ".join(f"`{x}`" for x in already_in)))
    if invalid:    lines.append(t("ticker_invalid",  tickers="  ".join(f"`{x}`" for x in invalid)))
    return "\n".join(lines)


def cmd_search(parts: list) -> str:
    """Usage: /search <company name> — search SEC database by company name."""
    if len(parts) < 2:
        return t("search_usage")
    query = " ".join(parts[1:])
    try:
        results = find(query)
        if not results:
            return t("search_no_results", query=query)
        lines = [t("search_results", query=query, count=len(results))]
        count = 0
        for company in results:
            if count >= 10:
                break
            if company is None:
                continue
            tickers = getattr(company, "tickers", [])
            ticker = tickers[0] if tickers else "?"
            name = getattr(company, "name", "?")
            cik = getattr(company, "cik", "?")
            lines.append(f"  `{ticker}` — {name} (CIK: {cik})")
            count += 1
        if len(results) > 10:
            lines.append(t("search_more", count=len(results) - 10))
        return "\n".join(lines)
    except Exception as e:
        log.debug("search(%s): %s", query, e)
        return t("search_error", query=query)


def cmd_company(parts: list) -> str:
    """Usage: /company <TICKER> — show company info from EDGAR."""
    if len(parts) < 2:
        return t("company_usage")
    ticker = parts[1].upper().strip()
    try:
        company = Company(ticker)
        tickers = getattr(company, "tickers", [ticker])
        info = [
            f"🏢 *{getattr(company, 'name', ticker)}*",
            f"CIK: `{getattr(company, 'cik', 'N/A')}`",
            f"Ticker: `{', '.join(tickers) if tickers else 'N/A'}`",
            f"Industry: {getattr(company, 'industry', 'N/A')}",
            f"SIC: {getattr(company, 'sic', 'N/A')}",
        ]
        shares = getattr(company, "shares_outstanding", None)
        pfloat = getattr(company, "public_float", None)
        if shares:
            info.append(f"Shares Outstanding: {shares:,.0f}")
        if pfloat:
            info.append(f"Public Float: ${pfloat:,.0f}")
        return "\n".join(info)
    except Exception as e:
        log.debug("company(%s): %s", ticker, e)
        return t("company_not_found", ticker=ticker)


def cmd_removeticker(parts: list) -> str:
    if len(parts) < 2: return t("removeticker_usage")
    removed, not_found = [], []
    def _remove(c):
        for raw in parts[1:]:
            ticker = raw.upper().strip()
            if ticker in c["tickers"]: c["tickers"].remove(ticker); removed.append(ticker)
            else: not_found.append(ticker)
    mutate_chat_cfg(_remove)
    lines = []
    if removed:  lines.append(t("ticker_removed",       tickers="  ".join(f"`{x}`" for x in removed)))
    if not_found: lines.append(t("ticker_not_found_list", tickers="  ".join(f"`{x}`" for x in not_found)))
    return "\n".join(lines)

def cmd_listtickers() -> str:
    cfg = get_chat_cfg(); lst = cfg["tickers"]
    if not lst: return t("listtickers_empty")
    return t("listtickers_title",
             count=len(lst),
             lines="\n".join(f"  • `{x}`" for x in lst))

# ─── Ticker validation (R4) ───────────────────────────────
_TICKER_RE = re.compile(r"^[A-Z]{1,6}(?:[.\-][A-Z0-9]{1,3})?$")

def valid_ticker(symbol: str) -> bool:
    """PURE: True iff symbol looks like a valid US equity ticker format."""
    return bool(_TICKER_RE.match(symbol))

def validate_ticker_edgar(ticker: str) -> tuple[bool, str]:
    """Validate ticker against EDGAR database. Returns (ok, company_name).

    Uses edgartools' bundled ticker data (offline) as first check.
    Falls back to Company() construction (network) for edge cases.
    """
    if not valid_ticker(ticker):
        return False, ""
    try:
        company = Company(ticker)
        return True, getattr(company, "name", ticker)
    except Exception:
        return False, ""

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
    raw_tickers = [p.upper().strip() for p in parts[2:]]
    valid   = [x for x in raw_tickers if valid_ticker(x)]
    invalid = [x for x in raw_tickers if not valid_ticker(x)]
    if not valid:
        return t("group_no_valid_tickers", name=name)
    def _add(c):
        c["groups"][name] = sorted(set(valid))
    mutate_chat_cfg(_add)
    lines = [t("group_added", name=name,
               tickers="  ".join(f"`{x}`" for x in sorted(set(valid))))]
    if invalid:
        lines.append(t("ticker_invalid", tickers="  ".join(f"`{x}`" for x in invalid)))
    return "\n".join(lines)

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
    mutate_chat_cfg(_rm)
    if missing["flag"]:
        return t("group_not_found", name=name)
    return t("group_removed", name=name)

def cmd_listgroups() -> str:
    """Show all defined groups."""
    groups = get_chat_cfg().get("groups", {})
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
    cfg = get_chat_cfg()
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
    cfg   = get_chat_cfg(); active = cfg["default_forms"]
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
    cfg = get_chat_cfg()
    ticker_list = ""
    if cfg["tickers"]:
        ticker_list = " — `" + "  ".join(cfg["tickers"]) + "`"
    # Active LLM provider display
    default_prov = cfg.get("default_provider", "")
    api_keys = cfg.get("api_keys", {})
    if not default_prov:
        active_provider = t("settings_provider_none")
    elif not api_keys.get(default_prov):
        active_provider = t("settings_provider_no_key", provider=default_prov)
    else:
        active_provider = default_prov
    # Registered LLM provider names (names only, no keys/masks)
    llm_registered = [p for p in _PROVIDERS if api_keys.get(p)]
    registered_providers = ", ".join(llm_registered) if llm_registered else t("label_off")
    block = t("settings_block",
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
             prompt_count=len(cfg.get('custom_prompts', {})),
             active_provider=active_provider,
             registered_providers=registered_providers)
    rich_line = t("rich_settings_line",
                  rich=t("label_on") if cfg.get('rich_format', True) else t("label_off"))
    return block + "\n" + rich_line

def _status_data() -> dict:
    """IO: build status dict from snapshot + config. Single-read, no formatting."""
    snap = status_snapshot()
    now = datetime.now()
    baslangi = datetime.fromisoformat(snap["started"])
    sure = now - baslangi
    saat = int(sure.total_seconds() // 3600)
    dakika = int((sure.total_seconds() % 3600) // 60)

    def time_format(iso: str | None) -> str:
        if not iso: return t("label_dash")
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y %H:%M")

    cfg = get_chat_cfg()
    tg_hata = snap["tg_errors"]
    or_hata = snap["or_errors"]
    return {
        "uptime": t("uptime_format", hours=saat, minutes=dakika),
        "last_update": time_format(snap["last_update"]),
        "last_scan": time_format(snap["last_scan"]),
        "last_alarm": time_format(snap["last_alarm"]),
        "total_analyzed": snap["total_analyzed"],
        "tg_errors": "✅ 0" if tg_hata == 0 else f"⚠️ {tg_hata}",
        "or_errors": "✅ 0" if or_hata == 0 else f"⚠️ {or_hata}",
        "language": get_lang(),
        "schedule": cfg.get("schedule") or t("label_off"),
        "alarm": t("label_on") if cfg.get("alarm_on") else t("label_off"),
        "digest": t("label_on") if cfg.get("weekly_digest") else t("label_off"),
        "ticker_count": len(cfg["tickers"]),
    }


def _status_legacy(d: dict) -> str:
    """PURE: legacy status block. Byte-identical to old cmd_status output."""
    return t("status_block", **d)


def _status_rich(d: dict) -> str:
    """PURE: GFM-native status panel table for sendRichMessage."""
    return t("status_rich", **d)


def cmd_status() -> None:
    d = _status_data()
    body = _status_legacy(d)
    rich = _status_rich(d)
    tg(body, rich_md=rich or None)

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
    mutate_chat_cfg(lambda c: c.update({"model": " ".join(parts[1:])}))
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
        cur = "on" if get_chat_cfg().get("price_action_enabled", True) else "off"
        return t("priceaction_usage", current=cur)
    val = parts[1].lower()
    if val == "off":
        mutate_chat_cfg(lambda c: c.update({"price_action_enabled": False}))
        return t("priceaction_disabled")
    if val == "on":
        mutate_chat_cfg(lambda c: c.update({"price_action_enabled": True}))
        return t("priceaction_enabled")
    return t("priceaction_usage", current="on" if get_chat_cfg().get("price_action_enabled", True) else "off")

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
        mutate_chat_cfg(lambda c: c.update({"schedule": None}))
        return t("schedule_disabled")
    sh, sd = _parse_hhmm(value)
    if sh < 0 or not (0 <= sh <= 23 and 0 <= sd <= 59):
        return t("schedule_invalid")
    mutate_chat_cfg(lambda c: c.update({"schedule": value}))
    return t("schedule_set", time=value)

def cmd_alarm(parts: list) -> str:
    if len(parts) >= 2 and parts[1].lower() == "off":
        mutate_chat_cfg(lambda c: c.update({"alarm_on": False}))
        return t("alarm_disabled")
    mutate_chat_cfg(lambda c: c.update({"alarm_on": True}))
    return t("alarm_enabled")

# ─── Watchwords / EDGAR full-text search (G1) ────────────
# Probe-only: no LLM calls, no cache writes.  Pure helpers are IO-free and
# offline-testable; fetch_fts_hits is the only thin-IO wrapper.

_EFTS_URL            = "https://efts.sec.gov/LATEST/search-index"
_EFTS_DISPLAY_TICKER = re.compile(
    r'\((?!CIK\s)([A-Z]{1,7}(?:[.\-][A-Z0-9]{1,3})?)\)'
)
_WATCHWORD_MAX       = 10   # hard cap on number of phrases


def _build_fts_query(phrase: str, from_date: str) -> dict:
    """PURE: return params dict for EFTS search-index endpoint.

    URL: https://efts.sec.gov/LATEST/search-index
    Required params: q (quoted phrase), dateRange=custom, startdt (YYYY-MM-DD).
    """
    return {"q": f'"{phrase}"', "dateRange": "custom", "startdt": from_date}


def _parse_fts_hits(payload: dict) -> list[dict]:
    """PURE: extract one record per unique filing (adsh) from EFTS JSON.

    Each record: {"ticker_or_cik", "form", "date", "accession", "url"}.
    Multiple hits for the same adsh (different exhibit files) are de-duped.
    Ticker parsed from display_names; falls back to stripped CIK.
    Filing URL: https://www.sec.gov/Archives/edgar/data/{cik}/{adsh_nodash}/
    """
    hits: list[dict] = []
    seen_adsh: set[str] = set()
    for h in (payload.get("hits") or {}).get("hits", []):
        src = h.get("_source", {})
        adsh = src.get("adsh", "")
        if not adsh or adsh in seen_adsh:
            continue
        seen_adsh.add(adsh)

        ciks = src.get("ciks") or []
        cik_raw = ciks[0] if ciks else ""
        cik_stripped = cik_raw.lstrip("0") or cik_raw

        display_names = src.get("display_names") or []
        display = display_names[0] if display_names else ""
        m = _EFTS_DISPLAY_TICKER.search(display)
        ticker_or_cik = m.group(1) if m else (cik_stripped or adsh[:10])

        adsh_nodash = adsh.replace("-", "")
        url = (
            f"https://www.sec.gov/Archives/edgar/data"
            f"/{cik_stripped}/{adsh_nodash}/"
        )
        hits.append({
            "ticker_or_cik": ticker_or_cik,
            "form":           src.get("form", ""),
            "date":           src.get("file_date", ""),
            "accession":      adsh,
            "url":            url,
        })
    return hits


def _watchword_analyzable_hits(hits: list) -> list:
    """PURE: map watchword hits to alarm-compatible (ticker, form, date) tuples.

    Only includes hits where ticker_or_cik passes valid_ticker().
    Capped at 5 to match the display cap in format_watchword_alert.
    """
    return [(h["ticker_or_cik"], h["form"], h["date"])
            for h in hits if valid_ticker(h["ticker_or_cik"])][:5]


def format_watchword_alert(word: str, hits: list) -> str:
    """PURE: Markdown alert for watchword matches (max 5 hits + overflow line)."""
    cap     = 5
    shown   = hits[:cap]
    extra   = len(hits) - cap if len(hits) > cap else 0
    lines   = [t("watchword_alert_header", word=_md_escape(word), count=len(hits))]
    for h in shown:
        lines.append(t(
            "watchword_alert_hit",
            ticker=_md_escape(h["ticker_or_cik"]),
            form=_md_escape(h["form"]),
            date=h["date"],
            url=h["url"],
        ))
    if extra:
        lines.append(t("watchword_alert_more", n=extra))
    return "\n".join(lines)


def format_watchword_alert_rich(word: str, hits: list) -> str:
    """PURE: GFM-rich counterpart to format_watchword_alert for sendRichMessage.

    Uses ### header (GFM) instead of *bold* (legacy). No _md_escape — raw
    values are GFM-safe (word in backtick, ticker/form are controlled tokens).
    """
    if not hits:
        return ""
    cap = 5
    shown = hits[:cap]
    extra = len(hits) - cap if len(hits) > cap else 0
    lines = [t("watchword_alert_rich_header", word=word, count=len(hits))]
    for h in shown:
        lines.append(t(
            "watchword_alert_rich_hit",
            ticker=h["ticker_or_cik"],
            form=h["form"],
            date=h["date"],
            url=h["url"],
        ))
    if extra:
        lines.append(t("watchword_alert_rich_more", n=extra))
    return "\n".join(lines)


def fetch_fts_hits(phrase: str, from_date: str) -> list | None:
    """THIN IO: call EFTS; single attempt; any exception → None + log.debug.

    Uses EDGAR_IDENTITY User-Agent per SEC policy.
    retry() is intentionally NOT used — the alarm path must be silent.
    """
    try:
        params = _build_fts_query(phrase, from_date)
        resp = requests.get(
            _EFTS_URL,
            params=params,
            headers={"User-Agent": EDGAR_IDENTITY},
            timeout=15,
        )
        resp.raise_for_status()
        return _parse_fts_hits(resp.json())
    except Exception as e:
        log.debug(f"fetch_fts_hits({phrase!r}): {e}")
        return None


def _update_watchword_seen(
    state: dict, phrase: str, new_accessions: list[str]
) -> list[str]:
    """PURE: return accessions not yet seen; update state in-place (FIFO 200)."""
    seen      = state.get(phrase, [])
    seen_set  = set(seen)
    fresh     = [a for a in new_accessions if a not in seen_set]
    updated   = seen + fresh
    state[phrase] = updated[-200:]
    return fresh


def cmd_addword(parts: list) -> str:
    if len(parts) < 2:
        return t("addword_usage")
    phrase = " ".join(parts[1:]).strip()
    outcome: list[str] = []

    def _add(c: dict) -> None:
        words = c.setdefault("watchwords", [])
        if phrase in words:
            outcome.append("dup")
        elif len(words) >= _WATCHWORD_MAX:
            outcome.append("limit")
        else:
            words.append(phrase)
            outcome.append("ok")

    mutate_chat_cfg(_add)
    status = outcome[0] if outcome else "ok"
    if status == "dup":
        return t("addword_duplicate", word=_md_escape(phrase))
    if status == "limit":
        return t("addword_limit", max=_WATCHWORD_MAX)
    return t("addword_added", word=_md_escape(phrase))


def cmd_removeword(parts: list) -> str:
    if len(parts) < 2:
        return t("removeword_usage")
    phrase = " ".join(parts[1:]).strip()
    outcome: list[str] = []

    def _remove(c: dict) -> None:
        words = c.get("watchwords", [])
        if phrase in words:
            words.remove(phrase)
            outcome.append("ok")
        else:
            outcome.append("not_found")

    mutate_chat_cfg(_remove)
    status = outcome[0] if outcome else "not_found"
    if status == "not_found":
        return t("removeword_not_found", word=_md_escape(phrase))
    return t("removeword_removed", word=_md_escape(phrase))


def cmd_listwords() -> str:
    words = get_chat_cfg().get("watchwords", [])
    if not words:
        return t("listwords_empty")
    return t(
        "listwords_header",
        count=len(words),
        lines="\n".join(f"  • `{_md_escape(w)}`" for w in words),
    )


# ─── Multi-chat admin commands (I1) ───────────────────────

def cmd_addchat(parts: list, caller_id: str) -> str:
    """Add a new authorized chat ID. Admin only. Usage: /addchat <id>"""
    if len(parts) < 2:
        return t("addchat_format_error")
    try:
        new_id = str(int(parts[1]))          # validate: must be an integer
    except ValueError:
        return t("addchat_format_error")
    ids = [str(c) for c in get_cfg_value("chat_ids", [])]
    if len(ids) >= _CHAT_MAX:
        return t("addchat_limit", max=_CHAT_MAX)
    if new_id in ids:
        return t("addchat_already_exists", id=new_id)
    mutate_cfg(lambda c: c["chat_ids"].append(new_id))
    init_chat_config(new_id)
    return t("addchat_confirm", id=new_id)


def cmd_removechat(parts: list, caller_id: str) -> str:
    """Remove an authorized chat ID. Admin only. Usage: /removechat <id>"""
    if len(parts) < 2:
        return t("removechat_format_error")
    try:
        rem_id = str(int(parts[1]))
    except ValueError:
        return t("removechat_format_error")
    if rem_id == str(caller_id):
        return t("removechat_self_remove")
    ids = [str(c) for c in get_cfg_value("chat_ids", [])]
    if rem_id not in ids:
        return t("removechat_not_found", id=rem_id)
    def _remove(c: dict):
        c["chat_ids"] = [x for x in c.get("chat_ids", []) if str(x) != rem_id]
    mutate_cfg(_remove)
    _purge_chat_data(rem_id)
    return t("removechat_confirm", id=rem_id)


def cmd_listchats() -> str:
    """List all authorized chat IDs. Admin only."""
    ids = get_cfg_value("chat_ids", [])
    if not ids:
        return t("listchats_empty")
    rows = []
    for i, cid in enumerate(ids):
        rows.append(t("listchats_row", n=i + 1, id=cid,
                      admin=" ⭐" if i == 0 else ""))
    return t("listchats_header") + "\n" + "\n".join(rows)


# ─── Portfolio P&L (G2) ───────────────────────────────────
# Unrealized P&L only (v1). No sell/realize tracking.
# Price source: yfinance (optional dep — same as /checkprice and /checknews).
# Storage: cfg["portfolio"] = list of lot dicts.
# All pure helpers are IO-free and offline-testable.

_PORTFOLIO_MAX         = 50    # hard cap on number of lots
_PORTFOLIO_HISTORY_CAP = 730  # max records in portfolio_history.json (J4 ≈ 2 years)


def _parse_pos_args(parts: list) -> "dict | str":
    """PURE: validate /addpos args; return lot dict or i18n error key.

    Expected: /addpos TICKER QTY PRICE [DATE]
    QTY: positive float (fractional shares OK).
    PRICE: non-negative float (0 = free/granted share).
    DATE: optional ISO YYYY-MM-DD; format error → error key.
    """
    if len(parts) < 4:
        return "addpos_usage"
    ticker = parts[1].upper().strip()
    try:
        qty = float(parts[2])
    except ValueError:
        return "addpos_invalid_qty"
    if qty <= 0:
        return "addpos_invalid_qty"
    try:
        cost = float(parts[3])
    except ValueError:
        return "addpos_invalid_cost"
    if cost < 0:
        return "addpos_invalid_cost"
    date = ""
    if len(parts) >= 5:
        try:
            datetime.fromisoformat(parts[4])
            date = parts[4]
        except ValueError:
            return "addpos_invalid_date"
    return {"ticker": ticker, "qty": qty, "cost": cost, "date": date}


def aggregate_positions(lots: list) -> dict:
    """PURE: collapse lots → {ticker: (total_qty, weighted_avg_cost)}, alphabetical.

    Weighted avg cost = Σ(qty·cost) / Σqty per ticker.
    Returns OrderedDict-equivalent sorted by ticker.
    """
    totals: dict[str, list] = {}   # ticker → [sum_qty, sum_cost_qty]
    for lot in lots:
        t_key = lot["ticker"]
        if t_key not in totals:
            totals[t_key] = [0.0, 0.0]
        totals[t_key][0] += lot["qty"]
        totals[t_key][1] += lot["qty"] * lot["cost"]
    result = {}
    for ticker in sorted(totals):
        sq, scq = totals[ticker]
        avg_cost = scq / sq if sq else 0.0
        result[ticker] = (sq, avg_cost)
    return result


def compute_pnl_rows(agg: dict, prices: dict) -> list:
    """PURE: compute per-ticker P&L rows.

    agg:    {ticker: (total_qty, avg_cost)}
    prices: {ticker: float | None}
    Row keys: ticker, qty, avg_cost, last, value, pnl_usd, pnl_pct.
    last=None → value/pnl_usd/pnl_pct = None (shown as n/a).
    avg_cost=0 → pnl_usd = value, pnl_pct = None.
    """
    rows = []
    for ticker, (qty, avg_cost) in agg.items():
        last = prices.get(ticker)
        if last is None:
            rows.append({
                "ticker": ticker, "qty": qty, "avg_cost": avg_cost,
                "last": None, "value": None, "pnl_usd": None, "pnl_pct": None,
            })
        else:
            value   = qty * last
            cost_b  = qty * avg_cost
            pnl_usd = value - cost_b
            pnl_pct = (pnl_usd / cost_b * 100.0) if avg_cost != 0 else None
            rows.append({
                "ticker": ticker, "qty": qty, "avg_cost": avg_cost,
                "last": last, "value": value,
                "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
            })
    return rows


def _fmt_ticker(tk: str, width: int = 6) -> str:
    """Fit ticker into fixed column: truncate long names to width-1 + '…'."""
    if len(tk) <= width:
        return tk.ljust(width)
    return tk[:width - 1] + "…"


def _fmt_value(v: "float | None") -> str:
    """Format dollar value as rounded integer with thousands separator."""
    if v is None:
        return "n/a"
    return f"${int(round(v)):,}"


def _digest_top_movers(priced: list) -> "tuple | None":
    """PURE: return (best_row, worst_row) by pnl_pct, or None.

    Best = highest pnl_pct, worst = lowest. Requires ≥2 eligible rows
    (pnl_pct is not None) with distinct tickers.
    """
    eligible = [r for r in priced if r["pnl_pct"] is not None]
    if len(eligible) < 2:
        return None
    best = max(eligible, key=lambda r: r["pnl_pct"])
    worst = min(eligible, key=lambda r: r["pnl_pct"])
    if best["ticker"] == worst["ticker"]:
        return None
    return best, worst


def _fmt_pct(pct: "float | None") -> str:
    """Format P&L percent: +12.3% or n/a."""
    if pct is None:
        return "n/a"
    return f"{pct:+.1f}%"


def _fmt_qty_col(qty: float, width: int = 5) -> str:
    """Cosmetic: fit qty into fixed column width for monospace table.

    Abbreviates when _fmt_qty output exceeds width:
      ≥ 1 000 000  → '1.2M' (or '123M' if needed)
      ≥    10 000  → '12.3k' (or '123k' if needed)
      < 10 000     → reduce decimal places (dp=3 → 2 → 1 → 0)

    PURELY cosmetic — P&L/VALUE calculations always use raw qty.
    """
    raw = _fmt_qty(qty)
    if len(raw) <= width:
        return raw.rjust(width)
    if qty >= 1_000_000:
        s = f"{qty / 1_000_000:.1f}M"
        if len(s) > width:
            s = f"{int(round(qty / 1_000_000))}M"
    elif qty >= 10_000:
        s = f"{qty / 1_000:.1f}k"
        if len(s) > width:
            s = f"{int(round(qty / 1_000))}k"
    else:
        s = raw
        for dp in range(3, -1, -1):
            candidate = f"{qty:.{dp}f}"
            if len(candidate) <= width:
                s = candidate
                break
        else:
            s = str(int(round(qty)))
    return s.rjust(width)


def _pnl_table(rows: list) -> str:
    """PURE: build monospace-aligned P&L table string (no surrounding backticks).

    Column layout (≤38 chars target):
      TICKER  QTY    LAST   VALUE   P&L%
    Header + one data row per position.
    No emojis inside — they break monospace alignment.
    n/a rows: LAST/VALUE/P&L% shown as 'n/a'.
    """
    # Column widths
    W_TK  = 6   # ticker
    W_QTY = 5   # qty (right)
    W_LAS = 7   # last price (right)
    W_VAL = 8   # value (right)
    W_PCT = 7   # pnl% (right)

    sep = " "
    header = (
        "TICKER".ljust(W_TK) + sep +
        "QTY".rjust(W_QTY) + sep +
        "LAST".rjust(W_LAS) + sep +
        "VALUE".rjust(W_VAL) + sep +
        "P&L%".rjust(W_PCT)
    )
    divider = "-" * len(header)
    data_lines = []
    for r in rows:
        tk_col  = _fmt_ticker(r["ticker"], W_TK)
        qty_col = _fmt_qty_col(r["qty"], W_QTY)
        if r["last"] is None:
            las_col = "n/a".rjust(W_LAS)
            val_col = "n/a".rjust(W_VAL)
            pct_col = "n/a".rjust(W_PCT)
        else:
            las_col = f"${r['last']:.2f}".rjust(W_LAS)
            val_col = _fmt_value(r["value"]).rjust(W_VAL)
            pct_col = _fmt_pct(r["pnl_pct"]).rjust(W_PCT)
        data_lines.append(
            tk_col + sep + qty_col + sep + las_col + sep + val_col + sep + pct_col
        )
    return "\n".join([header, divider] + data_lines)


def format_pnl(rows: list) -> str:
    """PURE: Markdown P&L summary using monospace table. Total only over priced rows; n/a footnote."""
    if not rows:
        return t("pnl_empty")

    table_body = _pnl_table(rows)

    na_count   = 0
    total_val  = 0.0
    total_pnl  = 0.0
    total_cost = 0.0
    for r in rows:
        if r["last"] is None:
            na_count += 1
        else:
            total_val  += r["value"]
            total_pnl  += r["pnl_usd"]
            total_cost += r["qty"] * r["avg_cost"]

    priced = len(rows) - na_count
    lines = [t("pnl_header"), f"```\n{table_body}\n```"]

    if priced > 0:
        t_emoji = "📈" if total_pnl >= 0 else "📉"
        t_pct   = _fmt_pct(total_pnl / total_cost * 100.0 if total_cost != 0 else None)
        lines.append(t("pnl_total",
                       emoji=t_emoji,
                       value=total_val,
                       pnl_usd=total_pnl,
                       pnl_pct=t_pct))
    if na_count:
        lines.append(t("pnl_na_note", count=na_count))
    return "\n".join(lines)


def _fmt_qty(qty: float) -> str:
    """Format quantity: integer if whole, up to 4 sig-fig decimal otherwise."""
    return str(int(qty)) if qty == int(qty) else f"{qty:g}"


def _pnl_rich_md(rows: list) -> str:
    """PURE: build GFM-rich P&L table for sendRichMessage.

    Returns '' when rows is empty (caller falls back to legacy pnl_empty).
    Uses **bold** (GFM) instead of *bold* (legacy). No fixed-width columns
    — raw _fmt_qty is shown, so K3/QTY-overflow bug-class is eliminated.
    """
    if not rows:
        return ""

    na_count = 0
    total_val = 0.0
    total_pnl = 0.0
    total_cost = 0.0
    for r in rows:
        if r["last"] is None:
            na_count += 1
        else:
            total_val += r["value"]
            total_pnl += r["pnl_usd"]
            total_cost += r["qty"] * r["avg_cost"]

    priced = len(rows) - na_count
    lines = [t("pnl_rich_header")]

    lines.append("| Ticker | Qty | Last | Value | P&L% |")
    lines.append("| --- | ---: | ---: | ---: | ---: |")
    for r in rows:
        tk = r["ticker"]
        qty = _fmt_qty(r["qty"])
        if r["last"] is None:
            lines.append(f"| {tk} | {qty} | n/a | n/a | n/a |")
        else:
            last = f"${r['last']:.2f}"
            val = _fmt_value(r["value"])
            pct = _fmt_pct(r["pnl_pct"])
            lines.append(f"| {tk} | {qty} | {last} | {val} | {pct} |")

    if priced > 0:
        t_emoji = "📈" if total_pnl >= 0 else "📉"
        t_pct = _fmt_pct(total_pnl / total_cost * 100.0 if total_cost != 0 else None)
        lines.append(t("pnl_rich_total",
                       emoji=t_emoji,
                       value=f"${int(round(total_val)):,}",
                       pnl_usd=f"{total_pnl:+,.0f}",
                       pnl_pct=t_pct))
    if na_count:
        lines.append(t("pnl_na_note", count=na_count))
    return "\n".join(lines)


def fetch_last_close(ticker: str) -> "float | None":
    """THIN IO: yfinance last 5 trading days → most recent close. Any exception → None."""
    if not YF_OK:
        return None
    try:
        def _call():
            h = yf.Ticker(ticker).history(period="5d")
            if h is None or h.empty:
                return None
            closes = h["Close"].dropna()
            return float(closes.iloc[-1]) if not closes.empty else None
        return retry(_call, attempts=2, base=3, cap=15,
                     label=f"last_close {ticker}",
                     on_error=lambda e, a: status_inc("yf_errors"))
    except Exception as e:
        log.debug(f"fetch_last_close({ticker!r}): {e}")
        return None


def cmd_addpos(parts: list) -> str:
    lot_or_err = _parse_pos_args(parts)
    if isinstance(lot_or_err, str):
        return t(lot_or_err)
    lot = lot_or_err
    outcome: list[str] = []

    def _add(c: dict) -> None:
        portfolio = c.setdefault("portfolio", [])
        if len(portfolio) >= _PORTFOLIO_MAX:
            outcome.append("limit")
        else:
            portfolio.append(lot)
            outcome.append("ok")

    mutate_chat_cfg(_add)
    if outcome and outcome[0] == "limit":
        return t("addpos_limit", max=_PORTFOLIO_MAX)
    return t("addpos_added",
             ticker=_md_escape(lot["ticker"]),
             qty=_fmt_qty(lot["qty"]),
             cost=lot["cost"])


def cmd_removepos(parts: list) -> str:
    if len(parts) < 2:
        return t("removepos_usage")
    ticker = parts[1].upper().strip()
    removed: list[int] = []

    def _remove(c: dict) -> None:
        portfolio = c.get("portfolio", [])
        before    = len(portfolio)
        c["portfolio"] = [l for l in portfolio if l["ticker"] != ticker]
        removed.append(before - len(c["portfolio"]))

    mutate_chat_cfg(_remove)
    count = removed[0] if removed else 0
    if count == 0:
        return t("removepos_not_found", ticker=_md_escape(ticker))
    return t("removepos_removed",
             ticker=_md_escape(ticker), count=count)


# ─── Portfolio value history helpers (J4) ─────────────────────────────────────

def _prune_history(h: dict, cap: int = _PORTFOLIO_HISTORY_CAP) -> dict:
    """PURE: remove oldest entries until len <= cap. Returns new dict."""
    if len(h) <= cap:
        return h
    keys = sorted(h.keys())
    trim = keys[:len(h) - cap]
    return {k: v for k, v in h.items() if k not in trim}


def _compute_delta(history: dict, today_val: float, days: int) -> "tuple[float, float] | None":
    """PURE: compute (abs_delta, pct_delta) vs snapshot ~`days` ago.

    Finds nearest record <= target date (today - days), within 5-day tolerance.
    Returns None if no qualifying record found, or if base value is 0.
    today_val itself is NOT in history yet when this is called.
    """
    from datetime import date, timedelta
    target = date.today() - timedelta(days=days)
    target_str = target.isoformat()
    candidates = [k for k in history if k <= target_str]
    if not candidates:
        return None
    best = max(candidates)
    cutoff = (target - timedelta(days=5)).isoformat()
    if best < cutoff:
        return None
    base_val = history[best]
    if base_val == 0:
        return None
    abs_delta = today_val - base_val
    pct_delta = abs_delta / base_val * 100.0
    return abs_delta, pct_delta


def load_portfolio_history() -> dict:
    """IO: read portfolio_history.json → dict. Missing/corrupt → {}."""
    with _phistory_lock:
        return _read_json(PORTFOLIO_HISTORY, {})


def save_portfolio_history(h: dict) -> None:
    """IO: atomically write portfolio_history.json."""
    with _phistory_lock:
        _atomic_write_json(PORTFOLIO_HISTORY, h)


def maybe_snapshot_portfolio_value(agg: dict, prices: dict) -> None:
    """IO: write today's UTC total value if ALL tickers have prices.

    agg:    {ticker: (qty, avg_cost)}
    prices: {ticker: float | None}

    If any price is None → log.debug + skip (no partial snapshot written).
    Same-day second call overwrites (last price wins).
    """
    if not YF_OK or not agg:
        return
    for ticker in agg:
        if prices.get(ticker) is None:
            log.debug(f"maybe_snapshot: skipping — no price for {ticker}")
            return
    total = sum(agg[tk][0] * prices[tk] for tk in agg)
    from datetime import timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    h = load_portfolio_history()
    h[today] = total
    h = _prune_history(h, cap=_PORTFOLIO_HISTORY_CAP)
    save_portfolio_history(h)
    log.debug(f"maybe_snapshot: saved {today} = {total:.2f}")


def _format_delta(abs_d: "float | None", pct_d: "float | None") -> str:
    """PURE: format one delta column. None → 'n/a'."""
    if abs_d is None or pct_d is None:
        return "n/a"
    emoji = "📈" if abs_d >= 0 else "📉"
    sign  = "+" if abs_d >= 0 else ""
    return f"{emoji} {sign}${abs_d:,.2f} ({sign}{pct_d:.2f}%)"


def cmd_pnl() -> None:
    if not YF_OK:
        tg(t("yfinance_missing", cmd="/pnl"))
        return
    cfg  = get_chat_cfg()
    lots = cfg.get("portfolio", [])
    if not lots:
        tg(t("pnl_empty"))
        return
    agg    = aggregate_positions(lots)
    prices: dict[str, "float | None"] = {}
    for ticker in agg:
        prices[ticker] = fetch_last_close(ticker)
        time.sleep(0.5)
    rows = compute_pnl_rows(agg, prices)
    # J4: opportunistic snapshot (only if all prices present)
    maybe_snapshot_portfolio_value(agg, prices)
    base = format_pnl(rows)
    # J4: append delta line if snapshot data available
    priced_rows = [r for r in rows if r["last"] is not None]
    delta_line = ""
    if priced_rows and len(priced_rows) == len(rows):
        today_val = sum(r["value"] for r in priced_rows)
        h = load_portfolio_history()
        from datetime import timezone
        today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        h_without_today = {k: v for k, v in h.items() if k != today_key}
        d1  = _compute_delta(h_without_today, today_val, 1)
        d7  = _compute_delta(h_without_today, today_val, 7)
        d30 = _compute_delta(h_without_today, today_val, 30)
        col1  = _format_delta(d1[0]  if d1  else None, d1[1]  if d1  else None)
        col7  = _format_delta(d7[0]  if d7  else None, d7[1]  if d7  else None)
        col30 = _format_delta(d30[0] if d30 else None, d30[1] if d30 else None)
        delta_line = t("pnl_delta_line", d1=col1, d7=col7, d30=col30)
    # Rich path: GFM table (K3/QTY-overflow eliminated)
    rich_body = _pnl_rich_md(rows)
    legacy_full = base + ("\n" + delta_line if delta_line else "")
    if rich_body:
        rich_full = rich_body + ("\n" + delta_line if delta_line else "")
        tg(legacy_full, rich_md=rich_full)
    else:
        tg(legacy_full)


# ─── /sheet — financial statements from EDGAR (Company Facts API) ──

def _sheet_format_val(v: "float | None", abbreviate: bool = True) -> str:
    """PURE: format a financial value. None → n/a. Large values abbreviated."""
    if v is None:
        return "n/a"
    if abbreviate and abs(v) >= 1_000_000_000:
        return f"{v / 1_000_000_000:+.2f}B"
    if abbreviate and abs(v) >= 1_000_000:
        return f"{v / 1_000_000:+.1f}M"
    if abbreviate and abs(v) >= 10_000:
        return f"{v / 1_000:+.1f}K"
    return f"{v:+,.0f}"


def _sheet_rich_md(ticker: str, period: str, sections: list) -> str:
    """PURE: GFM-rich counterpart to cmd_sheet's ASCII code block.

    sections: [(title, short_cols, rows), ...] where rows = [(label, vals_str), ...]
    Returns '' when sections is empty or content exceeds 32768 chars.
    """
    if not sections:
        return ""
    lines = [t("sheet_rich_header", ticker=ticker, period=period)]
    for title, short_cols, rows in sections:
        lines.append("")
        lines.append(t("sheet_rich_section", title=title))
        header = "| Concept | " + " | ".join(short_cols) + " |"
        sep = "|:--|" + "--:|" * len(short_cols)
        lines.append(header)
        lines.append(sep)
        for label, vals_str in rows:
            cells = vals_str.split("  ")
            lines.append("| " + label + " | " + " | ".join(cells) + " |")
    body = "\n".join(lines)
    if len(body) > 32768:
        return ""
    return body


def cmd_sheet(parts: list) -> str:
    """Usage: /sheet TICKER [year_from-year_to] [quarterly|yearly]

    Fetches multi-year financial statements via Company Facts API.
    Default: last 5 years, yearly.
    """
    if len(parts) < 2:
        return t("sheet_usage")
    ticker = parts[1].upper().strip()

    now = datetime.now()
    year_from = now.year - 4
    year_to = now.year
    num_years = 5

    for arg in parts[2:]:
        low = arg.lower().strip()
        if low in ("quarterly", "q", "çeyreklik", "çeyrek"):
            num_years = 12  # quarterly: ~3 years
        elif low in ("yearly", "y", "yıllık", "yıl"):
            num_years = 5
        elif "-" in low:
            try:
                a, b = low.split("-", 1)
                year_from = int(a)
                year_to = int(b)
                num_years = year_to - year_from + 1
            except (ValueError, TypeError):
                pass

    tg(t("sheet_fetching", ticker=ticker))

    try:
        company = Company(ticker)
    except Exception as e:
        log.error(f"sheet {ticker}: Company init failed: {e}")
        return t("sheet_fetch_failed", ticker=ticker)

    period = t("sheet_period_quarterly") if num_years > 5 else t("sheet_period_yearly")
    lines = [t("sheet_header", ticker=ticker, period=period)]
    found_any = False
    table_lines: list = []
    rich_sections: list = []

    def _fmt(v):
        return _sheet_format_val(v) if v is not None else "n/a"

    def _col_filter(df, year_from, year_to):
        fy_cols = [c for c in df.columns if c.startswith("FY")]
        filtered = []
        for c in fy_cols:
            try:
                yr = int(c.split()[1])
                if year_from <= yr <= year_to:
                    filtered.append(c)
            except (IndexError, ValueError):
                pass
        return filtered

    def _short_hdr(col: str) -> str:
        parts = col.split()
        if len(parts) == 3:
            q = parts[2]
            yr = parts[1][2:]
            return f"{q}'{yr}"
        if len(parts) == 2:
            return parts[1]
        return col

    def _section_block(title: str, fy_cols: list, rows: list):
        """Build a section inside the single code block with aligned columns."""
        if not fy_cols or not rows:
            return []
        short = [_short_hdr(c) for c in fy_cols]
        W_LBL = max(len(r[0]) for r in rows)
        W_LBL = max(W_LBL, 7)
        W_VAL = max(len(r[1].split("  ")[i])
                    for r in rows for i in range(len(fy_cols)))
        W_VAL = max(W_VAL, 3)
        W_HDR = max(len(h) for h in short)
        col_w = max(W_VAL, W_HDR) + 1
        header = "Concept".ljust(W_LBL) + "".join(f" {h:>{col_w}s}" for h in short)
        out = [f"  {title}", header, "-" * len(header)]
        for label, vals_str in rows:
            parts = vals_str.split("  ")
            vals_aligned = "".join(f" {v:>{col_w}s}" for v in parts)
            out.append(f"{label:<{W_LBL}s}{vals_aligned}")
        out.append("")
        return out

    # Income Statement
    try:
        inc = company.income_statement(periods=num_years)
        inc_df = inc.to_dataframe()
        fy_cols = _col_filter(inc_df, year_from, year_to)
        if fy_cols:
            found_any = True
            is_rows = []
            for concept in ["RevenueFromContractWithCustomerExcludingAssessedTax",
                            "GrossProfit", "OperatingIncomeLoss",
                            "NetIncomeLoss", "EarningsPerShareDiluted"]:
                if concept in inc_df.index:
                    vals = "  ".join(_fmt(inc_df.loc[concept, c]) for c in fy_cols)
                    short = concept.replace("RevenueFromContractWithCustomerExcludingAssessedTax", "Revenue")
                    is_rows.append((short, vals))
            table_lines.extend(_section_block("INCOME STATEMENT", fy_cols, is_rows))
            rich_sections.append(("INCOME STATEMENT", [_short_hdr(c) for c in fy_cols], is_rows))
    except Exception as e:
        log.debug(f"sheet {ticker}: income_statement failed: {e}")

    # Balance Sheet
    try:
        bs = company.balance_sheet(periods=num_years)
        bs_df = bs.to_dataframe()
        fy_cols = _col_filter(bs_df, year_from, year_to)
        if fy_cols:
            found_any = True
            bs_rows = []
            for concept in ["CashAndCashEquivalentsAtCarryingValue",
                            "Assets", "Liabilities", "StockholdersEquity"]:
                if concept in bs_df.index:
                    vals = "  ".join(_fmt(bs_df.loc[concept, c]) for c in fy_cols)
                    bs_rows.append((concept, vals))
            table_lines.extend(_section_block("BALANCE SHEET", fy_cols, bs_rows))
            rich_sections.append(("BALANCE SHEET", [_short_hdr(c) for c in fy_cols], bs_rows))
    except Exception as e:
        log.debug(f"sheet {ticker}: balance_sheet failed: {e}")

    # Cash Flow
    try:
        cf = company.cashflow_statement(periods=num_years)
        cf_df = cf.to_dataframe()
        fy_cols = _col_filter(cf_df, year_from, year_to)
        if fy_cols:
            found_any = True
            cf_rows = []
            for concept in ["NetCashProvidedByUsedInOperatingActivities",
                            "NetCashProvidedByUsedInInvestingActivities",
                            "NetCashProvidedByUsedInFinancingActivities"]:
                if concept in cf_df.index:
                    vals = "  ".join(_fmt(cf_df.loc[concept, c]) for c in fy_cols)
                    short = concept.replace("NetCashProvidedByUsedIn", "Cash:")
                    cf_rows.append((short, vals))
            table_lines.extend(_section_block("CASH FLOW", fy_cols, cf_rows))
            rich_sections.append(("CASH FLOW", [_short_hdr(c) for c in fy_cols], cf_rows))
    except Exception as e:
        log.debug(f"sheet {ticker}: cashflow_statement failed: {e}")

    if not found_any:
        return t("sheet_no_data", ticker=ticker)

    if table_lines:
        lines.append("```\n" + "\n".join(table_lines) + "```")

    legacy_body = "\n".join(lines).rstrip()
    rich = _sheet_rich_md(ticker, period, rich_sections)
    tg(legacy_body, rich_md=rich or None)
    return None


# ─── DEF 14A proxy analysis (edgartools) ───────────────────

def analyze_def14a(ticker: str) -> str | None:
    """Extract proxy statement data using edgartools. Returns formatted string or None."""
    try:
        company = Company(ticker)
        filings = company.get_filings(form="DEF 14A")
        if filings is None or len(filings) == 0:
            return None
        latest = filings.latest()
        if latest is None:
            return None
        proxy = latest.obj()
    except Exception as e:
        log.debug(f"analyze_def14a {ticker}: {e}")
        return None

    lines = [f"🗳️ *{ticker} — Proxy Statement (DEF 14A)*\n"]

    # CEO / PEO info
    try:
        peo_name = getattr(proxy, "peo_name", None)
        peo_total = getattr(proxy, "peo_total_comp", None)
        peo_paid = getattr(proxy, "peo_actually_paid_comp", None)
        if peo_name:
            lines.append(f"*Principal Executive Officer:* {peo_name}")
        if peo_total is not None:
            lines.append(f"*CEO Total Compensation (SCT):* ${peo_total:,.0f}")
        if peo_paid is not None:
            lines.append(f"*CEO Compensation Actually Paid:* ${peo_paid:,.0f}")
        lines.append("")
    except Exception as e:
        log.debug(f"analyze_def14a peo: {e}")

    # Executive compensation
    try:
        exec_comp = proxy.executive_compensation
        if exec_comp is not None and hasattr(exec_comp, "to_dataframe"):
            df = exec_comp.to_dataframe()
            if df is not None and not df.empty:
                lines.append("*Executive Compensation:*")
                for _, row in df.head(10).iterrows():
                    name = row.get("Name", row.get("name", ""))
                    comp = row.get("Total", row.get("total", row.get("Total Compensation", None)))
                    if name:
                        comp_str = f"${comp:,.0f}" if comp and comp != 0 else "n/a"
                        lines.append(f"  • {name}: {comp_str}")
                lines.append("")
    except Exception as e:
        log.debug(f"analyze_def14a exec_comp: {e}")

    # Pay vs Performance
    try:
        pvp = proxy.pay_vs_performance
        if pvp is not None and hasattr(pvp, "to_dataframe"):
            df = pvp.to_dataframe()
            if df is not None and not df.empty:
                lines.append("*Pay vs Performance:*")
                for _, row in df.head(5).iterrows():
                    year = row.get("Year", row.get("year", ""))
                    comp = row.get("SummaryCompensationTotal", row.get("compensation", ""))
                    perf = row.get("NetIncome", row.get("net_income", ""))
                    if year:
                        comp_str = f"${comp:,.0f}" if comp else "n/a"
                        perf_str = f"${perf:,.0f}" if perf else "n/a"
                        lines.append(f"  {year}: Comp {comp_str} · Net Income {perf_str}")
                lines.append("")
    except Exception as e:
        log.debug(f"analyze_def14a pay_vs_perf: {e}")

    # Company-selected performance measures
    try:
        measures = getattr(proxy, "performance_measures", None)
        selected = getattr(proxy, "company_selected_measure", None)
        selected_val = getattr(proxy, "company_selected_measure_value", None)
        if measures:
            lines.append("*Performance Measures:*")
            if isinstance(measures, list):
                for m in measures[:5]:
                    lines.append(f"  • {m}")
            else:
                lines.append(f"  {measures}")
            if selected:
                val_str = f" = {selected_val:,.0f}" if selected_val else ""
                lines.append(f"  *Company Selected:* {selected}{val_str}")
            lines.append("")
    except Exception as e:
        log.debug(f"analyze_def14a perf_measures: {e}")

    # Shareholder returns
    try:
        tsr = getattr(proxy, "total_shareholder_return", None)
        peer_tsr = getattr(proxy, "peer_group_tsr", None)
        if tsr is not None or peer_tsr is not None:
            lines.append("*Shareholder Returns:*")
            if tsr is not None:
                lines.append(f"  Company TSR: {tsr:.1%}" if isinstance(tsr, float) else f"  Company TSR: {tsr}")
            if peer_tsr is not None:
                lines.append(f"  Peer Group TSR: {peer_tsr:.1%}" if isinstance(peer_tsr, float) else f"  Peer Group TSR: {peer_tsr}")
            lines.append("")
    except Exception as e:
        log.debug(f"analyze_def14a tsr: {e}")

    if len(lines) <= 2:
        return None

    return "\n".join(lines).rstrip()


# ─── Filing markdown (full text as .md) ───────────────────

def fetch_filing_markdown(ticker: str, form: str) -> tuple[str, str] | None:
    """Fetch the latest filing's markdown content via edgartools.

    Returns (filename, markdown_content) or None.
    """
    try:
        company = Company(ticker)
        filings = company.get_filings(form=form)
        if filings is None or len(filings) == 0:
            return None
        latest = filings.latest()
        md = latest.markdown()
        if not md:
            return None
        safe_form = form.replace(" ", "_")
        filename = f"{ticker}_{safe_form}_{latest.date.strftime('%Y-%m-%d')}.md"
        return filename, md
    except Exception as e:
        log.debug(f"fetch_filing_markdown {ticker} {form}: {e}")
        return None


def cmd_fulltext(parts: list) -> str:
    """Usage: /fulltext TICKER [FORM] — send the latest filing as a .md file."""
    if len(parts) < 2:
        return t("fulltext_usage")
    ticker = parts[1].upper().strip()
    form = parts[2].upper().strip() if len(parts) >= 3 else "10-K"
    form = _match_form(form) or form

    result = fetch_filing_markdown(ticker, form)
    if result is None:
        return t("fulltext_failed", ticker=ticker, form=form)
    filename, content = result
    tg_send_document(filename, content, caption=f"📄 {ticker} {form}")
    return ""


def _should_run_scheduled_scan(cid: str, now: datetime, last_sched_scan: dict) -> bool:
    """PURE: per-chat gate for scheduled scan. Returns True if the scan should run."""
    last = last_sched_scan.get(cid, datetime.min)
    return (now - last).total_seconds() > 90


# ─── Background thread ────────────────────────────────────
def background_thread():
    log.info("Background thread started.")
    last_alarm_check: dict[str, datetime] = {}   # chat_id → last alarm check time
    last_digest_yw: dict[str, tuple] = {}        # chat_id → (ISO year, ISO week)
    last_sched_scan: dict[str, datetime] = {}    # chat_id → last scheduled scan time
    last_dailynews: dict[str, str] = {}          # chat_id → last daily news date (ISO)
    bg_errors = 0

    while not _stop_event.is_set():
        try:
            now = datetime.now()
            chat_ids = get_cfg_value("chat_ids", [])

            for cid in chat_ids:
                cid = str(cid)
                try:
                    # Set reactive context so per-chat config works
                    _ctx.chat_id = cid
                    cfg = get_chat_cfg()

                    # Auto schedule — per-user
                    schedule_str = cfg.get("schedule")
                    if schedule_str:
                        sh, sd = _parse_hhmm(schedule_str)
                        if sh >= 0 and now.hour == sh and now.minute == sd:
                            if _should_run_scheduled_scan(cid, now, last_sched_scan):
                                log.info(f"Scheduled scan ({cid}): {schedule_str}")
                                _tg_to(cid, t("scheduled_scan_starting", time=schedule_str))
                                # Run scan with this user's tickers/forms
                                items = cfg.get("tickers", [])
                                forms = cfg.get("default_forms", DEFAULT_FORMS)
                                if items:
                                    for ticker in items:
                                        scan_ticker(ticker, forms, True, True, quiet=True)
                                        time.sleep(2)
                                # Portfolio snapshot
                                if YF_OK:
                                    _bg_lots = cfg.get("portfolio", [])
                                    if _bg_lots:
                                        _bg_agg = aggregate_positions(_bg_lots)
                                        _bg_prices: dict[str, "float | None"] = {}
                                        for _bg_tk in _bg_agg:
                                            _bg_prices[_bg_tk] = fetch_last_close(_bg_tk)
                                            time.sleep(0.5)
                                        maybe_snapshot_portfolio_value(_bg_agg, _bg_prices)
                                last_sched_scan[cid] = now

                    # Hourly alarm — per-user (probe only)
                    if cfg.get("alarm_on"):
                        last_ac = last_alarm_check.get(cid, datetime.min)
                        if (now - last_ac).total_seconds() >= 3600:
                            log.info(f"Alarm probe ({cid})")
                            # Use this user's tickers for probe
                            user_tickers = cfg.get("tickers", [])
                            if user_tickers:
                                hits = []
                                for tk in user_tickers:
                                    for form in cfg.get("default_forms", DEFAULT_FORMS):
                                        rows = fetch_new_filings(
                                            tk, [form],
                                            cfg.get("days_lookback", 35),
                                            load_cache(), True, True,
                                            n_latest=1, max_chars_per=100,
                                        )
                                        if rows:
                                            hits.append((tk, form, rows[0][1]))
                                        time.sleep(0.5)
                                if hits:
                                    send_alarm_alert(hits)

                            # Watchword scan — per-user
                            words = cfg.get("watchwords", [])
                            if words:
                                from_date = (now - timedelta(hours=24)).strftime("%Y-%m-%d")
                                with _watchword_lock:
                                    ww_state = _read_json(WATCHWORD_SEEN, {})
                                for phrase in words:
                                    ww_hits = fetch_fts_hits(phrase, from_date)
                                    if ww_hits is None:
                                        time.sleep(1)
                                        continue
                                    new_acc = [h["accession"] for h in ww_hits]
                                    with _watchword_lock:
                                        fresh_acc = _update_watchword_seen(
                                            ww_state, phrase, new_acc
                                        )
                                        _atomic_write_json(WATCHWORD_SEEN, ww_state)
                                    if fresh_acc:
                                        fresh_set  = set(fresh_acc)
                                        fresh_hits = [h for h in ww_hits
                                                      if h["accession"] in fresh_set]
                                        text = format_watchword_alert(phrase, fresh_hits)
                                        rich = format_watchword_alert_rich(phrase, fresh_hits)
                                        analyzable = _watchword_analyzable_hits(fresh_hits)
                                        if analyzable:
                                            token = register_alarm_hits(analyzable)
                                            _tg_with_keyboard_to(cid, text,
                                                build_alarm_keyboard(token, analyzable, set()),
                                                rich_md=rich or None)
                                        else:
                                            _tg_to(cid, text, rich_md=rich or None)
                                    time.sleep(1)

                            last_alarm_check[cid] = now

                    # Weekly digest (Sunday 09:00) — per-user
                    if cfg.get("weekly_digest"):
                        cur_yw = tuple(now.isocalendar()[:2])
                        user_last = last_digest_yw.get(cid, (-1, -1))
                        if (now.weekday() == 6 and now.hour == 9
                                and now.minute == 0 and user_last != cur_yw):
                            log.info(f"Sending weekly digest ({cid}).")
                            send_weekly_digest()
                            last_digest_yw[cid] = cur_yw

                    # Daily news (N2) — per-user, 08:00
                    if cfg.get("daily_news"):
                        today_iso = now.date().isoformat()
                        if now.hour == 8 and now.minute == 0 and last_dailynews.get(cid) != today_iso:
                            log.info(f"Sending daily news ({cid}).")
                            send_daily_news()
                            last_dailynews[cid] = today_iso

                except Exception as e:
                    log.error(f"Background error for chat {cid}: {e}")
                finally:
                    _ctx.chat_id = None

            bg_errors = 0
        except Exception as e:
            bg_errors += 1
            log.exception(f"Background iteration failed ({bg_errors}): {e}")
            if bg_errors == 1:
                try:
                    tg(t("background_error"))
                except Exception:
                    pass

        time.sleep(60)

    log.info("Background thread stopped.")

# ─── First-run wizard ─────────────────────────────────────
WIZARD: dict = {}

def _tg_to_master(text: str):
    """Send a message to the master chat only (for startup wizard)."""
    if _is_valid_chat_id(MASTER_CHAT_ID):
        _tg_to(str(MASTER_CHAT_ID), text)

def start_wizard():
    """Step 1: language picker. Bilingual until the user picks. Persists wizard_step."""
    WIZARD["step"] = "lang"
    mutate_cfg(lambda c: c.update({"wizard_step": "lang"}))
    _tg_to_master(t("wizard_lang_menu"))

def _advance_to_api_step():
    """After language chosen: welcome (in selected lang) + API key menu. Persists step."""
    WIZARD["step"] = "api"
    mutate_cfg(lambda c: c.update({"wizard_step": "api"}))
    api_keys = get_cfg().get("api_keys", {})
    llm_keys = {p: k for p, k in api_keys.items() if p in _PROVIDERS and k}
    if llm_keys:
        masked = ", ".join(f"`{p}` ({_mask_key(k)})" for p, k in llm_keys.items())
        _tg_to_master(t("welcome_bootstrap", master_chat_id=MASTER_CHAT_ID, version=__version__)
           + "\n\n" + t("wizard_api_existing", masked_providers=masked))
    else:
        _tg_to_master(t("welcome_bootstrap", master_chat_id=MASTER_CHAT_ID, version=__version__)
           + "\n\n" + t("wizard_api_menu"))

def _advance_to_forms_step():
    """Move to forms step. Persists wizard_step."""
    WIZARD["step"] = "forms"
    mutate_cfg(lambda c: c.update({"wizard_step": "forms"}))
    _tg_to_master(t("wizard_form_menu"))

def _show_wizard_step_menu(step: str):
    """On restart: re-display the menu for the current wizard step."""
    if step == "api":
        _advance_to_api_step()
    elif step == "forms":
        _tg_to_master(t("wizard_form_menu"))
    elif step == "tickers":
        _tg_to_master(t("wizard_ticker_menu"))
    else:
        start_wizard()

def wizard_handle(text: str, parts: list, chat_id: str = "", msg: dict | None = None) -> bool:
    """Route wizard-step messages. Returns True if handled."""
    step = WIZARD.get("step")
    if not step: return False
    if msg is None: msg = {}

    # Step 1 — language picker (bilingual UI)
    if step == "lang":
        if parts and parts[0].lower() == "/lang" and len(parts) >= 2:
            code = parts[1].lower().strip()
            if code in SUPPORTED_LANGS:
                set_lang(code)
                _advance_to_api_step()
                return True
        tg(t("wizard_lang_unknown"))
        return True

    # Step 2 — API key loop (optional; /skip to continue)
    if step == "api":
        if parts and parts[0].lower() == "/addapi":
            result = cmd_addapi(parts, chat_id, msg)
            tg(result)
            # One-message form (key inline) → key saved → prompt for more
            if len(parts) >= 3:
                tg(t("wizard_api_more"))
            # Two-message form → addapi_prompt shown; wizard_api_more sent by _handle_pending_key
            return True
        if text == "/skip":
            _advance_to_forms_step()
            return True
        if text.startswith("/"):
            return False
        tg(t("wizard_api_menu"))
        return True

    # Step 3 — form selection
    if step == "forms":
        if text == "/usedefaults":
            WIZARD["step"] = "tickers"
            mutate_cfg(lambda c: c.update({"default_forms": DEFAULT_FORMS, "wizard_step": "tickers"}))
            tg(t("wizard_forms_set", forms="  ".join(DEFAULT_FORMS))
               + t("wizard_ticker_menu"))
            return True
        if parts and parts[0].lower() == "/setforms" and len(parts) >= 2:
            valid = []
            for f in [p.upper() for p in parts[1:]]:
                m = _match_form(f)
                if m: valid.append(m)
                else: tg(t("unknown_form_skipped", form=f))
            if not valid:
                tg(t("wizard_no_valid_forms") + t("wizard_form_menu"))
                return True
            WIZARD["step"] = "tickers"
            mutate_cfg(lambda c: c.update({"default_forms": valid, "wizard_step": "tickers"}))
            tg(t("wizard_forms_set", forms="  ".join(valid))
               + t("wizard_ticker_menu"))
            return True
        tg(t("wizard_use_default_or_setforms"))
        return True

    # Step 4 — tickers
    if step == "tickers":
        if text == "/skip":
            WIZARD.pop("step", None)
            mutate_cfg(lambda c: c.update({"first_run": False, "wizard_step": ""}))
            tg(t("wizard_complete"))
            return True
        if parts and parts[0].lower() == "/addticker" and len(parts) >= 2:
            result = cmd_addticker(parts)
            WIZARD.pop("step", None)
            mutate_cfg(lambda c: c.update({"first_run": False, "wizard_step": ""}))
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

def build_weekly_csv(entries: list) -> str:
    """PURE: convert weekly_log entries to CSV string.

    Input keys: ticker / form / tarih / ekleme / analiz
    Output columns: ticker, form, filing_date, added_at, analysis
    """
    import csv, io
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_ALL, lineterminator="\n")
    w.writerow(["ticker", "form", "filing_date", "added_at", "analysis"])
    for e in entries:
        w.writerow([e.get("ticker",""), e.get("form",""),
                    e.get("tarih",""), e.get("ekleme",""),
                    e.get("analiz","").replace("\n", " ")])
    return buf.getvalue()

def cmd_export():
    """Send this week's weekly_log as a CSV file."""
    data = get_weekly_log()
    if not data:
        tg(t("export_no_data"))
        return
    content  = build_weekly_csv(data)
    filename = f"sec_export_{datetime.now().strftime('%Y%m%d')}.csv"
    tg_send_document(filename, content, t("export_caption", count=len(data)))

def help_msg() -> str:
    cfg = get_chat_cfg()
    return t("help_block",
             forms="  ".join(cfg['default_forms']),
             ticker_count=len(cfg['tickers']),
             language=get_lang(),
             version=__version__)

# ─── Update handler (polling and webhook) ─────────────────
def handle_analyze_callback(cq: dict, token: str, idx: int):
    """Inline [🔍 TICKER FORM] pressed — analyze that one filing, then retire
    its button from the alert message."""
    entry = get_alarm_hits(token)
    if entry is None or idx < 0 or idx >= len(entry["hits"]):
        tg_answer_callback(cq["id"], t("alarm_expired"))
        return
    if idx in entry["done"]:
        tg_answer_callback(cq["id"], t("alarm_already_done"))
        return
    ticker, form, _date = entry["hits"][idx]
    tg_answer_callback(cq["id"], t("alarm_analyzing", ticker=ticker))
    mark_alarm_done(token, idx)
    # Rebuild the keyboard without the just-used button.
    refreshed = get_alarm_hits(token)
    msg = cq.get("message", {})
    tg_edit_markup(msg.get("chat", {}).get("id"), msg.get("message_id"),
                   build_alarm_keyboard(token, refreshed["hits"], refreshed["done"]))
    # Run the real analysis (LLM) — sends a normal analysis message.
    scan_ticker(ticker, [form], True, True, quiet=False)

def handle_analyzeall_callback(cq: dict, token: str):
    """Inline [🔍 Analyze all] pressed — analyze every pending filing and
    strip the whole keyboard."""
    entry = get_alarm_hits(token)
    if entry is None:
        tg_answer_callback(cq["id"], t("alarm_expired"))
        return
    tg_answer_callback(cq["id"], t("alarm_analyzing_all"))
    msg = cq.get("message", {})
    tg_edit_markup(msg.get("chat", {}).get("id"), msg.get("message_id"), None)
    pending = [(i, h) for i, h in enumerate(entry["hits"])
               if i not in entry["done"]]
    for i, (ticker, form, _date) in pending:
        mark_alarm_done(token, i)
        scan_ticker(ticker, [form], True, True, quiet=False)


# ─── Multi-LLM API management commands (J2) ──────────────

def cmd_addapi(parts: list, chat_id: str, msg: dict) -> str:
    """Add or update an API key — saves per-user if chat has its own config.

    Two-message form: /addapi openrouter  → prompts for key in next message.
    One-message form: /addapi openrouter sk-or-v1-xxx  → saves immediately.
    Rejected in group chats (members can see message before deletion).
    """
    valid = list(_PROVIDERS.keys())
    if msg.get("chat", {}).get("type", "private") != "private":
        return t("addapi_group_rejected")
    if len(parts) < 2:
        return t("addapi_usage", providers=", ".join(valid))
    provider = parts[1].lower()
    if provider not in valid:
        return t("addapi_unknown_provider", provider=provider, providers=", ".join(valid))
    if len(parts) >= 3:
        # One-message form: key is inline
        key = parts[2]
        if len(key) < 8:
            return t("addapi_invalid_key_short")
        _tg_delete_msg(chat_id, msg.get("message_id"))
        # Set context so mutate_chat_cfg routes to per-chat config
        _ctx.chat_id = chat_id
        try:
            def _do(c: dict):
                c.setdefault("api_keys", {})[provider] = key
                if not c.get("default_provider"):
                    c["default_provider"] = provider
            mutate_chat_cfg(_do)
        finally:
            _ctx.chat_id = None
        prefix_warn = _validate_provider_key(provider, key)
        saved_msg = t("addapi_saved", provider=provider, masked_key=_mask_key(key))
        return f"{saved_msg}\n{prefix_warn}" if prefix_warn else saved_msg
    # Two-message form: register pending entry
    with _pending_lock:
        _pending_api_key[chat_id] = {"provider": provider, "expires": time.time() + 120}
    return t("addapi_prompt", provider=provider)


def cmd_apis() -> str:
    """Admin: /apis — list configured LLM providers."""
    cfg = get_chat_cfg()
    api_keys = cfg.get("api_keys", {})
    default = cfg.get("default_provider", "")
    rows = []
    for prov in _PROVIDERS:
        key = api_keys.get(prov, "")
        if key:
            star = " ⭐" if prov == default else ""
            rows.append(t("apis_row", provider=prov, masked_key=_mask_key(key), star=star))
    if not rows:
        return t("apis_empty")
    return t("apis_header") + "\n" + "\n".join(rows)


def cmd_setapi(parts: list) -> str:
    """Admin: /setapi <provider> [model] — change the default LLM provider and model.

    /setapi openrouter                     → set provider, show model menu
    /setapi openrouter meta-llama/...      → set provider + model inline
    """
    valid = list(_PROVIDERS.keys())
    if len(parts) < 2:
        return t("setapi_usage", providers=", ".join(valid))
    provider = parts[1].lower()
    if provider not in _PROVIDERS or not _get_provider_key(provider):
        return t("setapi_unknown", provider=provider)
    # If model specified inline, set both provider and model
    if len(parts) >= 3:
        model = " ".join(parts[2:])
        mutate_chat_cfg(lambda c: c.update({
            "default_provider": provider,
            "provider_models": {**c.get("provider_models", {}), provider: model},
        }))
        return t("setapi_done_model", provider=provider, model=model)
    # No model specified — set provider, show model menu
    mutate_chat_cfg(lambda c: c.update({"default_provider": provider}))
    models = _PROVIDER_MODELS.get(provider, [])
    if not models:
        return t("setapi_done", provider=provider)
    current = _get_provider_model(provider)
    lines = [t("setapi_done", provider=provider), t("setapi_model_menu",
             provider=provider, current=current)]
    for i, m in enumerate(models, 1):
        marker = " *" if m == current else ""
        lines.append(f"  {i}. `{m}`{marker}")
    lines.append(t("setapi_model_hint"))
    return "\n".join(lines)


def cmd_delapi(parts: list) -> str:
    """Admin: /delapi <provider> — delete an API key."""
    all_valid = list(_PROVIDERS.keys())
    if len(parts) < 2:
        return t("delapi_usage", providers=", ".join(all_valid))
    provider = parts[1].lower()
    cfg = get_chat_cfg()
    if not cfg.get("api_keys", {}).get(provider):
        return t("delapi_unknown", provider=provider)
    def _do(c: dict):
        c.setdefault("api_keys", {}).pop(provider, None)
        c.setdefault("provider_models", {}).pop(provider, None)
        if c.get("default_provider") == provider:
            remaining = [p for p in _PROVIDERS if c.get("api_keys", {}).get(p)]
            c["default_provider"] = remaining[0] if remaining else ""
    mutate_chat_cfg(_do)
    return t("delapi_done", provider=provider)


def _process_update(upd: dict):
    global _webhook_active

    # Lazy cleanup: purge expired pending API key entries (M1.7)
    now = time.time()
    with _pending_lock:
        expired = [cid for cid, e in _pending_api_key.items() if now > e["expires"]]
        for cid in expired:
            _pending_api_key.pop(cid, None)

    # Callback query (inline button)
    cq = upd.get("callback_query")
    if cq:
        chat_id = str(cq.get("from", {}).get("id", ""))
        if _is_authorized(chat_id):
            _ctx.chat_id = chat_id
            try:
                data = cq.get("data", "")
                if data.startswith("raw:"):
                    send_raw_filing(cq["id"], data[4:])
                elif data.startswith("md:"):
                    send_md_for_key(cq["id"], data[3:])
                elif data.startswith("analyzeall:"):
                    handle_analyzeall_callback(cq, data[len("analyzeall:"):])
                elif data.startswith("analyze:"):
                    # callback_data form: "analyze:{token}:{idx}"
                    token, _, idx_str = data[len("analyze:"):].rpartition(":")
                    try:
                        idx = int(idx_str)
                    except ValueError:
                        idx = -1
                    handle_analyze_callback(cq, token, idx)
                elif data == "retry":
                    _handle_retry_callback(cq)
                else:
                    tg_answer_callback(cq["id"])
            finally:
                _ctx.chat_id = None
        else:
            log.debug(f"Unauthorized callback from chat_id={chat_id}")
        return

    # Normal message
    msg     = upd.get("message", {})
    chat_id = str(msg.get("chat", {}).get("id", ""))
    raw     = msg.get("text", "").strip()
    text   = raw.lower()
    parts = raw.split()
    komut   = parts[0].lower() if parts else ""

    if not _is_authorized(chat_id):
        log.debug(f"Unauthorized message from chat_id={chat_id}")
        return
    _ctx.chat_id = chat_id
    try:
        # Pending API key intake (J2) — intercept key messages before command dispatch
        with _pending_lock:
            _pentry = _pending_api_key.get(chat_id)
        if _pentry is not None:
            if time.time() <= _pentry["expires"]:
                if not raw.startswith("/"):
                    # This message is the API key
                    _handle_pending_key(chat_id, raw, msg)
                    return
                else:
                    # A command arrived while waiting — cancel pending, fall through
                    with _pending_lock:
                        _pending_api_key.pop(chat_id, None)
                    tg(t("addapi_cancelled"))
            else:
                with _pending_lock:
                    _pending_api_key.pop(chat_id, None)

        if wizard_handle(text, parts, chat_id=chat_id, msg=msg):   return

        # ── Command dispatch table ──────────────────────────────────────
        # Each entry: command → (handler, needs_parts, admin_only)
        # handler(parts) for needs_parts=True, handler() otherwise.
        # Commands with special return handling or side effects stay as elif.
        _CMDS: dict[str, tuple] = {
            "/addticker":    (cmd_addticker,    True,  False),
            "/removeticker": (cmd_removeticker, True,  False),
            "/listtickers":  (cmd_listtickers,  False, False),
            "/addgroup":     (cmd_addgroup,     True,  False),
            "/removegroup":  (cmd_removegroup,  True,  False),
            "/listgroups":   (cmd_listgroups,   False, False),
            "/listforms":    (cmd_listforms,    False, False),
            "/addform":      (cmd_addform,      True,  False),
            "/removeform":   (cmd_removeform,   True,  False),
            "/addword":      (cmd_addword,      True,  False),
            "/removeword":   (cmd_removeword,   True,  False),
            "/listwords":    (cmd_listwords,    False, False),
            "/addpos":       (cmd_addpos,       True,  False),
            "/removepos":    (cmd_removepos,    True,  False),
            "/pnl":          (cmd_pnl,          False, False),
            "/setprompt":    (cmd_setprompt,    True,  False),
            "/getprompt":    (cmd_getprompt,    True,  False),
            "/resetprompt":  (cmd_resetprompt,  True,  False),
            "/listprompts":  (cmd_listprompts,  False, False),
            "/setschedule":  (cmd_setschedule,  True,  False),
            "/alarm":        (cmd_alarm,        True,  False),
            "/setwebhook":   (cmd_setwebhook,   True,  False),
            "/delwebhook":   (cmd_delwebhook,   False, False),
            "/settings":     (cmd_settings,     False, False),
            "/status":       (cmd_status,       False, False),
            "/setlang":      (cmd_setlang,      True,  False),
            "/setmodel":     (cmd_setmodel,     True,  False),
            "/setlookback":  (cmd_setlookback,  True,  False),
            "/setchars":     (cmd_setchars,     True,  False),
            "/setrawmax":    (cmd_setrawmax,    True,  False),
            "/priceaction":  (cmd_priceaction,  True,  False),
            "/setlookforward":(cmd_setlookforward,True, False),
            "/scanticker":   (cmd_scanticker,   True,  False),
            "/compare":      (cmd_compare,      True,  False),
            "/checkprice":   (cmd_checkprice,   True,  False),
            "/checknews":    (cmd_checknews,    True,  False),
            "/sheet":        (cmd_sheet,        True,  False),
            "/fulltext":     (cmd_fulltext,     True,  False),
            "/search":       (cmd_search,       True,  False),
            "/company":      (cmd_company,      True,  False),
            # Admin-only commands
            "/addapi":       (cmd_addapi,       True,  False),
            "/apis":         (cmd_apis,         False, False),
            "/setapi":       (cmd_setapi,       True,  False),
            "/delapi":       (cmd_delapi,       True,  False),
            "/addchat":      (cmd_addchat,      True,  True),
            "/removechat":   (cmd_removechat,   True,  True),
            "/listchats":    (cmd_listchats,    False, True),
        }

        entry = _CMDS.get(komut)
        if entry:
            handler, needs_parts, admin_only = entry
            if admin_only and not _is_admin(chat_id):
                tg(t("unauthorized_admin"))
            elif needs_parts:
                # /addapi and /addchat need extra args beyond parts
                if komut == "/addapi":
                    tg(handler(parts, chat_id, msg))
                elif komut == "/addchat":
                    tg(handler(parts, chat_id))
                elif komut == "/removechat":
                    tg(handler(parts, chat_id))
                else:
                    result = handler(parts)
                    if result: tg(result)
            else:
                result = handler()
                if result: tg(result)
        # Commands with special return/side-effect handling
        elif komut == "/scangroup":
            cmd_scangroup(parts)
        elif komut == "/digest":
            r = cmd_digest(parts)
            if r: tg(r)
        elif komut == "/dailynews":
            r = cmd_dailynews(parts)
            if r: tg(r)
        elif komut == "/setrich":
            r = cmd_setrich(parts)
            if r: tg(r)
        elif komut == "/richtest":
            cmd_richtest()
        elif komut == "/report":
            cmd_report()
        elif komut == "/export":
            cmd_export()
        # Natural-language and keyword triggers (not dict-dispatchable)
        elif text in ["/start", "/help"]:
            tg(help_msg())
        elif text.startswith("/sentiment"):
            if len(parts) >= 2 and parts[1].lower() == "trend":
                cmd_sentiment_trend(parts)
            else:
                cmd_sentiment()
        elif text == "/all":
            cmd_sec(); cmd_insider()
        elif text == "/insider":
            cmd_insider()
        elif text == "/scan":
            cmd_sec()
    finally:
        _ctx.chat_id = None


def run_startup_checks() -> list:
    """
    Validate every critical prerequisite before the bot starts.

    Returns a list of human-readable issue strings; empty list means all
    clear. Covers the three required secrets (EDGAR identity, Telegram token,
    MASTER_CHAT_ID) and the language files the i18n layer depends on.
    OPENROUTER_API_KEY is NOT checked here — it is migrated from .env into
    bot_config.json on first run (J1) and fetched dynamically thereafter.
    Pure-ish: reads module globals only, no side effects — easy to test.
    """
    issues: list = []

    ok, msg = validate_edgar_identity(EDGAR_IDENTITY)
    if not ok:
        issues.append(msg)

    if (not TELEGRAM_BOT_TOKEN
            or TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN"
            or ":" not in TELEGRAM_BOT_TOKEN):
        issues.append("TELEGRAM_BOT_TOKEN is missing or malformed "
                       "(expected '123456:ABC...') — set it in your .env file.")

    if not _is_valid_chat_id(MASTER_CHAT_ID):
        issues.append("MASTER_CHAT_ID is missing or invalid — set it to your "
                       "numeric Telegram chat ID in your .env file.")

    if not LANG_DIR.exists():
        issues.append(f"Language directory not found: {LANG_DIR}")
    elif not list(LANG_DIR.glob("*.json")):
        issues.append(f"No language files (*.json) in {LANG_DIR}")

    return issues


def main():
    global _webhook_active
    log.info(f"Bot v{__version__} started.")

    # Validate every prerequisite at startup; refuse to run on any failure.
    issues = run_startup_checks()
    if issues:
        for issue in issues:
            log.error(f"Startup check failed: {issue}")
            sys.stderr.write(f"\n❌ {issue}\n")
        sys.stderr.write("\nFix the above (edit your .env file) and restart.\n")
        sys.exit(1)

    set_identity(EDGAR_IDENTITY)
    log.info("EDGAR identity registered.")

    # Startup cache cleanup — drop entries older than cache_max_age_days.
    pruned = prune_cache_expired()
    if pruned:
        log.info(f"Pruned {pruned} expired cache entries.")

    # Prime the lang cache early
    _ = get_lang()
    register_bot_commands()

    # Migrate scalar chat_id → chat_ids list (I1). Idempotent.
    _migrate_chat_ids()
    # Guarantee MASTER_CHAT_ID is always chat_ids[0] (J1). Idempotent.
    _ensure_master_in_chat_ids()
    log.info(f"Master chat: {MASTER_CHAT_ID}")
    log.info(f"Authorized chats: {get_cfg_value('chat_ids', [])}")

    # One-time .env migration → bot_config.json (J1).
    migrated = _import_legacy_env()
    if migrated:
        tg(t("env_import_done", keys=", ".join(migrated)))
        log.info(f"Migrated from .env: {migrated}")
    # Migrate legacy openrouter_api_key → api_keys["openrouter"] (J2). Idempotent.
    _migrate_openrouter_key()

    bg = threading.Thread(target=background_thread, daemon=True)
    bg.start()

    cfg = get_cfg()
    if cfg.get("first_run", True):
        wizard_step = cfg.get("wizard_step", "")
        if wizard_step in ("api", "forms", "tickers"):
            WIZARD["step"] = wizard_step
            _show_wizard_step_menu(wizard_step)
        else:
            start_wizard()
    else:
        tg(t("bot_active", version=__version__))

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
                # offset advances BEFORE processing — a bad update is never
                # retried in an infinite loop.
                offset = upd["update_id"] + 1
                # [H7] Per-update crash guard. One malformed update or one
                # bug in a command handler used to propagate out of the
                # polling loop and kill the entire bot. Now it is logged,
                # the user is told, and polling continues.
                try:
                    _process_update(upd)
                except Exception as e:
                    log.exception(
                        f"Update processing failed "
                        f"(update_id={upd.get('update_id')}): {e}")
                    try:
                        tg(t("update_error"))
                    except Exception:
                        pass
            time.sleep(1)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Bot stopping...")
        _stop_event.set()
