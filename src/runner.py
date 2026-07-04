"""CLI orchestrator for the Phase1 data pipeline.

Usage:
    python -m src.runner --stages all --days-back 1
    python -m src.runner --stages fetch,enrich,score --start-date 2025-01-01 --end-date 2026-06-30
    python -m src.runner --stages score --start-date 2026-06-01 --end-date 2026-06-30 --from-disk-only
"""

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, date

from config import ROOT_DIR
from db.connection import init_schema, get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("runner")


def _setup_file_logging():
    """Add a file handler to the runner logger, writing to logs/pipeline_*.log."""
    log_dir = ROOT_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fh = logging.FileHandler(str(log_file), encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logging.getLogger("runner").addHandler(fh)
    logger.info("File logging enabled: %s", log_file)
    return log_file

STAGE_ORDER = [
    "fetch",
    "equity_master",
    "enrich",
    "technical",
    "price_level",
    "momentum",
    "volatility",
    "averages",
    "derivatives",
    "score",
    "hits",
]


def _business_days(start, end):
    """Yield all trading dates between start and end (inclusive).

    Regular trading: Mon–Fri, excluding NSE_HOLIDAYS_2025_2026.
    Also includes SPECIAL_TRADING_DAYS (e.g. Budget Saturday sessions).
    """
    from src.data_pipeline.fetcher import NSE_HOLIDAYS_2025_2026, SPECIAL_TRADING_DAYS

    current = start
    while current <= end:
        is_weekday = current.weekday() < 5
        is_special = current in SPECIAL_TRADING_DAYS
        if (is_weekday or is_special) and current not in NSE_HOLIDAYS_2025_2026:
            yield current
        current += timedelta(days=1)


def _parse_date_arg(raw):
    """Parse a date string in YYYY-MM-DD format, or return None."""
    if raw is None:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        logger.error("Invalid date format: %s (expected YYYY-MM-DD)", raw)
        sys.exit(1)


def _resolve_date_range(args):
    """Determine start_date and end_date from CLI args."""
    end = _parse_date_arg(args.end_date)
    if end is None:
        end = date.today()

    if args.days_back is not None:
        start = end - timedelta(days=args.days_back)
    else:
        start = _parse_date_arg(args.start_date)
        if start is None:
            # Default: last 5 trading days
            start = end - timedelta(days=5)
    return start, end


def _run_fetch(start, end, from_disk_only):
    """Run the fetch stage."""
    logger.info("=== Stage START: fetch ===")
    _t0 = time.perf_counter()
    try:
        from src.data_pipeline.fetcher import load_historical_data

        load_historical_data(start, end, from_disk_only)
        _elapsed = time.perf_counter() - _t0
        logger.info("=== Stage END: fetch (%.2fs) ===", _elapsed)
    except ImportError as e:
        logger.error("fetch module not available: %s", e)
        raise


def _run_stage_timed(name, start, end, module_path, stage_fn=None):
    """Run a stage with START/END timing log."""
    logger.info("=== Stage START: %s ===", name)
    _t0 = time.perf_counter()
    try:
        if stage_fn is None:
            import importlib
            mod = importlib.import_module(module_path)
            stage_fn = mod.run_stage
        stage_fn(start, end)
        _elapsed = time.perf_counter() - _t0
        logger.info("=== Stage END: %s (%.2fs) ===", name, _elapsed)
    except ImportError as e:
        logger.error("%s module not available: %s", name, e)
        raise


def _run_equity_master(start, end):
    _run_stage_timed("equity_master", start, end, "src.data_pipeline.equity_master")


def _run_enrich(start, end):
    _run_stage_timed("enrich", start, end, "src.data_pipeline.enricher")


def _run_technical(start, end):
    _run_stage_timed("technical", start, end, "src.data_pipeline.technical")


def _run_price_level(start, end):
    _run_stage_timed("price_level", start, end, "src.data_pipeline.price_level")


def _run_momentum(start, end):
    _run_stage_timed("momentum", start, end, "src.data_pipeline.momentum")


def _run_volatility(start, end):
    _run_stage_timed("volatility", start, end, "src.data_pipeline.volatility")


def _run_averages(start, end):
    _run_stage_timed("averages", start, end, "src.data_pipeline.averages")


def _run_derivatives(start, end):
    _run_stage_timed("derivatives", start, end, "src.data_pipeline.derivatives")


def _run_score(start, end):
    """Run the scoring stage — threaded per-date with ThreadPoolExecutor(max_workers=4)."""
    logger.info("=== Stage START: score ===")
    _t0 = time.perf_counter()
    try:
        from src.scoring.scorer import run_scoring
    except ImportError as e:
        logger.error("score module not available: %s", e)
        raise

    # Collect all trading dates in the range
    dates = list(_business_days(start, end))
    if not dates:
        logger.warning("No trading dates in range %s to %s", start, end)
        return

    logger.info("Scoring %d trading dates with max_workers=4", len(dates))

    successes = 0
    failures = 0

    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(run_scoring, d.isoformat()): d for d in dates
        }
        for future in as_completed(future_map):
            d = future_map[future]
            try:
                future.result()
                successes += 1
            except Exception as exc:
                logger.error("Scoring failed for %s: %s", d, exc)
                failures += 1

    _elapsed = time.perf_counter() - _t0
    logger.info(
        "=== Stage END: score (%.2fs) — %d succeeded, %d failed out of %d",
        _elapsed, successes, failures, len(dates),
    )


