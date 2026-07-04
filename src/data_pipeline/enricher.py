"""Enrich Stage.

Joins stage + stage_delivery data, computes delivery rolling averages,
wvap, and 5-day closing extremes.

Writes to:
  - daily table (OHLCV + delivery metrics + wvap + closing extremes)
"""

import logging
import time

import numpy as np
import pandas as pd

from db.connection import get_connection

logger = logging.getLogger("runner")

EXCHANGE = "NSE"

# ── SQL Queries ──────────────────────────────────────────────────────────────

STAGE_READ_SQL = """
    SELECT exchange, trade_date, symbol,
           open_price, high_price, low_price, close_price,
           previous_close, traded_volume, traded_value,
           day_return_pct, upper_circuit_hit, lower_circuit_hit,
           isin
    FROM stage
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

DELIVERY_READ_SQL = """
    SELECT exchange, trade_date, symbol,
           qty, pct
    FROM stage_delivery
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

DAILY_INSERT_SQL = """
    INSERT OR REPLACE INTO daily
    (exchange, trade_date, symbol,
     open_price, high_price, low_price, close_price,
     previous_close, traded_volume, traded_value,
     day_return_pct, upper_circuit_hit, lower_circuit_hit,
     delivery_volume, delivery_value, delivery_pct,
     wvap_price, isin,
     lowest_closing_5days, highest_closing_5days,
     delivery_qty_5d_avg, delivery_qty_20d_avg, delivery_pct_5d_avg, delivery_pct_20d_avg,
     delivery_pct_trend, vol_spike)
    VALUES (?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?,
            ?, ?, ?,
            ?, ?, ?,
            ?, ?,
            ?, ?,
            ?, ?, ?, ?,
            ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_div(a, b, default=0.0):
    """Safe division, returns default when divisor is zero or NaN."""
    if b is None:
        return default
    if isinstance(b, (int, float)) and b == 0:
        return default
    try:
        return a / b
    except (ZeroDivisionError, TypeError, ValueError):
        return default


def _compute_slope_5d(values: np.ndarray) -> np.ndarray:
    """Compute linear slope over rolling 5-day windows.

    Args:
        values: 1D numpy array of values.

    Returns:
        Array of slope values, one per element.
    """
    n = len(values)
    result = np.zeros(n, dtype=float)
    for i in range(n):
        start = max(0, i - 4)
        segment = values[start:i + 1]
        if len(segment) >= 2:
            x = np.arange(len(segment), dtype=float)
            y = segment.astype(float)
            try:
                slope = np.polyfit(x, y, 1)[0]
                result[i] = round(float(slope), 2)
            except (np.linalg.LinAlgError, ValueError):
                result[i] = 0.0
    return result


# ── Core Logic ───────────────────────────────────────────────────────────────

def run_stage(start_date: str, end_date: str):
    """Run the enrich stage.

    Reads stage + stage_delivery data, computes wvap, delivery metrics,
    and 5-day closing extremes, writes to daily table.
    """
    _t0 = time.perf_counter()
    logger.info("Starting enrich stage: %s to %s", start_date, end_date)
    conn = get_connection()

    # ── 1. Read stage data ─────────────────────────────────────────────
    stage_df = pd.read_sql(STAGE_READ_SQL, conn, params=(start_date, end_date))
    if stage_df.empty:
        logger.warning("No stage data found for date range")
        conn.close()
        return

    # ── 2. Read stage_delivery data ────────────────────────────────────
    delivery_df = pd.read_sql(DELIVERY_READ_SQL, conn, params=(start_date, end_date))

    # ── 3. Merge stage + stage_delivery ────────────────────────────────
    merged = stage_df.merge(
        delivery_df,
        on=["exchange", "trade_date", "symbol"],
        how="left",
        suffixes=("", "_del"),
    )

    # Fill missing delivery values
    merged["qty"] = merged["qty"].fillna(0).astype(np.int64)
    merged["pct"] = merged["pct"].fillna(0.0).astype(float)

    # Sort for rolling operations
    merged.sort_values(["symbol", "trade_date"], inplace=True)
    merged.reset_index(drop=True, inplace=True)

    # ── 4. Compute wvap_price ──────────────────────────────────────────
    # Volume-weighted average price (simple approximation)
    merged["wvap_price"] = (
        (merged["high_price"] + merged["low_price"] + merged["close_price"]) / 3.0
    ).round(2)

    # ── 5. Compute delivery_value ──────────────────────────────────────
    merged["delivery_value"] = (merged["qty"] * merged["wvap_price"]).round(2)

    # ── 6. lowest_closing_5days ────────────────────────────────────────
    # True when today's close is the lowest closing price in the last 5
    # trading days for this symbol.  Uses min_periods=5 so the first 4 rows
    # per symbol always produce False.
    min_close_5d = merged.groupby("symbol")["close_price"].transform(
        lambda x: x.rolling(5, min_periods=5).min()
    )
    merged["lowest_closing_5days"] = (
        merged["close_price"] == min_close_5d
    ).astype(int)

    # ── 7. highest_closing_5days ───────────────────────────────────────
    # True when today's close is the highest closing price in the last 5
    # trading days for this symbol.
    max_close_5d = merged.groupby("symbol")["close_price"].transform(
        lambda x: x.rolling(5, min_periods=5).max()
    )
    merged["highest_closing_5days"] = (
        merged["close_price"] == max_close_5d
    ).astype(int)

    # ── 8. Delivery rolling averages ───────────────────────────────────
    qty = merged["qty"]
    pct = merged["pct"]

    merged["delivery_qty_5d_avg"] = qty.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    ).round(2)
    merged["delivery_qty_20d_avg"] = qty.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(20, min_periods=1).mean()
    ).round(2)
    merged["delivery_pct_5d_avg"] = pct.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    ).round(2)
    merged["delivery_pct_20d_avg"] = pct.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(20, min_periods=1).mean()
    ).round(2)

    # ── 9. delivery_pct_trend (linear slope of delivery pct over 5d) ───
    merged["delivery_pct_trend"] = 0.0
    for symbol, group in merged.groupby("symbol"):
        idx = group.index
        merged.loc[idx, "delivery_pct_trend"] = _compute_slope_5d(
            group["pct"].values
        )

    # ── 10. vol_spike ──────────────────────────────────────────────────
    merged["vol_spike"] = (
        (merged["qty"] > 2 * merged["delivery_qty_20d_avg"]) & (merged["delivery_qty_20d_avg"] > 0)
    ).astype(int)

    # ── 11. Batch upsert: daily table ──────────────────────────────────
    daily_rows = []
    for _, row in merged.iterrows():
        isin_val = str(row["isin"]).strip() if pd.notna(row["isin"]) and str(row["isin"]).strip() else None
        daily_rows.append((
            EXCHANGE,
            row["trade_date"],
            row["symbol"],
            round(row["open_price"], 2),
            round(row["high_price"], 2),
            round(row["low_price"], 2),
            round(row["close_price"], 2),
            round(row["previous_close"], 2),
            int(row["traded_volume"]),
            round(row["traded_value"], 2),
            float(row["day_return_pct"]) if pd.notna(row["day_return_pct"]) else None,
            int(row["upper_circuit_hit"]),
            int(row["lower_circuit_hit"]),
            int(row["qty"]),
            float(row["delivery_value"]),
            float(row["pct"]),
            float(row["wvap_price"]),
            isin_val,
            int(row["lowest_closing_5days"]),
            int(row["highest_closing_5days"]),
            float(row["delivery_qty_5d_avg"]),
            float(row["delivery_qty_20d_avg"]),
            float(row["delivery_pct_5d_avg"]),
            float(row["delivery_pct_20d_avg"]),
            float(row["delivery_pct_trend"]),
            int(row["vol_spike"]),
        ))

    cursor = conn.cursor()
    for i in range(0, len(daily_rows), 500):
        cursor.executemany(DAILY_INSERT_SQL, daily_rows[i:i + 500])

    conn.commit()
    conn.close()

    _elapsed = time.perf_counter() - _t0
    logger.info(
        "Completed enrich stage: %d rows upserted to daily (%.2fs)",
        len(daily_rows),
        _elapsed,
    )


# ── Standalone ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_stage("2025-01-01", "2026-06-30")
