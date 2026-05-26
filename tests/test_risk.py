"""Unit tests for RiskManager — no Polymarket credentials required."""
import sys
import os
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
    fake_positions = [{"size": 5, "avgPrice": 0.5}] * 3
    ok, reason = r.can_open(_market(), fake_positions, balance=50.0)
    assert not ok
    assert "cap" in reason


def test_blocks_on_max_exposure():
    r = _fresh_risk(max_exposure=10.0)
    # 5 positions × size=5 × avgPrice=0.5 = 12.5 > 10
    fake_positions = [{"size": 5, "avgPrice": 0.5}] * 5
    ok, reason = r.can_open(_market(), fake_positions, balance=50.0)
    assert not ok
    assert "exposure" in reason


def test_size_order_respects_cap():
    r = _fresh_risk(max_position=10.0, max_exposure=100.0)
    size = r.size_order(balance=200.0, open_positions=[], price=0.5)
    assert size <= r.max_position


def test_size_order_zero_when_balance_too_low():
    r = _fresh_risk(min_order=5.0)
    size = r.size_order(balance=3.0, open_positions=[], price=0.5)
    assert size == 0.0
