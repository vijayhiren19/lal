# Data Pipeline Skill

## Purpose
Define patterns, conventions, and best practices for fetching, enriching, and computing technical indicators for Indian stock market data.

---

## Validation-First Approach

Build in this order, validating each step before moving to the next:

1. **Load 3 months of data** for 1 symbol (RELIANCE) — verify OHLCV matches NSE website
2. **Compute RSI(14) and SMA(20)** for that symbol — compare values against TradingView
3. **Scale to all symbols** — run full pipeline on 3 months
4. **Add more data** — increase to 1 year, then full history
5. **Add scoring heuristics** — one at a time, validate each

### Quick Sanity Check

Before wiring up the full pipeline, run this standalone check:

```python
# scripts/sanity_check.py
# 1. Load stage data for RELIANCE
# 2. Compute RSI-14 and SMA-20 using pandas-ta
# 3. Print last 10 rows
# 4. Compare against TradingView manually
```

All stages accept `--days-back N` — start with `--days-back 125` (~6 months).

> **Note:** Volume metrics (rolling volume averages, vol_ratio, vol_breakout_up, vol_trend_5d, volume_score) are computed in the Enrich stage (`enricher.py`), not a separate stage.

---

## Execution Model

The data pipeline is designed for **batch-oriented, on-demand execution**.

