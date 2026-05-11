"""
SEC Analyzer Bot — Telegram (English)
Full-featured: wizard setup, ticker & form management,
on-demand scans, configurable LLM settings.
"""

import time, json, logging, hashlib
from datetime import datetime, timedelta
from pathlib import Path

import requests
from edgar import Company, set_identity

from config import (
    EDGAR_IDENTITY, OPENROUTER_API_KEY,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)

# ─── Paths ────────────────────────────────────────────────
BASE_DIR    = Path.home() / "sec-analyzer"
OUTPUT_DIR  = BASE_DIR / "reports"
CACHE_FILE  = BASE_DIR / "cache.json"
CONFIG_FILE = BASE_DIR / "bot_config.json"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

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

# ─── Supported forms ──────────────────────────────────────
FORMS = {
    "10-K":    "Annual report",
    "10-Q":    "Quarterly report",
    "8-K":     "Current events / material events",
    "4":       "Insider buy/sell transactions",
    "144":     "Restricted stock sale notice",
    "SC 13G":  "Passive major shareholder (>5%)",
    "SC 13D":  "Active major shareholder (>5%)",
    "S-1":     "IPO registration statement",
    "424B4":   "Prospectus",
    "20-F":    "Foreign company annual report",
    "6-K":     "Foreign company current report",
    "DEF 14A": "Proxy / shareholder vote statement",
    "11-K":    "Employee retirement plan report",
}
DEFAULT_FORMS = ["10-K", "10-Q", "8-K", "4"]

# ─── Bot config (runtime, managed via Telegram) ───────────
def load_cfg() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        return {}

def save_cfg(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

def get_cfg() -> dict:
    cfg = load_cfg()
    cfg.setdefault("tickers",      [])
    cfg.setdefault("default_forms", DEFAULT_FORMS)
    cfg.setdefault("model",        "openrouter/free")
    cfg.setdefault("days_lookback", 35)
    cfg.setdefault("max_chars",    10000)
    cfg.setdefault("first_run",    True)
    return cfg

def update_cfg(**kwargs):
    cfg = get_cfg()
    cfg.update(kwargs)
    save_cfg(cfg)

# ─── Cache ────────────────────────────────────────────────
def load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except Exception:
        return {}

def save_cache(c: dict):
    CACHE_FILE.write_text(json.dumps(c, indent=2))

def ck(*args) -> str:
    return hashlib.md5("_".join(str(a) for a in args).encode()).hexdigest()

def is_new(cache, *args) -> bool:
    return ck(*args) not in cache

def mark_done(cache, *args):
    cache[ck(*args)] = {"at": datetime.now().isoformat()}

# ─── Telegram ─────────────────────────────────────────────
_TG = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg(text: str):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur); cur = line
        else:
            cur += "\n" + line
    if cur.strip():
        chunks.append(cur)
    for chunk in chunks:
        try:
            requests.post(f"{_TG}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk.strip(),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=15)
            time.sleep(0.3)
        except Exception as e:
            log.error(f"Telegram: {e}")

def get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{_TG}/getUpdates",
                         params={"offset": offset, "timeout": 30}, timeout=35)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates: {e}"); return []

# ─── OpenRouter LLM ───────────────────────────────────────
_OR = "https://openrouter.ai/api/v1/chat/completions"
_OR_H = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/authorturker/sec-analyzer-ai",
    "X-Title": "SEC Analyzer AI",
}
_SYS = (
    "You are an experienced financial analyst specializing in SEC filings. "
    "Analyze documents from an investor's perspective. Be concise, structured, "
    "use bullet points. Highlight key risks and opportunities. Use emojis."
)

def llm(prompt: str, model: str) -> str:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYS},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": 1200, "temperature": 0.2,
    }
    for attempt in range(3):
        try:
            r = requests.post(_OR, headers=_OR_H, json=payload, timeout=120)
            if r.status_code == 429:
                w = 60 * (attempt + 1)
                log.warning(f"Rate limit — waiting {w}s"); time.sleep(w); continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"LLM error (attempt {attempt+1}): {e}"); time.sleep(10)
    return "⚠️ Analysis unavailable — API did not respond."

