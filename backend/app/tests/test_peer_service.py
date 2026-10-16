"""Peer-group helpers: one peer per master fund."""
import pytest

from app.services.peer_service import master_key, normalise_master_name


@pytest.mark.parametrize("a,b", [
    ("JPMorgan Funds - US Growth Fund, Class I (acc) - USD", "JPMorgan Funds – US Growth Fund A2 USD"),
    ("Goldman Sachs India Equity Portfolio Class I Shares (Acc.)", "Goldman Sachs India Equity Portfolio"),
    ("Janus Henderson Global Life Sciences Fund I Acc", "JANUS HENDERSON GLOBAL LIFE SCIENCES FUND"),
    ("กองทุน PIMCO GIS Income Fund, Class INST USD Acc", "PIMCO GIS Income Fund"),
    ("Baillie Gifford Worldwide Discovery Fund Class B USD Acc", "Baillie Gifford Worldwide Discovery Fund"),
])
def test_same_master_written_differently_normalises_equal(a, b):
    assert normalise_master_name(a) == normalise_master_name(b)


def test_different_masters_stay_different():
    assert normalise_master_name("JPMorgan Funds - US Growth Fund") != \
        normalise_master_name("JPMorgan Funds - US Value Fund")
    assert normalise_master_name("iShares MSCI ACWI ETF") != normalise_master_name("iShares MSCI World ETF")


def test_isin_wins_over_name():
    assert master_key("us4642882579", "iShares MSCI ACWI ETF") == ("isin", "US4642882579")
    assert master_key(None, "iShares MSCI ACWI ETF") == ("name", "ISHARES MSCI ACWI ETF")
    assert master_key(None, None) is None
