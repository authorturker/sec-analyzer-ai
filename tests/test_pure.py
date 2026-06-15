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


# ─── extract_section ─────────────────────────────────────
class TestExtractSection:
    """Characterization tests for extract_section() — locks in 8-K + existing behaviour."""

    # ── 10-K / 10-Q (unchanged baseline) ──────────────────
    def test_10k_extracts_named_items(self, bot):
        text = "garbage\nItem 1. Business\nwe do things\nItem 7. MD&A\ndiscussion"
        out = bot.extract_section(text, "10-K", 5000)
        assert "we do things" in out
        assert "garbage" not in out

    def test_10k_fallback_when_no_match(self, bot):
        text = "x" * 300
        out = bot.extract_section(text, "10-K", 200)
        assert len(out) == 200

    def test_unknown_form_returns_truncated_text(self, bot):
        text = "prefix\nItem 1.01 stuff\nmore"
        # SC 13G has no keywords → blind truncation
        out = bot.extract_section(text, "SC 13G", 10)
        assert out == text[:10]

    # ── 8-K (core fix) ────────────────────────────────────
    def test_8k_extracts_item_101(self, bot):
        text = "cover page boilerplate\nItem 1.01 Entry into a Material Agreement\nKey deal details"
        out = bot.extract_section(text, "8-K", 5000)
        assert "Key deal details" in out
        assert "cover page boilerplate" not in out

    def test_8k_extracts_item_201(self, bot):
        text = "preamble\nItem 2.01 Completion of Acquisition\nDeal closed for $1B"
        out = bot.extract_section(text, "8-K", 5000)
        assert "Deal closed for $1B" in out

    def test_8k_extracts_item_502(self, bot):
        text = "unrelated\nItem 5.02 Departure of Directors\nCEO resigned effective today"
        out = bot.extract_section(text, "8-K", 5000)
        assert "CEO resigned effective today" in out

    def test_8k_extracts_item_105_cybersecurity(self, bot):
        # Item 1.05 — Material Cybersecurity Incidents (SEC 2023 rule)
        text = "boilerplate\nItem 1.05 Material Cybersecurity Incident\nBreach detected in Q3"
        out = bot.extract_section(text, "8-K", 5000)
        assert "Breach detected in Q3" in out

    def test_8k_extracts_item_701(self, bot):
        text = "cover\nItem 7.01 Regulation FD Disclosure\nEarnings guidance raised"
        out = bot.extract_section(text, "8-K", 5000)
        assert "Earnings guidance raised" in out

    def test_8k_extracts_item_901(self, bot):
        text = "cover\nItem 9.01 Financial Statements and Exhibits\nSee attached"
        out = bot.extract_section(text, "8-K", 5000)
        assert "See attached" in out

    def test_8k_fallback_no_items_found(self, bot):
        # No recognised 8-K items → blind truncation (not empty string)
        text = "plain text without any item headers, just prose " * 5
        out = bot.extract_section(text, "8-K", 50)
        assert len(out) == 50

    def test_8k_respects_max_k(self, bot):
        # extract_section appends whole lines then checks; once chars >= max_k the
        # truncation marker is added and the loop breaks — guaranteeing no further
        # lines are added (characterization of existing line-level granularity).
        body = "Item 1.01 Agreement\nshort line A\nshort line B\n" + "x" * 5000
        out = bot.extract_section(body, "8-K", 30)
        # Truncation marker must appear and no lines past the cap line
        assert "[text truncated]" in out
        assert "short line A" in out     # first line within limit captured
        # The 5000-char line caused the cap to fire — output is finite
        assert len(out) < 10000

    def test_8k_case_insensitive_matching(self, bot):
        # EDGAR filings often use mixed-case headings
        text = "ITEM 2.02 Results of Operations\nRevenue beat by 15%"
        out = bot.extract_section(text, "8-K", 5000)
        assert "Revenue beat by 15%" in out

    def test_8k_multiple_items_all_captured(self, bot):
        text = (
            "cover\n"
            "Item 1.01 Agreement\nDeal signed\n"
            "Item 5.02 Officers\nNew CFO appointed\n"
        )
        out = bot.extract_section(text, "8-K", 5000)
        assert "Deal signed" in out
        assert "New CFO appointed" in out


