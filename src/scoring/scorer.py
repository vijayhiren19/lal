"""V2a Scoring Formula — 13-component scrip scoring system.

Complete implementation of the V2a formula as specified in AGENTS.md.
Computes 13 component scores from configurable weights in config/scoring.yaml,
saves sector-diversified top-20 picks, and auto-verifies past picks.

Designed for ThreadPoolExecutor (max_workers=4) — each trade_date call
is self-contained: loads config (lru_cached), queries DB, computes scores,
batch-inserts via executemany, and verifies past picks.
"""

import json
import logging
import time
from functools import lru_cache

import numpy as np
import pandas as pd
import yaml

from config import ROOT_DIR
from db.connection import get_connection

logger = logging.getLogger("runner")


# ── Config Loading (cached, called once per process) ────────────────────────

@lru_cache(maxsize=1)
def _load_config():
    """Load scoring configuration from config/scoring.yaml.

    Cached with @lru_cache(maxsize=1) — called once per process,
    eliminating ~1,100 redundant YAML reads per 366-day run.
    """
    config_path = ROOT_DIR / "config" / "scoring.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)
    return config["scoring"]


# ── Shareholding Data ──────────────────────────────────────────────────────

def _load_shareholding(conn, trade_date):
    """Load latest 2 quarters of shareholding data (4 quarters apart).

    Computes smart_money_delta = (fii_current - fii_4q_ago)
                               + (dii_current - dii_4q_ago)

    Returns a DataFrame with columns: symbol, curr_fii, curr_dii,
    prev_fii, prev_dii.
    """
    td_int = int(trade_date.replace("-", ""))

    query = """
        WITH ranked AS (
            SELECT symbol, period, quarter_end_int,
                   fii_pct, dii_pct,
                   ROW_NUMBER() OVER (
                       PARTITION BY symbol ORDER BY quarter_end_int DESC
                   ) AS rn
            FROM shareholding
            WHERE quarter_end_int <= ?
        ),
        curr AS (SELECT * FROM ranked WHERE rn = 1),
        prev AS (SELECT * FROM ranked WHERE rn = 5)
        SELECT
            c.symbol,
            c.fii_pct  AS curr_fii,
            c.dii_pct  AS curr_dii,
            p.fii_pct  AS prev_fii,
            p.dii_pct  AS prev_dii
        FROM curr c
        LEFT JOIN prev p ON c.symbol = p.symbol
    """
    df = pd.read_sql(query, conn, params=(td_int,))
    return df


# ── F&O / Nifty 500 / Sector Membership ────────────────────────────────────

def _load_fno_membership(conn, trade_date):
    """Return set of F&O symbols valid at trade_date (PIT)."""
    query = """
        SELECT DISTINCT symbol FROM fno_membership
        WHERE valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
    """
    rows = conn.execute(query, (trade_date, trade_date)).fetchall()
    return {row[0] for row in rows}


def _load_nifty500_members(conn, trade_date):
    """Return set of Nifty 500 symbols valid at trade_date (PIT)."""
    query = """
        SELECT DISTINCT symbol FROM index_membership
        WHERE index_name = 'Nifty 500'
          AND valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
    """
    rows = conn.execute(query, (trade_date, trade_date)).fetchall()
    return {row[0] for row in rows}


# Known sector index names (from build_index_history.py SECTOR_INDEX_MAP values).
# Used to prioritise sector indices over broad-market indices in sector mapping.
_SECTOR_INDEX_NAMES = frozenset({
    "AUTO", "BANKING", "CONSUMER_DURABLES", "CONSUMER",
    "DIVIDEND", "FINANCIAL_SERVICES", "FMCG", "GROWTH",
    "HEALTHCARE", "IT", "MEDIA", "METAL", "MOMENTUM",
    "OIL_GAS", "PHARMA", "PRIVATE_BANK", "PSU_BANK",
    "REALTY", "SERVICES",
})


