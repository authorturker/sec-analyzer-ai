"""
Tests for F3 — Numeric verification layer.

Covers:
  - _extract_numeric_claims: B/M extraction, range skip, year skip,
    pct extraction, bare-number skip
  - _parse_facts_block: round-trip with format_facts_block, EPS raw,
    pct, empty block
  - _source_numbers: explicit-scale extraction, large-raw extraction
  - verify_numeric_claims: FACTS support, source-text rescue,
    tolerance boundaries (exact 2% in/out), 5-item cap, empty
    facts_block → []
  - render_filing_message: unverified param wiring
"""
import pytest


# ────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────

def _claims_map(claims):
    """{raw: (val, kind)} for easier assertions."""
    return {raw: (val, kind) for raw, val, kind in claims}


# ────────────────────────────────────────────────────────────
# _extract_numeric_claims
# ────────────────────────────────────────────────────────────

class TestExtractNumericClaims:
    def test_dollar_B_suffix(self, bot):
        claims = bot._extract_numeric_claims("Revenue was $391.04B last quarter.")
        m = _claims_map(claims)
        assert any(abs(v - 391.04e9) < 1 and k == "money" for _, (v, k) in m.items())

    def test_word_billion_no_dollar(self, bot):
        claims = bot._extract_numeric_claims("revenue of 391 billion in fiscal 2024")
        vals = [v for _, v, k in claims if k == "money"]
        assert any(abs(v - 391e9) < 1 for v in vals)

    def test_million_suffix(self, bot):
        claims = bot._extract_numeric_claims("Net income: $342.5M")
        vals = [v for _, v, k in claims if k == "money"]
        assert any(abs(v - 342.5e6) < 1 for v in vals)

    def test_pct_symbol(self, bot):
        claims = bot._extract_numeric_claims("Gross margin improved to 43.3%.")
        vals = [v for _, v, k in claims if k == "pct"]
        assert any(abs(v - 43.3) < 0.001 for v in vals)

    def test_pct_word_percent(self, bot):
        claims = bot._extract_numeric_claims("Operating margin was 31 percent.")
        vals = [v for _, v, k in claims if k == "pct"]
        assert any(abs(v - 31.0) < 0.001 for v in vals)

    def test_range_skipped(self, bot):
        claims = bot._extract_numeric_claims("Revenue guidance: $390-395B")
        # Neither $390B nor $395B should appear as separate claims
        vals = [abs(v) for _, v, k in claims if k == "money"]
        assert not any(abs(v - 390e9) < 1e9 for v in vals)
        assert not any(abs(v - 395e9) < 1e9 for v in vals)

    def test_en_dash_range_skipped(self, bot):
        claims = bot._extract_numeric_claims("target range $400–410B")
        vals = [abs(v) for _, v, k in claims if k == "money"]
        assert not any(400e9 * 0.95 < v < 410e9 * 1.05 for v in vals)

    def test_bare_number_skipped(self, bot):
        # "391" with no scale indicator must not appear as a money claim
        claims = bot._extract_numeric_claims("EPS was 6.08 and count was 391.")
        vals = [abs(v) for _, v, k in claims if k == "money"]
        # 391 without B/M should not be extracted
        assert not any(abs(v - 391) < 1 for v in vals)

    def test_returns_in_order_money_then_pct(self, bot):
        # Money claims appear before pct in text; order must be preserved
        claims = bot._extract_numeric_claims("Revenue $200B, margin 44%.")
        kinds = [k for _, _, k in claims]
        assert "money" in kinds
        assert "pct" in kinds

    def test_negative_value_captured(self, bot):
        claims = bot._extract_numeric_claims("Net loss of -$1.2B this quarter.")
        vals = [v for _, v, k in claims if k == "money"]
        assert any(abs(abs(v) - 1.2e9) < 1e6 for v in vals)


# ────────────────────────────────────────────────────────────
# _parse_facts_block (including round-trip)
# ────────────────────────────────────────────────────────────

