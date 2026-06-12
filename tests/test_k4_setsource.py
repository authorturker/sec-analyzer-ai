"""
Tests for K4 — facts_source cfg + _fiscal_enabled gate + /setsource command.

Coverage:
- _fiscal_enabled: 6 matrix cases (3 sources × key present/absent) + unknown value
- /setsource: no-arg, invalid, 3 valid modes, fiscalai-no-key warning, persistence
- cmd_settings: 5-state data_source label
- i18n: 4 new keys EN+TR parite
- help_block: /setsource present (K2 regression guard)
"""
import pytest


# ─── _fiscal_enabled matrix ────────────────────────────────────

class TestFiscalEnabled:
    def _set_source_and_key(self, bot, monkeypatch, tmp_path, source, has_key):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        key_val = "fai-test-key" if has_key else ""
        bot.mutate_cfg(lambda c: c.update({
            "facts_source": source,
            "api_keys": {"fiscalai": key_val} if key_val else {},
        }))

    def test_edgar_no_key(self, bot, monkeypatch, tmp_path):
        self._set_source_and_key(bot, monkeypatch, tmp_path, "edgar", False)
        assert bot._fiscal_enabled() is False

    def test_edgar_with_key(self, bot, monkeypatch, tmp_path):
        """edgar mode: always False even if key present."""
        self._set_source_and_key(bot, monkeypatch, tmp_path, "edgar", True)
        assert bot._fiscal_enabled() is False

    def test_fiscalai_no_key(self, bot, monkeypatch, tmp_path):
        self._set_source_and_key(bot, monkeypatch, tmp_path, "fiscalai", False)
        assert bot._fiscal_enabled() is False

    def test_fiscalai_with_key(self, bot, monkeypatch, tmp_path):
        self._set_source_and_key(bot, monkeypatch, tmp_path, "fiscalai", True)
        assert bot._fiscal_enabled() is True

    def test_auto_no_key(self, bot, monkeypatch, tmp_path):
        self._set_source_and_key(bot, monkeypatch, tmp_path, "auto", False)
        assert bot._fiscal_enabled() is False

    def test_auto_with_key(self, bot, monkeypatch, tmp_path):
        self._set_source_and_key(bot, monkeypatch, tmp_path, "auto", True)
        assert bot._fiscal_enabled() is True

    def test_unknown_value_treated_as_auto(self, bot, monkeypatch, tmp_path):
        """Unknown facts_source value defaults to auto behaviour (key-dependent)."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "facts_source": "invalid_value",
            "api_keys": {"fiscalai": "fai-key"},
        }))
        # Unknown → auto → key present → True
        assert bot._fiscal_enabled() is True

    def test_unknown_value_no_key(self, bot, monkeypatch, tmp_path):
        """Unknown facts_source value, no key → False (auto-like)."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "facts_source": "both",
            "api_keys": {},
        }))
        assert bot._fiscal_enabled() is False


# ─── /setsource command ────────────────────────────────────────

