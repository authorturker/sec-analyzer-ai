"""
Tests for E1 — price action (pure compute helpers and snippet formatting).
Network IO (compute_price_snippet) is not exercised here; pure helpers
cover the logic. _compute_price_change accepts (date, close) pairs
regardless of whether they came from yfinance or any other source.
"""
import pytest


# ─── _compute_price_change ────────────────────────────────
class TestComputePriceChange:
    def test_empty_returns_none(self, bot):
        assert bot._compute_price_change([], "2026-04-01", 5) is None

    def test_invalid_filing_date_returns_none(self, bot):
        rows = [("2026-04-01", 100.0), ("2026-04-08", 105.0)]
        assert bot._compute_price_change(rows, "not-a-date", 5) is None

    def test_normal_positive_change(self, bot):
        rows = [
            ("2026-04-01", 100.0),
            ("2026-04-02", 101.0),
            ("2026-04-08", 105.0),
        ]
        out = bot._compute_price_change(rows, "2026-04-01", 5)
        assert out is not None
        assert out["start_date"] == "2026-04-01"
        assert out["start_close"] == pytest.approx(100.0)
        # 5 days after 2026-04-01 is 2026-04-06 — first row >= is 2026-04-08
        assert out["end_date"] == "2026-04-08"
        assert out["end_close"] == pytest.approx(105.0)
        assert out["pct"] == pytest.approx(5.0)

    def test_negative_change(self, bot):
        rows = [("2026-04-01", 100.0), ("2026-04-09", 92.0)]
        out = bot._compute_price_change(rows, "2026-04-01", 5)
        assert out["pct"] == pytest.approx(-8.0)

    def test_filing_on_weekend_uses_next_trading_day(self, bot):
        # filing on Saturday 2026-04-04, first row >= is Monday 2026-04-06
        rows = [
            ("2026-04-06", 100.0),
            ("2026-04-13", 103.0),
        ]
        out = bot._compute_price_change(rows, "2026-04-04", 5)
        assert out["start_date"] == "2026-04-06"
        assert out["end_date"] == "2026-04-13"
        assert out["pct"] == pytest.approx(3.0)

    def test_only_one_data_point_returns_none(self, bot):
        rows = [("2026-04-01", 100.0)]
        # start and end would be same row — invalid
        assert bot._compute_price_change(rows, "2026-04-01", 5) is None

    def test_no_data_after_filing_returns_none(self, bot):
        rows = [("2026-03-01", 100.0), ("2026-03-15", 105.0)]
        # No row on or after 2026-04-01
        assert bot._compute_price_change(rows, "2026-04-01", 5) is None


# ─── _format_price_snippet ────────────────────────────────
class TestFormatPriceSnippet:
    def test_none_returns_empty(self, bot):
        assert bot._format_price_snippet(None) == ""
        assert bot._format_price_snippet({}) == ""

    def test_positive_uses_up_emoji(self, bot):
        out = bot._format_price_snippet({
            "start_date": "2026-04-01", "start_close": 100.0,
            "end_date":   "2026-04-08", "end_close":   105.0,
            "pct":        5.0,
        })
        assert "📈" in out
        assert "+5.00" in out
        assert "2026-04-01" in out
        assert "2026-04-08" in out

    def test_negative_uses_down_emoji(self, bot):
        out = bot._format_price_snippet({
            "start_date": "2026-04-01", "start_close": 100.0,
            "end_date":   "2026-04-08", "end_close":   95.0,
            "pct":        -5.0,
        })
        assert "📉" in out
        assert "-5.00" in out


# ─── render_filing_message with price_snippet ─────────────
class TestRenderWithPrice:
    def test_no_snippet_default_behavior(self, bot):
        msg = bot.render_filing_message("AAPL", "10-K", "2026", "body", "")
        # No price text
        assert "Price action" not in msg
        assert "Fiyat hareketi" not in msg

    def test_snippet_appears_above_separator(self, bot):
        msg = bot.render_filing_message(
            "AAPL", "10-K", "2026", "body", "",
            price_snippet="📈 *Price action:* `+3.20%` (2026-04-01 → 2026-04-08)",
        )
        sep_index = msg.rfind("─" * 28)
        snippet_index = msg.find("Price action")
        assert sep_index > 0
        assert snippet_index > 0
        assert snippet_index < sep_index