| Property | Value |
|---|---|
| Trigger | CLI command (`python -m src.runner --stages technical,momentum`) or scheduled (cron at EOD) |
| Frequency | Typically once per trading day after market close |
| Latency | **Time-consuming is acceptable** — stages may take minutes to hours |
| Read pattern | **Heavy reads** — each stage reads the full `stage` / `daily` tables, processes all symbols in one pass |
| Write pattern | Batch upserts — all symbols processed in bulk, committed per date or per chunk |
| Concurrency | Single-threaded per stage (SQLite doesn't benefit from parallel writes) |
| Data volume | ~2000 symbols × 750 trading days = ~1.5M rows per table |

**Design implications:**
- Always read the full dataset once per stage, never row-by-row.
- Use vectorized pandas operations, not loops.
- Batch INSERT / UPDATE — never one row at a time.
- Log progress every N symbols / dates so long runs are monitorable.
- Heavy reads are fine — the DB is local SQLite, no network overhead.

---

## Library Requirement — pandas-ta

**All technical indicator calculations must use the `pandas-ta` library.** No custom implementation of RSI, MACD, Bollinger Bands, or any other indicator.

```python
import pandas_ta as ta

df["rsi_14"] = ta.rsi(df["close"], length=14).round(2)
df.ta.macd(close="close", fast=12, slow=26, signal=9, append=True)
df.ta.bbands(close="close", length=20, std=2, append=True)
```

Rationale: pandas-ta is well-tested, vectorized, and handles edge cases (e.g., insufficient data, NaN windows). It is the single canonical source for technical calculations.

### Precision Standard
**Every indicator value must be `round(value, 2)` before storage.** pandas-ta outputs high-precision floats — always round at the point of assignment.

### pandas-ta None Handling

`pandas-ta` functions return `None` when there is insufficient data for the requested window (e.g., calling `ta.sma(length=200)` on 50 rows). **Always check for `None` before calling `.round()`:**

```python
def _safe_rnd(result):
    return result.round(2) if result is not None else None

rsi = ta.rsi(close, length=14)
df["rsi_14"] = _safe_rnd(rsi)

# For functions that return a DataFrame (macd, bbands, stoch, kc):
macd_result = ta.macd(close, fast=12, slow=26, signal=9)
if macd_result is not None:
    df["macd"] = macd_result["MACD_12_26_9"].round(2)
else:
    df["macd"] = np.nan
```

**Functions that can return `None`:**
- Single-series: `sma`, `ema`, `rsi`, `mfi`, `cci`, `willr`, `atr`
- DataFrame-series: `macd`, `bbands`, `stoch`, `kc`, `adx`

---

## Fetch Stage
- Sources: NSE (via NSE official API), BSE
- Data to fetch: Daily OHLCV, adjusted close, volume, delivery data, F&O data
- Handle: Rate limiting, session management, retry logic, data gaps (trading holidays)
- Output: Raw files cached on disk in `{DATA_DIR}/bhavcopy_nse/` and `{DATA_DIR}/delivery_nse/`
- Module: `src/data_pipeline/fetcher.py`

### Key Considerations
- Use the local file cache first — download from NSE only when the file doesn't exist on disk
- For NSE-specific download details (URLs, file naming, cache logic, session management, column mappings, and error handling), see `stage-loading.skill.md`

---

## Enrich Stage
- Join stage + delivery, merge index membership, compute volume rolling averages/averages/breakouts/score
- Compute delivery rolling averages (delivery_qty_5d_avg, delivery_qty_20d_avg, delivery_pct_5d_avg, delivery_pct_20d_avg, delivery_pct_trend, vol_spike)
- Output: daily table (OHLCV + volume metrics) + enriched delivery table
- Module: `src/data_pipeline/enricher.py`

---

## Volume Metrics (within Enrich Stage)

Volume metrics are computed inside `enricher.py` as part of the daily table creation.

| Metric | Formula | Purpose |
|---|---|---|
| `vol_5d_avg` | `rolling(5).mean()` on traded_volume | Short-term volume baseline |
| `vol_10d_avg` | `rolling(10).mean()` | Medium-term volume baseline |
| `vol_20d_avg` | `rolling(20).mean()` | Standard volume baseline |
| `vol_ratio` | `traded_volume / vol_20d_avg` | Today's relative volume |
| `vol_breakout_up` | `1` if vol_ratio >= 1.5 AND close > open | Volume breakout signal |
| `vol_trend_5d` | Linear slope of volume over last 5 sessions | Volume direction |
| `volume_score` | Composite (0-100) from vol_ratio, trend, breakout | Overall volume quality |

**Precision:** All values `round(..., 2)` except `vol_breakout_up` (INTEGER) and `traded_volume` (INTEGER).

---

## Technical Stage — Detailed Indicator Formulas

### Approach
1. Read `daily` table for the full symbol universe into a pandas DataFrame.
2. Sort by `(symbol, trade_date)` to ensure correct rolling windows.
3. Group by `symbol` and apply pandas-ta functions per group.
4. Collect results and batch upsert into the target table.

### Moving Averages

| Column | pandas-ta Call | Notes |
|---|---|---|
| `sma_20` | `ta.sma(close, length=20).round(2)` | Simple average of last 20 closes |
| `sma_50` | `ta.sma(close, length=50).round(2)` | Simple average of last 50 closes |
| `sma_100` | `ta.sma(close, length=100).round(2)` | Simple average of last 100 closes |
| `sma_200` | `ta.sma(close, length=200).round(2)` | Simple average of last 200 closes |
| `ema_9` | `ta.ema(close, length=9).round(2)` | Exponential weighting, more weight to recent |
| `ema_20` | `ta.ema(close, length=20).round(2)` | |
| `ema_50` | `ta.ema(close, length=50).round(2)` | |
| `ema_200` | `ta.ema(close, length=200).round(2)` | |

**Derived from MAs:**
| Column | Formula |
|---|---|
| `price_vs_sma20_pct` | `round((close - sma_20) / sma_20 * 100, 2)` |
| `price_vs_sma50_pct` | `round((close - sma_50) / sma_50 * 100, 2)` |
| `price_vs_sma200_pct` | `round((close - sma_200) / sma_200 * 100, 2)` |
| `golden_cross` | `1` if sma_50 crosses above sma_200 (compare current vs previous row) |
| `ema20_gt_ema50` | `1` if ema_20 > ema_50 |
| `price_gt_sma200` | `1` if close > sma_200 |

### Oscillators / Momentum

| Column | pandas-ta Call | Range |
|---|---|---|
| `rsi_14` | `ta.rsi(close, length=14).round(2)` | 0–100 |
| `rsi_9` | `ta.rsi(close, length=9).round(2)` | 0–100 |
| `macd` | `ta.macd(close, fast=12, slow=26, signal=9)` — returns 3 columns: `MACD_12_26_9`, `MACDs_12_26_9`, `MACDh_12_26_9` | unbounded |
| `macd_signal` | same call → `.iloc[:,1]` | |
| `macd_histogram` | same call → `.iloc[:,2]` | |
| `macd_crossover` | `1` if MACD crosses above Signal (current > previous) | 0/1 |
| `stoch_k` | `ta.stoch(high, low, close, k=14, d=3, smooth_k=3).round(2)` — returns `STOCHk_14_3_3`, `STOCHd_14_3_3` | 0–100 |
| `stoch_d` | same call → `.iloc[:,1]` | 0–100 |
| `stoch_signal` | optional: 3-period SMA of stoch_k | 0–100 |
| `mfi_14` | `ta.mfi(high, low, close, volume, length=14).round(2)` | 0–100 |
| `cci_20` | `ta.cci(high, low, close, length=20).round(2)` | -400 to +400 (typical) |
| `williams_r_14` | `ta.willr(high, low, close, length=14).round(2)` | -100 to 0 |

### Trend Strength

| Column | pandas-ta Call | Notes |
|---|---|---|
| `adx_14` | `ta.adx(high, low, close, length=14).round(2)` | Returns ADX, DMP, DMN. Store ADX. 0–100. |

### Derived Momentum

| Column | Formula |
|---|---|
| `momentum_score` | Composite (0-100) from RSI trend, MACD crossover, stochastic position |
| `decay_factor` | Exponential decay weight for older readings (range 0.5–1.0) |

### Target Table: `technical`

```sql
INSERT OR REPLACE INTO technical
(exchange, trade_date, symbol,
 sma_20, sma_50, sma_100, sma_200,
 ema_9, ema_20, ema_50, ema_200,
 price_vs_sma20_pct, price_vs_sma50_pct, price_vs_sma200_pct,
 golden_cross, ema20_gt_ema50, price_gt_sma200,
 trend_stage, trend_score, trend_strength)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```

---

## Price Level Stage

Computed from the `daily` table using rolling window aggregates.

| Column | Formula |
|---|---|
| `week52_high` | `rolling(252).max()` on high |
| `week52_low` | `rolling(252).min()` on low |
| `week26_high` | `rolling(126).max()` on high |
| `week26_low` | `rolling(126).min()` on low |
| `week4_high` | `rolling(20).max()` on high |
| `week4_low` | `rolling(20).min()` on low |
| `pct_from_52wk_high` | `round((week52_high - close) / week52_high * 100, 2)` |
| `pct_from_52wk_low` | `round((close - week52_low) / week52_low * 100, 2)` |
| `pct_from_4wk_high` | `round((week4_high - close) / week4_high * 100, 2)` |
| `near_52wk_high_flag` | `1` if pct_from_52wk_high <= 5 |
| `near_52wk_low_flag` | `1` if pct_from_52wk_low <= 5 |
| `breakout_flag` | `1` if close > week4_high (previous row) |
| `pivot_p` | `round((high + low + close) / 3, 2)` |
| `pivot_r1` | `round(2 * pivot_p - low, 2)` |
| `pivot_r2` | `round(pivot_p + (high - low), 2)` |
| `pivot_s1` | `round(2 * pivot_p - high, 2)` |
| `pivot_s2` | `round(pivot_p - (high - low), 2)` |

**Note:** Pivot levels use today's high/low to compute tomorrow's pivots. Store pivots keyed to today's date — they are valid for the next trading session.

### Target Table: `price_level`

```sql
INSERT OR REPLACE INTO price_level
(exchange, trade_date, symbol,
 week52_high, week52_low, week26_high, week26_low,
 week4_high, week4_low,
 pct_from_52wk_high, pct_from_52wk_low, pct_from_4wk_high,
 near_52wk_high_flag, near_52wk_low_flag, breakout_flag,
 pivot_p, pivot_r1, pivot_r2, pivot_s1, pivot_s2)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```

---

## Momentum Stage

Stored in the `momentum` table. All indicators from the Technical Stage section above (`rsi_14` through `williams_r_14`) are populated here as a dedicated table for focused momentum queries.

### Target Table: `momentum`

```sql
INSERT OR REPLACE INTO momentum
(exchange, trade_date, symbol,
 rsi_14, rsi_9,
 macd, macd_signal, macd_histogram, macd_crossover,
 stoch_k, stoch_d, stoch_signal,
 mfi_14, adx_14, cci_20, williams_r_14,
 momentum_score, decay_factor)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```

---

## Volatility Stage

### Indicators

| Column | pandas-ta Call / Formula | Notes |
|---|---|---|
| `atr_14` | `ta.atr(high, low, close, length=14).round(2)` | Average True Range in rupees |
| `atr_pct` | `round(atr_14 / close * 100, 2)` | ATR as % of close |
| `bb_upper` | `ta.bbands(close, length=20, std=2)` → `BBU_20_2.0` | Upper band |
| `bb_middle` | same call → `BBM_20_2.0` | SMA-20 (middle band) |
| `bb_lower` | same call → `BBL_20_2.0` | Lower band |
| `bb_width` | `round((bb_upper - bb_lower) / bb_middle, 4)` | Bandwidth (4 decimals) |
| `bb_squeeze` | `1` if bb_width at 6-month low (rolling 126-day min) | Bollinger Squeeze |
| `keltner_upper` | `ta.kc(high, low, close, length=20, scalar=1.5, mamode='ema')` → `KCUe_20_1.5` | Keltner upper |
| `keltner_lower` | same call → `KCLe_20_1.5` | Keltner lower |
| `historical_vol_20d` | `round(close.pct_change().rolling(20).std() * sqrt(252) * 100, 2)` | Annualised historical vol % |

**Note:** `ta.bbands()` and `ta.kc()` return multiple columns. Use `.iloc` or column name extraction to pick the right band.

### Target Table: `volatility`

```sql
INSERT OR REPLACE INTO volatility
(exchange, trade_date, symbol,
 atr_14, atr_pct,
 bb_upper, bb_middle, bb_lower, bb_width, bb_squeeze,
 keltner_upper, keltner_lower,
 historical_vol_20d)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```

---

## Averages Stage
- Rolling averages over: 1M (21d), 3M (63d), 6M (126d), 1Y (252d), 3Y (756d)
- Metrics to average: close, volume, RSI, delivery %, volatility
- Store as separate feature columns with suffix `_avg_{window}`
- Module: `src/data_pipeline/averages.py`

**Precision:** `round(..., 2)` on every average column.

### Target Table: `averages`

```sql
INSERT OR REPLACE INTO averages
(exchange, trade_date, symbol,
 close_avg_21d, close_avg_63d, close_avg_126d, close_avg_252d, close_avg_756d,
 volume_avg_21d, volume_avg_63d, volume_avg_126d, volume_avg_252d, volume_avg_756d,
 rsi_avg_21d, rsi_avg_63d, rsi_avg_126d, rsi_avg_252d, rsi_avg_756d,
 delivery_pct_avg_21d, delivery_pct_avg_63d, delivery_pct_avg_126d, delivery_pct_avg_252d, delivery_pct_avg_756d,
 volatility_avg_21d, volatility_avg_63d, volatility_avg_126d, volatility_avg_252d, volatility_avg_756d)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
```

---

## Batch Processing Pattern

Every stage follows the same pattern:

```python
def run_stage(start_date, end_date):
    logger.info(f"Starting {STAGE_NAME} for {start_date} to {end_date}")
    conn = get_connection()
    cursor = conn.cursor()

    # 1. Read all source data in one query
    df = pd.read_sql("SELECT * FROM source_table WHERE trade_date BETWEEN ? AND ? ORDER BY symbol, trade_date", conn, params=(start_date, end_date))

    # 2. Compute per symbol (groupby + pandas-ta)
    results = []
    for symbol, group in df.groupby("symbol"):
        computed = compute_indicators(group)
        results.append(computed)

    result_df = pd.concat(results)

    # 3. Batch upsert
    batch = []
    for _, row in result_df.iterrows():
        batch.append((row["exchange"], row["trade_date"], row["symbol"], ...))
        if len(batch) >= 500:
            cursor.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        cursor.executemany(INSERT_SQL, batch)

    conn.commit()
    conn.close()
    logger.info(f"Completed {STAGE_NAME}: {len(result_df)} rows")
```

---

## Extensibility — Adding a New Indicator

No stage module needs modification when another stage adds a new indicator. Each stage owns its target table via `INSERT OR REPLACE` using explicit column lists — new columns in other tables have no effect.

To add a new indicator to an existing table:

```python
# 1. Add column (in init_schema migration)
cursor.execute("ALTER TABLE technical ADD COLUMN super_trend REAL")

# 2. Add one pandas-ta line in the stage module
df["super_trend"] = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0).round(2)

# 3. Add column to INSERT column list and VALUES tuple
INSERT_SQL = """
    INSERT OR REPLACE INTO technical
    (exchange, trade_date, symbol, ..., super_trend)
    VALUES (?, ?, ?, ..., ?)
"""
```

No changes needed to other stages, orchestrator, or validation gates.

---

## Symbol Filtering — Equity-Only via ISIN

The pipeline stores the **ISIN** from the NSE bhavcopy in the `stage.isin` column. ISIN prefix determines instrument type:

| Prefix | Type | Example |
|--------|------|---------|
| `INE` | Equity (common stock) | `INE144J01027` (20MICRONS) |
| `INF` | ETF / Mutual Fund | `INF579M01BB5` (GOLD360) |
| `IN9` | Other (e.g., REITs) | `IN9175A01010` (JISLDVREQS) |

**The scoring pipeline only scores symbols with ISIN starting with `INE`** — ETFs (INF) are excluded because their price patterns differ fundamentally from equities.

Implementation detail in `fetcher.py`:
- `_parse_bhavcopy()` preserves the `ISIN` column from the CSV (column name is always uppercase `ISIN` in all NSE bhavcopy formats)
- `process_nse_data()` stores it in `stage.isin` via the `STAGE_INSERT_SQL`
- The `stage` table schema includes `isin TEXT` (added via ALTER TABLE migration in `init_schema()`)

## Scoring Formula — Derived from Data Mining

The scoring model is a **5-factor composite** built by reverse-engineering what predicts 5-10% price increases over 10-20 trading days:

```python
# 1. Contrarian entry (reward recent pullbacks)
neg_ret = df['day_return_pct'].fillna(0) < 0
score = np.where(neg_ret, df['day_return_pct'].fillna(0).abs() * 1.5, 0)

# 2. High delivery % (> 60% = strong hands)
score += (df['pct'].fillna(0) >= 60).astype(float) * 15

# 3. Rising delivery trend (delivery_pct_trend >= 0.5)
score += (df['delivery_pct_trend'].fillna(0) >= 0.5).astype(float) * 10

# 4. Delivery quantity surge (qty > 1.5x 20d avg)
score += (df['delivery_qty_ratio'].fillna(0) >= 1.5).astype(float) * 10

# 5. Size/liquidity (traded_value percentile)
score += df['traded_value'].rank(pct=True) * 15
```

**Backtest verified (top 30 stocks, 2025-07 to 2026-05):**
- 10-day forward: **+2.68%** avg, 53.9% win rate vs +1.28% random
- 15-day forward: **+2.31%** avg, 54.8% win rate vs -0.65% random

See `scoring.skill.md` for full analysis and rejected alternatives.

## Common Patterns
- Always validate input data shape before processing
- Log stage entry/exit with row counts and time taken
- Round ALL numeric outputs to 2 decimal places at the point of creation — never trust pandas-ta defaults
- Heavy reads are expected — read entire table at once, filter and process in pandas
- Time per stage is acceptable — no query-level performance optimization needed
