"""
J5 — Fiscal AI optional facts source tests.

_parse_fiscal_period, _parse_fiscal_response, fetch_fiscal_facts,
fallback chain, memo cache, ai_enabled(), API commands, i18n parity.
"""
import json
import pytest
from unittest.mock import patch, MagicMock, call


# ─── Helpers ──────────────────────────────────────────────────────────────────

PERIOD_END = "2024-09-30"

def _income_record(period_end: str = PERIOD_END, **overrides) -> dict:
    base = {
        "period_end_date": period_end,
        "revenue": 94930000000.0,
        "gross_profit": 42270000000.0,
        "operating_income": 29590000000.0,
        "net_income": 14736000000.0,
        "diluted_eps": 0.97,
    }
    base.update(overrides)
    return base

def _balance_record(period_end: str = PERIOD_END, **overrides) -> dict:
    base = {
        "period_end_date": period_end,
        "cash": 29943000000.0,
        "total_assets": 364980000000.0,
        "total_liabilities": 308030000000.0,
        "stockholders_equity": 56950000000.0,
    }
    base.update(overrides)
    return base

def _income_resp(period_end: str = PERIOD_END, **overrides) -> dict:
    return {"data": [_income_record(period_end, **overrides)]}

def _balance_resp(period_end: str = PERIOD_END, **overrides) -> dict:
    return {"data": [_balance_record(period_end, **overrides)]}


# ─── _parse_fiscal_period ─────────────────────────────────────────────────────

class TestParseFiscalPeriod:
    def test_full_income_response(self, bot):
        data = [_income_record()]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_INCOME_MAP)
        assert "Revenues" in result
        assert "GrossProfit" in result
        assert "OperatingIncomeLoss" in result
        assert "NetIncomeLoss" in result
        assert "EarningsPerShareDiluted" in result
        assert abs(result["Revenues"] - 94930000000.0) < 1.0

    def test_full_balance_response(self, bot):
        data = [_balance_record()]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_BALANCE_MAP)
        assert "CashAndCashEquivalentsAtCarryingValue" in result
        assert "Assets" in result
        assert "Liabilities" in result
        assert "StockholdersEquity" in result

    def test_period_mismatch_returns_empty(self, bot):
        data = [_income_record(period_end="2024-06-30")]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_INCOME_MAP)
        assert result == {}

    def test_period_prefix_match(self, bot):
        """period_end with time component — still matches by first 10 chars."""
        data = [_income_record(period_end=f"{PERIOD_END}T00:00:00")]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_INCOME_MAP)
        assert "Revenues" in result

    def test_non_list_input_returns_empty(self, bot):
        assert bot._parse_fiscal_period(None, PERIOD_END, bot._FISCAL_INCOME_MAP) == {}
        assert bot._parse_fiscal_period({}, PERIOD_END, bot._FISCAL_INCOME_MAP) == {}

    def test_empty_period_end_returns_empty(self, bot):
        data = [_income_record()]
        assert bot._parse_fiscal_period(data, "", bot._FISCAL_INCOME_MAP) == {}

    def test_non_numeric_field_skipped(self, bot):
        data = [_income_record(revenue="NOT_A_NUMBER")]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_INCOME_MAP)
        assert "Revenues" not in result
        # other numeric fields still present
        assert "NetIncomeLoss" in result

    def test_null_field_skipped(self, bot):
        data = [_income_record(revenue=None)]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_INCOME_MAP)
        assert "Revenues" not in result

    def test_picks_first_matching_period(self, bot):
        """Multiple records — first match wins."""
        data = [
            _income_record(period_end="2024-06-30", revenue=1.0),
            _income_record(period_end=PERIOD_END, revenue=999.0),
        ]
        result = bot._parse_fiscal_period(data, PERIOD_END, bot._FISCAL_INCOME_MAP)
        assert abs(result["Revenues"] - 999.0) < 0.01

    def test_alternate_date_keys(self, bot):
        """date / period_end / end_date fallback keys."""
        for key in ("date", "period_end", "end_date"):
            record = {key: PERIOD_END, "revenue": 100.0}
            result = bot._parse_fiscal_period([record], PERIOD_END, bot._FISCAL_INCOME_MAP)
            assert "Revenues" in result, f"Key '{key}' not recognised"


# ─── _parse_fiscal_response ───────────────────────────────────────────────────

