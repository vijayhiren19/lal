"""Project configuration constants."""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = ROOT_DIR / "mydb1.db"

# Ensure data directories exist
DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "bhavcopy_nse").mkdir(parents=True, exist_ok=True)
(DATA_DIR / "delivery_nse").mkdir(parents=True, exist_ok=True)
