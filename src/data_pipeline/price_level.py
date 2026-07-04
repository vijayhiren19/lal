"""Price Level Stage.

Computes 52-week/26-week/4-week highs and lows,
pivot points, and breakout signals.

Reads from daily table, writes to price_level table.
"""

import logging

import numpy as np
import pandas as pd

from db.connection import get_connection

logger = logging.getLogger("runner")

EXCHANGE = "NSE"

# ── SQL ──────────────────────────────────────────────────────────────────────

DAILY_READ_SQL = """
    SELECT exchange, trade_date, symbol,
           open_price, high_price, low_price, close_price
    FROM daily
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

INSERT_SQL = """
    INSERT OR REPLACE INTO price_level
    (exchange, trade_date, symbol,
     week52_high, week52_low, week26_high, week26_low,
     week4_high, week4_low,
     pct_from_52wk_high, pct_from_52wk_low, pct_from_4wk_high,
     near_52wk_high_flag, near_52wk_low_flag, breakout_flag,
     pivot_p, pivot_r1, pivot_r2, pivot_s1, pivot_s2)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────


def _compute_price_levels_for_group(group: pd.DataFrame) -> pd.DataFrame:
    """Compute price level indicators for a single symbol group.

    Args:
        group: DataFrame sorted by trade_date for one symbol.

    Returns:
        DataFrame with price level columns added.
    """
    df = group.sort_values("trade_date").copy()
    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]

    # ── Rolling Highs / Lows ──────────────────────────────────────────
    df["week52_high"] = high.rolling(252, min_periods=1).max().round(2)
    df["week52_low"] = low.rolling(252, min_periods=1).min().round(2)
    df["week26_high"] = high.rolling(126, min_periods=1).max().round(2)
    df["week26_low"] = low.rolling(126, min_periods=1).min().round(2)
    df["week4_high"] = high.rolling(20, min_periods=1).max().round(2)
    df["week4_low"] = low.rolling(20, min_periods=1).min().round(2)

    # ── Percentage from Highs / Lows ──────────────────────────────────
    # pct_from_52wk_high: how far below 52wk high
    df["pct_from_52wk_high"] = np.where(
        df["week52_high"].notna() & (df["week52_high"] != 0),
        ((df["week52_high"] - close) / df["week52_high"] * 100).round(2),
        np.nan,
    )
    # pct_from_52wk_low: how far above 52wk low
    df["pct_from_52wk_low"] = np.where(
        df["week52_low"].notna() & (df["week52_low"] != 0),
        ((close - df["week52_low"]) / df["week52_low"] * 100).round(2),
        np.nan,
    )
    # pct_from_4wk_high
    df["pct_from_4wk_high"] = np.where(
        df["week4_high"].notna() & (df["week4_high"] != 0),
        ((df["week4_high"] - close) / df["week4_high"] * 100).round(2),
        np.nan,
    )

    # ── Flags ──────────────────────────────────────────────────────────
    df["near_52wk_high_flag"] = np.where(
        df["pct_from_52wk_high"].notna(),
        (df["pct_from_52wk_high"] <= 5).astype(int),
        0,
    )
    df["near_52wk_low_flag"] = np.where(
        df["pct_from_52wk_low"].notna(),
        (df["pct_from_52wk_low"] <= 5).astype(int),
        0,
    )

    # breakout_flag: close > previous day's week4_high (rolling 20d high)
    prev_week4_high = df["week4_high"].shift(1)
    df["breakout_flag"] = np.where(
        prev_week4_high.notna(),
        (close > prev_week4_high).astype(int),
        0,
    )

    # ── Pivot Points ──────────────────────────────────────────────────
    # Classic pivot formula using today's H/L/C (valid for next session)
    df["pivot_p"] = ((high + low + close) / 3).round(2)
    df["pivot_r1"] = (2 * df["pivot_p"] - low).round(2)
    df["pivot_r2"] = (df["pivot_p"] + (high - low)).round(2)
    df["pivot_s1"] = (2 * df["pivot_p"] - high).round(2)
    df["pivot_s2"] = (df["pivot_p"] - (high - low)).round(2)

    return df


# ── Stage Entry Point ────────────────────────────────────────────────────────

def run_stage(start_date: str, end_date: str):
    """Run the price level stage.

    Reads daily data, computes rolling highs/lows, pivots, and
    breakout signals per symbol. Batch upserts into price_level table.
    """
    logger.info("Starting price_level stage: %s to %s", start_date, end_date)

    conn = get_connection()

    # 1. Read daily data
    df = pd.read_sql(DAILY_READ_SQL, conn, params=(start_date, end_date))
    if df.empty:
        logger.warning("No daily data found for date range")
        conn.close()
        return

    logger.info("Read %d rows from daily table", len(df))

    # 2. Compute per symbol
    results = []
    for symbol, group in df.groupby("symbol", sort=False):
        computed = _compute_price_levels_for_group(group)
        results.append(computed)

    result_df = pd.concat(results, ignore_index=True)
    logger.info("Computed price levels for %d rows", len(result_df))

    # 3. Build batch
    cursor = conn.cursor()
    batch = []
    for _, row in result_df.iterrows():
        batch.append((
            EXCHANGE,
            row["trade_date"],
            row["symbol"],
            _get(row, "week52_high"),
            _get(row, "week52_low"),
            _get(row, "week26_high"),
            _get(row, "week26_low"),
            _get(row, "week4_high"),
            _get(row, "week4_low"),
            _get(row, "pct_from_52wk_high"),
            _get(row, "pct_from_52wk_low"),
            _get(row, "pct_from_4wk_high"),
            int(_get(row, "near_52wk_high_flag", 0)),
            int(_get(row, "near_52wk_low_flag", 0)),
            int(_get(row, "breakout_flag", 0)),
            _get(row, "pivot_p"),
            _get(row, "pivot_r1"),
            _get(row, "pivot_r2"),
            _get(row, "pivot_s1"),
            _get(row, "pivot_s2"),
        ))
        if len(batch) >= 500:
            cursor.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        cursor.executemany(INSERT_SQL, batch)

    conn.commit()
    conn.close()

    logger.info("Completed price_level stage: %d rows upserted", len(result_df))


def _get(row, col, default=None):
    """Safely get a value from a pandas row."""
    val = row.get(col, default)
    if val is None:
        return default
    if isinstance(val, (int, float, np.integer, np.floating)):
        if np.isnan(val):
            return default
        return float(val)
    return val


# ── Standalone ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_stage("2025-01-01", "2026-06-30")
