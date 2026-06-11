"""
Tests for H6 (run_startup_checks) and R1 (get_company cache).
"""
import pytest


# ─── H6 — run_startup_checks ──────────────────────────────
class TestStartupChecks:
    def _set_secrets(self, bot, monkeypatch, *, edgar=None, token=None,
                     master=None):
        """Patch the three required secret module globals to known-good defaults,
        overriding individual ones as needed."""
        monkeypatch.setattr(bot, "EDGAR_IDENTITY",
                            edgar if edgar is not None else "Real Person r@e.com")
        monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN",
                            token if token is not None else "123456:ABCdef")
        monkeypatch.setattr(bot, "MASTER_CHAT_ID",
                            master if master is not None else "123456789")

    def test_all_good_returns_empty(self, bot, monkeypatch):
        self._set_secrets(bot, monkeypatch)
        assert bot.run_startup_checks() == []

    def test_placeholder_edgar_flagged(self, bot, monkeypatch):
        self._set_secrets(bot, monkeypatch, edgar="Your Name yourname@email.com")
        issues = bot.run_startup_checks()
        assert any("EDGAR" in i for i in issues)

    def test_malformed_token_flagged(self, bot, monkeypatch):
        # No colon → malformed
        self._set_secrets(bot, monkeypatch, token="noColonHere")
        issues = bot.run_startup_checks()
        assert any("TELEGRAM_BOT_TOKEN" in i for i in issues)

    def test_placeholder_token_flagged(self, bot, monkeypatch):
        self._set_secrets(bot, monkeypatch, token="YOUR_BOT_TOKEN")
        issues = bot.run_startup_checks()
        assert any("TELEGRAM_BOT_TOKEN" in i for i in issues)

    def test_missing_lang_dir_flagged(self, bot, monkeypatch, tmp_path):
        self._set_secrets(bot, monkeypatch)
        monkeypatch.setattr(bot, "LANG_DIR", tmp_path / "no_such_lang")
        issues = bot.run_startup_checks()
        assert any("Language directory" in i for i in issues)

    def test_empty_lang_dir_flagged(self, bot, monkeypatch, tmp_path):
        self._set_secrets(bot, monkeypatch)
        empty = tmp_path / "lang"
        empty.mkdir()
        monkeypatch.setattr(bot, "LANG_DIR", empty)
        issues = bot.run_startup_checks()
        assert any("No language files" in i for i in issues)

    def test_multiple_issues_all_reported(self, bot, monkeypatch):
        self._set_secrets(bot, monkeypatch,
                          edgar="Your Name yourname@email.com",
                          token="YOUR_BOT_TOKEN")
        issues = bot.run_startup_checks()
        # Both the EDGAR and the Telegram problem must surface
        assert len(issues) >= 2


# ─── R1 — get_company cache ───────────────────────────────
class TestCompanyCache:
    def test_constructed_once_per_ticker(self, bot, monkeypatch):
        calls = []

        class FakeCompany:
            def __init__(self, ticker):
                calls.append(ticker)
                self.ticker = ticker

        monkeypatch.setattr(bot, "Company", FakeCompany)
        bot._company_cache.clear()

        c1 = bot.get_company("AAPL")
        c2 = bot.get_company("AAPL")
        # Same cached object, constructor hit exactly once
        assert c1 is c2
        assert calls == ["AAPL"]

    def test_distinct_tickers_distinct_objects(self, bot, monkeypatch):
        class FakeCompany:
            def __init__(self, ticker):
                self.ticker = ticker

        monkeypatch.setattr(bot, "Company", FakeCompany)
        bot._company_cache.clear()

        a = bot.get_company("AAPL")
        m = bot.get_company("MSFT")
        assert a is not m
        assert a.ticker == "AAPL" and m.ticker == "MSFT"

    def test_failure_not_cached(self, bot, monkeypatch):
        """A failed construction must not poison the cache — the next call
        should retry rather than return a stale failure."""
        attempts = []

        def flaky(ticker):
            attempts.append(ticker)
            if len(attempts) == 1:
                raise RuntimeError("network down")
            return {"ticker": ticker}

        monkeypatch.setattr(bot, "Company", flaky)
        bot._company_cache.clear()

        with pytest.raises(RuntimeError):
            bot.get_company("AAPL")
        # Second call retries (not cached as failed) and succeeds
        result = bot.get_company("AAPL")
        assert result == {"ticker": "AAPL"}
        assert len(attempts) == 2


# ─── R1 — raw_max default is now 100 ──────────────────────
class TestRawMaxDefault:
    def test_default_raw_max_is_100(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        assert bot.get_cfg()["raw_max"] == 100
