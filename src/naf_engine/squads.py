"""Squad-set storage and CSV (de)serialisation.

A *squad set* is a named tournament roster with the same shape as the built-in
``EUROBOWL_SQUADS``::

    {team_name: {coach_name: {"number": int, "race": str}}}

Sets are persisted as one JSON file each under a store directory. The built-in
Eurobowl roster ships as a read-only preset so there's always something to view.

This module is storage-only and engine-independent: it validates structure and
race names, but checking that a NAF number actually exists is left to the caller
(which has the loaded coach data).
"""

from __future__ import annotations

import csv
import io
import json
import re
from pathlib import Path
from typing import Dict, List

from .rosters import EUROBOWL_SQUADS, RACE_IDS

# A squad set: team -> coach -> {"number": int, "race": str}
SquadSet = Dict[str, Dict[str, dict]]

BUILTIN_SETS: Dict[str, SquadSet] = {"Eurobowl 2025": EUROBOWL_SQUADS}

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9 _-]+")


class SquadValidationError(ValueError):
    """A squad set failed validation (bad structure, race, or number)."""


def _safe_filename(name: str) -> str:
    """Map a set name to a safe file stem (prevents path traversal)."""
    cleaned = _SAFE_NAME_RE.sub("", name).strip()
    if not cleaned:
        raise SquadValidationError(f"Invalid squad-set name: {name!r}")
    return cleaned


def validate_squad_set(squads: SquadSet) -> SquadSet:
    """Validate structure, coerce numbers to int, and check race names.

    Returns a normalised copy. Raises SquadValidationError on any problem.
    """
    if not isinstance(squads, dict) or not squads:
        raise SquadValidationError("A squad set must be a non-empty mapping of teams.")

    normalised: SquadSet = {}
    for team, coaches in squads.items():
        if not isinstance(coaches, dict) or not coaches:
            raise SquadValidationError(f"Team {team!r} must have at least one coach.")
        team_out: Dict[str, dict] = {}
        for coach, info in coaches.items():
            if not isinstance(info, dict) or "number" not in info or "race" not in info:
                raise SquadValidationError(
                    f"Coach {coach!r} in {team!r} needs 'number' and 'race'."
                )
            try:
                number = int(info["number"])
            except (TypeError, ValueError):
                raise SquadValidationError(
                    f"Coach {coach!r} in {team!r} has a non-integer number: {info['number']!r}."
                )
            race = str(info["race"]).strip()
            if race not in RACE_IDS:
                raise SquadValidationError(
                    f"Coach {coach!r} in {team!r} has an unknown race: {race!r}."
                )
            team_out[str(coach).strip()] = {"number": number, "race": race}
        normalised[str(team).strip()] = team_out
    return normalised


def squads_from_csv(text: str) -> SquadSet:
    """Parse a ``squad,coach,number,race`` CSV into a (validated) squad set."""
    reader = csv.DictReader(io.StringIO(text))
    required = {"squad", "coach", "number", "race"}
    if reader.fieldnames is None or not required.issubset({f.strip().lower() for f in reader.fieldnames}):
        raise SquadValidationError(
            f"CSV must have a header with columns: {', '.join(sorted(required))}."
        )
    squads: SquadSet = {}
    for line_no, row in enumerate(reader, start=2):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        team, coach = row.get("squad"), row.get("coach")
        if not team or not coach:
            raise SquadValidationError(f"Row {line_no}: 'squad' and 'coach' are required.")
        squads.setdefault(team, {})[coach] = {"number": row.get("number"), "race": row.get("race")}
    return validate_squad_set(squads)


def squads_to_csv(squads: SquadSet) -> str:
    """Serialise a squad set to ``squad,coach,number,race`` CSV text."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["squad", "coach", "number", "race"])
    for team, coaches in squads.items():
        for coach, info in coaches.items():
            writer.writerow([team, coach, info["number"], info["race"]])
    return buffer.getvalue()


class SquadStore:
    """Named squad sets persisted as JSON, plus read-only built-in presets."""

    def __init__(self, store_dir: str | Path = "squads"):
        self.store_dir = Path(store_dir)

    # ---- listing / reading ----
    def list_sets(self) -> List[dict]:
        """All available sets as ``[{"name", "builtin", "teams"}]`` (builtins first)."""
        out = [
            {"name": name, "builtin": True, "teams": len(squads)}
            for name, squads in BUILTIN_SETS.items()
        ]
        if self.store_dir.exists():
            for path in sorted(self.store_dir.glob("*.json")):
                try:
                    squads = json.loads(path.read_text(encoding="utf-8"))
                    out.append({"name": path.stem, "builtin": False, "teams": len(squads)})
                except (json.JSONDecodeError, OSError):
                    continue
        return out

    def get_set(self, name: str) -> SquadSet:
        if name in BUILTIN_SETS:
            return BUILTIN_SETS[name]
        path = self.store_dir / f"{_safe_filename(name)}.json"
        if not path.exists():
            raise KeyError(name)
        return json.loads(path.read_text(encoding="utf-8"))

    def exists(self, name: str) -> bool:
        return name in BUILTIN_SETS or (self.store_dir / f"{_safe_filename(name)}.json").exists()

    # ---- writing ----
    def save_set(self, name: str, squads: SquadSet) -> SquadSet:
        """Validate and persist a squad set. Built-in preset names are protected."""
        if name in BUILTIN_SETS:
            raise SquadValidationError(f"{name!r} is a built-in preset and cannot be overwritten.")
        normalised = validate_squad_set(squads)
        self.store_dir.mkdir(parents=True, exist_ok=True)
        path = self.store_dir / f"{_safe_filename(name)}.json"
        path.write_text(json.dumps(normalised, indent=2), encoding="utf-8")
        return normalised

    def delete_set(self, name: str) -> None:
        if name in BUILTIN_SETS:
            raise SquadValidationError(f"{name!r} is a built-in preset and cannot be deleted.")
        path = self.store_dir / f"{_safe_filename(name)}.json"
        if not path.exists():
            raise KeyError(name)
        path.unlink()
