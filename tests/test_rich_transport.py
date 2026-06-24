"""
O1 — Rich message transport foundation (Bot API 10.1 / Wave O).

Covers the pure classifier, the _rich_enabled gate, the _tg_send_rich_to
transport primitive, the rich_md threading into _tg_to / _tg_with_keyboard_to,
the toggle command, and — most importantly — the regression guard that proves
rich_md=None NEVER calls sendRichMessage (legacy path byte-identical).

All offline; requests/time mocked.
"""
import pytest


class FakeResp:
    def __init__(self, status=200, body="", headers=None):
        self.status_code = status
        self.ok = (200 <= status < 300)
        self.text = body
        self.headers = headers or {}

    def raise_for_status(self):
        if not self.ok:
            raise Exception(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _reset_cap(bot):
    """Each test starts with an unknown capability flag."""
    bot._RICH_CAP = None
    yield
    bot._RICH_CAP = None


# ─── _classify_rich_error matrix ──────────────────────────
class TestClassifyRichError:
    def test_404_unsupported(self, bot):
        assert bot._classify_rich_error(404, "") == "unsupported"

    def test_method_not_found_body_unsupported(self, bot):
        assert bot._classify_rich_error(400, "Bad Request: method not found") == "unsupported"

    def test_unknown_method_body_unsupported(self, bot):
        assert bot._classify_rich_error(400, "unknown method: sendRichMessage") == "unsupported"

    def test_400_content_error(self, bot):
        assert bot._classify_rich_error(400, "can't parse entities") == "content"

    def test_429_transient(self, bot):
        assert bot._classify_rich_error(429, "Too Many Requests") == "transient"

    def test_500_transient(self, bot):
        assert bot._classify_rich_error(500, "") == "transient"

    def test_502_transient(self, bot):
        assert bot._classify_rich_error(502, "Bad Gateway") == "transient"

    def test_503_transient(self, bot):
        assert bot._classify_rich_error(503, "") == "transient"

    def test_network_empty_body_transient(self, bot):
        assert bot._classify_rich_error(0, "") == "transient"


# ─── _rich_enabled matrix ─────────────────────────────────
class TestRichEnabled:
    def _set_pref(self, bot, monkeypatch, value):
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {"rich_format": value})

    def test_true_cap_none(self, bot, monkeypatch):
        bot._RICH_CAP = None
        self._set_pref(bot, monkeypatch, True)
        assert bot._rich_enabled("0") is True

    def test_true_cap_true(self, bot, monkeypatch):
        bot._RICH_CAP = True
        self._set_pref(bot, monkeypatch, True)
        assert bot._rich_enabled("0") is True

    def test_true_cap_false(self, bot, monkeypatch):
        bot._RICH_CAP = False
        self._set_pref(bot, monkeypatch, True)
        assert bot._rich_enabled("0") is False

    def test_false_pref_cap_none(self, bot, monkeypatch):
        bot._RICH_CAP = None
        self._set_pref(bot, monkeypatch, False)
        assert bot._rich_enabled("0") is False

    def test_false_pref_cap_true(self, bot, monkeypatch):
        bot._RICH_CAP = True
        self._set_pref(bot, monkeypatch, False)
        assert bot._rich_enabled("0") is False


# ─── _tg_send_rich_to behavior ────────────────────────────
class TestTgSendRichTo:
    def test_success_sets_cap_true(self, bot, monkeypatch):
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: FakeResp(200))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        assert bot._tg_send_rich_to("0", "# Hi") is True
        assert bot._RICH_CAP is True

    def test_cap_false_short_circuits_no_http(self, bot, monkeypatch):
        bot._RICH_CAP = False
        called = []
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: called.append(1) or FakeResp(200))
        assert bot._tg_send_rich_to("0", "# Hi") is False
        assert called == []   # no HTTP attempted

    def test_unsupported_sets_cap_false(self, bot, monkeypatch):
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: FakeResp(404))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        assert bot._tg_send_rich_to("0", "# Hi") is False
        assert bot._RICH_CAP is False

    def test_content_error_returns_false_cap_unchanged(self, bot, monkeypatch):
        bot._RICH_CAP = None
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: FakeResp(400, "can't parse entities"))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        assert bot._tg_send_rich_to("0", "# Hi") is False
        assert bot._RICH_CAP is None   # content error does not poison cap

    def test_transient_then_success(self, bot, monkeypatch):
        seq = [FakeResp(500), FakeResp(200)]
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: seq.pop(0))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        assert bot._tg_send_rich_to("0", "# Hi") is True

    def test_never_raises_on_exception(self, bot, monkeypatch):
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(Exception("net")))
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        assert bot._tg_send_rich_to("0", "# Hi") is False   # no raise


