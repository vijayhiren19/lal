# Phase1 — Stock Scrip Scoring System

Complete specification. An LLM reading this should be able to regenerate the full codebase with identical pipeline behaviour.

---

## Project Structure

```
phase1/
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
│   │   ├── fetcher.py        Download + parse NSE bhavcopy + delivery MTO
│   │   ├── equity_master.py  Sector/industry from NSE equity master CSV
│   │   ├── enricher.py       Merge stage + delivery → daily table
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
└── requirements.txt          pyyaml>=6.0, pandas>=2.0, pandas-ta>=0.3, requests>=2.31, numpy>=1.24
```

---

## Pipeline Architecture

```
STAGE ORDER (src/runner.py):
  fetch → equity_master → enrich → technical → price_level → momentum
    → volatility → averages → derivatives → score → hits

  fetch:         Download BhavCopy CSVs + MTO.DAT files from NSE, parse →
                 stage table (raw price) + delivery table (qty, pct)
  equity_master: Download EQ_MAST.csv → equity_master table (sector, industry)
  enrich:        Join stage + delivery, compute rolling volume/delivery avgs →
                 daily table + enriched delivery table
  technical:     SMA(20/50/100/200), EMA(9/20/50/200), crossovers →
                 technical table
  price_level:   252/126/20-day rolling highs/lows, pivot points →
                 price_level table
  momentum:      RSI(14/9), MACD(12/26/9), Stoch(14/3/3), MFI(14), ADX(14),
                 CCI(20), Williams %R(14) → momentum table
  volatility:    ATR(14), Bollinger Bands(20,2σ), Keltner Channels,
                 historical vol(20d) → volatility table
  averages:      Rolling means of close, volume, RSI, delivery%, volatility
                 at 21/63/126/252/756 windows → averages table
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

### delivery
| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| qty | INTEGER | |
| pct | REAL | delivery % |
| qty_5d_avg | REAL | rolling mean (enricher) |
| qty_20d_avg | REAL | |
| pct_5d_avg | REAL | |
| pct_20d_avg | REAL | |
| pct_trend | REAL | linear slope over 5 days |
| vol_spike | INTEGER | qty > 2× qty_20d_avg |

Source: NSE MTO_{DDMMYYYY}.DAT files (pipe-delimited with comma-separated fields starting with '20'). Columns (positional): date, ref, symbol, series, isin, qty, pct.

### daily
| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| open_price, high_price, low_price, close_price, previous_close | REAL | |
| traded_volume | INTEGER | |
| traded_value | REAL | |
| vol_5d_avg, vol_10d_avg, vol_20d_avg | REAL | Rolling volume means |
| vol_ratio | REAL | vol / vol_20d_avg |
| vol_breakout_up | INTEGER | vol_ratio >= 1.5 AND close > open |
| vol_trend_5d | REAL | Linear slope of volume over 5d |
| volume_score | REAL | 0-100 composite = vol_ratio_score + trend_score + vs_avg_score + breakout_score |

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

Source: `github.com/aditya-jha/nse-historical-membership` → `shareholding_history/data/parsed/_flat.csv` (CC BY 4.0)

### fno_membership
| symbol | TEXT PK |
| valid_from | TEXT PK |
| valid_to | TEXT (nullable = still active) |

### index_membership
| symbol | TEXT PK |
| index_name | TEXT PK |
| index_id | INTEGER PK |
| valid_from | TEXT PK |
| valid_to | TEXT |
| weightage | REAL |

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

### FO UDiFF (derivatives) via NSE API
```
GET https://www.nseindia.com/api/daily-reports?key=FO
```
Returns JSON with CurrentDay/PreviousDay arrays. Each item has: fileKey='FO-UDIFF-BHAVCOPY-CSV', tradingDate (DD-Mon-YYYY), filePath, fileActlName. Full URL = filePath + fileActlName. Response is a ZIP containing one CSV. Columns: FinInstrmTp='STF' = stock futures, TckrSymb, XpryDt, OpnPric, HghPric, LwPric, ClsPric, OpnIntrst, ChngInOpnIntrst, TtlTradgVol, UndrlygPric.

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
5. Batch insert into stage and delivery tables: `executemany(sql, batch[i:i+500])` per date, per table, then commit.

---

## V2a Scoring Formula (scorer.py)

### Data Merge

One SQLite query per data source at the given trade_date, merged on symbol:

```python
daily         [exchange, trade_date, symbol, close_price, previous_close, traded_value]
delivery      [qty, pct, qty_20d_avg, pct_trend]
stage         [day_return_pct, isin]
shareholding  [fii_pct, dii_pct, promoter_pct, ...]  — latest 2 quarters, diff computed
futures_data  [basis_pct, oi_change_pct]
momentum      [rsi_14, adx_14, macd, ...]
volatility    [atr_pct]
sector        [sector]  — from index_membership, fallback to equity_master
```

ISIN filter: `isin.str.startswith('INE', na=False)`.  
Delivery value filter: `qty × close_price >= min_dev_trade_value_cr × 1e7` (default 0.5 Cr).

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
quality_score = pct_trend_boost (10)  IF pct_trend >= pct_trend_threshold (0.5)
               ELSE 0

# 4. Delivery qty surge (↗ technical_strength, weight ~10-15%)
ratio = qty / qty_20d_avg  (only where avg > 0)
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

### Output (7,320 picks, Jan 2025–Jun 2026, open+3%)
| Target Hit | Count | % |
|------------|-------|---|
| None | 4,984 | 68.4% |
| Tg1 (4% in 5d) | 142 | 1.9% |
| Tg2 (5% in 10d) | 909 | 12.5% |
| Tg3 (10% in 15d) | 1,255 | 17.2% |
| **Any** | **2,306** | **31.6%** |

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

- All numeric values rounded to 2 decimal places at creation
- `_safe_rnd(result)` wraps `pandas_ta` calls: `result.round(2) if result is not None else None`
- `np.where` with Series: convert `.to_numpy()` first to avoid pandas-index alignment issues
- `.astype(float)` after `np.where` to ensure float64 dtype
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

## Flask UI Server

```bash
# Start Flask UI in background (non-blocking for main agent)
# Use Start-Process (not Start-Job) to keep it running
Start-Process -NoNewWindow -FilePath ".venv\Scripts\python.exe" -ArgumentList "run_flask.py"