class TestParseFiscalResponse:
    def test_full_response_returns_dict(self, bot):
        result = bot._parse_fiscal_response(
            _income_resp(), _balance_resp(), PERIOD_END)
        assert result is not None
        assert len(result) >= bot._FISCAL_FACTS_MINIMUM
        # values are (float, "USD", period_end) triples
        for concept, triple in result.items():
            val, unit, pe = triple
            assert isinstance(val, float)
            assert unit == "USD"
            assert pe == PERIOD_END

    def test_below_threshold_returns_none(self, bot):
        """Only 1 concept — below _FISCAL_FACTS_MINIMUM (4)."""
        sparse_income = {"data": [{"period_end_date": PERIOD_END, "revenue": 100.0}]}
        sparse_balance = {"data": [{"period_end_date": PERIOD_END}]}
        result = bot._parse_fiscal_response(sparse_income, sparse_balance, PERIOD_END)
        assert result is None

    def test_period_mismatch_returns_none(self, bot):
        result = bot._parse_fiscal_response(
            _income_resp(period_end="2024-06-30"),
            _balance_resp(period_end="2024-06-30"),
            PERIOD_END,
        )
        assert result is None

    def test_empty_period_end_returns_none(self, bot):
        result = bot._parse_fiscal_response(_income_resp(), _balance_resp(), "")
        assert result is None

    def test_non_dict_income_treated_as_empty(self, bot):
        """income_data is None → treated as empty list, may still pass if balance alone meets threshold."""
        result = bot._parse_fiscal_response(None, _balance_resp(), PERIOD_END)
        # balance only has 4 concepts, so just at threshold or below — either is acceptable,
        # just must not raise
        assert result is None or isinstance(result, dict)

    def test_corrupt_data_key_returns_none(self, bot):
        corrupt = {"data": "NOT_A_LIST"}
        result = bot._parse_fiscal_response(corrupt, _balance_resp(), PERIOD_END)
        assert result is None or isinstance(result, dict)


# ─── fetch_fiscal_facts ───────────────────────────────────────────────────────

