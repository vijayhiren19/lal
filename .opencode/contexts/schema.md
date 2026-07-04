# Database Schema — Indian Stock Scrip Scoring

## Overview

| Property | Value |
|---|---|---|
| Engine | SQLite3 |
| Database file | `mydb1.db` in project root folder |
| Dates | `TEXT` (ISO `YYYY-MM-DD`) |
| Booleans / flags | `INTEGER` (0 / 1) |
| Prices, percentages, scores | `REAL` |
| Volumes, quantities | `INTEGER` |
| Foreign keys | App-level only (not enforced in SQLite) |
| Schema changes | Additive `ALTER TABLE ADD COLUMN` only — never drop or rename

## Pipeline Flow
```
               stage
              /     \
         delivery  daily
                     │
           ┌────────┼────────┐
           │        │        │
      technical  price_level momentum
           │        │        │
           └────────┼────────┘
                    │
              volatility
                    │
               averages
                    │
            futures_data
                    │
            scoring_result  ──→ scoring_picks
                                      │
                                predicted_stock ──→ scoring_performance
```

**Data pipeline stages (9):** Fetch → Equity Master → Enrich → Technical → Price Level → Momentum → Volatility → Averages → Derivatives  
**Scoring stages (2):** Score → Hits  
**Total: 11 stages**

---

## Table: `stage`
Raw landing table — data as received from NSE/BSE API before any transformation.

```sql
CREATE TABLE stage (
    exchange        TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,  -- YYYY-MM-DD
    symbol          TEXT    NOT NULL,
    open_price      REAL    NOT NULL,
    high_price      REAL    NOT NULL,
    low_price       REAL    NOT NULL,
    close_price     REAL    NOT NULL,
    previous_close  REAL    NOT NULL,
    traded_volume   INTEGER NOT NULL,
    traded_value    REAL    NOT NULL,  -- Rupees
    day_return_pct  REAL,              -- (close - prev_close) / prev_close * 100
    upper_circuit_hit INTEGER DEFAULT 0,  -- 1 if day_return_pct >= 19.5
    lower_circuit_hit INTEGER DEFAULT 0,  -- 1 if day_return_pct <= -19.5
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

```

---

## Table: `daily`
Enriched base table — stage data plus volume-derived metrics.

