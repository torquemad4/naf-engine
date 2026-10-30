"""Who may vote in Team England community votes (Team England Charter, point 9).

"In order to vote in any of these elections, voters must be English NAF members and must
have played a tabletop NAF tournament in the 12 months prior to voting."

Read here as:
- English: the coach's NAF country is England.
- NAF member: the NAF membership expiry date is on or after the voting date.
- Tabletop NAF tournament: an approved NAF tournament that isn't the Online variant
  (variant 8), starting in the 12 months up to and including the voting date, with the
  coach on its coach list.

Nothing here downloads the NAF dump: pass in a loaded ``NafDatamine`` or its tables.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from .tournaments import variant_name

ONLINE_VARIANT = 8
VOTER_NATION = "England"


def _iso(value) -> Optional[str]:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def voter_eligibility(
    coaches: pd.DataFrame,
    tour_data: pd.DataFrame,
    tour_coach_data: pd.DataFrame,
    coach_id: int,
    on: date,
) -> Optional[dict]:
    """Charter point 9 eligibility for one coach on a date, with the facts behind it.

    ``coaches`` is the CoachExport table indexed by "NAF Nr". Returns None for an
    unknown NAF number.
    """
    try:
        coach = coaches.loc[coach_id]
    except KeyError:
        return None
    if isinstance(coach, pd.DataFrame):  # duplicate NAF numbers: take the first
        coach = coach.iloc[0]

    country = coach.get("Country")
    country = None if country is None or (isinstance(country, float) and pd.isna(country)) else str(country).strip() or None
    expires = _iso(coach.get("Expiry Date"))
    english = country == VOTER_NATION
    member = expires is not None and expires >= on.isoformat()

    window_start = (pd.Timestamp(on) - pd.DateOffset(months=12)).date().isoformat()
    entered = tour_coach_data.loc[tour_coach_data["nafcoach"] == coach_id, "naftournament"]
    tours = tour_data[
        tour_data["tournamentid"].isin(entered)
        & (tour_data["tournamentstatus"] == "APPROVED")
        & (pd.to_numeric(tour_data["naf_variantsid"], errors="coerce") != ONLINE_VARIANT)
    ]
    tours = tours.assign(_start=tours["tournamentstartdate"].astype(str).str.slice(0, 10))
    played = tours[tours["_start"] <= on.isoformat()].sort_values("_start")
    recent = played[played["_start"] > window_start]

    def describe(row) -> dict:
        return {
            "naf_id": int(row["tournamentid"]),
            "name": str(row["tournamentname"]).strip(),
            "date": row["_start"],
            "variant": variant_name(row["naf_variantsid"]),
        }

    last = describe(played.iloc[-1]) if len(played) else None
    return {
        "naf_number": int(coach_id),
        "name": str(coach.get("NAF name")),
        "country": country,
        "membership_expires": expires,
        "on": on.isoformat(),
        "english": english,
        "member": member,
        "tabletop_window_start": window_start,
        "tabletop_tournaments_in_window": int(len(recent)),
        "last_tabletop_tournament": last,
        "eligible": bool(english and member and len(recent) > 0),
    }
