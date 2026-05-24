"""
Tests for the Item-4 interactive alarm and the Item-5 on-demand .md button.

Item 4 — the hourly alarm no longer just says "new filing found". It lists
each new filing and attaches inline buttons: one [🔍 TICKER FORM] per filing
plus an [🔍 Analyze all] button. Pressing a button runs the real LLM
analysis and retires the button.

Item 5 — the .md analysis report is no longer pushed after every analysis.
The analysis message carries a [📄 .md report] button; the report is built
on demand from the data parked in the raw-filing store.

Both features are in-memory only — a bot restart invalidates outstanding
buttons (alarm tokens / raw keys are lost).
"""
import pytest


# ─── Item 5 — build_inline_button: raw + md ───────────────
class TestInlineButton:
    def test_two_buttons_returned(self, bot):
        kb = bot.build_inline_button("abc123")
        row = kb["inline_keyboard"][0]
        assert len(row) == 2

    def test_callback_data_raw_and_md(self, bot):
        kb = bot.build_inline_button("abc123")
        datas = [b["callback_data"] for b in kb["inline_keyboard"][0]]
        assert "raw:abc123" in datas
        assert "md:abc123" in datas


# ─── Item 5 — store_raw_filing keeps analysis + diff ──────
class TestStoreRawFilingAnalysis:
    def test_analysis_and_diff_stored(self, bot):
        k = bot.store_raw_filing("AAPL", "10-K", "2026-05-01", "body",
                                 analysis="the analysis", diff="the diff")
        v = bot.get_raw_filing(k)
        assert v["analysis"] == "the analysis"
        assert v["diff"]     == "the diff"

    def test_analysis_defaults_empty(self, bot):
        k = bot.store_raw_filing("AAPL", "10-K", "2026-05-01", "body")
        v = bot.get_raw_filing(k)
        assert v["analysis"] == ""
        assert v["diff"]     == ""


# ─── Item 5 — send_md_for_key ─────────────────────────────
class TestSendMdForKey:
    def test_missing_key_reports_not_found(self, bot, monkeypatch):
        answers, msgs = [], []
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: answers.append(a))
        monkeypatch.setattr(bot, "tg", lambda m, *a, **k: msgs.append(m))
        monkeypatch.setattr(bot, "tg_send_document",
                            lambda *a, **k: pytest.fail("must not send a doc"))
        bot.send_md_for_key("cb1", "no_such_key")
        assert any(bot.t("raw_filing_not_found") == m for m in msgs)

    def test_valid_key_sends_md_with_stored_analysis(self, bot, monkeypatch):
        k = bot.store_raw_filing("MSFT", "8-K", "2026-05-10", "body",
                                 analysis="ANALYSIS-TEXT", diff="DIFF-TEXT")
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: None)
        sent = {}
        def fake_doc(filename, content, caption=""):
            sent["filename"] = filename
            sent["content"]  = content
        monkeypatch.setattr(bot, "tg_send_document", fake_doc)
        bot.send_md_for_key("cb1", k)
        assert sent["filename"].endswith(".md")
        assert "MSFT" in sent["filename"]
        # The on-demand .md is built from the parked analysis/diff.
        assert "ANALYSIS-TEXT" in sent["content"]
        assert "DIFF-TEXT" in sent["content"]


