# kalshi — Claude Context

KalshiTrader — automated trading engine for Kalshi event-prediction markets.
Scheduler-driven (APScheduler) Python process; no web server is run by default.

## Stack
- Python 3 (venv-based), single long-running process via `main.py`
- APScheduler (background scheduler) for the trading/scan/snapshot loops
- Kalshi REST + WebSocket API; RSA-PSS-SHA256 request signing (`core/auth.py`)
- SQLAlchemy + SQLite (`kalshitrader.db`) for persistence
- scikit-learn / numpy ML fair-value model; nltk for news sentiment
- requests, websockets, python-dotenv, loguru, pandas, polars
- fastapi/uvicorn listed in requirements but unused (kept for future dashboard work)

## Dev Commands
- Setup: `./scripts/setup_venv.sh` (creates `venv/`, installs deps, copies `.env.example` → `.env`)
- Configure creds interactively: `./scripts/init_env.sh`
- Run (live): `venv/bin/python main.py`
- Run (paper): `DRY_RUN=true venv/bin/python main.py`
- Test: `venv/bin/pytest -q` (covers risk + scanner; no network/creds needed)

## Architecture
- `main.py` — entrypoint + APScheduler jobs:
  - trading cycle every 30s (market-maker reprice, directional, mean reversion)
  - market scan every 30min, hourly + daily-midnight snapshots
  - dashboard sync every 5min, strategy discovery + ML retrain daily 01:00 UTC
  - circuit breaker halts all trading on session-loss limit breach
- `core/` — `auth.py` (signer), `client.py` (REST client + `DRY_RUN`),
  `markets.py`, `orderbook.py` (WS book), `risk.py`, `fair_value_model.py` (ML),
  `sentiment.py` / `sentiment_fair_value.py`, `adaptive_params.py`,
  `dashboard_sync.py` (pushes closed trades to a Cloudflare Worker dashboard)
- `strategies/` — `market_maker.py`, `directional.py`, `mean_reversion.py`
- `research/` — `scanner.py` (finds/scores markets), `strategy_discovery.py`
- `models/` — `database.py`, `repository.py` (SQLAlchemy/SQLite)
- `utils/logger.py` — loguru logging to stderr + `logs/kalshitrader.log` (rotated at 10 MB)

## Conventions
- Config via `.env` (see `.env.example`); key vars: `KALSHI_API_KEY_ID`,
  `KALSHI_PRIVATE_KEY_PATH`, `KALSHI_ENV` (prod|demo), `DRY_RUN`,
  `MAX_POSITIONS`, `STRATEGY_CONFIG_FILE`, `NEWS_API_KEY`,
  `DASHBOARD_URL` / `DASHBOARD_SECRET` (optional dashboard sync)
- DRY_RUN defaults to false (LIVE) when unset — set `DRY_RUN=true` for paper trading
- Prices are dollars/contract (0.01–0.99); sizes are whole contracts ($1 each)
- Ctrl-C / SIGTERM shuts down cleanly and cancels all open quotes

## Deploy
- No CI / no deploy automation. Runs as a manually-launched local process
  from `~/Projects/kalshi` via `venv/bin/python main.py`.