# ─── Text extraction ──────────────────────────────────────
_SEC = {
    "10-K": ["item 1.", "item 1a.", "item 7.", "item 8."],
    "10-Q": ["item 1.", "item 2.", "item 3."],
}

def prepare(text: str, form: str, max_chars: int) -> str:
    kws = _SEC.get(form, [])
    if not kws:
        return text[:max_chars]
    lines, on, chars, out = text.split("\n"), False, 0, []
    for line in lines:
        if any(line.lower().strip().startswith(k) for k in kws): on = True
        if on:
            out.append(line); chars += len(line)
            if chars >= max_chars: out.append("[text truncated]"); break
    return "\n".join(out) if out else text[:max_chars]

# ─── Analysis prompts ─────────────────────────────────────
def build_prompt(ticker: str, form: str, date: str, body: str) -> str:
    base = f"{ticker} — {form} ({date})\n\n{body}\n\n"
    if form == "10-K":
        return base + (
            "Analyze:\n"
            "1. 📌 Business model & competitive advantage\n"
            "2. ⚠️ Top 3 critical risks\n"
            "3. 💰 Financial highlights (revenue, margins, growth)\n"
            "4. 🔭 Management's 12-month outlook\n"
            "5. 🎯 Investor verdict: BUY / HOLD / CAUTION"
        )
    if form == "10-Q":
        return base + (
            "Analyze:\n"
            "1. 📊 Quarter performance vs prior quarter\n"
            "2. 🔑 3 key management messages\n"
            "3. ⚠️ Notable changes or concerns\n"
            "4. 👀 3 factors to watch next quarter"
        )
    if form in ("4", "144"):
        return base + (
            "Analyze:\n"
            "1. 👤 Who transacted? (name and title)\n"
            "2. 📈 Buy or sell? Quantity and estimated value\n"
            "3. 🔍 Insider sentiment: Bullish / Bearish / Neutral\n"
            "4. 💡 What does this signal for investors?"
        )
    if form in ("SC 13G", "SC 13D"):
        return base + (
            "Analyze:\n"
            "1. 🏦 Who is the major shareholder?\n"
            "2. 📊 Stake size and change\n"
            "3. 🎯 Passive or activist intent?\n"
            "4. 💡 Implications for minority investors"
        )
    if form in ("S-1", "424B4"):
        return base + (
            "Analyze:\n"
            "1. 📌 Business overview & IPO rationale\n"
            "2. 💰 Use of proceeds\n"
            "3. ⚠️ Key risk factors\n"
            "4. 🎯 Investor attractiveness: HIGH / MEDIUM / LOW"
        )
    if form == "DEF 14A":
        return base + (
            "Analyze:\n"
            "1. 🗳️ Key votes on the agenda\n"
            "2. 💼 Executive compensation — reasonable?\n"
            "3. ⚠️ Any controversial proposals?\n"
            "4. 💡 Recommended shareholder stance"
        )
    return base + (
        "Analyze:\n"
        "1. 📣 What happened? (summary)\n"
        "2. 📈 Impact on stock / business\n"
        "3. 🚨 Requires immediate attention?"
    )

