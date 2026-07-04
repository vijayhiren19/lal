---
description: Run the data pipeline for a date/range. Supports "26-06-2026", "last 15 days", "last 2 months", or empty for last trading day.
agent: developer
---

Parse the user's date input (after "/fill") and execute the data pipeline.

User input: "$ARGUMENTS"

Supported formats:
- Single date: "26-06-2026" or "15-07-26" (DD-MM-YYYY or DD-MM-YY)
- Relative: "last N days" or "last N months"
- "last trading day", "today", "yesterday"
- Empty (defaults to last trading day)

The helper script will be created at `scripts/fill.py` to resolve the date range. It outputs START=YYYY-MM-DD and END=YYYY-MM-DD.

Then run the pipeline with those dates. Use the FULL pipeline for a single date or last trading day:

```
python -m src.runner --stages all --start-date {START} --end-date {END}
```

For date ranges ("last 15 days", "last 2 months"), skip the `fetch` stage if files are already cached and run from `enrich`:

```
python -m src.runner --stages enrich,technical,price_level,momentum,volatility,averages,derivatives,score --start-date {START} --end-date {END}
```

After the pipeline completes, print a summary:
- Date range processed
- Stages that ran
- Any errors or warnings from the output
