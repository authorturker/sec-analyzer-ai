"""
Tests for pure (no-IO) and near-pure helpers.

These functions are the result of the scan_ticker refactor — they were
extracted specifically so they could be tested in isolation.
"""
import pytest


# ─── render_filing_message ────────────────────────────────
class TestRenderFilingMessage:
    def test_with_diff(self, bot):
        msg = bot.render_filing_message(
            "AAPL", "10-K", "2026-04-01",
            "Strong fundamentals.\nGross margin: 38%.",
            "➕ AI competition risk added",
        )
        assert "AAPL" in msg
        assert "10-K" in msg
        assert "2026-04-01" in msg
        assert "Strong fundamentals" in msg
        assert "AI competition risk added" in msg
        # Risk section header should appear when diff is present
        assert "Risk" in msg

    def test_without_diff(self, bot):
        msg = bot.render_filing_message(
            "MSFT", "8-K", "2026-05-10",
            "Ad-hoc disclosure.", ""
        )
        assert "MSFT" in msg
        assert "Ad-hoc disclosure" in msg
        # No risk section when diff is empty
        assert "Risk Factor" not in msg

    def test_separator_present(self, bot):
        msg = bot.render_filing_message("X", "10-K", "2026", "body", "")
        # 28-character separator at the end
        assert "─" * 28 in msg


# ─── extract_section (section extraction) ─────────────────────────
class TestHazirla:
    def test_unknown_form_truncates_at_max(self, bot):
        text = "a" * 5000
        out = bot.extract_section(text, "8-K", 1000)
        assert len(out) == 1000

    def test_10k_extracts_from_first_item(self, bot):
        text = "header garbage\nItem 1. Business\nDescription here\nItem 2. blah"
        out = bot.extract_section(text, "10-K", 200)
        assert "Item 1." in out
        assert "Description here" in out
        assert "header garbage" not in out

    def test_10k_no_items_falls_back_to_truncate(self, bot):
        text = "no item headers anywhere\n" * 100
        out = bot.extract_section(text, "10-K", 100)
        assert len(out) == 100

    def test_truncation_marker_appended(self, bot):
        text = "Item 1. start\n" + ("filler line\n" * 500)
        out = bot.extract_section(text, "10-K", 200)
        assert "[text truncated]" in out


# ─── _risk_section ────────────────────────────────────────
class TestRiskSection:
    def test_extracts_after_risk_factor_marker(self, bot):
        text = "preamble\nrisk factor: cyber threat\nmore detail\nitem 2. blah"
        out = bot._risk_section(text)
        assert "cyber threat" in out
        assert "preamble" not in out

    def test_stops_at_item_1b(self, bot):
        text = "item 1a. risk\nbig risk\nitem 1b. resolved\nshould not appear"
        out = bot._risk_section(text)
        assert "big risk" in out
        assert "should not appear" not in out

    def test_no_risk_section(self, bot):
        out = bot._risk_section("no relevant markers here")
        assert out == ""


# ─── _match_form ──────────────────────────────────────────
class TestMatchForm:
    @pytest.mark.parametrize("raw,expected", [
        ("10-K",     "10-K"),
        ("10-Q",     "10-Q"),
        ("8-K",      "8-K"),
        ("4",        "4"),
        ("SC 13G",   "SC 13G"),
        ("SC13G",    "SC 13G"),    # whitespace-insensitive
        ("sc 13g",   None),         # uppercase required
        ("UNKNOWN",  None),
        ("DEF 14A",  "DEF 14A"),
        ("DEF14A",   "DEF 14A"),
    ])
    def test_match(self, bot, raw, expected):
        assert bot._match_form(raw) == expected