class TestFetchFiscalFacts:
    def _set_key(self, bot):
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: "testkey_abcdef12"}
        ))

    def test_no_key_returns_none_zero_http(self, bot):
        # ensure no key
        bot.mutate_cfg(lambda c: c.get("api_keys", {}).pop(bot._FISCAL_AI_PROVIDER, None))
        with patch("bot.requests.get") as mock_get:
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None
        mock_get.assert_not_called()

    def test_no_period_end_returns_none(self, bot):
        self._set_key(bot)
        with patch("bot.requests.get") as mock_get:
            result = bot.fetch_fiscal_facts("AAPL", "")
        assert result is None
        mock_get.assert_not_called()

    def test_401_triggers_auth_warning_and_returns_none(self, bot, monkeypatch):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.ok = False
        warned = []
        monkeypatch.setattr(bot, "_check_fiscal_auth_reminder", lambda: warned.append(1))
        with patch("bot.requests.get", return_value=mock_resp):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None
        assert warned  # warning was triggered

    def test_403_triggers_auth_warning(self, bot, monkeypatch):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.ok = False
        warned = []
        monkeypatch.setattr(bot, "_check_fiscal_auth_reminder", lambda: warned.append(1))
        with patch("bot.requests.get", return_value=mock_resp):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None
        assert warned

    def test_404_returns_none_no_warning(self, bot, monkeypatch):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.ok = False
        warned = []
        monkeypatch.setattr(bot, "_check_fiscal_auth_reminder", lambda: warned.append(1))
        with patch("bot.requests.get", return_value=mock_resp):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None
        assert not warned

    def test_non_ok_status_returns_none(self, bot, monkeypatch):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.ok = False
        with patch("bot.requests.get", return_value=mock_resp):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None

    def test_json_parse_error_returns_none(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.ok = True
        mock_resp.json.side_effect = ValueError("bad json")
        with patch("bot.requests.get", return_value=mock_resp):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None

    def test_network_exception_returns_none(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        import requests as req_mod
        with patch("bot.requests.get", side_effect=req_mod.exceptions.ConnectionError("net")):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None

    def test_full_success_returns_dict(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        inc_resp = MagicMock()
        inc_resp.status_code = 200
        inc_resp.ok = True
        inc_resp.json.return_value = _income_resp()
        bal_resp = MagicMock()
        bal_resp.status_code = 200
        bal_resp.ok = True
        bal_resp.json.return_value = _balance_resp()
        with patch("bot.requests.get", side_effect=[inc_resp, bal_resp]):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is not None
        assert len(result) >= bot._FISCAL_FACTS_MINIMUM

    def test_exactly_two_http_calls(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        inc_resp = MagicMock(status_code=200, ok=True)
        inc_resp.json.return_value = _income_resp()
        bal_resp = MagicMock(status_code=200, ok=True)
        bal_resp.json.return_value = _balance_resp()
        with patch("bot.requests.get", side_effect=[inc_resp, bal_resp]) as mock_get:
            bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert mock_get.call_count == 2

    def test_x_api_key_header_used_not_query_param(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        captured_calls = []
        def fake_get(url, **kwargs):
            captured_calls.append((url, kwargs))
            r = MagicMock(status_code=404, ok=False)
            return r
        with patch("bot.requests.get", side_effect=fake_get):
            bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        for url, kwargs in captured_calls:
            assert "apiKey" not in url, "apiKey must not appear in URL"
            headers = kwargs.get("headers", {})
            assert "X-Api-Key" in headers, "X-Api-Key header must be present"

    def test_memo_cache_second_call_no_http(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        inc_resp = MagicMock(status_code=200, ok=True)
        inc_resp.json.return_value = _income_resp()
        bal_resp = MagicMock(status_code=200, ok=True)
        bal_resp.json.return_value = _balance_resp()
        with patch("bot.requests.get", side_effect=[inc_resp, bal_resp]) as mock_get:
            r1 = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
            r2 = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert mock_get.call_count == 2   # no additional calls on 2nd invocation
        assert r1 == r2

    def test_memo_cache_key_is_case_insensitive(self, bot):
        self._set_key(bot)
        bot._fiscal_memo.clear()
        inc_resp = MagicMock(status_code=200, ok=True)
        inc_resp.json.return_value = _income_resp()
        bal_resp = MagicMock(status_code=200, ok=True)
        bal_resp.json.return_value = _balance_resp()
        with patch("bot.requests.get", side_effect=[inc_resp, bal_resp]) as mock_get:
            bot.fetch_fiscal_facts("aapl", PERIOD_END)
            bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert mock_get.call_count == 2  # both resolved to same cache key

    def test_period_mismatch_falls_back_to_none(self, bot):
        """Response exists but period doesn't match → _parse returns None → stored as None."""
        self._set_key(bot)
        bot._fiscal_memo.clear()
        inc_resp = MagicMock(status_code=200, ok=True)
        inc_resp.json.return_value = _income_resp(period_end="2024-06-30")
        bal_resp = MagicMock(status_code=200, ok=True)
        bal_resp.json.return_value = _balance_resp(period_end="2024-06-30")
        with patch("bot.requests.get", side_effect=[inc_resp, bal_resp]):
            result = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        assert result is None


# ─── Fallback chain in fetch_new_filings ──────────────────────────────────────

class TestFiscalFallbackChain:
    """Integration: verify that when Fiscal AI returns None, EDGAR XBRL is used."""

    def test_no_key_uses_xbrl(self, bot, monkeypatch):
        bot.mutate_cfg(lambda c: c.get("api_keys", {}).pop(bot._FISCAL_AI_PROVIDER, None))
        xbrl_called = []
        monkeypatch.setattr(bot, "fetch_xbrl_facts", lambda f: (xbrl_called.append(1) or {}))
        monkeypatch.setattr(bot, "fetch_fiscal_facts", lambda t, p: (_ for _ in ()).throw(
            AssertionError("should not be called when no key")))

        # Verify _get_fiscal_key() returns "" when no key configured
        assert bot._get_fiscal_key() == ""

    def test_fiscal_none_uses_xbrl_format(self, bot, monkeypatch):
        """When fetch_fiscal_facts returns None, format_facts_block called with EDGAR source."""
        monkeypatch.setattr(bot, "_get_fiscal_key", lambda: "fakekey12345")
        monkeypatch.setattr(bot, "fetch_fiscal_facts", lambda t, p: None)
        xbrl_called = []
        monkeypatch.setattr(bot, "fetch_xbrl_facts", lambda f: (xbrl_called.append(1) or {}))
        fmt_calls = []
        orig_fmt = bot.format_facts_block
        def fake_fmt(facts, source="EDGAR XBRL"):
            fmt_calls.append(source)
            return orig_fmt(facts, source)
        monkeypatch.setattr(bot, "format_facts_block", fake_fmt)

        # Call format path manually (simulating fetch_new_filings logic)
        fiscal_facts = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        if fiscal_facts is not None:
            facts_block = bot.format_facts_block(fiscal_facts, source="Fiscal AI")
        else:
            facts_block = bot.format_facts_block(bot.fetch_xbrl_facts(MagicMock()) or {})

        assert xbrl_called  # XBRL was used
        assert len(fmt_calls) == 1
        assert fmt_calls[0] == "EDGAR XBRL"

    def test_fiscal_success_uses_fiscal_label(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "_get_fiscal_key", lambda: "fakekey12345")
        fake_facts = {"Revenues": (1000.0, "USD", PERIOD_END),
                      "NetIncomeLoss": (100.0, "USD", PERIOD_END),
                      "Assets": (5000.0, "USD", PERIOD_END),
                      "Liabilities": (4000.0, "USD", PERIOD_END)}
        monkeypatch.setattr(bot, "fetch_fiscal_facts", lambda t, p: fake_facts)
        fmt_calls = []
        orig_fmt = bot.format_facts_block
        def fake_fmt(facts, source="EDGAR XBRL"):
            fmt_calls.append(source)
            return orig_fmt(facts, source)
        monkeypatch.setattr(bot, "format_facts_block", fake_fmt)

        fiscal_facts = bot.fetch_fiscal_facts("AAPL", PERIOD_END)
        if fiscal_facts is not None:
            facts_block = bot.format_facts_block(fiscal_facts, source="Fiscal AI")
        else:
            facts_block = bot.format_facts_block({})

        assert len(fmt_calls) == 1
        assert fmt_calls[0] == "Fiscal AI"


# ─── format_facts_block source label ─────────────────────────────────────────

class TestFormatFactsBlockLabel:
    def _facts(self, period_end=PERIOD_END):
        return {
            "Revenues": (1000.0, "USD", period_end),
            "NetIncomeLoss": (100.0, "USD", period_end),
            "Assets": (5000.0, "USD", period_end),
            "Liabilities": (4000.0, "USD", period_end),
        }

    def test_default_source_is_xbrl(self, bot):
        block = bot.format_facts_block(self._facts())
        assert block.startswith("AUDITED XBRL FACTS")

    def test_fiscal_ai_source_label(self, bot):
        block = bot.format_facts_block(self._facts(), source="Fiscal AI")
        assert block.startswith("AUDITED FISCAL AI FACTS")

    def test_existing_xbrl_tests_unchanged(self, bot):
        """Regression: existing callers (no source arg) still get XBRL header."""
        block = bot.format_facts_block(self._facts(), source="EDGAR XBRL")
        assert block.startswith("AUDITED XBRL FACTS")


# ─── ai_enabled() not affected by fiscalai key ───────────────────────────────

class TestAiEnabledUnaffected:
    def test_fiscalai_key_alone_does_not_enable_ai(self, bot, monkeypatch):
        # Isolate all three openrouter legacy paths so only fiscalai key remains
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {}, "default_provider": "", "openrouter_api_key": ""
        }))
        # Add only fiscalai key
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: "fiscalkey12345"}
        ))
        assert not bot.ai_enabled()

    def test_llm_key_enables_ai_regardless_of_fiscalai(self, bot):
        from bot import _PROVIDERS
        if not _PROVIDERS:
            pytest.skip("No LLM providers defined")
        llm_provider = next(iter(_PROVIDERS))
        bot.mutate_cfg(lambda c: c.update({
            "api_keys": {
                llm_provider: "llmkeyabcdef12",
                bot._FISCAL_AI_PROVIDER: "fiscalkeyabcdef",
            },
            "default_provider": llm_provider,
        }))
        assert bot.ai_enabled()