# ─── REGRESSION GUARD — rich_md=None never calls sendRichMessage ─
class TestRegressionGuardNoRichWhenNone:
    def test_tg_to_none_never_calls_send_rich(self, bot, monkeypatch):
        urls = []
        def fake_post(url, json=None, **k):
            urls.append(url)
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot._tg_to("0", "*bold legacy*")           # rich_md defaults to None
        assert urls, "legacy path must still send"
        assert all("sendRichMessage" not in u for u in urls)
        assert bot._RICH_CAP is None               # cap untouched

    def test_tg_with_keyboard_none_never_calls_send_rich(self, bot, monkeypatch):
        urls = []
        def fake_post(url, json=None, **k):
            urls.append(url)
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot._tg_with_keyboard_to("0", "*bold legacy*", {"inline_keyboard": [[]]})
        assert urls, "legacy path must still send"
        assert all("sendRichMessage" not in u for u in urls)
        assert bot._RICH_CAP is None


# ─── Fallback decision: rich provided but fails → legacy still sends ─
class TestFallbackDecision:
    def test_rich_md_with_cap_false_falls_back_to_legacy(self, bot, monkeypatch):
        bot._RICH_CAP = False                       # rich known-unsupported
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {"rich_format": True})
        urls = []
        def fake_post(url, json=None, **k):
            urls.append(url)
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot._tg_to("0", "legacy text", rich_md="# rich")
        # rich skipped (cap False); legacy sendMessage delivered the message
        assert any("sendMessage" in u for u in urls)
        assert all("sendRichMessage" not in u for u in urls)

    def test_rich_content_failure_falls_back_to_legacy(self, bot, monkeypatch):
        bot._RICH_CAP = None
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {"rich_format": True})
        urls = []
        def fake_post(url, json=None, **k):
            urls.append(url)
            if "sendRichMessage" in url:
                return FakeResp(400, "can't parse entities")   # content error
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot._tg_to("0", "legacy text", rich_md="# rich")
        assert any("sendRichMessage" in u for u in urls)   # rich was tried
        assert any("sendMessage" in u and "sendRichMessage" not in u
                   for u in urls)                          # legacy fallback ran

    def test_rich_md_success_skips_legacy(self, bot, monkeypatch):
        bot._RICH_CAP = None
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {"rich_format": True})
        urls = []
        def fake_post(url, json=None, **k):
            urls.append(url)
            return FakeResp(200)
        monkeypatch.setattr(bot.requests, "post", fake_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
        bot._tg_to("0", "legacy text", rich_md="# rich")
        assert urls == [f"{bot._TG}/sendRichMessage"]   # rich succeeded, no legacy


# ─── Toggle command branches ──────────────────────────────
class TestCmdSetRich:
    def _mock_mutate(self, bot, monkeypatch):
        captured = {}
        def fake_mutate(fn):
            c = {}
            fn(c)
            captured.update(c)
            return c
        monkeypatch.setattr(bot, "mutate_chat_cfg", fake_mutate)
        return captured

    def test_off_disables(self, bot, monkeypatch):
        captured = self._mock_mutate(bot, monkeypatch)
        bot._current_lang = "en"
        out = bot.cmd_setrich(["/setrich", "off"])
        assert captured.get("rich_format") is False
        assert out == bot.t("rich_disabled")

    def test_on_enables(self, bot, monkeypatch):
        captured = self._mock_mutate(bot, monkeypatch)
        bot._current_lang = "en"
        out = bot.cmd_setrich(["/setrich", "on"])
        assert captured.get("rich_format") is True
        assert out == bot.t("rich_enabled")

    def test_default_enables(self, bot, monkeypatch):
        captured = self._mock_mutate(bot, monkeypatch)
        out = bot.cmd_setrich(["/setrich"])
        assert captured.get("rich_format") is True


# ─── Config defaults present ──────────────────────────────
class TestConfigDefaults:
    def test_rich_format_in_cfg_defaults(self, bot):
        assert bot._CFG_DEFAULTS.get("rich_format") is True

    def test_rich_format_in_chat_defaults(self, bot):
        assert bot._CHAT_DEFAULTS.get("rich_format") is True

    def test_rich_format_in_per_user_keys(self, bot):
        assert "rich_format" in bot._CHAT_PER_USER_KEYS


# ─── Lang parity for the new keys ─────────────────────────
class TestRichLangKeys:
    NEW_KEYS = {"rich_enabled", "rich_disabled", "rich_unsupported",
                "rich_test_sample", "rich_settings_line", "cmd_desc_setrich",
                "pnl_rich_header", "pnl_rich_total",
                "filing_rich_header", "filing_rich_risk_header",
                "filing_rich_risk_summary", "filing_thinking_draft",
                "dailynews_rich_header", "dailynews_rich_ticker",
                "dailynews_rich_item",
                "digest_rich_title", "digest_rich_ticker", "digest_rich_item",
                "digest_rich_pnl_line", "digest_rich_movers_line",
                "compare_rich_header", "compare_rich_metrics_header",
                "checknews_rich_header", "checknews_rich_item",
                "status_rich",
                "watchword_alert_rich_header", "watchword_alert_rich_hit",
                "watchword_alert_rich_more",
                "sheet_rich_header", "sheet_rich_section"}

    def test_keys_present_both_langs(self, bot):
        en = set(bot._load_lang("en"))
        tr = set(bot._load_lang("tr"))
        assert self.NEW_KEYS <= en
        assert self.NEW_KEYS <= tr

    def test_parity(self, bot):
        assert set(bot._load_lang("en")) == set(bot._load_lang("tr"))


# ═══════════════════════════════════════════════════════════
# O2 — Rich P&L table (_pnl_rich_md + cmd_pnl dual-render)
# ═══════════════════════════════════════════════════════════

def _make_row(ticker, qty, last, avg_cost=100.0):
    """Build a compute_pnl_rows-compatible dict (no IO)."""
    value = qty * last if last is not None else None
    pnl_usd = (last - avg_cost) * qty if last is not None else None
    pnl_pct = (last - avg_cost) / avg_cost * 100.0 if last is not None else None
    return {
        "ticker": ticker, "qty": qty, "last": last,
        "avg_cost": avg_cost, "value": value,
        "pnl_usd": pnl_usd, "pnl_pct": pnl_pct,
    }


class TestPnlRichMd:
    def test_empty_rows_returns_empty(self, bot):
        assert bot._pnl_rich_md([]) == ""

    def test_gfm_structure(self, bot):
        rows = [_make_row("AAPL", 10, 150.0, 120.0)]
        out = bot._pnl_rich_md(rows)
        assert out.startswith("### ")
        assert "| Ticker | Qty | Last | Value | P&L% |" in out
        assert "| --- | ---: | ---: | ---: | ---: |" in out
        assert "| AAPL | 10 | $150.00 | $1,500 | +25.0% |" in out

    def test_two_rows(self, bot):
        rows = [_make_row("AAPL", 10, 150.0, 120.0),
                _make_row("MSFT", 5, 300.0, 280.0)]
        out = bot._pnl_rich_md(rows)
        assert "| AAPL |" in out
        assert "| MSFT |" in out
        assert "**Toplam:**" in out or "**Total:**" in out

    def test_na_row(self, bot):
        rows = [_make_row("AAPL", 10, 150.0, 120.0),
                _make_row("TSLA", 5, None)]
        out = bot._pnl_rich_md(rows)
        assert "| TSLA | 5 | n/a | n/a | n/a |" in out
        assert "AAPL" in out

    def test_all_na(self, bot):
        rows = [_make_row("TSLA", 5, None), _make_row("NFLX", 3, None)]
        out = bot._pnl_rich_md(rows)
        assert "| --- |" in out
        assert "**Toplam:**" not in out and "**Total:**" not in out

    def test_no_single_star_bold(self, bot):
        rows = [_make_row("AAPL", 10, 150.0, 120.0)]
        out = bot._pnl_rich_md(rows)
        import re
        singles = re.findall(r'(?<!\*)\*(?!\*)', out)
        assert singles == [], f"Legacy *bold* found: {singles}"

    def test_large_qty_no_overflow(self, bot):
        rows = [_make_row("AAPL", 1234567, 150.0, 120.0)]
        out = bot._pnl_rich_md(rows)
        assert "1234567" in out


class TestCmdPnlDualRender:
    def test_rich_md_passed_when_enabled(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", True)
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {
            "portfolio": [{"ticker": "AAPL", "qty": 10, "avg_cost": 120.0}],
            "rich_format": True,
        })
        monkeypatch.setattr(bot, "aggregate_positions", lambda lots: {"AAPL": lots[0]})
        monkeypatch.setattr(bot, "fetch_last_close", lambda t: 150.0)
        monkeypatch.setattr(bot, "time", type("T", (), {"sleep": staticmethod(lambda *a: None)})())
        monkeypatch.setattr(bot, "compute_pnl_rows", lambda agg, prices: [
            _make_row("AAPL", 10, 150.0, 120.0)])
        monkeypatch.setattr(bot, "maybe_snapshot_portfolio_value", lambda *a: None)
        captured = {}
        def fake_tg(text, rich_md=None):
            captured["text"] = text
            captured["rich_md"] = rich_md
        monkeypatch.setattr(bot, "tg", fake_tg)
        bot.cmd_pnl()
        assert captured.get("rich_md") is not None
        assert "**Toplam:**" in captured["rich_md"] or "**Total:**" in captured["rich_md"]

    def test_empty_portfolio_calls_tg_no_rich(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", True)
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {
            "portfolio": [],
            "rich_format": True,
        })
        captured = {}
        def fake_tg(text, rich_md=None):
            captured["text"] = text
            captured["rich_md"] = rich_md
        monkeypatch.setattr(bot, "tg", fake_tg)
        result = bot.cmd_pnl()
        assert result is None
        assert captured.get("rich_md") is None

    def test_returns_none(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", False)
        captured = {}
        def fake_tg(text, rich_md=None):
            captured["text"] = text
        monkeypatch.setattr(bot, "tg", fake_tg)
        result = bot.cmd_pnl()
        assert result is None
        assert "text" in captured


# ═══════════════════════════════════════════════════════════
# O3 — Filing rich analysis (dual-render + <details>)
# ═══════════════════════════════════════════════════════════

class TestFilingRichMd:
    def test_diffless_analysis(self, bot):
        out = bot._filing_rich_md("AAPL", "10-K", "2026-06-01",
                                  "Strong revenue growth.", "")
        assert "### " in out
        assert "AAPL" in out
        assert "10-K" in out
        assert "Strong revenue growth." in out
        assert "<details>" not in out
        assert "---" in out

    def test_diff_with_details(self, bot):
        out = bot._filing_rich_md("MSFT", "8-K", "2026-06-15",
                                  "Analysis body.", "Risk line 1\nRisk line 2")
        assert "<details>" in out
        assert "<summary>" in out
        assert "</details>" in out
        assert "Risk line 1" in out
        assert "Risk line 2" in out

    def test_unverified_added(self, bot):
        out = bot._filing_rich_md("AAPL", "10-K", "2026-06-01",
                                  "Body.", "", unverified=["fig1", "fig2"])
        assert "fig1" in out
        assert "fig2" in out

    def test_price_snippet_added(self, bot):
        out = bot._filing_rich_md("AAPL", "10-K", "2026-06-01",
                                  "Body.", "", price_snippet="📈 +2.3%")
        assert "📈 +2.3%" in out

    def test_no_single_star_bold(self, bot):
        out = bot._filing_rich_md("AAPL", "10-K", "2026-06-01",
                                  "Body.", "Diff text")
        import re
        singles = re.findall(r'(?<!\*)\*(?!\*)', out)
        assert singles == [], f"Legacy *bold* found: {singles}"

    def test_32768_guard(self, bot):
        huge = "x" * 40000
        out = bot._filing_rich_md("AAPL", "10-K", "2026-06-01", huge, "")
        assert out == ""


class TestSendFilingResultDualRender:
    def test_rich_md_passed_when_enabled(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "save_prev", lambda *a: None)
        monkeypatch.setattr(bot, "store_raw_filing", lambda *a, **kw: "rawkey123")
        monkeypatch.setattr(bot, "compute_price_snippet", lambda *a: "")
        monkeypatch.setattr(bot, "log_weekly", lambda *a: None)
        monkeypatch.setattr(bot, "load_cache", lambda: {})
        monkeypatch.setattr(bot, "mark_processed", lambda *a: None)
        monkeypatch.setattr(bot, "save_cache", lambda *a: None)
        monkeypatch.setattr(bot, "status_inc", lambda *a: None)
        captured = {}
        def fake_tg_with_button(text, raw_key, rich_md=None):
            captured["text"] = text
            captured["raw_key"] = raw_key
            captured["rich_md"] = rich_md
        monkeypatch.setattr(bot, "tg_with_button", fake_tg_with_button)
        bot.send_filing_result("AAPL", "10-K", "2026-06-01", "raw text",
                               "Analysis body.", "", True, False)
        assert captured.get("rich_md") is not None
        assert "### " in captured["rich_md"]

    def test_rich_empty_passes_none(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "save_prev", lambda *a: None)
        monkeypatch.setattr(bot, "store_raw_filing", lambda *a, **kw: "rawkey123")
        monkeypatch.setattr(bot, "compute_price_snippet", lambda *a: "")
        monkeypatch.setattr(bot, "log_weekly", lambda *a: None)
        monkeypatch.setattr(bot, "load_cache", lambda: {})
        monkeypatch.setattr(bot, "mark_processed", lambda *a: None)
        monkeypatch.setattr(bot, "save_cache", lambda *a: None)
        monkeypatch.setattr(bot, "status_inc", lambda *a: None)
        captured = {}
        def fake_tg_with_button(text, raw_key, rich_md=None):
            captured["rich_md"] = rich_md
        monkeypatch.setattr(bot, "tg_with_button", fake_tg_with_button)
        # 40000 char analysis triggers 32768 guard → rich_full = "" → None passed
        bot.send_filing_result("AAPL", "10-K", "2026-06-01", "raw",
                               "x" * 40000, "", True, False)
        assert captured.get("rich_md") is None

    def test_quiet_skips_sending(self, bot, monkeypatch):
        called = []
        monkeypatch.setattr(bot, "save_prev", lambda *a: None)
        monkeypatch.setattr(bot, "store_raw_filing", lambda *a, **kw: "rawkey123")
        monkeypatch.setattr(bot, "log_weekly", lambda *a: None)
        monkeypatch.setattr(bot, "load_cache", lambda: {})
        monkeypatch.setattr(bot, "mark_processed", lambda *a: None)
        monkeypatch.setattr(bot, "save_cache", lambda *a: None)
        monkeypatch.setattr(bot, "status_inc", lambda *a: None)
        monkeypatch.setattr(bot, "tg_with_button", lambda *a, **kw: called.append(1))
        bot.send_filing_result("AAPL", "10-K", "2026-06-01", "raw",
                               "Analysis.", "", True, True)
        assert called == []


# ═══════════════════════════════════════════════════════════
# O4 — Daily news + weekly digest rich (dual-render)
# ═══════════════════════════════════════════════════════════

class TestFormatDailyNewsRich:
    def test_empty_when_no_fresh(self, bot):
        from datetime import date as _date
        today = _date.today().isoformat()
        news = {"AAPL": [{"content": {"title": "Old", "pubDate": "2020-01-01",
                                       "canonicalUrl": {"url": ""},
                                       "provider": {"displayName": ""}}}]}
        assert bot._format_daily_news_rich(news, today) == ""

    def test_gfm_header_and_structure(self, bot):
        from datetime import date as _date
        today = _date.today().isoformat()
        raw = [{"content": {"title": "News1", "pubDate": today,
                             "canonicalUrl": {"url": "http://x.com"},
                             "provider": {"displayName": "Source"}}}]
        news = {"AAPL": raw}
        out = bot._format_daily_news_rich(news, today)
        assert "### " in out
        assert "**AAPL**" in out
        assert "[News1](http://x.com)" in out

    def test_per_ticker_cap(self, bot):
        from datetime import date as _date
        today = _date.today().isoformat()
        raw = [{"content": {"title": f"N{i}", "pubDate": today,
                             "canonicalUrl": {"url": ""},
                             "provider": {"displayName": ""}}}
                for i in range(10)]
        news = {"AAPL": raw}
        out = bot._format_daily_news_rich(news, today, per_ticker=2)
        assert out.count("- [") == 2


class TestDigestRichMd:
    def test_empty_data_shows_no_filings(self, bot):
        out = bot._digest_rich_md([], "01.06.2026", "22.06.2026")
        assert "### " in out
        assert bot.t("digest_no_filings") in out

    def test_data_with_entries(self, bot):
        data = [{"ticker": "AAPL", "form": "10-K", "tarih": "2026-06-01",
                 "analiz": "Revenue up"}]
        out = bot._digest_rich_md(data, "01.06.2026", "22.06.2026")
        assert "#### 🏢 AAPL" in out
        assert "- 10-K (2026-06-01):" in out
        assert "Revenue up" in out

    def test_pnl_rich_appended(self, bot):
        out = bot._digest_rich_md([], "01.06.2026", "22.06.2026",
                                  pnl_rich="**Portfolio:** $1,500")
        assert "**Portfolio:** $1,500" in out


class TestDigestPnlSplit:
    def test_legacy_byte_equivalence(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", True)
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {
            "portfolio": [{"ticker": "AAPL", "qty": 10, "avg_cost": 100.0}]})
        monkeypatch.setattr(bot, "aggregate_positions", lambda lots: {"AAPL": lots[0]})
        monkeypatch.setattr(bot, "fetch_last_close", lambda t: 150.0)
        monkeypatch.setattr(bot, "compute_pnl_rows", lambda a, p: [
            _make_row("AAPL", 10, 150.0, 100.0)])
        monkeypatch.setattr(bot, "load_portfolio_history", lambda: {})
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        legacy = bot._digest_pnl_summary()
        d = bot._digest_pnl_data()
        assert d is not None
        from_rebuilt = bot._digest_pnl_fmt_legacy(d)
        assert legacy == from_rebuilt

    def test_rich_uses_bold(self, bot, monkeypatch):
        d = {"total_val": 1500, "total_pnl": 500, "total_cost": 1000,
             "t_emoji": "📈", "t_pct": "+50.0%",
             "movers": ({"ticker": "AAPL", "pnl_pct": 50.0},
                        {"ticker": "MSFT", "pnl_pct": -5.0}),
             "delta_parts": ["1W: 📈 +$100 (+7.0%)"]}
        out = bot._digest_pnl_rich(d)
        assert "**Portfolio:**" in out or "**Portföy:**" in out
        assert "**Top:**" in out or "**En iyi:**" in out


class TestSendDailyNewsDualRender:
    def test_rich_md_passed(self, bot, monkeypatch):
        from datetime import date as _date
        today = _date.today().isoformat()
        monkeypatch.setattr(bot, "YF_OK", True)
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {"tickers": ["AAPL"]})
        monkeypatch.setattr(bot, "fetch_yfinance_news", lambda t: [
            {"content": {"title": "N", "pubDate": today,
                          "canonicalUrl": {"url": "http://x"},
                          "provider": {"displayName": "S"}}}])
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        captured = {}
        def fake_tg(text, rich_md=None):
            captured["text"] = text
            captured["rich_md"] = rich_md
        monkeypatch.setattr(bot, "tg", fake_tg)
        result = bot.send_daily_news()
        assert result is True
        assert captured.get("rich_md") is not None
        assert "### " in captured["rich_md"]


# ═══════════════════════════════════════════════════════════
# O5 — Thinking draft (sendRichMessageDraft + <tg-thinking>)
# ═══════════════════════════════════════════════════════════

class TestIsPrivateChat:
    def test_positive_is_private(self, bot):
        assert bot._is_private_chat("123456") is True

    def test_negative_is_not_private(self, bot):
        assert bot._is_private_chat("-100123") is False

    def test_zero_is_not_private(self, bot):
        assert bot._is_private_chat("0") is False

    def test_garbage_is_not_private(self, bot):
        assert bot._is_private_chat("abc") is False


class TestThinkingDraftMd:
    def test_contains_thinking_tag(self, bot):
        out = bot._thinking_draft_md("AAPL", "10-K")
        assert "<tg-thinking>" in out
        assert "</tg-thinking>" in out
        assert "AAPL" in out
        assert "10-K" in out


class TestTgSendRichDraftTo:
    def _reset(self, bot):
        bot._RICH_DRAFT_CAP = None

    def test_success_sets_cap(self, bot, monkeypatch):
        self._reset(bot)
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: type("R", (), {"status_code": 200, "ok": True, "text": "", "headers": {}})())
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        assert bot._tg_send_rich_draft_to("123", "# Draft", 42) is True
        assert bot._RICH_DRAFT_CAP is True

    def test_cap_false_short_circuits(self, bot, monkeypatch):
        bot._RICH_DRAFT_CAP = False
        called = []
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: called.append(1) or type("R", (), {"status_code": 200})())
        assert bot._tg_send_rich_draft_to("123", "# Draft", 42) is False
        assert called == []

    def test_unsupported_sets_only_draft_cap(self, bot, monkeypatch):
        self._reset(bot)
        bot._RICH_CAP = True
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: type("R", (), {"status_code": 404, "ok": False, "text": "", "headers": {}})())
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        assert bot._tg_send_rich_draft_to("123", "# Draft", 42) is False
        assert bot._RICH_DRAFT_CAP is False
        assert bot._RICH_CAP is True  # isolation: _RICH_CAP untouched

    def test_never_raises(self, bot, monkeypatch):
        self._reset(bot)
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(Exception("net")))
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        assert bot._tg_send_rich_draft_to("123", "# Draft", 42) is False


