"""
NSE Data Downloader and Parser (fetcher).

Downloads BhavCopy CSVs (ZIP → CSV) and MTO delivery DAT files from NSE,
parses them into stage (OHLCV) and delivery tables.

Three exports:
  download_nse_data(start_date, end_date, from_disk_only=False)
  process_nse_data(start_date, end_date)
  load_historical_data(start_date=None, end_date=None, from_disk_only=False)
"""

import logging
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta

import numpy as np
import pandas as pd
import requests

from config import DATA_DIR
from db.connection import get_connection

logger = logging.getLogger("runner")

# ── Constants ──────────────────────────────────────────────────────────────

EXCHANGE = "NSE"

BHAVCOPY_DIR = DATA_DIR / "bhavcopy_nse"
DELIVERY_DIR = DATA_DIR / "delivery_nse"
ERROR_LOG = DATA_DIR / "download_errors.log"

BHAVCOPY_URL_TEMPLATE = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{YYYYMMDD}_F_0000.csv.zip"
)
MTO_URL_TEMPLATE = (
    "https://archives.nseindia.com/archives/equities/mto/MTO_{DDMMYYYY}.DAT"
)

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# NSE Trading Holidays 2025–2026
NSE_HOLIDAYS_2025_2026 = {
    # 2025 (14 holidays)
    date(2025, 2, 26), date(2025, 3, 14), date(2025, 3, 31),
    date(2025, 4, 10), date(2025, 4, 14), date(2025, 4, 18),
    date(2025, 5, 1), date(2025, 8, 15), date(2025, 8, 27),
    date(2025, 10, 2), date(2025, 10, 21), date(2025, 10, 22),
    date(2025, 11, 5), date(2025, 12, 25),
    # 2026 (16 holidays)
    date(2026, 1, 15), date(2026, 1, 26), date(2026, 3, 3),
    date(2026, 3, 26), date(2026, 3, 31), date(2026, 4, 3),
    date(2026, 4, 14), date(2026, 5, 1), date(2026, 5, 28),
    date(2026, 6, 26), date(2026, 9, 14), date(2026, 10, 2),
    date(2026, 10, 20), date(2026, 11, 10), date(2026, 11, 24),
    date(2026, 12, 25),
}

# Special trading days when NSE is open on a weekend (e.g., Budget day, special sessions)
SPECIAL_TRADING_DAYS = {
    date(2026, 2, 28),  # Saturday — Union Budget / special trading session
}

STAGE_INSERT_SQL = """
    INSERT OR REPLACE INTO stage
    (exchange, trade_date, symbol,
     open_price, high_price, low_price, close_price, previous_close,
     traded_volume, traded_value,
     day_return_pct, upper_circuit_hit, lower_circuit_hit, isin)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

STAGE_DELIVERY_INSERT_SQL = """
    INSERT OR REPLACE INTO stage_delivery
    (exchange, trade_date, symbol, qty, pct)
    VALUES (?, ?, ?, ?, ?)
"""

# ── Column name mappings for the three NSE Bhavcopy formats ────────────────

_COLUMN_MAP_OLD = {
    "TCKRSYMB": "SYMBOL",
    "SCTYSRS": "SERIES",
    "OPNPRIC": "OPEN",
    "HGHPRIC": "HIGH",
    "LWPRIC": "LOW",
    "CLSPRIC": "CLOSE",
    "PRVSCLSGPRIC": "PREVCLOSE",
    "TTLTRADGVOL": "TOTTRDQTY",
    "TTLTRFVAL": "TOTTRDVAL",
}

_COLUMN_MAP_MF = {
    "TckrSymb": "SYMBOL",
    "SctySrs": "SERIES",
    "OpnPric": "OPEN",
    "HghPric": "HIGH",
    "LwPric": "LOW",
    "ClsPric": "CLOSE",
    "PrvsClsgPric": "PREVCLOSE",
    "TtlTradgVol": "TOTTRDQTY",
    "TtlTrfVal": "TOTTRDVAL",
}

_STANDARD_COLS = [
    "SYMBOL", "SERIES", "OPEN", "HIGH", "LOW",
    "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TOTTRDVAL",
]


# ── Helpers ────────────────────────────────────────────────────────────────

def _safe_rnd(result):
    """Round a pandas Series to 2 decimal places, or return None if None."""
    return result.round(2) if result is not None else None


def _business_days(start, end):
    """Yield trading dates between start and end, inclusive.

    Regular trading: Mon–Fri, excluding NSE_HOLIDAYS_2025_2026.
    Also includes SPECIAL_TRADING_DAYS (e.g. Budget Saturday sessions).
    """
    current = start
    while current <= end:
        is_weekday = current.weekday() < 5
        is_special = current in SPECIAL_TRADING_DAYS
        if (is_weekday or is_special) and current not in NSE_HOLIDAYS_2025_2026:
            yield current
        current += timedelta(days=1)


def _log_error(date_str, file_type, reason):
    """Append a line to data/download_errors.log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(ERROR_LOG, "a") as f:
        f.write(f"{timestamp} | DATE: {date_str} | FILE: {file_type} | ERROR: {reason}\n")