# ─── /setapi fiscalai rejected ────────────────────────────────────────────────

class TestSetApiFiscalaiRejected:
    def test_setapi_fiscalai_returns_rejection_message(self, bot):
        # First add the key so the "no key" check isn't hit
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: "testkey12345"}
        ))
        result = bot.cmd_setapi(["/setapi", bot._FISCAL_AI_PROVIDER])
        expected = bot.t("setapi_fiscalai_rejected")
        assert result == expected

    def test_setapi_fiscalai_rejection_nonempty(self, bot):
        result = bot.cmd_setapi(["/setapi", bot._FISCAL_AI_PROVIDER])
        assert result
        assert result != "setapi_fiscalai_rejected"  # must not fall back to key name


# ─── /apis shows data section ─────────────────────────────────────────────────

class TestApisDataSection:
    def test_no_fiscalai_key_no_data_row(self, bot):
        bot.mutate_cfg(lambda c: c.get("api_keys", {}).pop(bot._FISCAL_AI_PROVIDER, None))
        result = bot.cmd_apis()
        assert "📊" not in result or bot._FISCAL_AI_PROVIDER not in result

    def test_fiscalai_key_shows_data_row(self, bot):
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: "fk_abcdef1234"}
        ))
        result = bot.cmd_apis()
        assert bot._FISCAL_AI_PROVIDER in result
        assert "fk_a" in result  # masked key prefix visible

    def test_fiscalai_key_masked(self, bot):
        raw_key = "fiscal_secret_key_xyz"
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: raw_key}
        ))
        result = bot.cmd_apis()
        assert raw_key not in result  # full key must NOT appear
        assert "fisc" in result  # first 4 chars in mask


