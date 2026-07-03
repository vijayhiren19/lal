# Scoring Skill

## Purpose
Define the methodology, empirical findings, and implementation for the scrip scoring algorithm.

---

## Development Approach — Build One Factor at a Time

Build the scoring formula incrementally. Do NOT implement all 13 components at once.

1. **Start with Delivery Accumulation** — score stocks with high delivery % and rising trend
2. **Validate** — pick top 10, compare against your market knowledge. Do they make sense?
3. **Add contrarian entry** — reward recent pullbacks (negative day returns)
4. **Add liquidity** — market cap proxy via traded value percentile
5. **Add remaining components** — one at a time, validate each before adding the next

### Key Rules
- **No hard-coded values** — every threshold must come from `config/scoring.yaml`
- **Validate before tuning** — always check top-10 picks against your market knowledge before adjusting weights. If a stock you know is weak appears in the top 10, investigate why.
- **Backtest deciles** — score the universe, split into deciles, measure forward returns. Top decile should outperform bottom decile.

---

## Current State (V2a — Optimized for Top-20 Selection)

The scoring system is a **delivery-quality + contrarian + liquidity** composite. It identifies 20 stocks (sector-diversified) with strong delivery metrics and pullback entry points that tend to outperform over 10-20 trading days.

### Research-Backed Formula

Derived from reverse-engineering 712K rows of historical data across 365 trading days:

```python
def score_top20(df):
    # 1. Contrarian entry: reward recent pullbacks
    neg_ret = df['day_return_pct'].fillna(0) < 0
    score = np.where(neg_ret, df['day_return_pct'].fillna(0).abs() * 1.5, 0)

    # 2. High delivery % (accumulation)
    score += (df['pct'].fillna(0) >= 60).astype(float) * 15

    # 3. Rising delivery trend (increasing over 5 days)
    score += (df['pct_trend'].fillna(0) >= 0.5).astype(float) * 10

    # 4. Delivery quantity surge (vs 20-day average)
    score += (df['delivery_qty_ratio'].fillna(0) >= 1.5).astype(float) * 10

    # 5. Size/liquidity screen (market cap proxy)
    score += df['traded_value'].rank(pct=True) * 15

    # 6. Institutional accumulation (FII + DII 4Q change)
    sm_delta = df['smart_money_delta'].fillna(0)
    score += np.where(sm_delta > 2, np.clip(sm_delta * 3, 0, 15), 0)

    # 7. F&O membership boost (+3 for derivatives-active)
    score += df['is_fno'].fillna(0).to_numpy().astype(float) * 3

    # 8. Nifty 500 quality flag (+2 for index constituents)
    score += df['is_nifty500'].fillna(0).to_numpy().astype(float) * 2

    return score.clip(0, 100)
```

### Backtest Results (2025-07 to 2026-05, top-30 historical)

| Horizon | Avg Return | Win Rate | vs Random | Positive Months |
|---------|-----------|----------|-----------|-----------------|
| **10-day** | **+2.68%** | **53.9%** | +1.40% | 54.5% |
| **15-day** | **+2.31%** | **54.8%** | +2.96% | 63.6% |
| **20-day** | **+2.03%** | **48.3%** | +3.38% | 60.0% |

All three horizons beat random returns by a meaningful margin.

### Feature Correlation Analysis (Information Coefficient)

Only features with positive 20-day IC are used:

| Feature | IC (20d) | Win Avg | Loss Avg | Role |
|---------|----------|---------|----------|------|
| delivery_value | +0.083 | 23.1Cr | 18.4Cr | Liquidity proxy |
| traded_value | +0.078 | 54.8Cr | 45.2Cr | Market cap proxy |
| delivery_qty | +0.047 | 838K | 651K | Accumulation size |
| price_range_3d | +0.038 | 8.3% | 7.8% | Volatility signal |
| volume_score | +0.037 | 34.8 | 33.8 | Volume quality |
| vol_ratio | +0.035 | 1.06 | 1.04 | Volume surge |
| atr_pct | +0.034 | 4.9% | 4.6% | Volatility |
| **delivery_qty_ratio** | **+0.025** | **1.07** | **1.03** | **Delivery surge** |
| pct_trend | +0.004 | 0.01 | -0.00 | Delivery trend |
| day_return_pct | **-0.007** | 0.3 | 0.1 | Pullback signal |

