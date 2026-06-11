"""
Tests for J1 — Minimal .env + Master-User Bootstrap:
  _is_valid_chat_id, run_startup_checks (MASTER_CHAT_ID),
  _ensure_master_in_chat_ids, _import_legacy_env,
  and i18n key presence.
"""
import pytest


# ─── _is_valid_chat_id ────────────────────────────────────
class TestIsValidChatId:
    def test_valid_positive(self, bot):
        assert bot._is_valid_chat_id("123456789") is True

    def test_valid_negative(self, bot):
        assert bot._is_valid_chat_id("-1001234567890") is True

    def test_zero_invalid(self, bot):
        assert bot._is_valid_chat_id("0") is False

    def test_empty_invalid(self, bot):
        assert bot._is_valid_chat_id("") is False

    def test_nonnumeric_invalid(self, bot):
        assert bot._is_valid_chat_id("YOUR_CHAT_ID") is False


# ─── run_startup_checks — MASTER_CHAT_ID coverage ─────────
class TestFailFast:
    def _good(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "EDGAR_IDENTITY", "Real Person r@e.com")
        monkeypatch.setattr(bot, "TELEGRAM_BOT_TOKEN", "123456:ABCdef")
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "123456789")

    def test_all_good_empty_issues(self, bot, monkeypatch):
        self._good(bot, monkeypatch)
        assert bot.run_startup_checks() == []

    def test_missing_master(self, bot, monkeypatch):
        self._good(bot, monkeypatch)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "")
        issues = bot.run_startup_checks()
        assert any("MASTER_CHAT_ID" in i for i in issues)

    def test_placeholder_master(self, bot, monkeypatch):
        self._good(bot, monkeypatch)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "YOUR_CHAT_ID")
        issues = bot.run_startup_checks()
        assert any("MASTER_CHAT_ID" in i for i in issues)

    def test_invalid_master(self, bot, monkeypatch):
        self._good(bot, monkeypatch)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "0")
        issues = bot.run_startup_checks()
        assert any("MASTER_CHAT_ID" in i for i in issues)

    def test_openrouter_not_checked_at_startup(self, bot, monkeypatch):
        """OPENROUTER_API_KEY absence must NOT block startup (J1 design)."""
        self._good(bot, monkeypatch)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        assert bot.run_startup_checks() == []


# ─── _ensure_master_in_chat_ids ───────────────────────────
class TestMasterMerge:
    def _reset_cfg(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_empty_list_bootstraps(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "111")
        bot._ensure_master_in_chat_ids()
        assert bot.get_cfg()["chat_ids"][0] == "111"

    def test_different_admin_master_wins(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "999")
        bot.mutate_cfg(lambda c: c.update({"chat_ids": ["111", "222"]}))
        bot._ensure_master_in_chat_ids()
        ids = bot.get_cfg()["chat_ids"]
        assert ids[0] == "999"
        assert "111" in ids
        assert "222" in ids

    def test_master_already_admin_noop(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "111")
        bot.mutate_cfg(lambda c: c.update({"chat_ids": ["111", "222"]}))
        bot._ensure_master_in_chat_ids()
        assert bot.get_cfg()["chat_ids"] == ["111", "222"]

    def test_master_in_list_not_at_front(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "MASTER_CHAT_ID", "222")
        bot.mutate_cfg(lambda c: c.update({"chat_ids": ["111", "222", "333"]}))
        bot._ensure_master_in_chat_ids()
        ids = bot.get_cfg()["chat_ids"]
        assert ids[0] == "222"
        assert "111" in ids
        assert "333" in ids


# ─── _import_legacy_env ───────────────────────────────────
class TestLegacyEnvImport:
    def _reset_cfg(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_imports_openrouter_key(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "sk-or-v1-realkey1234")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "")
        result = bot._import_legacy_env()
        assert "OPENROUTER_API_KEY" in result
        assert bot.get_cfg_value("openrouter_api_key") == "sk-or-v1-realkey1234"

    def test_idempotent(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "sk-or-v1-realkey1234")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "")
        bot._import_legacy_env()
        result2 = bot._import_legacy_env()
        assert result2 == []

    def test_does_not_overwrite_existing(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"openrouter_api_key": "already-set"}))
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "sk-or-v1-newkey12345")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "")
        bot._import_legacy_env()
        assert bot.get_cfg_value("openrouter_api_key") == "already-set"

    def test_placeholder_not_imported(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "sk-or-v1-YOUR_KEY_HERE")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "")
        result = bot._import_legacy_env()
        assert "OPENROUTER_API_KEY" not in result

    def test_telegram_chat_id_added(self, bot, monkeypatch, tmp_path):
        self._reset_cfg(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        monkeypatch.setattr(bot, "TELEGRAM_CHAT_ID", "555666777")
        result = bot._import_legacy_env()
        assert "TELEGRAM_CHAT_ID" in result
        assert "555666777" in [str(i) for i in bot.get_cfg_value("chat_ids", [])]


# ─── i18n key presence ────────────────────────────────────
class TestWelcomeFirstRun:
    def test_welcome_bootstrap_key_exists_both_langs(self, bot):
        for code in ("en", "tr"):
            strings = bot._load_lang(code)
            assert "welcome_bootstrap" in strings, \
                f"Missing 'welcome_bootstrap' in {code}.json"

    def test_env_import_done_key_exists_both_langs(self, bot):
        for code in ("en", "tr"):
            strings = bot._load_lang(code)
            assert "env_import_done" in strings, \
                f"Missing 'env_import_done' in {code}.json"
