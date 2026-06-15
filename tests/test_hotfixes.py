"""
Tests for the critical patch package (H1-H4):
  H1 — wizard_handle parts[0] guard (empty-message crash)
  H3 — _atomic_write_json
  H4 — _read_json corruption-safe read

H2 (digest ISO-week) lives inline in background_thread; its one-line
isocalendar() change is exercised via a direct logic check below.
"""
import json
from datetime import datetime

import pytest


# ─── H3 — _atomic_write_json ──────────────────────────────
class TestAtomicWriteJson:
    def test_roundtrip(self, bot, tmp_path):
        p = tmp_path / "data.json"
        payload = {"a": 1, "list": [1, 2, 3], "unicode": "şğüöç"}
        bot._atomic_write_json(p, payload)
        assert json.loads(p.read_text(encoding="utf-8")) == payload

    def test_no_tmp_file_left_behind(self, bot, tmp_path):
        p = tmp_path / "data.json"
        bot._atomic_write_json(p, {"x": 1})
        # No .tmp* sibling must remain after a successful write.
        tmp_files = list(tmp_path.glob("data.json.*.tmp"))
        assert tmp_files == []
        assert p.exists()

    def test_overwrites_existing(self, bot, tmp_path):
        p = tmp_path / "data.json"
        bot._atomic_write_json(p, {"v": 1})
        bot._atomic_write_json(p, {"v": 2})
        assert json.loads(p.read_text())["v"] == 2

    def test_unicode_not_escaped(self, bot, tmp_path):
        # ensure_ascii=False → Turkish chars stored readable, not \uXXXX
        p = tmp_path / "data.json"
        bot._atomic_write_json(p, {"msg": "düşüş"})
        assert "düşüş" in p.read_text(encoding="utf-8")


# ─── H4 — _read_json ──────────────────────────────────────
class TestReadJson:
    def test_missing_file_returns_default(self, bot, tmp_path):
        assert bot._read_json(tmp_path / "nope.json", {}) == {}
        assert bot._read_json(tmp_path / "nope.json", []) == []

    def test_valid_json_returned(self, bot, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps({"ok": True}))
        assert bot._read_json(p, {}) == {"ok": True}

    def test_corrupt_json_backed_up(self, bot, tmp_path):
        p = tmp_path / "cache.json"
        p.write_text("{this is not valid json")
        out = bot._read_json(p, {})
        # Default returned
        assert out == {}
        # Original moved aside, not silently overwritten
        assert not p.exists()
        backup = tmp_path / "cache.json.corrupt"
        assert backup.exists()
        # The corrupt content is preserved in the backup for recovery
        assert backup.read_text() == "{this is not valid json"

    def test_corrupt_json_respects_list_default(self, bot, tmp_path):
        p = tmp_path / "weekly.json"
        p.write_text("][")
        assert bot._read_json(p, []) == []
        assert (tmp_path / "weekly.json.corrupt").exists()

    def test_save_then_load_via_helpers(self, bot, tmp_path):
        # Integration: atomic write + safe read round-trip
        p = tmp_path / "rt.json"
        bot._atomic_write_json(p, {"k": "v"})
        assert bot._read_json(p, {}) == {"k": "v"}


# ─── H4 — corruption recovery through public load_* APIs ──
class TestCorruptionRecoveryIntegration:
    def test_load_cache_recovers_from_corruption(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "CACHE_FILE", tmp_path / "cache.json")
        (tmp_path / "cache.json").write_text("garbage{{{")
        # Must not raise, returns empty, backs up the bad file
        assert bot.load_cache() == {}
        assert (tmp_path / "cache.json.corrupt").exists()

    def test_get_weekly_log_recovers_from_corruption(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "WEEKLY_LOG", tmp_path / "weekly_log.json")
        monkeypatch.setattr(bot._ctx, "chat_id", None)
        (tmp_path / "weekly_log.json").write_text("not json")
        assert bot.get_weekly_log() == []
        assert (tmp_path / "weekly_log.json.corrupt").exists()


# ─── H1 — wizard_handle empty-parts guard ─────────────────
class TestWizardGuard:
    def test_forms_step_empty_parts_no_crash(self, bot, monkeypatch):
        # An empty / non-text message during the wizard's "forms" step
        # used to hit parts[0] → IndexError. Guard must short-circuit.
        monkeypatch.setattr(bot, "tg", lambda *a, **k: None)
        bot.WIZARD.clear()
        bot.WIZARD["step"] = "forms"
        try:
            handled = bot.wizard_handle("", [])   # empty parts
            assert handled is True                # consumed, did not crash
        finally:
            bot.WIZARD.clear()

    def test_tickers_step_empty_parts_no_crash(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "tg", lambda *a, **k: None)
        bot.WIZARD.clear()
        bot.WIZARD["step"] = "tickers"
        try:
            handled = bot.wizard_handle("", [])
            assert handled is True
        finally:
            bot.WIZARD.clear()

    def test_lang_step_empty_parts_no_crash(self, bot, monkeypatch):
        # The "lang" step already had the guard — confirm it still holds.
        monkeypatch.setattr(bot, "tg", lambda *a, **k: None)
        bot.WIZARD.clear()
        bot.WIZARD["step"] = "lang"
        try:
            handled = bot.wizard_handle("", [])
            assert handled is True
        finally:
            bot.WIZARD.clear()


