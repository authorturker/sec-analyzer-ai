"""
J4 — Portfolio Value History tests.

_prune_history, _compute_delta, maybe_snapshot_portfolio_value,
load/save, cmd_pnl delta line, YF_OK=False guard, i18n parity.
"""
import json
import pytest
from pathlib import Path
from datetime import date, timedelta, datetime


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _iso(days_ago: int = 0) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


# ─── _prune_history ───────────────────────────────────────────────────────────

class TestPruneHistory:
    def test_under_cap_unchanged(self, bot):
        h = {_iso(i): float(i) for i in range(5)}
        assert bot._prune_history(h, cap=10) == h

    def test_exactly_at_cap_unchanged(self, bot):
        h = {_iso(i): float(i) for i in range(10)}
        result = bot._prune_history(h, cap=10)
        assert len(result) == 10

    def test_over_cap_trims_oldest(self, bot):
        h = {_iso(i): float(i) for i in range(12)}  # 12 records
        result = bot._prune_history(h, cap=10)
        assert len(result) == 10
        # oldest 2 removed (highest days_ago = most negative YYYY-MM-DD)
        oldest2 = sorted(h.keys())[:2]
        for k in oldest2:
            assert k not in result

    def test_cap_730(self, bot):
        h = {(date(2020, 1, 1) + timedelta(days=i)).isoformat(): float(i)
             for i in range(735)}
        result = bot._prune_history(h, cap=730)
        assert len(result) == 730


# ─── _compute_delta ───────────────────────────────────────────────────────────

class TestComputeDelta:
    def test_exact_match(self, bot):
        target = _iso(7)
        h = {target: 1000.0}
        result = bot._compute_delta(h, 1100.0, 7)
        assert result is not None
        abs_d, pct_d = result
        assert abs(abs_d - 100.0) < 0.001
        assert abs(pct_d - 10.0) < 0.001

    def test_tolerance_within_5_days(self, bot):
        # record 3 days older than target (target = 7d ago, record = 10d ago)
        record_date = _iso(10)
        h = {record_date: 1000.0}
        result = bot._compute_delta(h, 1200.0, 7)
        assert result is not None
        abs_d, pct_d = result
        assert abs(abs_d - 200.0) < 0.001

    def test_tolerance_exactly_5_days(self, bot):
        # record 5 days older than target = boundary, should still qualify
        record_date = _iso(12)  # target=7, record=12, diff=5
        h = {record_date: 500.0}
        result = bot._compute_delta(h, 600.0, 7)
        assert result is not None

    def test_tolerance_exceeded_returns_none(self, bot):
        # record 6 days older than target → outside window
        record_date = _iso(13)  # target=7, record=13, diff=6
        h = {record_date: 500.0}
        result = bot._compute_delta(h, 600.0, 7)
        assert result is None

    def test_no_candidates_returns_none(self, bot):
        # All records are more RECENT than target (future dates)
        h = {_iso(0): 1000.0, _iso(1): 900.0}  # only today and yesterday
        result = bot._compute_delta(h, 1100.0, 7)
        assert result is None

    def test_single_record_no_self_compare(self, bot):
        """Spec: single record case → n/a because we exclude today's key."""
        today = _iso(0)
        h = {today: 1000.0}
        # caller excludes today before passing h
        h_without_today = {k: v for k, v in h.items() if k != today}
        result = bot._compute_delta(h_without_today, 1100.0, 1)
        assert result is None

    def test_zero_base_returns_none(self, bot):
        target = _iso(7)
        h = {target: 0.0}
        result = bot._compute_delta(h, 1000.0, 7)
        assert result is None

    def test_negative_delta(self, bot):
        target = _iso(7)
        h = {target: 1200.0}
        result = bot._compute_delta(h, 1000.0, 7)
        assert result is not None
        abs_d, pct_d = result
        assert abs_d < 0
        assert pct_d < 0

    def test_picks_nearest_older_record(self, bot):
        # two candidates, pick the closer one (5d ago), not 10d ago
        h = {_iso(10): 900.0, _iso(5): 1000.0}
        result = bot._compute_delta(h, 1100.0, 3)
        assert result is not None
        abs_d, _ = result
        # nearest candidate to target (3d ago) that is <= target: 5d ago
        assert abs(abs_d - 100.0) < 0.001