class TestMaybePostThinkingDraft:
    def _setup(self, bot, monkeypatch):
        self.called_with = []
        monkeypatch.setattr(bot, "_tg_send_rich_draft_to",
                            lambda cid, md, did: (self.called_with.append((cid, md, did)), True)[-1])
        bot._RICH_DRAFT_CAP = None
        monkeypatch.setattr(bot, "_rich_enabled", lambda cid: True)

    def test_quiet_returns_false(self, bot, monkeypatch):
        self._setup(bot, monkeypatch)
        bot._ctx.chat_id = "123"
        assert bot._maybe_post_thinking_draft("AAPL", "10-K", quiet=True) is False
        assert self.called_with == []

    def test_no_context_returns_false(self, bot, monkeypatch):
        self._setup(bot, monkeypatch)
        bot._ctx.chat_id = None
        assert bot._maybe_post_thinking_draft("AAPL", "10-K", quiet=False) is False
        assert self.called_with == []

    def test_negative_chat_returns_false(self, bot, monkeypatch):
        self._setup(bot, monkeypatch)
        bot._ctx.chat_id = "-100123"
        assert bot._maybe_post_thinking_draft("AAPL", "10-K", quiet=False) is False
        assert self.called_with == []

    def test_cap_false_returns_false(self, bot, monkeypatch):
        self._setup(bot, monkeypatch)
        bot._RICH_DRAFT_CAP = False
        bot._ctx.chat_id = "123"
        assert bot._maybe_post_thinking_draft("AAPL", "10-K", quiet=False) is False
        assert self.called_with == []

    def test_rich_disabled_returns_false(self, bot, monkeypatch):
        self._setup(bot, monkeypatch)
        monkeypatch.setattr(bot, "_rich_enabled", lambda cid: False)
        bot._ctx.chat_id = "123"
        assert bot._maybe_post_thinking_draft("AAPL", "10-K", quiet=False) is False
        assert self.called_with == []

    def test_happy_path_posts_draft(self, bot, monkeypatch):
        self._setup(bot, monkeypatch)
        bot._ctx.chat_id = "123"
        result = bot._maybe_post_thinking_draft("AAPL", "10-K", quiet=False)
        assert result is True
        assert len(self.called_with) == 1
        assert self.called_with[0][0] == "123"
        assert "<tg-thinking>" in self.called_with[0][1]