# ─── Core scan logic ──────────────────────────────────────
def scan_ticker_forms(ticker: str, forms: list, use_cache: bool, save: bool):
    cfg   = get_cfg()
    cache = load_cache()
    cutoff = datetime.now() - timedelta(days=cfg["days_lookback"])
    found  = []

    set_identity(EDGAR_IDENTITY)
    try:
        company = Company(ticker)
        for form in forms:
            try:
                latest = company.get_filings(form=form).latest(1)
                if not latest: continue
                d = latest.filing_date
                if hasattr(d, "date"): d = d.date()
                ds = str(d)
                if datetime.combine(d, datetime.min.time()) < cutoff: continue
                if use_cache and not is_new(cache, ticker, form, ds): continue
                found.append((form, ds, latest.text()))
                time.sleep(1)
            except Exception as e:
                log.error(f"{ticker} {form}: {e}")
    except Exception as e:
        tg(f"❌ *{ticker}* not found: `{e}`"); return

    if not found:
        tg(f"✅ *{ticker}*: No new filings found.")
        return

    tg(f"📬 *{ticker}* — {len(found)} filing(s) found, analyzing...")
    for form, date, text in found:
        body     = prepare(text, form, cfg["max_chars"])
        analysis = llm(build_prompt(ticker, form, date, body), cfg["model"])
        tg(f"🏢 *{ticker} — {form}*\n📅 {date}\n\n{analysis}\n\n{'─'*28}")
        if save:
            c = load_cache(); mark_done(c, ticker, form, date); save_cache(c)
        time.sleep(5)

# ─── High-level scan commands ─────────────────────────────
def cmd_sec(forms_override=None):
    cfg   = get_cfg()
    lst   = cfg["tickers"]
    forms = forms_override or cfg["default_forms"]
    if not lst: tg("ℹ️ Watchlist is empty. Add tickers with `/addticker AAPL`."); return
    tg("🔍 *SEC scan started...*\n`" + "  ".join(lst) + "`\n"
       f"Forms: `{'  '.join(forms)}`")
    for ticker in lst:
        scan_ticker_forms(ticker, forms, use_cache=True, save=True)
        time.sleep(2)
    tg("✅ *Scan complete.*\n_Not investment advice._")

def cmd_insider():
    cfg = get_cfg()
    lst = cfg["tickers"]
    if not lst: tg("ℹ️ Watchlist is empty."); return
    tg("🔍 *Insider scan started...*")
    for ticker in lst:
        scan_ticker_forms(ticker, ["4"], use_cache=True, save=True)
        time.sleep(2)
    tg("✅ *Insider scan complete.*")

def cmd_scanticker(parts: list):
    # /scanticker AAPL [form1 form2 ...]
    if len(parts) < 2:
        tg("Usage: `/scanticker AAPL` or `/scanticker AAPL 10-K 4`"); return
    ticker = parts[1].upper()
    if len(parts) >= 3:
        raw_forms = [p.upper() for p in parts[2:]]
        forms = []
        for f in raw_forms:
            matched = next((k for k in FORMS if k.upper() == f or k.replace(" ","").upper() == f.replace(" ","")), None)
            if matched: forms.append(matched)
            else: tg(f"⚠️ Unknown form `{f}` — skipped.")
        if not forms: tg("No valid forms provided."); return
    else:
        forms = get_cfg()["default_forms"]
    tg(f"🔍 On-demand scan: *{ticker}* | Forms: `{'  '.join(forms)}`\n_(not added to watchlist)_")
    scan_ticker_forms(ticker, forms, use_cache=False, save=False)
    tg(f"✅ *{ticker}* on-demand scan complete.\n_Not investment advice._")

# ─── Ticker management ────────────────────────────────────
def cmd_addticker(parts: list) -> str:
    if len(parts) < 2: return "Usage: `/addticker AAPL` or `/addticker AAPL MSFT NVDA`"
    cfg = get_cfg()
    added, already = [], []
    for raw in parts[1:]:
        t = raw.upper().strip()
        if t in cfg["tickers"]: already.append(t)
        else: cfg["tickers"].append(t); added.append(t)
    save_cfg(cfg)
    lines = []
    if added:   lines.append("✅ Added: " + "  ".join(f"`{t}`" for t in added))
    if already: lines.append("ℹ️ Already tracked: " + "  ".join(f"`{t}`" for t in already))
    return "\n".join(lines)

def cmd_removeticker(parts: list) -> str:
    if len(parts) < 2: return "Usage: `/removeticker AAPL`"
    cfg = get_cfg()
    removed, notfound = [], []
    for raw in parts[1:]:
        t = raw.upper().strip()
        if t in cfg["tickers"]: cfg["tickers"].remove(t); removed.append(t)
        else: notfound.append(t)
    save_cfg(cfg)
    lines = []
    if removed:  lines.append("🗑 Removed: " + "  ".join(f"`{t}`" for t in removed))
    if notfound: lines.append("ℹ️ Not found: " + "  ".join(f"`{t}`" for t in notfound))
    return "\n".join(lines)