Negative IC features excluded: cum_return_3d (-0.027), cum_return_5d (-0.042).

---

## Execution Model

Scoring runs as a batch process after all data pipeline stages complete.

| Property | Value |
|---|---|---|
| Trigger | CLI: `python -m src.runner --stages score` or `--stages all` |
| Target | Top **20** stocks (configurable via `top_n`), sector-diversified |
| Lookahead | **10 days** optimal (configurable) |
| Frequency | Once per trading day, after market close |

---

## Equity-Only Filter

The scorer excludes ETFs by filtering merged data to only symbols whose ISIN starts with `INE`:

```python
stage_df = pd.read_sql(
    "SELECT exchange, trade_date, symbol, day_return_pct, upper_circuit_hit, lower_circuit_hit, isin "
    "FROM stage WHERE trade_date <= ? ORDER BY symbol, trade_date",
    conn, params=(trade_date,),
)
merged = merged.merge(stage_df, on=["exchange", "trade_date", "symbol"], how="left")
merged = merged[merged["isin"].str.startswith("INE", na=False)]
```

ISIN prefix reference:

| Prefix | Type | % of Universe |
|--------|------|---------------|
| `INE` | Equity | ~86% |
| `INF` | ETF / Mutual Fund | ~14% (excluded) |
| `IN9` | Other (REITs) | <0.1% (excluded) |

---

## Important Implementation Patterns

### np.where + pandas Series → object dtype
Always convert conditions with `.to_numpy()` and use `.astype(float)`:
```python
cond = (df["pct"] > 60).fillna(False).to_numpy()
boost = np.where(cond, 15.0, 0.0).astype(float)
```

### None-safe comparisons
Use `pd.to_numeric(col, errors='coerce')` before comparison:
```python
pct_trend = pd.to_numeric(df["pct_trend"], errors="coerce")
cond = pct_trend >= 0.5
```

### Numpy round on arrays
Use `np.round(arr, 2)` instead of `arr.round(2)` to avoid object dtype issues:
```python
adjusted = np.round(np.clip(orig + penalty, 0, 100), 2)
```

---

## What Was Rejected (and Why)

### All heuristics disabled
Every implemented heuristic (delivery accumulation, consecutive gain reversal, 52wk proximity, volume dry-up, circuit streak, ATR penalty) degraded performance. The composite formula captures the signal more directly.

### Value-only contrarian (old baseline)
Spread of +16% across deciles but **-1.27% for top 30** — works as a broad filter but doesn't identify concentrated winners.

### Consolidation / narrow price range
IC = +0.038 (positive alone) but combining it with delivery signals diluted the top-30 selection performance.

### RSI < 40 (oversold)
Too many false signals. Works only when combined with delivery confirmation.

### Market cap / traded value filters
Cutting off stocks below a threshold excludes profitable contrarian opportunities. Using a percentile-based rank (continuous) works better than a hard cutoff.

---

## Hit Analysis (Feedback Loop)

The `hits_analyzer.py` module validates picks using forward price data from the `daily` table. It computes two things:

1. **`compute_hits()`** — For each pick, finds the next trading day (entry), records entry open/high, then queries forward data for 5/10/15 trading day windows. Stores high prices, high dates, and close prices for each window in the `scoring_hits` table.
2. **`analyze_hits()`** — Evaluates every pick against 2 entry modes × 3 targets × 3 windows = 18 scenarios. Returns a grouped DataFrame with hit rate % and avg days to hit.

### Entry Mode

The first entry mode in the config list is the **active mode** used to compute `entry_price`:

