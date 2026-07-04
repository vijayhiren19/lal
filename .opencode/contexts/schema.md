# Database Schema

SQLite (WAL mode), `mydb1.db`. All dates TEXT (ISO YYYY-MM-DD). Prices/scores REAL. Booleans INTEGER (0/1).

---

## Table: stage

Raw OHLCV from NSE BhavCopy CSVs. SERIES='EQ' only.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | 'NSE' |
| trade_date | TEXT PK | ISO date |
| symbol | TEXT PK | |
| open_price | REAL NOT NULL | |
| high_price | REAL NOT NULL | |
| low_price | REAL NOT NULL | |
| close_price | REAL NOT NULL | |
| previous_close | REAL NOT NULL | |
| traded_volume | INTEGER NOT NULL | |
| traded_value | REAL NOT NULL | Rupees |
| day_return_pct | REAL | (close - prev_close) / prev_close × 100 |
| upper_circuit_hit | INTEGER DEFAULT 0 | day_return_pct >= 19.5 |
| lower_circuit_hit | INTEGER DEFAULT 0 | day_return_pct <= -19.5 |
| isin | TEXT | INE-prefix |

Source: `BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip`. Three column-name formats auto-detected (old TCKRSYMB, intermediate SYMBOL, new TckrSymb). Inserted by `fetcher.py`.

---

## Table: stage_delivery

Raw MTO delivery data from NSE. Populated by `fetcher.py`, consumed by `enricher.py`.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | 'NSE' |
| trade_date | TEXT PK | ISO date |
| symbol | TEXT PK | |
| qty | INTEGER NOT NULL | delivery quantity |
| pct | REAL NOT NULL | delivery % of traded volume |

Source: `MTO_{DDMMYYYY}.DAT`. Lines starting with '20', comma-separated fields. SERIES='EQ' only.

---

## Table: daily

Enriched base table. Built by `enricher.py` by merging `stage` + `stage_delivery`.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| open_price | REAL NOT NULL | |
| high_price | REAL NOT NULL | |
| low_price | REAL NOT NULL | |
| close_price | REAL NOT NULL | |
| previous_close | REAL NOT NULL | |
| traded_volume | INTEGER NOT NULL | |
| traded_value | REAL NOT NULL | |
| day_return_pct | REAL | (close - prev_close) / prev_close × 100 |
| upper_circuit_hit | INTEGER DEFAULT 0 | day_return_pct >= 19.5 |
| lower_circuit_hit | INTEGER DEFAULT 0 | day_return_pct <= -19.5 |
| delivery_volume | INTEGER | from stage_delivery.qty |
| delivery_value | REAL | delivery_volume × wvap_price |
| delivery_pct | REAL | from stage_delivery.pct |
| wvap_price | REAL | (high + low + close) / 3 |
| isin | TEXT | from stage |
| lowest_closing_5days | INTEGER DEFAULT 0 | close is lowest in last 5 days (min_periods=5) |
| highest_closing_5days | INTEGER DEFAULT 0 | close is highest in last 5 days (min_periods=5) |
| delivery_qty_5d_avg | REAL | rolling 5d mean of delivery_volume |
| delivery_qty_20d_avg | REAL | rolling 20d mean of delivery_volume |
| delivery_pct_5d_avg | REAL | rolling 5d mean of delivery_pct |
| delivery_pct_20d_avg | REAL | rolling 20d mean of delivery_pct |
| delivery_pct_trend | REAL | linear slope of delivery_pct over 5d |
| vol_spike | INTEGER | delivery_volume > 2× delivery_qty_20d_avg |

---

## Table: technical