def _make_nse_session():
    """Create and return a requests.Session with NSE headers, warmed up."""
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except requests.RequestException:
        pass  # warm-up is best-effort
    return session


# ── Bhavcopy Parsing ───────────────────────────────────────────────────────

def _parse_bhavcopy(filepath):
    """Parse a BhavCopy ZIP → CSV into a DataFrame with standardised columns.

    Detects three column-name formats:
      - Old (TCKRSYMB, SCTYSRS, …)
      - Standard (SYMBOL, SERIES, …)
      - MF / new (TckrSymb, SctySrs, …)

    Returns a DataFrame filtered to SERIES='EQ', or None on failure.
    """
    try:
        with zipfile.ZipFile(filepath) as zf:
            csv_names = [n for n in zf.namelist() if n.endswith(".csv")]
            if not csv_names:
                logger.error("No CSV found inside ZIP: %s", filepath)
                return None
            with zf.open(csv_names[0]) as f:
                raw = pd.read_csv(f)
    except Exception as e:
        logger.error("Failed to read ZIP/CSV %s: %s", filepath, e)
        return None

    if raw.empty:
        logger.warning("Empty CSV in ZIP: %s", filepath)
        return None

    # ── Normalise column names ──────────────────────────────────────
    if "TCKRSYMB" in raw.columns:
        # Pre-2024 old format
        raw.rename(columns=_COLUMN_MAP_OLD, inplace=True)
    elif "TckrSymb" in raw.columns:
        # Post-2025 MF format
        raw.rename(columns=_COLUMN_MAP_MF, inplace=True)
    elif "SYMBOL" not in raw.columns:
        logger.error("Unknown column format in %s. Columns: %s",
                     filepath, list(raw.columns))
        return None
    # else: standard 2024-2025 format — names already match

    # ── Filter to EQ series only ────────────────────────────────────
    if "SERIES" in raw.columns:
        raw = raw[raw["SERIES"] == "EQ"].copy()
    if raw.empty:
        logger.warning("No EQ rows after filtering in %s", filepath)
        return None

    # ── Keep only columns we need (ISIN is optional) ────────────────
    keep = [c for c in _STANDARD_COLS + ["ISIN"] if c in raw.columns]
    df = raw[keep].copy()

    # ── Type conversion and rounding ────────────────────────────────
    for col in ["OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").round(2)
    df["TOTTRDQTY"] = pd.to_numeric(df["TOTTRDQTY"], errors="coerce").fillna(0).astype(int)
    df["TOTTRDVAL"] = pd.to_numeric(df["TOTTRDVAL"], errors="coerce").round(2)

    return df


# ── MTO Delivery Parsing ──────────────────────────────────────────────────

