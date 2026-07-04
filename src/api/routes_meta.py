"""Meta routes — /api/v1/columns, /api/v1/health, /api/v1/dates."""

import logging
import os

from flask import Blueprint, current_app, jsonify

from config import DB_PATH
from db.connection import get_connection, get_schema_version

logger = logging.getLogger("runner")

meta_bp = Blueprint("meta", __name__)

# ── /api/v1/columns ──────────────────────────────────────────────────────────


@meta_bp.route("/columns", methods=["GET"])
def list_columns():
    """Return the column registry, grouped by group."""
    columns_cfg = current_app.config.get("COLUMNS_CONFIG", {})
    cols = columns_cfg.get("columns", [])

    groups = {}
    for c in cols:
        group = c.get("group", "other")
        groups.setdefault(group, []).append(c)

    return jsonify(groups)


# ── /api/v1/health ───────────────────────────────────────────────────────────


@meta_bp.route("/health", methods=["GET"])
def health():
    """Health check — DB size, table row counts, last data dates."""
    conn = get_connection()
    try:
        db_size_mb = round(os.path.getsize(str(DB_PATH)) / (1024 * 1024), 1) if DB_PATH.exists() else 0

        tables = [
            "daily", "stage", "stage_delivery", "technical", "price_level",
            "momentum", "volatility", "averages", "futures_data",
            "scoring_result", "scoring_picks", "scoring_performance",
            "shareholding", "fno_membership", "index_membership",
            "equity_master", "predicted_stock", "pipeline_jobs",
        ]
        table_counts = {}
        for t in tables:
            try:
                count = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                table_counts[t] = count
            except Exception:
                table_counts[t] = -1

        last_data = conn.execute(
            "SELECT MAX(trade_date) FROM daily"
        ).fetchone()[0]

        last_scored = conn.execute(
            "SELECT MAX(trade_date) FROM scoring_result"
        ).fetchone()[0]

        result = {
            "status": "ok",
            "db_size_mb": db_size_mb,
            "schema_version": get_schema_version(),
            "tables": table_counts,
            "last_data_date": last_data,
            "last_scored_date": last_scored,
        }
    finally:
        conn.close()

    return jsonify(result)


# ── /api/v1/dates ──────────────────────────────────────────────────────────────


@meta_bp.route("/dates", methods=["GET"])
def available_dates():
    """Return available date range (min/max trade_date) for each time-series table."""
    time_series_tables = [
        "daily", "stage", "stage_delivery", "technical", "price_level",
        "momentum", "volatility", "averages", "futures_data",
        "scoring_result", "scoring_picks", "predicted_stock",
        "scoring_performance",
    ]

    conn = get_connection()
    try:
        date_col_overrides = {
            "predicted_stock": "pick_date",
            "scoring_performance": "pick_date",
        }

        ranges = {}
        for t in time_series_tables:
            try:
                date_col = date_col_overrides.get(t, "trade_date")
                row = conn.execute(
                    f"SELECT MIN({date_col}), MAX({date_col}), COUNT(*) FROM {t}"
                ).fetchone()
                ranges[t] = {
                    "date_column": date_col,
                    "min_date": row[0],
                    "max_date": row[1],
                    "row_count": row[2],
                }
            except Exception as e:
                ranges[t] = {"error": str(e)}
    finally:
        conn.close()

    return jsonify(ranges)