# Verify server is running
(Invoke-WebRequest -Uri "http://127.0.0.1:5000/" -UseBasicParsing).StatusCode

# Stop the server
Get-Process python | Where-Object { $_.CommandLine -match "run_flask" } | Stop-Process -Force
```

**Agent instruction**: When starting the Flask server, use `Start-Process` (background, non-blocking) or delegate to a sub-agent via `task` tool so the main agent stays responsive. Do NOT use `Start-Job` — it terminates when the shell exits. To verify the UI, launch a sub-agent (`general` type) with Playwright browser tools to navigate pages and check for console errors.

## Daily Workflow

```bash
# Full pipeline (after market close)
.venv\Scripts\python -m src.runner --stages all --days-back 1

# Run scoring only (if pipeline already ran)
.venv\Scripts\python -c "from src.scoring.scorer import run_scoring; run_scoring('2026-06-25')"

# Refresh equity master data
.venv\Scripts\python -m src.runner --stages equity_master

# Rebuild F&O membership data
.venv\Scripts\python build_fno_membership.py

# Rebuild index membership data (sector/Nifty 500)
.venv\Scripts\python build_index_history.py

# Rebuild shareholding data
.venv\Scripts\python build_shareholding.py

# Query top picks for a given date
.venv\Scripts\python -c "
from db.connection import get_connection
conn = get_connection()
df = pd.read_sql('SELECT * FROM scoring_picks WHERE trade_date = \"2026-06-25\" ORDER BY rank', conn)
print(df)
"

# Query verification performance
.venv\Scripts\python -c "
from db.connection import get_connection
conn = get_connection()
df = pd.read_sql('SELECT * FROM scoring_performance ORDER BY pick_date', conn)
print(f'Avg return: {df.return_pct.mean():.2f}%  Win rate: {(df.return_pct > 0).mean()*100:.1f}%')
"

# Compute + analyze hit rates
.venv\Scripts\python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-06-04
.venv\Scripts\python -m src.validation.run_hits --analyze

# Hits as a pipeline stage
.venv\Scripts\python -m src.runner --stages hits --start-date 2026-05-01 --end-date 2026-06-04
```