def _parse_mto_dat(filepath):
    """Parse an MTO DAT file into a dict {symbol: {qty, pct}}.

    Format: comma-separated lines starting with '20'.
    Columns (positional): date, ref, symbol, series, isin, qty, pct.
    Only SERIES='EQ' rows are kept.
    """
    result = {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or not line.startswith("20"):
                    continue
                parts = line.split(",")
                if len(parts) < 7:
                    continue
                series = parts[3].strip()
                if series != "EQ":
                    continue
                symbol = parts[2].strip()
                try:
                    qty = int(parts[5].strip())
                    pct = round(float(parts[6].strip()), 2)
                except (ValueError, IndexError):
                    continue
                result[symbol] = {"qty": qty, "pct": pct}
    except FileNotFoundError:
        logger.warning("MTO file not found (delivery skipped): %s", filepath)
    except Exception as e:
        logger.error("Error parsing MTO file %s: %s", filepath, e)
    return result


# ── Download ───────────────────────────────────────────────────────────────

def _download_single_file(url, dest_path, date_str, file_type):
    """Download one file from NSE to disk. Returns True on success.

    Uses a fresh session per call to avoid cookie-stale issues.
    """
    if dest_path.exists():
        return True  # already cached

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    session = _make_nse_session()

    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            msg = f"HTTP {resp.status_code}"
            logger.warning("Download failed — %s %s: %s", file_type, date_str, msg)
            _log_error(date_str, file_type, msg)
            return False

        with open(dest_path, "wb") as f:
            f.write(resp.content)
        logger.info("Downloaded %s (%s): %s — %d bytes",
                    file_type, date_str, dest_path.name, len(resp.content))
        return True

    except requests.RequestException as e:
        logger.warning("Download exception — %s %s: %s", file_type, date_str, e)
        _log_error(date_str, file_type, str(e))
        return False
    except Exception as e:
        logger.warning("Unexpected error — %s %s: %s", file_type, date_str, e)
        _log_error(date_str, file_type, f"Unexpected: {e}")
        return False


def download_nse_data(start_date, end_date, from_disk_only=False):
    """Download all missing Bhavcopy and MTO files for each trading day
    between *start_date* and *end_date* (inclusive).

    Uses ThreadPoolExecutor(max_workers=10) for parallel downloads.
    Files already on disk are skipped.

    When *from_disk_only* is True, this function does nothing.
    """
    if from_disk_only:
        logger.info("from_disk_only=True — download phase skipped")
        return

    dates = list(_business_days(start_date, end_date))
    logger.info("Download phase — %d trading days: %s to %s",
                len(dates), start_date, end_date)

    tasks = []
    for dt in dates:
        bhav_date_str = dt.strftime("%Y%m%d")
        del_date_str = dt.strftime("%d%m%Y")
        date_iso = dt.isoformat()

        # Bhavcopy
        bhav_name = f"BhavCopy_NSE_CM_0_0_0_{bhav_date_str}_F_0000.csv.zip"
        bhav_path = BHAVCOPY_DIR / bhav_name
        if not bhav_path.exists():
            bhav_url = BHAVCOPY_URL_TEMPLATE.format(YYYYMMDD=bhav_date_str)
            tasks.append((bhav_url, bhav_path, date_iso, "Bhavcopy"))

        # MTO Delivery
        del_name = f"MTO_{del_date_str}.DAT"
        del_path = DELIVERY_DIR / del_name
        if not del_path.exists():
            del_url = MTO_URL_TEMPLATE.format(DDMMYYYY=del_date_str)
            tasks.append((del_url, del_path, date_iso, "MTO"))

    if not tasks:
        logger.info("All files already cached on disk — nothing to download")
        return

    logger.info("Downloading %d files with ThreadPoolExecutor(10)", len(tasks))
    with ThreadPoolExecutor(max_workers=10) as executor:
        fut_map = {
            executor.submit(_download_single_file, url, path, ds, ft):
                (url, path, ds, ft)
            for url, path, ds, ft in tasks
        }
        for future in as_completed(fut_map):
            url, path, ds, ft = fut_map[future]
            try:
                future.result()
            except Exception as e:
                logger.error("Thread error — %s %s: %s", ft, ds, e)
                _log_error(ds, ft, f"Thread exception: {e}")


# ── Processing ─────────────────────────────────────────────────────────────

def process_nse_data(start_date, end_date):
    """Parse every cached Bhavcopy CSV and MTO DAT file between
    *start_date* and *end_date*, then batch-insert rows into the
    **stage** and **stage_delivery** tables.

    Returns (stage_rows_inserted, delivery_rows_inserted).
    """
    conn = get_connection()
    cursor = conn.cursor()

    dates = list(_business_days(start_date, end_date))
    logger.info("Processing %d trading days: %s to %s",
                len(dates), start_date, end_date)

    exchange = EXCHANGE
    stage_count = 0
    delivery_count = 0

    for dt in dates:
        bhav_date_str = dt.strftime("%Y%m%d")
        del_date_str = dt.strftime("%d%m%Y")
        date_iso = dt.isoformat()

        # ── Bhavcopy ──────────────────────────────────────────────
        bhav_name = f"BhavCopy_NSE_CM_0_0_0_{bhav_date_str}_F_0000.csv.zip"
        bhav_path = BHAVCOPY_DIR / bhav_name

        if not bhav_path.exists():
            logger.warning("Bhavcopy file missing (skip): %s", bhav_name)
            continue

        df = _parse_bhavcopy(bhav_path)
        if df is None or df.empty:
            continue

        # ── MTO Delivery ──────────────────────────────────────────
        del_name = f"MTO_{del_date_str}.DAT"
        del_path = DELIVERY_DIR / del_name
        del_data = _parse_mto_dat(del_path) if del_path.exists() else {}

        # ── Build rows ────────────────────────────────────────────
        stage_batch = []
        delivery_batch = []

        for _, row in df.iterrows():
            symbol = str(row["SYMBOL"]).strip()
            open_p = float(row["OPEN"])
            high_p = float(row["HIGH"])
            low_p = float(row["LOW"])
            close_p = float(row["CLOSE"])
            prev_close = float(row["PREVCLOSE"])
            volume = int(row["TOTTRDQTY"])
            value = round(float(row["TOTTRDVAL"]), 2)
            isin_val = str(row.get("ISIN", "")).strip() if "ISIN" in row else ""

            # Derived fields
            if prev_close != 0:
                day_return = round((close_p - prev_close) / prev_close * 100, 2)
            else:
                day_return = None
            upper_circuit = 1 if day_return is not None and day_return >= 19.5 else 0
            lower_circuit = 1 if day_return is not None and day_return <= -19.5 else 0

            stage_batch.append((
                exchange, date_iso, symbol,
                round(open_p, 2), round(high_p, 2), round(low_p, 2),
                round(close_p, 2), round(prev_close, 2),
                volume, value,
                day_return, upper_circuit, lower_circuit,
                isin_val if isin_val else None,
            ))

            # Delivery (default to zero if no MTO data for this symbol)
            del_info = del_data.get(symbol, {"qty": 0, "pct": 0.0})
            delivery_batch.append((
                exchange, date_iso, symbol,
                int(del_info["qty"]),
                round(float(del_info["pct"]), 2),
            ))

        # ── Batch INSERT (500 rows per transaction) ───────────────
        for i in range(0, len(stage_batch), 500):
            cursor.executemany(STAGE_INSERT_SQL, stage_batch[i:i + 500])
        stage_count += len(stage_batch)

        for i in range(0, len(delivery_batch), 500):
            cursor.executemany(STAGE_DELIVERY_INSERT_SQL, delivery_batch[i:i + 500])
        delivery_count += len(delivery_batch)

        conn.commit()
        logger.debug("  %s — %d stage, %d stage_delivery rows",
                     date_iso, len(stage_batch), len(delivery_batch))

    conn.close()
    logger.info("Processing complete — %d stage rows, %d stage_delivery rows",
                stage_count, delivery_count)
    return stage_count, delivery_count


# ── Orchestration ──────────────────────────────────────────────────────────

def load_historical_data(start_date=None, end_date=None, from_disk_only=False):
    """Orchestrate the full data load pipeline.

    1. Resolve default dates (None → today / 750 days back).
    2. Ensure cache directories exist.
    3. Download missing files from NSE (unless *from_disk_only*).
    4. Initialise the database schema.
    5. Parse cached files into stage and stage_delivery tables.

    Returns (stage_rows, delivery_rows) as per *process_nse_data*.
    """
    # Resolve default date range
    if end_date is None:
        end_date = date.today()
    elif isinstance(end_date, str):
        end_date = pd.Timestamp(end_date).date()

    if start_date is None:
        start_date = end_date - timedelta(days=750)
    elif isinstance(start_date, str):
        start_date = pd.Timestamp(start_date).date()

    # Ensure directories exist
    BHAVCOPY_DIR.mkdir(parents=True, exist_ok=True)
    DELIVERY_DIR.mkdir(parents=True, exist_ok=True)

    logger.info("load_historical_data — %s to %s (from_disk_only=%s)",
                start_date, end_date, from_disk_only)

    # 1. Download missing files
    download_nse_data(start_date, end_date, from_disk_only=from_disk_only)

    # 2. Init schema (idempotent — creates tables if missing)
    from db.connection import init_schema
    init_schema()

    # 3. Parse → DB
    return process_nse_data(start_date, end_date)


# ── Conditional CLI entry point ────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    import argparse

    parser = argparse.ArgumentParser(description="NSE Data Fetcher")
    parser.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    parser.add_argument("--days-back", type=int, default=None,
                        help="Override start-date: today minus N calendar days")
    parser.add_argument("--from-disk-only", action="store_true",
                        help="Skip NSE download, parse cached files only")
    args = parser.parse_args()

    if args.days_back is not None:
        end = date.today()
        start = end - timedelta(days=args.days_back)
    else:
        start = args.start_date
        end = args.end_date

    load_historical_data(start_date=start, end_date=end,
                         from_disk_only=args.from_disk_only)
