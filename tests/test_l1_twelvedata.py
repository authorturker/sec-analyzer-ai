"""
Tests for L1 — Twelve Data as second grounding data provider + source chain.

Coverage:
- _data_source_chain: 4 modes (auto/fiscalai/twelvedata/edgar) × key combinations
- _parse_twelve_response: exact match, period mismatch → None, str numbers,
  below threshold, error-body → None
- Chain ordered trial + fallback (fiscalai first in auto, twelvedata second)
- /setsource twelvedata (accept, no-key warn, persistence)
- _data_source_label(): all 7 states
- /addapi twelvedata (key stored, memo cleared)
- /setapi twelvedata rejection (data provider ≠ LLM provider)
- i18n parity EN+TR: twelvedata_auth_error, updated setsource_no_key,
  setapi_data_provider_rejected
- Existing fiscalai tests remain green (regression guard)
"""
import pytest
from unittest.mock import patch, MagicMock


# ─── helpers ──────────────────────────────────────────────────────

def _cfg(bot, monkeypatch, tmp_path, source="auto", keys: dict | None = None):
    monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
    bot._cfg_cache = None
    bot.mutate_cfg(lambda c: c.update({
        "facts_source": source,
        "api_keys": keys if keys is not None else {},
    }))


# ─── _data_source_chain matrix ────────────────────────────────────

class TestDataSourceChain:
    """4 modes × key combinations → chain list."""

    def test_edgar_no_key(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "edgar", {})
        assert bot._data_source_chain() == []

    def test_edgar_with_any_key(self, bot, monkeypatch, tmp_path):
        """edgar: always empty regardless of keys."""
        _cfg(bot, monkeypatch, tmp_path, "edgar",
             {"fiscalai": "fai-k", "twelvedata": "td-k"})
        assert bot._data_source_chain() == []

    def test_fiscalai_explicit_with_key(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "fiscalai", {"fiscalai": "fai-k"})
        assert bot._data_source_chain() == ["fiscalai"]

    def test_fiscalai_explicit_no_key(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "fiscalai", {})
        assert bot._data_source_chain() == []

    def test_twelvedata_explicit_with_key(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "twelvedata", {"twelvedata": "td-k"})
        assert bot._data_source_chain() == ["twelvedata"]

    def test_twelvedata_explicit_no_key(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "twelvedata", {})
        assert bot._data_source_chain() == []

    def test_auto_both_keys(self, bot, monkeypatch, tmp_path):
        """auto with both keys → fiscalai first (provider order)."""
        _cfg(bot, monkeypatch, tmp_path, "auto",
             {"fiscalai": "fai-k", "twelvedata": "td-k"})
        chain = bot._data_source_chain()
        assert chain[0] == "fiscalai"
        assert chain[1] == "twelvedata"

    def test_auto_only_twelvedata(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "auto", {"twelvedata": "td-k"})
        assert bot._data_source_chain() == ["twelvedata"]

    def test_auto_only_fiscalai(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "auto", {"fiscalai": "fai-k"})
        assert bot._data_source_chain() == ["fiscalai"]

    def test_auto_no_keys(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "auto", {})
        assert bot._data_source_chain() == []

    def test_unknown_value_treated_as_auto_with_both_keys(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "garbage",
             {"fiscalai": "fai-k", "twelvedata": "td-k"})
        chain = bot._data_source_chain()
        assert "fiscalai" in chain
        assert "twelvedata" in chain

    def test_unknown_value_no_keys(self, bot, monkeypatch, tmp_path):
        _cfg(bot, monkeypatch, tmp_path, "garbage", {})
        assert bot._data_source_chain() == []


# ─── _parse_twelve_response ────────────────────────────────────────

