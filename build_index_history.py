"""Build index_membership table from NSE index membership history.

Downloads index_membership_history.csv from the nse-historical-membership repository
and populates the index_membership table with idempotent INSERT OR REPLACE.
Maps 20 NSE sector index IDs to normalized sector names.

Source: github.com/aditya-jha/nse-historical-membership
         → index_history/data/index_membership_history.csv
License: CC BY 4.0

Usage:
    python build_index_history.py
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
    "index_history/data/index_membership_history.csv"
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/plain, text/csv, */*",
}

# ── Sector index ID to normalized sector name mapping ─────────────────
# NSE maintains 20+ sector indices. This map normalises them to human-readable names.
# Index IDs and names may be updated over time; unknown index IDs are passed through.

SECTOR_INDEX_MAP = {
    "NIFTY AUTO": "AUTO",
    "NIFTY BANK": "BANKING",
    "NIFTY CONSUMER DURABLES": "CONSUMER_DURABLES",
    "NIFTY FINANCIAL SERVICES": "FINANCIAL_SERVICES",
    "NIFTY FMCG": "FMCG",
    "NIFTY HEALTHCARE": "HEALTHCARE",
    "NIFTY IT": "IT",
    "NIFTY MEDIA": "MEDIA",
    "NIFTY METAL": "METAL",
    "NIFTY OIL & GAS": "OIL_GAS",
    "NIFTY PHARMA": "PHARMA",
    "NIFTY PRIVATE BANK": "PRIVATE_BANK",
    "NIFTY PSU BANK": "PSU_BANK",
    "NIFTY REALTY": "REALTY",
    "NIFTY CONSUMER": "CONSUMER",
    "NIFTY DIVIDEND OPPORTUNITIES": "DIVIDEND",
    "NIFTY GROWTH SECTORS": "GROWTH",
    "NIFTY SERVICES": "SERVICES",
    "NIFTY MID SMALL HEALTHCARE": "HEALTHCARE",
    "NIFTY200 MOMENTUM 30": "MOMENTUM",
    # Fallback: use index_name directly if not in map
}

DB_BATCH_SIZE = 500


def download_csv(url):
    """Download CSV from URL and return as string."""
    logger.info("Downloading index membership data from %s", url)
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    logger.info("Downloaded %d bytes", len(resp.content))
    return resp.text


def normalize_sector(index_name, index_id):
    """Map index_id/index_name to a normalized sector name.

    Uses SECTOR_INDEX_MAP lookup first, then falls back to the index_name
    with spaces/special chars cleaned up.
    """
    key = index_name.strip()
    if key in SECTOR_INDEX_MAP:
        return SECTOR_INDEX_MAP[key]

    # Fallback: create a clean name from index_name
    cleaned = key.replace("NIFTY ", "").replace("&", "AND").replace(" ", "_").upper()
    return cleaned


def parse_and_insert(csv_text):
    """Parse CSV text and INSERT OR REPLACE into index_membership."""
    conn = get_connection()
    cur = conn.cursor()

    reader = csv.DictReader(io.StringIO(csv_text))
    fieldnames = reader.fieldnames
    logger.info("CSV columns: %s", fieldnames)

    # Normalise column names
    col_map = {}
    for fn in fieldnames or []:
        fn_lower = fn.strip().lower()
        if fn_lower in ("symbol", "ticker", "scrip"):
            col_map["symbol"] = fn
        elif fn_lower in ("index_name", "index", "sector", "sector_name"):
            col_map["index_name"] = fn
        elif "index_id" in fn_lower or "sector_id" in fn_lower:
            col_map["index_id"] = fn
        elif "valid_from" in fn_lower or "from" in fn_lower:
            col_map["valid_from"] = fn
        elif "valid_to" in fn_lower or "to" in fn_lower:
            col_map["valid_to"] = fn
        elif "weight" in fn_lower:
            col_map["weightage"] = fn

    required = ["symbol", "index_name", "valid_from"]
    missing = [r for r in required if r not in col_map]
    if missing:
        logger.error("Missing required columns: %s. Available: %s", missing, fieldnames)
        conn.close()
        return 0

    insert_sql = """
        INSERT OR REPLACE INTO index_membership
            (symbol, index_name, index_id, valid_from, valid_to, weightage)
        VALUES (?, ?, ?, ?, ?, ?)
    """

    batch = []
    rows_inserted = 0
    skipped = 0

    for row in reader:
        symbol = row.get(col_map["symbol"], "").strip()
        index_name = row.get(col_map["index_name"], "").strip()
        valid_from = row.get(col_map["valid_from"], "").strip()

        if not symbol or not index_name or not valid_from:
            skipped += 1
            continue

        # Parse index_id (may be integer or string)
        raw_id = row.get(col_map.get("index_id", ""), "").strip()
        try:
            index_id = int(float(raw_id)) if raw_id else 1
        except (ValueError, TypeError):
            index_id = 1

        # Normalise sector name
        sector = normalize_sector(index_name, index_id)

        # Parse valid_to
        valid_to = row.get(col_map.get("valid_to", ""), "").strip()
        if not valid_to or valid_to.lower() in ("na", "n/a", "", "null", "none", "present"):
            valid_to = None

        # Parse weightage
        raw_wt = row.get(col_map.get("weightage", ""), "").strip()
        try:
            weightage = round(float(raw_wt), 4) if raw_wt else None
        except (ValueError, TypeError):
            weightage = None

        batch.append((symbol, sector, index_id, valid_from, valid_to, weightage))

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
        "Inserted %d rows into index_membership (%d skipped)",
        rows_inserted,
        skipped,
    )
    return rows_inserted


def main():
    """Main entry point."""
    logger.info("Building index_membership table…")
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
