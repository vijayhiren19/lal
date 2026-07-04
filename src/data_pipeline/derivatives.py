"""Derivatives Stage.

Fetches FO UDiFF futures data via NSE daily-reports API.
Parses ZIP contents, extracts stock futures (STF) rows at nearest expiry,
computes basis_pct and oi_change_pct.

Source: GET https://www.nseindia.com/api/daily-reports?key=FO
"""

import io
import logging
import zipfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import requests

from db.connection import get_connection

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

DAILY_REPORTS_URL = "https://www.nseindia.com/api/daily-reports?key=FO"
FILE_KEY = "FO-UDIFF-BHAVCOPY-CSV"

INSERT_SQL = """
    INSERT OR REPLACE INTO futures_data
    (trade_date, symbol, expiry_date,
     futures_open, futures_high, futures_low, futures_close,
     futures_oi, futures_oi_chg, futures_volume,
     spot_price, basis_pct, oi_change_pct)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _parse_nse_date(date_str: str) -> str:
    """Parse NSE date to ISO 'YYYY-MM-DD'.

    Handles two formats:
      - 'DD-Mon-YYYY' (e.g. '25-Aug-2026')  ← legacy NSE format
      - 'YYYY-MM-DD'  (e.g. '2026-08-25')   ← newer NSE format
    """
    s = date_str.strip()
    # If already ISO format, return as-is
    if len(s) == 10 and s[4] == '-' and s[7] == '-':
        return s
    return datetime.strptime(s, "%d-%b-%Y").strftime("%Y-%m-%d")


def _fetch_daily_reports_json() -> list:
    """Fetch the daily-reports JSON from NSE.

    Returns:
        List of item dicts from CurrentDay and PreviousDay arrays.
    """
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    session.get("https://www.nseindia.com", timeout=10)

    resp = session.get(DAILY_REPORTS_URL, timeout=30)
    resp.raise_for_status()

    data = resp.json()
    session.close()

    items = []
    for key in ("CurrentDay", "PreviousDay"):
        items.extend(data.get(key, []))

    return items


def _find_fo_bhavcopy(items: list) -> tuple:
    """Find the FO UDiFF bhavcopy entry in the daily reports list.

    Args:
        items: List of report item dicts.

    Returns:
        Tuple of (trading_date_iso, download_url) or None.
    """
    for item in items:
        if item.get("fileKey") == FILE_KEY:
            trading_date = _parse_nse_date(item["tradingDate"])
            file_path = item["filePath"]
            file_name = item["fileActlName"]
            # Ensure file_path ends with /
            if not file_path.endswith("/"):
                file_path += "/"
            url = file_path + file_name
            return trading_date, url

    return None


def _download_zip(url: str) -> bytes:
    """Download a ZIP file from NSE.

    Args:
        url: Full download URL.

    Returns:
        Raw ZIP bytes.
    """
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    session.get("https://www.nseindia.com", timeout=10)

    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    session.close()

    return resp.content


def _parse_fo_csv(zip_bytes: bytes) -> pd.DataFrame:
    """Parse the FO UDiFF CSV from a ZIP file.

    Args:
        zip_bytes: Raw ZIP file bytes.

    Returns:
        DataFrame with parsed stock futures data.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Find the CSV file inside the ZIP
        csv_files = [n for n in zf.namelist() if n.endswith(".csv")]
        if not csv_files:
            raise ValueError("No CSV found in FO UDiFF ZIP")
        csv_name = csv_files[0]
        with zf.open(csv_name) as f:
            df = pd.read_csv(f, dtype=str, keep_default_na=False)

    # Normalise column names (trim whitespace)
    df.columns = [c.strip() for c in df.columns]

    # Filter to stock futures (STF = Stock Futures)
    df = df[df["FinInstrmTp"] == "STF"].copy()

    if df.empty:
        logger.warning("No STF rows found in FO UDiFF data")
        return df

    # Parse numeric columns
    numeric_cols = [
        "OpnPric", "HghPric", "LwPric", "ClsPric",
        "OpnIntrst", "ChngInOpnIntrst", "TtlTradgVol",
        "UndrlygPric",
    ]
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    # Rename to standard column names
    df.rename(columns={
        "TckrSymb": "symbol",
        "XpryDt": "expiry_date",
        "OpnPric": "futures_open",
        "HghPric": "futures_high",
        "LwPric": "futures_low",
        "ClsPric": "futures_close",
        "OpnIntrst": "futures_oi",
        "ChngInOpnIntrst": "futures_oi_chg",
        "TtlTradgVol": "futures_volume",
        "UndrlygPric": "spot_price",
    }, inplace=True)

    # Parse expiry_date (format: DD-Mon-YYYY)
    df["expiry_date"] = df["expiry_date"].apply(
        lambda x: _parse_nse_date(x) if x else ""
    )

    # ── Keep only nearest expiry per symbol ───────────────────────────
    # Multiple expiries exist; we keep the nearest (earliest) one
    df = df.sort_values("expiry_date").groupby("symbol", sort=False).first().reset_index()

    return df