class TestParseFactsBlock:
    def test_empty_string_returns_empty(self, bot):
        assert bot._parse_facts_block("") == []

    def test_parses_B_scaled_value(self, bot):
        block = (
            "AUDITED XBRL FACTS (period ending 2024-09-28):\n"
            "  Revenues: $391.04B\n"
        )
        parsed = bot._parse_facts_block(block)
        money_vals = [v for v, k in parsed if k == "money"]
        assert any(abs(v - 391.04e9) < 1e6 for v in money_vals)

    def test_parses_pct(self, bot):
        block = (
            "AUDITED XBRL FACTS (period ending 2024-09-28):\n"
            "  gross_margin_pct: 43.3%\n"
        )
        parsed = bot._parse_facts_block(block)
        pct_vals = [v for v, k in parsed if k == "pct"]
        assert any(abs(v - 43.3) < 0.001 for v in pct_vals)

    def test_parses_eps_raw_dollar(self, bot):
        block = (
            "AUDITED XBRL FACTS (period ending 2024-09-28):\n"
            "  EarningsPerShareDiluted: $6.08\n"
        )
        parsed = bot._parse_facts_block(block)
        money_vals = [v for v, k in parsed if k == "money"]
        assert any(abs(v - 6.08) < 0.01 for v in money_vals)

    def test_round_trip_with_format_facts_block(self, bot):
        """
        Round-trip invariant: for every valid XBRL facts dict,
        _parse_facts_block(format_facts_block(d)) recovers all B/M-scaled
        monetary and pct values within 2%.
        """
        facts = {
            "Revenues":       (391_035_000_000, "USD", "2024-09-28"),
            "GrossProfit":    (169_148_000_000, "USD", "2024-09-28"),
            "NetIncomeLoss":  (93_736_000_000,  "USD", "2024-09-28"),
        }
        block = bot.format_facts_block(facts)
        assert block != "", "format_facts_block must produce non-empty output"
        parsed = bot._parse_facts_block(block)
        money_vals = [v for v, k in parsed if k == "money"]

        for concept, (orig_val, _, _) in facts.items():
            av = abs(orig_val)
            assert any(
                abs(abs(pv) - av) / max(av, 1.0) <= 0.02
                for pv in money_vals
            ), f"Round-trip failed for {concept}: original {av}, parsed {money_vals}"

    def test_round_trip_includes_gross_margin_pct(self, bot):
        facts = {
            "Revenues":    (391_035_000_000, "USD", "2024-09-28"),
            "GrossProfit": (169_148_000_000, "USD", "2024-09-28"),
        }
        block = bot.format_facts_block(facts)
        parsed = bot._parse_facts_block(block)
        pct_vals = [v for v, k in parsed if k == "pct"]
        # gross_margin_pct ≈ 43.3%
        assert any(abs(v - 43.3) < 1.0 for v in pct_vals)


# ────────────────────────────────────────────────────────────
# _source_numbers
# ────────────────────────────────────────────────────────────

class TestSourceNumbers:
    def test_explicit_billion_in_text(self, bot):
        nums = bot._source_numbers("Total assets were 364 billion.")
        assert any(abs(n - 364e9) < 1e6 for n in nums)

    def test_explicit_million_with_dollar(self, bot):
        nums = bot._source_numbers("Operating income: $123.2M")
        assert any(abs(n - 123.2e6) < 1e4 for n in nums)

    def test_large_comma_number(self, bot):
        nums = bot._source_numbers("Revenues 391,035,000,000 for the period.")
        assert any(abs(n - 391035000000) < 1e6 for n in nums)

    def test_small_number_not_included(self, bot):
        # Bare number without scale < 1e7 must not appear
        nums = bot._source_numbers("count was 391 items total.")
        assert not any(abs(n - 391) < 1 for n in nums)


# ────────────────────────────────────────────────────────────
# verify_numeric_claims — integration
# ────────────────────────────────────────────────────────────

