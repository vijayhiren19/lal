"""Momentum Stage.

Computes RSI(14/9), MACD(12/26/9), Stoch(14/3/3), MFI(14),
ADX(14), CCI(20), Williams %R(14), plus momentum_score and decay_factor.

Reads from daily table, writes to momentum table.
"""

import logging

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
    INSERT OR REPLACE INTO momentum
    (exchange, trade_date, symbol,
     rsi_14, rsi_9,
     macd, macd_signal, macd_histogram, macd_crossover,
     stoch_k, stoch_d, stoch_signal,
     mfi_14, adx_14, cci_20, williams_r_14,
     momentum_score, decay_factor)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_rnd(result):
    """Round a pandas-ta result to 2 decimals, handling None."""
    if result is not None:
        return result.round(2)
    return None


def _compute_momentum_for_group(group: pd.DataFrame) -> pd.DataFrame:
    """Compute all momentum indicators for a single symbol group.

    Args:
        group: DataFrame sorted by trade_date for one symbol.

    Returns:
        DataFrame with momentum columns added.
    """
    df = group.sort_values("trade_date").copy()
    high = df["high_price"]
    low = df["low_price"]
    close = df["close_price"]
    volume = df["traded_volume"]

    n = len(df)

    # ── Helper: force float columns ────────────────────────────────────
    numeric_cols = {}

    # ── RSI ───────────────────────────────────────────────────────────
    numeric_cols["rsi_14"] = _safe_rnd(ta.rsi(close, length=14))
    numeric_cols["rsi_9"] = _safe_rnd(ta.rsi(close, length=9))

    # ── MACD ──────────────────────────────────────────────────────────
    macd_result = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_result is not None:
        numeric_cols["macd"] = macd_result.iloc[:, 0].round(2)
        numeric_cols["macd_signal"] = macd_result.iloc[:, 1].round(2)
        numeric_cols["macd_histogram"] = macd_result.iloc[:, 2].round(2)
    else:
        numeric_cols["macd"] = pd.Series(np.nan, index=df.index)
        numeric_cols["macd_signal"] = pd.Series(np.nan, index=df.index)
        numeric_cols["macd_histogram"] = pd.Series(np.nan, index=df.index)

    # ── Stochastic ────────────────────────────────────────────────────
    stoch_result = ta.stoch(high, low, close, k=14, d=3, smooth_k=3)
    if stoch_result is not None:
        numeric_cols["stoch_k"] = stoch_result.iloc[:, 0].round(2)
        numeric_cols["stoch_d"] = stoch_result.iloc[:, 1].round(2)
    else:
        numeric_cols["stoch_k"] = pd.Series(np.nan, index=df.index)
        numeric_cols["stoch_d"] = pd.Series(np.nan, index=df.index)

    # ── MFI ───────────────────────────────────────────────────────────
    numeric_cols["mfi_14"] = _safe_rnd(ta.mfi(high, low, close, volume, length=14))

    # ── ADX ───────────────────────────────────────────────────────────
    adx_result = ta.adx(high, low, close, length=14)
    if adx_result is not None:
        numeric_cols["adx_14"] = adx_result.iloc[:, 0].round(2)
    else:
        numeric_cols["adx_14"] = pd.Series(np.nan, index=df.index)

    # ── CCI ───────────────────────────────────────────────────────────
    numeric_cols["cci_20"] = _safe_rnd(ta.cci(high, low, close, length=20))

    # ── Williams %R ───────────────────────────────────────────────────
    numeric_cols["williams_r_14"] = _safe_rnd(ta.willr(high, low, close, length=14))

    # Assign all numeric columns with coercion to float
    for col, series in numeric_cols.items():
        df[col] = pd.to_numeric(series, errors="coerce")

    # ── macd_crossover: macd crosses above signal — after df has macd + macd_signal
    if n >= 2:
        prev_macd = df["macd"].shift(1)
        prev_signal = df["macd_signal"].shift(1)
        df["macd_crossover"] = (
            (df["macd"] > df["macd_signal"])
            & (prev_macd <= prev_signal)
        ).astype(int)
    else:
        df["macd_crossover"] = 0

    # ── stoch_signal: 3-period SMA of stoch_k
    df["stoch_signal"] = pd.to_numeric(df["stoch_k"].rolling(3, min_periods=1).mean(), errors="coerce").round(2)

    # ── momentum_score (0-100) ────────────────────────────────────────
    # Composite: rsi_comp(33) + macd_comp(34) + stoch_comp(33)
    score = np.zeros(n, dtype=float)

    # RSI component (0-33): RSI between 50-70 is bullish momentum
    rsi = np.asarray(pd.to_numeric(df["rsi_14"], errors="coerce").fillna(50), dtype=float)
    rsi_comp = np.where(
        (rsi > 50) & (rsi < 70),
        (rsi - 50) / 20 * 33,  # Score 0-33 as RSI goes 50→70
        np.where(rsi >= 70, 33, 0),  # Overbought still gets max
    )
    score += np.minimum(rsi_comp, 33)

    # MACD component (0-34): histogram positive + crossover
    hist = np.asarray(pd.to_numeric(df["macd_histogram"], errors="coerce").fillna(0), dtype=float)
    crossover = np.asarray(pd.to_numeric(df["macd_crossover"], errors="coerce").fillna(0), dtype=int)
    # Histogram positive contributes up to 20
    hist_pos = np.maximum(hist, 0)
    hist_max = hist_pos.max() if hist_pos.max() > 0 else 1
    macd_hist_score = hist_pos / hist_max * 20
    np.nan_to_num(macd_hist_score, copy=False)
    # Crossover contributes 14
    macd_cross_score = crossover.astype(float) * 14
    score += np.minimum(macd_hist_score + macd_cross_score, 34)

    # Stochastic component (0-33): K line 20-80 zone, above 50 is bullish
    stochk = np.asarray(pd.to_numeric(df["stoch_k"], errors="coerce").fillna(50), dtype=float)
    stoch_comp = np.where(
        (stochk > 50) & (stochk < 80),
        (stochk - 50) / 30 * 33,
        np.where(stochk >= 80, 33, 0),
    )
    score += np.minimum(stoch_comp, 33)

    df["momentum_score"] = np.minimum(score, 100).round(2)

    # ── decay_factor ──────────────────────────────────────────────────
    # 0.5 + 0.5 * exp(-t/63) where t = row index (trading days)
    t = np.arange(n, dtype=float)
    df["decay_factor"] = (0.5 + 0.5 * np.exp(-t / 63)).round(2)

    return df


