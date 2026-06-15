"""tests/test_chatcfg_locking.py — M1 per-chat config lock discipline tests.

Coverage:
- get_chat_cfg snapshot consistency with _has_chat_cfg flip-mid-read mock
- init_chat_config idempotency: two calls → single file, defaults, no exception
- mutate_chat_cfg routing: per-chat write stays in chat file, doesn't leak to global
- Lock holding: structural proof via threading wrapper that single acquire covers global+chat
"""

import copy
import json
import threading
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_chat_cfg(tmp_path, bot, monkeypatch):
    """Set up isolated config + chat dir for per-chat tests."""
    cfg_path = tmp_path / "bot_config.json"
    chat_dir = tmp_path / "chats"
    chat_dir.mkdir()
    monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
    monkeypatch.setattr(bot, "CHAT_DIR", chat_dir)
    bot._cfg_cache = None
    yield bot, cfg_path, chat_dir


class TestGetChatCfgSnapshot:
    """get_chat_cfg returns self-consistent snapshot even when _has_chat_cfg
    would flip mid-execution under the old (unlocked) pattern."""

    def test_has_chat_cfg_flips_to_false_still_consistent(self, tmp_chat_cfg, monkeypatch):
        bot, _, chat_dir = tmp_chat_cfg

        cid = "111"
        chat_file = bot._chat_cfg_path(cid)
        chat_file.write_text(json.dumps({"tickers": ["AAPL"]}), encoding="utf-8")

        call_count = 0
        original_has = bot._has_chat_cfg

        def flipping_has(chat_id):
            nonlocal call_count
            if chat_id == cid:
                call_count += 1
                return call_count == 1
            return original_has(chat_id)

        monkeypatch.setattr(bot, "_has_chat_cfg", flipping_has)
        bot._ctx.chat_id = cid
        try:
            result = bot.get_chat_cfg()
            assert isinstance(result, dict)
            if "AAPL" in result.get("tickers", []):
                assert result["tickers"] == ["AAPL"]
            else:
                assert "tickers" in result
        finally:
            bot._ctx.chat_id = None

    def test_has_chat_cfg_flips_to_true_still_consistent(self, tmp_chat_cfg, monkeypatch):
        """_has_chat_cfg returns False first, then True — no exception."""
        bot, _, _ = tmp_chat_cfg

        call_count = 0

        def flipping_has(chat_id):
            nonlocal call_count
            if chat_id == "999":
                call_count += 1
                return call_count > 1
            return False

        monkeypatch.setattr(bot, "_has_chat_cfg", flipping_has)
        bot._ctx.chat_id = "999"
        try:
            result = bot.get_chat_cfg()
            assert isinstance(result, dict)
            assert "tickers" in result
        finally:
            bot._ctx.chat_id = None


class TestInitChatConfigIdempotency:
    """init_chat_config called twice on same chat_id → single file, defaults."""

    def test_two_calls_single_file(self, tmp_chat_cfg):
        bot, _, chat_dir = tmp_chat_cfg
        cid = "200"

        bot.init_chat_config(cid)
        bot.init_chat_config(cid)

        chat_files = list(chat_dir.glob("chat_*.json"))
        assert len(chat_files) == 1

        data = json.loads(chat_files[0].read_text(encoding="utf-8"))
        assert data == bot._CHAT_DEFAULTS

    def test_no_exception_on_second_call(self, tmp_chat_cfg):
        bot, _, _ = tmp_chat_cfg
        bot.init_chat_config("300")
        bot.init_chat_config("300")
        bot.init_chat_config("300")

    def test_defaults_content_match(self, tmp_chat_cfg):
        bot, _, _ = tmp_chat_cfg
        bot.init_chat_config("400")
        bot._ctx.chat_id = "400"
        try:
            cfg = bot.get_chat_cfg()
            for k, v in bot._CHAT_DEFAULTS.items():
                assert cfg.get(k) == v, f"Key {k} mismatch after init"
        finally:
            bot._ctx.chat_id = None


