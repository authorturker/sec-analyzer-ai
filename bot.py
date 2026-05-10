"""
SEC Analyzer AI — Telegram
• SEC Filings : 10-K, 10-Q, 8-K
• Insider Trading : Form 4
• LLM : OpenRouter API
"""

import time, json, logging, hashlib
from datetime import datetime, timedelta
from pathlib import Path

import requests
from edgar import Company, set_identity

from config import (
    EDGAR_IDENTITY, OPENROUTER_API_KEY, OPENROUTER_MODEL,
    TICKERS, FILING_TYPES, DAYS_LOOKBACK,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    OUTPUT_DIR, CACHE_FILE, MAX_CHARS,
)

# ─── Logging ──────────────────────────────────────────────
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(f"{OUTPUT_DIR}/bot.log"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

# ─── Command matching ─────────────────────────────────────
CMD_SEC     = ["sec", "any news", "check", "scan", "filings",
               "/sec", "/check", "/scan"]
CMD_INSIDER = ["insider", "form4", "form 4", "/insider", "/form4"]
CMD_ALL     = ["check all", "scan all", "everything", "/all"]
CMD_HELP    = ["/start", "/help"]

# ─── Cache ────────────────────────────────────────────────
def load_cache() -> dict:
    try:
        return json.loads(Path(CACHE_FILE).read_text())
    except Exception:
        return {}

def save_cache(cache: dict):
    Path(CACHE_FILE).write_text(json.dumps(cache, indent=2))

def cache_key(*args) -> str:
    return hashlib.md5("_".join(str(a) for a in args).encode()).hexdigest()

def is_new(cache, *args) -> bool:
    return cache_key(*args) not in cache

def mark_done(cache, *args):
    cache[cache_key(*args)] = {"analyzed_at": datetime.now().isoformat()}

# ─── Telegram ─────────────────────────────────────────────
_TG_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

def tg_send(text: str):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 4000:
            chunks.append(cur)
            cur = line
        else:
            cur += "\n" + line
    if cur.strip():
        chunks.append(cur)
    for chunk in chunks:
        try:
            requests.post(f"{_TG_URL}/sendMessage", json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": chunk.strip(),
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
            }, timeout=15)
            time.sleep(0.3)
        except Exception as e:
            log.error(f"Telegram send error: {e}")

def get_updates(offset: int) -> list:
    try:
        r = requests.get(f"{_TG_URL}/getUpdates",
                         params={"offset": offset, "timeout": 30}, timeout=35)
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception as e:
        log.error(f"getUpdates error: {e}")
        return []

# ─── OpenRouter API ───────────────────────────────────────
_OR_URL = "https://openrouter.ai/api/v1/chat/completions"
_OR_HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": "https://github.com/authorturker/sec-analyzer-ai/",
    "X-Title": "SEC Analyzer AI",
}
_SYSTEM = (
    "You are an experienced financial analyst specializing in SEC filings. "
    "Analyze documents from an investor's perspective. Be concise, structured, "
    "and use bullet points. Highlight key risks and opportunities. Use emojis "
    "to improve readability."
)

