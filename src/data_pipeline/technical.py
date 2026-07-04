"""Technical Stage.

Computes SMA(20/50/100/200), EMA(9/20/50/200), crossovers,
trend stage classification and scoring.

Reads from daily table, writes to technical table.
"""

import logging
import time

import numpy as np
import pandas as pd
import pandas_ta as ta

from db.connection import get_connection

logger = logging.getLogger("runner")

EXCHANGE = "NSE"

# ── SQL ──────────────────────────────────────────────────────────────────────

DAILY_READ_SQL = """
    SELECT exchange, trade_date, symbol,
           close_price, open_price
    FROM daily
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

INSERT_SQL = """
    INSERT OR REPLACE INTO technical
    (exchange, trade_date, symbol,
     sma_20, sma_50, sma_100, sma_200,
     ema_9, ema_20, ema_50, ema_200,
     price_vs_sma20_pct, price_vs_sma50_pct, price_vs_sma200_pct,
     golden_cross, ema20_gt_ema50, price_gt_sma200,
     trend_stage, trend_score, trend_strength)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_rnd(result):
    """Round a pandas-ta result to 2 decimals, handling None."""
    if result is not None:
        return result.round(2)
    return None


def _compute_technical_for_group(group: pd.DataFrame) -> pd.DataFrame:
    """Compute all technical indicators for a single symbol group.

    Args:
        group: DataFrame sorted by trade_date for one symbol.

    Returns:
        DataFrame with computed columns added.
    """
    df = group.sort_values("trade_date").copy()
    close = df["close_price"]

    # ── Moving Averages ───────────────────────────────────────────────
    ma_cols = {}
    for length in [20, 50, 100, 200]:
        result = ta.sma(close, length=length)
        ma_cols[f"sma_{length}"] = _safe_rnd(result)
    for length in [9, 20, 50, 200]:
        result = ta.ema(close, length=length)
        ma_cols[f"ema_{length}"] = _safe_rnd(result)
    for col, series in ma_cols.items():
        df[col] = pd.to_numeric(series, errors="coerce")

    # ── Price vs MA percentages ───────────────────────────────────────
    for col, name in [("sma_20", "20"), ("sma_50", "50"), ("sma_200", "200")]:
        pct_col = f"price_vs_sma{name}_pct"
        sma = df[col]
        df[pct_col] = np.where(
            sma.notna() & (sma != 0),
            ((close - sma) / sma * 100).round(2),
            np.nan,
        )

    # ── Golden Cross ──────────────────────────────────────────────────
    df["golden_cross"] = 0
    if len(df) >= 2:
        sma50_vals = df["sma_50"].fillna(np.nan).to_numpy(dtype=float)
        sma200_vals = df["sma_200"].fillna(np.nan).to_numpy(dtype=float)
        both_ok = ~np.isnan(sma50_vals) & ~np.isnan(sma200_vals)
        prev_sma50 = np.roll(sma50_vals, 1)
        prev_sma200 = np.roll(sma200_vals, 1)
        prev_sma50[0] = np.nan
        prev_sma200[0] = np.nan
        gc = np.where(
            both_ok & (sma50_vals > sma200_vals)
            & ~np.isnan(prev_sma50) & ~np.isnan(prev_sma200)
            & (prev_sma50 <= prev_sma200),
            1, 0
        )
        df["golden_cross"] = gc.astype(int)

    # ── EMA comparison flags ──────────────────────────────────────────
    ema20_vals = df["ema_20"].fillna(np.nan).to_numpy(dtype=float)
    ema50_vals = df["ema_50"].fillna(np.nan).to_numpy(dtype=float)
    ema_both_ok = ~np.isnan(ema20_vals) & ~np.isnan(ema50_vals)
    df["ema20_gt_ema50"] = np.where(
        ema_both_ok & (ema20_vals > ema50_vals),
        1, 0
    ).astype(int)

    df["price_gt_sma200"] = np.where(
        df["sma_200"].notna(),
        (close > df["sma_200"]).astype(int),
        0,
    )

    # ── Trend Stage ───────────────────────────────────────────────────
    conditions = [
        (df["ema_20"].notna() & (df["ema_20"] > df["ema_50"]))
        & (df["sma_200"].notna() & (close > df["sma_200"])),
        (df["ema_20"].notna() & (df["ema_20"] < df["ema_50"]))
        & (df["sma_200"].notna() & (close < df["sma_200"])),
        (df["sma_200"].notna() & (close > df["sma_200"]))
        & (df["ema_20"].notna() & (df["ema_20"] < df["ema_50"])),
    ]
    choices = ["bullish", "bearish", "recovering"]
    df["trend_stage"] = np.select(conditions, choices, default="range-bound")

    # ── Trend Score (0-100) ───────────────────────────────────────────
    # Components: sma200_distance(0-30) + sma50_distance(0-25)
    #             + ema50_strength(0-20) + golden_cross(0-15) + stage(0-10)
    score = np.zeros(len(df), dtype=float)

    # sma200 distance (0-30): how far price is from 200 SMA (%)
    sma200_dist = np.where(
        df["sma_200"].notna() & (df["sma_200"] != 0),
        np.abs((close - df["sma_200"]) / df["sma_200"] * 100),
        0,
    )
    score += np.minimum(sma200_dist * 3, 30)

    # sma50 distance (0-25)
    sma50_dist = np.where(
        df["sma_50"].notna() & (df["sma_50"] != 0),
        np.abs((close - df["sma_50"]) / df["sma_50"] * 100),
        0,
    )
    score += np.minimum(sma50_dist * 2.5, 25)

    # ema50 strength (0-20): if ema20 > ema50
    score += np.where(df["ema_20"].notna() & df["ema_50"].notna(), 10, 0).astype(float)
    score += np.where(
        df["ema_20"].notna() & df["ema_50"].notna()
        & (df["ema_20"] > df["ema_50"]),
        10,
        0,
    ).astype(float)

    # golden_cross (0-15)
    score += (df["golden_cross"].astype(float) * 15)

    # stage bonus (0-10)
    stage_bonus = np.select(
        [df["trend_stage"] == "bullish",
         df["trend_stage"] == "recovering",
         df["trend_stage"] == "range-bound"],
        [10, 5, 2],
        default=0,
    )
    score += stage_bonus

    df["trend_score"] = np.minimum(score, 100).round(2)

    # ── Trend Strength (0-100) ────────────────────────────────────────
    # ma_gap: distance between ema_20 and ema_50 as % of close
    strength = np.zeros(len(df), dtype=float)
    ma_gap = np.where(
        df["ema_20"].notna() & df["ema_50"].notna() & (close != 0),
        np.abs(df["ema_20"] - df["ema_50"]) / close * 100,
        0,
    )
    strength += np.minimum(ma_gap * 10, 50)

    # close_change: 20-day price change magnitude
    if len(df) >= 20:
        chg_20d = close.pct_change(20).fillna(0).abs() * 100
        strength += np.minimum(chg_20d, 50)
    else:
        strength += np.minimum(close.pct_change().fillna(0).abs() * 100, 50)

    df["trend_strength"] = np.minimum(strength, 100).round(2)

    return df


