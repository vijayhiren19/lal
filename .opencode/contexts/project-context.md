# Indian Stock Scrip Scoring — Project Context

## Architecture Overview
A modular, configurable data pipeline that processes Indian stock market data through 11 stages (9 data pipeline + 2 scoring). Each stage is independently developed, tested, and validated. The pipeline is orchestrated by a central runner that enforces stage ordering.

## Team
| Agent | Focus | Key Files |
|---|---|---|
| Developer | Generates/modifies all pipeline, scoring, and validation code from `AGENTS.md` spec + domain skills | `src/` (all) |
| Tester | Validates pipeline outputs, runs hit analysis, executes tests, checks data quality | `src/validation/`, `tests/` |

## How It Works

```
AGENTS.md (master spec) ──→ Developer Agent ──→ generates/modifies code
       +                            ↑
   domain skills ───────────────────┘
       |
   Tester Agent ──→ validates outputs, runs tests, reports quality
```

Changes to `AGENTS.md` or a skill → Developer re-generates the affected code. Team members get AGENTS.md + .opencode → full codebase.

## Development Roadmap (Validation-First)

| # | Step | Verify Against |
|---|---|---|
| 1 | Stage loading for 1 symbol, 3 months | NSE website OHLCV |
| 2 | RSI + SMA for 1 symbol | TradingView |
| 3 | Full pipeline, all symbols, 3 months | No errors, reasonable values |
| 4 | Scale to full history | Performance OK |
| 5 | Scoring heuristic #1 (Delivery) | CSV output, intuition check |
| 6 | Remaining heuristics | Each one improves vs previous |
| 7 | Backtesting | Score deciles → forward returns |

## Pipeline Stages

| Stage | Description |
|---|---|
| Fetch | Raw data from NSE/BSE (daily OHLCV, corporate actions, indices) |
| Enrich | Add sector, market cap, F&O availability, delivery %, etc. |
| Volume Metrics | Volume rolling averages, ratios, breakouts, volume score |
| Technical | Moving averages, crossovers, RSI, MACD, ATR, Bollinger Bands, trend assessment |
| Price Level | 52-week / 26-week / 4-week price levels, pivot points, breakout flags |
| Momentum | Oscillators, momentum score, decay factor |
| Volatility | ATR, Bollinger Bands, Keltner Channels, historical volatility |
| Averages | Rolling averages over 21d / 63d / 126d / 252d / 756d for key metrics |
| Score | Weighted composite scoring (0-100) with configurable heuristics |
| Validate | Validation gates, data quality checks, score distribution, backtesting |

## Source Conventions
- `src/data_pipeline/` — Fetch, enrich, volume metrics, technical, price level, momentum, volatility, averages stages
- `src/scoring/` — Scoring factors, heuristics, backtesting
- `src/validation/` — Validation gates and orchestration
- `src/common/` — Shared utilities, DB connection, data models
- `tests/` — Mirror of src/ structure
- `data/` — Raw and processed data (gitignored)
- `config/` — Pipeline configuration files (YAML)

## Code Conventions
- Python 3.11+, type hints required for all function signatures
- Docstrings: Google style
- Config-driven: all tunable parameters in `config/*.yaml`
- Async for I/O-bound fetching; synchronous for compute
- Data model contracts in `src/common/models.py` shared across all agents
- Validation gates are non-negotiable checkpoints before downstream consumption

## Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (macOS/Linux)
source .venv/bin/activate

# Install dependencies
pip install pandas requests numpy pyyaml pandas-ta
```

Dependencies are listed in `requirements.txt` at project root.

## Running the Pipeline

```bash
# Run full pipeline (after market close)
python -m src.runner --stages all --days-back 1

# Run a date range
python -m src.runner --stages fetch,enrich,technical --days-back 125

# Skip download, only parse cached files
python -m src.runner --stages fetch --days-back 125 --from-disk-only
```

The runner supports these stages in order: `fetch`, `equity_master`, `enrich`, `volume`, `technical`, `price_level`, `momentum`, `volatility`, `averages`, `score`, `hits`.

## Quick Validation — Single Symbol, 3 Months

To validate the pipeline with 1 symbol before scaling:

```bash
python -m src.runner --stages fetch --days-back 125 --start-date 2026-03-25 --end-date 2026-06-25
# Then manually verify RELIANCE OHLCV against NSE website / TradingView
```