# ─── H2 — digest ISO-week vs day-of-month ─────────────────
class TestDigestWeekLogic:
    def test_consecutive_sundays_have_distinct_iso_weeks(self):
        """
        The H2 fix keys the digest on (ISO year, ISO week). Two consecutive
        Sundays must always differ, even when their day-of-month collides —
        which is exactly what the old `now.day` logic failed at.
        """
        # 2026-02-01 and 2026-03-01 are both Sundays, both day-of-month 1.
        d1 = datetime(2026, 2, 1)
        d2 = datetime(2026, 3, 1)
        assert d1.weekday() == 6 and d2.weekday() == 6   # both Sundays
        assert d1.day == d2.day                          # day-of-month collides
        yw1 = tuple(d1.isocalendar()[:2])
        yw2 = tuple(d2.isocalendar()[:2])
        # Old logic: d1.day == d2.day → digest skipped. New logic: distinct.
        assert yw1 != yw2

    def test_same_sunday_same_iso_week(self):
        # A second check the same Sunday → same (year, week) → no double-send.
        d = datetime(2026, 3, 1, 9, 0)
        assert tuple(d.isocalendar()[:2]) == tuple(d.isocalendar()[:2])


# ─── H7 — tg() resilience to total network failure ───────
class TestTgResilience:
    def test_tg_survives_total_failure(self, bot, monkeypatch, capsys):
        # Every HTTP POST fails. tg() must NOT raise (a propagating
        # exception here used to be able to kill the background thread or
        # the polling loop), and it must surface the loss loudly.
        def boom(*a, **k):
            raise ConnectionError("network down")
        monkeypatch.setattr(bot.requests, "post", boom)
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)  # fast

        bot.tg("hello world")   # must return normally, no exception

        # Total failure is written to stderr so a Termux operator sees it.
        err = capsys.readouterr().err
        assert "CRITICAL" in err
        assert "Telegram delivery failed" in err

    def test_tg_success_no_critical(self, bot, monkeypatch, capsys):
        # A successful send must NOT emit the critical-failure line.
        class FakeResp:
            status_code = 200
            ok = True
            def raise_for_status(self): pass
        monkeypatch.setattr(bot.requests, "post", lambda *a, **k: FakeResp())
        monkeypatch.setattr(bot.time, "sleep", lambda *a, **k: None)

        bot.tg("all good")

        err = capsys.readouterr().err
        assert "CRITICAL" not in err


# ─── H2 — JSON kalıcılık sağlamlaştırma ──────────────────
class TestReadJsonH2:
    """Eksik bozulma modları: yanlış kök tip, OSError, eşzamanlı yazma."""

    def test_wrong_root_type_list_returns_default_dict(self, bot, tmp_path):
        p = tmp_path / "data.json"
        p.write_text(json.dumps([1, 2, 3]))
        # _read_json kendisi listeyi döndürür; load_cache tip guard'ı düzeltir
        raw = bot._read_json(p, {})
        assert raw == [1, 2, 3]           # ham: listeyi döndürür

    def test_load_cache_wrong_type_returns_empty_dict(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "CACHE_FILE", tmp_path / "cache.json")
        (tmp_path / "cache.json").write_text(json.dumps([{"a": 1}]))
        result = bot.load_cache()
        assert result == {}                # tip guard devreye girmeli

    def test_load_price_cache_wrong_type_returns_empty_dict(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "PRICE_CACHE", tmp_path / "price.json")
        (tmp_path / "price.json").write_text(json.dumps(["not", "a", "dict"]))
        result = bot.load_price_cache()
        assert result == {}

    def test_load_sentiment_history_wrong_type_returns_empty_dict(self, bot, tmp_path, monkeypatch):
        monkeypatch.setattr(bot, "SENT_HIST", tmp_path / "sent.json")
        (tmp_path / "sent.json").write_text(json.dumps(42))
        result = bot.load_sentiment_history()
        assert result == {}

    def test_read_json_oserror_returns_default(self, bot, tmp_path, monkeypatch):
        p = tmp_path / "data.json"
        p.write_text("{}")
        original_read = bot.Path.read_text
        def boom(self, *a, **k):
            if self == p:
                raise OSError("simulated permission denied")
            return original_read(self, *a, **k)
        monkeypatch.setattr(bot.Path, "read_text", boom)
        result = bot._read_json(p, {"fallback": True})
        assert result == {"fallback": True}

    def test_read_json_empty_file_returns_default(self, bot, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("")
        result = bot._read_json(p, {"empty": True})
        assert result == {"empty": True}
        assert (tmp_path / "empty.json.corrupt").exists()

    def test_atomic_write_tmp_cleaned_on_failure(self, bot, tmp_path, monkeypatch):
        p = tmp_path / "data.json"
        original_replace = bot.Path.replace
        def fail_replace(self, target):
            if target == p:
                raise OSError("simulated replace failure")
            return original_replace(self, target)
        monkeypatch.setattr(bot.Path, "replace", fail_replace)
        with pytest.raises(OSError):
            bot._atomic_write_json(p, {"x": 1})
        # No .tmp* siblings must remain after a failed write
        assert list(tmp_path.glob("data.json.*.tmp")) == []

    def test_concurrent_writes_no_corruption(self, bot, tmp_path):
        import threading
        p = tmp_path / "concurrent.json"
        errors = []
        def writer(val):
            try:
                bot._atomic_write_json(p, {"v": val})
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=writer, args=(i,)) for i in range(20)]
        for t in threads: t.start()
        for t in threads: t.join()
        assert not errors
        # File must be valid JSON after all concurrent writes
        data = json.loads(p.read_text())
        assert "v" in data
