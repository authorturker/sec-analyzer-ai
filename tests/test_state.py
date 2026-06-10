"""
Tests for thread-safe state helpers (_status, _raw_filings) and last_scan_dt.
"""
import json
import threading
from datetime import datetime, timedelta

import pytest


class TestDurumHelpers:
    def test_set_and_snapshot(self, bot):
        bot.status_set(custom_key="hello")
        snap = bot.status_snapshot()
        assert snap["custom_key"] == "hello"

    def test_snapshot_is_a_copy(self, bot):
        snap = bot.status_snapshot()
        snap["tg_errors"] = 999
        # Original should be unchanged
        assert bot.status_snapshot()["tg_errors"] != 999

    def test_inc_default_step(self, bot):
        before = bot.status_snapshot()["total_analyzed"]
        bot.status_inc("total_analyzed")
        assert bot.status_snapshot()["total_analyzed"] == before + 1

    def test_inc_custom_step(self, bot):
        bot.status_inc("tg_errors", by=5)
        assert bot.status_snapshot()["tg_errors"] == 5

    def test_reset_zero(self, bot):
        bot.status_inc("tg_errors", by=10)
        bot.status_reset_zero("tg_errors")
        assert bot.status_snapshot()["tg_errors"] == 0


class TestLastScanDt:
    def test_returns_none_when_unset(self, bot):
        assert bot.last_scan_dt() is None

    def test_returns_datetime_after_set(self, bot):
        now = datetime.now()
        bot.status_set(last_scan=now.isoformat())
        out = bot.last_scan_dt()
        assert out is not None
        assert isinstance(out, datetime)
        # Within a microsecond of what we set
        assert abs((out - now).total_seconds()) < 1

    def test_invalid_iso_returns_none(self, bot):
        bot.status_set(last_scan="not-a-date")
        assert bot.last_scan_dt() is None


class TestHamHelpers:
    def test_kaydet_returns_short_key(self, bot):
        k = bot.store_raw_filing("AAPL", "10-K", "2026", "raw text")
        assert isinstance(k, str)
        assert len(k) == 16

    def test_kaydet_then_al(self, bot):
        k = bot.store_raw_filing("MSFT", "8-K", "2026-05-10", "the body")
        v = bot.get_raw_filing(k)
        assert v["ticker"] == "MSFT"
        assert v["form"]   == "8-K"
        assert v["tarih"]  == "2026-05-10"
        assert v["metin"]  == "the body"

    def test_al_missing_returns_none(self, bot):
        assert bot.get_raw_filing("nonexistent_key") is None

    def test_keys_are_unique(self, bot):
        keys = {bot.store_raw_filing("X", "F", "D", str(i)) for i in range(50)}
        assert len(keys) == 50  # no collisions


class TestThreadSafety:
    """
    Smoke tests — concurrent writes shouldn't corrupt state or lose updates.
    The locks should serialize critical sections.
    """
    def test_status_inc_concurrent(self, bot):
        N_THREADS = 10
        N_OPS     = 100

        def worker():
            for _ in range(N_OPS):
                bot.status_inc("total_analyzed")

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for th in threads: th.start()
        for th in threads: th.join()

        # If lock works correctly, every increment lands.
        assert bot.status_snapshot()["total_analyzed"] == N_THREADS * N_OPS

    def test_store_raw_filing_concurrent(self, bot, monkeypatch):
        # Isolate the concern: race safety, not FIFO eviction. Force the
        # store unlimited here — eviction is covered by TestHamMaxCap.
        monkeypatch.setattr(bot, "_raw_cap", lambda: 0)

        N_THREADS = 8
        N_OPS     = 50
        results: list = []
        results_lock = threading.Lock()

        def worker(tid):
            local_keys = []
            for i in range(N_OPS):
                k = bot.store_raw_filing(f"T{tid}", "F", "D", f"thread{tid}_{i}")
                local_keys.append(k)
            with results_lock:
                results.extend(local_keys)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for th in threads: th.start()
        for th in threads: th.join()

        # All keys distinct → no race lost a write
        assert len(set(results)) == N_THREADS * N_OPS
        # Every key is retrievable (store is unlimited for this test)
        for k in results:
            assert bot.get_raw_filing(k) is not None


class TestHamMaxCap:
    """D3 — opt-in cap on _raw_filings with FIFO eviction."""
    def _set_cap(self, bot, tmp_path, monkeypatch, cap):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.update_cfg(raw_max=cap)

    def test_zero_means_unlimited(self, bot, tmp_path, monkeypatch):
        self._set_cap(bot, tmp_path, monkeypatch, 0)
        for i in range(20):
            bot.store_raw_filing(f"T{i}", "F", "D", f"body{i}")
        with bot._raw_filings_lock:
            assert len(bot._raw_filings) == 20

    def test_cap_evicts_oldest_fifo(self, bot, tmp_path, monkeypatch):
        self._set_cap(bot, tmp_path, monkeypatch, 5)
        keys = [bot.store_raw_filing(f"T{i}", "F", "D", f"body{i}") for i in range(8)]
        with bot._raw_filings_lock:
            assert len(bot._raw_filings) == 5
        # First 3 keys evicted; last 5 remain
        for k in keys[:3]:
            assert bot.get_raw_filing(k) is None
        for k in keys[3:]:
            assert bot.get_raw_filing(k) is not None


class TestPruneCacheExpired:
    """D4 — startup cleanup of stale cache entries."""
    def _setup(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "CACHE_FILE", tmp_path / "cache.json")
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_empty_cache_returns_zero(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        assert bot.prune_cache_expired() == 0

    def test_prunes_only_old_entries(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        fresh_iso = datetime.now().isoformat()
        old_iso   = (datetime.now() - timedelta(days=400)).isoformat()
        bot.save_cache({
            "old_1": {"at": old_iso},
            "old_2": {"at": old_iso},
            "fresh": {"at": fresh_iso},
        })
        pruned = bot.prune_cache_expired(max_age_days=365)
        assert pruned == 2
        remaining = bot.load_cache()
        assert "fresh" in remaining
        assert "old_1" not in remaining and "old_2" not in remaining

    def test_corrupt_timestamp_is_dropped(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        bot.save_cache({
            "bad":  {"at": "not-a-date"},
            "good": {"at": datetime.now().isoformat()},
        })
        pruned = bot.prune_cache_expired(max_age_days=365)
        assert pruned == 1
        assert "good" in bot.load_cache()

    def test_max_age_zero_skips_pruning(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        old_iso = (datetime.now() - timedelta(days=1000)).isoformat()
        bot.save_cache({"old": {"at": old_iso}})
        assert bot.prune_cache_expired(max_age_days=0) == 0
        assert "old" in bot.load_cache()
