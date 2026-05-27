"""Unit tests for the market scanner (no Kalshi credentials needed)."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from research.scanner import _score_market, MarketScanner


def _make_market(yes_price=0.5, volume=10000, liquidity=1000, spread=0.02):
    return {
        "ticker":       "TEST-MARKET",
        "event_ticker": "TEST-EVENT",
        "question":     "Test?",
        "yes_price":    yes_price,
        "no_price":     1 - yes_price,
        "spread":       spread,
        "volume":       volume,
        "liquidity":    liquidity,
        "active":       True,
        "closed":       False,
        "status":       "active",
        "close_time":   None,
    }


def test_high_volume_high_score():
    m = _make_market(yes_price=0.5, volume=50000, liquidity=2000, spread=0.01)
    score = _score_market(m)
    assert score >= 50, f"Expected >=50 for great market, got {score}"


def test_low_volume_low_score():
    m = _make_market(yes_price=0.5, volume=10, liquidity=50, spread=0.10)
    score = _score_market(m)
    assert score < 30, f"Expected <30 for thin market, got {score}"


def test_extreme_price_penalised():
    extreme  = _make_market(yes_price=0.99, volume=100000,
                             liquidity=5000, spread=0.01)
    mid      = _make_market(yes_price=0.50, volume=100000,
                             liquidity=5000, spread=0.01)
    assert _score_market(extreme) < _score_market(mid)


def test_wide_spread_penalised():
    tight = _make_market(yes_price=0.5, spread=0.01)
    wide  = _make_market(yes_price=0.5, spread=0.10)
    assert _score_market(tight) > _score_market(wide)


def test_scanner_get_by_ticker():
    scanner = MarketScanner()
    scanner.results = [
        _make_market() | {"ticker": "AAA", "score": 80},
        _make_market() | {"ticker": "BBB", "score": 60},
    ]
    assert scanner.get("AAA")["score"] == 80
    assert scanner.get("ZZZ") is None
