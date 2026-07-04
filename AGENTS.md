# Phase1 — Stock Scrip Scoring System

Complete specification. An LLM reading this should be able to regenerate the full codebase with identical pipeline behaviour.

---

## How to Use This Document

**This file is the complete technical specification.** Any change to the application starts here.

> **See also:** `information.md` for the development workflow and process guide.

### Workflow

1. **Edit this file** — Add/modify specification for new features
2. **Ask agent to implement** — Agent reads this file and generates code
3. **Verify** — Run tests/lint to confirm changes match spec

### Quick Reference

| Section | Purpose |
|---------|---------|
| Pipeline Architecture | Stage order and what each stage does |
| Database Tables | All table schemas |
| V2a Scoring Formula | How stocks are scored |
| CLI | Command-line interface usage |
| Implementation Conventions | Coding patterns to follow |

---

## Project Structure

```
├── AGENTS.md                 ← this file (complete spec)
├── config/
│   ├── __init__.py           ROOT_DIR, DATA_DIR, DB_PATH constants
│   └── scoring.yaml          All tunable parameters (44 lines)
├── db/
│   └── connection.py         SQLite schema, init_schema(), get_connection()
├── data/                     Cached CSVs (bhavcopy, MTO, eq_mast.csv)
├── src/
│   ├── runner.py             CLI orchestrator, stage dispatch
│   ├── data_pipeline/
│   │   ├── fetcher.py        Download + parse NSE bhavcopy → stage table, MTO → stage_delivery
│   │   ├── equity_master.py  Sector/industry from NSE equity master CSV
│   │   ├── enricher.py       Merge stage + stage_delivery → daily table (wvap, delivery, 5d flags)
│   │   ├── technical.py      SMA, EMA, golden cross, trend stage
│   │   ├── price_level.py    52wk/26wk/4wk highs/lows, pivots
│   │   ├── momentum.py       RSI, MACD, Stoch, MFI, ADX, CCI, Williams %R
│   │   ├── volatility.py     ATR, Bollinger Bands, Keltner Channels
│   │   ├── averages.py       Multi-window rolling averages (5 windows × 5 metrics)
│   │   └── derivatives.py    FO UDiFF futures data via NSE daily-reports API
│   ├── scoring/
│   │   └── scorer.py         13-component V2a scoring formula
│   └── validation/
│       ├── hits_analyzer.py  predicted_stock table + hit rate analysis
│       └── run_hits.py       CLI for --compute / --analyze
├── build_fno_membership.py   Populate fno_membership table
├── build_index_history.py    Populate index_membership table
├── build_shareholding.py     Populate shareholding table
├── build_equity_master.py    Build data/eq_mast.csv
└── requirements.txt          pyyaml>=6.0, pandas>=2.0, pandas-ta>=0.3, requests>=2.31, numpy>=1.24, brotli>=1.2
```

---

## Pipeline Architecture

```
STAGE ORDER (src/runner.py):
  fetch → equity_master → enrich → technical → price_level → momentum
    → volatility → averages → derivatives → score → hits

  fetch:         Download BhavCopy CSVs + MTO.DAT files from NSE, parse →
                 stage table (raw price) + stage_delivery table (qty, pct)
  equity_master: Download NSE EQUITY_L.csv (or EQ_MAST.csv) → equity_master table (symbol, isin, sector, industry)
  enrich:        Join stage + stage_delivery, compute wvap, delivery metrics,
                 lowest_closing_5days, highest_closing_5days → daily table
  technical:     SMA(20/50/100/200), EMA(9/20/50/200), crossovers →
                 technical table
                 ⚠ SMA/EMA from pandas_ta may contain Python `None` (not NaN).
                   Always coerce via `pd.to_numeric(col, errors="coerce")` before
                   comparison ops like `>`, `<`, `==` to avoid `TypeError`.  See
                   conventions below.
  price_level:   252/126/20-day rolling highs/lows, pivot points →
                 price_level table
  momentum:      RSI(14/9), MACD(12/26/9), Stoch(14/3/3), MFI(14), ADX(14),
                 CCI(20), Williams %R(14) → momentum table
                 ⚠ Window functions (rsi, stoch_k, macd component scoring) must
                   use `np.asarray(pd.to_numeric(col, errors="coerce").fillna(v), dtype=float)`
                   to convert columns before numpy ops.  pandas-ta can produce
                   Python `None` → object dtype → `UFuncOutputCastingError`.
  volatility:    ATR(14), Bollinger Bands(20,2σ), Keltner Channels,
                 historical vol(20d) → volatility table
  averages:      Rolling means of close, volume, RSI, delivery%, volatility
                  at 21/63/126/252/756 windows + volume technical metrics
                  (vol_5d_avg, vol_10d_avg, vol_20d_avg, vol_ratio,
                   vol_breakout_up, vol_trend_5d, volume_score) → averages table
                 ⚠ The merged DataFrame column names must match metric names:
                   rename `close_price→close`, `traded_volume→volume` before
                   the per-metric loop, or `KeyError` on column lookup.
  derivatives:   NSE FO UDiFF bhavcopy via daily-reports API →
                 futures_data table (basis, OI change)
  score:         V2a formula (13 components), delivery value filter,
                 sector-diversified top 20 → scoring_result + scoring_picks
  hits:          Forward-return validation → predicted_stock table
```

Execution: stages run sequentially. Each stage reads from SQLite (WAL mode), computes, batch-inserts via `executemany(500 rows)`. Scoring is threaded: `ThreadPoolExecutor(max_workers=4)` with per-date try/except. Config read once: `_load_config()` cached via `@lru_cache(maxsize=1)`.

---

## Database Tables (SQLite, WAL journal mode)

### stage
| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | 'NSE' |
| trade_date | TEXT PK | ISO date |
| symbol | TEXT PK | |
| open_price | REAL | |
| high_price | REAL | |
| low_price | REAL | |
| close_price | REAL | |
| previous_close | REAL | |
| traded_volume | INTEGER | |
| traded_value | REAL | Rupees |
| day_return_pct | REAL | (close - prev_close) / prev_close × 100 |
| upper_circuit_hit | INTEGER | day_return_pct >= 19.5 |
| lower_circuit_hit | INTEGER | day_return_pct <= -19.5 |
| isin | TEXT | INE-prefix only |

Source: BhavCopy_JSon files, 3-column-name formats, filtered to SERIES='EQ'.

### stage_delivery
| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | 'NSE' |
| trade_date | TEXT PK | ISO date |
| symbol | TEXT PK | |
| qty | INTEGER | delivery quantity |
| pct | REAL | delivery % |

Source: NSE MTO_{DDMMYYYY}.DAT files (pipe-delimited with comma-separated fields starting with '20'). Columns (positional): date, ref, symbol, series, isin, qty, pct. Filtered to SERIES='EQ'. Raw delivery data stored here, then merged into daily by enricher.

### daily
| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| open_price, high_price, low_price, close_price, previous_close | REAL | |
| traded_volume | INTEGER | |
| traded_value | REAL | |
| day_return_pct | REAL | (close - prev_close) / prev_close × 100 |
| upper_circuit_hit | INTEGER | day_return_pct >= 19.5 |
| lower_circuit_hit | INTEGER | day_return_pct <= -19.5 |
| delivery_volume | INTEGER | delivery quantity |
| delivery_value | REAL | delivery_volume × wvap_price |
| delivery_pct | REAL | delivery % (from MTO) |
| wvap_price | REAL | (high_price + low_price + close_price) / 3 |
| lowest_closing_5days | INTEGER | 1 if today's close is lowest in last 5 trading days (min_periods=5) |
| highest_closing_5days | INTEGER | 1 if today's close is highest in last 5 trading days (min_periods=5) |
| delivery_qty_5d_avg | REAL | Rolling 5-day mean of delivery_volume |
| delivery_qty_20d_avg | REAL | Rolling 20-day mean of delivery_volume |
| delivery_pct_5d_avg | REAL | Rolling 5-day mean of delivery_pct |
| delivery_pct_20d_avg | REAL | Rolling 20-day mean of delivery_pct |
| delivery_pct_trend | REAL | Linear slope of delivery_pct over 5d |
| vol_spike | INTEGER | delivery_volume > 2× delivery_qty_20d_avg |

