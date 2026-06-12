"""
Tests for K3 — /pnl Monospace Table + K2 i18n Touch-up.

Coverage:
- _pnl_table: column alignment, width <= 38, no emojis inside block
- _pnl_table: n/a rows aligned, ticker truncation
- format_pnl: code block delimiters, total+delta structure
- Edge cases: single position, all n/a, mixed, negative total, zero cost
- K2 touch-up: settings_provider_none / settings_provider_no_key i18n keys
"""
import re
import pytest


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


# ─── _pnl_table internals ─────────────────────────────────────

class TestPnlTable:
    def test_all_data_rows_same_width(self, bot):
        """Every data row (including header and divider) has identical length."""
        rows = [
            _make_row("AAPL", qty=10, avg_cost=150, last=185),
            _make_row("MSFT", qty=5, avg_cost=300, last=420),
            _make_row("NVDA", qty=2, avg_cost=800, last=950),
        ]
        table = bot._pnl_table(rows)
        lines = table.splitlines()
        widths = [len(l) for l in lines]
        assert len(set(widths)) == 1, f"Unequal row widths: {widths}"

    def test_width_at_most_38(self, bot):
        """Table row width fits mobile ≤38 char target."""
        rows = [_make_row("AAPL", qty=100, avg_cost=150, last=185.50)]
        table = bot._pnl_table(rows)
        for line in table.splitlines():
            assert len(line) <= 38, f"Row too wide ({len(line)}): {repr(line)}"

    def test_no_emojis_in_table(self, bot):
        """No emoji characters appear inside the monospace table."""
        rows = [
            _make_row("AAPL", qty=10, avg_cost=150, last=185),  # profit
            _make_row("META", qty=5, avg_cost=500, last=300),    # loss
        ]
        table = bot._pnl_table(rows)
        # Simple check: no chars with codepoint > 0x2FFF (emoji range)
        for ch in table:
            assert ord(ch) <= 0x2FFF or ch in " $-+.,%/&\n", \
                f"Emoji/non-ASCII found in table: {repr(ch)}"

    def test_na_row_aligned_with_priced_rows(self, bot):
        """n/a row width equals priced row width."""
        rows = [
            _make_row("AAPL", qty=10, avg_cost=150, last=185),
            _make_row("DELIST", qty=5, avg_cost=100, last=None),
        ]
        table = bot._pnl_table(rows)
        lines = table.splitlines()
        widths = [len(l) for l in lines]
        assert len(set(widths)) == 1, f"n/a row misaligned: {widths}"

    def test_long_ticker_truncated(self, bot):
        """Ticker longer than 6 chars is truncated to 5 + ellipsis."""
        rows = [_make_row("TOOLONG7", qty=1, avg_cost=100, last=110)]
        table = bot._pnl_table(rows)
        # Original ticker must not appear intact; truncated form should
        assert "TOOLONG7" not in table
        assert "TOOLO" in table  # first 5 chars

    def test_short_ticker_padded(self, bot):
        """Short ticker is left-padded to column width (all rows same width)."""
        rows = [_make_row("A", qty=1, avg_cost=50, last=60)]
        table = bot._pnl_table(rows)
        lines = table.splitlines()
        widths = [len(l) for l in lines]
        assert len(set(widths)) == 1

    def test_header_contains_expected_columns(self, bot):
        """First line of table contains TICKER, QTY, LAST, VALUE, P&L%."""
        rows = [_make_row()]
        table = bot._pnl_table(rows)
        header = table.splitlines()[0]
        for col in ("TICKER", "QTY", "LAST", "VALUE", "P&L%"):
            assert col in header, f"Column '{col}' missing from header"


# ─── format_pnl structure ─────────────────────────────────────

