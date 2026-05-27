# Kalshi Trader

Automated trading engine for [Kalshi](https://kalshi.com) — CFTC-regulated
event-prediction markets settling in USD.

Two strategies run in parallel:

- **Market making** (`strategies/market_maker.py`) — posts both-sided quotes on
  the YES side of selected markets and earns the bid/ask spread.
- **Directional** (`strategies/directional.py`) — opens a YES or NO position
  when the live mid diverges from a pluggable fair-value estimate, then exits
  on take-profit or stop-loss.

A scheduler picks tradeable markets every 30 minutes, the market makers
re-quote every 30 seconds, and a daily snapshot of balance + P&L is written
to SQLite.

## Architecture

```
main.py                   ← scheduler: scan, MM reprice, daily snapshot
├── core/
│   ├── auth.py           ← RSA-PSS-SHA256 request signer
│   ├── client.py         ← Kalshi REST client (markets + portfolio)
│   ├── markets.py        ← public-market helpers (active markets, summary)
│   ├── orderbook.py      ← real-time WS book (`orderbook_delta` channel)
│   └── risk.py           ← per-market + total-exposure guards
├── research/
│   └── scanner.py        ← finds tradeable markets and scores them
├── strategies/
│   ├── market_maker.py
│   └── directional.py
├── models/               ← SQLite persistence (SQLAlchemy)
└── tests/                ← pytest unit tests (no creds needed)
```

## Setup

```bash
git clone https://github.com/RobertoPujol/kalshi.git
cd kalshi
./scripts/setup_venv.sh    # creates venv/ and copies .env.example → .env
```

Then edit `.env` with:

- **`KALSHI_API_KEY_ID`** — the UUID shown in your Kalshi dashboard
  under *Account → Profile → API Keys*.
- **`KALSHI_PRIVATE_KEY_PATH`** — path to the PEM-encoded RSA private key
  Kalshi generated when you created the API key.  Keep it `chmod 600`.
- `KALSHI_ENV=prod` (default) or `demo` for the sandbox.
- `DRY_RUN=true` while you're shaking it out — orders are logged, not sent.

## Running

```bash
venv/bin/python main.py
```

Logs stream to stderr and to `logs/kalshitrader.log` (rotated at 10 MB).
Press Ctrl-C to shut down cleanly — all open quotes are cancelled.

## Tests

```bash
venv/bin/pytest -q
```

Unit tests cover risk and scanner logic.  They don't hit the network and
don't need credentials.

## Notes

- **Prices** are dollars per contract (0.01–0.99) — same range as
  probabilities, so all the existing strategy math carries over cleanly.
  Kalshi's wire format uses integer cents; `core/markets.py` normalises
  to dollars on the way in, and `core/client.py` posts the modern
  `yes_price_dollars` / `no_price_dollars` order fields.
- **Sizes** are in **contracts** (each contract = $1 at settlement).
  Risk caps are stated in USD; `risk.size_order()` floor-divides to
  whole contracts.
- **Orderbook**: Kalshi publishes bids only, on both YES and NO sides.
  We synthesise the YES ask as `1 - best_no_bid` in `core/orderbook.py`.
- **Fees**: Kalshi currently quotes 0% trading fees on most markets; the
  strategies don't bake in a fee adjustment.  Sanity-check on your event
  before going live.