### technical
| Column | Type | Notes |
|--------|------|-------|
| sma_20, sma_50, sma_100, sma_200 | REAL | pandas_ta.sma() |
| ema_9, ema_20, ema_50, ema_200 | REAL | pandas_ta.ema() |
| price_vs_sma20_pct, price_vs_sma50_pct, price_vs_sma200_pct | REAL | (close - sma) / sma × 100 |
| golden_cross | INTEGER | sma_50 crosses above sma_200 |
| ema20_gt_ema50 | INTEGER | ema_20 > ema_50 |
| price_gt_sma200 | INTEGER | close > sma_200 |
| trend_stage | TEXT | 'bullish' / 'bearish' / 'recovering' / 'range-bound' |
| trend_score | REAL | 0-100 (sma200 distance + sma50 distance + ema50 + gc + stage) |
| trend_strength | REAL | 0-100 (ma_gap + close_change) |

### price_level
| Column | Type | Notes |
|--------|------|-------|
| week52_high, week52_low | REAL | rolling(252).max()/.min() |
| week26_high, week26_low | REAL | rolling(126) |
| week4_high, week4_low | REAL | rolling(20) |
| pct_from_52wk_high, pct_from_52wk_low, pct_from_4wk_high | REAL | |
| near_52wk_high_flag | INTEGER | close >= 0.95 × week52_high |
| near_52wk_low_flag | INTEGER | close <= 1.05 × week52_low |
| breakout_flag | INTEGER | close > prev week4_high |
| pivot_p, pivot_r1, pivot_r2, pivot_s1, pivot_s2 | REAL | Classic pivot (H+L+C)/3 |

### momentum
| Column | Type | Notes |
|--------|------|-------|
| rsi_14, rsi_9 | REAL | pandas_ta.rsi() |
| macd, macd_signal, macd_histogram | REAL | pandas_ta.macd(12,26,9) |
| macd_crossover | INTEGER | macd crosses above signal |
| stoch_k, stoch_d | REAL | pandas_ta.stoch(14,3,3) |
| stoch_signal | REAL | stoch_k.rolling(3).mean() |
| mfi_14 | REAL | pandas_ta.mfi() |
| adx_14 | REAL | pandas_ta.adx() |
| cci_20 | REAL | pandas_ta.cci() |
| williams_r_14 | REAL | pandas_ta.willr() |
| momentum_score | REAL | 0-100 = rsi_comp(33) + macd_comp(34) + stoch_comp(33) |
| decay_factor | REAL | 0.5 + 0.5×exp(-t/63) |

### volatility
| Column | Type | Notes |
|--------|------|-------|
| atr_14 | REAL | pandas_ta.atr(14) |
| atr_pct | REAL | atr_14 / close × 100 |
| bb_upper, bb_middle, bb_lower | REAL | pandas_ta.bbands(20,2) |
| bb_width | REAL | (bb_upper - bb_lower) / bb_middle |
| bb_squeeze | INTEGER | bb_width < 20-period rolling mean of bb_width |
| keltner_upper, keltner_lower | REAL | pandas_ta.kc(20,2) |
| historical_vol_20d | REAL | close.pct_change().rolling(20).std() × √252 × 100 |

### averages
5 windows: [21, 63, 126, 252, 756]. Columns follow pattern `{metric}_avg_{window}d`:
- close_avg_{w}d, volume_avg_{w}d, rsi_avg_{w}d, delivery_pct_avg_{w}d, volatility_avg_{w}d
- Volume technical metrics appended: vol_5d_avg, vol_10d_avg, vol_20d_avg, vol_ratio, vol_breakout_up, vol_trend_5d, volume_score

### futures_data
| Column | Type | Notes |
|--------|------|-------|
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| expiry_date | TEXT PK | Nearest monthly expiry |
| futures_open, futures_high, futures_low, futures_close | REAL | From FO UDiFF |
| futures_oi | INTEGER | Open interest |
| futures_oi_chg | INTEGER | Change in OI |
| futures_volume | INTEGER | (aliased but not in PK) |
| spot_price | REAL | Underlying price from report |
| basis_pct | REAL | (futures_close - spot_price) / spot_price × 100 |
| oi_change_pct | REAL | (oi_chg / oi) × 100 |

Source: NSE daily-reports API → FO-UDIFF-BHAVCOPY-CSV → STF (stock futures) rows at nearest expiry.

### scoring_result
| Column | Type |
|--------|------|
| overall_score | REAL (0-100) |
| momentum_score, value_score, quality_score, technical_strength, volume_liquidity, institutional_score, fno_score, futures_basis_score, oi_trend_score, sector_momentum_score, volatility_score, confirmation_score | REAL |
| nifty500_member | INTEGER |
| heuristic_penalties | TEXT (JSON) |
| rank, percentile | INTEGER, REAL |

### scoring_picks
| trade_date | TEXT PK |
| symbol | TEXT PK |
| score | REAL |
| rank | INTEGER |
| sector | TEXT |

### scoring_performance
| pick_date | TEXT PK |
| check_date | TEXT PK |
| symbol | TEXT PK |
| entry_price | REAL |
| exit_price | REAL |
| return_pct | REAL |

### shareholding
| symbol | TEXT PK |
| period | TEXT PK | e.g. '2025-03' |
| quarter_end | TEXT |
| quarter_end_int | INTEGER |
| promoter_pct, fii_pct, dii_pct, public_pct | REAL |

Source: `github.com/aditya-jha/nse-historical-membership` → `shareholding_history/data/parsed/_flat.csv` (CC BY 4.0).
Column `period` detected by exact match `fn_lower == "period"` (in addition to substring match), since the flat CSV uses a bare `period` column name.

### fno_membership
| symbol | TEXT PK |
| valid_from | TEXT PK |
| valid_to | TEXT (nullable = still active) |

Source: `github.com/aditya-jha/nse-historical-membership` → `fno_history/data/fno_membership_history.csv` (CC BY 4.0).

### index_membership
| symbol | TEXT PK |
| index_name | TEXT PK |
| index_id | INTEGER PK |
| valid_from | TEXT PK |
| valid_to | TEXT |
| weightage | REAL |

Source: `github.com/aditya-jha/nse-historical-membership` → `index_history/data/index_membership_history.csv` (CC BY 4.0).
20 sector indices mapped to normalized sector names. Nifty 500 = quality gate.

### predicted_stock
| Column | Type | Description |
|--------|------|-------------|
| pick_date | TEXT PK | Date pick was made |
| symbol | TEXT PK | |
| entry_date | TEXT | Next trading day after pick |
| entry_price | REAL | open × (1 + slippage_pct/100), or high price if slippage_pct=null |
| tg1_price | REAL | entry × (1 + target1%/100) |
| tg1_date | TEXT | First date high reached tg1_price (null = not hit) |
| tg1_high | REAL | Highest price within window 1 |
| tg1_close | REAL | Close at window 1 end |
| tg2_price, tg2_date, tg2_high, tg2_close | | Same pattern for target 2 |
| tg3_price, tg3_date, tg3_high, tg3_close | | Same pattern for target 3 |
| window_low | REAL | Lowest price across full window |
| window_low_date | TEXT | |
| window_end_date | TEXT | |
| target_hit | INTEGER | 0=none, 1=tg1, 2=tg2, 3=tg3 (hierarchical) |
| data_complete | INTEGER | |

All tables have primary keys and indexes on (symbol, trade_date) and (trade_date).

---

## NSE Data Sources

### Bhavcopy URL
```
https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip
```
3-column-name format variants detected and auto-renamed: TCKRSYMB→SYMBOL, SCTYSRS→SERIES etc. (old format), TckrSymb→SYMBOL etc. (intermediate), SYMBOL directly (new format). Multiple SERIES per symbol; only EQ kept. ISIN column may be absent.

