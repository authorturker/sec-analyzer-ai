"""tests/test_multichat.py — I1 multi-chat support (Model A) offline tests.

Coverage:
- Config migration (scalar → list, idempotent, env bootstrap)
- Auth helpers (_is_authorized, _is_admin)
- broadcast() error isolation
- Admin commands (addchat, removechat, listchats)
- i18n key parity EN == TR
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, call


# ─── helpers ──────────────────────────────────────────────────────────────────

def _reset_cfg(bot):
    """Clear the in-memory config cache so tests start from a known state."""
    import bot as _bot_mod
    _bot_mod._cfg_cache = None


# ─── Chat ID migration ────────────────────────────────────────────────────────

class TestChatMigration:
    """_migrate_chat_ids() bootstraps chat_ids list from various starting states."""

    def test_migrate_from_env(self, tmp_path, monkeypatch):
        """No chat_ids in config, valid TELEGRAM_CHAT_ID env → bootstrap."""
        import bot
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "111222333")
        bot._cfg_cache = None

        bot._migrate_chat_ids()

        ids = bot.get_cfg_value("chat_ids", [])
        assert ids == ["111222333"]

    def test_migrate_from_legacy_key(self, tmp_path, monkeypatch):
        """Config has old 'chat_id' scalar → promote to list, remove key."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_id": "999888777"}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
        bot._cfg_cache = None

        bot._migrate_chat_ids()

        cfg = bot.get_cfg()
        assert cfg.get("chat_ids") == ["999888777"]
        assert "chat_id" not in cfg

    def test_migrate_idempotent_existing_list(self, tmp_path, monkeypatch):
        """chat_ids already in config → no change."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(
            json.dumps({"chat_ids": ["111", "222"]}), encoding="utf-8"
        )
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "999")
        bot._cfg_cache = None

        bot._migrate_chat_ids()

        ids = bot.get_cfg_value("chat_ids", [])
        assert ids == ["111", "222"]   # env var NOT prepended

    def test_migrate_cleans_stale_scalar(self, tmp_path, monkeypatch):
        """chat_ids present AND stale chat_id key present → stale key removed."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(
            json.dumps({"chat_ids": ["111"], "chat_id": "111"}), encoding="utf-8"
        )
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
        bot._cfg_cache = None

        bot._migrate_chat_ids()

        cfg = bot.get_cfg()
        assert "chat_id" not in cfg
        assert cfg["chat_ids"] == ["111"]

    def test_migrate_env_missing_no_crash(self, tmp_path, monkeypatch):
        """No chat_ids, placeholder TELEGRAM_CHAT_ID → empty list, no crash."""
        import bot
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")
        bot._cfg_cache = None

        bot._migrate_chat_ids()   # must not raise

        ids = bot.get_cfg_value("chat_ids", [])
        assert ids == []


# ─── Auth helpers ─────────────────────────────────────────────────────────────

