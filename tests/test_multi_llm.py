"""
Tests for J2 — Multi-LLM Provider Abstraction:
  Response parsers, _ordered_providers, _mask_key, _migrate_openrouter_key,
  _llm_one (mocked HTTP), llm() reactive/proactive asymmetry,
  _handle_pending_key, _handle_retry_callback,
  cmd_addapi/cmd_apis/cmd_setapi/cmd_delapi.
"""
import time
import threading
import unittest.mock as mock

import pytest


# ─── Response parsers (pure) ─────────────────────────────

class TestParsers:
    def test_openai_success(self, bot):
        body = {"choices": [{"message": {"content": "hello world"}}]}
        assert bot._parse_openai_resp(body) == "hello world"

    def test_openai_empty_choices(self, bot):
        assert bot._parse_openai_resp({"choices": []}) == ""

    def test_openai_missing_choices(self, bot):
        assert bot._parse_openai_resp({}) == ""

    def test_anthropic_success(self, bot):
        body = {"content": [{"type": "text", "text": "  analysis  "}]}
        assert bot._parse_anthropic_resp(body) == "analysis"

    def test_anthropic_empty(self, bot):
        assert bot._parse_anthropic_resp({"content": []}) == ""

    def test_gemini_success(self, bot):
        body = {"candidates": [{"content": {"parts": [{"text": "result"}]}}]}
        assert bot._parse_gemini_resp(body) == "result"

    def test_gemini_no_candidates(self, bot):
        assert bot._parse_gemini_resp({"candidates": []}) == ""


# ─── _mask_key ───────────────────────────────────────────

class TestMaskKey:
    def test_normal_key(self, bot):
        assert bot._mask_key("sk-or-v1-abc123") == "sk-o…"

    def test_short_key(self, bot):
        assert bot._mask_key("abc") == "abc…"

    def test_empty_key(self, bot):
        assert bot._mask_key("") == "…"


# ─── _ordered_providers ──────────────────────────────────

class TestOrderedProviders:
    def _set_cfg(self, bot, monkeypatch, tmp_path, api_keys, default=""):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"api_keys": api_keys, "default_provider": default}))

    def test_no_keys_returns_empty(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")  # conftest sets "test-key"
        self._set_cfg(bot, monkeypatch, tmp_path, {})
        assert bot._ordered_providers() == []

    def test_default_first(self, bot, monkeypatch, tmp_path):
        self._set_cfg(bot, monkeypatch, tmp_path,
                      {"openrouter": "key1", "groq": "key2"}, default="groq")
        providers = bot._ordered_providers()
        assert providers[0] == "groq"
        assert "openrouter" in providers

    def test_no_default_follows_registry_order(self, bot, monkeypatch, tmp_path):
        self._set_cfg(bot, monkeypatch, tmp_path,
                      {"groq": "key2", "openrouter": "key1"}, default="")
        providers = bot._ordered_providers()
        # openrouter appears before groq in _PROVIDERS dict
        assert providers.index("openrouter") < providers.index("groq")

    def test_default_not_in_keys_ignored(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")  # conftest sets "test-key"
        self._set_cfg(bot, monkeypatch, tmp_path,
                      {"groq": "key"}, default="openrouter")
        providers = bot._ordered_providers()
        assert providers == ["groq"]


# ─── _migrate_openrouter_key ─────────────────────────────

class TestMigrateOpenrouterKey:
    def _reset(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_moves_legacy_key(self, bot, monkeypatch, tmp_path):
        self._reset(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"openrouter_api_key": "sk-or-v1-abc"}))
        bot._migrate_openrouter_key()
        assert bot.get_cfg()["api_keys"]["openrouter"] == "sk-or-v1-abc"

    def test_sets_default_provider(self, bot, monkeypatch, tmp_path):
        self._reset(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"openrouter_api_key": "sk-or-v1-abc"}))
        bot._migrate_openrouter_key()
        assert bot.get_cfg()["default_provider"] == "openrouter"

    def test_idempotent(self, bot, monkeypatch, tmp_path):
        self._reset(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"openrouter_api_key": "sk-or-v1-abc"}))
        bot._migrate_openrouter_key()
        bot._migrate_openrouter_key()
        assert bot.get_cfg()["api_keys"]["openrouter"] == "sk-or-v1-abc"

    def test_does_not_overwrite_existing(self, bot, monkeypatch, tmp_path):
        self._reset(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({
            "openrouter_api_key": "sk-or-v1-legacy",
            "api_keys": {"openrouter": "sk-or-v1-already"},
        }))
        bot._migrate_openrouter_key()
        assert bot.get_cfg()["api_keys"]["openrouter"] == "sk-or-v1-already"


# ─── _llm_one (mocked HTTP) ──────────────────────────────