class TestParseTwelveResponse:
    _PERIOD = "2024-09-30"

    def _income(self, fd=None, **extra):
        record = {
            "fiscal_date": fd or self._PERIOD,
            "revenue": "1000000",
            "gross_profit": "400000",
            "operating_income": "200000",
            "net_income": "150000",
        }
        record.update(extra)
        return {"data": [record]}

    def _balance(self, fd=None, **extra):
        record = {
            "fiscal_date": fd or self._PERIOD,
            "cash_and_equivalents": "50000",
            "total_assets": "2000000",
            "total_liabilities": "800000",
            "total_equity": "1200000",
        }
        record.update(extra)
        return {"data": [record]}

    def test_exact_match_returns_dict(self, bot):
        result = bot._parse_twelve_response(
            self._income(), self._balance(), self._PERIOD)
        assert result is not None
        assert "Revenues" in result
        assert "Assets" in result

    def test_period_mismatch_returns_none(self, bot):
        result = bot._parse_twelve_response(
            self._income(fd="2024-06-30"),
            self._balance(fd="2024-06-30"),
            self._PERIOD,
        )
        assert result is None

    def test_str_numbers_parsed(self, bot):
        result = bot._parse_twelve_response(
            self._income(revenue="999999.99"),
            self._balance(),
            self._PERIOD,
        )
        assert result is not None
        assert result["Revenues"][0] == pytest.approx(999999.99)

    def test_below_threshold_returns_none(self, bot):
        """Only 1 income field + 0 balance → below _FISCAL_FACTS_MINIMUM (4)."""
        sparse_income = {"data": [{"fiscal_date": self._PERIOD, "revenue": "1000"}]}
        sparse_balance = {"data": [{"fiscal_date": self._PERIOD}]}
        result = bot._parse_twelve_response(sparse_income, sparse_balance, self._PERIOD)
        assert result is None

    def test_error_body_returns_none(self, bot):
        error_body = {"status": "error", "code": 403, "message": "plan limit"}
        result = bot._parse_twelve_response(error_body, error_body, self._PERIOD)
        assert result is None

    def test_empty_period_returns_none(self, bot):
        result = bot._parse_twelve_response(self._income(), self._balance(), "")
        assert result is None

    def test_none_inputs_return_none(self, bot):
        result = bot._parse_twelve_response(None, None, self._PERIOD)
        assert result is None

    def test_result_tuple_format(self, bot):
        """Each value is (amount, currency, period)."""
        result = bot._parse_twelve_response(
            self._income(), self._balance(), self._PERIOD)
        assert result is not None
        for concept, val in result.items():
            assert isinstance(val, tuple), f"{concept} should be a tuple"
            assert len(val) == 3
            assert isinstance(val[0], float)
            assert val[1] == "USD"

    def test_non_numeric_field_skipped(self, bot):
        """Non-numeric values are gracefully skipped, not raised."""
        income = self._income(revenue="N/A", gross_profit="400000",
                              operating_income="200000", net_income="150000",
                              diluted_eps="2.5")
        result = bot._parse_twelve_response(income, self._balance(), self._PERIOD)
        assert result is not None
        assert "Revenues" not in result  # N/A was skipped

    def test_date_field_fallback(self, bot):
        """Record using 'date' instead of 'fiscal_date' still matches."""
        income = {"data": [{"date": self._PERIOD, "revenue": "1000000",
                             "gross_profit": "400000", "operating_income": "200000",
                             "net_income": "150000"}]}
        result = bot._parse_twelve_response(income, self._balance(), self._PERIOD)
        assert result is not None


# ─── Chain ordered trial + fallback ───────────────────────────────

