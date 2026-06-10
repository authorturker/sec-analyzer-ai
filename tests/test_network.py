"""
tests/test_network.py — Opt-in live endpoint smoke tests.

Run with:  python -m pytest tests/ -q --network -m network

These tests hit real external endpoints (EDGAR, EFTS, Stooq).
They are SKIPPED in all normal runs (no --network flag) — CI is unaffected.

Assertions target SCHEMA (field presence, type), never exact values.
Live data changes constantly; value assertions produce permanent reds.

SEC courtesy: ≥1 s sleep between EDGAR/EFTS requests.  All requests carry
a real-looking User-Agent as required by SEC fair-access policy.
"""
import importlib
import sys
import time
from datetime import date, timedelta

import pytest
import requests

_UA = "Test User test@example.com"


# ── Fixture: real edgar module (bypasses the conftest import stub) ────────────

@pytest.fixture(scope="module")
def real_edgar():
    """Temporarily restore the real edgar package for EDGAR live tests.

    conftest._install_stubs() replaces sys.modules["edgar"] with a fake before
    bot.py is imported.  This fixture pops the stub out, loads the real package,
    and restores the stub when the module is done — leaving the offline suite
    completely undisturbed.
    """
    stub = sys.modules.pop("edgar", None)
    try:
        real = importlib.import_module("edgar")
        yield real
    finally:
        if stub is not None:
            sys.modules["edgar"] = stub
        elif "edgar" in sys.modules:
            del sys.modules["edgar"]


# ── EDGAR live tests ──────────────────────────────────────────────────────────

@pytest.mark.network
class TestEdgarLive:

    def test_10k_accessible(self, real_edgar):
        """AAPL latest 10-K: response exists and carries form type + date fields."""
        real_edgar.set_identity(_UA)
        company = real_edgar.Company("AAPL")
        filings = company.get_filings(form="10-K")
        assert filings is not None
        filing = filings[0]
        # Schema: form type must contain "10-K"
        form = getattr(filing, "form", None) or getattr(filing, "form_type", None)
        assert form is not None
        assert "10-K" in str(form)
        # Schema: filing date must be present
        filing_date = (
            getattr(filing, "filing_date", None) or getattr(filing, "date", None)
        )
        assert filing_date is not None

    def test_xbrl_object_reachable(self, real_edgar):
        """AAPL latest 10-K: XBRL object is reachable and non-None."""
        time.sleep(1)  # SEC courtesy
        real_edgar.set_identity(_UA)
        company = real_edgar.Company("AAPL")
        filings = company.get_filings(form="10-K")
        filing = filings[0]
        # edgartools v5.x: filing.xbrl() → XBRL instance
        xbrl = filing.xbrl()
        assert xbrl is not None, "Expected an XBRL object but got None"


# ── EFTS live tests ───────────────────────────────────────────────────────────

@pytest.mark.network
class TestEftsLive:

    def test_efts_response_schema(self, bot):
        """EFTS: query returns JSON with hits / total / adsh structure."""
        time.sleep(1)  # SEC courtesy
        from_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        params = bot._build_fts_query("annual report", from_date)
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params,
            headers={"User-Agent": _UA},
            timeout=15,
        )
        assert resp.status_code == 200
        payload = resp.json()
        # Top-level schema
        assert "hits" in payload
        hits_obj = payload["hits"]
        assert "total" in hits_obj
        assert "hits" in hits_obj
        # At least one hit must carry _source with adsh
        hits_list = hits_obj["hits"]
        assert len(hits_list) > 0, "Expected at least one EFTS hit"
        assert "_source" in hits_list[0]
        assert "adsh" in hits_list[0]["_source"]

    def test_parse_fts_hits_on_live_response(self, bot):
        """_parse_fts_hits correctly structures a live EFTS payload."""
        time.sleep(1)  # SEC courtesy
        from_date = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")
        params = bot._build_fts_query("revenue", from_date)
        resp = requests.get(
            "https://efts.sec.gov/LATEST/search-index",
            params=params,
            headers={"User-Agent": _UA},
            timeout=15,
        )
        assert resp.status_code == 200
        parsed = bot._parse_fts_hits(resp.json())
        assert isinstance(parsed, list)
        if parsed:
            h = parsed[0]
            assert "accession" in h
            assert "form" in h
            assert "date" in h
            assert "url" in h
            assert h["url"].startswith("https://")


# ── Stooq live tests ──────────────────────────────────────────────────────────

@pytest.mark.network
class TestStooqLive:

    def test_aapl_price_positive(self, bot):
        """fetch_last_close returns a positive float for AAPL."""
        price = bot.fetch_last_close("AAPL")
        assert price is not None, "Expected a price but got None for AAPL"
        assert isinstance(price, float)
        assert price > 0.0

    def test_fake_ticker_returns_none(self, bot):
        """fetch_last_close returns None for a delisted/unknown ticker (n/a path)."""
        price = bot.fetch_last_close("ZZZXXX_FAKE_9999")
        assert price is None, f"Expected None for fake ticker but got {price!r}"