SMA/EMA, price vs MA ratios, crossovers, trend classification.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| sma_20 | REAL | pandas_ta.sma(close, 20) |
| sma_50 | REAL | pandas_ta.sma(close, 50) |
| sma_100 | REAL | pandas_ta.sma(close, 100) |
| sma_200 | REAL | pandas_ta.sma(close, 200) |
| ema_9 | REAL | pandas_ta.ema(close, 9) |
| ema_20 | REAL | pandas_ta.ema(close, 20) |
| ema_50 | REAL | pandas_ta.ema(close, 50) |
| ema_200 | REAL | pandas_ta.ema(close, 200) |
| price_vs_sma20_pct | REAL | (close - sma_20) / sma_20 × 100 |
| price_vs_sma50_pct | REAL | (close - sma_50) / sma_50 × 100 |
| price_vs_sma200_pct | REAL | (close - sma_200) / sma_200 × 100 |
| golden_cross | INTEGER | sma_50 crosses above sma_200 |
| ema20_gt_ema50 | INTEGER | ema_20 > ema_50 |
| price_gt_sma200 | INTEGER | close > sma_200 |
| trend_stage | TEXT | bullish / bearish / recovering / range-bound |
| trend_score | REAL | 0–100 composite |
| trend_strength | REAL | 0–100 composite |

⚠ SMA/EMA from `pandas_ta` may contain Python `None`. Always coerce via `pd.to_numeric(col, errors="coerce")` before comparisons.

---

## Table: price_level

52-week / 26-week / 4-week highs/lows, classic pivot points.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| week52_high | REAL | rolling(252).max() |
| week52_low | REAL | rolling(252).min() |
| week26_high | REAL | rolling(126).max() |
| week26_low | REAL | rolling(126).min() |
| week4_high | REAL | rolling(20).max() |
| week4_low | REAL | rolling(20).min() |
| pct_from_52wk_high | REAL | |
| pct_from_52wk_low | REAL | |
| pct_from_4wk_high | REAL | |
| near_52wk_high_flag | INTEGER | close >= 0.95 × week52_high |
| near_52wk_low_flag | INTEGER | close <= 1.05 × week52_low |
| breakout_flag | INTEGER | close > prev week4_high |
| pivot_p | REAL | (H + L + C) / 3 |
| pivot_r1 | REAL | 2×P − L |
| pivot_r2 | REAL | P + (H − L) |
| pivot_s1 | REAL | 2×P − H |
| pivot_s2 | REAL | P − (H − L) |

---

## Table: momentum

Technical oscillators and momentum indicators via `pandas_ta`.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| rsi_14 | REAL | Relative Strength Index (14) |
| rsi_9 | REAL | RSI (9) |
| macd | REAL | MACD line (12, 26, 9) |
| macd_signal | REAL | Signal line |
| macd_histogram | REAL | MACD − Signal |
| macd_crossover | INTEGER | MACD crosses above signal |
| stoch_k | REAL | Stochastic %K (14, 3, 3) |
| stoch_d | REAL | Stochastic %D |
| stoch_signal | REAL | %K rolling(3).mean() |
| mfi_14 | REAL | Money Flow Index (14) |
| adx_14 | REAL | Average Directional Index (14) |
| cci_20 | REAL | Commodity Channel Index (20) |
| williams_r_14 | REAL | Williams %R (14) |
| momentum_score | REAL | 0–100 = rsi_comp(33) + macd_comp(34) + stoch_comp(33) |
| decay_factor | REAL | 0.5 + 0.5×exp(−t/63) |

⚠ Window functions (RSI, Stoch, MACD) must convert to `np.asarray(pd.to_numeric(..., errors="coerce").fillna(v), dtype=float)` before numpy ops to avoid `UFuncOutputCastingError`.

---

## Table: volatility

Volatility metrics via `pandas_ta`.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| atr_14 | REAL | Average True Range (14) |
| atr_pct | REAL | atr_14 / close × 100 |
| bb_upper | REAL | Upper Bollinger Band (20, 2σ) |
| bb_middle | REAL | Middle BB (SMA 20) |
| bb_lower | REAL | Lower BB |
| bb_width | REAL | (bb_upper − bb_lower) / bb_middle |
| bb_squeeze | INTEGER | bb_width < 20-period mean(bb_width) |
| keltner_upper | REAL | Upper Keltner Channel (20, 2) |
| keltner_lower | REAL | Lower Keltner Channel |
| historical_vol_20d | REAL | close.pct_change().rolling(20).std() × √252 × 100 |

---

