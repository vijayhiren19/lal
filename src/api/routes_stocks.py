"""Routes for stock data querying — /api/v1/stocks/* and /api/v1/picks."""

import csv
import io
import logging

from flask import Blueprint, Response, current_app, jsonify, request

from db.connection import get_connection

logger = logging.getLogger("runner")

stocks_bp = Blueprint("stocks", __name__)

VIEW_NAME = "stock_universe"
MAX_LIMIT = 1000
DEFAULT_LIMIT = 100


def _get_columns_config():
    """Return the loaded columns config from the Flask app config."""
    return current_app.config.get("COLUMNS_CONFIG", {})


def _validate_columns(requested_cols, columns_cfg):
    """Validate requested column names against the registry.

    Returns (valid_cols, invalid_cols).
    """
    valid_names = {c["name"] for c in columns_cfg.get("columns", [])}
    if not requested_cols:
        return list(valid_names), []
    invalid = [c for c in requested_cols if c not in valid_names]
    valid = [c for c in requested_cols if c in valid_names]
    return valid, invalid


def _resolve_columns(args):
    """Resolve requested columns from request args.

    Priority:
        1. ``columns`` param (comma-separated, defines order)
        2. ``fields`` repeated params (appended after ``columns``)
        3. All columns (if neither provided)
    """
    columns_cfg = _get_columns_config()
    all_names = [c["name"] for c in columns_cfg.get("columns", [])]

    cols_param = args.get("columns", "").strip()
    fields_params = args.getlist("fields")

    if cols_param:
        selected = [c.strip() for c in cols_param.split(",") if c.strip()]
    else:
        selected = []

    if fields_params:
        for f in fields_params:
            f = f.strip()
            if f and f not in selected:
                selected.append(f)

    if not selected:
        return list(all_names), []

    return _validate_columns(selected, columns_cfg)


def _build_select_clause(columns, prefix=None):
    """Build a comma-separated SELECT clause from column names.

    If *prefix* is given, each column is prefixed (e.g. ``v.symbol``).
    """
    if prefix:
        return ", ".join(f'{prefix}."{c}"' for c in columns)
    return ", ".join(f'"{c}"' for c in columns)


def _query_view(select_clause, where_clauses, params, order_by, order_dir, limit, offset):
    """Execute a query against the stock_universe view."""
    sql = f"SELECT {select_clause} FROM {VIEW_NAME}"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    if order_by:
        sql += f' ORDER BY "{order_by}" {order_dir}'
    sql += f" LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_connection()
    try:
        cursor = conn.execute(sql, params)
        col_names = [desc[0] for desc in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(col_names, row)) for row in rows]
    finally:
        conn.close()


def _build_csv_response(rows, filename):
    """Build a CSV response from a list of dicts."""
    if not rows:
        return Response("", mimetype="text/csv", status=200)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── /api/v1/stocks ───────────────────────────────────────────────────────────


@stocks_bp.route("/stocks", methods=["GET"])
def list_stocks():
    """Flexible stock data query from stock_universe.

    Query params: date, symbol, sector, columns, fields, order_by,
                  order_dir, limit, offset, min_score, format.
    """
    cols, invalid = _resolve_columns(request.args)
    if invalid:
        return jsonify({"error": f"Unknown columns: {invalid}", "valid_columns": [c["name"] for c in _get_columns_config().get("columns", [])]}), 400

    select_clause = _build_select_clause(cols)

    where_clauses = []
    params = []

    trade_date = request.args.get("date")
    if trade_date:
        where_clauses.append("trade_date = ?")
        params.append(trade_date)
    else:
        return jsonify({"error": "date parameter is required (YYYY-MM-DD)"}), 400

    symbol = request.args.get("symbol")
    if symbol:
        where_clauses.append("symbol = ?")
        params.append(symbol.upper())

    sector = request.args.get("sector")
    if sector:
        where_clauses.append("sector = ?")
        params.append(sector.upper())

    min_score = request.args.get("min_score")
    if min_score:
        try:
            val = float(min_score)
            where_clauses.append("overall_score >= ?")
            params.append(val)
        except ValueError:
            return jsonify({"error": "min_score must be a numeric value"}), 400

    order_by = request.args.get("order_by", "rank")
    order_dir = request.args.get("order_dir", "asc").lower()
    if order_dir not in ("asc", "desc"):
        order_dir = "asc"

    valid_names = {c["name"] for c in _get_columns_config().get("columns", [])}
    if order_by not in valid_names:
        return jsonify({"error": f"Unknown order_by column: {order_by}", "valid_columns": sorted(valid_names)}), 400

    limit = min(int(request.args.get("limit", DEFAULT_LIMIT)), MAX_LIMIT)
    offset = int(request.args.get("offset", 0))

    rows = _query_view(select_clause, where_clauses, params, order_by, order_dir, limit, offset)

    output_format = request.args.get("format", "json")

    if output_format == "csv":
        date_part = trade_date.replace("-", "") if trade_date else "data"
        return _build_csv_response(rows, f"stocks_{date_part}.csv")

    return jsonify(rows)


# ── /api/v1/stocks/history ───────────────────────────────────────────────────


