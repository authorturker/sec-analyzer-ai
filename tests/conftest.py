"""
Shared pytest fixtures and import-time stubs.

The bot.py module imports `config` (user secrets) and `edgar` (third-party SDK)
at module load. Tests stub both so the suite runs without real credentials or
network access.
"""
import sys
import types
from pathlib import Path

# Make the project root importable so `import bot` works.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _install_stubs():
    """Install fake `config` and `edgar` modules before bot.py is imported."""
    if "config" not in sys.modules:
        fake_cfg = types.ModuleType("config")
        fake_cfg.EDGAR_IDENTITY     = "Test User test@example.com"
        fake_cfg.OPENROUTER_API_KEY = "test-key"
        fake_cfg.TELEGRAM_BOT_TOKEN = "test-token"
        fake_cfg.TELEGRAM_CHAT_ID   = "0"
        sys.modules["config"] = fake_cfg

    if "edgar" not in sys.modules:
        fake_edgar = types.ModuleType("edgar")
        fake_edgar.Company      = lambda *a, **k: None
        fake_edgar.set_identity = lambda *a, **k: None
        sys.modules["edgar"] = fake_edgar


_install_stubs()


import pytest
import bot as _bot


@pytest.fixture
def bot():
    """Yield the bot module and reset transient state after each test."""
    # Reset language cache so tests are independent
    _bot._current_lang = None
    _bot._lang_cache.clear()
    # Reset _raw_filings and _status to known state
    with _bot._raw_filings_lock:
        _bot._raw_filings.clear()
    with _bot._status_lock:
        _bot._status.update({
            "started":        _bot.datetime.now().isoformat(),
            "last_update":    None,
            "tg_errors":      0,
            "or_errors":      0,
            "last_scan":      None,
            "last_alarm":     None,
            "total_analyzed": 0,
        })
    yield _bot


# ── Network opt-in mechanism ─────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--network",
        action="store_true",
        default=False,
        help="Run live network tests against real endpoints (EDGAR, EFTS, Stooq).",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: live endpoint tests; run with --network flag",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--network"):
        skip_net = pytest.mark.skip(reason="needs --network flag")
        for item in items:
            if item.get_closest_marker("network"):
                item.add_marker(skip_net)
