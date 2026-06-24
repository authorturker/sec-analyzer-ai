"""
J3 — No-AI Mode testleri.

ai_enabled(), _deliver_raw_text(), _check_no_keys_reminder(),
llm() kısa devre (sıfır HTTP), analyze_filing()/scan_ticker() bozulma,
/compare ve /sentiment bozulma, i18n parite.
"""
import io
import pytest


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _clear_keys(bot, monkeypatch):
    """Ensure no LLM keys visible — empties cfg api_keys + legacy env vars."""
    monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
    bot.mutate_cfg(lambda c: (
        c.update({"api_keys": {}, "default_provider": "", "openrouter_api_key": ""})
    ))


def _set_key(bot, provider="openrouter", key="sk-test-1234"):
    bot.mutate_cfg(lambda c: (
        c.setdefault("api_keys", {}).__setitem__(provider, key)
        or c.update({"default_provider": provider})
    ))


# ─── ai_enabled() ─────────────────────────────────────────────────────────────

class TestAiEnabled:
    def test_no_keys_returns_false(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        assert bot.ai_enabled() is False

    def test_api_key_returns_true(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        _set_key(bot)
        assert bot.ai_enabled() is True

    def test_env_key_still_true(self, bot):
        """conftest sets OPENROUTER_API_KEY='test-key' → ai_enabled True by default."""
        bot.mutate_cfg(lambda c: c.update({"api_keys": {}, "default_provider": "",
                                            "openrouter_api_key": ""}))
        # env var still present from conftest — legacy fallback
        assert bot.ai_enabled() is True

    def test_after_clear_env_returns_false(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        assert bot.ai_enabled() is False


# ─── llm() NO_KEYS kısa devresi ───────────────────────────────────────────────

class TestLlmNoKeys:
    def test_returns_none_with_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        called = []
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: called.append(True) or (_ for _ in ()).throw(
                                AssertionError("HTTP must not be called")))
        result = bot.llm("any prompt", "any-model")
        assert result is None
        assert called == [], "No HTTP request expected on NO_KEYS"

    def test_zero_http_calls_on_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        http_calls = []
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: http_calls.append(True))
        bot.llm("p", "m")
        assert http_calls == []

    def test_with_key_still_makes_http(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        _set_key(bot)
        called = []

        class FakeResp:
            status_code = 200
            ok = True
            text = ""
            def json(self): return {"choices": [{"message": {"content": "ok"}}]}
            def raise_for_status(self): pass

        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: called.append(True) or FakeResp())
        result = bot.llm("p", "m")
        assert called, "HTTP should be called when key exists"
        assert result == "ok"


# ─── _deliver_raw_text() ──────────────────────────────────────────────────────

class TestDeliverRawText:
    def _tg_calls(self, bot, monkeypatch):
        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text, **kw: sent.append(text))
        return sent

    def test_empty_body_sends_only_warning(self, bot, monkeypatch):
        sent = self._tg_calls(bot, monkeypatch)
        bot._deliver_raw_text("", "AAPL", "10-K", "2024-01-01", "no_ai_no_keys")
        assert len(sent) == 1
        assert bot.t("no_ai_no_keys") in sent[0]

    def test_short_body_inline_message(self, bot, monkeypatch):
        sent = self._tg_calls(bot, monkeypatch)
        body = "x" * 100
        bot._deliver_raw_text(body, "AAPL", "10-K", "2024-01-01", "no_ai_no_keys")
        assert len(sent) == 1
        assert bot.t("no_ai_no_keys") in sent[0]
        assert body in sent[0]

    def test_exactly_at_limit_goes_inline(self, bot, monkeypatch):
        sent = self._tg_calls(bot, monkeypatch)
        body = "a" * bot._RAW_TEXT_INLINE_LIMIT
        bot._deliver_raw_text(body, "AAPL", "8-K", "2024-01-01", "no_ai_no_keys")
        assert len(sent) == 1  # inline, not document

    def test_long_body_sends_document(self, bot, monkeypatch):
        docs_sent = []
        monkeypatch.setattr(bot.requests, "post", lambda url, **k:
            docs_sent.append(url) or type("R", (), {"ok": True, "status_code": 200})())
        body = "z" * (bot._RAW_TEXT_INLINE_LIMIT + 1)
        bot._deliver_raw_text(body, "TSLA", "10-K", "2024-03-15", "no_ai_no_keys")
        assert any("sendDocument" in u for u in docs_sent)

    def test_document_filename_format(self, bot, monkeypatch):
        filenames = []
        def fake_post(url, data=None, files=None, **k):
            if files and "document" in files:
                filenames.append(files["document"][0])
            return type("R", (), {"ok": True, "status_code": 200})()
        monkeypatch.setattr(bot.requests, "post", fake_post)
        body = "y" * (bot._RAW_TEXT_INLINE_LIMIT + 1)
        bot._deliver_raw_text(body, "MSFT", "10-Q", "2024-06-30", "no_ai_no_keys")
        assert filenames, "sendDocument must be called"
        assert filenames[0] == "MSFT_10-Q_2024-06-30.txt"

    def test_200kb_cap_truncated(self, bot, monkeypatch):
        contents = []
        def fake_post(url, data=None, files=None, **k):
            if files and "document" in files:
                raw = files["document"][1].read()
                contents.append(raw.decode("utf-8"))
            return type("R", (), {"ok": True, "status_code": 200})()
        monkeypatch.setattr(bot.requests, "post", fake_post)
        body = "w" * (bot._RAW_TEXT_FILE_MAX + 1000)
        bot._deliver_raw_text(body, "GOOG", "10-K", "2024-01-01", "no_ai_no_keys")
        assert contents, "sendDocument must be called"
        assert len(contents[0].encode("utf-8")) <= bot._RAW_TEXT_FILE_MAX + 50  # +50 for "[truncated]"
        assert "[truncated]" in contents[0]

    def test_document_failure_fallback_inline(self, bot, monkeypatch):
        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text, **kw: sent.append(text))
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: (_ for _ in ()).throw(ConnectionError("fail")))
        body = "q" * (bot._RAW_TEXT_INLINE_LIMIT + 1)
        bot._deliver_raw_text(body, "AAPL", "10-K", "2024-01-01", "no_ai_no_keys")
        assert sent, "Inline fallback must be sent on document failure"
        assert bot.t("no_ai_no_keys") in sent[0]

    def test_all_failed_warn_key(self, bot, monkeypatch):
        sent = self._tg_calls(bot, monkeypatch)
        bot._deliver_raw_text("short body", "X", "8-K", "2024-01-01", "no_ai_all_failed")
        assert bot.t("no_ai_all_failed") in sent[0]


