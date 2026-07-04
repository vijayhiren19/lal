"""CLI entry point for hit analysis.

Usage:
    python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-06-04
    python -m src.validation.run_hits --analyze
    python -m src.validation.run_hits --analyze --detail 1
"""

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("runner")

from db.connection import init_schema


def _parse_date(raw):
    """Parse a YYYY-MM-DD date string."""
    if raw is None:
        return None
    try:
        from datetime import datetime

        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        logger.error("Invalid date format: %s (expected YYYY-MM-DD)", raw)
        sys.exit(1)


def parse_args(argv=None):
    """Parse CLI arguments for hit analysis."""
    parser = argparse.ArgumentParser(
        description="Hit analysis for scoring picks — compute and analyze forward-return hits.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m src.validation.run_hits --compute --start-date 2026-05-01 --end-date 2026-06-04\n"
            "  python -m src.validation.run_hits --analyze\n"
            "  python -m src.validation.run_hits --analyze --detail 1\n"
        ),
    )

    parser.add_argument(
        "--compute",
        action="store_true",
        default=False,
        help="Compute hit data for scoring picks in the specified date range.",
    )

    parser.add_argument(
        "--analyze",
        action="store_true",
        default=False,
        help="Analyze existing hit data and print summary tables.",
    )

    parser.add_argument(
        "--detail",
        type=int,
        default=0,
        help="When used with --analyze, shows individual picks with target_hit >= N (default: 0 = summary only).",
    )

    parser.add_argument(
        "--start-date",
        type=str,
        default=None,
        help="Start date in YYYY-MM-DD format (inclusive). Required for --compute.",
    )

    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help="End date in YYYY-MM-DD format (inclusive). Required for --compute.",
    )

    return parser.parse_args(argv)


def main(argv=None):
    """Main entry point."""
    args = parse_args(argv)

    if not args.compute and not args.analyze:
        logger.error("Specify at least one of --compute or --analyze")
        sys.exit(1)

    # Ensure schema is initialised (predicted_stock table)
    init_schema()

    if args.compute:
        from src.validation.hits_analyzer import compute_hits

        start = _parse_date(args.start_date)
        end = _parse_date(args.end_date)

        if start is None or end is None:
            logger.error("--compute requires both --start-date and --end-date")
            sys.exit(1)

        logger.info("Computing hits from %s to %s", start, end)
        compute_hits(start.isoformat(), end.isoformat())

    if args.analyze:
        from src.validation.hits_analyzer import analyze_hits

        analyze_hits(detail_threshold=args.detail)


if __name__ == "__main__":
    main()