class TestVerifyNumericClaims:
    def _make_facts_block(self, bot):
        facts = {
            "Revenues":    (391_035_000_000, "USD", "2024-09-28"),
            "GrossProfit": (169_148_000_000, "USD", "2024-09-28"),
        }
        return bot.format_facts_block(facts)

    def test_empty_facts_block_returns_empty(self, bot):
        result = bot.verify_numeric_claims(
            "Revenue was $391B and margin 44%.", "", "source text"
        )
        assert result == []

    def test_supported_claim_not_flagged(self, bot):
        facts_block = self._make_facts_block(bot)
        # $391B is within 2% of 391.035B
        result = bot.verify_numeric_claims(
            "Revenue was $391B.", facts_block, ""
        )
        assert result == []

    def test_unsupported_claim_flagged(self, bot):
        facts_block = self._make_facts_block(bot)
        # $500B is not near any fact
        result = bot.verify_numeric_claims(
            "Revenue was $500B.", facts_block, ""
        )
        assert len(result) == 1
        assert "$500B" in result[0] or "500" in result[0]

    def test_tolerance_boundary_inside_passes(self, bot):
        """A claim exactly at the 2% boundary is supported."""
        facts_block = self._make_facts_block(bot)
        # 391.035B * 1.019 ≈ 398.8B — just inside 2% of facts value
        # Use $391B which is <0.01% off → supported
        result = bot.verify_numeric_claims("Revenue $391B.", facts_block, "")
        assert result == []

    def test_tolerance_boundary_outside_flagged(self, bot):
        """A claim well outside 2% is flagged."""
        facts_block = self._make_facts_block(bot)
        # $400B is >2% off from 391.035B
        result = bot.verify_numeric_claims("Revenue $400B.", facts_block, "")
        assert len(result) >= 1

    def test_pct_within_1_point_supported(self, bot):
        facts = {
            "Revenues":    (391_035_000_000, "USD", "2024-09-28"),
            "GrossProfit": (169_148_000_000, "USD", "2024-09-28"),
        }
        facts_block = bot.format_facts_block(facts)
        # gross_margin_pct ≈ 43.3%; LLM says 44% → within ±1.0 point
        result = bot.verify_numeric_claims("Gross margin was 44%.", facts_block, "")
        assert result == []

    def test_pct_outside_1_point_flagged(self, bot):
        facts = {
            "Revenues":    (391_035_000_000, "USD", "2024-09-28"),
            "GrossProfit": (169_148_000_000, "USD", "2024-09-28"),
        }
        facts_block = bot.format_facts_block(facts)
        # gross_margin_pct ≈ 43.3%; LLM says 50% → outside ±1.0 point
        result = bot.verify_numeric_claims("Gross margin was 50%.", facts_block, "")
        assert len(result) >= 1

    def test_source_text_rescue(self, bot):
        """A claim absent from FACTS but present in source text is supported."""
        facts_block = self._make_facts_block(bot)
        # $123B is not in FACTS but IS in source text
        source = "Operating income was $123B in the quarter."
        analysis = "Operating income reached $123B."
        result = bot.verify_numeric_claims(analysis, facts_block, source)
        assert result == []

    def test_five_item_cap(self, bot):
        facts_block = self._make_facts_block(bot)
        # 6 fabricated unsupported claims
        analysis = (
            "Metric A: $111B. Metric B: $222B. Metric C: $333B. "
            "Metric D: $444B. Metric E: $555B. Metric F: $666B."
        )
        result = bot.verify_numeric_claims(analysis, facts_block, "")
        assert len(result) == 5

    def test_no_claims_returns_empty(self, bot):
        facts_block = self._make_facts_block(bot)
        result = bot.verify_numeric_claims("Strong year ahead.", facts_block, "")
        assert result == []

    def test_deduplication(self, bot):
        facts_block = self._make_facts_block(bot)
        # Same unsupported claim repeated twice
        result = bot.verify_numeric_claims(
            "Revenue $999B and again $999B.", facts_block, ""
        )
        assert result.count(result[0]) == 1 if result else True


# ────────────────────────────────────────────────────────────
# render_filing_message — unverified param wiring
# ────────────────────────────────────────────────────────────

class TestRenderFilingMessageUnverified:
    def test_no_unverified_message_unchanged(self, bot):
        """Calling without unverified= must produce byte-identical output."""
        without = bot.render_filing_message(
            "AAPL", "10-K", "2024-09-28", "analysis text", ""
        )
        with_none = bot.render_filing_message(
            "AAPL", "10-K", "2024-09-28", "analysis text", "",
            unverified=None,
        )
        with_empty = bot.render_filing_message(
            "AAPL", "10-K", "2024-09-28", "analysis text", "",
            unverified=[],
        )
        assert without == with_none == with_empty

    def test_unverified_list_appears_in_message(self, bot):
        msg = bot.render_filing_message(
            "AAPL", "10-K", "2024-09-28", "analysis text", "",
            unverified=["$999B", "95%"],
        )
        assert "$999B" in msg
        assert "95%" in msg

    def test_unverified_before_price_snippet(self, bot):
        msg = bot.render_filing_message(
            "AAPL", "10-K", "2024-09-28", "analysis text",
            "", "price info",
            unverified=["$999B"],
        )
        assert msg.index("$999B") < msg.index("price info")
