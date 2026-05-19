"""
Tests for E5 — /checkprice and /checknews pure formatters.
Network IO (fetch_yfinance_history / fetch_yfinance_news) and the
optional-yfinance behavior are smoke-tested separately.
"""
import pytest


# ─── _format_price_check ──────────────────────────────────
class TestFormatPriceCheck:
    def test_no_rows_returns_no_data(self, bot):
        out = bot._format_price_check("AAPL", 7, [])
        assert "AAPL" in out
        # English fallback contains "No price data" or Turkish equivalent
        assert "No price data" in out or "fiyat verisi yok" in out

    def test_positive_change(self, bot):
        rows = [
            # (date, open, high, low, close)
            ("2026-05-10", 100.0, 102.0, 99.0, 101.0),
            ("2026-05-11", 101.5, 103.0, 100.5, 102.5),
            ("2026-05-12", 102.5, 106.0, 102.0, 105.0),
        ]
        out = bot._format_price_check("AAPL", 7, rows)
        assert "AAPL" in out
        assert "📈" in out
        # Start open 100, end close 105 → +5.00%
        assert "+5.00" in out
        # High/low across the whole window
        assert "106.00" in out
        assert "99.00"  in out
        assert "2026-05-10" in out
        assert "2026-05-12" in out

    def test_negative_change(self, bot):
        rows = [
            ("2026-05-10", 100.0, 100.0, 90.0, 95.0),
            ("2026-05-11",  94.0,  96.0, 91.0, 92.0),
        ]
        out = bot._format_price_check("X", 7, rows)
        assert "📉" in out
        assert "-8.00" in out

    def test_zero_open_handled_gracefully(self, bot):
        # Edge case: start open is 0 → pct can't be computed; should default to 0
        rows = [("2026-05-10", 0.0, 0.0, 0.0, 0.0),
                ("2026-05-11", 1.0, 2.0, 1.0, 1.5)]
        out = bot._format_price_check("X", 7, rows)
        # Should not raise, should not be infinity/NaN in output
        assert "inf" not in out.lower()
        assert "nan" not in out.lower()


# ─── _news_extract ────────────────────────────────────────
class TestNewsExtract:
    def test_canonical_url_preferred(self, bot):
        item = {"content": {
            "title": "Headline",
            "canonicalUrl":    {"url": "https://publisher.com/article"},
            "clickThroughUrl": {"url": "https://finance.yahoo.com/redirect"},
            "provider":        {"displayName": "Publisher"},
            "pubDate":         "2026-05-17T20:20:00Z",
        }}
        n = bot._news_extract(item)
        assert n["title"]    == "Headline"
        assert n["url"]      == "https://publisher.com/article"
        assert n["provider"] == "Publisher"
        assert n["date"]     == "2026-05-17"

    def test_falls_back_to_clickthrough(self, bot):
        item = {"content": {
            "title": "X",
            "clickThroughUrl": {"url": "https://yahoo.com/y"},
            "provider":        {"displayName": "P"},
            "pubDate":         "2026-01-01T00:00:00Z",
        }}
        assert bot._news_extract(item)["url"] == "https://yahoo.com/y"

    def test_missing_fields_safe(self, bot):
        # Empty / malformed item shouldn't raise
        n = bot._news_extract({})
        assert n["title"]    == "(no title)"
        assert n["url"]      == ""
        assert n["provider"] == ""
        assert n["date"]     == ""

    def test_none_content_safe(self, bot):
        n = bot._news_extract({"content": None})
        assert n["title"] == "(no title)"


# ─── _format_news_list ────────────────────────────────────
class TestFormatNewsList:
    def test_no_items_returns_no_data(self, bot):
        out = bot._format_news_list("AAPL", [], 5)
        assert "AAPL" in out
        assert "No recent news" in out or "son haber yok" in out

    def test_respects_count_limit(self, bot):
        items = [{"content": {
            "title": f"Headline {i}",
            "canonicalUrl": {"url": f"https://example.com/{i}"},
            "provider": {"displayName": "Source"},
            "pubDate": "2026-05-17T00:00:00Z",
        }} for i in range(10)]
        out = bot._format_news_list("X", items, count=3)
        # Header + 3 items = 4 lines (each item is multi-line, but bullets count)
        assert "Headline 0" in out
        assert "Headline 1" in out
        assert "Headline 2" in out
        assert "Headline 3" not in out
        assert "Headline 9" not in out

    def test_renders_link_and_meta(self, bot):
        items = [{"content": {
            "title": "Apple beats",
            "canonicalUrl": {"url": "https://wsj.com/x"},
            "provider": {"displayName": "WSJ"},
            "pubDate": "2026-05-17T20:20:00Z",
        }}]
        out = bot._format_news_list("AAPL", items, 5)
        assert "Apple beats" in out
        assert "https://wsj.com/x" in out
        assert "WSJ" in out
        assert "2026-05-17" in out


# ─── Smoke test: yfinance optional path ───────────────────
class TestYfinanceOptional:
    def test_yfinance_missing_message_present(self, bot):
        # If user happens to not have yfinance, this key still resolves.
        # We don't toggle YF_OK at runtime — just verify the i18n key exists.
        msg = bot.t("yfinance_missing", cmd="/checkprice")
        assert "yfinance" in msg
        assert "/checkprice" in msg
