"""Equity Master stage.

Downloads NSE equity master CSV (fallback chain: EQ_MAST.csv → EQUITY_L.csv)
or reads from local cache. Upserts to equity_master table with symbol, isin, sector,
industry, market_cap.

ISIN column detection uses substring match ("ISIN" in col_name) to handle both
"ISIN" and "ISIN NUMBER" column headers found across NSE CSV formats.
"""

import logging
import time
import csv
from pathlib import Path

import pandas as pd
import requests

from db.connection import get_connection
from config import ROOT_DIR, DATA_DIR

logger = logging.getLogger("runner")

# ── Constants ────────────────────────────────────────────────────────────────

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

EQ_MAST_URL = (
    "https://nsearchives.nseindia.com/content/equities/EQ_MAST.csv"
)
EQ_MAST_PATH = DATA_DIR / "eq_mast.csv"

# ── SQL ──────────────────────────────────────────────────────────────────────

UPSERT_SQL = """
    INSERT OR REPLACE INTO equity_master
    (symbol, isin, sector, industry, market_cap, last_updated)
    VALUES (?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _download_equity_master() -> Path:
    """Download EQ_MAST.csv from NSE to local cache. Returns path."""
    logger.info("Downloading equity master from NSE...")
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    # Seed session with NSE homepage to get cookies
    session.get("https://www.nseindia.com", timeout=10)

    resp = session.get(EQ_MAST_URL, timeout=60)
    resp.raise_for_status()

    with open(EQ_MAST_PATH, "wb") as f:
        f.write(resp.content)

    logger.info("Equity master saved to %s", EQ_MAST_PATH)
    session.close()
    return EQ_MAST_PATH


def _load_equity_master_csv(path: Path) -> pd.DataFrame:
    """Load EQ_MAST.csv into a DataFrame with normalised columns.

    The CSV has columns: SYMBOL, ISIN, SECTOR, INDUSTRY, MARKET_CAP.
    """
    df = pd.read_csv(
        path,
        dtype=str,
        keep_default_na=False,
    )
    # Normalise column names
    df.columns = [c.strip().upper() for c in df.columns]

    # Map whatever names we get
    col_map = {}
    for col in df.columns:
        if col in ("SYMBOL", "TCKRSYMB", "SYMBOL"):
            col_map[col] = "symbol"
        elif "ISIN" in col:
            col_map[col] = "isin"
        elif col in ("SECTOR", "SECTORNAME"):
            col_map[col] = "sector"
        elif col in ("INDUSTRY", "INDUSTRYNAME"):
            col_map[col] = "industry"
        elif col in ("MARKET_CAP", "MARKETCAP"):
            col_map[col] = "market_cap"
    df.rename(columns=col_map, inplace=True)

    # Ensure required columns exist
    for req in ("symbol",):
        if req not in df.columns:
            raise ValueError(f"Required column '{req}' not found in equity master CSV")

    # Fill missing optional columns
    for col in ("isin", "sector", "industry", "market_cap"):
        if col not in df.columns:
            df[col] = ""

    df["sector"] = df["sector"].replace("", "UNKNOWN").fillna("UNKNOWN")
    df["industry"] = df["industry"].replace("", "UNKNOWN").fillna("UNKNOWN")

    return df


def _upsert_equity_master(df: pd.DataFrame) -> int:
    """Batch upsert equity master data."""
    conn = get_connection()
    cursor = conn.cursor()

    from datetime import date
    today_str = date.today().isoformat()

    rows = []
    for _, row in df.iterrows():
        rows.append((
            str(row["symbol"]).strip(),
            str(row.get("isin", "")).strip(),
            str(row.get("sector", "UNKNOWN")).strip(),
            str(row.get("industry", "UNKNOWN")).strip(),
            str(row.get("market_cap", "")).strip(),
            today_str,
        ))

    total = len(rows)
    for i in range(0, total, 500):
        batch = rows[i:i + 500]
        cursor.executemany(UPSERT_SQL, batch)

    conn.commit()
    conn.close()
    logger.info("Upserted %d rows to equity_master", total)
    return total


# ── Stage Entry Point ────────────────────────────────────────────────────────

def run_stage(start_date=None, end_date=None):
    """Download (if needed) and upsert equity master data.

    Args:
        start_date: Ignored for this stage (always fetches full master).
        end_date: Ignored for this stage.
    """
    _t0 = time.perf_counter()
    logger.info("Starting equity_master stage")

    # Use cached file if available
    if EQ_MAST_PATH.exists():
        logger.info("Using cached equity master at %s", EQ_MAST_PATH)
        path = EQ_MAST_PATH
    else:
        path = _download_equity_master()

    df = _load_equity_master_csv(path)
    count = _upsert_equity_master(df)

    _elapsed = time.perf_counter() - _t0
    logger.info("Completed equity_master stage: %d symbols (%.2fs)", count, _elapsed)
    return count


# ── Standalone ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_stage()
