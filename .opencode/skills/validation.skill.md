# Validation Skill

## Purpose
Establish validation gates, data quality checks, and orchestration patterns to ensure pipeline integrity.

---

## Execution Model

Validation runs as **batch checks** after each pipeline stage completes.

| Property | Value |
|---|---|
| Trigger | Orchestrator calls `validate(stage, data)` after each stage |
| Frequency | Every pipeline run |
| Read pattern | Heavy — queries the full output table of the stage just completed |
| Write pattern | Report generation only (no DB writes) — results go to `data/reports/` |
| Gate failure | Logged as warning — does NOT block the pipeline (advisory gates) |

```python
def run_pipeline(stages: list[str]) -> PipelineResult:
    data = None
    for stage in stages:
        logger.info(f"Running stage: {stage}")
        data = execute_stage(stage, data)
        if stage in VALIDATION_GATES:
            report = validate(stage, data)
            if not report.passed:
                logger.warning(f"Gate {stage} failed: {report.summary()}")
            save_report(stage, report)
    return data
```

## Extensibility — Adding a New Gate

Create a new module in `src/validation/gates/` implementing the `Gate` interface. The orchestrator auto-discovers all gates and runs them for their configured stage.

```
src/validation/gates/
├── __init__.py            ← auto-discovers all gates
├── base.py                ← abstract base class
├── data_freshness.py
├── enrichment_completeness.py
├── technical_sanity.py
├── score_distribution.py
└── heuristic_validation.py  ← new gate = new file
```

## Validation Gates

### Gate 1: Data Freshness (after Fetch)
- All expected symbols present
- No missing trading days for active scrips
- OHLCV ranges within expected bounds
- Timestamps are business days

### Gate 2: Enrichment Completeness (after Enrich)
- No nulls in critical fields (sector, market cap)
- Categorization sanity (e.g., IT sector != Banking)
- Market cap tiers consistent

### Gate 3: Technical Sanity (after Technical)
- RSI in [0, 100]
- MACD values finite and reasonable
- Bollinger Bands: lower < middle < upper
- No NaN propagation beyond expected windows
- All values rounded to 2 decimals (fail if any column has >2 decimal places)

### Gate 4: Score Distribution (after Score)
- Scores roughly normally distributed (not all 0-10 or 90-100)
- No NaN scores
- Historical rank stability (prevents flip-flopping)
- Factor scores non-negative and bounded

### Gate 5: Scoring Heuristic Validation (after Score)
**Note: All 6 heuristics are disabled in the V2a formula.** This gate exists as a reference but heuristics are no longer applied. See `scoring.skill.md` for details on what was rejected and why.

The V2a formula uses 5 direct components (contrarian, delivery pct, delivery trend, qty surge, liquidity) with no heuristic penalties.

**Alert on anomaly:** If any check fails, log a warning with the deviation details. Do not block the pipeline — these are advisory gates.

---

## Orchestration
- Orchestrator: `src/validation/orchestrator.py`
- Validators: `src/validation/gates/` (one per gate)
- Config: `config/validation.yaml` (thresholds, tolerances)

### Pipeline Runner
```python
def run_pipeline(stages: list[str]) -> PipelineResult:
    data = None
    for stage in stages:
        data = execute_stage(stage, data)
        if stage in VALIDATION_GATES:
            validate(stage, data)
    return data
```

---

## Quality Reporting
- Generate validation report per run: `data/reports/run_{timestamp}.json`
- Track: pass/fail status, row counts, null percentages, outlier counts
- Alert on gate failures with specific error context
- Historical validation trends in `data/reports/history/`

---

## Testing
- Unit tests for each validator in `tests/validation/`
- Integration tests running mini pipeline with known-good data in `tests/integration/`
- Fixtures: sample DataFrames with known good/bad data patterns
- Heuristic validation tests: synthetic data that triggers each heuristic rule, verify score adjustments are within expected ranges
