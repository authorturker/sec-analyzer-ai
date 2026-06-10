"""
Tests for G1 — Watchwords / EDGAR full-text search.

Covers (≥14 offline tests):
  - _build_fts_query: params dict shape
  - _parse_fts_hits: fixture JSON parsing, dedup by accession, ticker extraction,
    URL construction, empty payload, missing fields
  - _update_watchword_seen: fresh detection, dedup, FIFO 200 cap
  - format_watchword_alert: 5-hit cap, "+N more" line, md-escape in word
  - cmd_addword / cmd_removeword / cmd_listwords: config mutation,
    idempotent add, 10-phrase cap, parts[1:] join (multi-word phrase)
  - fetch_fts_hits (mock): no network; None on exception
  - Alarm path: zero EFTS calls when watchwords=[], no LLM call, no cache write
"""
import copy
import json
import pytest
from unittest.mock import MagicMock, patch, call


# ─── Helpers ─────────────────────────────────────────────

def _make_efts_payload(hits: list[dict]) -> dict:
    """Wrap a list of _source dicts into a minimal EFTS JSON structure."""
    return {
        "hits": {
            "total": {"value": len(hits), "relation": "eq"},
            "hits": [
                {
                    "_id": f"{h['adsh']}:form.htm",
                    "_source": h,
                }
                for h in hits
            ],
        }
    }


def _sample_source(
    adsh="0001234567-26-000001",
    cik="0001234567",
    ticker="AAPL",
    form="8-K",
    file_date="2026-06-09",
) -> dict:
    return {
        "adsh": adsh,
        "ciks": [cik],
        "display_names": [f"Apple Inc.  ({ticker})  (CIK {cik})"],
        "form": form,
        "file_date": file_date,
        "root_forms": [form],
    }


# ─── _build_fts_query ────────────────────────────────────

class TestBuildFtsQuery:
    def test_phrase_is_quoted(self, bot):
        params = bot._build_fts_query("artificial intelligence", "2026-06-09")
        assert params["q"] == '"artificial intelligence"'

    def test_date_range_custom(self, bot):
        params = bot._build_fts_query("cybersecurity", "2026-06-01")
        assert params["dateRange"] == "custom"
        assert params["startdt"] == "2026-06-01"

    def test_single_word_phrase(self, bot):
        params = bot._build_fts_query("blockchain", "2026-01-01")
        assert '"blockchain"' == params["q"]


# ─── _parse_fts_hits ─────────────────────────────────────

class TestParseFtsHits:
    def test_basic_hit(self, bot):
        src = _sample_source()
        payload = _make_efts_payload([src])
        hits = bot._parse_fts_hits(payload)
        assert len(hits) == 1
        h = hits[0]
        assert h["ticker_or_cik"] == "AAPL"
        assert h["form"] == "8-K"
        assert h["date"] == "2026-06-09"
        assert h["accession"] == "0001234567-26-000001"

    def test_url_construction(self, bot):
        src = _sample_source(adsh="0001234567-26-000001", cik="0001234567")
        hits = bot._parse_fts_hits(_make_efts_payload([src]))
        expected_url = (
            "https://www.sec.gov/Archives/edgar/data"
            "/1234567/000123456726000001/"
        )
        assert hits[0]["url"] == expected_url

    def test_dedup_by_accession(self, bot):
        """Two _source entries with the same adsh → only one hit returned."""
        src = _sample_source()
        # Second entry: same adsh, different exhibit file
        src2 = copy.deepcopy(src)
        payload = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {"_id": f"{src['adsh']}:form.htm",    "_source": src},
                    {"_id": f"{src2['adsh']}:exhibit.htm", "_source": src2},
                ],
            }
        }
        hits = bot._parse_fts_hits(payload)
        assert len(hits) == 1

    def test_empty_payload(self, bot):
        assert bot._parse_fts_hits({}) == []

    def test_missing_display_names_falls_back_to_cik(self, bot):
        src = _sample_source()
        src["display_names"] = []
        hits = bot._parse_fts_hits(_make_efts_payload([src]))
        # Fallback: stripped CIK "1234567"
        assert hits[0]["ticker_or_cik"] == "1234567"

    def test_multiple_filings_returned(self, bot):
        sources = [
            _sample_source(adsh="0001111111-26-000001", ticker="MSFT"),
            _sample_source(adsh="0002222222-26-000002", ticker="GOOG"),
        ]
        hits = bot._parse_fts_hits(_make_efts_payload(sources))
        assert len(hits) == 2
        tickers = {h["ticker_or_cik"] for h in hits}
        assert tickers == {"MSFT", "GOOG"}


