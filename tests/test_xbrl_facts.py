"""
Offline tests for F1 XBRL fact extraction helpers.

All tests are network-free. _normalize_xbrl_facts and format_facts_block are
pure functions tested directly. fetch_xbrl_facts is tested with fake filing
objects to verify it never raises and handles every edge case.
"""
import pytest
import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# conftest.py installs stubs and imports bot; re-use that.
import bot  # noqa: E402  (stub already installed by conftest)


# ── Helpers ────────────────────────────────────────────────

def _make_raw(concept: str, entries: list) -> dict:
    """Convenience: build a raw_facts dict with one concept."""
    return {concept: entries}


def _entry(value=100.0, unit="USD", period_end="2024-09-30",
           period_type="duration", duration_days=365):
    return (value, unit, period_end, period_type, duration_days)


# ── _normalize_xbrl_facts ──────────────────────────────────

class TestNormalizeXbrlFacts:

    def test_empty_dict_returns_empty(self):
        assert bot._normalize_xbrl_facts({}) == {}

    def test_single_entry_returned(self):
        raw = _make_raw("Revenues", [_entry(500e9)])
        result = bot._normalize_xbrl_facts(raw)
        assert "Revenues" in result
        assert result["Revenues"][0] == 500e9

    def test_selects_most_recent_period_end(self):
        """When two entries differ only in period_end, the later date wins."""
        raw = _make_raw("NetIncomeLoss", [
            _entry(100.0, period_end="2023-09-30"),
            _entry(200.0, period_end="2024-09-30"),
        ])
        result = bot._normalize_xbrl_facts(raw)
        assert result["NetIncomeLoss"][0] == 200.0
        assert result["NetIncomeLoss"][2] == "2024-09-30"

    def test_ties_broken_by_shortest_duration(self):
        """Same period_end → prefer shortest duration (quarterly > annual cumulative)."""
        raw = _make_raw("OperatingIncomeLoss", [
            _entry(50.0,  period_end="2024-09-30", duration_days=365),
            _entry(20.0,  period_end="2024-09-30", duration_days=90),
        ])
        result = bot._normalize_xbrl_facts(raw)
        assert result["OperatingIncomeLoss"][0] == 20.0  # quarterly (90-day)

    def test_negative_value_preserved(self):
        raw = _make_raw("NetIncomeLoss", [_entry(-1_200_000_000.0)])
        result = bot._normalize_xbrl_facts(raw)
        assert result["NetIncomeLoss"][0] == -1_200_000_000.0

    def test_zero_value_kept(self):
        """Zero is a valid fact and must not be dropped."""
        raw = _make_raw("GrossProfit", [_entry(0.0)])
        result = bot._normalize_xbrl_facts(raw)
        assert "GrossProfit" in result
        assert result["GrossProfit"][0] == 0.0

    def test_partial_whitelist_valid(self):
        """Only a subset of whitelist concepts is fine."""
        raw = {
            "Revenues": [_entry(100e9)],
            "Assets":   [_entry(200e9)],
        }
        result = bot._normalize_xbrl_facts(raw)
        assert len(result) == 2
        assert "GrossProfit" not in result

    def test_multiple_concepts_all_selected(self):
        raw = {
            "Revenues":     [_entry(100e9, period_end="2024-09-30")],
            "GrossProfit":  [_entry(40e9,  period_end="2024-09-30")],
            "NetIncomeLoss":[_entry(20e9,  period_end="2024-09-30")],
        }
        result = bot._normalize_xbrl_facts(raw)
        assert len(result) == 3

    def test_unit_preserved_in_output(self):
        raw = _make_raw("Assets", [_entry(500e9, unit="iso4217:EUR")])
        result = bot._normalize_xbrl_facts(raw)
        assert result["Assets"][1] == "iso4217:EUR"


# ── format_facts_block ─────────────────────────────────────

