"""
Tests for the config layer: in-memory cache, atomic mutate, isolation,
defaults, and persistence.
"""
import json
import threading
from pathlib import Path

import pytest


@pytest.fixture
def tmp_cfg(tmp_path, bot, monkeypatch):
    """Point bot.CONFIG_FILE at a clean temp file and reset the cache."""
    cfg_path = tmp_path / "bot_config.json"
    monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
    bot._cfg_cache = None
    yield bot, cfg_path


class TestDefaults:
    def test_defaults_applied_on_first_load(self, tmp_cfg):
        bot, _ = tmp_cfg
        cfg = bot.get_cfg()
        assert cfg["language"] == "en"
        assert cfg["days_lookback"] == 35
        assert cfg["max_chars"] == 10000
        assert cfg["default_forms"] == ["10-K", "10-Q", "8-K", "4"]
        assert cfg["custom_prompts"] == {}
        assert cfg["tickers"] == []

    def test_explicit_value_wins_over_default(self, tmp_cfg):
        bot, p = tmp_cfg
        p.write_text(json.dumps({"language": "tr", "days_lookback": 60}))
        bot._cfg_cache = None  # force re-read
        cfg = bot.get_cfg()
        assert cfg["language"] == "tr"
        assert cfg["days_lookback"] == 60
        # other defaults still applied
        assert cfg["max_chars"] == 10000


class TestSnapshotIsolation:
    def test_mutating_snapshot_does_not_affect_canonical(self, tmp_cfg):
        bot, _ = tmp_cfg
        snap = bot.get_cfg()
        snap["tickers"].append("HACK")
        snap["custom_prompts"]["10-K"] = "evil"
        # Re-read — snapshot mutations must not have leaked
        fresh = bot.get_cfg()
        assert "HACK" not in fresh["tickers"]
        assert fresh["custom_prompts"] == {}

    def test_two_snapshots_are_independent(self, tmp_cfg):
        bot, _ = tmp_cfg
        a = bot.get_cfg()
        b = bot.get_cfg()
        a["tickers"].append("AAPL")
        assert b["tickers"] == []


class TestMutateCfg:
    def test_simple_set(self, tmp_cfg):
        bot, p = tmp_cfg
        bot.mutate_cfg(lambda c: c.update({"language": "tr"}))
        assert bot.get_cfg()["language"] == "tr"
        # Persisted to disk
        assert json.loads(p.read_text())["language"] == "tr"

    def test_nested_mutation(self, tmp_cfg):
        bot, _ = tmp_cfg
        bot.mutate_cfg(lambda c: c["custom_prompts"].update({"10-K": "test"}))
        assert bot.get_cfg()["custom_prompts"]["10-K"] == "test"

    def test_list_mutation(self, tmp_cfg):
        bot, _ = tmp_cfg
        bot.mutate_cfg(lambda c: c["tickers"].extend(["AAPL", "MSFT"]))
        assert bot.get_cfg()["tickers"] == ["AAPL", "MSFT"]

    def test_returns_post_state(self, tmp_cfg):
        bot, _ = tmp_cfg
        new_state = bot.mutate_cfg(lambda c: c.update({"days_lookback": 99}))
        assert new_state["days_lookback"] == 99


class TestUpdateCfg:
    def test_update_cfg_sets_keys(self, tmp_cfg):
        bot, _ = tmp_cfg
        bot.update_cfg(language="tr", days_lookback=60)
        cfg = bot.get_cfg()
        assert cfg["language"] == "tr"
        assert cfg["days_lookback"] == 60


class TestPersistence:
    def test_changes_persist_across_cache_reset(self, tmp_cfg):
        bot, _ = tmp_cfg
        bot.update_cfg(language="tr")
        bot._cfg_cache = None  # simulate restart
        cfg = bot.get_cfg()
        assert cfg["language"] == "tr"


class TestRaceProtection:
    def test_concurrent_appends_all_persist(self, tmp_cfg):
        """Without mutate_cfg's atomic lock, concurrent read-modify-write
        would lose updates to the last writer."""
        bot, _ = tmp_cfg
        N_THREADS = 8
        N_OPS     = 25

        def worker(tid):
            for i in range(N_OPS):
                bot.mutate_cfg(lambda c, k=f"T{tid}-{i}":
                               c["tickers"].append(k))

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(N_THREADS)]
        for th in threads: th.start()
        for th in threads: th.join()

        cfg = bot.get_cfg()
        # Every increment landed — no lost writes
        assert len(cfg["tickers"]) == N_THREADS * N_OPS
        # All distinct
        assert len(set(cfg["tickers"])) == N_THREADS * N_OPS
