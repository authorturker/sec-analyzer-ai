"""
Regression test for the alarm-probe bug + the Item-4 list contract.

Old behavior (broken): the hourly alarm called cmd_sec(quiet=True), which
silently invoked the LLM, wrote to cache + weekly_log, and then announced
'Alert: new filing detected'. The user's subsequent /check found nothing
because the cache was already populated.

Current behavior (probe_new_filings_for_watchlist): existence check only.
- Reads cache to filter already-seen filings.
- Probes EVERY ticker (no short-circuit) and returns a list of
  (ticker, form, date_str) tuples — one per new filing. Empty list = nothing.
- Does NOT call the LLM.
- Does NOT write to cache.
- Does NOT append to weekly_log.
- Does NOT touch previous_filings or _raw_filings.

The list contract is what lets the Item-4 interactive alarm name each new
filing and attach a per-filing [Analyze] button. The pre-Item-4 version
returned a bool and short-circuited on the first hit — see TestProbeReturnsHits
for the tests that pin the new behavior.
"""
import pytest


@pytest.fixture
def probe_env(tmp_path, bot, monkeypatch):
    """Wire bot's persistence paths to a temp dir and reset cache."""
    monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
    monkeypatch.setattr(bot, "CACHE_FILE",  tmp_path / "cache.json")
    monkeypatch.setattr(bot, "WEEKLY_LOG",  tmp_path / "weekly_log.json")
    monkeypatch.setattr(bot, "PREV_DIR",    tmp_path / "previous_filings")
    (tmp_path / "previous_filings").mkdir()
    # The probe sleeps 1s between tickers to be polite to EDGAR — skip it
    # so the suite stays fast even when many tickers are probed.
    monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)
    bot._cfg_cache = None
    yield bot, tmp_path


class TestProbeEmptyWatchlist:
    def test_no_tickers_returns_empty_list(self, probe_env):
        bot, _ = probe_env
        # default watchlist is empty → empty list, not False
        assert bot.probe_new_filings_for_watchlist() == []


class TestProbeSideEffects:
    """When the probe runs, persistence files MUST remain untouched."""

    def test_probe_does_not_create_cache_when_no_tickers(self, probe_env):
        bot, tmp_path = probe_env
        bot.probe_new_filings_for_watchlist()
        # cache file should not exist (probe didn't write to it)
        assert not (tmp_path / "cache.json").exists()

    def test_probe_does_not_create_weekly_log(self, probe_env):
        bot, tmp_path = probe_env
        bot.probe_new_filings_for_watchlist()
        assert not (tmp_path / "weekly_log.json").exists()

    def test_probe_does_not_call_fetch_for_empty_watchlist(self, probe_env, monkeypatch):
        bot, _ = probe_env
        calls = {"n": 0}
        def fake_fetch(*args, **kwargs):
            calls["n"] += 1
            return []
        monkeypatch.setattr(bot, "fetch_new_filings", fake_fetch)
        bot.probe_new_filings_for_watchlist()
        # No tickers → no fetch calls at all
        assert calls["n"] == 0


class TestProbeReturnsHits:
    """Item-4 contract: probe the WHOLE watchlist and return every hit."""

    def test_collects_hits_from_all_tickers(self, probe_env, monkeypatch):
        bot, _ = probe_env
        bot.update_cfg(tickers=["AAPL", "MSFT", "NVDA"])
        calls = []
        def fake_fetch(ticker, forms, lookback_days, **kw):
            calls.append(ticker)
            if ticker == "AAPL":
                return [("10-K", "2026-05-01", "body")]
            if ticker == "MSFT":
                return [("8-K", "2026-05-10", "body")]
            return []  # NVDA: nothing new
        monkeypatch.setattr(bot, "fetch_new_filings", fake_fetch)
        out = bot.probe_new_filings_for_watchlist()
        # Every ticker probed — no short-circuit on the first hit.
        assert calls == ["AAPL", "MSFT", "NVDA"]
        # Hits returned as (ticker, form, date_str) tuples, in watchlist order.
        assert out == [
            ("AAPL", "10-K", "2026-05-01"),
            ("MSFT", "8-K",  "2026-05-10"),
        ]

    def test_multiple_hits_from_one_ticker(self, probe_env, monkeypatch):
        bot, _ = probe_env
        bot.update_cfg(tickers=["AAPL"])
        def fake_fetch(ticker, forms, lookback_days, **kw):
            return [
                ("10-Q", "2026-05-02", "body1"),
                ("8-K",  "2026-05-03", "body2"),
            ]
        monkeypatch.setattr(bot, "fetch_new_filings", fake_fetch)
        out = bot.probe_new_filings_for_watchlist()
        assert out == [
            ("AAPL", "10-Q", "2026-05-02"),
            ("AAPL", "8-K",  "2026-05-03"),
        ]

    def test_returns_empty_when_nothing_found(self, probe_env, monkeypatch):
        bot, _ = probe_env
        bot.update_cfg(tickers=["AAPL", "MSFT"])
        monkeypatch.setattr(bot, "fetch_new_filings", lambda *a, **k: [])
        assert bot.probe_new_filings_for_watchlist() == []

    def test_probe_does_not_call_llm(self, probe_env, monkeypatch):
        """If the LLM were called, the test would crash on a network
        request — but probe must not invoke it at all, even on a hit."""
        bot, _ = probe_env
        bot.update_cfg(tickers=["AAPL"])
        llm_calls = {"n": 0}
        def fake_llm(*a, **k):
            llm_calls["n"] += 1
            return "should not be called"
        monkeypatch.setattr(bot, "llm", fake_llm)
        monkeypatch.setattr(bot, "fetch_new_filings",
                            lambda *a, **k: [("10-K", "2026-05-01", "body")])
        bot.probe_new_filings_for_watchlist()
        assert llm_calls["n"] == 0


class TestProbePassesCacheToFetch:
    """The probe must give fetch_new_filings the current cache so that
    already-processed filings are filtered out."""

    def test_cache_passed_through(self, probe_env, monkeypatch):
        bot, _ = probe_env
        bot.update_cfg(tickers=["AAPL"])
        # Pre-populate the cache
        bot.save_cache({"abc123": {"at": "2026-05-01T12:00:00"}})

        seen = {}
        def fake_fetch(ticker, forms, lookback_days,
                       cache_dict=None, use_cache=False, quiet=False, **kw):
            seen["cache_dict"] = cache_dict
            seen["use_cache"]  = use_cache
            return []
        monkeypatch.setattr(bot, "fetch_new_filings", fake_fetch)
        bot.probe_new_filings_for_watchlist()
        assert seen["use_cache"] is True
        assert seen["cache_dict"] == {"abc123": {"at": "2026-05-01T12:00:00"}}
