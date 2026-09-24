"""UK-county lookup for coaches, ported from the Azure bb-county-tool.

Faithful reimplementation of that tool's ``/api/counties`` logic:
- a coach's English-tournament postcodes are each cleaned and looked up in the
  full postcode -> county-code map, with ``POSTCODES_EXTRA`` overrides for
  malformed NAF-database entries,
- codes map to county names via ``CODE_TO_COUNTY``,
- distinct counties are counted and turned into a gamified ``patch_level``.

The full 2.26M-entry map is bundled gzipped (data/postcodes_lite.json.gz) and,
on first use, materialised into an on-disk SQLite database. Lookups then hit the
DB rather than a ~400 MB in-memory dict, so the process stays small while the
result is byte-identical to the original tool. Exceptions are kept in code.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).parent / "data"
_POSTCODES_GZ = _DATA_DIR / "postcodes_lite.json.gz"
# Where the materialised SQLite DB lives (overridable for read-only installs).
_DB_PATH = Path(os.getenv("NAF_POSTCODE_DB") or (_DATA_DIR / "postcodes.sqlite"))
_BUILD_LOCK = threading.Lock()

# County code (0-47) -> county name. From the bb-county-tool.
CODE_TO_COUNTY = {
    0: "Surrey", 1: "Somerset", 2: "East Riding of Yorkshire", 3: "Wiltshire",
    4: "Cumbria", 5: "City of London", 6: "Cambridgeshire", 7: "Devon",
    8: "Berkshire", 9: "Isle of Wight", 10: "Derbyshire", 11: "North Yorkshire",
    12: "South Yorkshire", 13: "Warwickshire", 14: "Suffolk", 15: "Kent",
    16: "Merseyside", 17: "Northamptonshire", 18: "Leicestershire", 19: "Bedfordshire",
    20: "Dorset", 21: "Cheshire", 22: "Northumberland", 23: "Cornwall",
    24: "Rutland", 25: "Worcestershire", 26: "Tyne and Wear", 27: "Lancashire",
    28: "Staffordshire", 29: "Shropshire", 30: "Oxfordshire", 31: "Hertfordshire",
    32: "Buckinghamshire", 33: "Herefordshire", 34: "West Midlands",
    35: "Greater Manchester", 36: "Nottinghamshire", 37: "Norfolk", 38: "West Yorkshire",
    39: "Gloucestershire", 40: "County Durham", 41: "Essex", 42: "West Sussex",
    43: "Hampshire", 44: "East Sussex", 45: "Bristol", 46: "Lincolnshire",
    47: "Greater London",
}

# Postcodes incorrectly entered in the NAF database, mapped to their county code.
POSTCODES_EXTRA = {
    "rg1": 8, "BS3 OEJ": 45, "A13 4LS": 3, "LA1 4JW 3PY": 27, "NN118BR": 17,
    "NN11 8BR": 17, "ST15": 28, "EX73LB": 7, "GL59 3HA": 39, "Oxfordshire": 30,
    "Po30": 9, "NG17 2WS": 36,
}


def build_postcode_db(db_path: Path = _DB_PATH, source_gz: Path = _POSTCODES_GZ) -> Path:
    """Materialise the gzipped postcode map into an on-disk SQLite DB.

    Idempotent and atomic (writes to a temp file then renames). Safe to call at
    container-build time or lazily at runtime.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()
    with gzip.open(source_gz, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    conn = sqlite3.connect(tmp)
    try:
        conn.execute("CREATE TABLE postcodes (code TEXT PRIMARY KEY, county INTEGER) WITHOUT ROWID")
        conn.executemany("INSERT OR REPLACE INTO postcodes VALUES (?, ?)", data.items())
        conn.commit()
    finally:
        conn.close()
    tmp.replace(db_path)
    return db_path


def _ensure_db() -> Path:
    if not _DB_PATH.exists():
        with _BUILD_LOCK:
            if not _DB_PATH.exists():
                build_postcode_db()
    return _DB_PATH


def _open_ro() -> sqlite3.Connection:
    """A fresh read-only connection (cheap; one per request keeps threads safe)."""
    _ensure_db()
    return sqlite3.connect(f"file:{_DB_PATH}?mode=ro", uri=True)


def _clean(code: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "", code).upper()


def _county_name(conn: sqlite3.Connection, raw: str):
    if raw in POSTCODES_EXTRA:
        return CODE_TO_COUNTY.get(POSTCODES_EXTRA[raw])
    row = conn.execute("SELECT county FROM postcodes WHERE code = ?", (_clean(raw),)).fetchone()
    return CODE_TO_COUNTY.get(row[0]) if row is not None else None


def county_for_postcode(raw: str):
    """County name for a single raw postcode, or None if unresolved."""
    conn = _open_ro()
    try:
        return _county_name(conn, raw)
    finally:
        conn.close()


def counties_for_postcodes(postcodes) -> tuple:
    """Distinct counties (sorted) and the count of unresolved postcodes."""
    conn = _open_ro()
    try:
        counties: list = []
        errors = 0
        for raw in postcodes:
            county = _county_name(conn, raw)
            if county is None:
                errors += 1
            elif county not in counties:
                counties.append(county)
        return sorted(counties), errors
    finally:
        conn.close()


@lru_cache(maxsize=1)
def patch_thresholds():  # kept as a function so the tiers live in one place
    return ((5, 0), (10, 5), (15, 10), (20, 15), (30, 20), (48, 30))


def patch_level(county_count: int) -> int:
    """Gamified reward tier for the number of counties played in (bb-county-tool)."""
    for ceiling, level in patch_thresholds():
        if county_count < ceiling:
            return level
    return 50