# ─── maybe_snapshot_portfolio_value ──────────────────────────────────────────

class TestMaybeSnapshot:
    def _fake_yf_ok(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", True)

    def test_skips_when_yf_not_ok(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", False)
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", tmp_path / "ph.json")
        agg = {"AAPL": (10, 100.0)}
        prices = {"AAPL": 150.0}
        bot.maybe_snapshot_portfolio_value(agg, prices)
        assert not (tmp_path / "ph.json").exists()

    def test_skips_empty_agg(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", tmp_path / "ph.json")
        bot.maybe_snapshot_portfolio_value({}, {})
        assert not (tmp_path / "ph.json").exists()

    def test_skips_when_price_missing(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", tmp_path / "ph.json")
        agg = {"AAPL": (10, 100.0), "TSLA": (5, 200.0)}
        prices = {"AAPL": 150.0, "TSLA": None}
        bot.maybe_snapshot_portfolio_value(agg, prices)
        assert not (tmp_path / "ph.json").exists()

    def test_writes_when_all_prices_present(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        ph = tmp_path / "ph.json"
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        agg = {"AAPL": (10, 100.0)}
        prices = {"AAPL": 150.0}
        bot.maybe_snapshot_portfolio_value(agg, prices)
        assert ph.exists()
        data = json.loads(ph.read_text())
        assert len(data) == 1
        val = list(data.values())[0]
        assert abs(val - 1500.0) < 0.001

    def test_same_day_overwrites(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        ph = tmp_path / "ph.json"
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        agg = {"AAPL": (10, 100.0)}
        prices1 = {"AAPL": 150.0}
        prices2 = {"AAPL": 160.0}
        bot.maybe_snapshot_portfolio_value(agg, prices1)
        bot.maybe_snapshot_portfolio_value(agg, prices2)
        data = json.loads(ph.read_text())
        assert len(data) == 1
        assert abs(list(data.values())[0] - 1600.0) < 0.001

    def test_prune_applied(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        ph = tmp_path / "ph.json"
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        monkeypatch.setattr(bot, "_PORTFOLIO_HISTORY_CAP", 5)
        # pre-fill with 5 records
        existing = {(date.today() - timedelta(days=i+1)).isoformat(): float(i * 100)
                    for i in range(5)}
        ph.write_text(json.dumps(existing))
        agg = {"AAPL": (1, 100.0)}
        prices = {"AAPL": 200.0}
        bot.maybe_snapshot_portfolio_value(agg, prices)
        data = json.loads(ph.read_text())
        assert len(data) <= 5


# ─── load / save portfolio_history ───────────────────────────────────────────

class TestLoadSaveHistory:
    def test_missing_file_returns_empty(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", tmp_path / "nope.json")
        assert bot.load_portfolio_history() == {}

    def test_corrupt_file_returns_empty(self, bot, monkeypatch, tmp_path):
        ph = tmp_path / "ph.json"
        ph.write_text("NOT JSON{{{")
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        result = bot.load_portfolio_history()
        assert result == {}

    def test_roundtrip(self, bot, monkeypatch, tmp_path):
        ph = tmp_path / "ph.json"
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        data = {"2024-01-01": 1000.0, "2024-01-02": 1050.0}
        bot.save_portfolio_history(data)
        loaded = bot.load_portfolio_history()
        assert loaded == data


# ─── cmd_pnl delta integration ───────────────────────────────────────────────

class TestCmdPnlDelta:
    def _setup_portfolio(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        ph = tmp_path / "ph.json"
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        bot.mutate_cfg(lambda c: c.update({
            "portfolio": [{"ticker": "AAPL", "qty": 10.0, "cost": 100.0, "date": "2024-01-01"}]
        }))
        monkeypatch.setattr(bot, "fetch_last_close", lambda tk: 150.0)
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        return ph

    def test_no_delta_line_on_first_day(self, bot, monkeypatch, tmp_path):
        ph = self._setup_portfolio(bot, monkeypatch, tmp_path)
        sent = []
        monkeypatch.setattr(bot, "tg", lambda text, rich_md=None: sent.append(text))
        bot.cmd_pnl()
        assert sent, "cmd_pnl should call tg with output"
        result = sent[0]
        assert "AAPL" in result or bot.t("pnl_empty") in result

    def test_delta_line_appears_with_history(self, bot, monkeypatch, tmp_path):
        ph = self._setup_portfolio(bot, monkeypatch, tmp_path)
        week_ago = _iso(7)
        ph.write_text(json.dumps({week_ago: 1200.0}))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda text, rich_md=None: sent.append(text))
        bot.cmd_pnl()
        assert sent, "cmd_pnl should call tg with output"
        result = sent[0]
        # All prices present → delta line should appear with "7g" (Turkish) or "7d"
        assert "7" in result

    def test_no_delta_when_yf_not_ok(self, bot, monkeypatch):
        monkeypatch.setattr(bot, "YF_OK", False)
        bot.mutate_cfg(lambda c: c.update({
            "portfolio": [{"ticker": "AAPL", "qty": 10.0, "cost": 100.0, "date": "2024-01-01"}]
        }))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda text, rich_md=None: sent.append(text))
        bot.cmd_pnl()
        assert sent
        assert bot.t("yfinance_missing", cmd="/pnl") in sent[0]

    def test_partial_prices_no_delta_line(self, bot, monkeypatch, tmp_path):
        monkeypatch.setattr(bot, "YF_OK", True)
        ph = tmp_path / "ph.json"
        monkeypatch.setattr(bot, "PORTFOLIO_HISTORY", ph)
        bot.mutate_cfg(lambda c: c.update({
            "portfolio": [
                {"ticker": "AAPL", "qty": 10.0, "cost": 100.0, "date": "2024-01-01"},
                {"ticker": "TSLA", "qty": 5.0, "cost": 200.0, "date": "2024-01-01"},
            ]
        }))
        monkeypatch.setattr(bot, "fetch_last_close",
                            lambda tk: 150.0 if tk == "AAPL" else None)
        monkeypatch.setattr(bot.time, "sleep", lambda *a: None)
        week_ago = _iso(7)
        ph.write_text(json.dumps({week_ago: 1000.0}))
        sent = []
        monkeypatch.setattr(bot, "tg", lambda text, rich_md=None: sent.append(text))
        bot.cmd_pnl()
        assert sent, "cmd_pnl should call tg with output"
        result = sent[0]
        assert "Σ" not in result


# ─── _format_delta ────────────────────────────────────────────────────────────

class TestFormatDelta:
    def test_none_returns_na(self, bot):
        assert bot._format_delta(None, None) == "n/a"

    def test_positive_shows_up_emoji(self, bot):
        result = bot._format_delta(100.0, 10.0)
        assert "📈" in result
        assert "+$100.00" in result
        assert "+10.00%" in result

    def test_negative_shows_down_emoji(self, bot):
        result = bot._format_delta(-50.0, -5.0)
        assert "📉" in result


# ─── i18n parity ─────────────────────────────────────────────────────────────

J4_KEYS = ["pnl_delta_line"]

@pytest.mark.parametrize("lang", ["en", "tr"])
@pytest.mark.parametrize("key", J4_KEYS)
class TestJ4I18nKeys:
    def test_key_exists_and_nonempty(self, bot, lang, key):
        bot.mutate_cfg(lambda c: c.update({"language": lang}))
        bot._current_lang = None
        bot._lang_cache.clear()
        val = bot.t(key, total=1000.0, d1="n/a", d7="n/a", d30="n/a")
        assert val, f"[{lang}] '{key}' must be non-empty"
        assert val != key, f"[{lang}] '{key}' must not fall back to key name"
