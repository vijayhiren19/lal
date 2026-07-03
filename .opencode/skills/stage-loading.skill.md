# Stage Loading Skill — NSE

## 1. Overview

Stage data loads in **two parts** from NSE:

| Part | Source | Content |
|---|---|---|
| **Bhavcopy (CM)** | `nsearchives.nseindia.com` | OHLCV, total traded quantity & value |
| **MTO (Delivery)** | `archives.nseindia.com` | Delivery quantity & delivery % |

The two files are downloaded independently, parsed, then **merged by symbol** for each trading date. Raw OHLCV data goes into the `stage` table; delivery data goes into the `delivery` table.

### Local File Cache — First Priority

All raw files are stored on disk in designated folders before any database insert. The on-disk cache is the **source of truth** — NSE is the fallback.

```
                 ┌──────────┐
                 │  START   │
                 └────┬─────┘
                      │
              ┌───────┴───────┐
              │ Check disk for │
              │   file exists  │
              └───────┬───────┘
                      │
          ┌───────────┴───────────┐
          │ YES                   │ NO
          ▼                       ▼
   ┌──────────────┐    ┌──────────────────┐
   │ Parse from   │    │ Download from    │
   │ local file   │    │ NSE              │
   └──────┬───────┘    └────────┬─────────┘
          │                     │
          │              ┌──────┴──────┐
          │              │ Save to disk│
          │              └──────┬──────┘
          └──────────┬──────────┘
                     ▼
            ┌────────────────┐
            │ Insert into DB │
            └────────────────┘
```

**Key consequences:**
- If you wipe the database, **re-parse from disk** — do NOT re-download from NSE.
- Failed downloads are retried next run without re-downloading existing files.
- Delete corrupted files manually so the next run re-downloads them.

### Folder Structure

```
{DATA_DIR}/
├── bhavcopy_nse/          ← BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
├── delivery_nse/          ← MTO_{DDMMYYYY}.DAT
└── download_errors.log    ← append-only error log
```

---

## 2. Volume vs Delivery — Core Concept

This distinction is fundamental to the application. All derived analysis (volume_score, delivery trends, accumulation signals) depends on getting this right.

| Term | Source | Meaning |
|---|---|---|
| **`traded_volume`** | Bhavcopy `TOTTRDQTY` | Total shares traded in the session = **intraday + delivery**. This is the gross activity. |
| **`delivery_qty`** | MTO `delivery_qty` | Shares that actually changed ownership (settled via delivery). Intraday trades are squared off and excluded. |
| **`delivery_pct`** | Calculated | `round(delivery_qty / traded_volume * 100, 2)` |

### Interpretation Heuristics

| Condition | Signal |
|---|---|
| High `delivery_pct` + rising price | Accumulation / strong hands buying |
| High `delivery_pct` + falling price | Distribution / strong hands selling |
| Low `delivery_pct` + rising price | Weak rally / speculative / likely to reverse |
| Low `delivery_pct` + falling price | Panic / weak hands selling, no strong buyers |

### Data Quality Notes

- `delivery_qty` can never exceed `traded_volume`. If it does, flag the data point.
- Some symbols may have MTO data but no Bhavcopy (and vice versa). Only merge where both exist.
- MTO data for `EQ` series only. Ignore other series (`BE`, `BL`, `BT`, etc.).

---

## 3. NSE Data Sources

### Bhavcopy (CM — Capital Market)

```
URL:     https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
File:    BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
Storage: {DATA_DIR}/bhavcopy_nse/
Format:  ZIP containing 1 CSV
```

**⚠ NSE-Specific Parsing:**
- ZIP archive containing a single CSV — must extract before reading.
- Column names change between old format (`TCKRSYMB`, `OPNPRIC`, ...) and new format (`SYMBOL`, `OPEN`, ...) — detect and map accordingly.
- Only `SERIES == "EQ"` rows are relevant.
- Other exchanges (BSE) use completely different file formats and column names.

### MTO (Delivery)

```
URL:     https://archives.nseindia.com/archives/equities/mto/MTO_{DDMMYYYY}.DAT
File:    MTO_{DDMMYYYY}.DAT
Storage: {DATA_DIR}/delivery_nse/
Format:  Plain text, comma-separated, fixed-width columns
```

**⚠ NSE-Specific Parsing:**
- Proprietary DAT format — no header row, record-type prefix (`20`).
- Fields: `20,{DATE},{SYMBOL},{SERIES},{MARKET_TYPE},{DELIVERY_QTY},{DELIVERY_PCT}`
- Filter `SERIES == "EQ"` only.
- `{DATE}` is `DD-MON-YYYY` format (e.g., `25-JUN-2026`).
- Not all Bhavcopy symbols appear in MTO — missing symbols get zero delivery.

---

## 4. Session & Headers

NSE blocks automated requests aggressively. Use the following approach:

### Headers
```python
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
```

### Session Initialization
```python
session = requests.Session()
session.headers.update(NSE_HEADERS)
session.get("https://www.nseindia.com", timeout=10)  # warm-up
```

