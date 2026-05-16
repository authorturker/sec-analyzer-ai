"""
Tests for E4 — build_compare_prompt is pure and easy to verify.
"""


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
