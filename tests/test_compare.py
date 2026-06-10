"""
Tests for E4 — build_compare_prompt is pure and easy to verify.
H7 — cmd_compare wiring: full body must reach extract_section, not
just the cover page (regression test for "limited to cover pages" bug).
"""
import pytest


class TestBuildComparePrompt:
    def test_includes_both_tickers_and_form(self, bot):
        out = bot.build_compare_prompt(
            "AAPL", "MSFT", "10-K",
            "AAPL filing body...",
            "MSFT filing body...",
        )
        assert "AAPL" in out
        assert "MSFT" in out
        assert "10-K" in out
        assert "AAPL filing body" in out
        assert "MSFT filing body" in out

    def test_includes_comparison_instructions(self, bot):
        out = bot.build_compare_prompt("A", "B", "10-Q", "txt-a", "txt-b")
        # The four numbered comparison axes should all appear
        for marker in ("Business momentum", "Key risks",
                       "Capital allocation", "attractiveness"):
            assert marker in out

    def test_separator_between_filings(self, bot):
        out = bot.build_compare_prompt("A", "B", "8-K", "AAA", "BBB")
        # The fenced section headers help the LLM separate the two filings
        assert "=== A 8-K ===" in out
        assert "=== B 8-K ===" in out


# ─── H7 — /compare full-body wiring ──────────────────────────
class TestComparePerSideBudget:
    """The per-side budget constant must stay within the llm() clamp."""

    def test_constant_defined(self, bot):
        assert hasattr(bot, "_COMPARE_PER_SIDE_MAX")
        assert isinstance(bot._COMPARE_PER_SIDE_MAX, int)

    def test_two_sides_fit_under_llm_clamp(self, bot):
        # Two filings share one prompt → 2 × per-side + scaffolding ≤ clamp.
        # Leave ~1000 chars headroom for the prompt scaffold.
        assert 2 * bot._COMPARE_PER_SIDE_MAX + 1000 <= bot._LLM_MAX_PROMPT

    def test_per_side_exceeds_cover_page(self, bot):
        # 10-K cover pages are typically 8-12K chars. Per-side budget must
        # comfortably exceed that so we capture Item 1 onwards, not just cover.
        assert bot._COMPARE_PER_SIDE_MAX > 12000


class TestCmdCompareFullBody:
    """Regression tests for the cover-page bug: cmd_compare must pass the
    full filing body to extract_section so Item anchors can be found."""

    def _make_10k_body(self, ticker: str) -> str:
        """Synthetic 10-K: ~15K cover page, then Item 1/1A/7/8 sections."""
        cover = (
            f"UNITED STATES SECURITIES AND EXCHANGE COMMISSION\n"
            f"FORM 10-K\n"
            f"REGISTRANT: {ticker} CORPORATION\n"
            f"CIK 0001234567 — Check the box: [X] Large accelerated filer\n"
        )
        # Pad cover to ~14K chars (well past cfg["max_chars"]=10000 default)
        cover += ("Cover page boilerplate line.\n" * 500)
        body = (
            "Item 1. Business\n"
            f"{ticker} operates in the optical components industry. " * 80 + "\n"
            "Item 1A. Risk Factors\n"
            f"{ticker} faces supply-chain and tariff risks. " * 80 + "\n"
            "Item 7. Management's Discussion and Analysis\n"
            f"{ticker} revenue grew 23% year-over-year. " * 80 + "\n"
            "Item 8. Financial Statements\n"
            f"{ticker} total revenue: $1.2B. Net income: $145M. " * 80 + "\n"
        )
        return cover + body

    @pytest.fixture
    def captured_prompt(self, bot, monkeypatch):
        """Run cmd_compare with mocked fetch/llm/tg and capture the LLM prompt."""
        captured = {"prompt": None}

        def fake_fetch(ticker, forms, lookback_days, *args, **kwargs):
            # Defensive: H7 fix MUST NOT pass max_chars_per. If a future
            # regression reintroduces it, this assertion fires.
            assert kwargs.get("max_chars_per") is None, (
                "cmd_compare must not pre-truncate text via max_chars_per — "
                "extract_section needs the full body to find Item anchors."
            )
            form = forms[0]
            return [(form, "2026-05-01", self._make_10k_body(ticker), "")]

        def fake_llm(prompt, model):
            captured["prompt"] = prompt
            return "stub-analysis"

        monkeypatch.setattr(bot, "fetch_new_filings", fake_fetch)
        monkeypatch.setattr(bot, "llm", fake_llm)
        monkeypatch.setattr(bot, "tg", lambda *a, **k: None)
        return bot, captured

    def test_compare_10k_sends_item_body_not_cover(self, captured_prompt):
        bot, captured = captured_prompt
        bot.cmd_compare(["/compare", "LITE", "COHR", "10-K"])
        prompt = captured["prompt"]
        assert prompt is not None, "LLM should have been called"
        # The body sections must appear — proves cover-page-only is fixed.
        assert "Item 1. Business" in prompt
        assert "Item 1A. Risk Factors" in prompt
        # MD&A is reachable inside the 14K per-side budget
        assert "Management's Discussion" in prompt

    def test_compare_10q_uses_section_extraction(self, bot, monkeypatch):
        captured = {"prompt": None}

        def make_10q(tk):
            cover = "Cover page boilerplate line.\n" * 500  # ~14K
            body = (
                "Item 1. Financial Statements\n"
                f"{tk} Q1 revenue: $300M. " * 50 + "\n"
                "Item 2. Management's Discussion and Analysis\n"
                f"{tk} margin expansion driven by mix. " * 50 + "\n"
            )
            return cover + body

        def fake_fetch(ticker, forms, lookback_days, *args, **kwargs):
            assert kwargs.get("max_chars_per") is None
            return [(forms[0], "2026-05-01", make_10q(ticker), "")]

        def fake_llm(prompt, model):
            captured["prompt"] = prompt
            return "stub"

        monkeypatch.setattr(bot, "fetch_new_filings", fake_fetch)
        monkeypatch.setattr(bot, "llm", fake_llm)
        monkeypatch.setattr(bot, "tg", lambda *a, **k: None)

        bot.cmd_compare(["/compare", "LITE", "COHR", "10-Q"])
        prompt = captured["prompt"]
        assert "Item 1. Financial Statements" in prompt
        assert "Item 2. Management's Discussion" in prompt

    def test_compare_prompt_within_llm_clamp(self, captured_prompt):
        bot, captured = captured_prompt
        bot.cmd_compare(["/compare", "LITE", "COHR", "10-K"])
        # The prompt fed to llm() must fit under the global clamp without
        # being truncated mid-section by llm()'s own safety cap.
        assert len(captured["prompt"]) <= bot._LLM_MAX_PROMPT