class TestChainFallback:
    """Integration: gate loops through _data_source_chain(); first success wins."""

    def _mock_facts(self, data: dict):
        return MagicMock(return_value=data)

    def test_fiscalai_first_wins(self, bot, monkeypatch, tmp_path):
        """auto + both keys → fiscalai called first; if it returns facts, td not called."""
        _cfg(bot, monkeypatch, tmp_path, "auto",
             {"fiscalai": "fai-k", "twelvedata": "td-k"})

        fai_facts = {"Revenues": (1e6, "USD", "2024-09-30")}
        td_facts   = {"Assets":  (2e6, "USD", "2024-09-30")}

        with patch.object(bot, "fetch_fiscal_facts", return_value=fai_facts) as m_fai, \
             patch.object(bot, "fetch_twelvedata_facts", return_value=td_facts) as m_td:
            chain = bot._data_source_chain()
            result = None
            name = ""
            for dp in chain:
                if dp == "fiscalai":
                    result = bot.fetch_fiscal_facts("AAPL", "2024-09-30")
                elif dp == "twelvedata":
                    result = bot.fetch_twelvedata_facts("AAPL", "2024-09-30")
                if result is not None:
                    name = dp
                    break

        assert name == "fiscalai"
        assert result is fai_facts
        m_td.assert_not_called()

    def test_fiscalai_none_falls_to_twelvedata(self, bot, monkeypatch, tmp_path):
        """auto + both keys → fiscalai returns None → twelvedata tried."""
        _cfg(bot, monkeypatch, tmp_path, "auto",
             {"fiscalai": "fai-k", "twelvedata": "td-k"})

        td_facts = {"Assets": (2e6, "USD", "2024-09-30")}

        with patch.object(bot, "fetch_fiscal_facts", return_value=None), \
             patch.object(bot, "fetch_twelvedata_facts", return_value=td_facts) as m_td:
            chain = bot._data_source_chain()
            result = None
            name = ""
            for dp in chain:
                if dp == "fiscalai":
                    result = bot.fetch_fiscal_facts("AAPL", "2024-09-30")
                elif dp == "twelvedata":
                    result = bot.fetch_twelvedata_facts("AAPL", "2024-09-30")
                if result is not None:
                    name = dp
                    break

        assert name == "twelvedata"
        assert result is td_facts
        m_td.assert_called_once()

    def test_both_none_chain_empty(self, bot, monkeypatch, tmp_path):
        """Both providers return None → result stays None (EDGAR XBRL fallback)."""
        _cfg(bot, monkeypatch, tmp_path, "auto",
             {"fiscalai": "fai-k", "twelvedata": "td-k"})

        with patch.object(bot, "fetch_fiscal_facts", return_value=None), \
             patch.object(bot, "fetch_twelvedata_facts", return_value=None):
            chain = bot._data_source_chain()
            result = None
            for dp in chain:
                if dp == "fiscalai":
                    result = bot.fetch_fiscal_facts("AAPL", "2024-09-30")
                elif dp == "twelvedata":
                    result = bot.fetch_twelvedata_facts("AAPL", "2024-09-30")
                if result is not None:
                    break

        assert result is None


# ─── /setsource twelvedata ─────────────────────────────────────────

class TestSetsourceTwelvedata:
    def _fresh(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_set_twelvedata_with_key_ok(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"twelvedata": "td-k"}}))
        bot._cfg_cache = None
        out = bot.cmd_setsource(["/setsource", "twelvedata"])
        assert bot.t("setsource_ok", source="twelvedata") in out
        assert bot.t("setsource_no_key", provider="twelvedata") not in out

    def test_set_twelvedata_no_key_warns(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"api_keys": {}}))
        bot._cfg_cache = None
        out = bot.cmd_setsource(["/setsource", "twelvedata"])
        assert bot.t("setsource_no_key", provider="twelvedata") in out

    def test_set_twelvedata_persistence(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"twelvedata": "td-k"}}))
        bot._cfg_cache = None
        bot.cmd_setsource(["/setsource", "twelvedata"])
        bot._cfg_cache = None
        assert bot.get_cfg().get("facts_source") == "twelvedata"

    def test_twelvedata_in_valid_options_display(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        out = bot.cmd_setsource(["/setsource"])
        assert "twelvedata" in out


# ─── _data_source_label() all states ──────────────────────────────

class TestDataSourceLabel:
    def _setup(self, bot, monkeypatch, tmp_path, source, keys):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"facts_source": source, "api_keys": keys}))

    def test_edgar(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "edgar", {})
        assert bot._data_source_label() == "EDGAR"

    def test_fiscalai_with_key(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "fiscalai", {"fiscalai": "k"})
        label = bot._data_source_label()
        assert "fiscalai" in label
        assert "EDGAR fallback" in label

    def test_fiscalai_no_key(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "fiscalai", {})
        label = bot._data_source_label()
        assert "fiscalai" in label
        assert "no key" in label

    def test_twelvedata_with_key(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "twelvedata", {"twelvedata": "k"})
        label = bot._data_source_label()
        assert "twelvedata" in label
        assert "EDGAR fallback" in label

    def test_twelvedata_no_key(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "twelvedata", {})
        label = bot._data_source_label()
        assert "twelvedata" in label
        assert "no key" in label

    def test_auto_both_keys(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "auto",
                    {"fiscalai": "k1", "twelvedata": "k2"})
        label = bot._data_source_label()
        assert "auto" in label
        assert "fiscalai" in label
        assert "twelvedata" in label
        assert "EDGAR" in label

    def test_auto_no_keys(self, bot, monkeypatch, tmp_path):
        self._setup(bot, monkeypatch, tmp_path, "auto", {})
        label = bot._data_source_label()
        assert "auto" in label
        assert "EDGAR" in label