## Table: averages

Rolling means at 5 windows × 5 metrics, plus volume technicals.

**Window metrics** follow pattern `{metric}_avg_{w}d` for `w` in [21, 63, 126, 252, 756]:
- `close_avg_{w}d` — rolling mean of close_price
- `volume_avg_{w}d` — rolling mean of traded_volume
- `rsi_avg_{w}d` — rolling mean of rsi_14
- `delivery_pct_avg_{w}d` — rolling mean of delivery_pct
- `volatility_avg_{w}d` — rolling mean of historical_vol_20d

**Volume technical columns** (computed from daily.traded_volume):
| Column | Type | Notes |
|--------|------|-------|
| vol_5d_avg | REAL | rolling(5) mean |
| vol_10d_avg | REAL | rolling(10) mean |
| vol_20d_avg | REAL | rolling(20) mean |
| vol_ratio | REAL | traded_volume / vol_20d_avg |
| vol_breakout_up | INTEGER | vol_ratio >= 1.5 AND close > open |
| vol_trend_5d | REAL | linear slope of volume over 5d |
| volume_score | REAL | 0–100 composite |

⚠ The merge loop uses metric names `close`, `volume`, `rsi`, `delivery_pct`, `volatility` — rename from `close_price`, `traded_volume` etc. before the per-metric loop.

---

## Table: futures_data

FO UDiFF derivatives data via NSE daily-reports API. Nearest monthly expiry.

| Column | Type | Notes |
|--------|------|-------|
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| expiry_date | TEXT PK | nearest monthly expiry |
| futures_open | REAL | |
| futures_high | REAL | |
| futures_low | REAL | |
| futures_close | REAL | |
| futures_oi | INTEGER | open interest |
| futures_oi_chg | INTEGER | change in OI |
| futures_volume | INTEGER | |
| spot_price | REAL | underlying price |
| basis_pct | REAL | (futures_close − spot_price) / spot_price × 100 |
| oi_change_pct | REAL | (oi_chg / oi) × 100 |

Source: `GET https://www.nseindia.com/api/daily-reports?key=FO` → FO-UDIFF-BHAVCOPY-CSV. Requires `brotli` package for `Content-Encoding: br`. Expiry date format: `DD-Mon-YYYY` or `YYYY-MM-DD`.

Only available for recent 1–2 days. Historical dates get 0 score components.

---

## Table: scoring_result

Full V2a 13-component scoring output per stock per date.

| Column | Type | Notes |
|--------|------|-------|
| exchange | TEXT PK | |
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| overall_score | REAL | 0–100 (capped sum of 13 components) |
| momentum_score | REAL | contrarian entry component |
| value_score | REAL | high delivery % component |
| quality_score | REAL | rising delivery trend component |
| technical_strength | REAL | delivery qty surge component |
| volume_liquidity | REAL | size/liquidity percentile component |
| institutional_score | REAL | FII/DII accumulation component |
| fno_score | REAL | F&O membership boost |
| futures_basis_score | REAL | futures basis premium component |
| oi_trend_score | REAL | OI expansion component |
| sector_momentum_score | REAL | sector-relative return component |
| volatility_score | REAL | low volatility filter component |
| confirmation_score | REAL | multi-indicator confirmation component |
| nifty500_member | INTEGER | 1 if in Nifty 500 |
| heuristic_penalties | TEXT | JSON |
| rank | INTEGER | overall_score rank (desc) |
| percentile | REAL | percentile rank (0–100) |

---

## Table: scoring_picks

Sector-diversified top-N picks per date.

| Column | Type | Notes |
|--------|------|-------|
| trade_date | TEXT PK | |
| symbol | TEXT PK | |
| score | REAL | overall_score |
| rank | INTEGER | 1-based |
| sector | TEXT | from index_membership → equity_master → 'UNKNOWN' |

Selection: top `max_per_sector` (5) per sector, max `top_n` (20) total.

---

## Table: scoring_performance

Forward-return verification of past picks.