def llm(prompt: str) -> str:
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": 1200,
        "temperature": 0.2,
    }
    for attempt in range(3):
        try:
            r = requests.post(_OR_URL, headers=_OR_HEADERS, json=payload, timeout=120)
            if r.status_code == 429:
                wait = 60 * (attempt + 1)
                log.warning(f"Rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            log.error(f"OpenRouter error (attempt {attempt + 1}): {e}")
            time.sleep(10)
    return "⚠️ Analysis unavailable — API did not respond."

# ─── Text extraction ──────────────────────────────────────
_SECTIONS = {
    "10-K": ["item 1.", "item 1a.", "item 7.", "item 8."],
    "10-Q": ["item 1.", "item 2.", "item 3."],
    "8-K":  [],
}

def prepare(text: str, form: str) -> str:
    keywords = _SECTIONS.get(form, [])
    if not keywords:
        return text[:MAX_CHARS]
    lines, capturing, chars, out = text.split("\n"), False, 0, []
    for line in lines:
        if any(line.lower().strip().startswith(k) for k in keywords):
            capturing = True
        if capturing:
            out.append(line)
            chars += len(line)
            if chars >= MAX_CHARS:
                out.append("[text truncated]")
                break
    return "\n".join(out) if out else text[:MAX_CHARS]

# ─── SEC analysis prompts ─────────────────────────────────
def _prompt(ticker: str, form: str, date: str, body: str) -> str:
    base = f"{ticker} — {form} ({date})\n\n{body}\n\n"
    if form == "10-K":
        return base + (
            "Analyze the following:\n"
            "1. 📌 Business model & competitive advantage\n"
            "2. ⚠️ Top 3 critical risks\n"
            "3. 💰 Financial highlights (revenue, margins, growth)\n"
            "4. 🔭 Management's 12-month outlook\n"
            "5. 🎯 Investor verdict: BUY / HOLD / CAUTION"
        )
    if form == "10-Q":
        return base + (
            "Analyze the following:\n"
            "1. 📊 Quarter performance vs prior quarter\n"
            "2. 🔑 3 key messages from management\n"
            "3. ⚠️ Notable changes or concerns\n"
            "4. 👀 3 factors to watch next quarter"
        )
    return base + (
        "Analyze the following:\n"
        "1. 📣 What happened? (summary)\n"
        "2. 📈 Impact on stock / business\n"
        "3. 🚨 Does this require immediate attention?"
    )

# ─── SEC scan ─────────────────────────────────────────────
def run_sec():
    set_identity(EDGAR_IDENTITY)
    cache = load_cache()
    cutoff = datetime.now() - timedelta(days=DAYS_LOOKBACK)
    found = []

    tg_send("🔍 *SEC scan started...*\n`" + "  ".join(TICKERS) + "`")

    for ticker in TICKERS:
        try:
            company = Company(ticker)
            for form in FILING_TYPES:
                try:
                    latest = company.get_filings(form=form).latest(1)
                    if not latest:
                        continue
                    d = latest.filing_date
                    if hasattr(d, "date"):
                        d = d.date()
                    date_str = str(d)
                    if datetime.combine(d, datetime.min.time()) < cutoff:
                        continue
                    if not is_new(cache, ticker, form, date_str):
                        continue
                    log.info(f"New: {ticker} {form} {date_str}")
                    found.append((ticker, form, date_str, latest.text()))
                    time.sleep(1)
                except Exception as e:
                    log.error(f"{ticker} {form}: {e}")
        except Exception as e:
            log.error(f"{ticker}: {e}")

    if not found:
        tg_send("✅ *SEC: No new filings found.*")
        return

    tg_send(f"📬 *{len(found)} new filing(s) found — analyzing...*")

    for ticker, form, date, text in found:
        body = prepare(text, form)
        analysis = llm(_prompt(ticker, form, date, body))
        tg_send(
            f"🏢 *{ticker} — {form}*\n"
            f"📅 {date}\n\n"
            f"{analysis}\n\n{'─'*28}"
        )
        cache = load_cache()
        mark_done(cache, ticker, form, date)
        save_cache(cache)
        time.sleep(5)  # rate limit buffer between filings

    tg_send("✅ *SEC analysis complete.*\n_This is not investment advice._")

# ─── Insider trading (Form 4) ─────────────────────────────
def run_insider():
    set_identity(EDGAR_IDENTITY)
    cache = load_cache()
    cutoff = datetime.now() - timedelta(days=30)

    tg_send("🔍 *Insider trading scan started...*")

    by_ticker: dict = {}
    for ticker in TICKERS:
        try:
            company = Company(ticker)
            filings = company.get_filings(form="4").latest(5)
            if not filings:
                continue
            filing_list = list(filings) if hasattr(filings, "__iter__") else [filings]
            for f in filing_list:
                d = f.filing_date
                if hasattr(d, "date"):
                    d = d.date()
                date_str = str(d)
                if datetime.combine(d, datetime.min.time()) < cutoff:
                    continue
                if not is_new(cache, ticker, "form4", date_str):
                    continue
                by_ticker.setdefault(ticker, []).append((date_str, f.text()[:6000]))
                time.sleep(0.5)
        except Exception as e:
            log.error(f"Insider {ticker}: {e}")

    if not by_ticker:
        tg_send("✅ *Insider: No new Form 4 filings in the last 30 days.*")
        return

    total = sum(len(v) for v in by_ticker.values())
    tg_send(f"📬 *{total} new Form 4 filing(s) — analyzing...*")

    for ticker, items in by_ticker.items():
        combined = "\n\n---\n\n".join(f"[{d}]\n{t}" for d, t in items)
        analysis = llm(
            f"{ticker} — Form 4 Insider Transactions (Last 30 days)\n\n"
            f"{combined[:12000]}\n\n"
            "Analyze the following:\n"
            "1. 👤 Who transacted? (name and title)\n"
            "2. 📈 Buy or sell? Quantity and estimated value\n"
            "3. 🔍 Overall insider sentiment: Bullish / Bearish / Neutral\n"
            "4. 💡 What does this signal for investors?"
        )
        tg_send(
            f"🔐 *{ticker} — Insider Trading*\n\n"
            f"{analysis}\n\n{'─'*28}"
        )
        cache = load_cache()
        for date_str, _ in items:
            mark_done(cache, ticker, "form4", date_str)
        save_cache(cache)
        time.sleep(5)

    tg_send("✅ *Insider analysis complete.*")

# ─── Help ─────────────────────────────────────────────────
def help_msg() -> str:
    return (
        "🤖 *SEC Analyzer AI*\n\n"
        "*SEC Filings:*\n"
        "`Any news?` · `Check` · `Scan` · `/sec`\n"
        "→ Scans 10-K, 10-Q, 8-K\n\n"
        "*Insider Trading:*\n"
        "`Insider` · `/insider`\n"
        "→ Analyzes Form 4 transactions\n\n"
        "*Everything:*\n"
        "`Check all` · `/all`\n"
        "→ SEC filings + Insider combined\n\n"
        f"*Tracked tickers:*\n`{'  '.join(TICKERS)}`"
    )

# ─── Main loop ────────────────────────────────────────────
def main():
    log.info("Bot started.")
    tg_send("🤖 *SEC Analyzer is online.*\nSend `/help` for available commands.")

    offset = 0
    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "").lower().strip()

            if chat_id != str(TELEGRAM_CHAT_ID):
                continue

            if text in CMD_HELP:
                tg_send(help_msg())
            elif any(t in text for t in CMD_ALL):
                run_sec()
                run_insider()
            elif any(t in text for t in CMD_INSIDER):
                run_insider()
            elif any(t in text for t in CMD_SEC):
                run_sec()

        time.sleep(1)

if __name__ == "__main__":
    main()