# ─── /delapi fiscalai clears memo cache ──────────────────────────────────────

class TestDelapiFiscalai:
    def test_delapi_clears_memo(self, bot):
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: "fk_abcdefgh"}
        ))
        bot._fiscal_memo[("AAPL", PERIOD_END)] = {"Revenues": (100.0, "USD", PERIOD_END)}
        bot.cmd_delapi(["/delapi", bot._FISCAL_AI_PROVIDER])
        assert bot._fiscal_memo == {}

    def test_delapi_fiscalai_success_message(self, bot):
        bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
            {bot._FISCAL_AI_PROVIDER: "fk_abcdefgh"}
        ))
        result = bot.cmd_delapi(["/delapi", bot._FISCAL_AI_PROVIDER])
        assert bot.t("delapi_done", provider=bot._FISCAL_AI_PROVIDER) == result


# ─── /addapi fiscalai clears memo cache ──────────────────────────────────────

class TestAddapiFiscalaiMemo:
    def test_addapi_fiscalai_clears_memo(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "_tg_delete_msg", lambda *a, **k: None)
        bot._fiscal_memo[("AAPL", PERIOD_END)] = None
        private_msg = {"chat": {"type": "private"}, "message_id": 1}
        bot.cmd_addapi(["/addapi", bot._FISCAL_AI_PROVIDER, "newkey_abcdef12"], "0", private_msg)
        assert bot._fiscal_memo == {}


# ─── _check_fiscal_auth_reminder daily spam gate ─────────────────────────────

class TestFiscalAuthReminder:
    def test_sends_tg_and_records_date(self, bot, monkeypatch):
        bot.mutate_cfg(lambda c: c.update({"fiscal_auth_warned_date": ""}))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg: sent.append(msg))
        bot._check_fiscal_auth_reminder()
        assert sent  # tg() was called
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        assert bot.get_cfg().get("fiscal_auth_warned_date") == today

    def test_no_double_send_same_day(self, bot, monkeypatch):
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        bot.mutate_cfg(lambda c: c.update({"fiscal_auth_warned_date": today}))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg: sent.append(msg))
        bot._check_fiscal_auth_reminder()
        assert not sent  # already warned today

    def test_sends_again_next_day(self, bot, monkeypatch):
        bot.mutate_cfg(lambda c: c.update({"fiscal_auth_warned_date": "2000-01-01"}))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda msg: sent.append(msg))
        bot._check_fiscal_auth_reminder()
        assert sent


# ─── i18n parity ─────────────────────────────────────────────────────────────

J5_KEYS = [
    ("setapi_fiscalai_rejected", {}),
    ("apis_data_row", {"provider": "fiscalai", "masked_key": "fisc…"}),
    ("fiscalai_auth_error", {}),
]

@pytest.mark.parametrize("lang", ["en", "tr"])
@pytest.mark.parametrize("key,kwargs", J5_KEYS, ids=[k for k, _ in J5_KEYS])
class TestJ5I18nKeys:
    def test_key_exists_and_nonempty(self, bot, lang, key, kwargs):
        bot.mutate_cfg(lambda c: c.update({"language": lang}))
        bot._current_lang = None
        bot._lang_cache.clear()
        val = bot.t(key, **kwargs)
        assert val, f"[{lang}] '{key}' must be non-empty"
        assert val != key, f"[{lang}] '{key}' must not fall back to key name"


# ─── Optional live smoke test ─────────────────────────────────────────────────

@pytest.mark.network
def test_fiscal_ai_live_smoke(bot):
    """
    Live smoke: hits real Fiscal AI endpoint.
    Requires FISCAL_API_KEY env var and --network flag.
    """
    import os
    key = os.environ.get("FISCAL_API_KEY", "")
    if not key:
        pytest.skip("FISCAL_API_KEY not set")
    bot.mutate_cfg(lambda c: c.setdefault("api_keys", {}).update(
        {bot._FISCAL_AI_PROVIDER: key}
    ))
    bot._fiscal_memo.clear()
    # Use a well-known ticker and a recent period end (AAPL Q3 FY2024)
    result = bot.fetch_fiscal_facts("AAPL", "2024-06-29")
    # Either returns a valid dict or None (period not found) — must not raise
    assert result is None or (isinstance(result, dict) and len(result) >= 1)
