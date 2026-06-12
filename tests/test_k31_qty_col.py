"""
Tests for K3.1 — QTY column clamping in /pnl monospace table.

Coverage:
- _fmt_qty_col: width enforcement, abbreviation logic, rjust padding
- _pnl_table: all rows equal width (37) for fractional/large qty inputs
- No calc bleed: VALUE/P&L% use raw qty, not cosmetic qty_col
"""
import pytest


# ─── _fmt_qty_col unit tests ──────────────────────────────────

class TestFmtQtyCol:
    def test_small_int_fits(self, bot):
        """Integer ≤ 5 chars: no abbreviation, right-justified."""
        assert bot._fmt_qty_col(10) == "   10"

    def test_whole_number_at_boundary(self, bot):
        """99999 is exactly 5 chars — fits without abbreviation."""
        assert bot._fmt_qty_col(99999) == "99999"

    def test_decimal_12_345_fits_in_5(self, bot):
        """12.345 → raw '12.345' (6 chars) → reduce to '12.35' (5 chars)."""
        result = bot._fmt_qty_col(12.345)
        assert len(result) == 5
        assert result == "12.35"

    def test_decimal_0_123456(self, bot):
        """0.123456 → raw '0.123456' (8 chars) → reduce to '0.123' (5 chars)."""
        result = bot._fmt_qty_col(0.123456)
        assert len(result) == 5
        assert result == "0.123"

    def test_large_int_123456_abbrev_k(self, bot):
        """123456 → raw '123456' (6 chars) → '123k' (4 chars), rjust to 5."""
        result = bot._fmt_qty_col(123456)
        assert len(result) == 5
        assert "k" in result

    def test_large_float_1234567_8_abbrev_M(self, bot):
        """1234567.8 → raw '1.23457e+06' → '1.2M' (4 chars), rjust to 5."""
        result = bot._fmt_qty_col(1234567.8)
        assert len(result) == 5
        assert "M" in result

    def test_10k_boundary_k_abbrev(self, bot):
        """12345.6 → raw '12345.6' (7 chars) → k abbreviation."""
        result = bot._fmt_qty_col(12345.6)
        assert len(result) == 5
        assert "k" in result

    def test_result_never_exceeds_width(self, bot):
        """Parametric: many qty values — result always ≤ 5 chars."""
        test_qtys = [
            0.001, 0.12345, 1.23456, 9.9999, 12.345, 99.999,
            999.9, 9999.9, 10000.1, 12345.6, 99999.9,
            100000, 123456, 999999, 1_000_000, 1234567.8,
            9_999_999, 12_345_678,
        ]
        for qty in test_qtys:
            result = bot._fmt_qty_col(qty)
            assert len(result) <= 5, f"_fmt_qty_col({qty}) = {repr(result)} exceeds 5 chars"

    def test_result_is_right_justified(self, bot):
        """Result should always be exactly width chars (rjust pads on left)."""
        for qty in [1.0, 5.5, 100.0, 999.0]:
            result = bot._fmt_qty_col(qty, width=5)
            assert len(result) == 5


# ─── _pnl_table row-width tests with large/fractional qty ─────

def _make_row(ticker="AAPL", qty=10.0, avg_cost=150.0, last=185.0):
    if last is None:
        return {"ticker": ticker, "qty": qty, "avg_cost": avg_cost,
                "last": None, "value": None, "pnl_usd": None, "pnl_pct": None}
    value = qty * last
    cost_b = qty * avg_cost
    pnl_usd = value - cost_b
    pnl_pct = (pnl_usd / cost_b * 100.0) if avg_cost != 0 else None
    return {"ticker": ticker, "qty": qty, "avg_cost": avg_cost,
            "last": last, "value": value, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct}


def _safe_last(qty: float) -> float:
    """Return a last price that keeps qty*last < 1_000_000 (fits W_VAL=8 with $)."""
    return min(20.0, 999_999 / max(qty, 1))


class TestPnlTableQtyOverflow:
    """All four qty values from K3.1 acceptance criteria.

    Prices are chosen so VALUE stays within W_VAL=8 chars — this test is only
    about the QTY column; VALUE overflow is a separate pre-existing concern.
    """

    def _assert_uniform_width(self, bot, qty):
        last = _safe_last(qty)
        rows = [_make_row("AAPL", qty=qty, avg_cost=last * 0.9, last=last)]
        table = bot._pnl_table(rows)
        lines = table.splitlines()
        widths = [len(l) for l in lines]
        assert len(set(widths)) == 1, \
            f"qty={qty}: unequal row widths {widths}\n{table}"
        assert widths[0] <= 38, \
            f"qty={qty}: row width {widths[0]} > 38"

    def test_qty_12_345(self, bot):
        self._assert_uniform_width(bot, 12.345)

    def test_qty_0_123456(self, bot):
        self._assert_uniform_width(bot, 0.123456)

    def test_qty_123456(self, bot):
        self._assert_uniform_width(bot, 123456)

    def test_qty_1234567_8(self, bot):
        self._assert_uniform_width(bot, 1234567.8)

    def test_mixed_qty_types_uniform(self, bot):
        """Table with all four problematic qty values: every row same width.
        Prices scaled so VALUE stays within 8-char column.
        """
        rows = [
            _make_row("A", qty=12.345,     avg_cost=10,    last=20),
            _make_row("B", qty=0.123456,   avg_cost=5,     last=10),
            _make_row("C", qty=123456,     avg_cost=1,     last=2),      # value=$246,912 (8 chars)
            _make_row("D", qty=1234567.8,  avg_cost=0.1,   last=0.5),   # value=$617,284 (8 chars)
        ]
        table = bot._pnl_table(rows)
        lines = table.splitlines()
        widths = [len(l) for l in lines]
        assert len(set(widths)) == 1, f"Mixed qty table unequal widths: {widths}\n{table}"


# ─── Calc bleed: VALUE/P&L must use raw qty ───────────────────

class TestNoCalcBleed:
    """Cosmetic abbreviation must not affect VALUE or P&L% in the table."""

    def test_value_uses_raw_qty_not_abbrev(self, bot):
        """VALUE column reflects qty*last with raw qty, not abbreviated qty."""
        qty = 123456
        last = 2.0
        expected_value = qty * last  # 246912
        rows = [_make_row("AAPL", qty=qty, avg_cost=1.0, last=last)]
        table = bot._pnl_table(rows)
        # Formatted value should be $246,912 or similar (rounded int)
        assert "246" in table, f"Expected value ~246912 in table:\n{table}"

    def test_pnl_pct_uses_raw_qty(self, bot):
        """P&L% uses raw qty in calculation: 123456 shares @ $1 cost, $2 last → +100%."""
        qty = 123456
        rows = [_make_row("AAPL", qty=qty, avg_cost=1.0, last=2.0)]
        table = bot._pnl_table(rows)
        assert "+100.0%" in table, f"Expected +100.0% in table:\n{table}"

    def test_fractional_qty_value_correct(self, bot):
        """0.123456 shares @ $100 cost, $200 last → VALUE ~$24 (0.123456 * 200)."""
        qty = 0.123456
        rows = [_make_row("TINY", qty=qty, avg_cost=100.0, last=200.0)]
        table = bot._pnl_table(rows)
        # value = 0.123456 * 200 = 24.6912 → rounded $25
        assert "$25" in table or "$24" in table, \
            f"Expected ~$25 in table:\n{table}"
