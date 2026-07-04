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
│   ├── data_pipeline/        Fetch, enrich, technical indicators
│   ├── scoring/              V2a scoring formula
│   └── validation/           Hit analysis
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