# ── Stage Entry Point ────────────────────────────────────────────────────────

def run_stage(start_date: str, end_date: str):
    """Run the technical indicators stage.

    Reads daily data, computes all technical indicators per symbol,
    batch upserts into the technical table.
    """
    _t0 = time.perf_counter()
    logger.info("Starting technical stage: %s to %s", start_date, end_date)

    conn = get_connection()

    # 1. Read all daily data for the date range
    df = pd.read_sql(DAILY_READ_SQL, conn, params=(start_date, end_date))
    if df.empty:
        logger.warning("No daily data found for date range")
        conn.close()
        return

    logger.info("Read %d rows from daily table", len(df))

    # 2. Compute technicals per symbol
    results = []
    for symbol, group in df.groupby("symbol", sort=False):
        computed = _compute_technical_for_group(group)
        results.append(computed)

    result_df = pd.concat(results, ignore_index=True)
    logger.info("Computed technicals for %d rows", len(result_df))

    # 3. Batch upsert
    cursor = conn.cursor()
    batch = []
    for _, row in result_df.iterrows():
        batch.append((
            EXCHANGE,
            row["trade_date"],
            row["symbol"],
            _get(row, "sma_20"),
            _get(row, "sma_50"),
            _get(row, "sma_100"),
            _get(row, "sma_200"),
            _get(row, "ema_9"),
            _get(row, "ema_20"),
            _get(row, "ema_50"),
            _get(row, "ema_200"),
            _get(row, "price_vs_sma20_pct"),
            _get(row, "price_vs_sma50_pct"),
            _get(row, "price_vs_sma200_pct"),
            int(_get(row, "golden_cross", 0)),
            int(_get(row, "ema20_gt_ema50", 0)),
            int(_get(row, "price_gt_sma200", 0)),
            str(row.get("trend_stage", "range-bound")),
            _get(row, "trend_score"),
            _get(row, "trend_strength"),
        ))
        if len(batch) >= 500:
            cursor.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        cursor.executemany(INSERT_SQL, batch)

    conn.commit()
    conn.close()

    _elapsed = time.perf_counter() - _t0
    logger.info("Completed technical stage: %d rows upserted (%.2fs)", len(result_df), _elapsed)


def _get(row, col, default=None):
    """Safely get a value from a pandas row, converting to float if numeric."""
    val = row.get(col, default)
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return default
    if isinstance(val, (int, float, np.integer, np.floating)):
        return float(val)
    return val


# ── Standalone ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_stage("2025-01-01", "2026-06-30")
