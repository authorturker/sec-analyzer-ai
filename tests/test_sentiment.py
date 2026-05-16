"""
Tests for E3 — sentiment trend persistence and pure trend rendering.
"""
from datetime import datetime, timedelta

import pytest


# ─── parse_sentiment_signal ───────────────────────────────
class TestParseSentimentSignal:
    @pytest.mark.parametrize("raw,label,emoji", [
        ("📈 Bullish — multiple insider buys",        "bullish",  "📈"),
        ("Bullish — insider accumulation",            "bullish",  "📈"),
        ("📉 Bearish — heavy CFO sales",              "bearish",  "📉"),
        ("Bearish — director exits",                  "bearish",  "📉"),
        ("➡️ Neutral — mixed activity",               "neutral",  "➡️"),
        ("Neutral activity",                           "neutral",  "➡️"),
        ("Yükseliş — alımlar baskın",                "bullish",  "📈"),
        ("Düşüş — satış ağırlıklı",                  "bearish",  "📉"),
        ("Nötr — net etkisiz",                       "neutral",  "➡️"),
        ("garbled noise",                              "unknown",  "❔"),
    ])
    def test_label_and_emoji(self, bot, raw, label, emoji):
        out_label, out_emoji = bot.parse_sentiment_signal(raw)
        assert out_label == label
        assert out_emoji == emoji


# ─── append_sentiment / load_sentiment_history ────────────
@pytest.fixture
def tmp_sent(tmp_path, bot, monkeypatch):
    monkeypatch.setattr(bot, "SENT_HIST", tmp_path / "sentiment_history.json")
    yield bot


class TestSentimentPersistence:
    def test_empty_history_is_dict(self, tmp_sent):
        assert tmp_sent.load_sentiment_history() == {}

    def test_append_and_read(self, tmp_sent):
        tmp_sent.append_sentiment("AAPL", "📈 Bullish — buys", on_date="2026-05-01")
        h = tmp_sent.load_sentiment_history()
        assert "AAPL" in h
        assert len(h["AAPL"]) == 1
        assert h["AAPL"][0]["label"] == "bullish"
        assert h["AAPL"][0]["emoji"] == "📈"
        assert h["AAPL"][0]["date"]  == "2026-05-01"

    def test_multiple_appends_same_ticker(self, tmp_sent):
        tmp_sent.append_sentiment("AAPL", "📈 Bullish — A", on_date="2026-04-01")
        tmp_sent.append_sentiment("AAPL", "📉 Bearish — B", on_date="2026-05-01")
        h = tmp_sent.load_sentiment_history()
        assert len(h["AAPL"]) == 2
        labels = [e["label"] for e in h["AAPL"]]
        assert labels == ["bullish", "bearish"]


# ─── build_trend_lines (pure) ─────────────────────────────
class TestBuildTrendLines:
    def test_no_history_returns_empty(self, bot):
        assert bot.build_trend_lines({}, days=30) == []

    def test_only_one_entry_shows_no_history_marker(self, bot):
        hist = {"AAPL": [{"date": "2026-05-15",
                          "label": "bullish", "emoji": "📈",
                          "raw": "📈 Bullish — recent buys"}]}
        ref = datetime(2026, 5, 16)
        lines = bot.build_trend_lines(hist, days=30, ref_date=ref)
        assert len(lines) == 1
        assert "AAPL" in lines[0]
        # Localized 'no historical comparison yet' phrase should be in the line
        assert "no historical comparison" in lines[0]

    def test_change_detected_bullish_to_bearish(self, bot):
        hist = {"AAPL": [
            {"date": "2026-03-15", "label": "bullish", "emoji": "📈", "raw": "x"},
            {"date": "2026-05-15", "label": "bearish", "emoji": "📉", "raw": "y"},
        ]}
        ref = datetime(2026, 5, 16)
        lines = bot.build_trend_lines(hist, days=30, ref_date=ref)
        assert len(lines) == 1
        # Latest is bearish, prev is bullish → shifted bearish
        assert "bearish" in lines[0].lower()
        assert "2026-03-15" in lines[0]
        assert "shifted bearish" in lines[0].lower()

    def test_no_change_when_labels_match(self, bot):
        hist = {"AAPL": [
            {"date": "2026-03-15", "label": "bullish", "emoji": "📈", "raw": "x"},
            {"date": "2026-05-15", "label": "bullish", "emoji": "📈", "raw": "y"},
        ]}
        ref = datetime(2026, 5, 16)
        lines = bot.build_trend_lines(hist, days=30, ref_date=ref)
        assert "no change" in lines[0].lower()

    def test_recent_only_falls_back_to_no_history_marker(self, bot):
        """If both entries are within `days`, there's no eligible 'prev'."""
        hist = {"AAPL": [
            {"date": "2026-05-10", "label": "bullish", "emoji": "📈", "raw": "x"},
            {"date": "2026-05-15", "label": "bearish", "emoji": "📉", "raw": "y"},
        ]}
        ref = datetime(2026, 5, 16)
        lines = bot.build_trend_lines(hist, days=30, ref_date=ref)
        assert "no historical comparison" in lines[0]

    def test_multiple_tickers_sorted(self, bot):
        hist = {
            "MSFT": [{"date": "2026-05-15", "label": "bearish", "emoji": "📉", "raw": "x"}],
            "AAPL": [{"date": "2026-05-15", "label": "bullish", "emoji": "📈", "raw": "y"}],
        }
        lines = bot.build_trend_lines(hist, days=30, ref_date=datetime(2026, 5, 16))
        # Sorted alphabetically
        assert "AAPL" in lines[0]
        assert "MSFT" in lines[1]
