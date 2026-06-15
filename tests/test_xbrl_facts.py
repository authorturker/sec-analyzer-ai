"""
Offline tests for XBRL fact extraction helpers.

All tests are network-free. format_facts_block is a pure function tested
directly. fetch_company_facts is tested with fake Company objects.
"""
import pytest
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import bot  # noqa: E402


# ── format_facts_block ─────────────────────────────────────

class TestFormatFactsBlock:

    def test_empty_dict_returns_empty_string(self):
        assert bot.format_facts_block({}) == ""

    def test_header_contains_period_date(self):
        facts = {"RevenueFromContractWithCustomerExcludingAssessedTax": (391e9, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "2024-09-28" in block
        assert block.startswith("AUDITED XBRL FACTS")

    def test_billion_scale_formatting(self):
        facts = {"RevenueFromContractWithCustomerExcludingAssessedTax": (8_710_000_000.0, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "$8.71B" in block

    def test_million_scale_formatting(self):
        facts = {"GrossProfit": (342_500_000.0, "USD", "2024-03-31")}
        block = bot.format_facts_block(facts)
        assert "$342.5M" in block

    def test_eps_raw_no_scaling(self):
        facts = {"EarningsPerShareDiluted": (1.23, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "$1.23" in block

    def test_negative_value_shows_sign(self):
        facts = {"NetIncomeLoss": (-1_200_000_000.0, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "-$1.20B" in block

    def test_non_usd_currency_prefix(self):
        facts = {"Assets": (1_200_000_000.0, "iso4217:EUR", "2024-12-31")}
        block = bot.format_facts_block(facts)
        assert "EUR 1.20B" in block

    def test_gross_margin_derived(self):
        facts = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": (100_000_000_000.0, "USD", "2024-09-28"),
            "GrossProfit": (44_000_000_000.0, "USD", "2024-09-28"),
        }
        block = bot.format_facts_block(facts)
        assert "44.0%" in block
        assert "gross_margin_pct" in block

    def test_no_gross_margin_when_revenue_missing(self):
        facts = {"GrossProfit": (44e9, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "gross_margin_pct" not in block

    def test_no_gross_margin_when_revenue_zero(self):
        facts = {
            "RevenueFromContractWithCustomerExcludingAssessedTax": (0.0, "USD", "2024-09-28"),
            "GrossProfit": (5e9, "USD", "2024-09-28"),
        }
        block = bot.format_facts_block(facts)
        assert "gross_margin_pct" not in block

    def test_block_length_cap_600(self):
        facts = {concept: (1_234_567_890.12, "USD", "2024-09-28")
                 for concept in bot._XBRL_DISPLAY_ORDER}
        block = bot.format_facts_block(facts)
        assert len(block) <= 600

    def test_zero_value_not_omitted(self):
        facts = {"GrossProfit": (0.0, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "GrossProfit" in block

    def test_display_order_respected(self):
        facts = {
            "latest": {
                "Assets":   (200e9, "USD", "2024-09-28"),
                "RevenueFromContractWithCustomerExcludingAssessedTax": (100e9, "USD", "2024-09-28"),
            },
            "years": {},
        }
        block = bot.format_facts_block(facts)
        rev_key = "RevenueFromContractWithCustomerExcludingAssessedTax"
        # Revenue short name "Revenue" should appear before "Assets"
        assert block.index("Revenue") < block.index("Assets")


# ── fetch_company_facts (no-exception contract) ────────────

import pandas as pd

class _FakeDataFrame:
    """Minimal DataFrame-like object for testing."""
    def __init__(self, data: dict, columns: list):
        self._data = data
        self.columns = columns

    def __getitem__(self, key):
        if isinstance(key, str):
            return pd.Series({k: v[key] for k, v in self._data.items() if key in v})
        return self

    def loc(self, key, col):
        return self._data.get(key, {}).get(col)


class _FakeStatement:
    def __init__(self, df):
        self._df = df
    def to_dataframe(self):
        return self._df


class _FakeCompany:
    def __init__(self, inc_df=None, bs_df=None):
        self._inc = inc_df
        self._bs = bs_df

    def income_statement(self, periods=1):
        if self._inc is None:
            raise RuntimeError("no income data")
        return _FakeStatement(self._inc)

    def balance_sheet(self, periods=1):
        if self._bs is None:
            raise RuntimeError("no balance data")
        return _FakeStatement(self._bs)


def _make_inc_df(revenue=416e9, gross_profit=195e9, operating=133e9,
                  net_income=112e9, eps=7.46):
    data = {
        "RevenueFromContractWithCustomerExcludingAssessedTax": {"FY 2025": revenue},
        "GrossProfit": {"FY 2025": gross_profit},
        "OperatingIncomeLoss": {"FY 2025": operating},
        "NetIncomeLoss": {"FY 2025": net_income},
        "EarningsPerShareDiluted": {"FY 2025": eps},
    }
    return pd.DataFrame(data, index=["FY 2025"]).T


def _make_bs_df(cash=36e9, assets=359e9, liabilities=286e9, equity=74e9):
    data = {
        "CashAndCashEquivalentsAtCarryingValue": {"FY 2025": cash},
        "Assets": {"FY 2025": assets},
        "Liabilities": {"FY 2025": liabilities},
        "StockholdersEquity": {"FY 2025": equity},
    }
    return pd.DataFrame(data, index=["FY 2025"]).T


class TestFetchCompanyFacts:

    def test_returns_facts_on_success(self, bot, monkeypatch):
        company = _FakeCompany(inc_df=_make_inc_df(), bs_df=_make_bs_df())
        monkeypatch.setattr(bot, "get_company", lambda tk: company)
        result = bot.fetch_company_facts("AAPL")
        assert result is not None
        assert "latest" in result
        assert "years" in result
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" in result["latest"]
        assert result["latest"]["RevenueFromContractWithCustomerExcludingAssessedTax"][0] == pytest.approx(416e9)

    def test_returns_none_on_income_failure(self, bot, monkeypatch):
        company = _FakeCompany(inc_df=None)
        monkeypatch.setattr(bot, "get_company", lambda tk: company)
        result = bot.fetch_company_facts("AAPL")
        assert result is None

    def test_continues_when_balance_sheet_fails(self, bot, monkeypatch):
        company = _FakeCompany(inc_df=_make_inc_df(), bs_df=None)
        monkeypatch.setattr(bot, "get_company", lambda tk: company)
        result = bot.fetch_company_facts("AAPL")
        assert result is not None
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" in result["latest"]
        assert "Assets" not in result["latest"]

    def test_never_raises_on_exception(self, bot, monkeypatch):
        def _boom(tk):
            raise RuntimeError("network gone")
        monkeypatch.setattr(bot, "get_company", _boom)
        result = bot.fetch_company_facts("AAPL")
        assert result is None

    def test_empty_data_returns_none(self, bot, monkeypatch):
        empty_inc = pd.DataFrame(columns=["FY 2025"])
        company = _FakeCompany(inc_df=empty_inc, bs_df=empty_inc)
        monkeypatch.setattr(bot, "get_company", lambda tk: company)
        result = bot.fetch_company_facts("AAPL")
        assert result is None
