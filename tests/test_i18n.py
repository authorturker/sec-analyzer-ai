"""
Tests for the i18n loader, t() function, and language metadata.
"""
import json
import pytest


class TestLangDiscovery:
    def test_discover_finds_en_and_tr(self, bot):
        # SUPPORTED_LANGS is computed at import time; both files should be picked up.
        assert "en" in bot.SUPPORTED_LANGS
        assert "tr" in bot.SUPPORTED_LANGS


class TestLoadLang:
    def test_loads_existing_language(self, bot):
        en = bot._load_lang("en")
        assert isinstance(en, dict)
        assert len(en) > 0

    def test_caches_result(self, bot):
        first  = bot._load_lang("en")
        second = bot._load_lang("en")
        assert first is second  # exact same object → cached

    def test_missing_lang_returns_empty_dict(self, bot):
        out = bot._load_lang("zz")
        assert out == {}


class TestT:
    def test_returns_translated_string(self, bot):
        bot._current_lang = "en"
        assert bot.t("watchlist_empty") == "ℹ️ Watchlist is empty."

    def test_switches_language(self, bot):
        bot._current_lang = "tr"
        assert "Takip listesi" in bot.t("watchlist_empty")

    def test_format_kwargs_substituted(self, bot):
        bot._current_lang = "en"
        out = bot.t("fetch_error_unknown", ticker="AAPL")
        assert "AAPL" in out

    def test_missing_key_falls_back_to_english(self, bot):
        # Inject a TR-only-missing key by removing from the cached dict
        bot._current_lang = "tr"
        # Force-load both
        en_dict = bot._load_lang("en")
        tr_dict = bot._load_lang("tr")
        # Pick a key that exists in en, manually delete from tr cache
        key = "watchlist_empty"
        original_tr = tr_dict.get(key)
        try:
            del tr_dict[key]
            # Should fall back to en
            assert bot.t(key) == en_dict[key]
        finally:
            if original_tr is not None:
                tr_dict[key] = original_tr

    def test_missing_key_in_all_returns_key_itself(self, bot):
        out = bot.t("__nonexistent_key_zzz__")
        assert out == "__nonexistent_key_zzz__"

    def test_format_failure_returns_unformatted(self, bot):
        # Use a key that requires kwargs but call without
        bot._current_lang = "en"
        # fetch_error_unknown needs {ticker}
        out = bot.t("fetch_error_unknown")  # no kwargs supplied
        # Returned string should still be the template, not raise
        assert "{ticker}" in out


class TestFormDesc:
    def test_known_form_en(self, bot):
        bot._current_lang = "en"
        assert bot.form_desc("10-K") == "Annual report"

    def test_known_form_tr(self, bot):
        bot._current_lang = "tr"
        assert bot.form_desc("10-K") == "Yıllık rapor"

    def test_unknown_form_returns_key(self, bot):
        bot._current_lang = "en"
        # No form_desc_FOO key → t() returns key itself
        assert bot.form_desc("FOO") == "form_desc_FOO"


class TestLangMeta:
    def test_en_meta(self, bot):
        bot._current_lang = "en"
        meta = bot.lang_meta()
        assert meta["code"] == "en"
        assert meta["name"] == "English"
        assert meta["llm_response_language"] == "English"

    def test_tr_meta(self, bot):
        bot._current_lang = "tr"
        meta = bot.lang_meta()
        assert meta["code"] == "tr"
        assert meta["llm_response_language"] == "Turkish"


class TestLangFileParity:
    """en and tr should have identical key sets — guards against drift."""
    def test_keys_match(self, bot):
        en_keys = set(bot._load_lang("en"))
        tr_keys = set(bot._load_lang("tr"))
        missing_in_tr = en_keys - tr_keys
        extra_in_tr   = tr_keys - en_keys
        assert not missing_in_tr, f"keys missing in tr.json: {missing_in_tr}"
        assert not extra_in_tr,   f"unexpected keys in tr.json: {extra_in_tr}"


class TestSystemMessage:
    def test_includes_response_language_en(self, bot):
        bot._current_lang = "en"
        assert "Respond in English" in bot.system_message()

    def test_includes_response_language_tr(self, bot):
        bot._current_lang = "tr"
        assert "Respond in Turkish" in bot.system_message()