def _load_sector_map(conn, trade_date):
    """Build {symbol: sector_name} mapping with fallback chain.

    Priority:
        1. index_membership table (20 NSE sector indices, PIT accurate)
        2. equity_master table (515 named sectors)
        3. 'UNKNOWN' (default)

    For symbols with multiple index memberships, the first sector index
    encountered wins (sector indices prioritised over broad-market indices).
    """
    # ── Primary: index_membership ──────────────────────────────────────
    # Order: sector indices first, then others
    query = """
        SELECT symbol, index_name FROM index_membership
        WHERE valid_from <= ?
          AND (valid_to IS NULL OR valid_to > ?)
        ORDER BY
            CASE WHEN index_name IN ('AUTO','BANKING','CONSUMER_DURABLES',
                          'CONSUMER','DIVIDEND','FINANCIAL_SERVICES','FMCG',
                          'GROWTH','HEALTHCARE','IT','MEDIA','METAL',
                          'MOMENTUM','OIL_GAS','PHARMA','PRIVATE_BANK',
                          'PSU_BANK','REALTY','SERVICES')
                 THEN 0 ELSE 1
            END,
            index_name
    """
    rows = conn.execute(query, (trade_date, trade_date)).fetchall()
    sector_map = {}
    for symbol, index_name in rows:
        if symbol not in sector_map:
            sector_map[symbol] = index_name

    # ── Fallback: equity_master ────────────────────────────────────────
    query2 = """
        SELECT symbol, sector FROM equity_master
        WHERE sector IS NOT NULL AND sector != ''
    """
    rows2 = conn.execute(query2).fetchall()
    for symbol, sector in rows2:
        if symbol not in sector_map:
            sector_map[symbol] = sector

    return sector_map


# ── Data Loading & Merge ───────────────────────────────────────────────────

