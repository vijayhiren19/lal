"""Routes for pipeline management — /api/v1/pipeline/* and /api/v1/hits/*.

Endpoints:
  POST   /pipeline/run               — run pipeline stages (async)
  GET    /pipeline/status             — check pipeline job status
  GET    /pipeline/stages             — list available pipeline stages
  GET    /pipeline/jobs               — list recent pipeline jobs
  POST   /pipeline/build/fno-membership     — rebuild F&O membership table
  POST   /pipeline/build/index-history      — rebuild index membership table
  POST   /pipeline/build/shareholding        — rebuild shareholding table
  POST   /pipeline/build/equity-master       — rebuild equity master CSV
  POST   /hits/compute                — compute hit analysis (async)
  GET    /hits/analyze                — hit rate summary + per-target breakdown
  GET    /hits/detail                 — individual picks with target_hit >= N
"""

import io
import logging
import threading
import traceback
from datetime import datetime

import pandas as pd
from flask import Blueprint, jsonify, request, Response

from db.connection import get_connection

logger = logging.getLogger("runner")

pipeline_bp = Blueprint("pipeline", __name__)

# ── Available stages ─────────────────────────────────────────────────────────

AVAILABLE_STAGES = [
    "fetch", "equity_master", "enrich", "technical", "price_level",
    "momentum", "volatility", "averages", "derivatives", "score", "hits",
]

# ── Helpers ─────────────────────────────────────────────────────────────────


def _generate_job_id(prefix="pipeline"):
    return f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def _log_capture_handler():
    """Create a (handler, string_io) pair attached to the 'runner' logger."""
    log_capture = io.StringIO()
    handler = logging.StreamHandler(log_capture)
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    runner_logger = logging.getLogger("runner")
    runner_logger.addHandler(handler)
    return handler, log_capture, runner_logger


def _check_running_job():
    """Return (running_job_id_or_None, response_or_None)."""
    conn = get_connection()
    try:
        existing = conn.execute(
            "SELECT job_id FROM pipeline_jobs WHERE status IN ('pending', 'running') LIMIT 1"
        ).fetchone()
        if existing is not None:
            return existing[0], jsonify({
                "error": "A pipeline job is already running",
                "running_job_id": existing[0],
            }), 409
        return None, None, None
    finally:
        conn.close()


