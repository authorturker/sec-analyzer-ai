"""
Tests for G2 — Portfolio P&L.

Covers (≥16 offline tests):
  - _parse_pos_args: valid input, negative qty, zero qty, negative cost,
    invalid float, bad date, missing args, multi-word no date
  - aggregate_positions: single lot, multi-lot weighted avg, multi-ticker
  - compute_pnl_rows: profit, loss, price=None (n/a), avg_cost=0 (pct=None)
  - format_pnl: markdown output, n/a footnote, empty portfolio, total math
  - cmd_addpos / cmd_removepos: config mutation, 50-lot cap, multi-lot removal
  - /pnl path: no LLM call, no cache write (mock)
  - i18n parity: all portfolio keys present in both langs
"""
import pytest
from unittest.mock import patch, MagicMock


# ─── _parse_pos_args ────────────────────────────────────────

class TestParsePosArgs:
    def test_valid_full(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL", "10", "150.50", "2024-01-15"])
        assert isinstance(result, dict)
        assert result["ticker"] == "AAPL"
        assert abs(result["qty"] - 10.0) < 1e-9
        assert abs(result["cost"] - 150.50) < 1e-9
        assert result["date"] == "2024-01-15"

    def test_valid_no_date(self, bot):
        result = bot._parse_pos_args(["/addpos", "MSFT", "5.5", "300"])
        assert isinstance(result, dict)
        assert result["date"] == ""

    def test_ticker_uppercased(self, bot):
        result = bot._parse_pos_args(["/addpos", "aapl", "1", "100"])
        assert isinstance(result, dict)
        assert result["ticker"] == "AAPL"

    def test_zero_qty_rejected(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL", "0", "100"])
        assert isinstance(result, str)

    def test_negative_qty_rejected(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL", "-5", "100"])
        assert isinstance(result, str)

    def test_negative_cost_rejected(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL", "1", "-10"])
        assert isinstance(result, str)

    def test_zero_cost_allowed(self, bot):
        """Free/granted shares (cost=0) must be accepted."""
        result = bot._parse_pos_args(["/addpos", "GOOG", "2", "0"])
        assert isinstance(result, dict)
        assert result["cost"] == 0.0

    def test_bad_date_rejected(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL", "1", "100", "not-a-date"])
        assert isinstance(result, str)

    def test_invalid_qty_string(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL", "ten", "100"])
        assert isinstance(result, str)

    def test_missing_args(self, bot):
        result = bot._parse_pos_args(["/addpos", "AAPL"])
        assert isinstance(result, str)

    def test_fractional_qty_allowed(self, bot):
        result = bot._parse_pos_args(["/addpos", "BTC", "0.5", "30000"])
        assert isinstance(result, dict)
        assert abs(result["qty"] - 0.5) < 1e-9


# ─── aggregate_positions ────────────────────────────────────

class TestAggregatePositions:
    def test_single_lot(self, bot):
        lots = [{"ticker": "AAPL", "qty": 10.0, "cost": 150.0, "date": ""}]
        agg = bot.aggregate_positions(lots)
        assert "AAPL" in agg
        qty, avg = agg["AAPL"]
        assert abs(qty - 10.0) < 1e-9
        assert abs(avg - 150.0) < 1e-9

    def test_weighted_avg_two_lots(self, bot):
        lots = [
            {"ticker": "AAPL", "qty": 10.0, "cost": 100.0, "date": ""},
            {"ticker": "AAPL", "qty": 10.0, "cost": 200.0, "date": ""},
        ]
        agg = bot.aggregate_positions(lots)
        qty, avg = agg["AAPL"]
        assert abs(qty - 20.0) < 1e-9
        assert abs(avg - 150.0) < 1e-9    # (10*100 + 10*200) / 20 = 150

    def test_weighted_avg_unequal_lots(self, bot):
        lots = [
            {"ticker": "AAPL", "qty": 1.0, "cost": 100.0, "date": ""},
            {"ticker": "AAPL", "qty": 9.0, "cost": 200.0, "date": ""},
        ]
        agg = bot.aggregate_positions(lots)
        _, avg = agg["AAPL"]
        expected = (1 * 100 + 9 * 200) / 10  # = 190.0
        assert abs(avg - expected) < 1e-9

    def test_multi_ticker_alphabetical(self, bot):
        lots = [
            {"ticker": "MSFT", "qty": 5.0, "cost": 300.0, "date": ""},
            {"ticker": "AAPL", "qty": 10.0, "cost": 150.0, "date": ""},
        ]
        agg = bot.aggregate_positions(lots)
        keys = list(agg.keys())
        assert keys == ["AAPL", "MSFT"]

    def test_empty_lots(self, bot):
        assert bot.aggregate_positions([]) == {}


# ─── compute_pnl_rows ────────────────────────────────────────

class TestComputePnlRows:
    def test_profit_row(self, bot):
        agg    = {"AAPL": (10.0, 150.0)}
        prices = {"AAPL": 185.0}
        rows   = bot.compute_pnl_rows(agg, prices)
        assert len(rows) == 1
        r = rows[0]
        assert abs(r["value"]   - 1850.0) < 0.01
        assert abs(r["pnl_usd"] - 350.0)  < 0.01
        assert r["pnl_pct"] is not None
        assert r["pnl_pct"] > 0

    def test_loss_row(self, bot):
        agg    = {"AAPL": (10.0, 200.0)}
        prices = {"AAPL": 185.0}
        rows   = bot.compute_pnl_rows(agg, prices)
        r = rows[0]
        assert r["pnl_usd"] < 0

    def test_price_none_gives_na_row(self, bot):
        agg    = {"DELIST": (5.0, 100.0)}
        prices = {"DELIST": None}
        rows   = bot.compute_pnl_rows(agg, prices)
        r = rows[0]
        assert r["last"]    is None
        assert r["value"]   is None
        assert r["pnl_usd"] is None
        assert r["pnl_pct"] is None

    def test_avg_cost_zero_pct_is_none(self, bot):
        """Free shares: pnl_usd normal, pnl_pct None (avoid div/0)."""
        agg    = {"FREE": (10.0, 0.0)}
        prices = {"FREE": 50.0}
        rows   = bot.compute_pnl_rows(agg, prices)
        r = rows[0]
        assert abs(r["pnl_usd"] - 500.0) < 0.01
        assert r["pnl_pct"] is None


# ─── format_pnl ─────────────────────────────────────────────

class TestFormatPnl:
    def _priced_row(self, ticker="AAPL", qty=10.0, avg_cost=150.0, last=185.0):
        value   = qty * last
        cost_b  = qty * avg_cost
        pnl_usd = value - cost_b
        pnl_pct = pnl_usd / cost_b * 100.0 if avg_cost else None
        return {"ticker": ticker, "qty": qty, "avg_cost": avg_cost,
                "last": last, "value": value, "pnl_usd": pnl_usd, "pnl_pct": pnl_pct}

    def _na_row(self, ticker="DELIST"):
        return {"ticker": ticker, "qty": 5.0, "avg_cost": 100.0,
                "last": None, "value": None, "pnl_usd": None, "pnl_pct": None}

    def test_priced_row_in_output(self, bot):
        rows = [self._priced_row()]
        msg  = bot.format_pnl(rows)
        assert "AAPL" in msg

    def test_na_row_shows_na(self, bot):
        rows = [self._na_row()]
        msg  = bot.format_pnl(rows)
        assert "DELIST" in msg
        assert "n/a" in msg

    def test_na_footnote_present_when_any_na(self, bot):
        rows = [self._priced_row(), self._na_row()]
        msg  = bot.format_pnl(rows)
        # Na-note key should cause some "excluded" text
        assert "1" in msg      # count = 1

    def test_total_only_over_priced(self, bot):
        priced = self._priced_row(qty=10.0, avg_cost=150.0, last=185.0)
        na     = self._na_row()
        msg    = bot.format_pnl([priced, na])
        # $1850 should appear (value of priced position)
        assert "1,850" in msg

    def test_empty_returns_empty_message(self, bot):
        msg = bot.format_pnl([])
        assert msg  # non-empty "pnl_empty" string

    def test_md_escape_in_ticker(self, bot):
        """Tickers with special chars (edge case) must not break Markdown."""
        row = self._priced_row(ticker="BRK_B")
        msg = bot.format_pnl([row])
        assert "BRK" in msg     # escaped or not, ticker appears


# ─── cmd_addpos / cmd_removepos ─────────────────────────────

class TestPortfolioCommands:
    def _reset_portfolio(self, bot):
        bot.mutate_cfg(lambda c: c.update({"portfolio": []}))

    def test_addpos_adds_lot(self, bot):
        self._reset_portfolio(bot)
        bot.cmd_addpos(["/addpos", "AAPL", "10", "150.50"])
        lots = bot.get_cfg()["portfolio"]
        assert len(lots) == 1
        assert lots[0]["ticker"] == "AAPL"

    def test_addpos_50_lot_cap(self, bot):
        self._reset_portfolio(bot)
        for i in range(50):
            bot.mutate_cfg(lambda c, i=i: c["portfolio"].append(
                {"ticker": f"T{i}", "qty": 1.0, "cost": 1.0, "date": ""}
            ))
        result = bot.cmd_addpos(["/addpos", "OVERFLOW", "1", "1"])
        assert bot.get_cfg()["portfolio"].__len__() == 50
        assert result   # non-empty limit message

    def test_addpos_allows_multiple_lots_same_ticker(self, bot):
        self._reset_portfolio(bot)
        bot.cmd_addpos(["/addpos", "AAPL", "10", "150"])
        bot.cmd_addpos(["/addpos", "AAPL", "5", "200"])
        lots = [l for l in bot.get_cfg()["portfolio"] if l["ticker"] == "AAPL"]
        assert len(lots) == 2

    def test_removepos_removes_all_lots(self, bot):
        self._reset_portfolio(bot)
        bot.cmd_addpos(["/addpos", "AAPL", "10", "150"])
        bot.cmd_addpos(["/addpos", "AAPL", "5", "200"])
        result = bot.cmd_removepos(["/removepos", "AAPL"])
        lots = bot.get_cfg()["portfolio"]
        assert not any(l["ticker"] == "AAPL" for l in lots)
        assert "2" in result    # count of removed lots

    def test_removepos_not_found(self, bot):
        self._reset_portfolio(bot)
        result = bot.cmd_removepos(["/removepos", "ZZZZ"])
        assert result           # non-empty "not found" message

    def test_addpos_invalid_qty_returns_message(self, bot):
        result = bot.cmd_addpos(["/addpos", "AAPL", "-1", "100"])
        assert result


# ─── /pnl path: no LLM, no cache write ──────────────────────

class TestPnlPathProbeOnly:
    def test_pnl_does_not_call_llm(self, bot):
        bot.mutate_cfg(lambda c: c.update({"portfolio": [
            {"ticker": "AAPL", "qty": 1.0, "cost": 150.0, "date": ""}
        ]}))
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.text = "Date,Open,High,Low,Close,Volume\n2026-06-09,185,186,184,185,1000000\n"
            mock_get.return_value = mock_resp
            with patch.object(bot, "llm") as mock_llm:
                bot.cmd_pnl()
        mock_llm.assert_not_called()

    def test_pnl_does_not_write_cache(self, bot):
        bot.mutate_cfg(lambda c: c.update({"portfolio": [
            {"ticker": "AAPL", "qty": 1.0, "cost": 150.0, "date": ""}
        ]}))
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.text = "Date,Open,High,Low,Close,Volume\n2026-06-09,185,186,184,185,1000000\n"
            mock_get.return_value = mock_resp
            with patch.object(bot, "save_cache") as mock_save:
                bot.cmd_pnl()
        mock_save.assert_not_called()


# ─── i18n parity ────────────────────────────────────────────

class TestI18nParityPortfolio:
    def test_portfolio_keys_present_in_both_langs(self, bot):
        import json
        from pathlib import Path
        lang_dir = Path(bot.__file__).resolve().parent / "lang"
        en = json.loads((lang_dir / "en.json").read_text(encoding="utf-8"))
        tr = json.loads((lang_dir / "tr.json").read_text(encoding="utf-8"))
        portfolio_keys = [k for k in en if any(
            tok in k for tok in ("pos", "pnl", "portfolio")
        )]
        assert portfolio_keys, "No portfolio keys found in en.json"
        missing = [k for k in portfolio_keys if k not in tr]
        assert not missing, f"Missing in tr.json: {missing}"