class TestFormatFactsBlock:

    def test_empty_dict_returns_empty_string(self):
        assert bot.format_facts_block({}) == ""

    def test_header_contains_period_date(self):
        facts = {"Revenues": (391e9, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "2024-09-28" in block
        assert block.startswith("AUDITED XBRL FACTS")

    def test_billion_scale_formatting(self):
        facts = {"Revenues": (8_710_000_000.0, "USD", "2024-09-28")}
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
        # Must not apply B/M/K scaling
        assert "B" not in block.split("EarningsPerShareDiluted")[-1].split("\n")[0]

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
            "Revenues":    (100_000_000_000.0, "USD", "2024-09-28"),
            "GrossProfit": (44_000_000_000.0,  "USD", "2024-09-28"),
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
            "Revenues":    (0.0, "USD", "2024-09-28"),
            "GrossProfit": (5e9, "USD", "2024-09-28"),
        }
        block = bot.format_facts_block(facts)
        assert "gross_margin_pct" not in block

    def test_block_length_cap_600(self):
        """Block must be truncated to ≤600 characters."""
        # Craft a facts dict that would produce a long block
        facts = {concept: (1_234_567_890.12, "USD", "2024-09-28")
                 for concept in bot._XBRL_DISPLAY_ORDER}
        block = bot.format_facts_block(facts)
        assert len(block) <= 600

    def test_zero_value_not_omitted(self):
        facts = {"GrossProfit": (0.0, "USD", "2024-09-28")}
        block = bot.format_facts_block(facts)
        assert "GrossProfit" in block

    def test_display_order_respected(self):
        """Revenues appears before Assets in the block."""
        facts = {
            "Assets":   (200e9, "USD", "2024-09-28"),
            "Revenues": (100e9, "USD", "2024-09-28"),
        }
        block = bot.format_facts_block(facts)
        assert block.index("Revenues") < block.index("Assets")


# ── fetch_xbrl_facts (no-exception contract) ──────────────

class _FakeFact:
    def __init__(self, element_id, context_ref, numeric_value, unit_ref="USD"):
        self.element_id   = element_id
        self.context_ref  = context_ref
        self.numeric_value = numeric_value
        self.unit_ref      = unit_ref


class _FakeContext:
    def __init__(self, period: dict, dimensions: dict | None = None):
        self.period     = period
        self.dimensions = dimensions or {}


class _FakeFiling:
    """Fake edgartools Filing with configurable xbrl() return."""
    def __init__(self, xbrl_obj):
        self._xbrl_obj = xbrl_obj

    def xbrl(self):
        return self._xbrl_obj


class _FakeXBRL:
    def __init__(self, facts_dict: dict, contexts_dict: dict):
        self._facts   = facts_dict
        self.contexts = contexts_dict


def _duration_ctx(end: str, start: str) -> _FakeContext:
    return _FakeContext({"type": "duration", "endDate": end, "startDate": start})

def _instant_ctx(date: str) -> _FakeContext:
    return _FakeContext({"type": "instant", "instant": date})


class TestFetchXbrlFacts:

    def test_returns_none_when_xbrl_is_none(self):
        filing = _FakeFiling(None)
        result = bot.fetch_xbrl_facts(filing)
        assert result is None

    def test_never_raises_on_exception_in_xbrl(self):
        class _BrokenFiling:
            def xbrl(self):
                raise RuntimeError("network gone")

        result = bot.fetch_xbrl_facts(_BrokenFiling())
        assert result is None

    def test_never_raises_when_facts_dict_missing(self):
        """Filing.xbrl() returns object with no _facts attribute."""
        class _WeirdXBRL:
            contexts = {}
        result = bot.fetch_xbrl_facts(_FakeFiling(_WeirdXBRL()))
        assert result is None

    def test_basic_revenue_fact_extracted(self):
        ctx = _duration_ctx("2024-09-30", "2023-10-01")
        xbrl = _FakeXBRL(
            {"Revenues_c1": _FakeFact("Revenues", "c1", 391_035_000_000.0)},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is not None
        assert "Revenues" in result
        assert result["Revenues"][0] == pytest.approx(391_035_000_000.0)

    def test_namespaced_concept_stripped(self):
        ctx = _duration_ctx("2024-09-30", "2023-10-01")
        xbrl = _FakeXBRL(
            {"us-gaap:NetIncomeLoss_c1": _FakeFact("us-gaap:NetIncomeLoss", "c1", 93_736_000_000.0)},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is not None
        assert "NetIncomeLoss" in result

    def test_dimensional_fact_skipped(self):
        """Facts with non-empty context dimensions (segment data) must be excluded."""
        ctx = _FakeContext(
            {"type": "duration", "endDate": "2024-09-30", "startDate": "2023-10-01"},
            dimensions={"us-gaap:SegmentReportingAxis": "iPhone"},
        )
        xbrl = _FakeXBRL(
            {"Revenues_c1": _FakeFact("Revenues", "c1", 100e9)},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is None  # nothing left after dimensional filter

    def test_shares_unit_filtered_for_monetary_concept(self):
        ctx = _duration_ctx("2024-09-30", "2023-10-01")
        xbrl = _FakeXBRL(
            {"Assets_c1": _FakeFact("Assets", "c1", 5_000_000.0, unit_ref="shares")},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is None

    def test_fallback_concept_promoted_to_primary(self):
        """RevenueFromContractWithCustomerExcludingAssessedTax → Revenues key."""
        ctx = _duration_ctx("2024-03-31", "2023-04-01")
        xbrl = _FakeXBRL(
            {
                "RevenueFromContractWithCustomerExcludingAssessedTax_c1":
                    _FakeFact("RevenueFromContractWithCustomerExcludingAssessedTax", "c1", 50e9),
            },
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is not None
        assert "Revenues" in result
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" not in result

    def test_primary_wins_over_fallback_when_both_present(self):
        ctx = _duration_ctx("2024-03-31", "2023-04-01")
        xbrl = _FakeXBRL(
            {
                "Revenues_c1":
                    _FakeFact("Revenues", "c1", 100e9),
                "RevenueFromContractWithCustomerExcludingAssessedTax_c1":
                    _FakeFact("RevenueFromContractWithCustomerExcludingAssessedTax", "c1", 90e9),
            },
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is not None
        assert result["Revenues"][0] == pytest.approx(100e9)
        assert "RevenueFromContractWithCustomerExcludingAssessedTax" not in result

    def test_out_of_whitelist_concept_ignored(self):
        ctx = _duration_ctx("2024-09-30", "2023-10-01")
        xbrl = _FakeXBRL(
            {"SomeObscureLine_c1": _FakeFact("SomeObscureLine", "c1", 99e9)},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is None

    def test_instant_context_duration_zero(self):
        ctx = _instant_ctx("2024-09-28")
        xbrl = _FakeXBRL(
            {"Assets_c1": _FakeFact("Assets", "c1", 364_980_000_000.0)},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is not None
        assert "Assets" in result

    def test_none_numeric_value_skipped(self):
        ctx = _duration_ctx("2024-09-30", "2023-10-01")
        xbrl = _FakeXBRL(
            {"Revenues_c1": _FakeFact("Revenues", "c1", None)},
            {"c1": ctx},
        )
        result = bot.fetch_xbrl_facts(_FakeFiling(xbrl))
        assert result is None
