"""Tournament listings from the NAF dump, for calendars and maps.

The NAF tournament table (``NafDatamine.tour_data``) has names, dates, addresses
and postcodes, but its latitude/longitude fields are empty for UK events. So
coordinates come from the postcode, via postcodes.io (free, no key), with a
disk cache so each postcode is looked up once.

Nothing here downloads the NAF dump: pass in a loaded ``NafDatamine`` (or its
``tour_data`` DataFrame).
"""

from __future__ import annotations

import json
import re
import threading
from datetime import date
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
import requests

# NAF variant ids (naf_tournament.naf_variantsid) -> the label the NAF shows on
# each tournament page. Read from member.thenaf.net, 24 Sep 2026. 0 = not set.
VARIANT_NAMES = {
    1: "Classic",
    2: "Dungeon Bowl",
    3: "Street Bowl",
    4: "Sevens",
    5: "Death Bowl",
    7: "Beach Bowl",
    8: "Online",
    9: "Dungeon 7s",
    10: "Death Bowl 7s",
    11: "Specialist",
    12: "Draft",
    13: "Blood Bowl 2020",
    14: "Gutter Bowl",
    15: "Blood Bowl 2025",
}
UNKNOWN_VARIANT = "Not stated"

UK_NATIONS = ("England", "Wales", "Scotland", "Northern Ireland")

NAF_TOURNAMENT_URL = "https://member.thenaf.net/index.php?module=NAF&type=tournaments&func=view&id={id}"

POSTCODES_IO = "https://api.postcodes.io"

# A full UK postcode: outward code (e.g. "LS28") + inward code (e.g. "5LY").
_POSTCODE_RE = re.compile(r"^([A-Z]{1,2}[0-9][0-9A-Z]?)\s*([0-9][A-Z]{2})$")
_OUTCODE_RE = re.compile(r"^[A-Z]{1,2}[0-9][0-9A-Z]?$")


def variant_name(variant_id) -> str:
    try:
        return VARIANT_NAMES.get(int(variant_id), UNKNOWN_VARIANT)
    except (TypeError, ValueError):
        return UNKNOWN_VARIANT


def normalise_postcode(raw) -> Optional[str]:
    """'ls285ly' -> 'LS28 5LY'. Returns None if it isn't a UK postcode shape."""
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    text = re.sub(r"\s+", "", str(raw)).upper()
    m = _POSTCODE_RE.match(text)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return text if _OUTCODE_RE.match(text) else None


