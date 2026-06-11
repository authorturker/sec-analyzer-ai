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