def _load_data(conn, trade_date):
    """Load and merge all data sources for the given trade_date.

    Returns a DataFrame with columns for all 13 scoring components,
    filtered to INE ISIN stocks meeting minimum delivery value.
    Returns empty DataFrame if no data is available.
    """
    cfg = _load_config()

    # ── 1. Daily (OHLCV + delivery + return + ISIN) ─────────────────────
    daily = pd.read_sql("""
        SELECT exchange, trade_date, symbol,
               close_price, previous_close, traded_value,
               day_return_pct, delivery_volume, delivery_pct,
               delivery_qty_20d_avg, delivery_pct_trend, isin
        FROM daily
        WHERE trade_date = ? AND exchange = 'NSE'
    """, conn, params=(trade_date,))

    if daily.empty:
        logger.warning("No daily data for %s", trade_date)
        return pd.DataFrame()

    # ── 2. Momentum (rsi_14, adx_14, macd, macd_histogram) ────────────
    momentum = pd.read_sql("""
        SELECT exchange, trade_date, symbol,
               rsi_14, adx_14, macd, macd_histogram
        FROM momentum
        WHERE trade_date = ? AND exchange = 'NSE'
    """, conn, params=(trade_date,))

    # ── 3. Volatility (atr_pct) ────────────────────────────────────────
    volatility = pd.read_sql("""
        SELECT exchange, trade_date, symbol, atr_pct
        FROM volatility
        WHERE trade_date = ? AND exchange = 'NSE'
    """, conn, params=(trade_date,))

    # ── 4. Futures (basis_pct, oi_change_pct) ─────────────────────────
    futures = pd.read_sql("""
        SELECT trade_date, symbol, basis_pct, oi_change_pct
        FROM futures_data
        WHERE trade_date = ?
    """, conn, params=(trade_date,))

    # ── 5. Shareholding (4-quarter FII/DII delta) ─────────────────────
    shareholding = _load_shareholding(conn, trade_date)

    # ── Merge all onto daily ───────────────────────────────────────────
    merged = daily.copy()

    for df, keys in [
        (momentum, ["exchange", "trade_date", "symbol"]),
        (volatility, ["exchange", "trade_date", "symbol"]),
    ]:
        if not df.empty:
            merged = merged.merge(df, on=keys, how="left")

    if not futures.empty:
        merged = merged.merge(
            futures, on=["trade_date", "symbol"], how="left"
        )
    else:
        # Ensure futures columns always exist even when no data
        merged["basis_pct"] = 0.0
        merged["oi_change_pct"] = 0.0

    if not shareholding.empty:
        merged = merged.merge(
            shareholding, on="symbol", how="left"
        )
    else:
        # Ensure shareholding columns always exist even when no data
        merged["curr_fii"] = 0.0
        merged["prev_fii"] = 0.0
        merged["curr_dii"] = 0.0
        merged["prev_dii"] = 0.0
        merged["smart_money_delta"] = 0.0

    # ── Sector mapping ─────────────────────────────────────────────────
    sector_map = _load_sector_map(conn, trade_date)
    merged["sector"] = merged["symbol"].map(sector_map).fillna("UNKNOWN")

    # ── ISIN filter: equities only (INE prefix) ────────────────────────
    merged = merged[merged["isin"].str.startswith("INE", na=False)].copy()
    if merged.empty:
        logger.warning("No INE-prefix stocks for %s", trade_date)
        return pd.DataFrame()

    # ── Delivery value filter ──────────────────────────────────────────
    min_value = cfg["min_dev_trade_value_cr"] * 1e7
    delivery_volume = pd.to_numeric(merged["delivery_volume"], errors="coerce").fillna(0)
    close = pd.to_numeric(merged["close_price"], errors="coerce").fillna(0)
    merged["dev_traded_value"] = delivery_volume * close
    merged = merged[merged["dev_traded_value"] >= min_value].copy()
    if merged.empty:
        logger.warning("No stocks pass delivery value filter for %s", trade_date)
        return pd.DataFrame()

    # ── Feature: delivery_qty_ratio ────────────────────────────────────
    qty_20d = pd.to_numeric(merged["delivery_qty_20d_avg"], errors="coerce").fillna(0)
    delivery_volume = pd.to_numeric(merged["delivery_volume"], errors="coerce").fillna(0)
    merged["delivery_qty_ratio"] = np.where(
        qty_20d > 0, delivery_volume / qty_20d, 0.0
    ).astype(float)

    # ── Feature: smart_money_delta ─────────────────────────────────────
    curr_fii = pd.to_numeric(merged["curr_fii"], errors="coerce").fillna(0)
    prev_fii = pd.to_numeric(merged["prev_fii"], errors="coerce").fillna(0)
    curr_dii = pd.to_numeric(merged["curr_dii"], errors="coerce").fillna(0)
    prev_dii = pd.to_numeric(merged["prev_dii"], errors="coerce").fillna(0)
    merged["smart_money_delta"] = (curr_fii - prev_fii) + (curr_dii - prev_dii)
    merged["smart_money_delta"] = merged["smart_money_delta"].fillna(0)

    # ── Flags: F&O / Nifty 500 ────────────────────────────────────────
    fno_set = _load_fno_membership(conn, trade_date)
    nifty500_set = _load_nifty500_members(conn, trade_date)
    merged["is_fno"] = merged["symbol"].isin(fno_set).astype(int)
    merged["is_nifty500"] = merged["symbol"].isin(nifty500_set).astype(int)

    return merged


# ── V2a Score Computation ──────────────────────────────────────────────────

