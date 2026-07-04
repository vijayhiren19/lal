"""Volatility Stage.

Computes ATR(14), Bollinger Bands(20,2σ), Keltner Channels,
historical volatility (20d).

Reads from daily table, writes to volatility table.
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
           open_price, high_price, low_price, close_price,
           traded_volume
    FROM daily
    WHERE trade_date BETWEEN ? AND ?
    ORDER BY symbol, trade_date
"""

INSERT_SQL = """
    INSERT OR REPLACE INTO volatility
    (exchange, trade_date, symbol,
     atr_14, atr_pct,
     bb_upper, bb_middle, bb_lower, bb_width, bb_squeeze,
     keltner_upper, keltner_lower,
     historical_vol_20d)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_rnd(result):
    """Round a pandas-ta result to 2 decimals, handling None."""
    if result is not None:
        return result.round(2)
    return None


def _compute_volatility_for_group(group: pd.DataFrame) -> pd.DataFrame:
    """Compute all volatility indicators for a single symbol group.

    Args:
        group: DataFrame sorted by trade_date for one symbol.

    Returns:
        DataFrame with volatility columns added.
    """
    df = group.sort_values("trade_date").copy()
    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]

    n = len(df)

    # ── ATR ───────────────────────────────────────────────────────────
    df["atr_14"] = _safe_rnd(ta.atr(high, low, close, length=14))
    df["atr_pct"] = np.where(
        df["atr_14"].notna() & (close != 0),
        (df["atr_14"] / close * 100).round(2),
        np.nan,
    )

    # ── Bollinger Bands ───────────────────────────────────────────────
    bb_result = ta.bbands(close, length=20, std=2)
    if bb_result is not None:
        df["bb_upper"] = bb_result.iloc[:, 0].round(2)
        df["bb_middle"] = bb_result.iloc[:, 1].round(2)
        df["bb_lower"] = bb_result.iloc[:, 2].round(2)

        # bb_width = (upper - lower) / middle
        df["bb_width"] = np.where(
            df["bb_middle"].notna() & (df["bb_middle"] != 0),
            ((df["bb_upper"] - df["bb_lower"]) / df["bb_middle"]).round(4),
            np.nan,
        )

        # bb_squeeze: bb_width < 20-period rolling mean of bb_width
        bb_width_mean = df["bb_width"].rolling(20, min_periods=1).mean()
        df["bb_squeeze"] = np.where(
            df["bb_width"].notna() & bb_width_mean.notna(),
            (df["bb_width"] < bb_width_mean).astype(int),
            0,
        )
    else:
        df["bb_upper"] = np.nan
        df["bb_middle"] = np.nan
        df["bb_lower"] = np.nan
        df["bb_width"] = np.nan
        df["bb_squeeze"] = 0

    # ── Keltner Channels ──────────────────────────────────────────────
    kc_result = ta.kc(high, low, close, length=20, scalar=1.5, mamode="ema")
    if kc_result is not None:
        # KC returns KCUe_20_1.5, KCBe_20_1.5, KCLe_20_1.5
        df["keltner_upper"] = kc_result.iloc[:, 0].round(2)
        df["keltner_lower"] = kc_result.iloc[:, 2].round(2)
    else:
        df["keltner_upper"] = np.nan
        df["keltner_lower"] = np.nan

    # ── Historical Volatility (20d) ───────────────────────────────────
    # annualised std of log returns
    log_returns = np.log(close / close.shift(1))
    hv = log_returns.rolling(20, min_periods=1).std() * np.sqrt(252) * 100
    df["historical_vol_20d"] = hv.round(2)

    return df


# ── Stage Entry Point ────────────────────────────────────────────────────────

def run_stage(start_date: str, end_date: str):
    """Run the volatility indicators stage.

    Reads daily OHLCV data, computes volatility indicators per symbol,
    batch upserts into the volatility table.
    """
    _t0 = time.perf_counter()
    logger.info("Starting volatility stage: %s to %s", start_date, end_date)

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
        computed = _compute_volatility_for_group(group)
        results.append(computed)

    result_df = pd.concat(results, ignore_index=True)
    logger.info("Computed volatility indicators for %d rows", len(result_df))

    # 3. Batch upsert
    cursor = conn.cursor()
    batch = []
    for _, row in result_df.iterrows():
        batch.append((
            EXCHANGE,
            row["trade_date"],
            row["symbol"],
            _get(row, "atr_14"),
            _get(row, "atr_pct"),
            _get(row, "bb_upper"),
            _get(row, "bb_middle"),
            _get(row, "bb_lower"),
            _get(row, "bb_width"),
            int(_get(row, "bb_squeeze", 0)),
            _get(row, "keltner_upper"),
            _get(row, "keltner_lower"),
            _get(row, "historical_vol_20d"),
        ))
        if len(batch) >= 500:
            cursor.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        cursor.executemany(INSERT_SQL, batch)

    conn.commit()
    conn.close()

    _elapsed = time.perf_counter() - _t0
    logger.info("Completed volatility stage: %d rows upserted (%.2fs)", len(result_df), _elapsed)


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