# ═══════════════════════════════════════════════════════════
# O6 — /compare rich (GFM metrics table + dual-render)
# ═══════════════════════════════════════════════════════════

def _make_metrics_data(a="AAPL", b="MSFT"):
    return {
        "a": a, "b": b,
        "rows": [
            ("Revenue", 394e9, 245e9),
            ("Net Income", 99e9, 72e9),
            ("Total Assets", 352e9, 411e9),
        ],
    }


class TestCompareMetricsRich:
    def test_gfm_table_structure(self, bot):
        d = _make_metrics_data()
        out = bot._compare_metrics_rich(d)
        assert "| Metric |" in out
        assert "|:--|--:|--:|--:|" in out
        assert "| Revenue |" in out

    def test_delta_calculated(self, bot):
        d = _make_metrics_data()
        out = bot._compare_metrics_rich(d)
        assert "+60.8%" in out  # (394-245)/245*100

    def test_none_when_no_data(self, bot):
        assert bot._compare_metrics_rich(None) == ""


class TestCompareMetricsLegacy:
    def test_byte_equivalence(self, bot):
        d = _make_metrics_data()
        out = bot._compare_metrics_legacy(d)
        assert "```" in out
        assert "📊 *Financial Metrics Comparison*" in out
        assert "AAPL" in out
        assert "MSFT" in out