class TestAuthHelpers:
    def _set_chat_ids(self, bot, ids, tmp_path, monkeypatch):
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_ids": ids}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

    def test_authorized_chat(self, tmp_path, monkeypatch):
        import bot
        self._set_chat_ids(bot, ["100", "200", "300"], tmp_path, monkeypatch)
        assert bot._is_authorized("200") is True

    def test_unauthorized_chat(self, tmp_path, monkeypatch):
        import bot
        self._set_chat_ids(bot, ["100", "200"], tmp_path, monkeypatch)
        assert bot._is_authorized("999") is False

    def test_empty_list_all_unauthorized(self, tmp_path, monkeypatch):
        import bot
        self._set_chat_ids(bot, [], tmp_path, monkeypatch)
        assert bot._is_authorized("100") is False

    def test_is_admin_first_element(self, tmp_path, monkeypatch):
        import bot
        self._set_chat_ids(bot, ["100", "200"], tmp_path, monkeypatch)
        assert bot._is_admin("100") is True
        assert bot._is_admin("200") is False

    def test_is_admin_empty_list(self, tmp_path, monkeypatch):
        import bot
        self._set_chat_ids(bot, [], tmp_path, monkeypatch)
        assert bot._is_admin("100") is False

    def test_is_authorized_int_string_coercion(self, tmp_path, monkeypatch):
        """chat_ids stored as ints should still match string chat_id from Telegram."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_ids": [123456789]}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None
        assert bot._is_authorized("123456789") is True


# ─── Broadcast isolation ──────────────────────────────────────────────────────

class TestBroadcastIsolation:
    def test_all_chats_receive(self, tmp_path, monkeypatch):
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(
            json.dumps({"chat_ids": ["111", "222", "333"]}), encoding="utf-8"
        )
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

        received = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text: received.append(cid))
        bot.broadcast("hello")

        assert received == ["111", "222", "333"]

    def test_error_in_middle_does_not_block_others(self, tmp_path, monkeypatch):
        """If sending to chat 222 raises, 111 and 333 still get the message."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(
            json.dumps({"chat_ids": ["111", "222", "333"]}), encoding="utf-8"
        )
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

        received = []
        def _mock_tg_to(cid, text):
            if cid == "222":
                raise RuntimeError("bot blocked")
            received.append(cid)

        monkeypatch.setattr(bot, "_tg_to", _mock_tg_to)
        bot.broadcast("hello")   # must not raise

        assert received == ["111", "333"]

    def test_broadcast_empty_list_no_send(self, tmp_path, monkeypatch):
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_ids": []}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

        calls = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text: calls.append(cid))
        bot.broadcast("hello")
        assert calls == []


# ─── tg() context routing ─────────────────────────────────────────────────────

