# Indian Stock Scrip Scoring — Project Context

## Architecture Overview
A modular, configurable data pipeline that processes Indian stock market data through 11 stages. Each stage is independently developed, tested, and validated. The pipeline is orchestrated by a central runner that enforces stage ordering.

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
| 5 | Scoring component #1 (Delivery) | CSV output, intuition check |
| 6 | Remaining components | Each one improves vs previous |
| 7 | Backtesting | Score deciles → forward returns |

## Pipeline Stages

| Stage | Description |
|---|---|---|
| Fetch | Download BhavCopy CSVs + MTO.DAT files from NSE, parse into stage (OHLCV) + delivery (qty, pct) |
| Equity Master | Download EQ_MAST.csv → equity_master table (sector, industry) |
| Enrich | Join stage + delivery, compute rolling volume/delivery avgs → daily table + enriched delivery table |
| Technical | SMA/EMA, crossovers, trend stage → technical table |
| Price Level | 252/126/20-day rolling highs/lows, pivot points → price_level table |
| Momentum | RSI, MACD, Stoch, MFI, ADX, CCI, Williams %R → momentum table |
| Volatility | ATR, Bollinger Bands, Keltner Channels, historical vol → volatility table |
| Averages | Rolling means (close, volume, RSI, delivery%, volatility) at 21/63/126/252/756 windows → averages table |
| Derivatives | NSE FO UDiFF futures data via daily-reports API → futures_data table (basis, OI change) |
| Score | V2a formula (13 components), delivery value filter, sector-diversified top 20 → scoring_result + scoring_picks |
| Hits | Forward-return validation → predicted_stock table |

## Source Conventions
- `src/data_pipeline/` — fetcher.py, enricher.py, technical.py, price_level.py, momentum.py, volatility.py, averages.py, derivatives.py
- `src/scoring/` — scorer.py
- `src/validation/` — hits_analyzer.py, run_hits.py
- `db/` — connection.py
- `config/` — __init__.py, scoring.yaml
- `data/` — bhavcopy_nse/, delivery_nse/, eq_mast.csv
- `tests/` — Mirror of src/ structure

## Code Conventions
- Python 3.11+, type hints required for all function signatures
- Docstrings: Google style
- Config-driven: all tunable parameters in `config/scoring.yaml`
- All pipeline modules share a logger via `get_logger('runner')`
- `INSERT OR REPLACE` everywhere — idempotent; `executemany(500 rows)` for batch inserts
- pandas-ta for all technical indicators; `_safe_rnd()` wrapper for None handling
- `np.where` with Series: convert `.to_numpy()` first, then `.astype(float)`
- All numeric values rounded to 2 decimal places at creation

## Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate (Windows)
.venv\Scripts\activate

# Activate (macOS/Linux)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
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

The runner supports these stages in order: `fetch`, `equity_master`, `enrich`, `technical`, `price_level`, `momentum`, `volatility`, `averages`, `derivatives`, `score`, `hits`.

## Quick Validation — Single Symbol, 3 Months

To validate the pipeline with 1 symbol before scaling:

```bash
python -m src.runner --stages fetch,enrich,technical,score --days-back 125
# Then manually verify RELIANCE OHLCV against NSE website / TradingView
```