| Column | Type | Notes |
|--------|------|-------|
| pick_date | TEXT PK | |
| check_date | TEXT PK | |
| symbol | TEXT PK | |
| entry_price | REAL | close on pick_date |
| exit_price | REAL | close on check_date |
| return_pct | REAL | (exit − entry) / entry × 100 |

Auto-verified on each scoring run for picks where `pick_date + lookahead_days (10) <= trade_date`.

---

## Table: shareholding

Quarterly promoter/FII/DII/public holding percentages.

| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT PK | |
| period | TEXT PK | e.g. '2025-03' |
| quarter_end | TEXT | |
| quarter_end_int | INTEGER | CAST(REPLACE(quarter_end, '-', '') AS INTEGER) |
| promoter_pct | REAL | |
| fii_pct | REAL | |
| dii_pct | REAL | |
| public_pct | REAL | |

Source: `github.com/aditya-jha/nse-historical-membership` → `shareholding_history/data/parsed/_flat.csv`. Column `period` detected by exact match `fn_lower == "period"` in addition to substring matching.

⚠ `quarter_end_int` must be populated or `_load_shareholding()` returns 0 rows (query filter: `WHERE quarter_end_int <= ?`).

---

## Table: fno_membership

F&O membership with point-in-time validity.

| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT PK | |
| valid_from | TEXT PK | |
| valid_to | TEXT | NULL = still active |

Source: `github.com/aditya-jha/nse-historical-membership` → `fno_history/data/fno_membership_history.csv`.

---

## Table: index_membership

NSE index constituents with point-in-time validity.

| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT PK | |
| index_name | TEXT PK | |
| index_id | INTEGER PK | |
| valid_from | TEXT PK | |
| valid_to | TEXT | NULL = still active |
| weightage | REAL | |

20 sector indices mapped to normalized sector names. Nifty 500 used as quality gate in scoring.

Source: `github.com/aditya-jha/nse-historical-membership` → `index_history/data/index_membership_history.csv`.

---

## Table: equity_master

Symbol → sector / industry mapping (fallback when index_membership has no entry).

| Column | Type | Notes |
|--------|------|-------|
| symbol | TEXT PK | |
| isin | TEXT | |
| sector | TEXT | |
| industry | TEXT | |
| market_cap | TEXT | |
| last_updated | TEXT | |

⚠ As of July 2026, NSE `EQ_MAST.csv` URL is dead. Falls back to `EQUITY_L.csv` (no INDUSTRY column) — all symbols get `UNKNOWN`. Primary sector source is always `index_membership`.

---

## Table: predicted_stock

Hit analysis — forward price targets per pick per entry mode.

| Column | Type | Description |
|--------|------|-------------|
| pick_date | TEXT PK | |
| symbol | TEXT PK | |
| entry_date | TEXT | next trading day |
| entry_price | REAL | open × (1 + slippage/100) or high |
| tg1_price | REAL | entry × (1 + target1%) |
| tg1_date | TEXT | first date high >= tg1_price |
| tg1_high | REAL | high within window 1 |
| tg1_close | REAL | close at window 1 end |
| tg2_price, tg2_date, tg2_high, tg2_close | | same for target 2 |
| tg3_price, tg3_date, tg3_high, tg3_close | | same for target 3 |
| window_low | REAL | lowest price across full window |
| window_low_date | TEXT | |
| window_end_date | TEXT | |
| target_hit | INTEGER | 0=none, 1=tg1, 2=tg2, 3=tg3 |
| data_complete | INTEGER | |

⚠ `_get_next_trading_day()` must filter by `symbol = ?`. Without it, all picks get the same entry_price from the first stock's next-day row.

---

## Conventions

- All tables use `INSERT OR REPLACE` (idempotent)
- Batch inserts: `executemany(sql, batch[0:500])`, 500 rows per transaction
- All numeric values rounded to 2 decimal places at creation
- Schema migrations are additive only (`ALTER TABLE ADD COLUMN` in try/except)
- No foreign key constraints (application-level only)
- Shared logger via `get_logger('runner')`
- Only dependencies: `pyyaml`, `pandas`, `pandas-ta`, `requests`, `numpy`, `brotli`