class TestCompareRichMd:
    def test_header_and_summary(self, bot):
        out = bot._compare_rich_md("AAPL", "2026-01-01", "MSFT", "2026-01-02",
                                   "10-K", "Both companies are strong.")
        assert "### " in out
        assert "AAPL" in out
        assert "MSFT" in out
        assert "Both companies are strong." in out

    def test_metrics_appended(self, bot):
        metrics = bot._compare_metrics_rich(_make_metrics_data())
        out = bot._compare_rich_md("AAPL", "2026-01-01", "MSFT", "2026-01-02",
                                   "10-K", "Summary.", metrics_rich=metrics)
        assert "| Metric |" in out

    def test_32768_guard(self, bot):
        huge = "x" * 40000
        out = bot._compare_rich_md("A", "d1", "B", "d2", "10-K", huge)
        assert out == ""

    def test_no_single_star_bold(self, bot):
        out = bot._compare_rich_md("A", "d1", "B", "d2", "10-K", "Summary.")
        import re
        singles = re.findall(r'(?<!\*)\*(?!\*)', out)
        assert singles == [], f"Legacy *bold* found: {singles}"


# ═══════════════════════════════════════════════════════════
# O7 — /checknews rich (haber başlıkları rich liste)
# ═══════════════════════════════════════════════════════════