def _clean_text(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_url(value) -> Optional[str]:
    text = _clean_text(value)
    if not text or " " in text or "." not in text:
        return None
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text
    return text


def _iso(value) -> Optional[str]:
    ts = pd.to_datetime(value, errors="coerce")
    return None if pd.isna(ts) else ts.date().isoformat()


def list_tournaments(
    source,
    nations: Optional[Iterable[str]] = UK_NATIONS,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    approved_only: bool = True,
) -> list:
    """Tournaments as plain dicts, sorted by start date.

    ``source`` is a loaded ``NafDatamine`` or its ``tour_data`` DataFrame.
    ``date_from`` defaults to today and matches on the end date, so an event
    already under way is still listed. ``nations=None`` means every nation.
    """
    td: pd.DataFrame = source if isinstance(source, pd.DataFrame) else source.tour_data
    start = pd.to_datetime(td["tournamentstartdate"], errors="coerce")
    end = pd.to_datetime(td["tournamentenddate"], errors="coerce").fillna(start)

    keep = start.notna()
    keep &= end >= pd.Timestamp(date_from or date.today().isoformat())
    if date_to:
        keep &= start <= pd.Timestamp(date_to)
    if nations is not None:
        keep &= td["tournamentnation"].isin(list(nations))
    if approved_only:
        keep &= td["tournamentstatus"].astype(str).str.upper().eq("APPROVED")

    rows = []
    for idx, t in td[keep].iterrows():
        rows.append(
            {
                "naf_id": int(t["tournamentid"]),
                "name": _clean_text(t["tournamentname"]),
                "start_date": start[idx].date().isoformat(),
                "end_date": end[idx].date().isoformat(),
                "variant": variant_name(t.get("naf_variantsid")),
                "style": _clean_text(t.get("tournamentstyle")),
                "type": _clean_text(t.get("tournamenttype")),
                "major": str(t.get("tournamentmajor", "")).lower() == "yes",
                "venue": _clean_text(t.get("tournamentaddress1")),
                "town": _clean_text(t.get("tournamentcity")),
                "postcode": normalise_postcode(t.get("tournamentzip")),
                "nation": _clean_text(t.get("tournamentnation")),
                "website": _clean_url(t.get("tournamenturl")),
                "naf_url": NAF_TOURNAMENT_URL.format(id=int(t["tournamentid"])),
                "lat": None,
                "lng": None,
            }
        )
    rows.sort(key=lambda r: (r["start_date"], r["name"] or ""))
    return rows


class PostcodeGeocoder:
    """Postcode -> (lat, lng) through postcodes.io, cached on disk.

    Full postcodes are looked up in bulk; any that fail (terminated, mistyped)
    fall back to the centre of their outward code, which is close enough for a
    map pin. A tournament with no usable postcode falls back to its town name. Network errors leave coordinates empty rather than raising.
    Failed lookups are not cached, so they are retried next time.
    """

    def __init__(self, cache_path, session: Optional[requests.Session] = None, timeout: float = 20.0):
        self.cache_path = Path(cache_path)
        self.session = session or requests.Session()
        self.timeout = timeout
        self._lock = threading.Lock()
        try:
            self._cache = json.loads(self.cache_path.read_text())
        except (OSError, ValueError):
            self._cache = {}

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._cache))
        tmp.replace(self.cache_path)

    def _bulk(self, postcodes: list) -> dict:
        found = {}
        for i in range(0, len(postcodes), 100):
            chunk = postcodes[i : i + 100]
            try:
                r = self.session.post(f"{POSTCODES_IO}/postcodes", json={"postcodes": chunk}, timeout=self.timeout)
                r.raise_for_status()
            except requests.RequestException:
                continue
            for item in r.json().get("result") or []:
                res = item.get("result")
                if res and res.get("latitude") is not None:
                    found[normalise_postcode(item["query"])] = [res["latitude"], res["longitude"]]
        return found

    def _outcode(self, outcode: str) -> Optional[list]:
        try:
            r = self.session.get(f"{POSTCODES_IO}/outcodes/{outcode}", timeout=self.timeout)
            if r.status_code != 200:
                return None
            res = r.json().get("result") or {}
        except (requests.RequestException, ValueError):
            return None
        if res.get("latitude") is None:
            return None
        return [res["latitude"], res["longitude"]]

    def _town(self, town: str) -> Optional[list]:
        try:
            r = self.session.get(f"{POSTCODES_IO}/places", params={"q": town, "limit": 1}, timeout=self.timeout)
            if r.status_code != 200:
                return None
            res = (r.json().get("result") or [None])[0] or {}
        except (requests.RequestException, ValueError, IndexError):
            return None
        if res.get("latitude") is None:
            return None
        return [res["latitude"], res["longitude"]]

    def town(self, name: Optional[str]) -> Optional[list]:
        """Centre of a named place (e.g. 'Folkestone'), cached. None if not found."""
        clean = re.sub(r"[^A-Za-z' -]", "", name or "").strip()
        if not clean:
            return None
        key = "town:" + clean.lower()
        with self._lock:
            if key not in self._cache:
                coords = self._town(clean)
                if not coords:
                    return None
                self._cache[key] = coords
                self._save()
            return self._cache[key]

    def lookup(self, postcodes: Iterable[str]) -> dict:
        """Returns {postcode: [lat, lng]} for every postcode that could be placed."""
        wanted = sorted({p for p in postcodes if p})
        with self._lock:
            missing = [p for p in wanted if p not in self._cache]
            full = [p for p in missing if " " in p]
            new = self._bulk(full) if full else {}
            for p in missing:
                if p in new:
                    continue
                coords = self._outcode(p.split(" ")[0])
                if coords:
                    new[p] = coords
            if new:
                self._cache.update(new)
                self._save()
            return {p: self._cache[p] for p in wanted if p in self._cache}

    def add_coordinates(self, tournaments: list) -> list:
        """Fills ``lat``/``lng`` in place on each tournament that has a postcode; returns the list."""
        coords = self.lookup(t["postcode"] for t in tournaments)
        for t in tournaments:
            c = coords.get(t["postcode"]) or self.town(t.get("town"))
            if c:
                t["lat"], t["lng"] = c
        return tournaments
