"""Build shareholding table from NSE shareholding history.

Downloads shareholding flat CSV from the nse-historical-membership repository
and populates the shareholding table with idempotent INSERT OR REPLACE.

The `period` column is detected by exact match `fn_lower == "period"` (in addition
to substring match with quarter/year), since the _flat.csv uses a bare `period`
column name (not `quarter_end` or `filing_period`).

Source: github.com/aditya-jha/nse-historical-membership
         → shareholding_history/data/parsed/_flat.csv
License: CC BY 4.0

Usage:
    python build_shareholding.py
"""

import csv
import logging
import io

import requests

from db.connection import get_connection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("runner")

# ── Constants ─────────────────────────────────────────────────────────

CSV_URL = (
    "https://raw.githubusercontent.com/aditya-jha/"
    "nse-historical-membership/main/"
    "shareholding_history/data/parsed/_flat.csv"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/plain, text/csv, */*",
}

DB_BATCH_SIZE = 500


def download_csv(url):
    """Download CSV from URL and return as string."""
    logger.info("Downloading shareholding data from %s", url)
    resp = requests.get(url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    logger.info("Downloaded %d bytes", len(resp.content))
    return resp.text


def parse_and_insert(csv_text):
    """Parse CSV text and INSERT OR REPLACE into shareholding."""
    conn = get_connection()
    cur = conn.cursor()

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames
    logger.info("CSV columns: %s", fieldnames)

    # Normalise column name mapping
    col_map = {}
    for fn in fieldnames or []:
        fn_lower = fn.strip().lower()
        if fn_lower in ("symbol", "ticker", "scrip", "security"):
            col_map["symbol"] = fn
        elif fn_lower == "period" or ("period" in fn_lower and ("quarter" in fn_lower or "year" in fn_lower)):
            col_map["period"] = fn
        elif "quarter_end" in fn_lower:
            col_map["quarter_end"] = fn
        elif "quarter_end_int" in fn_lower:
            col_map["quarter_end_int"] = fn
        elif "promoter" in fn_lower or "promoter_pct" in fn_lower:
            col_map["promoter_pct"] = fn
        elif "fii" in fn_lower or "fii_pct" in fn_lower:
            col_map["fii_pct"] = fn
        elif "dii" in fn_lower or "dii_pct" in fn_lower:
            col_map["dii_pct"] = fn
        elif "public" in fn_lower:
            col_map["public_pct"] = fn

    required = ["symbol", "period"]
    missing = [r for r in required if r not in col_map]
    if missing:
        logger.error("Missing required columns: %s. Available: %s", missing, fieldnames)
        conn.close()
        return 0

    insert_sql = """
        INSERT OR REPLACE INTO shareholding
            (symbol, period, quarter_end, quarter_end_int,
             promoter_pct, fii_pct, dii_pct, public_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """

    batch = []
    rows_inserted = 0
    skipped = 0

    for row in reader:
        symbol = row.get(col_map["symbol"], "").strip()
        period = row.get(col_map["period"], "").strip()

        if not symbol or not period:
            skipped += 1
            continue

        # Parse optional fields
        quarter_end = row.get(col_map.get("quarter_end", ""), "").strip()
        quarter_end_int = row.get(col_map.get("quarter_end_int", ""), "").strip()

        def safe_float(val):
            try:
                return round(float(val), 2) if val else None
            except (ValueError, TypeError):
                return None

        promoter_pct = safe_float(row.get(col_map.get("promoter_pct", ""), ""))
        fii_pct = safe_float(row.get(col_map.get("fii_pct", ""), ""))
        dii_pct = safe_float(row.get(col_map.get("dii_pct", ""), ""))
        public_pct = safe_float(row.get(col_map.get("public_pct", ""), ""))

        # Try to parse quarter_end_int
        qe_int = None
        if quarter_end_int:
            try:
                qe_int = int(float(quarter_end_int))
            except (ValueError, TypeError):
                pass

        # If period doesn't have a quarter_end, derive from period if possible
        if not quarter_end and period:
            # Period format is typically 'YYYY-MM' like '2025-03'
            quarter_end = period

        batch.append((
            symbol, period, quarter_end if quarter_end else None, qe_int,
            promoter_pct, fii_pct, dii_pct, public_pct,
        ))

        if len(batch) >= DB_BATCH_SIZE:
            cur.executemany(insert_sql, batch)
            conn.commit()
            rows_inserted += len(batch)
            batch = []

    if batch:
        cur.executemany(insert_sql, batch)
        conn.commit()
        rows_inserted += len(batch)

    conn.close()
    logger.info(
        "Inserted %d rows into shareholding (%d skipped)",
        rows_inserted,
        skipped,
    )
    return rows_inserted


def main():
    """Main entry point."""
    logger.info("Building shareholding table…")
    try:
        csv_text = download_csv(CSV_URL)
        parse_and_insert(csv_text)
    except requests.RequestException as e:
        logger.error("Download failed: %s", e)
        raise
    except Exception as e:
        logger.error("Processing failed: %s", e)
        raise


if __name__ == "__main__":
    main()