def _compute_v2a_score(df, cfg):
    """Compute all 13 V2a component scores and overall score.

    All thresholds and weights come from *cfg* (config/scoring.yaml).
    Uses np.where with .to_numpy() + .astype(float) to avoid pandas
    index alignment bugs.

    Args:
        df: DataFrame with all required features (from _load_data).
        cfg: scoring config dict.

    Returns:
        DataFrame with all 13 score columns, overall_score, rank, percentile.
    """
    result = df.copy()

    # ── Safe numeric conversion for all feature columns ────────────────
    day_return    = pd.to_numeric(result["day_return_pct"], errors="coerce").fillna(0)
    pct_val       = pd.to_numeric(result["delivery_pct"], errors="coerce").fillna(0)
    pct_trend     = pd.to_numeric(result["delivery_pct_trend"], errors="coerce").fillna(0)
    dq_ratio      = pd.to_numeric(result["delivery_qty_ratio"], errors="coerce").fillna(0)
    traded_value  = pd.to_numeric(result["traded_value"], errors="coerce").fillna(0)
    atr_pct       = pd.to_numeric(result["atr_pct"], errors="coerce").fillna(0)
    rsi_14        = pd.to_numeric(result["rsi_14"], errors="coerce").fillna(50)
    adx_14        = pd.to_numeric(result["adx_14"], errors="coerce").fillna(0)
    macd_hist     = pd.to_numeric(result["macd_histogram"], errors="coerce").fillna(0)
    basis_pct     = pd.to_numeric(result["basis_pct"], errors="coerce").fillna(0)
    oi_chg_pct    = pd.to_numeric(result["oi_change_pct"], errors="coerce").fillna(0)
    sm_delta      = pd.to_numeric(result["smart_money_delta"], errors="coerce").fillna(0)

    # ══════════════════════════════════════════════════════════════════
    #  1. momentum_score — Contrarian entry (↗ on pullback)
    #     abs(day_return_pct) * value_return_weight if day_return < 0
    #     Clip to [0, 50]
    # ══════════════════════════════════════════════════════════════════
    cond_contrarian = (day_return < 0).to_numpy()
    momentum_score = np.where(
        cond_contrarian,
        np.abs(day_return).to_numpy() * cfg["value_return_weight"],
        0.0
    ).astype(float)
    momentum_score = np.clip(momentum_score, 0, 50)

    # ══════════════════════════════════════════════════════════════════
    #  2. value_score — High delivery %
    #     delivery_pct_boost if pct >= delivery_pct_threshold
    # ══════════════════════════════════════════════════════════════════
    cond_value = (pct_val >= cfg["delivery_pct_threshold"]).to_numpy()
    value_score = np.where(cond_value, float(cfg["delivery_pct_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  3. quality_score — Rising delivery trend
    #     pct_trend_boost if pct_trend >= pct_trend_threshold
    # ══════════════════════════════════════════════════════════════════
    cond_quality = (pct_trend >= cfg["pct_trend_threshold"]).to_numpy()
    quality_score = np.where(cond_quality, float(cfg["pct_trend_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  4. technical_strength — Delivery qty surge
    #     delivery_qty_boost if delivery_volume/delivery_qty_20d_avg >= delivery_qty_ratio_threshold
    # ══════════════════════════════════════════════════════════════════
    cond_tech = (dq_ratio >= cfg["delivery_qty_ratio_threshold"]).to_numpy()
    technical_strength = np.where(cond_tech, float(cfg["delivery_qty_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  5. volume_liquidity — Size / liquidity
    #     mcap_tier = quartile of traded_value rank
    #     tier_liq  = traded_value percentile rank within mcap_tier
    #     volume_liquidity = tier_liq * liquidity_percentile_weight
    # ══════════════════════════════════════════════════════════════════
    tv_rank = traded_value.rank(method="average")

    # Quartile assignment (handle edge case of all-identical values)
    try:
        # Use pd.qcut for quartiles; duplicates='drop' handles ties
        mcap_tier = pd.qcut(
            tv_rank.replace(0, np.nan),
            q=4,
            labels=[1, 2, 3, 4],
            duplicates="drop",
        )
    except ValueError:
        # Fallback: single tier if quartiling fails
        mcap_tier = pd.Series([1] * len(tv_rank), index=tv_rank.index)

    # Fill NaN tiers (zero traded_value) with lowest tier
    mcap_tier = mcap_tier.cat.add_categories([0]) if hasattr(mcap_tier, 'cat') and 0 not in mcap_tier.cat.categories else mcap_tier
    mcap_tier = mcap_tier.fillna(0)

    # Percentile of traded_value within each tier
    tier_liq = result.groupby(mcap_tier, observed=False)["traded_value"].rank(pct=True)
    tier_liq = tier_liq.fillna(0)
    volume_liquidity = tier_liq.to_numpy() * cfg["liquidity_percentile_weight"]

    # ══════════════════════════════════════════════════════════════════
    #  6. institutional_score — Institutional accumulation
    #     smart_money_delta = (fii - prev_fii) + (dii - prev_dii)
    #     min(delta * scale_factor, max_boost) if delta > threshold
    # ══════════════════════════════════════════════════════════════════
    cond_inst = (sm_delta > cfg["institutional_delta_threshold"]).to_numpy()
    inst_raw = sm_delta.to_numpy() * cfg["institutional_scale_factor"]
    inst_capped = np.clip(inst_raw, 0, cfg["institutional_max_boost"])
    institutional_score = np.where(cond_inst, inst_capped, 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  7. fno_score — F&O membership
    # ══════════════════════════════════════════════════════════════════
    cond_fno = (result["is_fno"] == 1).to_numpy()
    fno_score = np.where(cond_fno, float(cfg["fno_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  8. nifty500_score — Nifty 500 membership
    # ══════════════════════════════════════════════════════════════════
    cond_n500 = (result["is_nifty500"] == 1).to_numpy()
    nifty500_score = np.where(cond_n500, float(cfg["nifty500_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  9. futures_basis_score — Futures basis
    #     min(basis_pct * 2, futures_basis_boost) if basis_pct > threshold
    # ══════════════════════════════════════════════════════════════════
    cond_basis = (basis_pct > cfg["futures_basis_threshold"]).to_numpy()
    basis_raw = basis_pct.to_numpy() * 2.0
    basis_capped = np.clip(basis_raw, 0, cfg["futures_basis_boost"])
    futures_basis_score = np.where(cond_basis, basis_capped, 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    # 10. oi_trend_score — OI trend
    #     oi_trend_boost if oi_change_pct > threshold AND day_return > 0
    # ══════════════════════════════════════════════════════════════════
    cond_oi = (
        (oi_chg_pct > cfg["oi_trend_threshold"]).to_numpy()
        & (day_return > 0).to_numpy()
    )
    oi_trend_score = np.where(cond_oi, float(cfg["oi_trend_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    # 11. sector_momentum_score — Sector-relative momentum
    #     sector_mean = groupby('sector')['day_return_pct'].mean()
    #     sector_relative = day_return - sector_mean
    #     percentile_rank(sector_relative) * sector_momentum_boost
    # ══════════════════════════════════════════════════════════════════
    sector_mean = result.groupby("sector")["day_return_pct"].transform("mean")
    sector_relative = day_return - sector_mean.fillna(0)
    sector_mom_pctile = sector_relative.rank(pct=True, method="average").fillna(0.5)
    sector_momentum_score = sector_mom_pctile.to_numpy() * cfg["sector_momentum_boost"]

    # ══════════════════════════════════════════════════════════════════
    # 12. volatility_score — Volatility filter
    #     (1 - atr_pctile) * volatility_penalty
    # ══════════════════════════════════════════════════════════════════
    atr_pctile = atr_pct.rank(pct=True, method="average").fillna(0.5)
    volatility_score = (1.0 - atr_pctile.to_numpy()) * cfg["volatility_penalty"]

    # ══════════════════════════════════════════════════════════════════
    # 13. confirmation_score — Multi-indicator confirmation
    #     confirmed = (50 < rsi_14 < 70) AND (adx_14 > 25)
    #                 AND (macd_histogram > 0)
    # ══════════════════════════════════════════════════════════════════
    cond_conf = (
        (rsi_14 > 50).to_numpy()
        & (rsi_14 < 70).to_numpy()
        & (adx_14 > 25).to_numpy()
        & (macd_hist > 0).to_numpy()
    )
    confirmation_score = np.where(cond_conf, float(cfg["confirmation_boost"]), 0.0).astype(float)

    # ══════════════════════════════════════════════════════════════════
    #  Overall score (capped 0–100)
    # ══════════════════════════════════════════════════════════════════
    overall = (
        momentum_score + value_score + quality_score + technical_strength
        + volume_liquidity + institutional_score + fno_score + nifty500_score
        + futures_basis_score + oi_trend_score + sector_momentum_score
        + volatility_score + confirmation_score
    )
    overall = np.clip(overall, 0, 100)

    # ── Round and store all component scores ──────────────────────────
    result["momentum_score"]         = np.round(momentum_score, 2)
    result["value_score"]            = np.round(value_score, 2)
    result["quality_score"]          = np.round(quality_score, 2)
    result["technical_strength"]     = np.round(technical_strength, 2)
    result["volume_liquidity"]       = np.round(volume_liquidity, 2)
    result["institutional_score"]    = np.round(institutional_score, 2)
    result["fno_score"]              = np.round(fno_score, 2)
    result["nifty500_score"]         = np.round(nifty500_score, 2)
    result["futures_basis_score"]    = np.round(futures_basis_score, 2)
    result["oi_trend_score"]         = np.round(oi_trend_score, 2)
    result["sector_momentum_score"]  = np.round(sector_momentum_score, 2)
    result["volatility_score"]       = np.round(volatility_score, 2)
    result["confirmation_score"]     = np.round(confirmation_score, 2)
    result["overall_score"]          = np.round(overall, 2)
    result["nifty500_member"]        = result["is_nifty500"]

    # ── Rank & percentile ─────────────────────────────────────────────
    result["rank"] = result["overall_score"].rank(
        ascending=False, method="min"
    ).fillna(0).astype(int)
    result["percentile"] = np.round(
        result["overall_score"].rank(pct=True).fillna(0) * 100, 2
    )

    return result


# ── Saving Results ─────────────────────────────────────────────────────────

def _save_scoring_result(conn, df, trade_date):
    """Save full scoring results to ``scoring_result`` table.

    Uses batch INSERT OR REPLACE with executemany (500 rows per batch).
    """
    sql = """
        INSERT OR REPLACE INTO scoring_result (
            exchange, trade_date, symbol,
            overall_score, momentum_score, value_score, quality_score,
            technical_strength, volume_liquidity, institutional_score,
            fno_score, futures_basis_score, oi_trend_score,
            sector_momentum_score, volatility_score, confirmation_score,
            nifty500_member, heuristic_penalties, rank, percentile
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """
    rows = []
    cols = [
        "symbol", "overall_score", "momentum_score", "value_score",
        "quality_score", "technical_strength", "volume_liquidity",
        "institutional_score", "fno_score", "futures_basis_score",
        "oi_trend_score", "sector_momentum_score", "volatility_score",
        "confirmation_score", "nifty500_member", "rank", "percentile",
    ]

    for _, row in df.iterrows():
        rows.append((
            "NSE",
            trade_date,
            str(row["symbol"]),
            float(row["overall_score"]),
            float(row["momentum_score"]),
            float(row["value_score"]),
            float(row["quality_score"]),
            float(row["technical_strength"]),
            float(row["volume_liquidity"]),
            float(row["institutional_score"]),
            float(row["fno_score"]),
            float(row["futures_basis_score"]),
            float(row["oi_trend_score"]),
            float(row["sector_momentum_score"]),
            float(row["volatility_score"]),
            float(row["confirmation_score"]),
            int(row["nifty500_member"]),
            "{}",
            int(row["rank"]),
            float(row["percentile"]),
        ))

    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        conn.executemany(sql, batch)
    conn.commit()
    logger.info("Saved %d scoring results for %s", len(rows), trade_date)


def _save_picks(conn, df, trade_date, cfg):
    """Select top-N picks with sector diversification and save to scoring_picks.

    Selection logic:
        1. Sort by overall_score descending.
        2. Iterate, accepting a stock if its sector has fewer than
           max_per_sector picks already selected.
        3. Stop when top_n picks have been chosen.

    Sector source: index_membership → equity_master → 'UNKNOWN'.
    """
    sorted_df = df.sort_values("overall_score", ascending=False)
    picks = []
    sector_count = {}

    for _, row in sorted_df.iterrows():
        sector = str(row.get("sector", "UNKNOWN"))
        current_count = sector_count.get(sector, 0)
        if current_count < cfg["max_per_sector"]:
            picks.append((
                trade_date,
                row["symbol"],
                round(float(row["overall_score"]), 2),
                len(picks) + 1,
                sector,
            ))
            sector_count[sector] = current_count + 1
            if len(picks) >= cfg["top_n"]:
                break

    sql = """
        INSERT OR REPLACE INTO scoring_picks
            (trade_date, symbol, score, rank, sector)
        VALUES (?, ?, ?, ?, ?)
    """
    for i in range(0, len(picks), 500):
        batch = picks[i:i + 500]
        conn.executemany(sql, batch)
    conn.commit()
    logger.info("Saved %d picks for %s", len(picks), trade_date)

    for p in picks:
        logger.info("  Pick #%d: %s (%.2f) [%s]", p[3], p[1], p[2], p[4])

    return picks


# ── Past Pick Verification ─────────────────────────────────────────────────

def _verify_past_picks(conn, trade_date, cfg):
    """Verify past picks whose lookahead period has elapsed.

    Finds all picks where ``pick_date + lookahead_days <= trade_date``
    and that have not yet been verified. Computes forward return using
    ``daily.close_price`` and stores in ``scoring_performance``.

    Uses INSERT OR IGNORE to avoid duplicates.
    """
    lookahead = cfg["lookahead_days"]

    # Find unverified picks eligible for verification
    query = """
        SELECT sp.trade_date, sp.symbol
        FROM scoring_picks sp
        LEFT JOIN scoring_performance perf
            ON sp.trade_date = perf.pick_date
            AND sp.symbol = perf.symbol
        WHERE perf.pick_date IS NULL
          AND julianday(?) - julianday(sp.trade_date) >= ?
    """
    rows = conn.execute(query, (trade_date, lookahead)).fetchall()

    if not rows:
        return

    entry_sql = """
        SELECT close_price FROM daily
        WHERE trade_date = ? AND symbol = ? AND exchange = 'NSE'
    """
    exit_sql = """
        SELECT close_price FROM daily
        WHERE trade_date = ? AND symbol = ? AND exchange = 'NSE'
    """
    insert_sql = """
        INSERT OR IGNORE INTO scoring_performance
            (pick_date, check_date, symbol, entry_price, exit_price, return_pct)
        VALUES (?, ?, ?, ?, ?, ?)
    """

    verified_count = 0
    for pick_date, symbol in rows:
        entry_row = conn.execute(entry_sql, (pick_date, symbol)).fetchone()
        if entry_row is None:
            continue
        entry_price = entry_row[0]

        exit_row = conn.execute(exit_sql, (trade_date, symbol)).fetchone()
        if exit_row is None:
            continue
        exit_price = exit_row[0]

        if entry_price > 0:
            return_pct = round(
                (exit_price - entry_price) / entry_price * 100, 2
            )
        else:
            return_pct = 0.0

        conn.execute(insert_sql, (
            pick_date, trade_date, symbol,
            round(entry_price, 2), round(exit_price, 2), return_pct,
        ))
        verified_count += 1

    if verified_count > 0:
        conn.commit()
        logger.info(
            "Verified %d past picks against %s",
            verified_count, trade_date,
        )


# ── Main Entry Point ───────────────────────────────────────────────────────

def run_scoring(trade_date):
    """Execute the V2a scoring formula for a given trade_date.

    Designed to be called per-date from ThreadPoolExecutor (max_workers=4).
    Each call is fully self-contained:

    1. Loads config (lru_cached)  ─────────────────────── _load_config()
    2. Creates its own connection  ────────────────────── get_connection()
    3. Loads & merges all sources  ────────────────────── _load_data()
    4. Computes 13 component scores  ─────────────────── _compute_v2a_score()
    5. Batch-inserts full results  ────────────────────── _save_scoring_result()
    6. Selects sector-diversified top-20  ────────────── _save_picks()
    7. Verifies past picks  ───────────────────────────── _verify_past_picks()

    Args:
        trade_date: ISO date string (YYYY-MM-DD).
    """
    _t0 = time.perf_counter()
    cfg = _load_config()
    conn = get_connection()

    try:
        df = _load_data(conn, trade_date)
        if df.empty:
            _elapsed = time.perf_counter() - _t0
            logger.warning("No data to score for %s (%.2fs)", trade_date, _elapsed)
            return

        scored = _compute_v2a_score(df, cfg)

        _save_scoring_result(conn, scored, trade_date)

        _save_picks(conn, scored, trade_date, cfg)

        _verify_past_picks(conn, trade_date, cfg)

        _elapsed = time.perf_counter() - _t0
        logger.info(
            "Completed scoring for %s: %d results, %d picks (%.2fs)",
            trade_date, len(scored), min(len(scored), cfg["top_n"]), _elapsed,
        )

    except Exception:
        logger.exception("Scoring failed for %s", trade_date)
        raise
    finally:
        conn.close()
