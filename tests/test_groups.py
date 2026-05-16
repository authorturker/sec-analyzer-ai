"""
Tests for E2 — watchlist groups.
"""
import pytest


@pytest.fixture
def tmp_cfg(tmp_path, bot, monkeypatch):
    monkeypatch.setattr(bot, "CONFIG_FILE", tmp_path / "bot_config.json")
    bot._cfg_cache = None
    yield bot


class TestValidGroupName:
    @pytest.mark.parametrize("name,ok", [
        ("tech",           True),
        ("TECH_2026",      True),
        ("us-stocks",      True),
        ("a",              True),
        ("",               False),
        ("with space",     False),
        ("$dollar",        False),
        ("emoji😀",        False),
        ("x" * 33,         False),
        ("x" * 32,         True),
    ])
    def test_validation(self, bot, name, ok):
        assert bot._valid_group_name(name) is ok


class TestAddGroup:
    def test_usage_when_missing_args(self, tmp_cfg):
        out = tmp_cfg.cmd_addgroup(["/addgroup"])
        assert "Usage" in out or "Kullanım" in out

    def test_creates_and_dedupes(self, tmp_cfg):
        out = tmp_cfg.cmd_addgroup(["/addgroup", "tech", "AAPL", "MSFT", "AAPL"])
        assert "tech" in out
        cfg = tmp_cfg.get_cfg()
        # Stored sorted and unique
        assert cfg["groups"]["tech"] == ["AAPL", "MSFT"]

    def test_uppercases_tickers(self, tmp_cfg):
        tmp_cfg.cmd_addgroup(["/addgroup", "g", "aapl", "msft"])
        assert tmp_cfg.get_cfg()["groups"]["g"] == ["AAPL", "MSFT"]

    def test_invalid_name_rejected(self, tmp_cfg):
        out = tmp_cfg.cmd_addgroup(["/addgroup", "bad name", "AAPL"])
        assert "Invalid" in out or "Geçersiz" in out
        # Group not created
        assert "bad name" not in tmp_cfg.get_cfg()["groups"]

    def test_overwrites_existing_group(self, tmp_cfg):
        tmp_cfg.cmd_addgroup(["/addgroup", "g", "AAPL"])
        tmp_cfg.cmd_addgroup(["/addgroup", "g", "MSFT", "NVDA"])
        # Latest call replaces (sorted)
        assert tmp_cfg.get_cfg()["groups"]["g"] == ["MSFT", "NVDA"]


class TestRemoveGroup:
    def test_usage_when_missing_arg(self, tmp_cfg):
        out = tmp_cfg.cmd_removegroup(["/removegroup"])
        assert "Usage" in out or "Kullanım" in out

    def test_remove_existing(self, tmp_cfg):
        tmp_cfg.cmd_addgroup(["/addgroup", "g", "AAPL"])
        out = tmp_cfg.cmd_removegroup(["/removegroup", "g"])
        assert "g" in out
        assert "g" not in tmp_cfg.get_cfg()["groups"]

    def test_remove_missing(self, tmp_cfg):
        out = tmp_cfg.cmd_removegroup(["/removegroup", "nope"])
        assert "not found" in out.lower() or "bulunamadı" in out.lower()


class TestListGroups:
    def test_empty(self, tmp_cfg):
        out = tmp_cfg.cmd_listgroups()
        assert "no groups" in out.lower() or "tanımlı grup yok" in out.lower()

    def test_lists_alphabetically(self, tmp_cfg):
        tmp_cfg.cmd_addgroup(["/addgroup", "tech", "AAPL", "MSFT"])
        tmp_cfg.cmd_addgroup(["/addgroup", "energy", "XOM", "CVX"])
        out = tmp_cfg.cmd_listgroups()
        # Both groups present
        assert "tech" in out
        assert "energy" in out
        # energy appears before tech (alphabetical)
        assert out.index("energy") < out.index("tech")