# ─── Item 4 — pending-alarm store ─────────────────────────
class TestAlarmStore:
    def test_register_and_get_roundtrip(self, bot):
        hits = [("AAPL", "10-K", "2026-05-01"), ("MSFT", "8-K", "2026-05-02")]
        token = bot.register_alarm_hits(hits)
        entry = bot.get_alarm_hits(token)
        assert entry["hits"] == hits
        assert entry["done"] == set()

    def test_get_unknown_token_returns_none(self, bot):
        assert bot.get_alarm_hits("nonexistent") is None

    def test_get_returns_shallow_copy(self, bot):
        token = bot.register_alarm_hits([("AAPL", "10-K", "2026-05-01")])
        entry = bot.get_alarm_hits(token)
        entry["done"].add(0)              # mutate the copy
        # Store must be unaffected by mutation of the returned copy.
        assert bot.get_alarm_hits(token)["done"] == set()

    def test_mark_done(self, bot):
        token = bot.register_alarm_hits(
            [("AAPL", "10-K", "2026-05-01"), ("MSFT", "8-K", "2026-05-02")])
        bot.mark_alarm_done(token, 1)
        assert bot.get_alarm_hits(token)["done"] == {1}

    def test_mark_done_unknown_token_is_noop(self, bot):
        # Must not raise.
        bot.mark_alarm_done("nonexistent", 0)

    def test_fifo_cap_at_50(self, bot):
        tokens = [bot.register_alarm_hits([("T", "F", "D")]) for _ in range(60)]
        # First 10 tokens evicted, last 50 retained.
        assert all(bot.get_alarm_hits(t) is None for t in tokens[:10])
        assert all(bot.get_alarm_hits(t) is not None for t in tokens[10:])


# ─── Item 4 — build_alarm_keyboard ────────────────────────
class TestBuildAlarmKeyboard:
    def test_single_hit_one_button_no_analyze_all(self, bot):
        hits = [("AAPL", "10-K", "2026-05-01")]
        kb = bot.build_alarm_keyboard("tok", hits, set())
        rows = kb["inline_keyboard"]
        assert len(rows) == 1                       # just the one filing
        assert rows[0][0]["callback_data"] == "analyze:tok:0"

    def test_multiple_hits_have_analyze_all_row(self, bot):
        hits = [("AAPL", "10-K", "D"), ("MSFT", "8-K", "D"), ("NVDA", "4", "D")]
        kb = bot.build_alarm_keyboard("tok", hits, set())
        rows = kb["inline_keyboard"]
        assert len(rows) == 4                       # 3 filings + analyze-all
        assert rows[-1][0]["callback_data"] == "analyzeall:tok"

    def test_done_hits_excluded(self, bot):
        hits = [("AAPL", "10-K", "D"), ("MSFT", "8-K", "D")]
        kb = bot.build_alarm_keyboard("tok", hits, {0})
        rows = kb["inline_keyboard"]
        # One hit done → one remaining → single button, no analyze-all.
        assert len(rows) == 1
        assert rows[0][0]["callback_data"] == "analyze:tok:1"

    def test_all_done_returns_none(self, bot):
        hits = [("AAPL", "10-K", "D"), ("MSFT", "8-K", "D")]
        assert bot.build_alarm_keyboard("tok", hits, {0, 1}) is None


# ─── Item 4 — send_alarm_alert ────────────────────────────
class TestSendAlarmAlert:
    def test_registers_token_and_sends_keyboard(self, bot, monkeypatch):
        sent = {}
        def fake_send(text, keyboard):
            sent["text"] = text
            sent["keyboard"] = keyboard
        monkeypatch.setattr(bot, "tg_with_keyboard", fake_send)
        hits = [("AAPL", "10-K", "2026-05-01"), ("MSFT", "8-K", "2026-05-02")]
        bot.send_alarm_alert(hits)
        # A keyboard is attached and it carries one button per hit.
        kb_rows = sent["keyboard"]["inline_keyboard"]
        analyze = [r[0]["callback_data"] for r in kb_rows
                   if r[0]["callback_data"].startswith("analyze:")]
        assert len(analyze) == 2
        # The message body names both tickers.
        assert "AAPL" in sent["text"] and "MSFT" in sent["text"]


