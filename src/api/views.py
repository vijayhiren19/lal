"""Database view creation for the Flask REST API."""

import logging

logger = logging.getLogger("runner")


STOCK_UNIVERSE_SQL = """
CREATE VIEW IF NOT EXISTS stock_universe AS
SELECT
    d.exchange,
    d.trade_date,
    d.symbol,
    d.open_price,
    d.high_price,
    d.low_price,
    d.close_price,
    d.previous_close,
    d.traded_volume,
    d.traded_value,
    d.day_return_pct,
    d.wvap_price,
    d.delivery_volume,
    d.delivery_value,
    d.delivery_pct,
    d.delivery_qty_20d_avg,
    d.delivery_pct_trend,
    d.isin,
    t.sma_20,
    t.sma_50,
    t.sma_100,
    t.sma_200,
    t.ema_9,
    t.ema_20,
    t.ema_50,
    t.price_vs_sma20_pct,
    t.golden_cross,
    t.ema20_gt_ema50,
    t.price_gt_sma200,
    t.trend_stage,
    t.trend_score,
    t.trend_strength,
    pl.week52_high,
    pl.week52_low,
    pl.pct_from_52wk_high,
    pl.near_52wk_high_flag,
    pl.week4_high,
    pl.breakout_flag,
    pl.pivot_p,
    pl.pivot_r1,
    pl.pivot_s1,
    m.rsi_14,
    m.macd,
    m.macd_signal,
    m.macd_histogram,
    m.macd_crossover,
    m.stoch_k,
    m.stoch_d,
    m.adx_14,
    m.mfi_14,
    m.cci_20,
    m.rsi_9,
    m.williams_r_14,
    m.stoch_signal,
    m.momentum_score                              AS comp_momentum_score,
    m.decay_factor,
    v.atr_14,
    v.atr_pct,
    v.bb_upper,
    v.bb_middle,
    v.bb_lower,
    v.bb_width,
    v.bb_squeeze,
    v.keltner_upper,
    v.keltner_lower,
    v.historical_vol_20d,
    a.close_avg_21d,
    a.close_avg_63d,
    a.close_avg_126d,
    a.close_avg_252d,
    a.volume_avg_21d,
    a.volume_avg_63d,
    a.volume_avg_126d,
    a.rsi_avg_21d,
    a.delivery_pct_avg_21d,
    a.volatility_avg_21d,
    a.vol_5d_avg,
    a.vol_10d_avg,
    a.vol_20d_avg,
    a.vol_ratio,
    a.vol_breakout_up,
    a.volume_score,
    f.basis_pct,
    f.oi_change_pct,
    sr.overall_score,
    sr.rank,
    sr.percentile,
    sr.momentum_score                             AS comp_momentum_score_sr,
    sr.value_score,
    sr.quality_score,
    sr.technical_strength,
    sr.volume_liquidity,
    sr.institutional_score,
    sr.fno_score,
    sr.nifty500_member,
    em.sector,
    em.industry
FROM daily d
LEFT JOIN technical t
    ON d.exchange = t.exchange AND d.trade_date = t.trade_date AND d.symbol = t.symbol
LEFT JOIN price_level pl
    ON d.exchange = pl.exchange AND d.trade_date = pl.trade_date AND d.symbol = pl.symbol
LEFT JOIN momentum m
    ON d.exchange = m.exchange AND d.trade_date = m.trade_date AND d.symbol = m.symbol
LEFT JOIN volatility v
    ON d.exchange = v.exchange AND d.trade_date = v.trade_date AND d.symbol = v.symbol
LEFT JOIN averages a
    ON d.exchange = a.exchange AND d.trade_date = a.trade_date AND d.symbol = a.symbol
LEFT JOIN futures_data f
    ON d.trade_date = f.trade_date AND d.symbol = f.symbol
LEFT JOIN scoring_result sr
    ON d.exchange = sr.exchange AND d.trade_date = sr.trade_date AND d.symbol = sr.symbol
LEFT JOIN equity_master em
    ON d.symbol = em.symbol
"""


def create_stock_universe_view(conn):
    """Create the ``stock_universe`` view if it doesn't exist."""
    conn.execute(STOCK_UNIVERSE_SQL)
    conn.commit()
    logger.info("stock_universe view created / already exists")