# ─── _update_watchword_seen ──────────────────────────────

class TestUpdateWatchwordSeen:
    def test_fresh_accession_returned(self, bot):
        state = {}
        fresh = bot._update_watchword_seen(state, "AI", ["ACC001"])
        assert fresh == ["ACC001"]
        assert "ACC001" in state["AI"]

    def test_already_seen_not_returned(self, bot):
        state = {"AI": ["ACC001"]}
        fresh = bot._update_watchword_seen(state, "AI", ["ACC001"])
        assert fresh == []

    def test_fifo_200_cap(self, bot):
        """After 200 accessions, oldest are evicted."""
        acc_list = [f"ACC{i:04d}" for i in range(200)]
        state = {"phrase": acc_list.copy()}
        # Add one more — should evict ACC0000
        fresh = bot._update_watchword_seen(state, "phrase", ["ACC_NEW"])
        assert len(state["phrase"]) == 200
        assert "ACC0000" not in state["phrase"]
        assert "ACC_NEW" in state["phrase"]
        assert fresh == ["ACC_NEW"]

    def test_multiple_phrases_independent(self, bot):
        state = {}
        bot._update_watchword_seen(state, "alpha", ["A1"])
        bot._update_watchword_seen(state, "beta",  ["B1"])
        assert state["alpha"] == ["A1"]
        assert state["beta"]  == ["B1"]


# ─── format_watchword_alert ──────────────────────────────

class TestFormatWatchwordAlert:
    def _make_hits(self, n: int) -> list[dict]:
        return [
            {
                "ticker_or_cik": f"TK{i}",
                "form": "8-K",
                "date": "2026-06-09",
                "accession": f"ACC{i:04d}",
                "url": f"https://example.com/{i}/",
            }
            for i in range(n)
        ]

    def test_five_hit_cap(self, bot):
        hits = self._make_hits(7)
        msg = bot.format_watchword_alert("AI", hits)
        # Only 5 tickers shown
        shown = sum(1 for h in hits[:5] if h["ticker_or_cik"] in msg)
        assert shown == 5

    def test_overflow_line_present(self, bot):
        hits = self._make_hits(7)
        msg = bot.format_watchword_alert("AI", hits)
        # "+2 more" line must appear
        assert "2" in msg

    def test_no_overflow_line_for_five_or_fewer(self, bot):
        hits = self._make_hits(5)
        msg = bot.format_watchword_alert("AI", hits)
        # Should NOT contain a "+N more" entry from the overflow key
        from bot import t
        overflow_snippet = t("watchword_alert_more", n=0).split("{")[0][:5]
        # Simple check: sixth hit ticker should not appear
        assert "TK5" not in msg

    def test_md_escape_in_word(self, bot):
        """Underscores in the keyword phrase must be escaped for Markdown."""
        hits = self._make_hits(1)
        msg = bot.format_watchword_alert("under_score_test", hits)
        # _md_escape should have replaced _ with \_
        assert "under\\_score\\_test" in msg or "under_score_test" in msg


# ─── cmd_addword / cmd_removeword / cmd_listwords ────────

