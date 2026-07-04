"""Averages Stage.

Computes rolling means at 5 windows (21/63/126/252/756 trading days)
for 5 metrics: close, volume, RSI, delivery %, volatility,
plus volume technical metrics (vol_5d_avg, vol_10d_avg, vol_20d_avg,
vol_ratio, vol_breakout_up, vol_trend_5d, volume_score).

Reads from daily, momentum, volatility tables.
Writes to averages table.
"""

import logging

import numpy as np
import pandas as pd

from db.connection import get_connection

logger = logging.getLogger("runner")

EXCHANGE = "NSE"

WINDOWS = [21, 63, 126, 252, 756]
METRICS = ["close", "volume", "rsi", "delivery_pct", "volatility"]

# ── SQL Queries ──────────────────────────────────────────────────────────────

DAILY_READ_SQL = """
    SELECT exchange, trade_date, symbol,
           open_price, close_price, traded_volume, delivery_pct
    FROM daily
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

MOMENTUM_READ_SQL = """
    SELECT exchange, trade_date, symbol, rsi_14
    FROM momentum
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

VOLATILITY_READ_SQL = """
    SELECT exchange, trade_date, symbol, historical_vol_20d
    FROM volatility
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

INSERT_SQL = """
    INSERT OR REPLACE INTO averages
    (exchange, trade_date, symbol,
     close_avg_21d, close_avg_63d, close_avg_126d, close_avg_252d, close_avg_756d,
     volume_avg_21d, volume_avg_63d, volume_avg_126d, volume_avg_252d, volume_avg_756d,
     rsi_avg_21d, rsi_avg_63d, rsi_avg_126d, rsi_avg_252d, rsi_avg_756d,
     delivery_pct_avg_21d, delivery_pct_avg_63d, delivery_pct_avg_126d, delivery_pct_avg_252d, delivery_pct_avg_756d,
     volatility_avg_21d, volatility_avg_63d, volatility_avg_126d, volatility_avg_252d, volatility_avg_756d,
     vol_5d_avg, vol_10d_avg, vol_20d_avg, vol_ratio, vol_breakout_up, vol_trend_5d, volume_score)
    VALUES (?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── Core Logic ───────────────────────────────────────────────────────────────

def _compute_averages_for_group(
    group: pd.DataFrame,
    col_name: str,
) -> pd.DataFrame:
    """Compute rolling means at all 5 windows for a given column.

    Args:
        group: DataFrame sorted by trade_date for one symbol.
        col_name: Name of the column to average.

    Returns:
        DataFrame with columns: trade_date, symbol, and
        {col_name}_avg_{w}d for each window.
    """
    df = group.sort_values("trade_date").copy()
    series = df[col_name].fillna(0)

    result_cols = {"trade_date": df["trade_date"], "symbol": df["symbol"]}
    for w in WINDOWS:
        avg_col = series.rolling(w, min_periods=1).mean().round(2)
        result_cols[f"{col_name}_avg_{w}d"] = avg_col

    return pd.DataFrame(result_cols)


def run_stage(start_date: str, end_date: str):
    """Run the averages stage.

    Reads close, volume, RSI, delivery %, and historical volatility data,
    computes rolling means at 5 windows, batch upserts into averages table.
    """
    logger.info("Starting averages stage: %s to %s", start_date, end_date)

    conn = get_connection()

    # ── 1. Read all source data ───────────────────────────────────────
    daily_df = pd.read_sql(DAILY_READ_SQL, conn, params=(start_date, end_date))
    if daily_df.empty:
        logger.warning("No daily data found for date range")
        conn.close()
        return

    momentum_df = pd.read_sql(MOMENTUM_READ_SQL, conn, params=(start_date, end_date))
    volatility_df = pd.read_sql(VOLATILITY_READ_SQL, conn, params=(start_date, end_date))

    logger.info(
        "Read %d daily, %d momentum, %d volatility rows",
        len(daily_df), len(momentum_df), len(volatility_df),
    )

    # ── 2. Merge all sources ──────────────────────────────────────────
    merged = daily_df.merge(
        momentum_df[["exchange", "trade_date", "symbol", "rsi_14"]],
        on=["exchange", "trade_date", "symbol"],
        how="left",
    ).merge(
        volatility_df[["exchange", "trade_date", "symbol", "historical_vol_20d"]],
        on=["exchange", "trade_date", "symbol"],
        how="left",
    )

    merged.rename(columns={
        "close_price": "close",
        "traded_volume": "volume",
        "rsi_14": "rsi",
        "delivery_pct": "delivery_pct",
        "historical_vol_20d": "volatility",
    }, inplace=True)

    # Fill missing values
    merged["rsi"] = merged["rsi"].fillna(50)
    merged["delivery_pct"] = merged["delivery_pct"].fillna(0)
    merged["volatility"] = merged["volatility"].fillna(0)

    merged.sort_values(["symbol", "trade_date"], inplace=True)
    merged.reset_index(drop=True, inplace=True)

    # ── 3. Compute rolling averages per symbol per metric ─────────────
    all_results = None

    for metric_name, col in [
        ("close", "close_price"),
        ("volume", "traded_volume"),
        ("rsi", "rsi"),
        ("delivery_pct", "delivery_pct"),
        ("volatility", "volatility"),
    ]:
        logger.debug("Computing averages for %s...", metric_name)

        parts = []
        for symbol, group in merged.groupby("symbol", sort=False):
            computed = _compute_averages_for_group(group, metric_name)
            parts.append(computed)

        metric_result = pd.concat(parts, ignore_index=True)

        # Drop trade_date and symbol from subsequent merges (keep only once)
        if all_results is None:
            all_results = metric_result[["trade_date", "symbol"]].copy()

        avg_cols = [f"{metric_name}_avg_{w}d" for w in WINDOWS]
        all_results = all_results.merge(
            metric_result[["trade_date", "symbol"] + avg_cols],
            on=["trade_date", "symbol"],
            how="left",
        )

    # ── 4. Compute volume technical metrics ───────────────────────────
    logger.debug("Computing volume technical metrics...")

    # vol_5d_avg, vol_10d_avg, vol_20d_avg
    vol = merged["volume"]
    merged["vol_5d_avg"] = vol.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    ).round(2)
    merged["vol_10d_avg"] = vol.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(10, min_periods=1).mean()
    ).round(2)
    merged["vol_20d_avg"] = vol.groupby(merged["symbol"]).transform(
        lambda x: x.rolling(20, min_periods=1).mean()
    ).round(2)

    # vol_ratio
    merged["vol_ratio"] = np.where(
        merged["vol_20d_avg"] > 0,
        (merged["volume"] / merged["vol_20d_avg"]).round(2),
        0.0,
    )

    # vol_breakout_up
    merged["vol_breakout_up"] = (
        (merged["vol_ratio"] >= 1.5) & (merged["close"] > merged["open_price"])
    ).astype(int)

    # vol_trend_5d (linear slope of volume over 5d)
    merged["vol_trend_5d"] = 0.0
    for symbol, group in merged.groupby("symbol"):
        idx = group.index
        merged.loc[idx, "vol_trend_5d"] = _compute_slope_5d(
            group["volume"].values
        )

    # volume_score (composite)
    # vol_ratio_score(0-30) + trend_score(0-20) + vs_avg_score(0-25) + breakout_score(0-25)
    vol_ratio_score = np.minimum(merged["vol_ratio"] / 3.0 * 30, 30)

    trend_raw = merged["vol_trend_5d"].fillna(0)
    max_trend = trend_raw.abs().max()
    if max_trend > 0:
        trend_score = np.maximum(0, (trend_raw / max_trend) * 20)
    else:
        trend_score = np.zeros(len(merged))

    vs_ratio = np.where(
        merged["vol_5d_avg"] > 0,
        merged["volume"] / merged["vol_5d_avg"],
        0,
    )
    vs_avg_score = np.minimum(vs_ratio * 15, 25)

    breakout_score = merged["vol_breakout_up"].astype(float) * 25

    merged["volume_score"] = np.minimum(
        vol_ratio_score + trend_score + vs_avg_score + breakout_score, 100
    ).round(2)

    # Attach volume metric columns to all_results
    vol_metric_cols = [
        "vol_5d_avg", "vol_10d_avg", "vol_20d_avg", "vol_ratio",
        "vol_breakout_up", "vol_trend_5d", "volume_score",
    ]
    all_results = all_results.merge(
        merged[["trade_date", "symbol"] + vol_metric_cols],
        on=["trade_date", "symbol"],
        how="left",
    )

    # ── 5. Build batch for upsert ─────────────────────────────────────
    cursor = conn.cursor()
    batch = []

    # Create a lookup from merged for exchange
    exchange_lookup = merged.set_index(["trade_date", "symbol"])["exchange"].to_dict()

    for _, row in all_results.iterrows():
        key = (row["trade_date"], row["symbol"])
        exchange = exchange_lookup.get(key, EXCHANGE)

        row_data = [exchange, row["trade_date"], row["symbol"]]
        for metric in METRICS:
            for w in WINDOWS:
                col = f"{metric}_avg_{w}d"
                row_data.append(_get(row, col))

        # Volume technical metrics
        for col in vol_metric_cols:
            row_data.append(_get(row, col, 0))

        batch.append(tuple(row_data))
        if len(batch) >= 500:
            cursor.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        cursor.executemany(INSERT_SQL, batch)

    conn.commit()
    conn.close()

    logger.info("Completed averages stage: %d rows upserted", len(all_results))


# ── Standalone ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_stage("2025-01-01", "2026-06-30")
