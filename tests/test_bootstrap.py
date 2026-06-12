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


# ─── K1 — Setup wizard reordering ────────────────────────────
class TestWizardK1:
    """Tests for K1: wizard order lang → api → forms → tickers."""

    def _reset(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.WIZARD.clear()

    def test_start_wizard_sends_lang_menu_only(self, bot, monkeypatch, tmp_path):
        """First launch: only lang menu is sent (no welcome_bootstrap yet)."""
        self._reset(bot, monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg, **kw: sent.append(msg))
        bot.start_wizard()
        assert len(sent) == 1
        assert bot.t("wizard_lang_menu") in sent[0]
        assert bot.WIZARD.get("step") == "lang"
        assert bot.get_cfg_value("wizard_step") == "lang"

    def test_lang_choice_advances_to_api(self, bot, monkeypatch, tmp_path):
        """After /lang en → wizard moves to api step and sends welcome+api menu."""
        self._reset(bot, monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg, **kw: sent.append(msg))
        bot.start_wizard()
        sent.clear()
        handled = bot.wizard_handle("/lang en", ["/lang", "en"], chat_id="111", msg={})
        assert handled is True
        assert bot.WIZARD.get("step") == "api"
        assert bot.get_cfg_value("wizard_step") == "api"
        assert any(bot.t("welcome_bootstrap", master_chat_id=bot.MASTER_CHAT_ID) in m for m in sent)

    def test_api_skip_advances_to_forms(self, bot, monkeypatch, tmp_path):
        """During api step: /skip → forms step."""
        self._reset(bot, monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg, **kw: sent.append(msg))
        bot.WIZARD["step"] = "api"
        handled = bot.wizard_handle("/skip", ["/skip"], chat_id="111", msg={})
        assert handled is True
        assert bot.WIZARD.get("step") == "forms"
        assert bot.get_cfg_value("wizard_step") == "forms"

    def test_forms_usedefaults_advances_to_tickers(self, bot, monkeypatch, tmp_path):
        """During forms step: /usedefaults → tickers step."""
        self._reset(bot, monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg, **kw: sent.append(msg))
        bot.WIZARD["step"] = "forms"
        handled = bot.wizard_handle("/usedefaults", ["/usedefaults"], chat_id="111", msg={})
        assert handled is True
        assert bot.WIZARD.get("step") == "tickers"
        assert bot.get_cfg_value("wizard_step") == "tickers"
        assert bot.get_cfg_value("default_forms") == bot.DEFAULT_FORMS

    def test_tickers_skip_completes_wizard(self, bot, monkeypatch, tmp_path):
        """During tickers step: /skip → wizard complete, first_run=False."""
        self._reset(bot, monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg, **kw: sent.append(msg))
        bot.WIZARD["step"] = "tickers"
        handled = bot.wizard_handle("/skip", ["/skip"], chat_id="111", msg={})
        assert handled is True
        assert "step" not in bot.WIZARD
        assert bot.get_cfg_value("first_run") is False
        assert bot.get_cfg_value("wizard_step") == ""
        assert any(bot.t("wizard_complete") in m for m in sent)

    def test_restart_mid_api_resumes(self, bot, monkeypatch, tmp_path):
        """On restart with wizard_step='api' in cfg, _show_wizard_step_menu restores state."""
        self._reset(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"first_run": True, "wizard_step": "api"}))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg, **kw: sent.append(msg))
        wizard_step = bot.get_cfg_value("wizard_step")
        assert wizard_step in ("api", "forms", "tickers")
        bot.WIZARD["step"] = wizard_step
        bot._show_wizard_step_menu(wizard_step)
        assert bot.WIZARD.get("step") == "api"
        assert len(sent) >= 1

    def test_old_config_no_wizard(self, bot, monkeypatch, tmp_path):
        """Config with first_run=False → wizard_handle returns False (inactive)."""
        self._reset(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"first_run": False, "wizard_step": ""}))
        # WIZARD is empty → wizard_handle returns False
        handled = bot.wizard_handle("/help", ["/help"], chat_id="111", msg={})
        assert handled is False

    def test_api_menu_keys_exist(self, bot):
        """New i18n keys wizard_api_menu, wizard_api_more, wizard_api_existing present in both langs."""
        for code in ("en", "tr"):
            strings = bot._load_lang(code)
            for key in ("wizard_api_menu", "wizard_api_more", "wizard_api_existing"):
                assert key in strings, f"Missing '{key}' in {code}.json"

    def test_wizard_welcome_removed_from_lang(self, bot):
        """wizard_welcome (dead key) removed from both lang files."""
        for code in ("en", "tr"):
            strings = bot._load_lang(code)
            assert "wizard_welcome" not in strings, f"Dead key 'wizard_welcome' still in {code}.json"