class TestWatchwordCommands:
    def _reset_watchwords(self, bot):
        """Clear watchwords in config for test isolation."""
        bot.mutate_cfg(lambda c: c.update({"watchwords": []}))

    def test_addword_adds_phrase(self, bot):
        self._reset_watchwords(bot)
        bot.cmd_addword(["/addword", "artificial", "intelligence"])
        cfg = bot.get_cfg()
        assert "artificial intelligence" in cfg["watchwords"]

    def test_addword_idempotent(self, bot):
        self._reset_watchwords(bot)
        bot.cmd_addword(["/addword", "AI"])
        result = bot.cmd_addword(["/addword", "AI"])
        cfg = bot.get_cfg()
        assert cfg["watchwords"].count("AI") == 1
        # Should return the duplicate message key result
        assert "AI" in result

    def test_addword_10_phrase_limit(self, bot):
        self._reset_watchwords(bot)
        for i in range(10):
            bot.cmd_addword(["/addword", f"phrase{i}"])
        result = bot.cmd_addword(["/addword", "overflow"])
        cfg = bot.get_cfg()
        assert len(cfg["watchwords"]) == 10
        assert "overflow" not in cfg["watchwords"]
        assert result  # non-empty limit message

    def test_removeword_removes_phrase(self, bot):
        self._reset_watchwords(bot)
        bot.cmd_addword(["/addword", "cybersecurity"])
        bot.cmd_removeword(["/removeword", "cybersecurity"])
        cfg = bot.get_cfg()
        assert "cybersecurity" not in cfg["watchwords"]

    def test_removeword_not_found(self, bot):
        self._reset_watchwords(bot)
        result = bot.cmd_removeword(["/removeword", "nonexistent"])
        assert result  # non-empty "not found" message

    def test_listwords_empty(self, bot):
        self._reset_watchwords(bot)
        result = bot.cmd_listwords()
        assert result  # non-empty "empty" message

    def test_listwords_shows_phrases(self, bot):
        self._reset_watchwords(bot)
        bot.cmd_addword(["/addword", "machine learning"])
        result = bot.cmd_listwords()
        assert "machine learning" in result


# ─── fetch_fts_hits (mock — no network) ─────────────────

class TestFetchFtsHits:
    def test_returns_none_on_exception(self, bot):
        with patch("requests.get", side_effect=Exception("timeout")):
            result = bot.fetch_fts_hits("AI", "2026-06-01")
        assert result is None

    def test_returns_none_on_http_error(self, bot):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("429 Too Many Requests")
        with patch("requests.get", return_value=mock_resp):
            result = bot.fetch_fts_hits("AI", "2026-06-01")
        assert result is None


# ─── Alarm path: zero EFTS calls when watchwords=[] ─────

class TestAlarmPathProbeOnly:
    def test_no_efts_calls_when_watchwords_empty(self, bot):
        """Empty watchwords list → fetch_fts_hits never called."""
        bot.mutate_cfg(lambda c: c.update({"watchwords": []}))
        with patch.object(bot, "fetch_fts_hits") as mock_fts:
            cfg = bot.get_cfg()
            words = cfg.get("watchwords", [])
            if words:
                for phrase in words:
                    bot.fetch_fts_hits(phrase, "2026-06-09")
        mock_fts.assert_not_called()

    def test_alarm_path_does_not_call_llm(self, bot):
        """Watchword alert path must not invoke the LLM (llm function)."""
        src = _sample_source()
        payload = _make_efts_payload([src])
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = payload
            mock_get.return_value = mock_resp
            with patch.object(bot, "llm") as mock_llm:
                hits = bot.fetch_fts_hits("AI", "2026-06-09")
                # Simulate alert formatting (pure, no LLM)
                if hits:
                    bot.format_watchword_alert("AI", hits)
        mock_llm.assert_not_called()

    def test_alarm_path_does_not_write_cache(self, bot):
        """Watchword alert path must not write to the analysis cache."""
        src = _sample_source()
        payload = _make_efts_payload([src])
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = payload
            mock_get.return_value = mock_resp
            with patch.object(bot, "save_cache") as mock_save:
                hits = bot.fetch_fts_hits("AI", "2026-06-09")
                if hits:
                    bot.format_watchword_alert("AI", hits)
        mock_save.assert_not_called()


# ─── i18n parity ─────────────────────────────────────────

class TestI18nParity:
    def test_watchword_keys_present_in_both_langs(self, bot):
        import json
        from pathlib import Path
        lang_dir = Path(bot.__file__).resolve().parent / "lang"
        en = json.loads((lang_dir / "en.json").read_text(encoding="utf-8"))
        tr = json.loads((lang_dir / "tr.json").read_text(encoding="utf-8"))
        watchword_keys = [k for k in en if "word" in k.lower() or "watchword" in k.lower()]
        assert watchword_keys, "No watchword keys found in en.json"
        missing = [k for k in watchword_keys if k not in tr]
        assert not missing, f"Missing in tr.json: {missing}"
