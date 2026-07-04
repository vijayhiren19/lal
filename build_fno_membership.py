"""Build fno_membership table from NSE F&O membership history.

Downloads fno_membership_history.csv from the nse-historical-membership repository
and populates the fno_membership table with idempotent INSERT OR REPLACE.

Source: github.com/aditya-jha/nse-historical-membership
         → fno_history/data/fno_membership_history.csv
License: CC BY 4.0

Usage:
    python build_fno_membership.py
"""

import csv
import logging
import io
from pathlib import Path

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
    "fno_history/data/fno_membership_history.csv"
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
    logger.info("Downloading F&O membership data from %s", url)
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    logger.info("Downloaded %d bytes", len(resp.content))
    return resp.text


def parse_and_insert(csv_text):
    """Parse CSV text and INSERT OR REPLACE into fno_membership."""
    conn = get_connection()
    cur = conn.cursor()

    reader = csv.DictReader(io.StringIO(csv_text))

    # Expected columns: symbol, valid_from, valid_to
    # The CSV may have different column names; try to detect them.
    fieldnames = reader.fieldnames
    logger.info("CSV columns: %s", fieldnames)

    # Normalise column name mapping
    col_map = {}
    for fn in fieldnames or []:
        fn_lower = fn.strip().lower()
        if fn_lower in ("symbol", "ticker", "scrip", "security"):
            col_map["symbol"] = fn
        elif "valid_from" in fn_lower or "from" in fn_lower:
            col_map["valid_from"] = fn
        elif "valid_to" in fn_lower or "to" in fn_lower:
            col_map["valid_to"] = fn

    if "symbol" not in col_map:
        logger.error("Could not identify symbol column in CSV. Columns: %s", fieldnames)
        conn.close()
        return 0

    insert_sql = """
        INSERT OR REPLACE INTO fno_membership (symbol, valid_from, valid_to)
        VALUES (?, ?, ?)
    """

    batch = []
    rows_inserted = 0
    skipped = 0

    for row in reader:
        symbol = row.get(col_map.get("symbol", ""), "").strip()
        valid_from = row.get(col_map.get("valid_from", ""), "").strip()
        valid_to = row.get(col_map.get("valid_to", ""), "").strip()

        if not symbol or not valid_from:
            skipped += 1
            continue

        # Handle empty valid_to as None (still active)
        if not valid_to or valid_to.lower() in ("na", "n/a", "", "null", "none", "present"):
            valid_to = None

        batch.append((symbol, valid_from, valid_to))

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
    logger.info("Inserted %d rows into fno_membership (%d skipped)", rows_inserted, skipped)
    return rows_inserted


def main():
    """Main entry point."""
    logger.info("Building fno_membership table…")
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