class TestFormatNewsListRich:
    def test_empty_returns_empty(self, bot):
        assert bot._format_news_list_rich("AAPL", [], 5) == ""

    def test_gfm_header_and_items(self, bot):
        raw = [{"content": {"title": "News1", "pubDate": "2026-06-22",
                             "canonicalUrl": {"url": "http://x.com"},
                             "provider": {"displayName": "Source"}}}]
        out = bot._format_news_list_rich("AAPL", raw, 5)
        assert "### 📰 AAPL" in out
        assert "[News1](http://x.com)" in out
        assert "*Source · 2026-06-22*" in out

    def test_count_limit(self, bot):
        raw = [{"content": {"title": f"N{i}", "pubDate": "2026-06-22",
                             "canonicalUrl": {"url": ""},
                             "provider": {"displayName": ""}}}
                for i in range(10)]
        out = bot._format_news_list_rich("AAPL", raw, 2)
        assert out.count("- [") == 2


# ═══════════════════════════════════════════════════════════
# O8 — /status rich (native durum paneli tablosu)
# ═══════════════════════════════════════════════════════════

class TestStatusRich:
    def test_legacy_byte_equivalence(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "status_snapshot", lambda: {
            "started": "2026-06-22T10:00:00", "last_update": "2026-06-22T12:00:00",
            "last_scan": None, "last_alarm": None,
            "total_analyzed": 42, "tg_errors": 0, "or_errors": 3,
        })
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {
            "schedule": "08:00", "alarm_on": True, "weekly_digest": True,
            "tickers": ["AAPL", "MSFT"],
        })
        monkeypatch.setattr(bot, "get_lang", lambda: "en")
        d = bot._status_data()
        legacy = bot._status_legacy(d)
        from_bot = bot.t("status_block", **d)
        assert legacy == from_bot

    def test_rich_gfm_table(self, bot):
        d = {"uptime": "5s", "last_update": "—", "last_scan": "—",
             "last_alarm": "—", "total_analyzed": 0,
             "tg_errors": "✅ 0", "or_errors": "✅ 0",
             "language": "en", "schedule": "off",
             "alarm": "off", "digest": "off", "ticker_count": 0}
        out = bot._status_rich(d)
        assert "### 📡" in out
        assert "| Alan |" in out or "| Field |" in out
        assert "|:--|:--|" in out
        assert "`5s`" in out

    def test_no_single_star_bold(self, bot):
        d = {"uptime": "1h", "last_update": "—", "last_scan": "—",
             "last_alarm": "—", "total_analyzed": 0,
             "tg_errors": "✅ 0", "or_errors": "✅ 0",
             "language": "en", "schedule": "off",
             "alarm": "off", "digest": "off", "ticker_count": 0}
        out = bot._status_rich(d)
        import re
        singles = re.findall(r'(?<!\*)\*(?!\*)', out)
        assert singles == [], f"Legacy *bold* found: {singles}"