def _compute_derived(df: pd.DataFrame) -> pd.DataFrame:
    """Compute derived columns: basis_pct, oi_change_pct.

    Args:
        df: DataFrame with raw futures columns.

    Returns:
        DataFrame with derived columns added.
    """
    # basis_pct = (futures_close - spot_price) / spot_price * 100
    df["basis_pct"] = np.where(
        df["spot_price"] > 0,
        ((df["futures_close"] - df["spot_price"]) / df["spot_price"] * 100).round(2),
        0.0,
    )

    # oi_change_pct = (oi_chg / oi) * 100
    df["oi_change_pct"] = np.where(
        df["futures_oi"] > 0,
        (df["futures_oi_chg"] / df["futures_oi"] * 100).round(2),
        0.0,
    )

    return df


# ── Stage Entry Point ────────────────────────────────────────────────────────

def run_stage(start_date: str = None, end_date: str = None):
    """Run the derivatives stage.

    Fetches FO UDiFF bhavcopy from NSE daily-reports API (most recent 1-2 days),
    parses stock futures, computes basis and OI metrics, batch upserts
    into futures_data table.

    Note: FO UDiFF data is only available for recent 1-2 days via the API.
    Historical dates will have no data to process.
    """
    logger.info("Starting derivatives stage")

    try:
        # ── 1. Fetch daily reports JSON ───────────────────────────────
        items = _fetch_daily_reports_json()
        fo_entry = _find_fo_bhavcopy(items)

        if fo_entry is None:
            logger.warning("No FO-UDIFF-BHAVCOPY-CSV entry found in daily reports")
            return

        trade_date, download_url = fo_entry
        logger.info("Found FO bhavcopy for %s", trade_date)

        # ── 2. Download and parse ─────────────────────────────────────
        zip_bytes = _download_zip(download_url)
        df = _parse_fo_csv(zip_bytes)

        if df.empty:
            logger.warning("No stock futures data parsed")
            return

        # ── 3. Compute derived columns ────────────────────────────────
        df = _compute_derived(df)
        df["trade_date"] = trade_date

        logger.info("Parsed %d stock futures rows for %s", len(df), trade_date)

        # ── 4. Batch upsert ───────────────────────────────────────────
        conn = get_connection()
        cursor = conn.cursor()

        batch = []
        for _, row in df.iterrows():
            batch.append((
                row["trade_date"],
                row["symbol"],
                row["expiry_date"],
                _get(row, "futures_open"),
                _get(row, "futures_high"),
                _get(row, "futures_low"),
                _get(row, "futures_close"),
                int(_get(row, "futures_oi", 0)),
                int(_get(row, "futures_oi_chg", 0)),
                int(_get(row, "futures_volume", 0)),
                _get(row, "spot_price"),
                _get(row, "basis_pct"),
                _get(row, "oi_change_pct"),
            ))
            if len(batch) >= 500:
                cursor.executemany(INSERT_SQL, batch)
                batch.clear()

        if batch:
            cursor.executemany(INSERT_SQL, batch)

        conn.commit()
        conn.close()

        logger.info("Completed derivatives stage: %d rows upserted", len(df))

    except Exception as e:
        logger.error("Derivatives stage failed: %s", str(e), exc_info=True)


def _get(row, col, default=None):
    """Safely get a value from a pandas row."""
    val = row.get(col, default)
    if val is None:
        return default
    if isinstance(val, (int, float, np.integer, np.floating)):
        if np.isnan(val):
            return default
        return float(val)
    return val


# ── Standalone ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_stage()