# ─── _check_no_keys_reminder() ────────────────────────────────────────────────

class TestNoKeysReminder:
    def test_sends_on_first_call(self, bot, monkeypatch):
        bot.mutate_cfg(lambda c: c.update({"no_keys_warned_date": ""}))
        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text, **kw: sent.append(text))
        bot._check_no_keys_reminder()
        assert sent, "Reminder must be sent when date is blank"
        assert bot.t("no_ai_reminder") in sent[0]

    def test_no_repeat_same_day(self, bot, monkeypatch):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        bot.mutate_cfg(lambda c: c.update({"no_keys_warned_date": today}))
        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text, **kw: sent.append(text))
        bot._check_no_keys_reminder()
        assert sent == [], "No reminder when already sent today"

    def test_resends_next_day(self, bot, monkeypatch):
        bot.mutate_cfg(lambda c: c.update({"no_keys_warned_date": "2000-01-01"}))
        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text, **kw: sent.append(text))
        bot._check_no_keys_reminder()
        assert sent, "Reminder must resend after date differs"

    def test_updates_config_date(self, bot, monkeypatch):
        from datetime import datetime
        bot.mutate_cfg(lambda c: c.update({"no_keys_warned_date": ""}))
        monkeypatch.setattr(bot, "_tg_to", lambda *a, **k: None)
        bot._check_no_keys_reminder()
        today = datetime.now().strftime("%Y-%m-%d")
        assert bot.get_cfg()["no_keys_warned_date"] == today


# ─── analyze_filing() bozulma ─────────────────────────────────────────────────

class TestAnalyzeFilingNoAi:
    def test_returns_none_tuple_when_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        analysis, diff = bot.analyze_filing(
            "AAPL", "10-K", "2024-01-01", "Some raw text",
            10000, "model", {}
        )
        assert analysis is None
        assert diff is None

    def test_no_http_in_analyze_when_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        called = []
        monkeypatch.setattr(bot.requests, "post",
                            lambda *a, **k: called.append(True))
        bot.analyze_filing("AAPL", "10-K", "2024-01-01", "text", 100, "m", {})
        assert called == []

    def test_with_key_returns_strings(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        _set_key(bot)

        class FakeResp:
            status_code = 200
            ok = True
            text = ""
            def json(self): return {"choices": [{"message": {"content": "Analysis result"}}]}
            def raise_for_status(self): pass

        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: FakeResp())
        analysis, diff = bot.analyze_filing(
            "AAPL", "10-K", "2024-01-01", "Some text here", 10000, "m", {}
        )
        assert isinstance(analysis, str)
        assert analysis != ""