def cmd_listtickers() -> str:
    cfg = get_cfg()
    lst = cfg["tickers"]
    if not lst: return "📋 Watchlist is empty.\nAdd with `/addticker AAPL`"
    return "📋 *Watchlist (" + str(len(lst)) + " tickers)*\n" + \
           "\n".join(f"  • `{t}`" for t in lst)

# ─── Form management ──────────────────────────────────────
def cmd_listforms() -> str:
    cfg     = get_cfg()
    active  = cfg["default_forms"]
    cats = [
        ("📄 Periodic Reports", ["10-K","10-Q","8-K"]),
        ("🔐 Insider & Ownership", ["4","144","SC 13G","SC 13D"]),
        ("🚀 Offerings", ["S-1","424B4"]),
        ("🌍 Foreign Issuers", ["20-F","6-K"]),
        ("📜 Other", ["DEF 14A","11-K"]),
    ]
    lines = ["📋 *Supported Form Types*\n"]
    for cat, flist in cats:
        lines.append(f"*{cat}*")
        for f in flist:
            mark = "✅" if f in active else "  "
            lines.append(f"  {mark} `{f}` — {FORMS[f]}")
        lines.append("")
    lines.append(f"*Active in default scan:* `{'  '.join(active)}`")
    lines.append("\n`/addform 10-K` · `/removeform 8-K`")
    return "\n".join(lines)

def cmd_addform(parts: list) -> str:
    if len(parts) < 2: return "Usage: `/addform SC 13G` or `/addform S-1`"
    raw   = " ".join(parts[1:]).upper()
    match = next((k for k in FORMS if k.upper() == raw or k.replace(" ","").upper() == raw.replace(" ","")), None)
    if not match: return f"❌ Unknown form `{raw}`.\nSend `/listforms` to see options."
    cfg = get_cfg()
    if match in cfg["default_forms"]: return f"ℹ️ `{match}` is already in default forms."
    cfg["default_forms"].append(match); save_cfg(cfg)
    return f"✅ `{match}` added to default scan forms."

def cmd_removeform(parts: list) -> str:
    if len(parts) < 2: return "Usage: `/removeform 8-K`"
    raw   = " ".join(parts[1:]).upper()
    match = next((k for k in FORMS if k.upper() == raw or k.replace(" ","").upper() == raw.replace(" ","")), None)
    if not match: return f"❌ Unknown form `{raw}`."
    cfg = get_cfg()
    if match not in cfg["default_forms"]: return f"ℹ️ `{match}` is not in default forms."
    cfg["default_forms"].remove(match); save_cfg(cfg)
    return f"🗑 `{match}` removed from default scan forms."

# ─── Settings ─────────────────────────────────────────────
def cmd_settings() -> str:
    cfg = get_cfg()
    return (
        "⚙️ *Current Settings*\n\n"
        f"🤖 Model      : `{cfg['model']}`\n"
        f"📅 Lookback   : `{cfg['days_lookback']} days`\n"
        f"📝 Max chars  : `{cfg['max_chars']}`\n"
        f"📋 Forms      : `{'  '.join(cfg['default_forms'])}`\n"
        f"🏢 Tickers    : `{len(cfg['tickers'])} ticker(s)`"
        + (f" — `{'  '.join(cfg['tickers'])}`" if cfg["tickers"] else "")
        + "\n\n"
        "`/setmodel` · `/setlookback` · `/setchars`"
    )

def cmd_setmodel(parts: list) -> str:
    if len(parts) < 2:
        return (
            "Usage: `/setmodel <model>`\n\nFree models:\n"
            "`meta-llama/llama-3.3-70b-instruct:free`\n"
            "`google/gemma-3-27b-it:free`\n"
            "`deepseek/deepseek-chat-v3-0324:free`"
        )
    model = " ".join(parts[1:])
    update_cfg(model=model)
    return f"✅ Model set to:\n`{model}`"

