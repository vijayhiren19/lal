# Phase 1 — Stock Scrip Scoring System

A Python + SQLite application that downloads Indian stock market data from NSE, computes technical indicators, and scores stocks using a multi-factor model to identify top picks.

---

## How This Project Works

**Markdown files are the source of truth.** Any change to the application starts with updating the markdown documentation, then implementing the code.

### Documentation Structure

| File | Purpose | When to Edit |
|------|---------|--------------|
| `AGENTS.md` | Complete specification (pipeline, DB schema, scoring formula, CLI) | When adding/modifying features |
| `.opencode/skills/*.skill.md` | Implementation patterns and conventions | When changing coding patterns |
| `README.md` | This file — project overview | Rarely |

### Workflow

1. **Edit markdown first** — Update `AGENTS.md` or relevant skill file with new requirements
2. **Ask orchestrator to implement** — The agent reads the updated markdown and generates code
3. **Verify** — Run tests/lint to confirm changes match the spec

---

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run full pipeline (fetch → enrich → technical → score)
python -m src.runner --stages all --days-back 125

# Run scoring only
python -m src.runner --stages score

# Compute hit analysis
python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-06-04

# Start Flask API server (http://127.0.0.1:5000)
python -m src.api.app

# With custom host/port
python -m src.api.app --host 0.0.0.0 --port 5001
```

---

## Project Structure

```
phase1/
├── AGENTS.md                 ← Full specification
├── config/
│   ├── __init__.py           ROOT_DIR, DATA_DIR, DB_PATH
│   └── scoring.yaml          Tunable parameters
├── db/
│   └── connection.py         SQLite schema, init_schema()
├── data/                     Cached CSVs (bhavcopy, MTO, eq_mast.csv)
├── src/
│   ├── runner.py             CLI orchestrator
│   ├── api/                  Flask REST API (stocks, pipeline, meta endpoints)
│   ├── data_pipeline/        Fetch, enrich, technical indicators
│   ├── scoring/              V2a scoring formula
│   └── validation/           Hit analysis
config/
│   ├── columns.yaml          Column registry for the API (97 columns)
├── build_fno_membership.py   F&O membership data
├── build_index_history.py    Index/sector membership
├── build_shareholding.py     Shareholding data
├── build_equity_master.py    Build equity master CSV
└── requirements.txt          pyyaml, pandas, pandas-ta, requests, numpy
```

---

## Pipeline Stages

```
fetch → equity_master → enrich → technical → price_level → momentum
  → volatility → averages → derivatives → score → hits
```

Each stage reads from SQLite, computes, and batch-inserts results. Full pipeline takes ~21 minutes for 18 months of data.

---

## Key Commands

```bash
# Full pipeline
python -m src.runner --stages all --start-date 2025-01-01 --end-date 2026-06-30

# Specific stages
python -m src.runner --stages fetch,enrich,technical

# Refresh membership data
python build_fno_membership.py
python build_index_history.py
python build_shareholding.py

# Query top picks
python -c "from db.connection import get_connection; import pandas as pd; print(pd.read_sql('SELECT * FROM scoring_picks WHERE trade_date=\"2026-06-25\" ORDER BY rank', get_connection()))"
```

---

## Flask REST API

The API exposes the pipeline and scoring data via HTTP. Start the dev server:

```bash
python -m src.api.app --host 0.0.0.0 --port=5000
```

Endpoints (all under `/api/v1`):

| Method | Path | Description |
|--------|------|-------------|
| GET | `/stocks?date=...` | Flexible stock data query with column/filter/pagination |
| GET | `/stocks/history?symbol=...` | Full history for a symbol |
| GET | `/stocks/detail?symbol=...` | Single stock snapshot |
| GET | `/stocks/search?q=...` | Fuzzy symbol/sector search |
| GET | `/picks?date=...&limit=20` | Top scoring picks |
| POST | `/pipeline/run` | Trigger pipeline (async) |
| GET | `/pipeline/status` | Check pipeline job status |
| GET | `/pipeline/stages` | List 11 available pipeline stages |
| GET | `/pipeline/jobs` | List recent pipeline jobs |
| POST | `/pipeline/build/fno-membership` | Rebuild F&O membership (async) |
| POST | `/pipeline/build/index-history` | Rebuild index membership (async) |
| POST | `/pipeline/build/shareholding` | Rebuild shareholding table (async) |
| POST | `/pipeline/build/equity-master` | Rebuild equity master CSV (async) |
| POST | `/hits/compute` | Compute hit analysis (async) |
| GET | `/hits/analyze` | Hit rate summary JSON |
| GET | `/hits/detail` | Individual pick details |
| GET | `/columns` | Column registry (97 columns, 10 groups) |
| GET | `/health` | DB stats, schema version, last data date |
| GET | `/dates` | Available date ranges per table |

Example queries:

```bash
# Top 20 picks for a date (CSV export)
curl "http://127.0.0.1:5000/api/v1/picks?date=2026-06-25&format=csv" -o picks.csv

# Stock universe with specific columns
curl "http://127.0.0.1:5000/api/v1/stocks?date=2026-06-25&columns=symbol,close_price,overall_score,rank&order_by=rank&order_dir=asc&limit=10"

# Trigger pipeline
curl -X POST "http://127.0.0.1:5000/api/v1/pipeline/run" -H "Content-Type: application/json" -d "{\"days_back\": 1}"

# Compute hit analysis
curl -X POST "http://127.0.0.1:5000/api/v1/hits/compute" -H "Content-Type: application/json" -d "{\"start_date\":\"2025-01-01\",\"end_date\":\"2026-07-03\"}"

# Rebuild F&O membership
curl -X POST "http://127.0.0.1:5000/api/v1/pipeline/build/fno-membership"
```

### Deployment Flow (Zero to Data)

Tested end-to-end. From a bare database to populated picks via API only:

```bash
# 1. Start server (auto-creates all 18 tables + stock_universe view)
python -m src.api.app

# 2. Trigger full pipeline (async — returns job_id immediately)
curl -X POST "http://127.0.0.1:5000/api/v1/pipeline/run" \
  -H "Content-Type: application/json" \
  -d '{"stages":"all","start_date":"2026-06-20","end_date":"2026-06-30"}'

# 3. Poll until status="completed"
curl "http://127.0.0.1:5000/api/v1/pipeline/status"

# 4. Verify data
curl "http://127.0.0.1:5000/api/v1/health"
curl "http://127.0.0.1:5000/api/v1/picks?date=2026-06-30"
```

Result: 11 stages, ~2m40s for 6 trading days. All 18 tables populated.

## How to Add a Feature

1. **Update `AGENTS.md`** — Add specification for new feature
2. **Update skill files** — Add implementation patterns if needed
3. **Ask agent to implement** — Agent reads markdown and generates code
4. **Run tests** — Verify implementation matches spec

Example: To add a new technical indicator:
- Add column definition to `AGENTS.md` technical table
- Add pandas-ta call to `data-pipeline.skill.md`
- Agent implements in `src/data_pipeline/technical.py`
- Schema auto-updates via `init_schema()` migration