# ─── diff_analysis() bozulma ──────────────────────────────────────────────────

class TestDiffAnalysisNoAi:
    def test_returns_empty_when_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        result = bot.diff_analysis("AAPL", "10-K", "new text", "model",
                                    previous="old text")
        assert result == ""

    def test_no_http_in_diff_when_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        called = []
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: called.append(True))
        bot.diff_analysis("AAPL", "10-K", "text", "m", previous="prev")
        assert called == []


# ─── /sentiment bozulma ───────────────────────────────────────────────────────

class TestSentimentNoAi:
    def test_no_ai_signal_placeholder_used(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text, **kw: sent.append(text))
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)

        # Mock Company and Form 4 filings with get_ownership_summary
        class MockTx:
            code = "S"
            insider_name = "Test Insider"
            shares = 1000
            price_per_share = 150.0
            transaction_type = "sale"

        class MockSummary:
            insider_name = "Test Insider"
            position = "Director"
            reporting_date = "2024-01-01"
            transactions = [MockTx()]

        class MockObj:
            def get_ownership_summary(self):
                return MockSummary()

        class MockFiling:
            def obj(self):
                return MockObj()

        class MockFilings:
            def head(self, n):
                return [MockFiling()]

        class MockCompany:
            def get_filings(self, form):
                return MockFilings()

        monkeypatch.setattr(bot, "Company", lambda tk: MockCompany())
        monkeypatch.setattr(bot, "append_sentiment", lambda *a, **k: None)

        bot.mutate_cfg(lambda c: c.update({"tickers": ["AAPL"], "model": "m",
                                            "days_lookback": 30}))
        bot.cmd_sentiment()

        assert sent, "A message must be sent even with AI off"
        combined = "\n".join(sent)
        assert bot.t("no_ai_signal_placeholder") in combined

    def test_no_http_in_sentiment_when_no_keys(self, bot, monkeypatch):
        _clear_keys(bot, monkeypatch)
        http_called = []
        original_post = bot.requests.post

        def spy_post(url, *a, **k):
            # Allow Telegram calls but fail on LLM endpoints
            if "openrouter" in url or "groq" in url or "anthropic" in url or "googleapis" in url:
                http_called.append(url)
            return type("R", (), {"ok": True, "status_code": 200,
                                   "text": "", "json": lambda self: {}})()

        monkeypatch.setattr(bot.requests, "post", spy_post)
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)

        class MockTx:
            code = "S"; insider_name = "X"; shares = 100; price_per_share = 100.0; transaction_type = "sale"
        class MockSummary:
            insider_name = "X"; position = "Dir"; reporting_date = "2024-01-01"; transactions = [MockTx()]
        class MockObj:
            def get_ownership_summary(self): return MockSummary()
        class MockFiling:
            def obj(self): return MockObj()
        class MockFilings:
            def head(self, n): return [MockFiling()]
        class MockCompany:
            def get_filings(self, form): return MockFilings()

        monkeypatch.setattr(bot, "Company", lambda tk: MockCompany())
        monkeypatch.setattr(bot, "append_sentiment", lambda *a, **k: None)
        bot.mutate_cfg(lambda c: c.update({"tickers": ["AAPL"], "model": "m",
                                            "days_lookback": 30}))
        bot.cmd_sentiment()
        assert http_called == [], "No LLM HTTP calls when no keys"


# ─── i18n parite ──────────────────────────────────────────────────────────────

J3_KEYS = [
    "no_ai_no_keys",
    "no_ai_all_failed",
    "no_ai_reminder",
    "no_ai_signal_placeholder",
]

@pytest.mark.parametrize("lang", ["en", "tr"])
@pytest.mark.parametrize("key", J3_KEYS)
class TestJ3I18nKeys:
    def test_key_exists_and_nonempty(self, bot, lang, key):
        bot.mutate_cfg(lambda c: c.update({"language": lang}))
        bot._current_lang = None
        bot._lang_cache.clear()
        val = bot.t(key)
        assert val, f"[{lang}] '{key}' must be non-empty"
        assert val != key, f"[{lang}] '{key}' must not fall back to key name"