- Always use a single `requests.Session()` for all downloads within a run.
- Set `timeout=30` per request.
- If `status_code != 200`, log the failure and skip (do NOT retry immediately — rate-limiting trigger).

### Error Logging
```python
# Append to {DATA_DIR}/download_errors.log
# Format: {timestamp} | DATE: {date} | FILE: {Bhavcopy|MTO} | ERROR: {reason}
```

---

## 5. Local-First Download Phase — `download_nse_data(start_date, end_date)`

### Algorithm
1. Generate all business days (Mon–Fri) between `start_date` and `end_date`.
2. For each date:
   a. Check if Bhavcopy ZIP already exists on disk.
   b. If YES → skip download, use cached file.
   c. If NO → download from NSE, save to disk.
   d. Repeat for MTO DAT file.
3. Log success/failure per file per date.

### Design Principle
**Local disk is the cache of record.** The code never downloads what it already has. This makes the pipeline:
- **Replayable**: wipe DB, re-run — files are parsed from disk, no network needed.
- **Resumable**: failed mid-way? Re-run skips already-downloaded files.
- **Auditable**: inspect `bhavcopy_nse/` and `delivery_nse/` to see exactly what data was processed.

### Corrupted File Handling
- A corrupted ZIP will fail during `zipfile.ZipFile()` — log error and skip.
- Corrupted files are **not** automatically deleted. User must delete them manually before re-run.
- Rationale: auto-delete would destroy evidence needed to debug the corruption source.

### File Naming
```python
bhav_date_str = dt.strftime("%Y%m%d")       # 20260625
del_date_str  = dt.strftime("%d%m%Y")        # 25062026

bhav_filename = f"BhavCopy_NSE_CM_0_0_0_{bhav_date_str}_F_0000.csv.zip"
del_filename  = f"MTO_{del_date_str}.DAT"
```

---

## 6. Parse & Merge Phase — `process_nse_data(start_date, end_date)`

### 6.1 Bhavcopy CSV Parsing (NSE-Specific)

NSE changes column names periodically. Handle **three formats** in order of detection:

**Old format (pre-2024) — NSE CM file:**
| CSV Column | Maps to |
|---|---|
| `TCKRSYMB` | `SYMBOL` |
| `SCTYSRS` | `SERIES` — filter for `EQ` |
| `OPNPRIC` | `OPEN` |
| `HGHPRIC` | `HIGH` |
| `LWPRIC` | `LOW` |
| `CLSPRIC` | `CLOSE` |
| `PRVSCLSGPRIC` | `PREVCLOSE` |
| `TTLTRADGVOL` | `TOTTRDQTY` |
| `TTLTRFVAL` | `TOTTRDVAL` |

**Standard format (2024-2025) — NSE CM file:**
| CSV Column | Maps to |
|---|---|
| `SYMBOL` | `SYMBOL` |
| `SERIES` | filter for `EQ` |
| `OPEN` | `OPEN` |
| `HIGH` | `HIGH` |
| `LOW` | `LOW` |
| `CLOSE` | `CLOSE` |
| `PREVCLOSE` | `PREVCLOSE` |
| `TOTTRDQTY` | `TOTTRDQTY` |
| `TOTTRDVAL` | `TOTTRDVAL` |

**MF format (post-2025) — NSE Market Feed file:**
Introduced ~mid-2025. All rows have `FinInstrmTp == "STK"`. Filter by `SctySrs == "EQ"`.
| CSV Column | Maps to |
|---|---|
| `TckrSymb` | `SYMBOL` |
| `SctySrs` | `SERIES` — filter for `EQ` |
| `OpnPric` | `OPEN` |
| `HghPric` | `HIGH` |
| `LwPric` | `LOW` |
| `ClsPric` | `CLOSE` |
| `PrvsClsgPric` | `PREVCLOSE` |
| `TtlTradgVol` | `TOTTRDQTY` |
| `TtlTrfVal` | `TOTTRDVAL` |

**Column detection:**
```python
if "TCKRSYMB" in raw.columns:
    # pre-2024 format — map old column names
elif "TckrSymb" in raw.columns:
    # post-2025 MF format — map camelCase column names
else:
    # 2024-2025 standard format — names already match
```

**Precision rule:** All price columns must be `round(value, 2)` after parsing.

### 6.2 MTO DAT Parsing (NSE-Specific)

File format (comma-separated, no header):
```
20,{DATE},{SYMBOL},{SERIES},{MARKET_TYPE},{DELIVERY_QTY},{DELIVERY_PCT}
```

Example row:
```
20,25-JUN-2026,RELIANCE,EQ,0,1234567,45.67
```

**Rules:**
- Line must start with `"20"` (NSE record type code).
- Only process rows where `SERIES == "EQ"`.
- Parse `DELIVERY_QTY` as `int`, `DELIVERY_PCT` as `float` rounded to 2 decimals.
- Return a dict: `{symbol: {"delivery_qty": int, "delivery_pct": float}}`.

**MTO does NOT include every symbol from Bhavcopy.** Symbols missing from MTO get zero delivery values.

### 6.3 Merge Logic