class TestCmdSetsource:
    def _fresh(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_no_arg_shows_current(self, bot, monkeypatch, tmp_path):
        """No argument: shows current source and valid options."""
        self._fresh(bot, monkeypatch, tmp_path)
        out = bot.cmd_setsource(["/setsource"])
        assert "auto" in out  # default
        assert "edgar" in out
        assert "fiscalai" in out

    def test_invalid_arg_rejected(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        out = bot.cmd_setsource(["/setsource", "both"])
        assert bot.t("setsource_invalid", valid="auto | fiscalai | twelvedata | edgar") in out

    def test_set_edgar(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        out = bot.cmd_setsource(["/setsource", "edgar"])
        assert bot.t("setsource_ok", source="edgar") in out

    def test_set_auto(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        out = bot.cmd_setsource(["/setsource", "auto"])
        assert bot.t("setsource_ok", source="auto") in out

    def test_set_fiscalai_no_key_warns(self, bot, monkeypatch, tmp_path):
        """fiscalai selected but no key → accepted + warning."""
        self._fresh(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"api_keys": {}}))
        bot._cfg_cache = None
        out = bot.cmd_setsource(["/setsource", "fiscalai"])
        assert bot.t("setsource_no_key", provider="fiscalai") in out

    def test_set_fiscalai_with_key_ok(self, bot, monkeypatch, tmp_path):
        """fiscalai selected with key → normal ok response."""
        self._fresh(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"fiscalai": "fai-key"}}))
        bot._cfg_cache = None
        out = bot.cmd_setsource(["/setsource", "fiscalai"])
        assert bot.t("setsource_ok", source="fiscalai") in out
        assert bot.t("setsource_no_key", provider="fiscalai") not in out

    def test_persistence(self, bot, monkeypatch, tmp_path):
        """Value survives config reload after /setsource."""
        self._fresh(bot, monkeypatch, tmp_path)
        bot.cmd_setsource(["/setsource", "edgar"])
        bot._cfg_cache = None  # force reload
        assert bot.get_cfg().get("facts_source") == "edgar"

    def test_case_insensitive(self, bot, monkeypatch, tmp_path):
        """Argument is lowercased: EDGAR → edgar."""
        self._fresh(bot, monkeypatch, tmp_path)
        out = bot.cmd_setsource(["/setsource", "EDGAR"])
        assert bot.t("setsource_ok", source="edgar") in out

    def test_legacy_config_no_key_defaults_auto(self, bot, monkeypatch, tmp_path):
        """Config without facts_source key → treated as auto."""
        self._fresh(bot, monkeypatch, tmp_path)
        # Write config without facts_source key
        bot.mutate_cfg(lambda c: c.pop("facts_source", None) or c)
        bot._cfg_cache = None
        out = bot.cmd_setsource(["/setsource"])
        assert "auto" in out  # current default shows as auto


# ─── cmd_settings data_source 5 states ────────────────────────

class TestSettingsDataSource:
    def _setup(self, bot, monkeypatch, tmp_path, source, has_key):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        key_map = {"fiscalai": "fai-key"} if has_key else {}
        bot.mutate_cfg(lambda c: c.update({
            "facts_source": source,
            "api_keys": key_map,
            "default_provider": "",
        }))

    def test_edgar_label(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "edgar", False)
        out = bot.cmd_settings()
        assert "EDGAR" in out
        assert "fiscalai" not in out.split("Data source")[1] if "Data source" in out else True

    def test_fiscalai_with_key_label(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "fiscalai", True)
        out = bot.cmd_settings()
        assert "fiscalai" in out
        assert "EDGAR fallback" in out

    def test_fiscalai_no_key_label(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "fiscalai", False)
        out = bot.cmd_settings()
        assert "fiscalai" in out
        assert "no key" in out

    def test_auto_with_key_label(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "auto", True)
        out = bot.cmd_settings()
        assert "auto" in out
        assert "fiscalai" in out

    def test_auto_no_key_label(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "auto", False)
        out = bot.cmd_settings()
        assert "auto" in out
        assert "EDGAR" in out


# ─── i18n parite ──────────────────────────────────────────────

class TestSetsourceI18n:
    _KEYS = ("setsource_current", "setsource_ok", "setsource_invalid", "setsource_no_key")

    def test_keys_exist_en(self, bot):
        strings = bot._load_lang("en")
        for k in self._KEYS:
            assert k in strings, f"Missing '{k}' in en.json"

    def test_keys_exist_tr(self, bot):
        strings = bot._load_lang("tr")
        for k in self._KEYS:
            assert k in strings, f"Missing '{k}' in tr.json"

    def test_setsource_current_has_placeholders(self, bot):
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code)["setsource_current"]
            assert "{source}" in tmpl, f"Missing {{source}} in setsource_current ({code})"
            assert "{valid}" in tmpl, f"Missing {{valid}} in setsource_current ({code})"

    def test_setsource_ok_has_source_placeholder(self, bot):
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code)["setsource_ok"]
            assert "{source}" in tmpl, f"Missing {{source}} in setsource_ok ({code})"

    def test_setsource_invalid_has_valid_placeholder(self, bot):
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code)["setsource_invalid"]
            assert "{valid}" in tmpl, f"Missing {{valid}} in setsource_invalid ({code})"


# ─── help_block regression (K2 dispatcher↔help tarayıcısı) ────

class TestHelpBlockSetsource:
    def test_setsource_in_help_block_en(self, bot):
        strings = bot._load_lang("en")
        assert "/setsource" in strings["help_block"]

    def test_setsource_in_help_block_tr(self, bot):
        strings = bot._load_lang("tr")
        assert "/setsource" in strings["help_block"]
