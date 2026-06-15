"""tests/test_dailynews.py — N2 daily news digest tests.

Coverage:
- _daily_news_fresh: date filtering, count limit, empty/missing date
- _format_daily_news: ticker sections, header, empty result
- send_daily_news: YF_OK guard, empty watchlist, no-fresh-news silence
- cmd_dailynews: on/off/now toggle behavior
"""

import json
import pytest
from datetime import date


# ─── _daily_news_fresh ────────────────────────────────────

class TestDailyNewsFresh:
    def _raw(self, title="Headline", url="http://x", provider="Reuters",
             date_str="2026-06-15"):
        return {"content": {
            "title": title, "canonicalUrl": {"url": url},
            "provider": {"displayName": provider}, "pubDate": date_str + "T12:00:00Z",
        }}

    def test_today_items_kept(self, bot):
        items = [self._raw("A"), self._raw("B")]
        result = bot._daily_news_fresh(items, "2026-06-15", 5)
        assert len(result) == 2
        assert result[0]["title"] == "A"

    def test_old_items_filtered(self, bot):
        items = [self._raw("old", date_str="2026-06-14"),
                 self._raw("new", date_str="2026-06-15")]
        result = bot._daily_news_fresh(items, "2026-06-15", 5)
        assert len(result) == 1
        assert result[0]["title"] == "new"

    def test_count_limit(self, bot):
        items = [self._raw(f"Item{i}") for i in range(10)]
        result = bot._daily_news_fresh(items, "2026-06-15", 3)
        assert len(result) == 3

    def test_empty_items(self, bot):
        assert bot._daily_news_fresh([], "2026-06-15", 5) == []

    def test_no_matching_date(self, bot):
        items = [self._raw("A", date_str="2026-06-14")]
        assert bot._daily_news_fresh(items, "2026-06-15", 5) == []

    def test_missing_date_field_filtered(self, bot):
        item = {"content": {"title": "X", "url": "", "pubDate": ""}}
        assert bot._daily_news_fresh([item], "2026-06-15", 5) == []


# ─── _format_daily_news ───────────────────────────────────

class TestFormatDailyNews:
    def _raw(self, title="Headline", url="http://x", provider="Reuters",
             date_str="2026-06-15"):
        return {"content": {
            "title": title, "canonicalUrl": {"url": url},
            "provider": {"displayName": provider}, "pubDate": date_str + "T12:00:00Z",
        }}

    def test_multiple_tickers_with_news(self, bot):
        news = {
            "AAPL": [self._raw("Apple news")],
            "MSFT": [self._raw("Microsoft news")],
        }
        body = bot._format_daily_news(news, "2026-06-15")
        assert "Daily News" in body or "Günlük Haber Akışı" in body
        assert "AAPL" in body
        assert "MSFT" in body

    def test_no_fresh_news_returns_empty(self, bot):
        news = {"AAPL": [self._raw("Old", date_str="2026-06-14")]}
        assert bot._format_daily_news(news, "2026-06-15") == ""

    def test_empty_dict_returns_empty(self, bot):
        assert bot._format_daily_news({}, "2026-06-15") == ""

    def test_per_ticker_limit(self, bot):
        news = {"AAPL": [self._raw(f"Item{i}") for i in range(5)]}
        body = bot._format_daily_news(news, "2026-06-15", per_ticker=2)
        # Should only contain 2 items
        assert body.count("• [") == 2

    def test_none_items_treated_as_empty(self, bot):
        news = {"AAPL": None}
        assert bot._format_daily_news(news, "2026-06-15") == ""


# ─── send_daily_news ──────────────────────────────────────

class TestSendDailyNews:
    def test_yf_not_ok_returns_false(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", False)
        assert bot.send_daily_news() is False

    def test_empty_watchlist_returns_false(self, bot, tmp_path, monkeypatch):
        cfg_path = tmp_path / "bot_config.json"
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir()
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "CHAT_DIR", chat_dir)
        bot._cfg_cache = None
        cfg_path.write_text(json.dumps({"chat_ids": ["100"], "tickers": []}), encoding="utf-8")
        bot._ctx.chat_id = "100"
        try:
            assert bot.send_daily_news() is False
        finally:
            bot._ctx.chat_id = None

    def test_no_fresh_news_returns_false(self, bot, tmp_path, monkeypatch):
        cfg_path = tmp_path / "bot_config.json"
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir()
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "CHAT_DIR", chat_dir)
        bot._cfg_cache = None
        cfg_path.write_text(json.dumps({"chat_ids": ["100"], "tickers": ["AAPL"]}), encoding="utf-8")
        monkeypatch.setattr(bot, "fetch_yfinance_news", lambda tk: [])
        bot._ctx.chat_id = "100"
        try:
            assert bot.send_daily_news() is False
        finally:
            bot._ctx.chat_id = None


# ─── cmd_dailynews ────────────────────────────────────────

class TestCmdDailynews:
    def _setup(self, bot, tmp_path, monkeypatch):
        cfg_path = tmp_path / "bot_config.json"
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir()
        monkeypatch.setattr(bot, "CONFIG_FILE", cfg_path)
        monkeypatch.setattr(bot, "CHAT_DIR", chat_dir)
        bot._cfg_cache = None
        cfg_path.write_text(json.dumps({"chat_ids": ["100"]}), encoding="utf-8")
        bot.init_chat_config("100")

    def test_on_enables(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        bot._ctx.chat_id = "100"
        try:
            result = bot.cmd_dailynews(["/dailynews", "on"])
            assert "enabled" in result.lower() or "açıldı" in result.lower()
            cfg = bot.get_chat_cfg()
            assert cfg.get("daily_news") is True
        finally:
            bot._ctx.chat_id = None

    def test_off_disables(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        bot._ctx.chat_id = "100"
        try:
            bot.cmd_dailynews(["/dailynews", "on"])
            result = bot.cmd_dailynews(["/dailynews", "off"])
            assert "disabled" in result.lower() or "kapatıldı" in result.lower()
            cfg = bot.get_chat_cfg()
            assert cfg.get("daily_news") is False
        finally:
            bot._ctx.chat_id = None

    def test_now_no_news_returns_none_msg(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        monkeypatch.setattr(bot, "YF_OK", False)
        bot._ctx.chat_id = "100"
        try:
            result = bot.cmd_dailynews(["/dailynews", "now"])
            assert "no fresh news" in result.lower() or "taze haber yok" in result.lower()
        finally:
            bot._ctx.chat_id = None

    def test_default_enables(self, bot, tmp_path, monkeypatch):
        self._setup(bot, tmp_path, monkeypatch)
        bot._ctx.chat_id = "100"
        try:
            result = bot.cmd_dailynews(["/dailynews"])
            assert "enabled" in result.lower() or "açıldı" in result.lower()
        finally:
            bot._ctx.chat_id = None