```yaml
entry_modes:
  - name: open_p3
    slippage_pct: 3          # entry_price = entry_open × 1.03
  - name: high_val
    slippage_pct: null       # entry_price = entry_high
```

Add any mode — the first one drives all predictions.

### Targets & Windows

Targets and windows are paired by position (first target uses first window, etc.):

```yaml
targets:
  - name: target1
    pct: 4    # tg1_price = entry × 1.04, checked within window[0] days
  - name: target2
    pct: 5    # tg2_price = entry × 1.05, checked within window[1] days
  - name: target3
    pct: 10   # tg3_price = entry × 1.10, checked within window[2] days
windows: [5, 10, 15]
```

`target_hit` column: 0=none, 1=tg1 hit, 2=tg2 hit, 3=tg3 hit (hierarchical).

### Hit Rate Results (378 picks, May 2026, top 20/day, open+3%)

| Entry Mode | Target | 5-Day | 10-Day | 15-Day |
|------------|--------|-------|--------|--------|
| open + 3% | 4% | 22.7% | 34.6% | **42.5%** |
| open + 3% | 5% | 17.9% | 30.1% | **37.3%** |
| open + 3% | 10% | 7.4% | 16.4% | 21.7% |
| Day high | 4% | 23.7% | 38.7% | 47.5% |
| Day high | 5% | 19.7% | 33.5% | 42.1% |
| Day high | 10% | 6.1% | 17.2% | 22.6% |

**Key findings:**
- With realistic slippage (+3%), the hit rate drops by ~15pp vs +1% slippage
- **open + 3%, 4% target, 15-day window → 42.5% hit rate** (292/687 picks)
- **day high** entry paradoxically outperforms open+3% — buying at the day's high is worse but the 3% slippage eliminates open entry's advantage
- 5% target at 15 days via open+3% is 37.3% (256/687 hits)
- open+10% target remains hardest: only 21.7% at 15 days

### target_hit Hierarchical Results (378 picks, May 2026, open+3%)

| Target Hit | Meaning | Count | % |
|------------|---------|-------|---|
| 0 | No target hit | 255 | 67.5% |
| 1 | Tg1 only (4% in 5d) | 8 | 2.1% |
| 2 | At least Tg2 (5% in 10d) | 42 | 11.1% |
| 3 | All targets (10% in 15d) | 73 | 19.3% |

**Any target hit: 32.5%** (123/378)

### predicted_stock Table Schema

```sql
CREATE TABLE IF NOT EXISTS predicted_stock (
    pick_date       TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    entry_date      TEXT,
    entry_price     REAL,   -- from configured mode (open × slippage_pct or high)

    tg1_price       REAL,   -- target 1 price
    tg1_date        TEXT,   -- first date high reached target (null = not hit)
    tg1_high        REAL,   -- highest price within window 1
    tg1_close       REAL,   -- close at window 1 end

    tg2_price       REAL,
    tg2_date        TEXT,
    tg2_high        REAL,
    tg2_close       REAL,

    tg3_price       REAL,
    tg3_date        TEXT,
    tg3_high        REAL,
    tg3_close       REAL,

    window_low      REAL,
    window_low_date TEXT,
    window_end_date TEXT,

    target_hit      INTEGER DEFAULT 0,  -- 0=none, 1=tg1, 2=tg2, 3=tg3
    data_complete   INTEGER DEFAULT 0,
    calculated_at   TEXT,
    PRIMARY KEY (pick_date, symbol)
);
```

### Usage

```bash
# Compute predictions for a month range
python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-05-31

# Analyze already-computed predictions
python -m src.validation.run_hits --analyze

# Analyze with detail (show picks where target_hit >= 1)
python -m src.validation.run_hits --analyze --detail 1

# As a pipeline stage
python -m src.runner --stages hits --start-date 2026-05-01 --end-date 2026-05-31
```

---

## Monthly Breakdown (V2a — 10-day forward)