@stocks_bp.route("/stocks/history", methods=["GET"])
def stock_history():
    """Full history for a symbol."""
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter is required"}), 400

    cols, invalid = _resolve_columns(request.args)
    if invalid:
        return jsonify({"error": f"Unknown columns: {invalid}"}), 400

    select_clause = _build_select_clause(cols)
    where_clauses = ["symbol = ?"]
    params = [symbol]

    start_date = request.args.get("start_date")
    if start_date:
        where_clauses.append("trade_date >= ?")
        params.append(start_date)

    end_date = request.args.get("end_date")
    if end_date:
        where_clauses.append("trade_date <= ?")
        params.append(end_date)

    sql = f"SELECT {select_clause} FROM {VIEW_NAME} WHERE {' AND '.join(where_clauses)} ORDER BY trade_date ASC"

    conn = get_connection()
    try:
        cursor = conn.execute(sql, params)
        col_names = [desc[0] for desc in cursor.description]
        rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()

    output_format = request.args.get("format", "json")
    if output_format == "csv":
        return _build_csv_response(rows, f"{symbol}_history.csv")

    return jsonify(rows)


# ── /api/v1/stocks/detail ────────────────────────────────────────────────────


@stocks_bp.route("/stocks/detail", methods=["GET"])
def stock_detail():
    """Single stock snapshot — all columns for one symbol+date."""
    symbol = request.args.get("symbol", "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol parameter is required"}), 400

    trade_date = request.args.get("date")

    where_clauses = ["symbol = ?"]
    params = [symbol]

    if trade_date:
        where_clauses.append("trade_date = ?")
        params.append(trade_date)

    sql = f"SELECT * FROM {VIEW_NAME} WHERE {' AND '.join(where_clauses)} LIMIT 1"

    conn = get_connection()
    try:
        cursor = conn.execute(sql, params)
        col_names = [desc[0] for desc in cursor.description]
        row = cursor.fetchone()
        if row is None:
            return jsonify({"error": "No data found"}), 404
        result = dict(zip(col_names, row))
    finally:
        conn.close()

    return jsonify(result)


# ── /api/v1/stocks/search ────────────────────────────────────────────────────


@stocks_bp.route("/stocks/search", methods=["GET"])
def stock_search():
    """Fuzzy search on symbol, sector, and industry."""
    q = request.args.get("q", "").strip().upper()
    if not q:
        return jsonify({"error": "q parameter is required"}), 400

    pattern = f"%{q}%"

    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT DISTINCT symbol FROM equity_master
            WHERE symbol LIKE ? OR sector LIKE ? OR industry LIKE ?
            ORDER BY symbol
            LIMIT 50
        """, (pattern, pattern, pattern)).fetchall()
        symbols = [r[0] for r in rows]

        if not symbols:
            return jsonify([])

        trade_date = request.args.get("date")
        date_filter = ""
        params = []
        if trade_date:
            date_filter = "AND trade_date = ?"
            params.append(trade_date)

        placeholders = ",".join("?" for _ in symbols)
        params = symbols + params

        cursor = conn.execute(f"""
            SELECT * FROM {VIEW_NAME}
            WHERE symbol IN ({placeholders}) {date_filter}
            ORDER BY symbol, trade_date DESC
        """, params)
        col_names = [desc[0] for desc in cursor.description]
        results = []
        seen = set()
        for row in cursor.fetchall():
            sym = row[col_names.index("symbol")]
            if sym not in seen:
                seen.add(sym)
                results.append(dict(zip(col_names, row)))
    finally:
        conn.close()

    return jsonify(results)


# ── /api/v1/picks ────────────────────────────────────────────────────────────


@stocks_bp.route("/picks", methods=["GET"])
def top_picks():
    """Top scoring picks for a date."""
    cols, invalid = _resolve_columns(request.args)
    if invalid:
        return jsonify({"error": f"Unknown columns: {invalid}"}), 400

    # Prefix with v. to avoid ambiguity with scoring_picks columns
    select_clause = _build_select_clause(cols, prefix="v")

    trade_date = request.args.get("date")
    params = []
    date_filter = ""
    if trade_date:
        date_filter = "AND sp.trade_date = ?"
        params.append(trade_date)

    limit = min(int(request.args.get("limit", 20)), MAX_LIMIT)

    sql = f"""
        SELECT {select_clause}
        FROM {VIEW_NAME} v
        INNER JOIN scoring_picks sp
            ON v.trade_date = sp.trade_date AND v.symbol = sp.symbol
        WHERE 1=1 {date_filter}
        ORDER BY sp.rank ASC
        LIMIT ?
    """
    params.append(limit)

    conn = get_connection()
    try:
        cursor = conn.execute(sql, params)
        col_names = [desc[0] for desc in cursor.description]
        rows = [dict(zip(col_names, row)) for row in cursor.fetchall()]
    finally:
        conn.close()

    output_format = request.args.get("format", "json")
    if output_format == "csv":
        date_part = trade_date.replace("-", "") if trade_date else "picks"
        return _build_csv_response(rows, f"picks_{date_part}.csv")

    return jsonify(rows)