# ── Stage Entry Point ────────────────────────────────────────────────────────

def run_stage(start_date: str, end_date: str):
    """Run the momentum indicators stage.

    Reads daily OHLCV data, computes momentum indicators per symbol,
    batch upserts into the momentum table.
    """
    logger.info("Starting momentum stage: %s to %s", start_date, end_date)

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
        computed = _compute_momentum_for_group(group)
        results.append(computed)

    result_df = pd.concat(results, ignore_index=True)
    logger.info("Computed momentum indicators for %d rows", len(result_df))

    # 3. Batch upsert
    cursor = conn.cursor()
    batch = []
    for _, row in result_df.iterrows():
        batch.append((
            EXCHANGE,
            row["trade_date"],
            row["symbol"],
            _get(row, "rsi_14"),
            _get(row, "rsi_9"),
            _get(row, "macd"),
            _get(row, "macd_signal"),
            _get(row, "macd_histogram"),
            int(_get(row, "macd_crossover", 0)),
            _get(row, "stoch_k"),
            _get(row, "stoch_d"),
            _get(row, "stoch_signal"),
            _get(row, "mfi_14"),
            _get(row, "adx_14"),
            _get(row, "cci_20"),
            _get(row, "williams_r_14"),
            _get(row, "momentum_score"),
            _get(row, "decay_factor"),
        ))
        if len(batch) >= 500:
            cursor.executemany(INSERT_SQL, batch)
            batch.clear()

    if batch:
        cursor.executemany(INSERT_SQL, batch)

    conn.commit()
    conn.close()

    logger.info("Completed momentum stage: %d rows upserted", len(result_df))


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