class TestCmdStatusDualRender:
    def test_returns_none_and_sends_once(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "status_snapshot", lambda: {
            "started": "2026-06-22T10:00:00", "last_update": None,
            "last_scan": None, "last_alarm": None,
            "total_analyzed": 0, "tg_errors": 0, "or_errors": 0,
        })
        monkeypatch.setattr(bot, "get_chat_cfg", lambda: {
            "schedule": None, "alarm_on": False, "weekly_digest": False,
            "tickers": [],
        })
        monkeypatch.setattr(bot, "get_lang", lambda: "en")
        captured = {}
        def fake_tg(text, rich_md=None):
            captured["text"] = text
            captured["rich_md"] = rich_md
        monkeypatch.setattr(bot, "tg", fake_tg)
        result = bot.cmd_status()
        assert result is None
        assert "text" in captured
        assert captured.get("rich_md") is not None
        assert "### 📡" in captured["rich_md"]


# ═══════════════════════════════════════════════════════════
# O9 — Watchword/EFTS alert rich (proaktif eşleşme listesi)
# ═══════════════════════════════════════════════════════════

def _make_hit(ticker="AAPL", form="10-K", date="2026-06-01"):
    return {"ticker_or_cik": ticker, "form": form, "date": date,
            "url": f"http://example.com/{ticker}", "accession": f"acc_{ticker}"}