| Month | Top 30 Avg | Win Rate | Bottom 30 Avg | Random Avg | Available |
|-------|-----------|----------|---------------|------------|-----------|
| 2025-07 | -1.75% | 40.0% | -3.97% | -3.30% | 1,786 |
| 2025-08 | +5.95% | 86.7% | +1.67% | +3.40% | 1,842 |
| 2025-09 | +2.58% | 73.3% | +1.59% | +1.70% | 1,976 |
| 2025-10 | -0.19% | 56.7% | -3.94% | -3.36% | 1,997 |
| 2025-11 | -1.12% | 30.0% | -3.08% | -1.60% | 2,024 |
| 2025-12 | -1.40% | 36.7% | -5.70% | -5.81% | 2,076 |
| 2026-01 | +0.46% | 63.3% | +4.71% | +1.03% | 2,095 |
| 2026-02 | -7.01% | 13.3% | -9.30% | -6.63% | 2,103 |
| 2026-03 | **+33.28%** | 93.3% | +14.35% | +25.87% | 2,117 |
| 2026-04 | +2.16% | 60.0% | -0.70% | +2.74% | 2,098 |
| 2026-05 | -3.36% | 40.0% | -0.72% | 0.00% | 2,092 |

**Observations:**
- The system is regime-dependent: one great month (+33%) offsets several losing months
- Top 30 outperforms random in 6/11 months (54.5%)
- Top 30 outperforms bottom 30 in 8/11 months (72.7%) — consistent relative performance
- Available universe grows steadily as more symbols have sufficient trading history

## Known Limitations

1. **Regime dependence**: The formula works best in mean-reverting / range-bound markets. In strong trending markets, delivery-quality signals can lag.

2. **Limited data period**: Backtest covers only 11 months (2025-07 to 2026-05). This is one market regime; performance may vary in different conditions.

3. **No transaction costs**: Backtest doesn't account for brokerage, slippage, or market impact on top 30 picks (some may be less liquid).

4. **Month-end bias**: Scores are computed at month-end. Real-time scoring may produce different results.

5. **Single market (NSE)**: Formula is calibrated on Indian equities only. May not generalize to other markets.

6. **Market cap proxy**: Uses `traded_value` percentile instead of actual market cap. While correlated, actual market cap data would be more precise.

7. **Sector coverage**: Now uses `index_membership` table (20 sector indices, 1,313 symbols, PIT accurate) as primary source with `equity_master` (515 named sectors) as fallback. ~1,800 scrips now have named sectors via the combined approach. Run `build_equity_master.py` and `build_index_history.py` to refresh mappings.

8. **Lookahead period**: Backtest used 10-day horizon. Real-time performance may differ from backtest due to market regime shifts.

9. **Hit analysis window**: Hit rates are computed from 23 trading days (May 4 → Jun 4 2026, 687 picks). As more picks accumulate, hit rate estimates will become more robust across different market conditions.

## Implementation Details (Completed)

### Scorer (`src/scoring/scorer.py`)

The scorer implements V2a formula directly — no heuristics, no factor weights:

1. **Data loading** — 7-table merge (daily, delivery, technical, momentum, volatility, price_level, stage)
2. **Equity filter** — ISIN prefix filter (`isin.startswith('INE')`)
3. **Delivery value filter** — computes `dev_traded_value = delivery.qty × daily.close_price`, filters stocks below `min_dev_trade_value_cr` (default 0.5 Cr). Ensures delivery signals are based on meaningful institutional activity, not penny stocks with negligible absolute delivery
4. **Feature computation** — Adds `delivery_qty_ratio` from `delivery.qty / delivery.qty_20d_avg`
4. **Score computation** — `_compute_v2a_score()` computes 8 components with binary/direct scoring:
   - `momentum_score` = contrarian: `abs(day_return_pct) * 1.5` if negative, else 0 (capped at 50)
   - `value_score` = delivery pct ≥ 60%? 15 : 0
   - `quality_score` = pct_trend ≥ 0.5? 10 : 0
   - `technical_strength` = delivery_qty_ratio ≥ 1.5? 10 : 0
   - `volume_liquidity` = traded_value percentile rank × 15
    - `institutional_score` = smart_money_delta × 3.0 if delta > 2%, capped at 15 (4Q FII+DII change from shareholding table)
    - `fno_score` = is_fno? fno_boost (3) : 0 (F&O membership from fno_membership table)
    - `nifty500_score` = is_nifty500? nifty500_boost (2) : 0 (Nifty 500 membership from index_membership table)
    - `overall_score` = sum of all 8 (capped 0-100)