# ─── _match_form ──────────────────────────────────────────
class TestMatchForm:
    @pytest.mark.parametrize("raw,expected", [
        ("10-K",     "10-K"),
        ("10-Q",     "10-Q"),
        ("8-K",      "8-K"),
        ("4",        "4"),
        ("SC 13G",   "SC 13G"),
        ("SC13G",    "SC 13G"),    # whitespace-insensitive
        ("sc 13g",   "SC 13G"),    # R3: normalize uppercases, so this now matches
        ("UNKNOWN",  None),
        ("DEF 14A",  "DEF 14A"),
        ("DEF14A",   "DEF 14A"),
        # R3 — separator-stripped matching
        ("10k",      "10-K"),      # bare, no dash
        ("10K",      "10-K"),      # uppercase bare
        ("sc13g",    "SC 13G"),    # no space
        ("8k",       "8-K"),       # lowercase bare
        ("def14a",   "DEF 14A"),   # compound bare
        # R3 — unicode dash variants
        ("10–K", "10-K"),     # en-dash
        ("10—K", "10-K"),     # em-dash
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


# ─── build_prompt F2 — facts_block injection ──────────────
class TestBuildPromptGrounding:
    """F2 acceptance: XBRL grounding injection in build_prompt."""

    FACTS = "AUDITED XBRL FACTS (period ending 2024-09-28):\n  Revenues: $391.04B"

    def test_empty_facts_block_byte_identical(self, bot):
        """facts_block='' must produce the exact same bytes as the pre-F2 call."""
        without = bot.build_prompt("AAPL", "10-K", "2026", "body text")
        with_empty = bot.build_prompt("AAPL", "10-K", "2026", "body text", facts_block="")
        assert without == with_empty

    def test_facts_block_appears_before_body(self, bot):
        out = bot.build_prompt("AAPL", "10-K", "2026", "BODY_SENTINEL",
                               facts_block=self.FACTS)
        assert out.index(self.FACTS) < out.index("BODY_SENTINEL")

    def test_grounding_instruction_between_facts_and_body(self, bot):
        out = bot.build_prompt("AAPL", "10-K", "2026", "BODY_SENTINEL",
                               facts_block=self.FACTS)
        pos_facts = out.index(self.FACTS)
        pos_instr = out.index(bot._GROUNDING_INSTRUCTION)
        pos_body  = out.index("BODY_SENTINEL")
        assert pos_facts < pos_instr < pos_body

    def test_custom_prompt_still_appended_with_facts(self, bot):
        custom = "Focus only on liquidity risk."
        out = bot.build_prompt("AAPL", "10-K", "2026", "body",
                               custom_prompt=custom, facts_block=self.FACTS)
        assert out.endswith(custom)
        assert self.FACTS in out
        assert bot._GROUNDING_INSTRUCTION in out

    def test_facts_block_with_default_template(self, bot):
        """Default template still appended when no custom_prompt, even with facts."""
        out = bot.build_prompt("AAPL", "10-K", "2026", "body",
                               facts_block=self.FACTS)
        # FACTS injected AND 10-K default template present
        assert self.FACTS in out
        assert bot.PROMPTS.get("10-K", bot._PROMPT_DEFAULT) in out

    def test_header_unchanged_with_facts(self, bot):
        out = bot.build_prompt("MSFT", "10-Q", "2025-03-31", "body",
                               facts_block=self.FACTS)
        assert out.startswith("MSFT — 10-Q (2025-03-31)\n\n")

    def test_empty_facts_block_no_grounding_instruction(self, bot):
        out = bot.build_prompt("X", "10-K", "2026", "body", facts_block="")
        assert bot._GROUNDING_INSTRUCTION not in out


# ─── _XBRL_FORMS membership ───────────────────────────────
class TestXbrlForms:
    def test_xbrl_forms_contains_10k_10q_20f(self, bot):
        assert "10-K" in bot._XBRL_FORMS
        assert "10-Q" in bot._XBRL_FORMS
        assert "20-F" in bot._XBRL_FORMS

    def test_xbrl_forms_excludes_8k_and_form4(self, bot):
        assert "8-K"  not in bot._XBRL_FORMS
        assert "4"    not in bot._XBRL_FORMS
        assert "144"  not in bot._XBRL_FORMS


# ─── fetch_new_filings 4-tuple contract ───────────────────
class TestFetchNewFilings4Tuple:
    """fetch_new_filings must return 4-tuples; non-XBRL forms must not call xbrl()."""

    def test_non_xbrl_form_never_calls_xbrl(self, bot, monkeypatch):
        """8-K is outside _XBRL_FORMS — filing.xbrl() must never be invoked."""
        xbrl_calls = {"n": 0}

        class _FakeFiling:
            filing_date = type("D", (), {"date": lambda self: __import__("datetime").date(2026, 5, 1)})()
            def text(self): return "8-K body text"
            def markdown(self): return "8-K body text"
            def xbrl(self):
                xbrl_calls["n"] += 1
                return None

        class _FakeCompany:
            def get_filings(self, form):
                return self
            def latest(self, n):
                return [_FakeFiling()]

        monkeypatch.setattr(bot, "get_company", lambda tk: _FakeCompany())
        monkeypatch.setattr(bot, "is_new_in_cache", lambda *a: True)

        rows = bot.fetch_new_filings("AAPL", ["8-K"], lookback_days=400)
        assert xbrl_calls["n"] == 0, "xbrl() must not be called for 8-K"

    def test_4tuple_structure_for_non_xbrl_form(self, bot, monkeypatch):
        """Even non-XBRL forms must return 4-tuples (facts_block="")."""
        class _FakeFiling:
            filing_date = type("D", (), {"date": lambda self: __import__("datetime").date(2026, 5, 1)})()
            def text(self): return "some body"
            def markdown(self): return "some body"

        class _FakeCompany:
            def get_filings(self, form):
                return self
            def latest(self, n):
                return [_FakeFiling()]

        monkeypatch.setattr(bot, "get_company", lambda tk: _FakeCompany())
        monkeypatch.setattr(bot, "is_new_in_cache", lambda *a: True)

        rows = bot.fetch_new_filings("AAPL", ["8-K"], lookback_days=400)
        assert len(rows) == 1
        assert len(rows[0]) == 4         # 4-tuple
        form, ds, text, facts_block = rows[0]
        assert form == "8-K"
        assert facts_block == ""         # non-XBRL form → empty


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


# ─── classify_error ───────────────────────────────────────
class TestClassifyError:
    def test_timeout(self, bot):
        import requests
        assert bot.classify_error(requests.exceptions.Timeout()) == "timeout"

    def test_connection_error(self, bot):
        import requests
        assert bot.classify_error(requests.exceptions.ConnectionError()) == "network"

    def test_rate_limit_http(self, bot):
        assert bot.classify_error(Exception("429 Too Many Requests")) == "rate_limit"

    def test_not_found_http(self, bot):
        assert bot.classify_error(Exception("404 not found")) == "not_found"

    def test_not_found_string(self, bot):
        assert bot.classify_error(Exception("company not found")) == "not_found"

    def test_unknown_fallback(self, bot):
        assert bot.classify_error(ValueError("boom")) == "unknown"


# ─── _backoff ─────────────────────────────────────────────
class TestBackoff:
    def test_base_case(self, bot):
        assert bot._backoff(0, 5, 120) == 5

    def test_doubles_each_attempt(self, bot):
        assert bot._backoff(1, 5, 120) == 10
        assert bot._backoff(2, 5, 120) == 20

    def test_cap_enforced(self, bot):
        assert bot._backoff(10, 5, 30) == 30

    def test_cap_equal_base(self, bot):
        assert bot._backoff(0, 60, 60) == 60


# ─── retry ────────────────────────────────────────────────
class TestRetry:
    def test_success_on_first_call(self, bot):
        calls = []
        def fn():
            calls.append(1)
            return "ok"
        assert bot.retry(fn, attempts=3, base=0, cap=0, label="t") == "ok"
        assert len(calls) == 1

    def test_succeeds_after_failures(self, bot):
        state = {"n": 0}
        def fn():
            state["n"] += 1
            if state["n"] < 3:
                raise ValueError("not yet")
            return "done"
        result = bot.retry(fn, attempts=4, base=0, cap=0, label="t")
        assert result == "done"
        assert state["n"] == 3

    def test_returns_none_on_exhaustion(self, bot):
        def fn(): raise RuntimeError("always fail")
        assert bot.retry(fn, attempts=3, base=0, cap=0, label="t") is None

    def test_on_error_called_each_failure(self, bot):
        errors = []
        def fn(): raise ValueError("x")
        bot.retry(fn, attempts=3, base=0, cap=0, label="t",
                  on_error=lambda e, a: errors.append(a))
        assert errors == [0, 1, 2]


# ─── _normalize_form_input (R3) ───────────────────────────
class TestNormalizeFormInput:
    def test_uppercase(self):
        from bot import _normalize_form_input
        assert _normalize_form_input("10-k") == "10-K"

    def test_strips_whitespace(self):
        from bot import _normalize_form_input
        assert _normalize_form_input("  10-K  ") == "10-K"

    def test_en_dash(self):
        from bot import _normalize_form_input
        assert _normalize_form_input("10–K") == "10-K"

    def test_em_dash(self):
        from bot import _normalize_form_input
        assert _normalize_form_input("10—K") == "10-K"

    def test_plain_ascii_unchanged(self):
        from bot import _normalize_form_input
        assert _normalize_form_input("SC 13G") == "SC 13G"


# ─── valid_ticker (R4) ────────────────────────────────────
class TestValidTicker:
    @pytest.mark.parametrize("sym,ok", [
        ("AAPL",    True),
        ("BRK.B",   True),
        ("BRK-B",   True),
        ("A",       True),
        ("ABCDEF",  True),
        ("",        False),
        ("TOOLONG", False),     # 7 chars
        ("123",     False),     # starts with digit
        ("aapl",    False),     # lowercase
        ("AAPL!",   False),     # special char
    ])
    def test_valid_ticker(self, bot, sym, ok):
        assert bot.valid_ticker(sym) is ok


# ─── build_weekly_csv (U5) ────────────────────────────────
class TestBuildWeeklyCsv:
    def _entry(self, ticker="AAPL", form="10-K", tarih="2026-05-01",
               ekleme="2026-05-02", analiz="Strong results."):
        return dict(ticker=ticker, form=form, tarih=tarih,
                    ekleme=ekleme, analiz=analiz)

    def test_header_row(self, bot):
        out = bot.build_weekly_csv([self._entry()])
        first = out.splitlines()[0]
        assert first == '"ticker","form","filing_date","added_at","analysis"'

    def test_data_row(self, bot):
        e = self._entry(ticker="MSFT", form="8-K")
        out = bot.build_weekly_csv([e])
        assert "MSFT" in out and "8-K" in out

    def test_empty_entries(self, bot):
        out = bot.build_weekly_csv([])
        assert out.strip() == '"ticker","form","filing_date","added_at","analysis"'

    def test_newline_in_analysis_flattened(self, bot):
        e = self._entry(analiz="line1\nline2")
        out = bot.build_weekly_csv([e])
        # Newline should be replaced by space inside the CSV field
        assert "line1 line2" in out

    def test_multiple_entries_row_count(self, bot):
        entries = [self._entry(ticker=f"T{i}") for i in range(3)]
        lines = [l for l in bot.build_weekly_csv(entries).splitlines() if l.strip()]
        assert len(lines) == 4  # 1 header + 3 data rows


# ─── _split_message ───────────────────────────────────────
class TestSplitMessage:
    def test_short_text_single_chunk(self, bot):
        result = bot._split_message("hello world")
        assert result == ["hello world"]

    def test_empty_text_returns_empty(self, bot):
        result = bot._split_message("")
        assert result == []

    def test_newline_boundary_preferred(self, bot):
        # Two lines that together exceed limit → split at newline boundary.
        line_a = "A" * 3000
        line_b = "B" * 3000
        result = bot._split_message(line_a + "\n" + line_b, limit=4000)
        assert len(result) == 2
        assert result[0] == line_a
        assert result[1] == line_b

    def test_oversized_single_line_hard_split(self, bot):
        # One line that exceeds limit → hard-split at limit chars.
        long = "X" * 9000
        result = bot._split_message(long, limit=4000)
        assert len(result) == 3
        assert all(len(c) <= 4000 for c in result)
        assert "".join(result) == long

    def test_oversized_line_mixed_with_normal_lines(self, bot):
        # Normal line + oversized line + normal line.
        normal = "short"
        huge   = "Y" * 5000
        result = bot._split_message(normal + "\n" + huge + "\n" + normal, limit=4000)
        assert all(len(c) <= 4000 for c in result)
        # The huge line alone needs at least 2 chunks
        assert len(result) >= 3

    def test_chunk_content_no_loss(self, bot):
        # All characters must survive the split (no data loss).
        text = "\n".join(["line" + str(i) + " " + "x" * 100 for i in range(100)])
        result = bot._split_message(text, limit=4000)
        assert "".join(result).replace("\n", "") == text.replace("\n", "")

    def test_all_chunks_within_limit(self, bot):
        import random, string
        random.seed(42)
        text = "".join(random.choices(string.ascii_letters + "\n", k=20000))
        result = bot._split_message(text, limit=4000)
        assert all(len(c) <= 4000 for c in result)

    def test_exactly_limit_chars_single_chunk(self, bot):
        text = "A" * 4000
        result = bot._split_message(text, limit=4000)
        assert len(result) == 1
        assert result[0] == text

    def test_whitespace_only_chunks_omitted(self, bot):
        # A blank text after stripping → no chunks.
        result = bot._split_message("   \n  \n  ", limit=4000)
        assert result == []