def _run_hits(start, end):
    """Run the hits (validation) stage."""
    logger.info("=== Stage START: hits ===")
    _t0 = time.perf_counter()
    try:
        from src.validation.hits_analyzer import compute_hits

        compute_hits(start, end)
        _elapsed = time.perf_counter() - _t0
        logger.info("=== Stage END: hits (%.2fs) ===", _elapsed)
    except ImportError as e:
        logger.error("hits module not available: %s", e)
        raise


# ── Stage dispatch map ────────────────────────────────────────────────

STAGE_DISPATCH = {
    "fetch": _run_fetch,
    "equity_master": _run_equity_master,
    "enrich": _run_enrich,
    "technical": _run_technical,
    "price_level": _run_price_level,
    "momentum": _run_momentum,
    "volatility": _run_volatility,
    "averages": _run_averages,
    "derivatives": _run_derivatives,
    "score": _run_score,
    "hits": _run_hits,
}


def _parse_args(argv=None):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Phase1 — Stock Scrip Scoring Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m src.runner --stages all --days-back 1\n"
            "  python -m src.runner --stages fetch,enrich,score\n"
            "  python -m src.runner --stages score --start-date 2026-06-01 --end-date 2026-06-30\n"
            "  python -m src.runner --stages hits --start-date 2026-05-01 --end-date 2026-06-04\n"
        ),
    )

    parser.add_argument(
        "--stages",
        type=str,
        default="all",
        help=(
            "Comma-separated list of stages to run, or 'all' for all stages. "
            f"Available: {', '.join(STAGE_ORDER)}"
        ),
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date in YYYY-MM-DD format (inclusive). Default: 5 days before end-date.",
    )

    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date in YYYY-MM-DD format (inclusive). Default: today.",
    )

    parser.add_argument(
        "--days-back",
        type=int,
        default=None,
        help="Number of days back from end-date (alternative to --start-date).",
    )

    parser.add_argument(
        "--from-disk-only",
        action="store_true",
        default=False,
        help="Skip download, process only files already cached on disk.",
    )

    return parser.parse_args(argv)


def _resolve_stages(stages_arg):
    """Convert CLI stages argument to a list of stage names."""
    if stages_arg == "all" or stages_arg is None:
        return list(STAGE_ORDER)

    stage_names = [s.strip().lower() for s in stages_arg.split(",")]
    invalid = [s for s in stage_names if s not in STAGE_ORDER]
    if invalid:
        logger.error(
            "Unknown stage(s): %s. Available: %s",
            invalid,
            ", ".join(STAGE_ORDER),
        )
        sys.exit(1)
    return stage_names


def main(argv=None):
    """Main entry point for the CLI orchestrator."""
    args = _parse_args(argv)
    start, end = _resolve_date_range(args)

    # Enable file logging
    log_file = _setup_file_logging()

    logger.info(
        "=== PIPELINE START: stages=%s, range=%s to %s, from_disk_only=%s ===",
        args.stages,
        start,
        end,
        args.from_disk_only,
    )
    _pipeline_t0 = time.perf_counter()

    # Initialize schema before any stage runs
    logger.info("Initialising database schema…")
    init_schema()

    conn = get_connection()
    conn.close()  # schema init manages its own connection

    stages = _resolve_stages(args.stages)
    logger.info("Resolved stages: %s", stages)

    for stage_name in stages:
        if stage_name == "fetch":
            _run_fetch(start, end, args.from_disk_only)
        elif stage_name == "score":
            _run_score(start, end)
        elif stage_name == "hits":
            _run_hits(start, end)
        else:
            # All other stages take (start, end)
            STAGE_DISPATCH[stage_name](start, end)

    _pipeline_elapsed = time.perf_counter() - _pipeline_t0
    logger.info(
        "=== PIPELINE COMPLETE (%.2fs) — log: %s ===",
        _pipeline_elapsed, log_file,
    )


if __name__ == "__main__":
    main()