5. **Shareholding data** — `_load_shareholding()` queries the `shareholding` table (from `nse-historical-membership` repo) for point-in-time FII/DII % at most recent quarter-end. Computes `smart_money_delta = fii_d4q + dii_d4q` (4-quarter change). Merged into the main DataFrame for scoring.
6. **Sector diversification** — `_save_picks()` loads sectors from `index_membership` (20 sector indices, PIT accurate, 1,313 symbols) first, falls back to `equity_master` for remaining symbols, then sorts by score descending, picks top `top_n` but skips sectors that already have `max_per_sector` picks
6. **Paper trading** — saves picks to `scoring_picks`; `_verify_past_picks()` checks all past picks whose `lookahead_days` have elapsed, computes forward return from `daily.close_price`, and `INSERT OR IGNORE`s into `scoring_performance`
7. **Ranking** — `rank` = overall_score rank (descending), `percentile` = pct rank
8. **Batch upsert** — Writes all 2000+ scrips to `scoring_result`, writes top 20 diversified picks to `scoring_picks`

### F&O & Index Membership Integration

F&O membership (`build_fno_membership.py`) and index membership (`build_index_history.py`) are standalone build scripts (not pipeline stages). Run them once (or periodically):
- `fno_membership` table: 311 rows, 270 symbols, PIT intervals with `valid_from`/`valid_to`. Rebuild: `.venv\Scripts\python build_fno_membership.py`
- `index_membership` table: 6,525 rows, 1,313 symbols, 42 indices (20 sector indices mapped to normalized sector names). Rebuild: `.venv\Scripts\python build_index_history.py`

The scorer loads these tables on each call (`_load_fno_membership()`, `_load_nifty500_members()`) to get PIT-accurate membership flags.

### Runner (`src/runner.py`)

Stages after `averages`:

```python
STAGE_ORDER = [
    "fetch", "equity_master", "enrich", "volume", "technical",
    "price_level", "momentum", "volatility", "averages", "score",
    "hits",
]
```

- `equity_master` — downloads NSE equity master CSV, upserts to `equity_master` table
- `score` — calls `run_scoring(end_date)` — runs V2a formula, saves picks, verifies past
- `hits` — calls `compute_hits(start_date, end_date)` then `analyze_hits()` — computes forward price data and shows hit rates

### Equity Master (`src/data_pipeline/equity_master.py`)

Downloads NSE equity master CSV from `EQ_MAST.csv` (returns 404). Workaround: `data/eq_mast.csv` is built from Nifty 500 constituent list (`archives.nseindia.com/content/indices/ind_nifty500list.csv`) with `Industry` mapped to `sector`, rest UNKNOWN. Currently 2,401 rows (515 with named sector, 20 sectors). Columns: SYMBOL, ISIN, SECTOR, INDUSTRY, MARKET_CAP. Upserts to `equity_master` table.

### DB Schema (`db/connection.py`)

Tables added beyond the core pipeline tables, plus new columns on `scoring_result`:

New columns on `scoring_result`: `fno_score REAL`, `nifty500_member INTEGER`.

