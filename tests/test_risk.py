"""Unit tests for RiskManager — no Kalshi credentials required."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.risk import RiskManager


def _fresh_risk(**overrides):
    r = RiskManager()
    for k, v in overrides.items():
        setattr(r, k, v)
    return r


def _market(liquidity=1000, spread=0.02):
    return {"liquidity": liquidity, "spread": spread}


def test_allows_entry_when_all_clear():
    r = _fresh_risk()
    ok, reason = r.can_open(_market(), [], balance=50.0)
    assert ok, reason


def test_blocks_on_low_liquidity():
    r = _fresh_risk(min_liquidity=500)
    ok, reason = r.can_open(_market(liquidity=100), [], balance=50.0)
    assert not ok
    assert "liquidity" in reason


def test_blocks_on_wide_spread():
    r = _fresh_risk(max_spread=0.04)
    ok, reason = r.can_open(_market(spread=0.08), [], balance=50.0)
    assert not ok
    assert "spread" in reason


def test_blocks_at_position_cap():
    r = _fresh_risk(max_positions=3)
    # Legacy-shape position fixtures still parse via the fallback branch
    fake_positions = [{"size": 5, "avgPrice": 0.5}] * 3
    ok, reason = r.can_open(_market(), fake_positions, balance=50.0)
    assert not ok
    assert "cap" in reason


def test_blocks_on_max_exposure():
    r = _fresh_risk(max_exposure=10.0)
    # market_exposure is in cents; 1500c = $15 > $10 cap
    fake_positions = [{"market_exposure": 1500}]
    ok, reason = r.can_open(_market(), fake_positions, balance=50.0)
    assert not ok
    assert "exposure" in reason


def test_size_order_returns_contracts():
    r = _fresh_risk(max_position=10.0, max_exposure=100.0, min_order=1)
    # $10 cap / $0.50 per contract = 20 contracts (cap-bound), then
    # capped further by balance*0.8 = $40 → still 20.
    size = r.size_order(balance=200.0, open_positions=[], price=0.5)
    assert isinstance(size, int)
    assert size == 20


def test_size_order_respects_max_position():
    r = _fresh_risk(max_position=10.0, max_exposure=100.0, min_order=1)
    # $10 / $0.25 = 40 contracts
    size = r.size_order(balance=200.0, open_positions=[], price=0.25)
    assert size == 40


def test_size_order_zero_when_balance_too_low():
    r = _fresh_risk(min_order=5)
    # balance*0.8 = $2.40; at $0.50 → 4 contracts < min_order=5
    size = r.size_order(balance=3.0, open_positions=[], price=0.5)
    assert size == 0
