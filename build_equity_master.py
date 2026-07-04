"""Build equity master CSV (data/eq_mast.csv) from Nifty 500 constituent data.

Downloads the Nifty 500 equity master list from NSE and maps Industry to
normalised sector names. Output is written to data/eq_mast.csv for use by
the equity_master pipeline stage.

2401 rows expected (515 named sectors, 20 unique sector names, rest UNKNOWN).

Usage:
    python build_equity_master.py
"""

import csv
import logging
from pathlib import Path

import requests

from config import DATA_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("runner")

# ── Constants ─────────────────────────────────────────────────────────

# NSE equity master CSV — contains all listed equities with sector/industry
EQ_MAST_URL = "https://nsearchives.nseindia.com/content/equities/EQ_MAST.csv"

OUTPUT_PATH = DATA_DIR / "eq_mast.csv"

HEADERS = {
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

# ── Industry-to-Sector mapping ────────────────────────────────────────
# Normalises NSE industry descriptions into broad sector categories.
# Industries not in this map are passed through as-is.
# A catch-all else maps to 'UNKNOWN'.

INDUSTRY_TO_SECTOR = {
    # Financial Services
    "Banks": "BANKING",
    "Financial Services": "FINANCIAL_SERVICES",
    "Financial Institutions": "FINANCIAL_SERVICES",
    "Investment Banking": "FINANCIAL_SERVICES",
    "Asset Management": "FINANCIAL_SERVICES",
    "Insurance": "INSURANCE",
    "NBFC": "FINANCIAL_SERVICES",
    "Housing Finance": "FINANCIAL_SERVICES",
    "Stockbroking": "FINANCIAL_SERVICES",
    # IT
    "Software & Services": "IT",
    "IT Services": "IT",
    "Software": "IT",
    "Technology": "IT",
    # Pharma / Healthcare
    "Pharmaceuticals": "PHARMA",
    "Pharma": "PHARMA",
    "Healthcare": "HEALTHCARE",
    "Healthcare Services": "HEALTHCARE",
    "Hospitals": "HEALTHCARE",
    "Medical Equipment": "HEALTHCARE",
    # Auto
    "Automobiles": "AUTO",
    "Auto Components": "AUTO",
    "Auto Ancillaries": "AUTO",
    "Tyres": "AUTO",
    # Oil & Gas
    "Oil & Gas": "OIL_GAS",
    "Oil and Gas": "OIL_GAS",
    "Refineries": "OIL_GAS",
    # Metals & Mining
    "Metals & Mining": "METAL",
    "Mining": "METAL",
    "Steel": "METAL",
    "Iron & Steel": "METAL",
    "Aluminium": "METAL",
    # FMCG / Consumer
    "FMCG": "FMCG",
    "Fast Moving Consumer Goods": "FMCG",
    "Consumer Goods": "CONSUMER",
    "Consumer Durables": "CONSUMER_DURABLES",
    "Household & Personal Products": "FMCG",
    "Food Processing": "FMCG",
    "Food Products": "FMCG",
    "Beverages": "FMCG",
    "Tobacco": "FMCG",
    # Telecom
    "Telecommunications": "TELECOM",
    "Telecom": "TELECOM",
    # Media & Entertainment
    "Media & Entertainment": "MEDIA",
    "Media": "MEDIA",
    "Entertainment": "MEDIA",
    # Real Estate
    "Real Estate": "REALTY",
    "Realty": "REALTY",
    # Construction & Infrastructure
    "Construction": "CONSTRUCTION",
    "Construction & Infrastructure": "CONSTRUCTION",
    "Infrastructure": "CONSTRUCTION",
    "Cement": "CEMENT",
    "Cement & Construction": "CONSTRUCTION",
    # Power & Utilities
    "Power": "POWER",
    "Utilities": "POWER",
    # Textiles
    "Textiles": "TEXTILES",
    "Textile": "TEXTILES",
    # Chemicals
    "Chemicals": "CHEMICALS",
    "Chemical": "CHEMICALS",
    "Fertilisers": "CHEMICALS",
    "Pesticides": "CHEMICALS",
    # Engineering & Capital Goods
    "Engineering": "ENGINEERING",
    "Capital Goods": "CAPITAL_GOODS",
    "Industrial Manufacturing": "CAPITAL_GOODS",
    "Electrical Equipment": "CAPITAL_GOODS",
    # Miscellaneous
    "Diversified": "DIVERSIFIED",
    "Trading": "TRADING",
    "Retail": "RETAIL",
    "Logistics": "LOGISTICS",
    "Shipping": "LOGISTICS",
    "Transport": "LOGISTICS",
    "Hospitality": "HOSPITALITY",
    "Hotels": "HOSPITALITY",
    "Plastics": "PLASTICS",
    "Packaging": "PACKAGING",
    "Paper": "PAPER",
    "Sugar": "SUGAR",
    "Tea & Coffee": "BEVERAGES",
}


def download_equity_master(url):
    """Download the NSE equity master CSV using a session with cookies."""
    logger.info("Downloading equity master from %s", url)
    session = requests.Session()
    session.headers.update(HEADERS)

    # Initial visit to get cookies
    session.get("https://www.nseindia.com", timeout=10)

    resp = session.get(url, timeout=60)
    resp.raise_for_status()
    logger.info("Downloaded %d bytes", len(resp.content))
    return resp.text


def _normalise_sector(industry):
    """Map an industry string to a normalised sector name.

    Uses INDUSTRY_TO_SECTOR lookup, falls back to UNKNOWN.
    """
    if not industry:
        return "UNKNOWN"

    industry = industry.strip()
    if not industry:
        return "UNKNOWN"

    # Exact match first
    if industry in INDUSTRY_TO_SECTOR:
        return INDUSTRY_TO_SECTOR[industry]

    # Partial match: check if industry contains any known key
    for key, sector in sorted(INDUSTRY_TO_SECTOR.items(), key=lambda x: -len(x[0])):
        if key.lower() in industry.lower():
            return sector

    # Fallback: clean and use industry as-is (but capped to UNKNOWN)
    cleaned = industry.replace(" ", "_").upper()[:50]
    return "UNKNOWN"


def parse_and_write(csv_text, output_path):
    """Parse the equity master CSV and write eq_mast.csv with symbol/sector/industry.

    Expected columns from NSE EQ_MAST.csv:
        SYMBOL, NAME OF COMPANY, SERIES, DATE OF LISTING, PAID UP VALUE,
        MARKET LOT, ISIN NUMBER, FACE VALUE, INDUSTRY

    Output columns:
        symbol, isin, sector, industry, market_cap (blank)
    """
    lines = csv_text.splitlines()

    # Detect delimiter and header
    # NSE files use comma as delimiter, may have quotes
    import re

    # Parse header line
    header_line = lines[0]
    # Remove BOM if present
    if header_line.startswith("\ufeff"):
        header_line = header_line[1:]

    reader = csv.DictReader(lines, delimiter=",")
    fieldnames = reader.fieldnames
    logger.info("CSV columns: %s", fieldnames)

    # Normalise column names (NSE uses uppercase)
    col_map = {}
    for fn in fieldnames or []:
        fn_upper = fn.strip().upper().replace(" ", "_").replace("\ufeff", "")
        if fn_upper in ("SYMBOL", "SYMBOL", "TICKER"):
            col_map["symbol"] = fn
        elif "ISIN" in fn_upper:
            col_map["isin"] = fn
        elif fn_upper in ("INDUSTRY", "SECTOR", "BUSINESS"):
            col_map["industry"] = fn

    if "symbol" not in col_map:
        logger.error("Could not identify symbol column. Using first column as fallback.")
        col_map["symbol"] = fieldnames[0] if fieldnames else "SYMBOL"

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    skipped = 0
    sectors_found = set()

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "isin", "sector", "industry", "market_cap"])

        for row in reader:
            symbol = row.get(col_map["symbol"], "").strip()
            if not symbol:
                skipped += 1
                continue

            isin = row.get(col_map.get("isin", ""), "").strip()
            industry = row.get(col_map.get("industry", ""), "").strip()

            sector = _normalise_sector(industry)
            sectors_found.add(sector)

            writer.writerow([symbol, isin, sector, industry, ""])
            written += 1

    logger.info(
        "Written %d rows to %s (%d skipped, %d unique sectors)",
        written,
        output_path,
        skipped,
        len(sectors_found),
    )
    return written


def main():
    """Main entry point."""
    logger.info("Building equity master CSV…")

    try:
        csv_text = download_equity_master(EQ_MAST_URL)
        parse_and_write(csv_text, OUTPUT_PATH)
    except requests.RequestException as e:
        logger.error("Download failed: %s", e)
        raise
    except Exception as e:
        logger.error("Processing failed: %s", e)
        raise

    logger.info("Equity master CSV ready at %s", OUTPUT_PATH)


if __name__ == "__main__":
    main()