# ─── Item 4 — handle_analyze_callback ─────────────────────
class TestHandleAnalyzeCallback:
    def _cq(self, cb_id="cb1"):
        return {"id": cb_id,
                "message": {"chat": {"id": 1}, "message_id": 99}}

    def test_valid_hit_runs_scan_and_marks_done(self, bot, monkeypatch):
        token = bot.register_alarm_hits([("AAPL", "10-K", "2026-05-01")])
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: None)
        monkeypatch.setattr(bot, "tg_edit_markup", lambda *a, **k: None)
        scans = []
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda ticker, forms, *a, **k: scans.append((ticker, forms)))
        bot.handle_analyze_callback(self._cq(), token, 0)
        assert scans == [("AAPL", ["10-K"])]
        assert bot.get_alarm_hits(token)["done"] == {0}

    def test_expired_token_no_scan(self, bot, monkeypatch):
        answers = []
        monkeypatch.setattr(bot, "tg_answer_callback",
                            lambda cb, text="": answers.append(text))
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda *a, **k: pytest.fail("must not scan"))
        bot.handle_analyze_callback(self._cq(), "nonexistent", 0)
        assert answers == [bot.t("alarm_expired")]

    def test_already_done_no_scan(self, bot, monkeypatch):
        token = bot.register_alarm_hits([("AAPL", "10-K", "2026-05-01")])
        bot.mark_alarm_done(token, 0)
        answers = []
        monkeypatch.setattr(bot, "tg_answer_callback",
                            lambda cb, text="": answers.append(text))
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda *a, **k: pytest.fail("must not scan"))
        bot.handle_analyze_callback(self._cq(), token, 0)
        assert answers == [bot.t("alarm_already_done")]

    def test_out_of_range_idx_expired(self, bot, monkeypatch):
        token = bot.register_alarm_hits([("AAPL", "10-K", "2026-05-01")])
        answers = []
        monkeypatch.setattr(bot, "tg_answer_callback",
                            lambda cb, text="": answers.append(text))
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda *a, **k: pytest.fail("must not scan"))
        bot.handle_analyze_callback(self._cq(), token, 5)
        assert answers == [bot.t("alarm_expired")]


# ─── Item 4 — handle_analyzeall_callback ──────────────────
class TestHandleAnalyzeAllCallback:
    def _cq(self):
        return {"id": "cb1", "message": {"chat": {"id": 1}, "message_id": 99}}

    def test_analyzes_every_pending_hit(self, bot, monkeypatch):
        token = bot.register_alarm_hits([
            ("AAPL", "10-K", "D"), ("MSFT", "8-K", "D"), ("NVDA", "4", "D")])
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: None)
        monkeypatch.setattr(bot, "tg_edit_markup", lambda *a, **k: None)
        scans = []
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda ticker, forms, *a, **k: scans.append((ticker, forms)))
        bot.handle_analyzeall_callback(self._cq(), token)
        assert scans == [("AAPL", ["10-K"]), ("MSFT", ["8-K"]), ("NVDA", ["4"])]
        assert bot.get_alarm_hits(token)["done"] == {0, 1, 2}

    def test_skips_already_done_hits(self, bot, monkeypatch):
        token = bot.register_alarm_hits([
            ("AAPL", "10-K", "D"), ("MSFT", "8-K", "D")])
        bot.mark_alarm_done(token, 0)            # AAPL already analyzed
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: None)
        monkeypatch.setattr(bot, "tg_edit_markup", lambda *a, **k: None)
        scans = []
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda ticker, forms, *a, **k: scans.append(ticker))
        bot.handle_analyzeall_callback(self._cq(), token)
        assert scans == ["MSFT"]                 # AAPL skipped

    def test_expired_token_no_scan(self, bot, monkeypatch):
        answers = []
        monkeypatch.setattr(bot, "tg_answer_callback",
                            lambda cb, text="": answers.append(text))
        monkeypatch.setattr(bot, "scan_ticker",
                            lambda *a, **k: pytest.fail("must not scan"))
        bot.handle_analyzeall_callback(self._cq(), "nonexistent")
        assert answers == [bot.t("alarm_expired")]