### MTO Delivery URL
```
https://archives.nseindia.com/archives/equities/mto/MTO_{DDMMYYYY}.DAT
```
Fixed-width-ish format: lines starting with '20' = data rows. Fields comma-separated after first 2 chars of date. Positions: [0]=date, [2]=symbol, [3]=series, [4]=ISIN, [5]=delivery_qty, [6]=delivery_pct. Filtered to SERIES='EQ'.

### Equity Master URL (fallback chain)

The primary NSE equity master CSV (`EQ_MAST.csv`) at `https://nsearchives.nseindia.com/content/equities/EQ_MAST.csv` **returns 404** as of July 2026. The working fallback is the listed-equities CSV:

```
https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv
```

Columns: `SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE, MARKET LOT, ISIN NUMBER, FACE VALUE`
(No INDUSTRY column — all symbols get sector=`UNKNOWN`, industry=`UNKNOWN` via the module's fallback.)

The `equity_master.py` module tries `EQ_MAST.csv` first, then falls back to `EQUITY_L.csv` if the primary file doesn't exist on disk. ISIN column detection uses substring `"ISIN" in col_name` (not exact match) to handle the `ISIN NUMBER` variant.

### FO UDiFF (derivatives) via NSE API
```
GET https://www.nseindia.com/api/daily-reports?key=FO
```
Returns JSON with CurrentDay/PreviousDay arrays. Each item has: fileKey='FO-UDIFF-BHAVCOPY-CSV', tradingDate (DD-Mon-YYYY), filePath, fileActlName. Full URL = filePath + fileActlName. Response is a ZIP containing one CSV. Columns: FinInstrmTp='STF' = stock futures, TckrSymb, XpryDt, OpnPric, HghPric, LwPric, ClsPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol, UndrlygPric.

> **Brotli compression:** The NSE API now returns responses with `Content-Encoding: br` (Brotli compression). The `brotli` Python package is required for requests to auto-decompress. Install via `pip install brotli` — listed in `requirements.txt`.

> **Expiry date format:** The `XpryDt` column in the CSV may be in either `DD-Mon-YYYY` (legacy) or `YYYY-MM-DD` (current) format. The parser's `_parse_nse_date()` function detects both formats automatically.

### HTTP Headers (all NSE requests)
```python
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}
```
Session init: `session.get("https://www.nseindia.com", timeout=10)` first to get cookies, then download. One session per download call.

---

## NSE Trading Holidays 2025–2026

Hardcoded as a Python `set` of `datetime.date` objects in `fetcher.py`:

```python
NSE_HOLIDAYS_2025_2026 = {
    # 2025 (14 holidays)
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1), date(2025, 8, 15), date(2025, 8, 27),
    date(2025, 10, 2), date(2025, 10, 21), date(2025, 10, 22),
    date(2025, 11, 5), date(2025, 12, 25),
    # 2026 (16 holidays)
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3),
    date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24),
    date(2026, 12, 25),
}
```
`_business_days(start, end)` skips weekends (weekday() >= 5) and dates in this set.

---

## Download Strategy (fetcher.py)

1. Check if file exists on disk (in `data/bhavcopy_nse/` or `data/delivery_nse/`) — skip if present.
2. Download using `ThreadPoolExecutor(max_workers=10)` — each file independently.
3. Log failures to `data/download_errors.log`.
4. Parse ZIP contents (bhavcopy) or line-by-line (MTO) in `process_nse_data()`.
5. Batch insert into stage and stage_delivery tables: `executemany(sql, batch[i:i+500])` per date, per table, then commit.

---

## V2a Scoring Formula (scorer.py)

### Data Merge

One SQLite query per data source at the given trade_date, merged on symbol:

```python
daily         [exchange, trade_date, symbol, close_price, previous_close, traded_value,
               day_return_pct, delivery_volume, delivery_pct, delivery_qty_20d_avg, delivery_pct_trend, isin]
shareholding  [fii_pct, dii_pct, promoter_pct, ...]  — latest 2 quarters, diff computed
futures_data  [basis_pct, oi_change_pct]
momentum      [rsi_14, adx_14, macd, ...]
volatility    [atr_pct]
sector        [sector]  — from index_membership, fallback to equity_master
```

ISIN filter: `isin.str.startswith('INE', na=False)`.  
Delivery value filter: `delivery_volume × wvap_price >= min_dev_trade_value_cr × 1e7` (default 0.5 Cr).

> **Defensive merge pattern:** `futures_data` and `shareholding` are optional data sources. When empty (no rows for the trade_date), the merge is skipped, and the expected columns (`basis_pct`, `oi_change_pct`, `curr_fii`, `prev_fii`, `curr_dii`, `prev_dii`) would be missing — causing `KeyError` in score computation. **Always add an `else` branch** after `if not df.empty:` that zero-fills the expected columns so downstream code never fails on missing columns.
>
> **Shareholding `quarter_end_int`:** The `_load_shareholding()` query filters on `WHERE quarter_end_int <= ?`. If this column is NULL (as it was after initial `build_shareholding.py` runs), the query returns 0 rows and shareholding data is silently skipped. The build script must populate `quarter_end_int` via `CAST(REPLACE(quarter_end, '-', '') AS INTEGER)`, or scoring will have no institutional data for any date.

### Component Score Computation

All weights from `config/scoring.yaml`. Pseudocode:

```python
# 1. Contrarian entry (↗ momentum_score, weight ~10-15%)
contrarian = abs(day_return_pct) * value_return_weight  IF day_return_pct < 0
             ELSE 0
clip to [0, 50]

# 2. High delivery % (↗ value_score, weight ~15-20%)
value_score = delivery_pct_boost (15)  IF pct >= delivery_pct_threshold (60)
              ELSE 0

# 3. Rising delivery trend (↗ quality_score, weight ~10-15%)
quality_score = pct_trend_boost (10)  IF delivery_pct_trend >= pct_trend_threshold (0.5)
               ELSE 0

# 4. Delivery qty surge (↗ technical_strength, weight ~10-15%)
ratio = delivery_volume / delivery_qty_20d_avg  (only where avg > 0)
technical_strength = delivery_qty_boost (10)  IF ratio >= delivery_qty_ratio_threshold (1.5)
                    ELSE 0

# 5. Size / liquidity (↗ volume_liquidity, weight ~15-20%)
mcap_tier = quartile of traded_value rank
tier_liq = traded_value rank within mcap_tier (percentile)
volume_liquidity = tier_liq * liquidity_percentile_weight (15)

# 6. Institutional accumulation (↗ institutional_score, weight ~10-15%)
smart_money_delta = (fii_pct - prev_fii_pct) + (dii_pct - prev_dii_pct)  (4 quarters apart)
institutional_score = min(delta * institutional_scale_factor, institutional_max_boost)
                     IF delta > institutional_delta_threshold (2.0)
                     ELSE 0

# 7. F&O membership (↗ fno_score, weight ~3-5%)
fno_score = fno_boost (3)  IF symbol is in fno_membership (PIT at trade_date)
           ELSE 0

# 8. Nifty 500 membership (↗ nifty500_member + nifty500_score, weight ~2-3%)
nifty500_score = nifty500_boost (2)  IF symbol in Nifty 500 (PIT)
               ELSE 0

# 9. Futures basis (↗ futures_basis_score, weight ~3-5%)
futures_basis_score = min(basis_pct × 2, futures_basis_boost (5))
                     IF basis_pct > futures_basis_threshold (0.0)
                     ELSE 0

# 10. OI trend (↗ oi_trend_score, weight ~3-5%)
oi_trend_score = oi_trend_boost (5)  IF oi_change_pct > oi_trend_threshold (5.0) AND day_return > 0
                ELSE 0

# 11. Sector-relative momentum (↗ sector_momentum_score, weight ~3-5%)
sector_mean_return = groupby('sector')['day_return_pct'].mean()
sector_relative = day_return_pct - sector_mean_return
sector_momentum_score = percentile_rank(sector_relative) * sector_momentum_boost (5)

# 12. Volatility filter (↗ volatility_score, weight ~3-5%)
atr_pctile = percentile_rank(atr_pct)
volatility_score = (1 - atr_pctile) * volatility_penalty (5)

# 13. Multi-indicator confirmation (↗ confirmation_score, weight ~2-3%)
confirmed = (50 < rsi_14 < 70) AND (adx_14 > 25) AND (macd_histogram > 0)
confirmation_score = confirmed * confirmation_boost (3)

# Overall (clipped to 0-100)
overall_score = momentum_score + value_score + quality_score + technical_strength
              + volume_liquidity + institutional_score + fno_score + nifty500_score
              + futures_basis_score + oi_trend_score + sector_momentum_score
              + volatility_score + confirmation_score
```

### Rank + Pick Selection

```python
today['rank'] = overall_score.rank(ascending=False, method='min')
today['percentile'] = overall_score.rank(pct=True) × 100
```

Top `max_per_sector` (5) per sector, sequentially from highest score. Result: `top_n` (20) picks saved to `scoring_picks` with sector name.

### Verification (auto-run each scoring call)

All past picks where `pick_date + lookahead_days (10) <= trade_date` and not yet verified: compute forward return using `daily.close_price`, store in `scoring_performance`.

---

## Sector Data Fallback Chain

1. `index_membership` table (20 NSE sector indices, PIT accurate, 1313 symbols) — primary source
2. `equity_master` table / `data/eq_mast.csv` (515 named sectors, rest UNKNOWN)
3. Default: `'UNKNOWN'`

Equity master rebuilt by `build_equity_master.py`: downloads Nifty 500 constituent list, maps Industry to sector column. 2401 rows (515 named sectors, 20 unique sector names).

> **Note:** As of July 2026, the NSE `EQ_MAST.csv` URL is dead. The pipeline stage falls back to `EQUITY_L.csv` (no INDUSTRY column), resulting in all symbols having sector=UNKNOWN / industry=UNKNOWN. The primary sector source is always `index_membership`.

---

## Hit Analysis (hits_analyzer.py)

### Compute
Reads `scoring_picks` for each pick_date, finds next trading day in `daily` table => entry_date. For each entry mode (from `scoring.yaml` hit_analysis):
- If `slippage_pct` is not null: `entry_price = next_day.open × (1 + slippage_pct/100)`
- If `slippage_pct` is null: `entry_price = next_day.high`

For each target (pct from config) and window (days from config):
- Target price = entry_price × (1 + target%/100)
- Scan daily data for pick_date + 1 to pick_date + window, find first date where high >= target price
- Record tg_high, tg_close, window_low, window_low_date

`target_hit`: hierarchical (0=none, 1=tg1 hit, 2=tg2 hit, 3=tg3 hit).

### Analyze
Pivot on entry_mode × target × window: count picks, count hits, compute hit %. Print grouped by entry_mode.

### Output (7,359 picks, Jan 2025–Jun 2026, open+3%)
| Target Hit | Count | % |
|------------|-------|---|
| None | 5,692 | 77.3% |
| Tg1 (4% in 5d) | 95 | 1.3% |
| Tg2 (5% in 10d) | 690 | 9.4% |
| Tg3 (10% in 15d) | 882 | 12.0% |
| **Any** | **1,667** | **22.7%** |

---

## Performance Optimisations

| Technique | Scope | Details |
|-----------|-------|---------|
| Vectorised batch build | enricher, technical, price_level, momentum, volatility, averages | `df[cols].to_numpy().tolist()` instead of `iterrows()` — 40% faster on 60K-row DataFrames |
| Threaded download | fetcher | `ThreadPoolExecutor(max_workers=10)` for parallel NSE file download |
| Threaded scoring | runner score stage | `ThreadPoolExecutor(max_workers=4)` with per-date try/except — SQLite WAL handles concurrent reads |
| Config caching | scorer | `@lru_cache(maxsize=1)` on `_load_config()` — eliminates ~1,100 redundant YAML reads per 366-day run |
| Batch inserts | All stages | `executemany(sql, batch[i:i+500])` — 500 rows per transaction |
| SQLite WAL mode | connection | `PRAGMA journal_mode=WAL` — concurrent readers, no writer lock contention |
| Pipeline timing | Full run | ~21 minutes for 18 months (Jan 2025–Jun 2026) from clean DB, files cached |

---

## Configuration (config/scoring.yaml)

See full file at `config/scoring.yaml` (44 lines). All component weights, thresholds, and boost values are configurable:

```yaml
scoring:
  confirmation_boost: 3
  delivery_pct_boost: 15
  delivery_pct_threshold: 60
  delivery_qty_boost: 10
  delivery_qty_ratio_threshold: 1.5
  fno_boost: 3
  futures_basis_boost: 5
  futures_basis_threshold: 0.0
  institutional_delta_threshold: 2.0
  institutional_max_boost: 15
  institutional_scale_factor: 3.0
  liquidity_percentile_weight: 15
  lookahead_days: 10
  max_per_sector: 5
  min_dev_trade_value_cr: 0.5
  nifty500_boost: 2
  oi_trend_boost: 5
  oi_trend_threshold: 5.0
  pct_trend_boost: 10
  pct_trend_threshold: 0.5
  sector_momentum_boost: 5
  top_n: 20
  value_return_weight: 1.5
  volatility_penalty: 5
hit_analysis:
  entry_modes:
    - name: open_p3
      slippage_pct: 3
    - name: high_val
      slippage_pct: null
  targets:
    - name: target1
      pct: 4
    - name: target2
      pct: 5
    - name: target3
      pct: 10
  windows: [5, 10, 15]
```

---

## Implementation Conventions

- ISIN column detection in equity master CSV uses `"ISIN" in col_name` (substring match), not exact match — handles both `ISIN` and `ISIN NUMBER` column headers
- Period column detection in shareholding CSV uses `fn_lower == "period"` (exact match) in addition to substring matching — the `_flat.csv` file uses a bare `period` column name
- All numeric values rounded to 2 decimal places at creation
- `_safe_rnd(result)` wraps `pandas_ta` calls: `result.round(2) if result is not None else None`
- `np.where` with Series: convert `.to_numpy()` first to avoid pandas-index alignment issues
- `.astype(float)` after `np.where` to ensure float64 dtype
- **SMA/EMA coercion:** pandas-ta may return Python `None` (not NaN) in SMA/EMA columns. Always use `pd.to_numeric(col, errors="coerce")` before comparison ops (`>`, `<`, `==`) to avoid `TypeError`. This applies to `golden_cross`, `ema20_gt_ema50`, `price_gt_sma200` and any flag using SMA/EMA values. See also: `pandas_ta` issue with return types.
- **Window function conversion:** RSI, Stoch, MACD histogram columns from pandas-ta can be object dtype when Python `None` values appear. Before using in `np.where` or `np.minimum`, convert with `np.asarray(pd.to_numeric(col, errors="coerce").fillna(v), dtype=float)` to avoid `UFuncOutputCastingError`.
- **Averages column rename:** The `averages.py` merge loop iterates with metric names (`"close"`, `"volume"`, etc.) as column keys. The merged DataFrame must have columns named exactly `close`, `volume`, `rsi`, `delivery_pct`, `volatility` — not `close_price`, `traded_volume`. Always add these renames to the `merged.rename(columns={...})` dict.
- **Derivatives date format:** The FO UDiFF CSV `XpryDt` column switched from `DD-Mon-YYYY` to `YYYY-MM-DD`. The parser's `_parse_nse_date()` function checks for ISO format first (via length/position heuristics), then falls back to `%d-%b-%Y` parsing.
- **Brotli compression:** NSE API responses now use `Content-Encoding: br`. The `brotli` package is required in `requirements.txt` for `requests` to auto-decompress. Without it, `resp.json()` fails with `JSONDecodeError: Expecting value`.
- **Scorer defensive merge:** Any optional data source (`futures_data`, `shareholding`) merged via `if not df.empty:` must have an `else` branch that zero-fills all expected columns. Without this, dates lacking that data raise `KeyError` in score computation because the merge never creates the columns.
- **Shareholding `quarter_end_int` must be populated:** `_load_shareholding()` filters on `WHERE quarter_end_int <= ?`. The build script (`build_shareholding.py`) must populate `quarter_end_int` via `CAST(REPLACE(quarter_end, '-', '') AS INTEGER)`. If NULL, the query returns 0 rows, the DataFame is empty, the merge is skipped, and `curr_fii`/`prev_fii`/`curr_dii`/`prev_dii` columns are missing — causing `KeyError: 'curr_fii'` for all dates.
- **Hit analyzer `_get_next_trading_day` must filter by symbol:** The function queries `FROM daily WHERE trade_date > ? ORDER BY trade_date ASC LIMIT 1`. Without `symbol = ?` in the WHERE clause, it returns the first row from ANY stock on the next trading day, making ALL picks share the same entry_price. **Always add `symbol = ?`** to the query parameters.
- **`lowest_closing_5days` / `highest_closing_5days` compute pattern:** Both use `groupby(symbol).transform(lambda x: x.rolling(5, min_periods=5).min()/.max())` on `close_price` then compare against the current value with `==`. The `min_periods=5` ensures the first 4 rows per symbol always produce `False` (NaN comparison). The comparison result is cast to `int`.
- All pipeline modules share a logger via `get_logger('runner')`
- `INSERT OR REPLACE` everywhere — idempotent
- Only `requests`, `pandas`, `pandas-ta`, `numpy`, `pyyaml` in requirements — no `yfinance`, `jugaad_data`, `vectorbt`
- Futures data (FO UDiFF) only available for recent 1-2 days via NSE daily-reports API — historical dates get 0 score
- Pipeline runs stages sequentially; within scoring stage, dates are parallelised (4 threads)

---

## CLI (src/runner.py)

```bash
python -m src.runner \
  --stages fetch,enrich,technical,score,hits   # comma-separated, default=all
  --start-date 2025-01-01 \
  --end-date 2026-06-30 \
  --days-back 1                                 # alternative to start/end
  --from-disk-only                              # skip download, process cached only
```

Stage names: `fetch`, `equity_master`, `enrich`, `technical`, `price_level`, `momentum`, `volatility`, `averages`, `derivatives`, `score`, `hits`. Pass `all` or comma-separated subset.

---

## Daily Workflow

Activate the virtual environment first, then use `python -m` for all commands:

```bash
# Activate (Linux / macOS)
source .venv/bin/activate
# Activate (Windows)
# .venv\Scripts\activate

# Full pipeline (after market close)
python -m src.runner --stages all --days-back 1

# Run scoring only (if pipeline already ran)
python -c "from src.scoring.scorer import run_scoring; run_scoring('2026-06-25')"

# Refresh equity master data
python -m src.runner --stages equity_master

# Rebuild F&O membership data
python build_fno_membership.py

# Rebuild index membership data (sector/Nifty 500)
python build_index_history.py

# Rebuild shareholding data
python build_shareholding.py

# Query top picks for a given date
python -c "
from db.connection import get_connection
conn = get_connection()
df = pd.read_sql('SELECT * FROM scoring_picks WHERE trade_date = \"2026-06-25\" ORDER BY rank', conn)
print(df)
"

# Query verification performance
python -c "
from db.connection import get_connection
conn = get_connection()
df = pd.read_sql('SELECT * FROM scoring_performance ORDER BY pick_date', conn)
print(f'Avg return: {df.return_pct.mean():.2f}%  Win rate: {(df.return_pct > 0).mean()*100:.1f}%')
"

# Compute + analyze hit rates
python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-06-04
python -m src.validation.run_hits --analyze

# Hits as a pipeline stage
python -m src.runner --stages hits --start-date 2026-05-01 --end-date 2026-06-04
```

---

## Flask REST API

Complete specification for the REST API layer. Exposes the pipeline, scoring results, and stock data via HTTP endpoints.

### API File Structure

```
src/api/
├── __init__.py
├── app.py                Flask factory: create_app(), register blueprints
├── views.py              CREATE VIEW IF NOT EXISTS stock_universe
├── routes_stocks.py      /api/v1/stocks, /history, /detail, /search
├── routes_pipeline.py    /api/v1/pipeline/run, /pipeline/status
└── routes_meta.py        /api/v1/columns, /api/v1/health

config/
├── scoring.yaml          (existing)
└── columns.yaml          NEW — 97 column definitions in 10 groups
```

### Database View — `stock_universe`

Created during `init_schema()` via `CREATE VIEW IF NOT EXISTS stock_universe`.

Joins all major tables on `(exchange, trade_date, symbol)` with LEFT JOIN.

**Column order** (most useful → specialized):

| Order | Group | Columns |
|-------|-------|---------|
| 1 | Identity | `exchange`, `trade_date`, `symbol`, `sector`, `industry`, `isin` |
| 2 | Price | `open_price`, `high_price`, `low_price`, `close_price`, `previous_close`, `traded_volume`, `traded_value`, `day_return_pct`, `wvap_price` |
| 3 | Delivery | `delivery_volume`, `delivery_value`, `delivery_pct`, `delivery_qty_20d_avg`, `delivery_pct_trend` |
| 4 | Technical | `sma_20`, `sma_50`, `sma_100`, `sma_200`, `ema_9`, `ema_20`, `ema_50`, `price_vs_sma20_pct`, `golden_cross`, `ema20_gt_ema50`, `price_gt_sma200`, `trend_stage`, `trend_score`, `trend_strength` |
| 5 | Price Level | `week52_high`, `week52_low`, `pct_from_52wk_high`, `near_52wk_high_flag`, `week4_high`, `breakout_flag`, `pivot_p`, `pivot_r1`, `pivot_s1` |
| 6 | Momentum | `rsi_14`, `macd`, `macd_signal`, `macd_histogram`, `macd_crossover`, `stoch_k`, `stoch_d`, `adx_14`, `mfi_14`, `cci_20`, `rsi_9`, `williams_r_14`, `stoch_signal`, `momentum_score`, `decay_factor` |
| 7 | Volatility | `atr_14`, `atr_pct`, `bb_upper`, `bb_middle`, `bb_lower`, `bb_width`, `bb_squeeze`, `keltner_upper`, `keltner_lower`, `historical_vol_20d` |
| 8 | Averages | `close_avg_21d`, `close_avg_63d`, `close_avg_126d`, `close_avg_252d`, `volume_avg_21d`, `volume_avg_63d`, `volume_avg_126d`, `rsi_avg_21d`, `delivery_pct_avg_21d`, `volatility_avg_21d`, `vol_5d_avg`, `vol_10d_avg`, `vol_20d_avg`, `vol_ratio`, `vol_breakout_up`, `volume_score` |
| 9 | Derivatives | `basis_pct`, `oi_change_pct` |
| 10 | Scoring | `overall_score`, `rank`, `percentile`, `comp_momentum_score`, `value_score`, `quality_score`, `technical_strength`, `volume_liquidity`, `institutional_score`, `fno_score`, `nifty500_member` |

All values from `scoring_result` columns are prefixed with `comp_` to avoid naming collisions with other pipeline stage columns.

**Full SQL:**
```sql
CREATE VIEW IF NOT EXISTS stock_universe AS
SELECT
    d.exchange,
    d.trade_date,
    d.symbol,
    d.open_price,
    d.high_price,
    d.low_price,
    d.close_price,
    d.previous_close,
    d.traded_volume,
    d.traded_value,
    d.day_return_pct,
    d.wvap_price,
    d.delivery_volume,
    d.delivery_value,
    d.delivery_pct,
    d.delivery_qty_20d_avg,
    d.delivery_pct_trend,
    d.isin,
    t.sma_20,
    t.sma_50,
    t.sma_100,
    t.sma_200,
    t.ema_9,
    t.ema_20,
    t.ema_50,
    t.price_vs_sma20_pct,
    t.golden_cross,
    t.ema20_gt_ema50,
    t.price_gt_sma200,
    t.trend_stage,
    t.trend_score,
    t.trend_strength,
    pl.week52_high,
    pl.week52_low,
    pl.pct_from_52wk_high,
    pl.near_52wk_high_flag,
    pl.week4_high,
    pl.breakout_flag,
    pl.pivot_p,
    pl.pivot_r1,
    pl.pivot_s1,
    m.rsi_14,
    m.macd,
    m.macd_signal,
    m.macd_histogram,
    m.macd_crossover,
    m.stoch_k,
    m.stoch_d,
    m.adx_14,
    m.mfi_14,
    m.cci_20,
    m.rsi_9,
    m.williams_r_14,
    m.stoch_signal,
    m.momentum_score                              AS comp_momentum_score,
    m.decay_factor,
    v.atr_14,
    v.atr_pct,
    v.bb_upper,
    v.bb_middle,
    v.bb_lower,
    v.bb_width,
    v.bb_squeeze,
    v.keltner_upper,
    v.keltner_lower,
    v.historical_vol_20d,
    a.close_avg_21d,
    a.close_avg_63d,
    a.close_avg_126d,
    a.close_avg_252d,
    a.volume_avg_21d,
    a.volume_avg_63d,
    a.volume_avg_126d,
    a.rsi_avg_21d,
    a.delivery_pct_avg_21d,
    a.volatility_avg_21d,
    a.vol_5d_avg,
    a.vol_10d_avg,
    a.vol_20d_avg,
    a.vol_ratio,
    a.vol_breakout_up,
    a.volume_score,
    f.basis_pct,
    f.oi_change_pct,
    sr.overall_score,
    sr.rank,
    sr.percentile,
    sr.momentum_score                             AS comp_momentum_score_sr,
    sr.value_score,
    sr.quality_score,
    sr.technical_strength,
    sr.volume_liquidity,
    sr.institutional_score,
    sr.fno_score,
    sr.nifty500_member,
    em.sector,
    em.industry
FROM daily d
LEFT JOIN technical t
    ON d.exchange = t.exchange AND d.trade_date = t.trade_date AND d.symbol = t.symbol
LEFT JOIN price_level pl
    ON d.exchange = pl.exchange AND d.trade_date = pl.trade_date AND d.symbol = pl.symbol
LEFT JOIN momentum m
    ON d.exchange = m.exchange AND d.trade_date = m.trade_date AND d.symbol = m.symbol
LEFT JOIN volatility v
    ON d.exchange = v.exchange AND d.trade_date = v.trade_date AND d.symbol = v.symbol
LEFT JOIN averages a
    ON d.exchange = a.exchange AND d.trade_date = a.trade_date AND d.symbol = a.symbol
LEFT JOIN futures_data f
    ON d.trade_date = f.trade_date AND d.symbol = f.symbol
LEFT JOIN scoring_result sr
    ON d.exchange = sr.exchange AND d.trade_date = sr.trade_date AND d.symbol = sr.symbol
LEFT JOIN equity_master em
    ON d.symbol = em.symbol
```

> **Note on `comp_momentum_score` vs `comp_momentum_score_sr`:** The `momentum` table stores a `momentum_score` (RSI+MACD+Stoch composite from the momentum pipeline stage). The `scoring_result` table also stores a `momentum_score` (contrarian entry component from the V2a formula). The view renames both to `comp_momentum_score` and `comp_momentum_score_sr` respectively so the caller can distinguish.

### Column Registry — `config/columns.yaml`

Single source of truth for all queryable columns. Defines name, group, type for each column in `stock_universe`. Loaded once at API startup.

```yaml
# config/columns.yaml
# Column registry for the Flask REST API.
# Each entry defines a column in the stock_universe view.
# Groups: identity, price, delivery, technical, price_level, momentum,
#         volatility, averages, derivatives, scoring

columns:
  - name: symbol
    group: identity
    type: TEXT
  - name: trade_date
    group: identity
    type: TEXT
  - name: sector
    group: identity
    type: TEXT
  - name: industry
    group: identity
    type: TEXT
  - name: exchange
    group: identity
    type: TEXT
  - name: isin
    group: identity
    type: TEXT
  - name: open_price
    group: price
    type: REAL
  - name: high_price
    group: price
    type: REAL
  - name: low_price
    group: price
    type: REAL
  - name: close_price
    group: price
    type: REAL
  - name: previous_close
    group: price
    type: REAL
  - name: traded_volume
    group: price
    type: INTEGER
  - name: traded_value
    group: price
    type: REAL
  - name: day_return_pct
    group: price
    type: REAL
  - name: wvap_price
    group: price
    type: REAL
  - name: delivery_volume
    group: delivery
    type: INTEGER
  - name: delivery_value
    group: delivery
    type: REAL
  - name: delivery_pct
    group: delivery
    type: REAL
  - name: delivery_qty_20d_avg
    group: delivery
    type: REAL
  - name: delivery_pct_trend
    group: delivery
    type: REAL
  - name: sma_20
    group: technical
    type: REAL
  - name: sma_50
    group: technical
    type: REAL
  - name: sma_100
    group: technical
    type: REAL
  - name: sma_200
    group: technical
    type: REAL
  - name: ema_9
    group: technical
    type: REAL
  - name: ema_20
    group: technical
    type: REAL
  - name: ema_50
    group: technical
    type: REAL
  - name: price_vs_sma20_pct
    group: technical
    type: REAL
  - name: golden_cross
    group: technical
    type: INTEGER
  - name: ema20_gt_ema50
    group: technical
    type: INTEGER
  - name: price_gt_sma200
    group: technical
    type: INTEGER
  - name: trend_stage
    group: technical
    type: TEXT
  - name: trend_score
    group: technical
    type: REAL
  - name: trend_strength
    group: technical
    type: REAL
  - name: week52_high
    group: price_level
    type: REAL
  - name: week52_low
    group: price_level
    type: REAL
  - name: pct_from_52wk_high
    group: price_level
    type: REAL
  - name: near_52wk_high_flag
    group: price_level
    type: INTEGER
  - name: week4_high
    group: price_level
    type: REAL
  - name: breakout_flag
    group: price_level
    type: INTEGER
  - name: pivot_p
    group: price_level
    type: REAL
  - name: pivot_r1
    group: price_level
    type: REAL
  - name: pivot_s1
    group: price_level
    type: REAL
  - name: rsi_14
    group: momentum
    type: REAL
  - name: macd
    group: momentum
    type: REAL
  - name: macd_signal
    group: momentum
    type: REAL
  - name: macd_histogram
    group: momentum
    type: REAL
  - name: macd_crossover
    group: momentum
    type: INTEGER
  - name: stoch_k
    group: momentum
    type: REAL
  - name: stoch_d
    group: momentum
    type: REAL
  - name: adx_14
    group: momentum
    type: REAL
  - name: mfi_14
    group: momentum
    type: REAL
  - name: cci_20
    group: momentum
    type: REAL
  - name: rsi_9
    group: momentum
    type: REAL
  - name: williams_r_14
    group: momentum
    type: REAL
  - name: stoch_signal
    group: momentum
    type: REAL
  - name: comp_momentum_score
    group: momentum
    type: REAL
  - name: decay_factor
    group: momentum
    type: REAL
  - name: atr_14
    group: volatility
    type: REAL
  - name: atr_pct
    group: volatility
    type: REAL
  - name: bb_upper
    group: volatility
    type: REAL
  - name: bb_middle
    group: volatility
    type: REAL
  - name: bb_lower
    group: volatility
    type: REAL
  - name: bb_width
    group: volatility
    type: REAL
  - name: bb_squeeze
    group: volatility
    type: INTEGER
  - name: keltner_upper
    group: volatility
    type: REAL
  - name: keltner_lower
    group: volatility
    type: REAL
  - name: historical_vol_20d
    group: volatility
    type: REAL
  - name: close_avg_21d
    group: averages
    type: REAL
  - name: close_avg_63d
    group: averages
    type: REAL
  - name: close_avg_126d
    group: averages
    type: REAL
  - name: close_avg_252d
    group: averages
    type: REAL
  - name: volume_avg_21d
    group: averages
    type: REAL
  - name: volume_avg_63d
    group: averages
    type: REAL
  - name: volume_avg_126d
    group: averages
    type: REAL
  - name: rsi_avg_21d
    group: averages
    type: REAL
  - name: delivery_pct_avg_21d
    group: averages
    type: REAL
  - name: volatility_avg_21d
    group: averages
    type: REAL
  - name: vol_5d_avg
    group: averages
    type: REAL
  - name: vol_10d_avg
    group: averages
    type: REAL
  - name: vol_20d_avg
    group: averages
    type: REAL
  - name: vol_ratio
    group: averages
    type: REAL
  - name: vol_breakout_up
    group: averages
    type: INTEGER
  - name: volume_score
    group: averages
    type: REAL
  - name: basis_pct
    group: derivatives
    type: REAL
  - name: oi_change_pct
    group: derivatives
    type: REAL
  - name: overall_score
    group: scoring
    type: REAL
  - name: rank
    group: scoring
    type: INTEGER
  - name: percentile
    group: scoring
    type: REAL
  - name: comp_momentum_score_sr
    group: scoring
    type: REAL
  - name: value_score
    group: scoring
    type: REAL
  - name: quality_score
    group: scoring
    type: REAL
  - name: technical_strength
    group: scoring
    type: REAL
  - name: volume_liquidity
    group: scoring
    type: REAL
  - name: institutional_score
    group: scoring
    type: REAL
  - name: fno_score
    group: scoring
    type: REAL
  - name: nifty500_member
    group: scoring
    type: INTEGER
```

### Endpoints

#### `GET /api/v1/stocks` — Flexible stock data query

Query from `stock_universe` with dynamic column selection, filtering, ordering, pagination.

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `date` | string | — | Trade date `YYYY-MM-DD` **(required)** |
| `symbol` | string | — | Filter by symbol |
| `sector` | string | — | Filter by sector name |
| `columns` | string | all | Comma-separated column names (order preserved) |
| `fields` | string | — | Repeated param, appended after `columns` |
| `order_by` | string | `rank` | Column to sort by |
| `order_dir` | string | `asc` | `asc` or `desc` |
| `limit` | int | `100` | Max rows (cap at 1000) |
| `offset` | int | `0` | Pagination offset |
| `min_score` | float | — | `overall_score >= min_score` filter |
| `format` | string | `json` | `json` or `csv` |

**Two field selection methods (coexist):**
```
# Method 1 — comma-separated (defines order)
GET /api/v1/stocks?date=2026-06-25&columns=symbol,close_price,overall_score,rank&order_by=rank&order_dir=asc&limit=20

# Method 2 — repeated fields param (appended to columns)
GET /api/v1/stocks?date=2026-06-25&fields=symbol&fields=close_price&fields=overall_score
```

If neither `columns` nor `fields` is provided, all columns are returned in view order.

If `columns` is provided, it defines the order and selection. If `fields` is also present, those are appended after the `columns` list (no duplicates).

**Column name validation:** Unknown column names return `400 Bad Request` with the list of invalid names.

**CSV output:** Sets `Content-Type: text/csv` and `Content-Disposition: attachment; filename="stocks_{date}.csv"`.

#### `GET /api/v1/stocks/history` — Full history for a symbol

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | string | — | Stock symbol **(required)** |
| `start_date` | string | — | Start date `YYYY-MM-DD` |
| `end_date` | string | — | End date `YYYY-MM-DD` |
| `columns` | string | all | Comma-separated column selection |
| `fields` | string | — | Repeated field params |
| `format` | string | `json` | `json` or `csv` |

If no date range given, returns all available history for the symbol.

#### `GET /api/v1/stocks/detail` — Single stock snapshot

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `symbol` | string | — | Stock symbol **(required)** |
| `date` | string | latest | Trade date |

Returns one row with all columns — the full stock_universe for that symbol+date.

#### `GET /api/v1/stocks/search` — Fuzzy search

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `q` | string | — | Search query **(required)** |
| `date` | string | latest | Trade date for data context |

Performs `LIKE '%q%'` search on `symbol`, `sector`, and `industry` columns (from `equity_master`). Returns deduplicated list of matching symbols with their latest available data row.

#### `GET /api/v1/picks` — Top scoring picks

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `date` | string | latest | Trade date |
| `columns` | string | all | Column selection |
| `limit` | int | `20` | Number of picks |
| `format` | string | `json` | `json` or `csv` |

Queries `scoring_picks` joined with `stock_universe` on `(trade_date, symbol)`.

#### `POST /api/v1/pipeline/run` — Trigger pipeline (async)

Accepts JSON body. Returns immediately with a `job_id`. Pipeline runs in background thread.

**Request body:**
```json
{
  "stages": "all",
  "start_date": "2026-06-25",
  "end_date": "2026-06-26",
  "days_back": 1
}
```

All fields optional (defaults match `runner.py` CLI semantics — default stages=all, end_date=today, start_date=5 days back).

**Response (202 Accepted):**
```json
{
  "job_id": "pipeline_20260704_123456",
  "status": "pending",
  "message": "Pipeline queued"
}
```

**Concurrency:** Only one pipeline run at a time. If a job is already `running`, returns `409 Conflict`.

#### `GET /api/v1/pipeline/status` — Check job status

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `job_id` | string | latest | Specific job ID, or omit for latest |

Returns the `pipeline_jobs` record.

**Response:**
```json
{
  "job_id": "pipeline_20260704_123456",
  "stages": "all",
  "start_date": "2026-06-25",
  "end_date": "2026-06-26",
  "status": "running",
  "started_at": "2026-07-04T12:34:56",
  "completed_at": null,
  "error_log": null
}
```

#### `GET /api/v1/columns` — Column discovery

Returns the full column registry grouped by `group`. Each entry includes `name`, `group`, `type`.

#### `GET /api/v1/health` — Health check

Returns:
```json
{
  "status": "ok",
  "db_size_mb": 156.2,
  "schema_version": "v1.17",
  "tables": { "daily": 320000, "scoring_result": 120000, ... },
  "last_data_date": "2026-07-03",
  "last_scored_date": "2026-07-03"
}
```

#### `GET /api/v1/pipeline/stages` — List available stages

Returns the 11 available pipeline stage names (`fetch`, `equity_master`, `enrich`, `technical`, `price_level`, `momentum`, `volatility`, `averages`, `derivatives`, `score`, `hits`).

#### `GET /api/v1/pipeline/jobs` — List recent pipeline jobs

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `limit` | int | `10` | Max rows (cap at 100) |
| `status` | string | — | Filter by status (`pending`/`running`/`completed`/`failed`) |

Returns `{ "jobs": [...], "count": N }`.

#### `POST /api/v1/pipeline/build/fno-membership` — Rebuild F&O membership (async)

Downloads `fno_membership_history.csv` from GitHub and populates the `fno_membership` table. Returns `202 Accepted` with `job_id`. Uses `pipeline_jobs` table for status tracking.

#### `POST /api/v1/pipeline/build/index-history` — Rebuild index membership (async)

Downloads `index_membership_history.csv` from GitHub and populates the `index_membership` table. Same async pattern.

#### `POST /api/v1/pipeline/build/shareholding` — Rebuild shareholding table (async)

Downloads shareholding flat CSV from GitHub and populates the `shareholding` table. Same async pattern.

#### `POST /api/v1/pipeline/build/equity-master` — Rebuild equity master CSV (async)

Downloads `EQ_MAST.csv` from NSE and writes `data/eq_mast.csv`. Same async pattern.

#### `POST /api/v1/hits/compute` — Compute hit analysis (async)

**Request body (JSON):**
```json
{
  "start_date": "2025-01-01",
  "end_date": "2026-07-03"
}
```

Both fields required. Computes forward-return hits for all `scoring_picks` in the range and writes to `predicted_stock`. Returns `202 Accepted` with `job_id`. On completion, `pipeline_jobs.error_log` contains the captured log output.

#### `GET /api/v1/hits/analyze` — Hit rate summary

Returns aggregate hit rates as JSON:
```json
{
  "total_picks": 7359,
  "summary": [
    { "level": 0, "label": "None", "count": 5692, "pct": 77.35 },
    { "level": 1, "label": "Tg1", "count": 95, "pct": 1.29 }
  ],
  "any_hits": { "count": 1667, "pct": 22.65 },
  "breakdown": [
    { "window_days": 5, "target": "Tg1", "target_pct": 4, "picks": 7359, "hits": 1667, "hit_pct": 22.65 }
  ]
}
```

#### `GET /api/v1/hits/detail` — Individual hit details

| Param | Type | Default | Description |
|-------|------|---------|-------------|
| `min_hit` | int | `1` | Minimum `target_hit` level (0=none, 1=tg1, 2=tg2, 3=tg3) |
| `limit` | int | `50` | Max rows (cap at 500) |
| `format` | string | `json` | `json` or `csv` |

Returns `{ "hits": [...], "count": N }` with full details per pick (entry_price, tg1_price, tg1_date, tg1_high, etc.).

#### `GET /api/v1/dates` — Available date ranges

Returns min/max `trade_date` (or `pick_date` for `predicted_stock`/`scoring_performance`) for each time-series table:
```json
{
  "daily": { "date_column": "trade_date", "min_date": "2025-01-01", "max_date": "2026-07-03", "row_count": 823962 },
  "predicted_stock": { "date_column": "pick_date", "min_date": "2025-01-01", "max_date": "2026-07-02", "row_count": 7359 }
}
```

### Pipeline Jobs Table

Created in `init_schema()` alongside other tables:

```sql
CREATE TABLE IF NOT EXISTS pipeline_jobs (
    job_id      TEXT PRIMARY KEY,
    stages      TEXT NOT NULL,
    start_date  TEXT,
    end_date    TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    started_at  TEXT,
    completed_at TEXT,
    error_log   TEXT
)
```

Status values: `pending` → `running` → `completed` / `failed`.

### Async Pipeline Execution Mechanism

1. `POST /pipeline/run` receives request → generates `job_id` (`pipeline_YYYYMMDD_HHMMSS_ffffff`) with microsecond precision → inserts `pipeline_jobs` row with `status='pending'`
2. Checks if any existing job has `status IN ('pending', 'running')` → if so, returns `409`
3. Starts a `threading.Thread` target function that:
   - Updates job to `status='running'`, `started_at = now`
   - Adds a `logging.StreamHandler` with `io.StringIO` to the "runner" logger to capture log output
   - Calls `runner.main()` with parsed args
   - On success: updates to `status='completed'`, `completed_at = now`, stores captured logs in `error_log`
   - On failure: updates to `status='failed'`, `completed_at = now`, stores exception traceback + captured logs in `error_log`
4. Returns `202 Accepted` with the `job_id` immediately

### CLI for Running the API

```bash
# Standalone Flask dev server
python -m src.api.app

# With optional host/port
python -m src.api.app --host 0.0.0.0 --port 5000
```

Default: `127.0.0.1:5000`, debug mode on for development.

### Deployment Flow (Tested End-to-End)

The pipeline has been verified to work from a bare database (zero pre-existing data) entirely via REST API calls.

**Test procedure:**
1. Back up existing DB (`mydb1.db` → `mydb1.db.bak`)
2. Start Flask server (`python -m src.api.app`)
3. Server auto-creates fresh DB with all 18 tables + `stock_universe` view via `init_schema()`
4. `POST /api/v1/pipeline/run` with `{"stages":"all","start_date":"2026-06-20","end_date":"2026-06-30"}`
5. Pipeline runs all 11 stages sequentially: `fetch` → `equity_master` → `enrich` → `technical` → `price_level` → `momentum` → `volatility` → `averages` → `derivatives` → `score` → `hits`
6. Poll `GET /api/v1/pipeline/status` until `status=completed`
7. Verify data via `GET /api/v1/health` (table counts), `GET /api/v1/dates` (date ranges), `GET /api/v1/picks` (scoring picks)

**Test results (6 trading days, 2026-06-20 to 2026-06-30):**
| Stage | Rows | Time |
|-------|------|------|
| fetch | 14,454 stage + 14,454 delivery | 5s |
| equity_master | 2,381 | <1s |
| enrich | 14,454 daily | 10s |
| technical | 14,454 | 38s |
| price_level | 14,454 | 25s |
| momentum | 14,454 | 18s |
| volatility | 14,454 | 12s |
| averages | 14,454 | 38s |
| derivatives | 210 futures | 3s |
| score | 6 dates, ~1,450 scored avg | 2s |
| hits | 164 predicted_stock | <1s |
| **Total** | **11 stages** | **2m40s** |

After completion, all 18 tables are populated with data, scoring picks are available for each trading date, and hit analysis is stored.

**Data integrity verification:** Rows were deleted via SQLite for a specific date (`DELETE FROM scoring_result WHERE trade_date='2026-06-30'`), then re-scored via the API. The same 1,419 scoring results and 20 picks were re-inserted with identical scores.

### Flask App Factory — `src/api/app.py`

```python
def create_app():
    app = Flask(__name__)
    app.config.from_mapping(
        DEBUG=True,
        COLUMNS_CONFIG=load_columns_config(),
    )
    app.register_blueprint(stocks_bp, url_prefix='/api/v1')
    app.register_blueprint(pipeline_bp, url_prefix='/api/v1')
    app.register_blueprint(meta_bp, url_prefix='/api/v1')
    return app
```

Three blueprints registered under `/api/v1`:
- `stocks_bp` — `/stocks`, `/stocks/history`, `/stocks/detail`, `/stocks/search`, `/picks`
- `pipeline_bp` — `/pipeline/run`, `/pipeline/status`, `/pipeline/stages`, `/pipeline/jobs`, `/pipeline/build/*`, `/hits/*`
- `meta_bp` — `/columns`, `/health`, `/dates`

### Pipeline Runner Integration

The API calls the same `src.runner.main()` function used by the CLI. The runner already has a clean `main(argv=None)` interface that accepts CLI-style args. The API constructs the argv list from the request params:

```python
argv = ["--stages", stages]
if start_date:
    argv += ["--start-date", start_date]
if end_date:
    argv += ["--end-date", end_date]
if days_back:
    argv += ["--days-back", str(days_back)]
runner.main(argv)
```

`init_schema()` is called inside `runner.main()`, so it's safe to run.

### Implementation Conventions

- `columns.yaml` is loaded once at app startup via `create_app()` and stored in `app.config['COLUMNS_CONFIG']`
- The `stock_universe` view is created in `init_schema()` (in `db/connection.py`) by importing and calling `create_stock_universe_view(conn)`
- All numeric values from the API are serialized as JSON numbers (not strings)
- `NaN` / `None` values are serialized as `null` in JSON
- Query building uses parameterized SQL to prevent injection
- Column validation checks against the loaded column registry before building SQL
- CSV export uses Python's `csv` module with `DictWriter`
- Pipeline job_id generation uses microsecond precision (`%f` in strftime) to avoid collisions
- Pipeline concurrency check looks for both `pending` AND `running` statuses (not just `running`), preventing race conditions between job insertion and thread startup
- Pipeline log capture uses `logging.StreamHandler` with `io.StringIO` attached to the `runner` logger (not sys.stdout/stderr redirection, which doesn't work with the logging module's stream handler) — captured output is stored in `pipeline_jobs.error_log` on both success and failure
- Pipeline background threads are daemon threads (`daemon=True`) — they are killed when the main process exits
- The `stock_universe` view joins on `(exchange, trade_date, symbol)` for all tables except `equity_master` (joined on `symbol` only) and `futures_data` (joined on `(trade_date, symbol)` — no exchange column)
```