class TestTgContextRouting:
    def test_reactive_context_sends_to_one(self, tmp_path, monkeypatch):
        """When _ctx.chat_id is set, tg() calls _tg_to with that chat only."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_ids": ["111", "222"]}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text: sent.append(cid))

        bot._ctx.chat_id = "111"
        try:
            bot.tg("hello")
        finally:
            bot._ctx.chat_id = None

        assert sent == ["111"]

    def test_no_context_broadcasts(self, tmp_path, monkeypatch):
        """When _ctx.chat_id is not set, tg() broadcasts to all."""
        import bot
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_ids": ["111", "222"]}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

        sent = []
        monkeypatch.setattr(bot, "_tg_to", lambda cid, text: sent.append(cid))
        bot._ctx.chat_id = None
        bot.tg("hello")

        assert sent == ["111", "222"]


# ─── Admin commands ───────────────────────────────────────────────────────────

class TestAdminCommands:
    def _make_cfg(self, bot, ids, tmp_path, monkeypatch):
        cfg_path = tmp_path / "bot_config.json"
        cfg_path.write_text(json.dumps({"chat_ids": ids}), encoding="utf-8")
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        bot._cfg_cache = None

    def test_addchat_success(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100"], tmp_path, monkeypatch)
        result = bot.cmd_addchat(["/addchat", "200"], "100")
        assert "200" in result
        assert "200" in [str(c) for c in bot.get_cfg()["chat_ids"]]

    def test_addchat_cap_enforced(self, tmp_path, monkeypatch):
        import bot
        ids = [str(i) for i in range(100, 100 + bot._CHAT_MAX)]
        self._make_cfg(bot, ids, tmp_path, monkeypatch)
        result = bot.cmd_addchat(["/addchat", "999"], "100")
        assert str(bot._CHAT_MAX) in result or "limit" in result.lower() or "Limit" in result

    def test_addchat_duplicate(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100", "200"], tmp_path, monkeypatch)
        result = bot.cmd_addchat(["/addchat", "200"], "100")
        # Should report already exists, not add a second entry
        assert bot.get_cfg()["chat_ids"].count("200") == 1

    def test_addchat_invalid_format(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100"], tmp_path, monkeypatch)
        result = bot.cmd_addchat(["/addchat", "notanid"], "100")
        assert "invalid" in result.lower() or "geçersiz" in result.lower() or "error" in result.lower()

    def test_addchat_missing_arg(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100"], tmp_path, monkeypatch)
        result = bot.cmd_addchat(["/addchat"], "100")
        assert result  # some error message returned

    def test_addchat_negative_id_valid(self, tmp_path, monkeypatch):
        """Telegram group IDs are negative — must be accepted."""
        import bot
        self._make_cfg(bot, ["100"], tmp_path, monkeypatch)
        result = bot.cmd_addchat(["/addchat", "-1001234567890"], "100")
        assert "-1001234567890" in [str(c) for c in bot.get_cfg()["chat_ids"]]

    def test_removechat_success(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100", "200", "300"], tmp_path, monkeypatch)
        result = bot.cmd_removechat(["/removechat", "200"], "100")
        assert "200" not in [str(c) for c in bot.get_cfg()["chat_ids"]]

    def test_removechat_self_remove_blocked(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100", "200"], tmp_path, monkeypatch)
        result = bot.cmd_removechat(["/removechat", "100"], "100")
        # self-remove blocked; admin still in list
        assert "100" in [str(c) for c in bot.get_cfg()["chat_ids"]]
        assert "remove" in result.lower() or "sile" in result.lower() or "admin" in result.lower()

    def test_removechat_not_found(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100", "200"], tmp_path, monkeypatch)
        result = bot.cmd_removechat(["/removechat", "999"], "100")
        assert "not" in result.lower() or "yok" in result.lower() or "found" in result.lower()

    def test_removechat_invalid_format(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100"], tmp_path, monkeypatch)
        result = bot.cmd_removechat(["/removechat", "abc"], "100")
        assert result

    def test_listchats_shows_all(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, ["100", "200", "300"], tmp_path, monkeypatch)
        result = bot.cmd_listchats()
        assert "100" in result
        assert "200" in result
        assert "300" in result

    def test_listchats_admin_marker(self, tmp_path, monkeypatch):
        """First chat gets the admin marker (⭐)."""
        import bot
        self._make_cfg(bot, ["100", "200"], tmp_path, monkeypatch)
        result = bot.cmd_listchats()
        lines = result.split("\n")
        admin_line = next(l for l in lines if "100" in l)
        assert "⭐" in admin_line

    def test_listchats_empty(self, tmp_path, monkeypatch):
        import bot
        self._make_cfg(bot, [], tmp_path, monkeypatch)
        result = bot.cmd_listchats()
        assert result  # non-empty message


# ─── i18n parity ──────────────────────────────────────────────────────────────

class TestI18nParity:
    def _load(self, path):
        text = Path(path).read_text(encoding="utf-8")
        d = json.loads(text)
        return {k for k in d if not k.startswith("_")}

    def test_en_tr_same_keys(self):
        root = Path(__file__).parent.parent / "lang"
        en_keys = self._load(root / "en.json")
        tr_keys = self._load(root / "tr.json")
        missing_in_tr = en_keys - tr_keys
        missing_in_en = tr_keys - en_keys
        assert not missing_in_tr, f"Missing in TR: {sorted(missing_in_tr)}"
        assert not missing_in_en, f"Missing in EN: {sorted(missing_in_en)}"

    def test_new_multichat_keys_present(self):
        """Smoke: all I1 keys exist in both lang files."""
        root = Path(__file__).parent.parent / "lang"
        new_keys = {
            "addchat_confirm", "addchat_limit", "addchat_format_error",
            "addchat_already_exists", "removechat_confirm", "removechat_not_found",
            "removechat_self_remove", "removechat_format_error",
            "listchats_header", "listchats_row", "listchats_empty",
            "unauthorized_admin",
        }
        for lang_file in ["en.json", "tr.json"]:
            keys = self._load(root / lang_file)
            missing = new_keys - keys
            assert not missing, f"{lang_file} missing I1 keys: {sorted(missing)}"


# ─── M2: Per-chat data purge on removechat ───────────────────────────────────

class TestPurgeChatData:
    """_purge_chat_data removes config + weekly log artifacts on deauth."""

    def _setup_chat(self, bot, chat_id, tmp_path, monkeypatch):
        cfg_path = tmp_path / "bot_config.json"
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir(exist_ok=True)
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "CHAT_DIR", chat_dir)
        bot._cfg_cache = None
        # Add chat to authorized list
        cfg_path.write_text(json.dumps({"chat_ids": ["100", chat_id]}), encoding="utf-8")
        bot.init_chat_config(chat_id)
        return cfg_path, chat_dir

    def test_removechat_purges_config_file(self, tmp_path, monkeypatch):
        """After removechat, chat_<id>.json no longer exists on disk."""
        import bot
        cfg_path, chat_dir = self._setup_chat(bot, "200", tmp_path, monkeypatch)
        bot.cmd_removechat(["/removechat", "200"], "100")
        assert not bot._chat_cfg_path("200").exists()

    def test_removechat_purges_weekly_log(self, tmp_path, monkeypatch):
        """After removechat, weekly_log_<id>.json no longer exists on disk."""
        import bot
        cfg_path, chat_dir = self._setup_chat(bot, "300", tmp_path, monkeypatch)
        # Create a weekly log file manually
        wlog_path = chat_dir / "weekly_log_300.json"
        wlog_path.write_text(json.dumps([{"ticker": "AAPL"}]), encoding="utf-8")
        bot.cmd_removechat(["/removechat", "300"], "100")
        assert not wlog_path.exists()

    def test_purge_idempotent_no_files(self, tmp_path, monkeypatch):
        """_purge_chat_data on non-existent files → no error."""
        import bot
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir()
        monkeypatch.setattr(bot, "CHAT_DIR", chat_dir)
        # Must not raise
        bot._purge_chat_data("nonexistent")

    def test_purge_idempotent_called_twice(self, tmp_path, monkeypatch):
        """_purge_chat_data called twice → no error."""
        import bot
        cfg_path, chat_dir = self._setup_chat(bot, "400", tmp_path, monkeypatch)
        bot._purge_chat_data("400")
        bot._purge_chat_data("400")  # must not raise

    def test_readd_gets_fresh_defaults(self, tmp_path, monkeypatch):
        """Remove → re-add → config equals _CHAT_DEFAULTS (no stale data)."""
        import bot
        cfg_path, chat_dir = self._setup_chat(bot, "500", tmp_path, monkeypatch)
        # Write some data to the chat config
        bot._ctx.chat_id = "500"
        try:
            bot.mutate_chat_cfg(lambda c: c["tickers"].extend(["AAPL", "MSFT"]))
            # Verify data was written
            chat_data = json.loads(bot._chat_cfg_path("500").read_text(encoding="utf-8"))
            assert "AAPL" in chat_data["tickers"]
        finally:
            bot._ctx.chat_id = None
        # Remove the chat
        bot.cmd_removechat(["/removechat", "500"], "100")
        assert not bot._chat_cfg_path("500").exists()
        # Re-add
        bot.cmd_addchat(["/addchat", "500"], "100")
        bot.init_chat_config("500")
        # Config must be fresh defaults
        bot._ctx.chat_id = "500"
        try:
            cfg = bot.get_chat_cfg()
            assert cfg["tickers"] == []
            assert cfg["portfolio"] == []
            assert cfg["api_keys"] == {}
        finally:
            bot._ctx.chat_id = None

    def test_no_api_keys_leak_after_remove(self, tmp_path, monkeypatch):
        """After removechat, no chat_<id>.json with api_keys on disk."""
        import bot
        cfg_path, chat_dir = self._setup_chat(bot, "600", tmp_path, monkeypatch)
        # Write api_keys to the chat config
        bot._ctx.chat_id = "600"
        try:
            bot.mutate_chat_cfg(lambda c: c.update({"api_keys": {"openai": "sk-secret"}}))
        finally:
            bot._ctx.chat_id = None
        # Remove
        bot.cmd_removechat(["/removechat", "600"], "100")
        # Verify no file on disk
        assert not bot._chat_cfg_path("600").exists()
        # Also verify no leftover files in chat_dir
        chat_files = list(chat_dir.glob("chat_600*"))
        assert chat_files == []

    def test_self_remove_does_not_purge(self, tmp_path, monkeypatch):
        """Self-remove is blocked, so files must NOT be purged."""
        import bot
        cfg_path, chat_dir = self._setup_chat(bot, "700", tmp_path, monkeypatch)
        bot.cmd_removechat(["/removechat", "700"], "700")
        # File must still exist
        assert bot._chat_cfg_path("700").exists()
        # Admin still in list
        assert "700" in [str(c) for c in bot.get_cfg()["chat_ids"]]


# ─── M3: Per-chat scheduled scan dedup ───────────────────────────────────────

class TestScheduledScanDedup:
    """_should_run_scheduled_scan is a per-chat gate — two chats with the
    same schedule both run; same chat within 90s does not re-trigger."""

    def test_two_chats_same_schedule_both_run(self):
        """Two chats at the same HH:MM → both should_run_scheduled_scan returns True."""
        import bot
        from datetime import datetime
        now = datetime(2026, 6, 15, 9, 0, 0)
        last = {}
        assert bot._should_run_scheduled_scan("A", now, last) is True
        assert bot._should_run_scheduled_scan("B", now, last) is True

    def test_intra_duplicate_within_90s_blocked(self):
        """Same chat scanned < 90s ago → should not run again."""
        import bot
        from datetime import datetime
        now = datetime(2026, 6, 15, 9, 0, 0)
        last = {"chat1": now}
        assert bot._should_run_scheduled_scan("chat1", now, last) is False

    def test_after_90s_allows_rerun(self):
        """Same chat scanned > 90s ago → should run again."""
        import bot
        from datetime import datetime
        from datetime import timedelta
        now = datetime(2026, 6, 15, 9, 2, 0)
        last = {"chat1": datetime(2026, 6, 15, 8, 59, 0)}
        assert bot._should_run_scheduled_scan("chat1", now, last) is True

    def test_empty_history_allows_run(self):
        """Chat never scanned before → should run."""
        import bot
        from datetime import datetime
        now = datetime(2026, 6, 15, 9, 0, 0)
        assert bot._should_run_scheduled_scan("new_chat", now, {}) is True

    def test_exactly_90s_boundary(self):
        """Exactly 90 seconds → should NOT run (must be strictly greater)."""
        import bot
        from datetime import datetime, timedelta
        now = datetime(2026, 6, 15, 9, 1, 30)
        last = {"chat1": datetime(2026, 6, 15, 9, 0, 0)}
        assert bot._should_run_scheduled_scan("chat1", now, last) is False


# ─── M4: Single-source version label ─────────────────────────────────────────

class TestVersionSingleSource:
    """Rendered bot_active / help_block / welcome_bootstrap must show the
    current __version__ and never contain a literal {version} placeholder."""

    def test_bot_active_renders_version(self):
        import bot
        result = bot.t("bot_active", version=bot.__version__)
        assert f"v{bot.__version__}" in result
        assert "{version}" not in result

    def test_help_block_renders_version(self):
        import bot
        result = bot.t("help_block", forms="10-K", ticker_count=0,
                       language="en", version=bot.__version__)
        assert f"v{bot.__version__}" in result
        assert "{version}" not in result

    def test_welcome_bootstrap_renders_version(self):
        import bot
        result = bot.t("welcome_bootstrap", master_chat_id="0",
                       version=bot.__version__)
        assert f"v{bot.__version__}" in result
        assert "{version}" not in result

    def test_version_single_source(self):
        """Changing __version__ propagates to all rendered strings."""
        import bot
        fake_ver = "99.99"
        for key in ("bot_active", "help_block", "welcome_bootstrap"):
            result = bot.t(key, version=fake_ver,
                           master_chat_id="0", forms="10-K",
                           ticker_count=0, language="en")
            assert f"v{fake_ver}" in result, f"{key} did not reflect version change"

    def test_no_hardcoded_v4_in_lang_files(self):
        """Lang files must not contain hardcoded v4.0 (or any v4.x) version."""
        import json
        from pathlib import Path
        lang_dir = Path(__file__).parent.parent / "lang"
        for lang_file in ("en.json", "tr.json"):
            data = json.loads((lang_dir / lang_file).read_text(encoding="utf-8"))
            for key in ("bot_active", "help_block", "welcome_bootstrap"):
                val = data.get(key, "")
                assert "v4.0" not in val, f"{lang_file}:{key} still contains v4.0"