def cmd_setlookback(parts: list) -> str:
    if len(parts) < 2: return "Usage: `/setlookback 60`"
    try:
        n = int(parts[1])
        if not 1 <= n <= 365: raise ValueError
        update_cfg(days_lookback=n)
        return f"✅ Lookback period set to `{n} days`."
    except ValueError:
        return "❌ Please provide a number between 1 and 365."

def cmd_setchars(parts: list) -> str:
    if len(parts) < 2: return "Usage: `/setchars 15000`"
    try:
        n = int(parts[1])
        if not 1000 <= n <= 50000: raise ValueError
        update_cfg(max_chars=n)
        return f"✅ Max chars set to `{n}`."
    except ValueError:
        return "❌ Please provide a number between 1000 and 50000."

# ─── First-run wizard ─────────────────────────────────────
WIZARD: dict = {}  # in-memory wizard state

FORMS_MENU = (
    "📋 *Step 1 / 2 — Choose default form types*\n\n"
    "*Periodic Reports*\n"
    "  `10-K`    Annual report\n"
    "  `10-Q`    Quarterly report\n"
    "  `8-K`     Current events\n\n"
    "*Insider & Ownership*\n"
    "  `4`       Insider buy/sell\n"
    "  `144`     Restricted stock sale\n"
    "  `SC 13G`  Passive major shareholder\n"
    "  `SC 13D`  Active major shareholder\n\n"
    "*Offerings*\n"
    "  `S-1`     IPO registration\n"
    "  `424B4`   Prospectus\n\n"
    "*Foreign Issuers*\n"
    "  `20-F`    Foreign annual\n"
    "  `6-K`     Foreign current\n\n"
    "*Other*\n"
    "  `DEF 14A` Proxy statement\n"
    "  `11-K`    Retirement plan\n\n"
    f"Default recommendation: `10-K  10-Q  8-K  4`\n\n"
    "✅ `/usedefaults` — use recommended\n"
    "✏️ `/setforms 10-K 10-Q 8-K 4 SC 13G` — custom selection"
)

TICKERS_MENU = (
    "📋 *Step 2 / 2 — Add your first tickers*\n\n"
    "Examples:\n"
    "`/addticker AAPL`\n"
    "`/addticker MU GOOG ASML NVDA`\n\n"
    "⏭ `/skip` — skip for now (add later anytime)"
)

def start_wizard():
    WIZARD["step"] = "forms"
    tg(
        "👋 *Welcome to SEC Analyzer Bot!*\n\n"
        "No configuration found — let's set things up.\n"
        "This takes about 1 minute.\n\n"
        + FORMS_MENU
    )

def wizard_handle(text: str, parts: list) -> bool:
    """Returns True if input was consumed by the wizard."""
    step = WIZARD.get("step")
    if not step:
        return False

    if step == "forms":
        if text == "/usedefaults":
            update_cfg(default_forms=DEFAULT_FORMS, first_run=False)
            WIZARD["step"] = "tickers"
            tg(f"✅ Default forms set: `{'  '.join(DEFAULT_FORMS)}`\n\n" + TICKERS_MENU)
            return True
        if parts[0].lower() == "/setforms" and len(parts) >= 2:
            raw   = [p.upper() for p in parts[1:]]
            valid = []
            for f in raw:
                m = next((k for k in FORMS if k.upper() == f or k.replace(" ","").upper() == f.replace(" ","")), None)
                if m: valid.append(m)
                else: tg(f"⚠️ Unknown form `{f}` — skipped.")
            if not valid:
                tg("❌ No valid forms. " + FORMS_MENU); return True
            update_cfg(default_forms=valid, first_run=False)
            WIZARD["step"] = "tickers"
            tg(f"✅ Forms set: `{'  '.join(valid)}`\n\n" + TICKERS_MENU)
            return True
        tg("Please use `/usedefaults` or `/setforms 10-K 10-Q 8-K 4`")
        return True

    if step == "tickers":
        if text == "/skip":
            WIZARD.pop("step", None)
            tg(
                "✅ *Setup complete!*\n\n"
                "Add tickers anytime with `/addticker AAPL`\n"
                "Send `/help` to see all commands."
            )
            return True
        if parts[0].lower() == "/addticker" and len(parts) >= 2:
            result = cmd_addticker(parts)
            WIZARD.pop("step", None)
            tg(result + "\n\n✅ *Setup complete!*\nSend `/help` to see all commands.")
            return True
        tg("Use `/addticker AAPL` or `/skip`")
        return True

    return False