```sql
CREATE TABLE daily (
    exchange        TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    open_price      REAL    NOT NULL,
    high_price      REAL    NOT NULL,
    low_price       REAL    NOT NULL,
    close_price     REAL    NOT NULL,
    previous_close  REAL    NOT NULL,
    traded_volume   INTEGER NOT NULL,
    traded_value    REAL    NOT NULL,
    vol_5d_avg      REAL,
    vol_10d_avg     REAL,
    vol_20d_avg     REAL,
    vol_ratio       REAL,
    vol_breakout_up INTEGER,
    vol_trend_5d    REAL,
    volume_score    REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details

| Column | Description |
|---|---|
| `vol_5d_avg` | Average traded volume over last 5 trading sessions |
| `vol_10d_avg` | Average traded volume over last 10 trading sessions |
| `vol_20d_avg` | Average traded volume over last 20 trading sessions |
| `vol_ratio` | Today's volume / vol_20d_avg — volume expansion/contraction |
| `vol_breakout_up` | Flag: 1 if today's volume > 1.5x vol_20d_avg and close > open |
| `vol_trend_5d` | Slope of volume over last 5 sessions (positive = rising) |
| `volume_score` | Composite volume quality score (0-100) |

---

## Table: `delivery`
Delivery data with rolling averages and trend signals.

```sql
CREATE TABLE delivery (
    exchange        TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    qty             INTEGER NOT NULL,
    pct             REAL    NOT NULL,
    qty_5d_avg      REAL,
    qty_20d_avg     REAL,
    pct_5d_avg      REAL,
    pct_20d_avg     REAL,
    pct_trend       REAL,
    vol_spike       INTEGER,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details

| Column | Description |
|---|---|
| `qty` | Quantity delivered (shares) |
| `pct` | Delivery percentage (delivery_qty / traded_volume * 100) |
| `qty_5d_avg` | Average delivery quantity over last 5 sessions |
| `qty_20d_avg` | Average delivery quantity over last 20 sessions |
| `pct_5d_avg` | Average delivery % over last 5 sessions |
| `pct_20d_avg` | Average delivery % over last 20 sessions |
| `pct_trend` | Linear slope of delivery % over last 5 sessions |
| `vol_spike` | Flag: 1 if delivery qty > 2x qty_20d_avg |

---

## Table: `technical`
Moving averages, crossovers, and trend assessment.

```sql
CREATE TABLE technical (
    exchange            TEXT    NOT NULL,
    trade_date          TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    sma_20              REAL,
    sma_50              REAL,
    sma_100             REAL,
    sma_200             REAL,
    ema_9               REAL,
    ema_20              REAL,
    ema_50              REAL,
    ema_200             REAL,
    price_vs_sma20_pct  REAL,
    price_vs_sma50_pct  REAL,
    price_vs_sma200_pct REAL,
    golden_cross        INTEGER,
    ema20_gt_ema50      INTEGER,
    price_gt_sma200     INTEGER,
    trend_stage         TEXT,
    trend_score         REAL,
    trend_strength      REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details

| Column | Description |
|---|---|
| `sma_20` / `sma_50` / `sma_100` / `sma_200` | Simple moving averages |
| `ema_9` / `ema_20` / `ema_50` / `ema_200` | Exponential moving averages |
| `price_vs_sma20_pct` | (close - sma_20) / sma_20 * 100 — distance from SMA |
| `golden_cross` | Flag: 1 if sma_50 crosses above sma_200 |
| `ema20_gt_ema50` | Flag: 1 if ema_20 > ema_50 |
| `price_gt_sma200` | Flag: 1 if close > sma_200 |
| `trend_stage` | Qualitative label: 'bullish', 'bearish', 'range-bound', 'recovering' |
| `trend_score` | Composite trend score (0-100) |
| `trend_strength` | Magnitude of trend (e.g., ADX value or slope magnitude) |

---

## Table: `price_level`
52-week / 26-week / 4-week price levels and pivot points.

```sql
CREATE TABLE price_level (
    exchange            TEXT    NOT NULL,
    trade_date          TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    week52_high         REAL,
    week52_low          REAL,
    week26_high         REAL,
    week26_low          REAL,
    week4_high          REAL,
    week4_low           REAL,
    pct_from_52wk_high  REAL,
    pct_from_52wk_low   REAL,
    pct_from_4wk_high   REAL,
    near_52wk_high_flag INTEGER,
    near_52wk_low_flag  INTEGER,
    breakout_flag       INTEGER,
    pivot_p             REAL,
    pivot_r1            REAL,
    pivot_r2            REAL,
    pivot_s1            REAL,
    pivot_s2            REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details

| Column | Description |
|---|---|
| `week52_high` / `week52_low` | 252-trading-day high/low |
| `week26_high` / `week26_low` | 126-trading-day high/low |
| `week4_high` / `week4_low` | 20-trading-day high/low |
| `pct_from_52wk_high` | (week52_high - close) / week52_high * 100 |
| `pct_from_52wk_low` | (close - week52_low) / week52_low * 100 |
| `near_52wk_high_flag` | 1 if close within 5% of 52wk high |
| `near_52wk_low_flag` | 1 if close within 5% of 52wk low |
| `breakout_flag` | 1 if close > week4_high (recent breakout) |
| `pivot_p` | Pivot point: (high + low + close) / 3 |
| `pivot_r1` / `pivot_r2` | Resistance levels 1 and 2 |
| `pivot_s1` / `pivot_s2` | Support levels 1 and 2 |

---

## Table: `momentum`
Oscillators and momentum indicators.

```sql
CREATE TABLE momentum (
    exchange        TEXT    NOT NULL,
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    rsi_14          REAL,
    rsi_9           REAL,
    macd            REAL,
    macd_signal     REAL,
    macd_histogram  REAL,
    macd_crossover  INTEGER,
    stoch_k         REAL,
    stoch_d         REAL,
    stoch_signal    REAL,
    mfi_14          REAL,
    adx_14          REAL,
    cci_20          REAL,
    williams_r_14   REAL,
    momentum_score  REAL,
    decay_factor    REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details

| Column | Description |
|---|---|
| `rsi_14` / `rsi_9` | Relative Strength Index (14, 9 period) |
| `macd` / `macd_signal` / `macd_histogram` | MACD line, signal line, histogram |
| `macd_crossover` | Flag: 1 if MACD crosses above signal line |
| `stoch_k` / `stoch_d` / `stoch_signal` | Stochastic oscillator %K, %D, signal |
| `mfi_14` | Money Flow Index (14 period) |
| `adx_14` | Average Directional Index (14 period) |
| `cci_20` | Commodity Channel Index (20 period) |
| `williams_r_14` | Williams %R (14 period) |
| `momentum_score` | Composite momentum score (0-100) |
| `decay_factor` | Exponential decay weight for older momentum readings |

---

## Table: `volatility`
Volatility indicators including ATR, Bollinger Bands, and Keltner Channels.

```sql
CREATE TABLE volatility (
    exchange            TEXT    NOT NULL,
    trade_date          TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    atr_14              REAL,
    atr_pct             REAL,
    bb_upper            REAL,
    bb_middle           REAL,
    bb_lower            REAL,
    bb_width            REAL,
    bb_squeeze          INTEGER,
    keltner_upper       REAL,
    keltner_lower       REAL,
    historical_vol_20d  REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details

| Column | Description |
|---|---|
| `atr_14` | Average True Range (14 period) |
| `atr_pct` | ATR as % of close — relative volatility |
| `bb_upper` / `bb_middle` / `bb_lower` | Bollinger Bands (20,2) — upper, SMA, lower |
| `bb_width` | (bb_upper - bb_lower) / bb_middle — bandwidth |
| `bb_squeeze` | Flag: 1 if Bollinger Bandwidth at 6-month low |
| `keltner_upper` / `keltner_lower` | Keltner Channels (20, 1.5 ATR) |
| `historical_vol_20d` | Annualized volatility over 20 days |

---

---

## Table: `averages`
Rolling averages over standard windows for key metrics. Computed by the Averages stage.

```sql
CREATE TABLE averages (
    exchange                TEXT    NOT NULL,
    trade_date              TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL,
    close_avg_21d           REAL,
    close_avg_63d           REAL,
    close_avg_126d          REAL,
    close_avg_252d          REAL,
    close_avg_756d          REAL,
    volume_avg_21d          REAL,
    volume_avg_63d          REAL,
    volume_avg_126d         REAL,
    volume_avg_252d         REAL,
    volume_avg_756d         REAL,
    rsi_avg_21d             REAL,
    rsi_avg_63d             REAL,
    rsi_avg_126d            REAL,
    rsi_avg_252d            REAL,
    rsi_avg_756d            REAL,
    delivery_pct_avg_21d    REAL,
    delivery_pct_avg_63d    REAL,
    delivery_pct_avg_126d   REAL,
    delivery_pct_avg_252d   REAL,
    delivery_pct_avg_756d   REAL,
    volatility_avg_21d      REAL,
    volatility_avg_63d      REAL,
    volatility_avg_126d     REAL,
    volatility_avg_252d     REAL,
    volatility_avg_756d     REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Windows
| Label | Trading Days | Approx Calendar |
|---|---|---|
| 21d | 21 | 1 month |
| 63d | 63 | 3 months |
| 126d | 126 | 6 months |
| 252d | 252 | 1 year |
| 756d | 756 | 3 years |

---

## Table: `scoring_result`
Composite scrip scores produced by the Scoring stage. One row per symbol per trade date.

```sql
CREATE TABLE scoring_result (
    exchange                TEXT    NOT NULL,
    trade_date              TEXT    NOT NULL,
    symbol                  TEXT    NOT NULL,
    overall_score           REAL,
    momentum_score          REAL,
    value_score             REAL,
    quality_score           REAL,
    technical_strength      REAL,
    volume_liquidity        REAL,
    institutional_score     REAL,
    fno_score               REAL,
    futures_basis_score     REAL,
    oi_trend_score          REAL,
    sector_momentum_score   REAL,
    volatility_score        REAL,
    confirmation_score      REAL,
    nifty500_member         INTEGER,
    heuristic_penalties     TEXT,
    rank                    INTEGER,
    percentile              REAL,
    PRIMARY KEY (exchange, trade_date, symbol)
);
```

### Column Details
| Column | Description |
|---|---|
| `overall_score` | Weighted composite score (0-100) |
| `momentum_score` / `value_score` / `quality_score` / `technical_strength` / `volume_liquidity` | Core factor breakdown (0-100) |
| `institutional_score` / `fno_score` / `futures_basis_score` / `oi_trend_score` / `sector_momentum_score` / `volatility_score` / `confirmation_score` | Extended factor breakdown (0-100) |
| `nifty500_member` | 1 if symbol is Nifty 500 constituent |
| `heuristic_penalties` | JSON string of per-heuristic penalty adjustments |
| `rank` | Rank within the symbol universe for the given trade date (1 = best) |
| `percentile` | Percentile rank (0-100) |

---

## Table: `futures_data`
FO UDiFF futures data via NSE daily-reports API. One row per symbol per expiry per trade date.

```sql
CREATE TABLE futures_data (
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    expiry_date     TEXT    NOT NULL,
    futures_open    REAL,
    futures_high    REAL,
    futures_low     REAL,
    futures_close   REAL,
    futures_oi      INTEGER,
    futures_oi_chg  INTEGER,
    futures_volume  INTEGER,
    spot_price      REAL,
    basis_pct       REAL,
    oi_change_pct   REAL,
    PRIMARY KEY (trade_date, symbol, expiry_date)
);
```

### Column Details
| Column | Description |
|---|---|
| `basis_pct` | (futures_close - spot_price) / spot_price × 100 |
| `oi_change_pct` | (futures_oi_chg / futures_oi) × 100 |
| `futures_volume` | Total traded volume (aliased, not in PK) |

---

## Table: `scoring_picks`
Top picks produced by the Scoring stage. Sector-diversified top 20.

```sql
CREATE TABLE scoring_picks (
    trade_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    score           REAL,
    rank            INTEGER,
    sector          TEXT,
    PRIMARY KEY (trade_date, symbol)
);
```

---

## Table: `scoring_performance`
Forward-return verification for past picks.

```sql
CREATE TABLE scoring_performance (
    pick_date       TEXT    NOT NULL,
    check_date      TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    entry_price     REAL,
    exit_price      REAL,
    return_pct      REAL,
    PRIMARY KEY (pick_date, check_date, symbol)
);
```

---

## Table: `predicted_stock`
Forward-return validation with multiple targets and windows.

```sql
CREATE TABLE predicted_stock (
    pick_date       TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    entry_date      TEXT,
    entry_price     REAL,
    tg1_price       REAL,
    tg1_date        TEXT,
    tg1_high        REAL,
    tg1_close       REAL,
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
    target_hit      INTEGER DEFAULT 0,
    data_complete   INTEGER DEFAULT 0,
    PRIMARY KEY (pick_date, symbol)
);
```

### Column Details
| Column | Description |
|---|---|
| `entry_price` | open × (1 + slippage_pct/100), or high price if slippage_pct=null |
| `tg1_price` / `tg2_price` / `tg3_price` | Target prices for 3 levels |
| `tg1_date` / `tg2_date` / `tg3_date` | First date high reached target (null = not hit) |
| `target_hit` | 0=none, 1=tg1, 2=tg2, 3=tg3 (hierarchical) |

---

## Table: `shareholding`
Quarterly shareholding data.

```sql
CREATE TABLE shareholding (
    symbol          TEXT    NOT NULL,
    period          TEXT    NOT NULL,
    quarter_end     TEXT,
    quarter_end_int INTEGER,
    promoter_pct    REAL,
    fii_pct         REAL,
    dii_pct         REAL,
    public_pct      REAL,
    PRIMARY KEY (symbol, period)
);
```

---

## Table: `fno_membership`
F&O membership with point-in-time validity.

```sql
CREATE TABLE fno_membership (
    symbol      TEXT    NOT NULL,
    valid_from  TEXT    NOT NULL,
    valid_to    TEXT,
    PRIMARY KEY (symbol, valid_from)
);
```

---

## Table: `index_membership`
Index and sector membership with point-in-time validity.

```sql
CREATE TABLE index_membership (
    symbol      TEXT    NOT NULL,
    index_name  TEXT    NOT NULL,
    index_id    INTEGER NOT NULL,
    valid_from  TEXT    NOT NULL,
    valid_to    TEXT,
    weightage   REAL,
    PRIMARY KEY (symbol, index_name, index_id, valid_from)
);
```

20 sector indices mapped to normalized sector names. Nifty 500 = quality gate.

---

## Recommended Indexes

```sql
-- Time-series lookups by symbol
CREATE INDEX idx_stage_sym_date          ON stage         (symbol, trade_date);
CREATE INDEX idx_daily_sym_date          ON daily         (symbol, trade_date);
CREATE INDEX idx_delivery_sym_date       ON delivery      (symbol, trade_date);
CREATE INDEX idx_technical_sym_date      ON technical     (symbol, trade_date);
CREATE INDEX idx_pricelevel_sym_date     ON price_level   (symbol, trade_date);
CREATE INDEX idx_momentum_sym_date       ON momentum      (symbol, trade_date);
CREATE INDEX idx_volatility_sym_date     ON volatility    (symbol, trade_date);
CREATE INDEX idx_averages_sym_date       ON averages      (symbol, trade_date);
CREATE INDEX idx_scoring_sym_date        ON scoring_result(symbol, trade_date);
CREATE INDEX idx_futures_sym_date        ON futures_data  (symbol, trade_date);

-- Date-range queries
CREATE INDEX idx_stage_date              ON stage         (trade_date);
CREATE INDEX idx_daily_date              ON daily         (trade_date);
CREATE INDEX idx_averages_date           ON averages      (trade_date);
CREATE INDEX idx_scoring_date            ON scoring_result(trade_date);
CREATE INDEX idx_futures_date            ON futures_data  (trade_date);
```