class TestLlmOne:
    def _mock_resp(self, status, json_body=None, exc=None):
        if exc:
            return mock.patch("bot.requests.post", side_effect=exc)
        resp = mock.MagicMock()
        resp.status_code = status
        resp.json.return_value = json_body or {}
        resp.raise_for_status = mock.MagicMock(
            side_effect=None if status < 400 else Exception(f"HTTP {status}"))
        return mock.patch("bot.requests.post", return_value=resp)

    def test_success_openrouter(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"openrouter": "sk-or-v1-abc"}}))
        body = {"choices": [{"message": {"content": "Analysis result"}}]}
        with self._mock_resp(200, body):
            result = bot._llm_one("prompt", "openrouter/auto", "openrouter")
        assert result == "Analysis result"

    def test_401_returns_auth_fail(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"groq": "badkey"}}))
        with self._mock_resp(401):
            result = bot._llm_one("prompt", "model", "groq")
        assert result is bot._AUTH_FAIL

    def test_429_returns_none(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"groq": "key"}}))
        with self._mock_resp(429):
            result = bot._llm_one("prompt", "model", "groq")
        assert result is None

    def test_500_returns_none(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"groq": "key"}}))
        with self._mock_resp(500):
            result = bot._llm_one("prompt", "model", "groq")
        assert result is None

    def test_no_key_returns_none(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        # No keys in config, no env var for anthropic
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        result = bot._llm_one("prompt", "model", "anthropic")
        assert result is None

    def test_unknown_provider_returns_none(self, bot):
        result = bot._llm_one("prompt", "model", "nonexistent")
        assert result is None


# ─── llm() reactive/proactive asymmetry ─────────────────

class TestLlmFallback:
    def _setup_two_providers(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys":        {"openrouter": "key1", "groq": "key2"},
            "default_provider": "openrouter",
        }))

    def test_proactive_falls_through_all_providers(self, bot, monkeypatch, tmp_path):
        self._setup_two_providers(bot, monkeypatch, tmp_path)
        bot._ctx.chat_id = None
        call_order = []
        def fake_llm_one(istem, model, provider):
            call_order.append(provider)
            return None  # all fail
        monkeypatch.setattr(bot, "_llm_one", fake_llm_one)
        result = bot.llm("prompt", "model")
        assert result == bot.t("analysis_unavailable")
        assert "openrouter" in call_order
        assert "groq" in call_order

    def test_proactive_returns_first_success(self, bot, monkeypatch, tmp_path):
        self._setup_two_providers(bot, monkeypatch, tmp_path)
        bot._ctx.chat_id = None
        def fake_llm_one(istem, model, provider):
            return "ok" if provider == "openrouter" else None
        monkeypatch.setattr(bot, "_llm_one", fake_llm_one)
        result = bot.llm("prompt", "model")
        assert result == "ok"

    def test_reactive_first_fail_returns_unavailable(self, bot, monkeypatch, tmp_path):
        self._setup_two_providers(bot, monkeypatch, tmp_path)
        # bot fixture sets _ctx.chat_id = "0"
        sent_msgs = []
        monkeypatch.setattr(bot, "tg", lambda msg: sent_msgs.append(msg))
        monkeypatch.setattr(bot, "tg_with_keyboard", lambda msg, kb: sent_msgs.append(("kb", msg)))
        monkeypatch.setattr(bot, "_llm_one", lambda i, m, p: None)
        result = bot.llm("prompt", "model")
        assert result == bot.t("analysis_unavailable")

    def test_reactive_first_fail_sends_retry_button(self, bot, monkeypatch, tmp_path):
        self._setup_two_providers(bot, monkeypatch, tmp_path)
        bot._ctx.chat_id = "0"
        kb_calls = []
        monkeypatch.setattr(bot, "tg", lambda m: None)
        monkeypatch.setattr(bot, "tg_with_keyboard", lambda msg, kb: kb_calls.append(kb))
        monkeypatch.setattr(bot, "_llm_one", lambda i, m, p: None)
        bot.llm("prompt", "model")
        assert len(kb_calls) == 1
        assert kb_calls[0]["inline_keyboard"][0][0]["callback_data"] == "retry"

    def test_reactive_success_no_button(self, bot, monkeypatch, tmp_path):
        self._setup_two_providers(bot, monkeypatch, tmp_path)
        bot._ctx.chat_id = "0"
        kb_calls = []
        monkeypatch.setattr(bot, "tg_with_keyboard", lambda msg, kb: kb_calls.append(kb))
        monkeypatch.setattr(bot, "_llm_one", lambda i, m, p: "success")
        result = bot.llm("prompt", "model")
        assert result == "success"
        assert kb_calls == []

    def test_reactive_no_next_provider_no_button(self, bot, monkeypatch, tmp_path):
        """Only one provider configured — no retry button offered."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"openrouter": "key1"}, "default_provider": "openrouter"}))
        bot._ctx.chat_id = "0"
        kb_calls = []
        monkeypatch.setattr(bot, "tg", lambda m: None)
        monkeypatch.setattr(bot, "tg_with_keyboard", lambda msg, kb: kb_calls.append(kb))
        monkeypatch.setattr(bot, "_llm_one", lambda i, m, p: None)
        bot.llm("prompt", "model")
        assert kb_calls == []


# ─── _handle_pending_key ────────────────────────────────

class TestHandlePendingKey:
    def _set_pending(self, bot, chat_id, provider, expires_offset=60):
        import time
        with bot._pending_lock:
            bot._pending_api_key[chat_id] = {
                "provider": provider,
                "expires":  time.time() + expires_offset,
            }

    def test_saves_key_to_config(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "_tg_delete_msg", lambda cid, mid: None)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda m: sent.append(m))
        self._set_pending(bot, "0", "groq")
        bot._handle_pending_key("0", "sk-groq-realkey123", {"message_id": 42})
        assert bot.get_cfg()["api_keys"].get("groq") == "sk-groq-realkey123"
        assert any("groq" in m for m in sent)

    def test_sets_default_if_none(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "_tg_delete_msg", lambda cid, mid: None)
        monkeypatch.setattr(bot, "tg", lambda m: None)
        self._set_pending(bot, "0", "groq")
        bot._handle_pending_key("0", "sk-groq-realkey123", {"message_id": 1})
        assert bot.get_cfg()["default_provider"] == "groq"

    def test_delete_msg_called(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        deleted = []
        monkeypatch.setattr(bot, "_tg_delete_msg", lambda cid, mid: deleted.append(mid))
        monkeypatch.setattr(bot, "tg", lambda m: None)
        self._set_pending(bot, "0", "groq")
        bot._handle_pending_key("0", "sk-groq-realkey123", {"message_id": 99})
        assert 99 in deleted

    def test_short_key_rejected(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        sent = []
        monkeypatch.setattr(bot, "tg", lambda m: sent.append(m))
        self._set_pending(bot, "0", "groq")
        bot._handle_pending_key("0", "short", {})
        assert bot.get_cfg().get("api_keys", {}).get("groq") is None
        assert len(sent) == 1  # an error message was sent

    def test_expired_entry_ignored(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        self._set_pending(bot, "0", "groq", expires_offset=-1)  # already expired
        bot._handle_pending_key("0", "sk-groq-realkey123", {})
        assert bot.get_cfg().get("api_keys", {}).get("groq") is None


# ─── _handle_retry_callback ─────────────────────────────

class TestHandleRetryCallback:
    def _cq(self, chat_id="0", cb_id="cb1"):
        return {"id": cb_id, "from": {"id": int(chat_id)}}

    def test_stale_callback_sends_expired(self, bot, monkeypatch):
        answered = []
        monkeypatch.setattr(bot, "tg_answer_callback",
                            lambda cb_id, text="": answered.append(text))
        with bot._retry_lock:
            bot._retry_prompt.clear()
        bot._handle_retry_callback(self._cq())
        assert any(bot.t("llm_retry_expired") in a or a == bot.t("llm_retry_expired")
                   for a in answered)

    def test_success_sends_result(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: None)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda m: sent.append(m))
        monkeypatch.setattr(bot, "_llm_one", lambda i, m, p: "retry-result")
        with bot._retry_lock:
            bot._retry_prompt["0"] = {
                "istem": "prompt", "model": "m",
                "remaining": ["groq"],
            }
        bot._handle_retry_callback(self._cq())
        assert "retry-result" in sent

    def test_all_fail_sends_unavailable(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "tg_answer_callback", lambda *a, **k: None)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda m: sent.append(m))
        monkeypatch.setattr(bot, "_llm_one", lambda i, m, p: None)
        with bot._retry_lock:
            bot._retry_prompt["0"] = {
                "istem": "prompt", "model": "m",
                "remaining": ["groq", "anthropic"],
            }
        bot._handle_retry_callback(self._cq())
        assert bot.t("analysis_unavailable") in sent


# ─── cmd_addapi ─────────────────────────────────────────

class TestCmdAddapi:
    def _private_msg(self, msg_id=1):
        return {"chat": {"type": "private"}, "message_id": msg_id}

    def _group_msg(self):
        return {"chat": {"type": "group"}, "message_id": 1}

    def test_group_rejected(self, bot):
        result = bot.cmd_addapi(["/addapi", "openrouter"], "0", self._group_msg())
        assert "private" in result.lower() or "özel" in result.lower()

    def test_no_provider_usage(self, bot):
        result = bot.cmd_addapi(["/addapi"], "0", self._private_msg())
        assert "provider" in result.lower() or "sağlayıcı" in result.lower()

    def test_unknown_provider(self, bot):
        result = bot.cmd_addapi(["/addapi", "badprovider"], "0", self._private_msg())
        assert "badprovider" in result.lower() or "unknown" in result.lower() or "bilinmeyen" in result.lower()

    def test_two_message_sets_pending(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        with bot._pending_lock:
            bot._pending_api_key.clear()
        bot.cmd_addapi(["/addapi", "groq"], "0", self._private_msg())
        with bot._pending_lock:
            assert "0" in bot._pending_api_key
            assert bot._pending_api_key["0"]["provider"] == "groq"

    def test_one_message_saves_key(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "_tg_delete_msg", lambda cid, mid: None)
        result = bot.cmd_addapi(
            ["/addapi", "groq", "sk-groq-realkey123"], "0", self._private_msg())
        assert "groq" in result
        assert bot.get_cfg()["api_keys"].get("groq") == "sk-groq-realkey123"

    def test_inline_key_too_short_rejected(self, bot):
        result = bot.cmd_addapi(["/addapi", "groq", "short"], "0", self._private_msg())
        assert "short" in result.lower() or "kısa" in result.lower()


# ─── cmd_apis ────────────────────────────────────────────

class TestCmdApis:
    def test_empty_returns_empty_msg(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        result = bot.cmd_apis()
        assert result == bot.t("apis_empty")

    def test_lists_provider_masked_key(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"groq": "sk-groq-realkey123"},
            "default_provider": "groq",
        }))
        result = bot.cmd_apis()
        assert "groq" in result
        assert "sk-g…" in result  # masked
        assert "⭐" in result

    def test_non_default_no_star(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"groq": "sk-groq-realkey123", "openrouter": "sk-or-v1-abc"},
            "default_provider": "openrouter",
        }))
        result = bot.cmd_apis()
        # groq should NOT have star
        lines = result.split("\n")
        groq_line = next((l for l in lines if "groq" in l), "")
        assert "⭐" not in groq_line


# ─── cmd_setapi ──────────────────────────────────────────

class TestCmdSetapi:
    def test_changes_default(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"groq": "key", "openrouter": "key2"},
            "default_provider": "openrouter",
        }))
        bot.cmd_setapi(["/setapi", "groq"])
        assert bot.get_cfg()["default_provider"] == "groq"

    def test_unknown_provider_error(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        result = bot.cmd_setapi(["/setapi", "noprovider"])
        assert "noprovider" in result.lower() or "not found" in result.lower() or "bulunamadı" in result.lower()

    def test_no_args_usage(self, bot):
        result = bot.cmd_setapi(["/setapi"])
        assert "setapi" in result.lower() or "provider" in result.lower() or "sağlayıcı" in result.lower()


# ─── cmd_delapi ──────────────────────────────────────────

class TestCmdDelapi:
    def test_deletes_key(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"groq": "key"}, "default_provider": "groq"}))
        bot.cmd_delapi(["/delapi", "groq"])
        assert not bot.get_cfg().get("api_keys", {}).get("groq")

    def test_reassigns_default(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"openrouter": "key1", "groq": "key2"},
            "default_provider": "openrouter",
        }))
        bot.cmd_delapi(["/delapi", "openrouter"])
        assert bot.get_cfg()["default_provider"] == "groq"

    def test_no_keys_left_clears_default(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {"groq": "key"}, "default_provider": "groq"}))
        bot.cmd_delapi(["/delapi", "groq"])
        assert bot.get_cfg()["default_provider"] == ""

    def test_unknown_provider_error(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        result = bot.cmd_delapi(["/delapi", "noprovider"])
        assert "noprovider" in result.lower() or "not found" in result.lower() or "bulunamadı" in result.lower()

    def test_no_args_usage(self, bot):
        result = bot.cmd_delapi(["/delapi"])
        assert "delapi" in result.lower() or "provider" in result.lower() or "sağlayıcı" in result.lower()


# ─── i18n key presence ───────────────────────────────────

class TestJ2I18nKeys:
    _KEYS = [
        "addapi_usage", "addapi_group_rejected", "addapi_unknown_provider",
        "addapi_prompt", "addapi_cancelled", "addapi_saved", "addapi_invalid_key_short",
        "apis_empty", "apis_header", "apis_row",
        "setapi_usage", "setapi_unknown", "setapi_done",
        "delapi_usage", "delapi_unknown", "delapi_done",
        "llm_retry_offer", "llm_retry_button", "llm_retry_expired",
        "llm_key_invalid",
    ]

    @pytest.mark.parametrize("key", _KEYS)
    def test_en_has_key(self, bot, key):
        assert key in bot._load_lang("en"), f"Missing '{key}' in en.json"

    @pytest.mark.parametrize("key", _KEYS)
    def test_tr_has_key(self, bot, key):
        assert key in bot._load_lang("tr"), f"Missing '{key}' in tr.json"