# ─── /addapi twelvedata ────────────────────────────────────────────

class TestAddApiTwelvedata:
    _MSG = {"chat": {"id": 0, "type": "private"}, "from": {"id": 0}}

    def _fresh(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None

    def test_addapi_stores_key(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        bot.cmd_addapi(["/addapi", "twelvedata", "td-testkey"], "0", self._MSG)
        bot._cfg_cache = None
        assert bot.get_cfg().get("api_keys", {}).get("twelvedata") == "td-testkey"

    def test_addapi_does_not_set_as_default_provider(self, bot, monkeypatch, tmp_path):
        """Twelve Data is a data provider — must never become default LLM provider."""
        self._fresh(bot, monkeypatch, tmp_path)
        bot.mutate_cfg(lambda c: c.update({"default_provider": ""}))
        bot._cfg_cache = None
        bot.cmd_addapi(["/addapi", "twelvedata", "td-testkey"], "0", self._MSG)
        bot._cfg_cache = None
        assert bot.get_cfg().get("default_provider", "") == ""

    def test_addapi_clears_twelve_memo(self, bot, monkeypatch, tmp_path):
        self._fresh(bot, monkeypatch, tmp_path)
        bot._twelve_memo[("AAPL", "2024-09-30")] = {"Revenues": (1e6, "USD", "2024-09-30")}
        bot.cmd_addapi(["/addapi", "twelvedata", "td-newkey"], "0", self._MSG)
        assert bot._twelve_memo == {}

    def test_addapi_does_not_clear_fiscal_memo(self, bot, monkeypatch, tmp_path):
        """Adding twelvedata key should not wipe fiscal memo."""
        self._fresh(bot, monkeypatch, tmp_path)
        bot._fiscal_memo[("AAPL", "2024-09-30")] = {"Revenues": (1e6, "USD", "2024-09-30")}
        bot.cmd_addapi(["/addapi", "twelvedata", "td-newkey"], "0", self._MSG)
        assert ("AAPL", "2024-09-30") in bot._fiscal_memo


# ─── /setapi twelvedata rejection ─────────────────────────────────

class TestSetApiTwelvedataRejected:
    def test_setapi_twelvedata_rejected(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        out = bot.cmd_setapi(["/setapi", "twelvedata"])
        assert bot.t("setapi_data_provider_rejected", provider="twelvedata") in out

    def test_setapi_fiscalai_still_rejected(self, bot, monkeypatch, tmp_path):
        """Regression: fiscalai rejection still works after L1 changes."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        out = bot.cmd_setapi(["/setapi", "fiscalai"])
        assert bot.t("setapi_data_provider_rejected", provider="fiscalai") in out


# ─── i18n parity EN+TR ────────────────────────────────────────────

class TestL1I18n:
    _NEW_KEYS = (
        "twelvedata_auth_error",
    )
    _UPDATED_KEYS = (
        "setsource_no_key",
        "setapi_data_provider_rejected",
    )

    def test_new_keys_exist_en(self, bot):
        strings = bot._load_lang("en")
        for k in self._NEW_KEYS:
            assert k in strings, f"Missing '{k}' in en.json"

    def test_new_keys_exist_tr(self, bot):
        strings = bot._load_lang("tr")
        for k in self._NEW_KEYS:
            assert k in strings, f"Missing '{k}' in tr.json"

    def test_setsource_no_key_has_provider_placeholder(self, bot):
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code)["setsource_no_key"]
            assert "{provider}" in tmpl, \
                f"Missing {{provider}} in setsource_no_key ({code})"

    def test_setapi_data_provider_rejected_has_provider_placeholder(self, bot):
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code)["setapi_data_provider_rejected"]
            assert "{provider}" in tmpl, \
                f"Missing {{provider}} in setapi_data_provider_rejected ({code})"

    def test_twelvedata_auth_error_nonempty(self, bot):
        for code in ("en", "tr"):
            val = bot._load_lang(code)["twelvedata_auth_error"]
            assert val.strip(), f"Empty twelvedata_auth_error ({code})"

    def test_help_block_has_twelvedata_in_setsource(self, bot):
        """help_block /setsource line must include twelvedata."""
        for code in ("en", "tr"):
            block = bot._load_lang(code)["help_block"]
            # Find the /setsource line and check it includes twelvedata
            setsource_line = next(
                (line for line in block.splitlines() if "/setsource" in line), ""
            )
            assert "twelvedata" in setsource_line, \
                f"/setsource line in help_block ({code}) missing 'twelvedata': {setsource_line!r}"

    def test_key_parity_en_tr(self, bot):
        """Every key in en.json must exist in tr.json and vice versa."""
        en = bot._load_lang("en")
        tr = bot._load_lang("tr")
        en_only = set(en) - set(tr)
        tr_only = set(tr) - set(en)
        assert not en_only, f"Keys in en.json but not tr.json: {en_only}"
        assert not tr_only, f"Keys in tr.json but not en.json: {tr_only}"


# ─── Fiscal AI regression guard ───────────────────────────────────

class TestFiscalAiRegression:
    """Existing fiscalai functionality must remain intact after L1 changes."""

    def test_setsource_fiscalai_accepted(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({"api_keys": {"fiscalai": "fai-k"}}))
        bot._cfg_cache = None
        out = bot.cmd_setsource(["/setsource", "fiscalai"])
        assert bot.t("setsource_ok", source="fiscalai") in out

    def test_addapi_fiscalai_stores_key(self, bot, monkeypatch, tmp_path):
        _msg = {"chat": {"id": 0, "type": "private"}, "from": {"id": 0}}
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.cmd_addapi(["/addapi", "fiscalai", "fai-testkey"], "0", _msg)
        bot._cfg_cache = None
        assert bot.get_cfg().get("api_keys", {}).get("fiscalai") == "fai-testkey"

    def test_chain_fiscalai_only_key(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        bot.mutate_cfg(lambda c: c.update({
            "facts_source": "auto",
            "api_keys": {"fiscalai": "fai-k"},
        }))
        assert bot._data_source_chain() == ["fiscalai"]


# ─── --network opt-in smoke test ──────────────────────────────────

import os

@pytest.mark.network
class TestTwelvedataSmoke:
    """Live API tests — run only with: pytest --network -m network"""

    @pytest.fixture(autouse=True)
    def require_key(self):
        key = os.environ.get("TWELVEDATA_API_KEY", "")
        if not key:
            pytest.skip("TWELVEDATA_API_KEY not set")
        return key

    def test_income_statement_endpoint(self, require_key, bot):
        """Smoke: income_statement endpoint returns parseable data."""
        import requests
        key = require_key
        url = (f"{bot._TWELVE_DATA_BASE}/income_statement"
               f"?symbol=AAPL&apikey={key}&period=annual")
        r = requests.get(url, timeout=20)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)

    def test_balance_sheet_endpoint(self, require_key, bot):
        """Smoke: balance_sheet endpoint returns parseable data."""
        import requests
        key = require_key
        url = (f"{bot._TWELVE_DATA_BASE}/balance_sheet"
               f"?symbol=AAPL&apikey={key}&period=annual")
        r = requests.get(url, timeout=20)
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body, dict)