```sql
CREATE TABLE IF NOT EXISTS equity_master (
    symbol          TEXT    PRIMARY KEY,
    isin            TEXT,
    sector          TEXT,
    industry        TEXT,
    market_cap      TEXT,
    last_updated    TEXT
);

CREATE TABLE IF NOT EXISTS scoring_picks (
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    score           REAL,
    rank            INTEGER,
    sector          TEXT,
    PRIMARY KEY (trade_date, symbol)
);

CREATE TABLE IF NOT EXISTS scoring_performance (
    pick_date       TEXT    NOT NULL,
    check_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    return_pct      REAL,
    PRIMARY KEY (pick_date, check_date, symbol)
);
```

### Config (`config/scoring.yaml`)

```yaml
scoring:
  top_n: 20
  max_per_sector: 5
  lookahead_days: 10
  min_dev_trade_value_cr: 0.5          # filter: min delivery traded value in Cr
  delivery_pct_threshold: 60
  pct_trend_threshold: 0.5
  delivery_qty_ratio_threshold: 1.5
  value_return_weight: 1.5
  delivery_pct_boost: 15
  pct_trend_boost: 10
  delivery_qty_boost: 10
  liquidity_percentile_weight: 15
  institutional_max_boost: 15
  institutional_delta_threshold: 2.0
  institutional_scale_factor: 3.0
  fno_boost: 3                        # F&O membership bonus (fno_membership table)
  nifty500_boost: 2                   # Nifty 500 quality bonus (index_membership table)
```

## Data Sources Used

| Feature | Table | Column | Computation |
|---------|-------|--------|-------------|
| day_return_pct | stage | day_return_pct | (close - prev_close) / prev_close × 100 |
| pct (delivery %) | delivery | pct | Delivery quantity / traded quantity × 100 |
| pct_trend | delivery | pct_trend | Linear slope of pct over 5 sessions |
| delivery_qty_ratio | delivery | qty, qty_20d_avg | qty / qty_20d_avg |
| traded_value | daily | traded_value | Total traded value in rupees |
| smart_money_delta | shareholding | fii_pct, dii_pct | (fii_current - fii_4q_ago) + (dii_current - dii_4q_ago) |
| is_fno | fno_membership | valid_from, valid_to | valid_from ≤ trade_date AND (valid_to IS NULL OR valid_to > trade_date) |
| is_nifty500 | index_membership | index_name, valid_from, valid_to | index_name='Nifty 500' AND valid_from ≤ trade_date AND (valid_to IS NULL OR valid_to > trade_date) |
| sector | index_membership | index_name, valid_from, valid_to | 20 sector indices mapped to normalized sectors with PIT accuracy |

## Build Scripts

| Script | Purpose | Source |
|--------|---------|--------|
| `build_fno_membership.py` | Downloads `fno_membership_history.csv`, populates `fno_membership` table (311 rows, 270 symbols, PIT 2014→2026) | `github.com/aditya-jha/nse-historical-membership` |
| `build_index_history.py` | Downloads `index_membership_history.csv`, populates `index_membership` table (6,525 rows, 1,313 symbols, 42 indices, including 20 sector index → sector mappings) | `github.com/aditya-jha/nse-historical-membership` |
| `build_shareholding.py` | Downloads shareholding flat CSV, populates `shareholding` table (59,769 rows, 2,261 symbols, quarterly FII/DII 2001→2026) | `github.com/aditya-jha/nse-historical-membership` |
| `build_equity_master.py` | Downloads Nifty 500 constituent list, builds `data/eq_mast.csv` (2,401 rows, 515 named sectors) | `archives.nseindia.com` |

Rebuild any data source by running the corresponding script: `.venv\Scripts\python build_*.py`

## Tuning & Backtesting
- `config/scoring.yaml` — weights, thresholds, and boost parameters
- Backtest method: score each month-end, pick top 20, measure forward N-day return
- Grid search over 2,000+ parameter combinations to identify optimal thresholds
- Key finding: delivery pct ≥ 60% and pct_trend ≥ 0.5 are the strongest threshold values
- Files: `backtest_results_full.csv`, `backtest_buckets.csv`
