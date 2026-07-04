"""Database connection and schema initialization.

All tables, indexes, and schema migrations are defined here.
Schema changes are additive only — never drop or rename columns.
"""

import sqlite3
import logging
from functools import lru_cache

from config import DB_PATH

logger = logging.getLogger("runner")


def get_connection():
    """Get a SQLite connection with WAL journal mode enabled."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=OFF")
    return conn


def init_schema():
    """Create all tables and indexes if they don't exist.

    This function is idempotent — safe to call on every run.
    Uses IF NOT EXISTS for tables and CREATE INDEX IF NOT EXISTS for indexes.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # ── Core data tables ──────────────────────────────────────────────

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stage (
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
            day_return_pct  REAL,
            upper_circuit_hit INTEGER DEFAULT 0,
            lower_circuit_hit INTEGER DEFAULT 0,
            isin            TEXT,
            PRIMARY KEY (exchange, trade_date, symbol)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS stage_delivery (
            exchange        TEXT    NOT NULL,
            trade_date      TEXT    NOT NULL,
            symbol          TEXT    NOT NULL,
            qty             INTEGER NOT NULL,
            pct             REAL    NOT NULL,
            PRIMARY KEY (exchange, trade_date, symbol)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS daily (
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
            day_return_pct  REAL,
            upper_circuit_hit INTEGER DEFAULT 0,
            lower_circuit_hit INTEGER DEFAULT 0,
            delivery_volume INTEGER,
            delivery_value  REAL,
            delivery_pct    REAL,
            wvap_price      REAL,
            isin            TEXT,
            lowest_closing_5days INTEGER DEFAULT 0,
            highest_closing_5days INTEGER DEFAULT 0,
            delivery_qty_5d_avg      REAL,
            delivery_qty_20d_avg     REAL,
            delivery_pct_5d_avg      REAL,
            delivery_pct_20d_avg     REAL,
            delivery_pct_trend       REAL,
            vol_spike       INTEGER,
            PRIMARY KEY (exchange, trade_date, symbol)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS technical (
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS price_level (
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS momentum (
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS volatility (
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS averages (
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
            vol_5d_avg              REAL,
            vol_10d_avg             REAL,
            vol_20d_avg             REAL,
            vol_ratio               REAL,
            vol_breakout_up         INTEGER,
            vol_trend_5d            REAL,
            volume_score            REAL,
            PRIMARY KEY (exchange, trade_date, symbol)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS futures_data (
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
        )
    """)

    # ── Scoring tables ────────────────────────────────────────────────

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scoring_result (
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scoring_picks (
            trade_date      TEXT    NOT NULL,
            symbol          TEXT    NOT NULL,
            score           REAL,
            rank            INTEGER,
            sector          TEXT,
            PRIMARY KEY (trade_date, symbol)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scoring_performance (
            pick_date       TEXT    NOT NULL,
            check_date      TEXT    NOT NULL,
            symbol          TEXT    NOT NULL,
            entry_price     REAL,
            exit_price      REAL,
            return_pct      REAL,
            PRIMARY KEY (pick_date, check_date, symbol)
        )
    """)

    # ── Master / reference tables ─────────────────────────────────────

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shareholding (
            symbol          TEXT    NOT NULL,
            period          TEXT    NOT NULL,
            quarter_end     TEXT,
            quarter_end_int INTEGER,
            promoter_pct    REAL,
            fii_pct         REAL,
            dii_pct         REAL,
            public_pct      REAL,
            PRIMARY KEY (symbol, period)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS fno_membership (
            symbol      TEXT    NOT NULL,
            valid_from  TEXT    NOT NULL,
            valid_to    TEXT,
            PRIMARY KEY (symbol, valid_from)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS index_membership (
            symbol      TEXT    NOT NULL,
            index_name  TEXT    NOT NULL,
            index_id    INTEGER NOT NULL,
            valid_from  TEXT    NOT NULL,
            valid_to    TEXT,
            weightage   REAL,
            PRIMARY KEY (symbol, index_name, index_id, valid_from)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS equity_master (
            symbol          TEXT    PRIMARY KEY,
            isin            TEXT,
            sector          TEXT,
            industry        TEXT,
            market_cap      TEXT,
            last_updated    TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS predicted_stock (
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
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_jobs (
            job_id      TEXT PRIMARY KEY,
            stages      TEXT NOT NULL,
            start_date  TEXT,
            end_date    TEXT,
            status      TEXT NOT NULL DEFAULT 'pending',
            started_at  TEXT,
            completed_at TEXT,
            error_log   TEXT
        )
    """)

    # ── Schema migrations (existing DB upgrade path) ──────────────────
    # All ALTER TABLE ADD COLUMN are idempotent via try/except.

    # 1. Stage_delivery already created above (IF NOT EXISTS).

    # 2. Migrate data from old delivery table if it exists.
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='delivery'")
    has_delivery = cursor.fetchone() is not None
    if has_delivery:
        # Copy delivery data to stage_delivery (if not already done)
        cursor.execute("SELECT COUNT(*) FROM stage_delivery")
        if cursor.fetchone()[0] == 0:
            cursor.execute("""
                INSERT OR IGNORE INTO stage_delivery (exchange, trade_date, symbol, qty, pct)
                SELECT exchange, trade_date, symbol, qty, pct FROM delivery
            """)
            logger.info("Migrated delivery → stage_delivery: %d rows", cursor.rowcount)

    # 3. Add new columns to daily table
    daily_new_cols = [
        "day_return_pct REAL",
        "upper_circuit_hit INTEGER DEFAULT 0",
        "lower_circuit_hit INTEGER DEFAULT 0",
        "delivery_volume INTEGER",
        "delivery_value REAL",
        "delivery_pct REAL",
        "wvap_price REAL",
        "isin TEXT",
        "highest_closing_5days INTEGER DEFAULT 0",
        "delivery_qty_5d_avg REAL",
        "delivery_qty_20d_avg REAL",
        "delivery_pct_5d_avg REAL",
        "delivery_pct_20d_avg REAL",
        "delivery_pct_trend REAL",
        "vol_spike INTEGER",
    ]
    for col_def in daily_new_cols:
        try:
            cursor.execute(f"ALTER TABLE daily ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # 4. Add new columns to averages table
    averages_new_cols = [
        "vol_5d_avg REAL",
        "vol_10d_avg REAL",
        "vol_20d_avg REAL",
        "vol_ratio REAL",
        "vol_breakout_up INTEGER",
        "vol_trend_5d REAL",
        "volume_score REAL",
    ]
    for col_def in averages_new_cols:
        try:
            cursor.execute(f"ALTER TABLE averages ADD COLUMN {col_def}")
        except sqlite3.OperationalError:
            pass  # column already exists

    # 5. Backfill existing daily rows from stage + delivery tables
    if has_delivery:
        # Backfill day_return_pct, upper_circuit_hit, lower_circuit_hit from stage
        try:
            cursor.execute("""
                UPDATE daily SET
                    day_return_pct = s.day_return_pct,
                    upper_circuit_hit = s.upper_circuit_hit,
                    lower_circuit_hit = s.lower_circuit_hit
                FROM stage s
                WHERE daily.exchange = s.exchange
                  AND daily.trade_date = s.trade_date
                  AND daily.symbol = s.symbol
            """)
        except sqlite3.OperationalError:
            pass

        # Backfill delivery columns from delivery table
        try:
            cursor.execute("""
                UPDATE daily SET
                    delivery_volume = d.qty,
                    delivery_pct = d.pct,
                    delivery_qty_5d_avg = d.qty_5d_avg,
                    delivery_qty_20d_avg = d.qty_20d_avg,
                    delivery_pct_5d_avg = d.pct_5d_avg,
                    delivery_pct_20d_avg = d.pct_20d_avg,
                    delivery_pct_trend = d.pct_trend,
                    vol_spike = d.vol_spike
                FROM delivery d
                WHERE daily.exchange = d.exchange
                  AND daily.trade_date = d.trade_date
                  AND daily.symbol = d.symbol
            """)
        except sqlite3.OperationalError:
            pass

    # Compute wvap_price for existing rows (safe to run regardless)
    try:
        cursor.execute("""
            UPDATE daily SET wvap_price = ROUND((high_price + low_price + close_price) / 3.0, 2)
            WHERE wvap_price IS NULL
        """)
    except sqlite3.OperationalError:
        pass

    # Compute delivery_value for existing rows
    try:
        cursor.execute("""
            UPDATE daily SET delivery_value = ROUND(delivery_volume * wvap_price, 2)
            WHERE delivery_value IS NULL AND delivery_volume IS NOT NULL AND wvap_price IS NOT NULL
        """)
    except sqlite3.OperationalError:
        pass

    # 6. Drop old delivery table after data migration
    if has_delivery:
        cursor.execute("DROP TABLE IF EXISTS delivery")
        logger.info("Dropped old delivery table")

    # ── Indexes for performance ───────────────────────────────────────

    indexes = [
        "CREATE INDEX IF NOT EXISTS idx_stage_sym_date ON stage (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_stage_date ON stage (trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_daily_sym_date ON daily (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_daily_date ON daily (trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_stage_delivery_sym_date ON stage_delivery (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_technical_sym_date ON technical (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_pricelevel_sym_date ON price_level (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_momentum_sym_date ON momentum (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_volatility_sym_date ON volatility (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_averages_sym_date ON averages (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_averages_date ON averages (trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_scoring_sym_date ON scoring_result (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_scoring_date ON scoring_result (trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_futures_sym_date ON futures_data (symbol, trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_futures_date ON futures_data (trade_date)",
        "CREATE INDEX IF NOT EXISTS idx_predicted_pick ON predicted_stock (pick_date)",
    ]

    for idx in indexes:
        cursor.execute(idx)

    # Create the stock_universe view
    try:
        from src.api.views import create_stock_universe_view
        create_stock_universe_view(conn)
    except Exception:
        logger.exception("Failed to create stock_universe view")

    conn.commit()
    conn.close()
    logger.info("Database schema initialized: %d tables", len(indexes) + 14)


def get_schema_version():
    """Return a simple schema version string based on table count."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'")
    count = cursor.fetchone()[0]
    conn.close()
    return f"v1.{count}"