# ─── Help ─────────────────────────────────────────────────
def help_msg() -> str:
    cfg = get_cfg()
    return (
        "🤖 *SEC Analyzer Bot*\n\n"
        "*Scans:*\n"
        "`Any news?` · `Check` · `/sec` → full watchlist scan\n"
        "`Insider` · `/insider` → Form 4 only\n"
        "`Check all` · `/all` → SEC + Insider\n\n"
        "*On-demand:*\n"
        "`/scanticker AAPL` → scan, no watchlist add\n"
        "`/scanticker AAPL 10-K 4` → specific forms\n\n"
        "*Tickers:*\n"
        "`/addticker AAPL MSFT` · `/removeticker AAPL`\n"
        "`/listtickers`\n\n"
        "*Forms:*\n"
        "`/listforms` → all supported + active\n"
        "`/addform SC 13G` · `/removeform 8-K`\n\n"
        "*Settings:*\n"
        "`/settings`\n"
        "`/setmodel <model>` · `/setlookback 60` · `/setchars 15000`\n\n"
        f"*Active forms:* `{'  '.join(cfg['default_forms'])}`\n"
        f"*Tickers:* `{len(cfg['tickers'])}` tracked"
    )

# ─── Main loop ────────────────────────────────────────────
def main():
    log.info("Bot started.")
    cfg = get_cfg()
    if cfg.get("first_run", True):
        start_wizard()
    else:
        tg("🤖 *SEC Analyzer is online.*\nSend `/help` for commands.")

    offset = 0
    while True:
        updates = get_updates(offset)
        for upd in updates:
            offset = upd["update_id"] + 1
            msg     = upd.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            raw     = msg.get("text", "").strip()
            text    = raw.lower()
            parts   = raw.split()
            cmd     = parts[0].lower() if parts else ""

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if wizard_handle(text, parts):
                continue

            # ── Ticker management ──
            if cmd == "/addticker":
                tg(cmd_addticker(parts))
            elif cmd == "/removeticker":
                tg(cmd_removeticker(parts))
            elif cmd == "/listtickers":
                tg(cmd_listtickers())

            # ── Form management ──
            elif cmd == "/listforms":
                tg(cmd_listforms())
            elif cmd == "/addform":
                tg(cmd_addform(parts))
            elif cmd == "/removeform":
                tg(cmd_removeform(parts))

            # ── Settings ──
            elif cmd == "/settings":
                tg(cmd_settings())
            elif cmd == "/setmodel":
                tg(cmd_setmodel(parts))
            elif cmd == "/setlookback":
                tg(cmd_setlookback(parts))
            elif cmd == "/setchars":
                tg(cmd_setchars(parts))

            # ── On-demand scan ──
            elif cmd == "/scanticker":
                cmd_scanticker(parts)

            # ── Help ──
            elif text in ["/start", "/help"]:
                tg(help_msg())

            # ── Scans ──
            elif any(t in text for t in ["check all", "scan all", "everything", "/all"]):
                cmd_sec(); cmd_insider()
            elif any(t in text for t in ["insider", "form4", "/insider", "/form4"]):
                cmd_insider()
            elif any(t in text for t in ["any news", "check", "scan", "sec", "filings", "/sec", "/check", "/scan"]):
                cmd_sec()

        time.sleep(1)

if __name__ == "__main__":
    main()