class TestFormatWatchwordAlertRich:
    def test_empty_returns_empty(self, bot):
        assert bot.format_watchword_alert_rich("revenue", []) == ""

    def test_gfm_header_and_hits(self, bot):
        hits = [_make_hit("AAPL", "10-K", "2026-06-01")]
        out = bot.format_watchword_alert_rich("revenue", hits)
        assert "### 🔍" in out
        assert "revenue" in out
        assert "- [AAPL 10-K 2026-06-01]" in out

    def test_cap_and_overflow(self, bot):
        hits = [_make_hit(f"T{i}") for i in range(6)]
        out = bot.format_watchword_alert_rich("query", hits)
        lines = [l for l in out.split("\n") if l.startswith("- [")]
        assert len(lines) == 5
        assert "+1" in out

    def test_no_escape_in_output(self, bot):
        hits = [_make_hit("AAPL")]
        out = bot.format_watchword_alert_rich("test*word", hits)
        assert "\\*" not in out


# ═══════════════════════════════════════════════════════════
# O10 — /sheet rich (üç GFM finansal-tablo)
# ═══════════════════════════════════════════════════════════

class TestSheetRichMd:
    def test_empty_sections_returns_empty(self, bot):
        assert bot._sheet_rich_md("AAPL", "Yearly", []) == ""

    def test_three_gfm_tables(self, bot):
        sections = [
            ("INCOME STATEMENT", ["FY2024", "FY2023"],
             [("Revenue", "$394.0B  $383.0B"), ("Net Income", "$99.0B  $88.0B")]),
            ("BALANCE SHEET", ["FY2024"],
             [("Assets", "$352.0B")]),
            ("CASH FLOW", ["FY2024"],
             [("Cash:Operating", "$110.0B")]),
        ]
        out = bot._sheet_rich_md("AAPL", "Yearly", sections)
        assert "### 📊 AAPL" in out
        assert "**INCOME STATEMENT**" in out
        assert "| Concept | FY2024 | FY2023 |" in out
        assert "|:--|--:|--:|" in out
        assert "| Revenue | $394.0B | $383.0B |" in out
        assert "**BALANCE SHEET**" in out
        assert "**CASH FLOW**" in out

    def test_no_pipe_in_cells(self, bot):
        sections = [("TEST", ["FY2024"], [("Revenue", "$100B")])]
        out = bot._sheet_rich_md("X", "Yearly", sections)
        lines = [l for l in out.split("\n") if l.startswith("|")]
        for line in lines:
            assert line.count("|") >= 3

    def test_32768_guard(self, bot):
        huge_rows = [("R" + str(i), "$100B") for i in range(5000)]
        sections = [("BIG", ["FY2024"], huge_rows)]
        out = bot._sheet_rich_md("X", "Yearly", sections)
        assert out == ""