# ─── _md_escape ───────────────────────────────────────────
class TestMdEscape:
    def test_escapes_markdown_chars(self, bot):
        out = bot._md_escape("*bold* _italic_ `code` [link]")
        # Original chars remain (escaped), so content is preserved
        assert "bold" in out and "italic" in out and "code" in out and "link" in out
        # Each special char is preceded by a backslash
        for ch in ["*", "_", "`", "["]:
            assert "\\" + ch in out

    def test_preserves_normal_text(self, bot):
        s = "Plain text with no specials"
        assert bot._md_escape(s) == s

    def test_escapes_backslash_first(self, bot):
        # If backslash weren't escaped first, '\*' would become '\\*'
        # then '*' would replace creating '\\\*' — wrong.
        # Verify '\' is properly escaped to '\\' and '*' to '\*' independently.
        out = bot._md_escape("a*b")
        assert out == "a\\*b"
        out2 = bot._md_escape("a\\b")
        assert out2 == "a\\\\b"


# ─── cache_key / is_new_in_cache / mark_processed ──────────────────────────
class TestCacheHelpers:
    def test_cache_key_deterministic(self, bot):
        assert bot.cache_key("AAPL", "10-K", "2026-01-01") == \
               bot.cache_key("AAPL", "10-K", "2026-01-01")

    def test_cache_key_distinct(self, bot):
        a = bot.cache_key("AAPL", "10-K", "2026-01-01")
        b = bot.cache_key("AAPL", "10-K", "2026-01-02")
        assert a != b

    def test_is_new_in_cache_and_mark_processed(self, bot):
        cache: dict = {}
        assert bot.is_new_in_cache(cache, "AAPL", "8-K", "2026-05-01")
        bot.mark_processed(cache, "AAPL", "8-K", "2026-05-01")
        assert not bot.is_new_in_cache(cache, "AAPL", "8-K", "2026-05-01")
        # Different filing still considered new
        assert bot.is_new_in_cache(cache, "AAPL", "8-K", "2026-05-02")


# ─── _parse_hhmm ─────────────────────────────────────────
class TestSaatDakika:
    @pytest.mark.parametrize("inp,expected", [
        ("08:00",  (8, 0)),
        ("23:59",  (23, 59)),
        ("00:00",  (0, 0)),
        ("9:5",    (9, 5)),
        ("invalid", (-1, -1)),
        ("",       (-1, -1)),
        ("12",     (-1, -1)),
        ("12:34:56", (-1, -1)),
    ])
    def test_parse(self, bot, inp, expected):
        assert bot._parse_hhmm(inp) == expected


# ─── build_prompt ─────────────────────────────────────────
class TestBuildPrompt:
    def test_default_template_for_unknown(self, bot):
        out = bot.build_prompt("X", "UNKNOWN", "2026", "body")
        assert out.endswith(bot._PROMPT_DEFAULT)

    def test_custom_overrides_template(self, bot):
        custom = "Just summarize in one paragraph."
        out = bot.build_prompt("X", "10-K", "2026", "body", custom)
        assert out.endswith(custom)
        # 10-K default text should not appear
        assert "Top 3 critical risks" not in out

    def test_form_4_alias(self, bot):
        out_4   = bot.build_prompt("X", "4",   "2026", "b")
        out_144 = bot.build_prompt("X", "144", "2026", "b")
        # Both should end with the same template body
        assert out_4.split("\n\n", 2)[-1] == out_144.split("\n\n", 2)[-1]

    def test_sc13_alias(self, bot):
        out_g = bot.build_prompt("X", "SC 13G", "2026", "b")
        out_d = bot.build_prompt("X", "SC 13D", "2026", "b")
        assert out_g.split("\n\n", 2)[-1] == out_d.split("\n\n", 2)[-1]

    def test_header_format(self, bot):
        out = bot.build_prompt("AAPL", "10-K", "2026-04-01", "BODY")
        assert "AAPL — 10-K (2026-04-01)" in out
        assert "BODY" in out


# ─── EDGAR identity validation ────────────────────────────
class TestValidateEdgarIdentity:
    @pytest.mark.parametrize("identity,ok", [
        ("Real Person real@example.com", True),
        ("First Last user.name+tag@sub.example.co.uk", True),
        ("",                              False),
        ("Your Name yourname@email.com",  False),  # placeholder
        ("NoEmail Here",                  False),
        ("only@email.com",                False),  # missing name
        ("Name only",                     False),  # missing email
    ])
    def test_validation(self, bot, identity, ok):
        result, msg = bot.validate_edgar_identity(identity)
        assert result is ok
        assert isinstance(msg, str)