class TestMutateChatCfgRouting:
    """Per-chat keys stay in chat file; global keys stay in global config."""

    def test_per_chat_key_writes_to_chat_file(self, tmp_chat_cfg):
        bot, cfg_path, chat_dir = tmp_chat_cfg
        cid = "500"
        bot.init_chat_config(cid)
        bot._ctx.chat_id = cid
        try:
            bot.mutate_chat_cfg(lambda c: c["tickers"].extend(["GOOG"]))
        finally:
            bot._ctx.chat_id = None

        chat_data = json.loads(bot._chat_cfg_path(cid).read_text(encoding="utf-8"))
        assert "GOOG" in chat_data.get("tickers", [])

        global_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "GOOG" not in global_data.get("tickers", [])

    def test_global_key_writes_to_global_file(self, tmp_chat_cfg):
        bot, cfg_path, chat_dir = tmp_chat_cfg
        cid = "600"
        bot.init_chat_config(cid)
        bot._ctx.chat_id = cid
        try:
            bot.mutate_chat_cfg(lambda c: c.update({"language": "tr"}))
        finally:
            bot._ctx.chat_id = None

        global_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert global_data.get("language") == "tr"

        chat_data = json.loads(bot._chat_cfg_path(cid).read_text(encoding="utf-8"))
        assert "language" not in chat_data

    def test_global_fallback_when_no_chat_file(self, tmp_chat_cfg):
        bot, cfg_path, _ = tmp_chat_cfg
        cid = "700"
        bot._ctx.chat_id = cid
        try:
            bot.mutate_chat_cfg(lambda c: c.update({"language": "tr"}))
        finally:
            bot._ctx.chat_id = None

        global_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert global_data.get("language") == "tr"

    def test_mixed_mutation(self, tmp_chat_cfg):
        """Both per-chat and global keys changed in one mutation."""
        bot, cfg_path, chat_dir = tmp_chat_cfg
        cid = "800"
        bot.init_chat_config(cid)
        bot._ctx.chat_id = cid
        try:
            def mixed(c):
                c["tickers"].append("TSLA")
                c["language"] = "tr"
            bot.mutate_chat_cfg(mixed)
        finally:
            bot._ctx.chat_id = None

        chat_data = json.loads(bot._chat_cfg_path(cid).read_text(encoding="utf-8"))
        assert "TSLA" in chat_data.get("tickers", [])

        global_data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert global_data.get("language") == "tr"


class TestLockHoldingProof:
    """Verify that get_chat_cfg and mutate_chat_cfg hold _cfg_lock for the
    full operation by wrapping the lock with a counting proxy."""

    def _install_counter(self, bot):
        """Replace _cfg_lock with a counting wrapper. Returns (wrapper, counter)."""
        counter = {"acquires": 0}
        real_lock = bot._cfg_lock

        class _CountingRLock:
            def acquire(self, blocking=True, timeout=-1):
                counter["acquires"] += 1
                return real_lock.acquire(blocking=blocking, timeout=timeout)

            def release(self):
                return real_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *args):
                self.release()

            @property
            def _owner(self):
                return real_lock._owner

            @property
            def _count(self):
                return real_lock._count

        wrapper = _CountingRLock()
        bot._cfg_lock = wrapper
        return wrapper, counter, real_lock

    def test_single_lock_acquire_for_per_chat(self, tmp_chat_cfg):
        bot, _, _ = tmp_chat_cfg
        cid = "900"
        bot.init_chat_config(cid)
        bot._ctx.chat_id = cid

        wrapper, counter, real_lock = self._install_counter(bot)
        try:
            bot.get_chat_cfg()
            assert counter["acquires"] == 1, (
                f"Expected 1 external acquire, got {counter['acquires']}"
            )
        finally:
            bot._cfg_lock = real_lock
            bot._ctx.chat_id = None

    def test_single_lock_acquire_for_no_chat(self, tmp_chat_cfg):
        """When no per-chat file exists, still exactly one lock acquire."""
        bot, _, _ = tmp_chat_cfg
        bot._ctx.chat_id = "noexist"

        wrapper, counter, real_lock = self._install_counter(bot)
        try:
            bot.get_chat_cfg()
            assert counter["acquires"] == 1
        finally:
            bot._cfg_lock = real_lock
            bot._ctx.chat_id = None

    def test_single_lock_acquire_mutate(self, tmp_chat_cfg):
        """mutate_chat_cfg also holds the lock once externally."""
        bot, _, _ = tmp_chat_cfg
        cid = "910"
        bot.init_chat_config(cid)
        bot._ctx.chat_id = cid

        wrapper, counter, real_lock = self._install_counter(bot)
        try:
            bot.mutate_chat_cfg(lambda c: c["tickers"].append("X"))
            assert counter["acquires"] == 1
        finally:
            bot._cfg_lock = real_lock
            bot._ctx.chat_id = None