```python
del_data = parse_mto_dat(del_filename)  # dict[symbol] -> {qty, pct}

for _, row in df.iterrows():
    symbol = str(row["SYMBOL"]).strip()
    del_info = del_data.get(symbol, {"delivery_qty": 0, "delivery_pct": 0.0})

    # Build stage row (OHLCV only)
    stage_row = (exchange, trade_date, symbol, open, high, low, close,
                 prev_close, traded_qty, traded_value_cr,
                 day_return_pct, upper_circuit, lower_circuit)

    # Build delivery row
    del_row = (exchange, trade_date, symbol,
               del_info["delivery_qty"], del_info["delivery_pct"])
```

### 6.4 Derived Fields (calculated during merge)

| Field | Formula | Stored In |
|---|---|---|
| `day_return_pct` | `round((close - prev_close) / prev_close * 100, 2)` if prev_close != 0 else `None` | `stage` |
| `upper_circuit_hit` | `1` if `day_return_pct >= 19.5` else `0` | `stage` |
| `lower_circuit_hit` | `1` if `day_return_pct <= -19.5` else `0` | `stage` |

> **Precision standard:** All numeric values must be `round(x, 2)` before storage. See `data-pipeline.skill.md` for the full precision policy.

> **Batch insert pattern:** Use `executemany` with explicit column lists, `INSERT OR REPLACE`, 500–1000 rows per batch. See `data-pipeline.skill.md` §"Batch Processing Pattern" for the full pattern.

### Extensibility — Adding New Columns

When a new column is added to a table:

```python
# 1. Schema migration in init_schema()
cursor.execute("""
    ALTER TABLE stage ADD COLUMN new_field REAL
""")

# 2. INSERT already works because we use explicit column lists —
#    the new column simply isn't included in the VALUES clause
#    until the code is updated to populate it.
```

**Rules:**
- New columns must be nullable or have a DEFAULT value — existing rows won't have data for them.
- Migration SQL must use `ALTER TABLE ... ADD COLUMN ...` with no constraints that fail on existing data.
- Never remove or rename columns — deprecated columns are left in place and simply not populated.

---

## 7. Target Tables

Refer to `schema.md` for the full table definitions (`stage` and `delivery`).

**Why two tables instead of one merged table?**
- `delivery` data is optional per symbol (not all symbols have MTO data).
- Keeps the raw OHLCV layer clean and replayable.
- Downstream enrichment stages join on `(exchange, trade_date, symbol)` as needed.

---

## 8. Orchestration — `load_historical_data(start_date, end_date)`

```python
def load_historical_data(start_date=None, end_date=None):
    1. Resolve default dates (default: last 750 trading days)
    2. Initialize DB schema (create tables if not exist)
    3. Download all missing files  (disk-first)
    4. Process all downloaded files into DB
```

### Default Behavior
- If `end_date` is `None`, use today.
- If `start_date` is `None`, use `end_date - 750 days` (~3 calendar years).

### Validation Mode — Start Small
For quick iteration during validation, override the default range:

```bash
# Load only 6 months for testing
python -m src.data_pipeline.fetcher --days-back 125
```

The `--days-back` flag converts to trading days and sets `start_date` accordingly. Start with a single symbol (RELIANCE) and 3 months before scaling up to the full universe and date range.

### Idempotency
- Uses `INSERT OR REPLACE` so re-running is safe.
- Only downloads files that don't exist on disk.
- To re-process from disk without re-downloading: delete DB rows, re-run `process_nse_data()`.

---

## 9. Error Handling Summary

| Scenario | Action | Logged To |
|---|---|---|
| Bhavcopy URL returns 404 | Skip date, log failure | `download_errors.log` |
| MTO URL returns 404 | Skip delivery for that date, log failure | `download_errors.log` |
| Bhavcopy ZIP is corrupted | Log error, skip date — user must delete file manually | `logger.error` |
| MTO DAT is missing/empty | Delivery = {0, 0} for all symbols | `logger.warning` |
| Delivery_qty > traded_volume | Log warning, clamp delivery_qty to traded_volume | `logger.warning` |
| Connection timeout | Log failure, skip | `download_errors.log` |
| CSV has unknown column names | Log error with column list, skip date | `logger.error` |
| MTO DAT parse error on a line | Log warning, skip that line, continue | `logger.warning` |

### Replay from Disk (after DB wipe)
```python
# Step 1: Init schema (creates empty tables)
init_schema()

# Step 2: Process only — no download needed
process_nse_data(start_date, end_date)
```
The orchestrator should support a `--from-disk-only` flag that skips the download phase entirely.

---

## 10. Quick Reference — File Mapping

```
config/__init__.py              — DB_PATH, DATA_DIR
db/connection.py                — init_schema(), get_connection()
utils/logger.py                 — setup_logger(), get_logger()
utils/date_utils.py             — today_str()
src/data_pipeline/              — Stage loader module(s) belong here
{DATA_DIR}/bhavcopy_nse/        — Cached Bhavcopy ZIP files
{DATA_DIR}/delivery_nse/        — Cached MTO DAT files
{DATA_DIR}/download_errors.log  — Download error log
```