class TestFormatPnlStructure:
    def test_output_contains_code_block(self, bot):
        """format_pnl wraps table in ``` code block."""
        rows = [_make_row()]
        out = bot.format_pnl(rows)
        assert "```" in out

    def test_header_before_code_block(self, bot):
        """pnl_header appears before the code block."""
        rows = [_make_row()]
        out = bot.format_pnl(rows)
        header_pos = out.find(bot.t("pnl_header"))
        code_pos   = out.find("```")
        assert header_pos < code_pos

    def test_total_after_code_block(self, bot):
        """Total line appears after the closing ``` of the code block."""
        rows = [_make_row()]
        out = bot.format_pnl(rows)
        last_backtick = out.rfind("```")
        # Total label varies by active language — find "$" after the code block
        # (the total value always appears as $N,NNN after the backtick block)
        after_block = out[last_backtick + 3:]
        assert "$" in after_block, "Total value not found after code block"

    def test_empty_returns_pnl_empty(self, bot):
        """Empty rows → pnl_empty string."""
        out = bot.format_pnl([])
        assert bot.t("pnl_empty") in out

    def test_na_footnote_present(self, bot):
        """Mixed rows: na footnote appears."""
        rows = [_make_row(), _make_row("DELIST", last=None)]
        out = bot.format_pnl(rows)
        # pnl_na_note with count=1
        assert "1" in out
        assert "excluded" in out or "hariç" in out

    def test_all_na_no_total(self, bot):
        """All n/a positions: no total line."""
        rows = [_make_row("A", last=None), _make_row("B", last=None)]
        out = bot.format_pnl(rows)
        assert "Total:" not in out and "Toplam:" not in out

    def test_negative_pnl_shows_minus(self, bot):
        """Loss position: P&L% shows negative sign in table."""
        rows = [_make_row("META", qty=5, avg_cost=500, last=300)]
        table = bot._pnl_table(rows)
        assert "-" in table  # negative pnl%

    def test_zero_cost_pct_na(self, bot):
        """avg_cost=0 → pnl_pct=None → n/a in P&L% column."""
        rows = [_make_row("FREE", qty=1, avg_cost=0, last=100)]
        table = bot._pnl_table(rows)
        assert "n/a" in table


# ─── K2 i18n touch-up ─────────────────────────────────────────

class TestSettingsProviderI18n:
    def test_settings_provider_none_key_exists_both_langs(self, bot):
        for code in ("en", "tr"):
            strings = bot._load_lang(code)
            assert "settings_provider_none" in strings, \
                f"Missing 'settings_provider_none' in {code}.json"

    def test_settings_provider_no_key_key_exists_both_langs(self, bot):
        for code in ("en", "tr"):
            strings = bot._load_lang(code)
            assert "settings_provider_no_key" in strings, \
                f"Missing 'settings_provider_no_key' in {code}.json"

    def test_settings_provider_no_key_has_placeholder(self, bot):
        for code in ("en", "tr"):
            tmpl = bot._load_lang(code)["settings_provider_no_key"]
            assert "{provider}" in tmpl, \
                f"Missing {{provider}} placeholder in settings_provider_no_key ({code})"

    def test_settings_no_provider_uses_i18n_key(self, bot, monkeypatch, tmp_path):
        """With no default_provider, settings output comes from i18n key (not raw string)."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({"api_keys": {}, "default_provider": ""}))
        out = bot.cmd_settings()
        expected = bot.t("settings_provider_none")
        assert expected in out

    def test_settings_missing_key_uses_i18n_key(self, bot, monkeypatch, tmp_path):
        """default_provider set but key deleted → settings_provider_no_key used."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({"api_keys": {}, "default_provider": "openrouter"}))
        out = bot.cmd_settings()
        expected = bot.t("settings_provider_no_key", provider="openrouter")
        assert expected in out

    def test_no_raw_string_cerrahisi_in_settings(self, bot, monkeypatch, tmp_path):
        """settings output must not contain fragment from no_ai_no_keys hack."""
        monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
        bot._cfg_cache = None
        monkeypatch.setattr(bot, "OPENROUTER_API_KEY", "")
        bot.mutate_cfg(lambda c: c.update({"api_keys": {}, "default_provider": ""}))
        out = bot.cmd_settings()
        # The old hack produced text like "AI mode is off" (from no_ai_no_keys)
        assert "no API key configured" not in out
