"""tests/test_watchword_button.py — N3 watchword inline button tests.

Coverage:
- _watchword_analyzable_hits: valid ticker, CIK rejection, mixed, all-CIK→empty, cap, empty
- Integration: keyboard path vs plain-text path (mocked send functions)
"""

import pytest


# ─── _watchword_analyzable_hits ───────────────────────────

class TestWatchwordAnalyzableHits:
    def _hit(self, ticker_or_cik="AAPL", form="8-K", date="2026-06-15"):
        return {"ticker_or_cik": ticker_or_cik, "form": form, "date": date,
                "accession": "0001234567-26-000001", "url": "http://x"}

    def test_valid_ticker_included(self, bot):
        hits = [self._hit("AAPL"), self._hit("MSFT")]
        result = bot._watchword_analyzable_hits(hits)
        assert len(result) == 2
        assert result[0] == ("AAPL", "8-K", "2026-06-15")

    def test_cik_rejected(self, bot):
        hits = [self._hit("0001234567")]
        assert bot._watchword_analyzable_hits(hits) == []

    def test_mixed_ticker_and_cik(self, bot):
        hits = [self._hit("AAPL"), self._hit("0001234567"), self._hit("MSFT")]
        result = bot._watchword_analyzable_hits(hits)
        assert len(result) == 2
        assert result[0][0] == "AAPL"
        assert result[1][0] == "MSFT"

    def test_all_cik_returns_empty(self, bot):
        hits = [self._hit("0001234567"), self._hit("0009998888")]
        assert bot._watchword_analyzable_hits(hits) == []

    def test_cap_at_five(self, bot):
        tickers = ["A", "B", "C", "D", "E", "F", "G", "H"]
        hits = [self._hit(t) for t in tickers]
        result = bot._watchword_analyzable_hits(hits)
        assert len(result) == 5
        assert result[4][0] == "E"

    def test_empty_input(self, bot):
        assert bot._watchword_analyzable_hits([]) == []

    def test_adsh_fallback_rejected(self, bot):
        """adsh[:10] fallback is not a valid ticker."""
        hits = [self._hit("0001234567")]
        assert bot._watchword_analyzable_hits(hits) == []


# ─── Integration: keyboard vs plain text ──────────────────

class TestWatchwordButtonIntegration:
    def test_analyzable_hits_use_keyboard(self, bot, monkeypatch):
        """When analyzable hits exist, _tg_with_keyboard_to is called."""
        sent = []
        monkeypatch.setattr(bot, "_tg_with_keyboard_to",
                            lambda cid, text, kb: sent.append(("kb", cid, text, kb)))
        monkeypatch.setattr(bot, "register_alarm_hits", lambda h: "testtoken")
        monkeypatch.setattr(bot, "build_alarm_keyboard",
                            lambda tok, h, d: {"inline_keyboard": [["btn"]]})

        hits = [{"ticker_or_cik": "AAPL", "form": "8-K", "date": "2026-06-15",
                 "accession": "000123", "url": "http://x"}]
        analyzable = bot._watchword_analyzable_hits(hits)
        assert len(analyzable) == 1

        # Simulate the bg_thread logic
        text = "alert text"
        if analyzable:
            token = bot.register_alarm_hits(analyzable)
            bot._tg_with_keyboard_to("100", text,
                bot.build_alarm_keyboard(token, analyzable, set()))
        else:
            bot._tg_to("100", text)

        assert len(sent) == 1
        assert sent[0][0] == "kb"
        assert sent[0][1] == "100"
        assert sent[0][3] is not None

    def test_no_analyzable_uses_plain_text(self, bot, monkeypatch):
        """When no analyzable hits, _tg_to is called (plain text)."""
        sent_kb = []
        sentPlain = []
        monkeypatch.setattr(bot, "_tg_with_keyboard_to",
                            lambda cid, text, kb: sent_kb.append(text))
        monkeypatch.setattr(bot, "_tg_to",
                            lambda cid, text: sentPlain.append(text))

        hits = [{"ticker_or_cik": "0001234567", "form": "8-K", "date": "2026-06-15",
                 "accession": "000123", "url": "http://x"}]
        analyzable = bot._watchword_analyzable_hits(hits)
        assert analyzable == []

        text = "alert text"
        if analyzable:
            bot._tg_with_keyboard_to("100", text, None)
        else:
            bot._tg_to("100", text)

        assert sent_kb == []
        assert len(sentPlain) == 1