def _insert_job(job_id, stages, start_date=None, end_date=None):
    """Insert a new pipeline_jobs row."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO pipeline_jobs (job_id, stages, start_date, end_date, status) VALUES (?, ?, ?, ?, 'pending')",
            (job_id, stages, start_date, end_date),
        )
        conn.commit()
    finally:
        conn.close()


def _start_background(target, args):
    """Start a daemon thread."""
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()


def _run_generic_job(job_id, fn, stages_label):
    """Background thread target for any build/process job.

    Args:
        job_id: pipeline_jobs.job_id
        fn: callable to execute (no args)
        stages_label: value for pipeline_jobs.stages column
    """
    import time
    _job_t0 = time.perf_counter()
    conn = get_connection()
    handler, log_capture, runner_logger = _log_capture_handler()

    try:
        conn.execute(
            "UPDATE pipeline_jobs SET status='running', started_at=? WHERE job_id=?",
            (datetime.now().isoformat(), job_id),
        )
        conn.commit()

        logger.info("Job %s (%s) started", job_id, stages_label)
        try:
            fn()
            _job_elapsed = time.perf_counter() - _job_t0
            logger.info("Job %s (%s) completed in %.2fs", job_id, stages_label, _job_elapsed)
            captured = log_capture.getvalue()
            conn.execute(
                "UPDATE pipeline_jobs SET status='completed', completed_at=?, error_log=? WHERE job_id=?",
                (datetime.now().isoformat(), captured or None, job_id),
            )
            conn.commit()
        except Exception:
            tb = traceback.format_exc()
            captured = log_capture.getvalue()
            full_log = f"{captured}\n--- EXCEPTION ---\n{tb}" if captured else tb
            conn.execute(
                "UPDATE pipeline_jobs SET status='failed', completed_at=?, error_log=? WHERE job_id=?",
                (datetime.now().isoformat(), full_log, job_id),
            )
            conn.commit()
    except Exception:
        logger.exception("Job %s thread error", job_id)
    finally:
        runner_logger.removeHandler(handler)
        handler.close()
        conn.close()


# ── POST /api/v1/pipeline/run ────────────────────────────────────────────────


@pipeline_bp.route("/pipeline/run", methods=["POST"])
def pipeline_run():
    """Trigger pipeline execution (async).

    Accepts JSON body with optional fields: stages, start_date, end_date, days_back.
    Returns 202 Accepted with job_id immediately.
    """
    data = request.get_json(silent=True) or {}

    stages = data.get("stages", "all")
    start_date = data.get("start_date")
    end_date = data.get("end_date")
    days_back = data.get("days_back")

    existing_id, error_resp, status = _check_running_job()
    if existing_id:
        return error_resp, status

    job_id = _generate_job_id("pipeline")
    _insert_job(job_id, stages, start_date, end_date)

    def target():
        _run_pipeline_inner(job_id, stages, start_date, end_date, days_back)

    _start_background(target, ())

    return jsonify({
        "job_id": job_id,
        "status": "pending",
        "message": "Pipeline queued",
    }), 202


def _run_pipeline_inner(job_id, stages, start_date, end_date, days_back):
    """Background thread — executes pipeline stages."""
    import time
    _job_t0 = time.perf_counter()
    conn = get_connection()
    handler, log_capture, runner_logger = _log_capture_handler()

    try:
        conn.execute(
            "UPDATE pipeline_jobs SET status='running', started_at=? WHERE job_id=?",
            (datetime.now().isoformat(), job_id),
        )
        conn.commit()

        logger.info(
            "Pipeline job %s: stages=%s, range=%s to %s, days_back=%s",
            job_id, stages, start_date, end_date, days_back,
        )

        argv = ["--stages", stages]
        if start_date:
            argv += ["--start-date", start_date]
        if end_date:
            argv += ["--end-date", end_date]
        if days_back is not None:
            argv += ["--days-back", str(days_back)]

        try:
            from src.runner import main as runner_main

            runner_main(argv)
            _job_elapsed = time.perf_counter() - _job_t0
            logger.info(
                "Pipeline job %s completed in %.2fs", job_id, _job_elapsed,
            )
            captured = log_capture.getvalue()
            conn.execute(
                "UPDATE pipeline_jobs SET status='completed', completed_at=?, error_log=? WHERE job_id=?",
                (datetime.now().isoformat(), captured or None, job_id),
            )
            conn.commit()
        except Exception:
            tb = traceback.format_exc()
            captured = log_capture.getvalue()
            full_log = f"{captured}\n--- EXCEPTION ---\n{tb}" if captured else tb
            conn.execute(
                "UPDATE pipeline_jobs SET status='failed', completed_at=?, error_log=? WHERE job_id=?",
                (datetime.now().isoformat(), full_log, job_id),
            )
            conn.commit()
    except Exception:
        logger.exception("Pipeline job %s thread error", job_id)
    finally:
        runner_logger.removeHandler(handler)
        handler.close()
        conn.close()


# ── GET /api/v1/pipeline/status ──────────────────────────────────────────────


@pipeline_bp.route("/pipeline/status", methods=["GET"])
def pipeline_status():
    """Check pipeline job status.

    Query params:
        job_id (optional) — specific job ID, or omit for latest.
    """
    job_id = request.args.get("job_id")

    conn = get_connection()
    try:
        if job_id:
            row = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM pipeline_jobs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()

        if row is None:
            return jsonify({"error": "No pipeline jobs found"}), 404

        col_names = [desc[0] for desc in conn.execute("SELECT * FROM pipeline_jobs LIMIT 0").description]
        result = dict(zip(col_names, row))
    finally:
        conn.close()

    return jsonify(result)


# ── GET /api/v1/pipeline/stages ──────────────────────────────────────────────


@pipeline_bp.route("/pipeline/stages", methods=["GET"])
def pipeline_stages():
    """List available pipeline stage names."""
    return jsonify({"stages": AVAILABLE_STAGES, "count": len(AVAILABLE_STAGES)})


# ── GET /api/v1/pipeline/jobs ────────────────────────────────────────────────


@pipeline_bp.route("/pipeline/jobs", methods=["GET"])
def pipeline_jobs_list():
    """List recent pipeline jobs.

    Query params:
        limit (optional, default 10, max 100) — number of jobs to return.
        status (optional) — filter by status (pending/running/completed/failed).
    """
    limit = min(int(request.args.get("limit", 10)), 100)
    status_filter = request.args.get("status")

    conn = get_connection()
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs WHERE status = ? ORDER BY rowid DESC LIMIT ?",
                (status_filter, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pipeline_jobs ORDER BY rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()

        col_names = [desc[0] for desc in conn.execute("SELECT * FROM pipeline_jobs LIMIT 0").description]
        results = [dict(zip(col_names, row)) for row in rows]
    finally:
        conn.close()

    return jsonify({"jobs": results, "count": len(results)})


# ═══════════════════════════════════════════════════════════════════════════════
# BUILD ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


def _queue_build_job(stages_label, fn):
    """Common pattern: check concurrency, insert job, start background thread."""
    existing_id, error_resp, status = _check_running_job()
    if existing_id:
        return error_resp, status

    job_id = _generate_job_id("build")
    _insert_job(job_id, stages_label)

    _start_background(_run_generic_job, (job_id, fn, stages_label))

    return jsonify({
        "job_id": job_id,
        "status": "pending",
        "message": f"Build {stages_label} queued",
    }), 202


# ── POST /api/v1/pipeline/build/fno-membership ────────────────────────────────


@pipeline_bp.route("/pipeline/build/fno-membership", methods=["POST"])
def build_fno_membership():
    """Rebuild fno_membership table from GitHub source (async)."""
    def _run():
        import build_fno_membership as mod
        csv_text = mod.download_csv(mod.CSV_URL)
        mod.parse_and_insert(csv_text)

    return _queue_build_job("build/fno-membership", _run)


# ── POST /api/v1/pipeline/build/index-history ─────────────────────────────────


@pipeline_bp.route("/pipeline/build/index-history", methods=["POST"])
def build_index_history():
    """Rebuild index_membership table from GitHub source (async)."""
    def _run():
        import build_index_history as mod
        csv_text = mod.download_csv(mod.CSV_URL)
        mod.parse_and_insert(csv_text)

    return _queue_build_job("build/index-history", _run)


# ── POST /api/v1/pipeline/build/shareholding ──────────────────────────────────


@pipeline_bp.route("/pipeline/build/shareholding", methods=["POST"])
def build_shareholding():
    """Rebuild shareholding table from GitHub source (async)."""
    def _run():
        import build_shareholding as mod
        csv_text = mod.download_csv(mod.CSV_URL)
        mod.parse_and_insert(csv_text)

    return _queue_build_job("build/shareholding", _run)


# ── POST /api/v1/pipeline/build/equity-master ─────────────────────────────────


@pipeline_bp.route("/pipeline/build/equity-master", methods=["POST"])
def build_equity_master():
    """Rebuild equity master CSV from NSE source (async)."""
    def _run():
        import build_equity_master as mod
        csv_text = mod.download_equity_master(mod.EQ_MAST_URL)
        mod.parse_and_write(csv_text, mod.OUTPUT_PATH)

    return _queue_build_job("build/equity-master", _run)


# ═══════════════════════════════════════════════════════════════════════════════
# HIT ANALYSIS ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════════


# ── POST /api/v1/hits/compute ──────────────────────────────────────────────────


@pipeline_bp.route("/hits/compute", methods=["POST"])
def hits_compute():
    """Compute hit analysis for scoring picks in a date range (async).

    JSON body: { start_date: "YYYY-MM-DD", end_date: "YYYY-MM-DD" }
    Both fields required.
    """
    data = request.get_json(silent=True) or {}
    start_date = data.get("start_date")
    end_date = data.get("end_date")

    if not start_date or not end_date:
        return jsonify({"error": "start_date and end_date are required"}), 400

    existing_id, error_resp, status = _check_running_job()
    if existing_id:
        return error_resp, status

    job_id = _generate_job_id("hits")
    stages_label = f"hits/compute/{start_date}/{end_date}"
    _insert_job(job_id, stages_label, start_date, end_date)

    def target():
        _run_hits_compute(job_id, start_date, end_date)

    _start_background(target, ())

    return jsonify({
        "job_id": job_id,
        "status": "pending",
        "message": f"Hit computation queued for {start_date} to {end_date}",
    }), 202


def _run_hits_compute(job_id, start_date, end_date):
    """Background thread — computes hits via hits_analyzer."""
    import time
    _job_t0 = time.perf_counter()
    conn = get_connection()
    handler, log_capture, runner_logger = _log_capture_handler()

    try:
        conn.execute(
            "UPDATE pipeline_jobs SET status='running', started_at=? WHERE job_id=?",
            (datetime.now().isoformat(), job_id),
        )
        conn.commit()

        logger.info("Hits compute job %s: %s to %s", job_id, start_date, end_date)
        try:
            from src.validation.hits_analyzer import compute_hits

            compute_hits(start_date, end_date)
            _job_elapsed = time.perf_counter() - _job_t0
            logger.info("Hits compute job %s completed in %.2fs", job_id, _job_elapsed)
            captured = log_capture.getvalue()
            conn.execute(
                "UPDATE pipeline_jobs SET status='completed', completed_at=?, error_log=? WHERE job_id=?",
                (datetime.now().isoformat(), captured or None, job_id),
            )
            conn.commit()
        except Exception:
            tb = traceback.format_exc()
            captured = log_capture.getvalue()
            full_log = f"{captured}\n--- EXCEPTION ---\n{tb}" if captured else tb
            conn.execute(
                "UPDATE pipeline_jobs SET status='failed', completed_at=?, error_log=? WHERE job_id=?",
                (datetime.now().isoformat(), full_log, job_id),
            )
            conn.commit()
    except Exception:
        logger.exception("Hits compute job %s thread error", job_id)
    finally:
        runner_logger.removeHandler(handler)
        handler.close()
        conn.close()


# ── GET /api/v1/hits/analyze ──────────────────────────────────────────────────


@pipeline_bp.route("/hits/analyze", methods=["GET"])
def hits_analyze():
    """Return hit rate summary and per-target breakdown as JSON."""
    conn = get_connection()

    df = pd.read_sql(
        """
        SELECT target_hit FROM predicted_stock
        """,
        conn,
    )

    if df.empty:
        conn.close()
        return jsonify({"error": "No data in predicted_stock. Run hits/compute first."}), 404

    total = len(df)
    hit_labels = {0: "None", 1: "Tg1", 2: "Tg2", 3: "Tg3"}
    summary = df["target_hit"].value_counts().sort_index()

    levels = []
    any_hits = 0
    for level in sorted(hit_labels.keys()):
        count = int(summary.get(level, 0))
        pct = round(count / total * 100, 2) if total > 0 else 0
        levels.append({
            "level": level,
            "label": hit_labels[level],
            "count": count,
            "pct": pct,
        })
        if level > 0:
            any_hits += count

    any_pct = round(any_hits / total * 100, 2) if total > 0 else 0

    # Per-target breakdown by window
    from src.validation.hits_analyzer import _load_config
    config = _load_config()
    windows = config["windows"]
    targets = config["targets"]

    breakdown = []
    for w in windows:
        for i, tgt in enumerate(targets):
            level = i + 1
            hits = int((df["target_hit"] >= level).sum())
            pct = round(hits / total * 100, 2) if total > 0 else 0
            breakdown.append({
                "window_days": w,
                "target": f"Tg{level}",
                "target_pct": tgt["pct"],
                "picks": total,
                "hits": hits,
                "hit_pct": pct,
            })

    conn.close()

    return jsonify({
        "total_picks": total,
        "summary": levels,
        "any_hits": {"count": any_hits, "pct": any_pct},
        "breakdown": breakdown,
    })


# ── GET /api/v1/hits/detail ────────────────────────────────────────────────────


@pipeline_bp.route("/hits/detail", methods=["GET"])
def hits_detail():
    """Return individual picks with target_hit >= threshold.

    Query params:
        min_hit (optional, default 1) — minimum target_hit level.
        limit (optional, default 50, max 500) — max rows.
        format (optional, default json) — json or csv.
    """
    min_hit = int(request.args.get("min_hit", 1))
    limit = min(int(request.args.get("limit", 50)), 500)
    fmt = request.args.get("format", "json")

    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT pick_date, symbol, entry_date, entry_price,
                   tg1_price, tg1_date, tg1_high, tg1_close,
                   tg2_price, tg2_date, tg2_high, tg2_close,
                   tg3_price, tg3_date, tg3_high, tg3_close,
                   window_low, window_low_date, window_end_date,
                   target_hit, data_complete
            FROM predicted_stock
            WHERE target_hit >= ?
            ORDER BY pick_date DESC, target_hit DESC
            LIMIT ?
            """,
            (min_hit, limit),
        ).fetchall()

        col_names = [
            "pick_date", "symbol", "entry_date", "entry_price",
            "tg1_price", "tg1_date", "tg1_high", "tg1_close",
            "tg2_price", "tg2_date", "tg2_high", "tg2_close",
            "tg3_price", "tg3_date", "tg3_high", "tg3_close",
            "window_low", "window_low_date", "window_end_date",
            "target_hit", "data_complete",
        ]
        results = [dict(zip(col_names, row)) for row in rows]

        if fmt == "csv":
            import csv as csv_module
            output = io.StringIO()
            writer = csv_module.DictWriter(output, fieldnames=col_names)
            writer.writeheader()
            writer.writerows(results)
            return Response(
                output.getvalue(),
                mimetype="text/csv",
                headers={"Content-Disposition": "attachment; filename=hits_detail.csv"},
            )

        return jsonify({"hits": results, "count": len(results)})
    finally:
        conn.close()
