"""
Tests for K2 — Command Surface: /help completeness + /settings provider visibility.

Coverage:
- settings output includes active_provider, registered_providers
- edge cases: no key, default_provider set but key deleted
- help_block contains all dispatcher commands (regression guard)
- EN/TR parity for new settings_block placeholders
"""
import re
import pytest


# ─── cmd_settings provider visibility ─────────────────────────────────────────

class TestSettingsProviderVisibility:
    def _reset(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_no_keys_shows_no_provider(self, bot, monkeypatch, tmp_path):
        """No API keys → active_provider line indicates AI off."""
        self._reset(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {},
            "default_provider": "",
        }))
        out = bot.cmd_settings()
        # The line must not show a provider name from _PROVIDERS
        for p in bot._PROVIDERS:
            assert f"`{p}`" not in out or "no key" in out or "—" in out

    def test_active_provider_shown(self, bot, monkeypatch, tmp_path):
        """default_provider set with key → provider name in settings output."""
        self._reset(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"openrouter": "sk-or-test1234"},
            "default_provider": "openrouter",
        }))
        out = bot.cmd_settings()
        assert "openrouter" in out

    def test_default_provider_missing_key_flagged(self, bot, monkeypatch, tmp_path):
        """default_provider set but key removed → 'no key' warning in output."""
        self._reset(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {},
            "default_provider": "openrouter",
        }))
        out = bot.cmd_settings()
        assert "openrouter" in out
        assert "no key" in out

    def test_registered_providers_names_only(self, bot, monkeypatch, tmp_path):
        """Registered providers list shows names, no key material."""
        self._reset(bot, monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {
                "openrouter": "sk-or-test1234",
                "anthropic":  "sk-ant-test456",
            },
            "default_provider": "openrouter",
        }))
        out = bot.cmd_settings()
        assert "openrouter" in out
        assert "anthropic" in out
        # No key fragments should appear
        assert "sk-or" not in out
        assert "sk-ant" not in out


# ─── /help dispatcher coverage (regression guard) ──────────────────────────────

class TestHelpBlockCoverage:
    """
    Extract every command from handle_update dispatcher and verify it appears
    in help_block. This is a regression guard: if a new command is added to the
    dispatcher without updating help_block, this test will catch it.

    Exceptions (intentionally excluded from help):
    - /start (alias of /help, implicit)
    - /lang  (wizard-only, not a public command)
    """
    EXCLUDED = {
        "/start",       # alias of /help, implicit
        "/lang",        # wizard-only language set
        "/help",        # matched by text check, not komut==
        "/form4",       # alias matched by text-contains check
        "/setforms",    # wizard step command, not dispatcher komut==
        "/skip",        # wizard step command
        "/usedefaults", # wizard step command
        "/webhook",     # substring appearing in config URLs, not a command
        "/richtest",    # hidden admin-only rich-transport verification (O1)
    }

    def _dispatcher_commands(self, bot) -> set:
        """
        Parse bot.py source to extract all /command strings from the
        handle_update dispatcher block (komut == "..." pattern).
        """
        import inspect, re
        source = inspect.getsource(bot)
        # Match: komut == "/cmd" or any(... "/cmd" ...)
        return {
            m.group(1)
            for m in re.finditer(r'"(/\w+)"', source)
            if m.group(1).startswith("/")
        }

    def test_all_dispatcher_commands_in_help_block(self, bot):
        """Every dispatcher command (minus exclusions) appears in help_block."""
        # Load english help text (with dummy placeholders)
        raw_help = bot._load_lang("en").get("help_block", "")

        dispatcher_cmds = self._dispatcher_commands(bot) - self.EXCLUDED
        missing = []
        for cmd in sorted(dispatcher_cmds):
            if cmd not in raw_help:
                missing.append(cmd)

        assert not missing, (
            f"Commands in dispatcher but missing from help_block:\n"
            + "\n".join(f"  {c}" for c in missing)
        )

    def test_help_block_has_watchwords_section(self, bot):
        help_en = bot._load_lang("en").get("help_block", "")
        assert "/addword" in help_en
        assert "/removeword" in help_en
        assert "/listwords" in help_en

    def test_help_block_has_portfolio_section(self, bot):
        help_en = bot._load_lang("en").get("help_block", "")
        assert "/addpos" in help_en
        assert "/removepos" in help_en
        assert "/pnl" in help_en

    def test_help_block_has_api_keys_section(self, bot):
        help_en = bot._load_lang("en").get("help_block", "")
        assert "/addapi" in help_en
        assert "/apis" in help_en
        assert "/setapi" in help_en
        assert "/delapi" in help_en

    def test_help_block_has_chats_section(self, bot):
        help_en = bot._load_lang("en").get("help_block", "")
        assert "/addchat" in help_en
        assert "/removechat" in help_en
        assert "/listchats" in help_en

    def test_help_block_has_export(self, bot):
        help_en = bot._load_lang("en").get("help_block", "")
        assert "/export" in help_en

    def test_help_block_has_setrawmax(self, bot):
        help_en = bot._load_lang("en").get("help_block", "")
        assert "/setrawmax" in help_en

    def test_all_dispatcher_commands_in_help_block_rich(self, bot):
        """Every dispatcher command (minus exclusions) appears in help_block_rich."""
        raw_help = bot._load_lang("en").get("help_block_rich", "")
        dispatcher_cmds = self._dispatcher_commands(bot) - self.EXCLUDED
        missing = []
        for cmd in sorted(dispatcher_cmds):
            if cmd not in raw_help:
                missing.append(cmd)
        assert not missing, (
            f"Commands in dispatcher but missing from help_block_rich:\n"
            + "\n".join(f"  {c}" for c in missing)
        )

    def test_settings_block_has_provider_placeholders(self, bot):
        """settings_block template contains provider placeholders."""
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code).get("settings_block", "")
            assert "{active_provider}" in tmpl, f"Missing {{active_provider}} in {code}.json"
            assert "{registered_providers}" in tmpl, f"Missing {{registered_providers}} in {code}.json"
