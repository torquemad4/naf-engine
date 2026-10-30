from datetime import date

import pandas as pd

from naf_engine.voters import voter_eligibility

ON = date(2026, 10, 30)


def _tables():
    coaches = pd.DataFrame(
        [
            (1, "Eligible", "England", "2027-01-01"),
            (2, "Lapsed", "England", "2026-10-29"),
            (3, "Welsh", "Wales", "2027-01-01"),
            (4, "OnlineOnly", "England", "2027-01-01"),
            (5, "TooLongAgo", "England", "2027-01-01"),
            (6, "NoExpiry", "England", None),
        ],
        columns=["NAF Nr", "NAF name", "Country", "Expiry Date"],
    ).set_index("NAF Nr")
    tours = pd.DataFrame(
        [
            # id, name, start, status, variant
            (10, "Recent Bowl", "2026-03-01", "APPROVED", 15),
            (11, "Online Cup", "2026-06-01", "APPROVED", 8),
            (12, "Old Bowl", "2025-10-30", "APPROVED", 15),  # exactly 12 months before: outside
            (13, "Edge Sevens", "2025-10-31", "APPROVED", 4),  # just inside
            (14, "Future Bowl", "2026-11-14", "APPROVED", 15),
            (15, "Unapproved", "2026-05-01", "NOTAPPROVED", 15),
        ],
        columns=["tournamentid", "tournamentname", "tournamentstartdate", "tournamentstatus", "naf_variantsid"],
    )
    entries = pd.DataFrame(
        [(10, 1), (10, 2), (10, 3), (11, 4), (15, 4), (14, 4), (12, 5), (10, 6), (13, 1)],
        columns=["naftournament", "nafcoach"],
    )
    return coaches, tours, entries


def check(coach_id):
    return voter_eligibility(*_tables(), coach_id, ON)


def test_eligible_coach_and_facts():
    r = check(1)
    assert r["eligible"] and r["english"] and r["member"]
    assert r["tabletop_tournaments_in_window"] == 2  # Recent Bowl and Edge Sevens
    assert r["last_tabletop_tournament"] == {"naf_id": 10, "name": "Recent Bowl", "date": "2026-03-01", "variant": "Blood Bowl 2025"}
    assert r["tabletop_window_start"] == "2025-10-30"


def test_each_reason_to_refuse():
    assert not check(2)["eligible"] and not check(2)["member"]  # membership expired the day before
    assert not check(3)["eligible"] and not check(3)["english"]
    online = check(4)  # online, unapproved and future tournaments don't count
    assert not online["eligible"] and online["tabletop_tournaments_in_window"] == 0 and online["last_tabletop_tournament"] is None
    old = check(5)  # a tournament exactly 12 months ago is outside the window, but is reported
    assert not old["eligible"] and old["last_tabletop_tournament"]["naf_id"] == 12
    assert not check(6)["member"] and check(6)["membership_expires"] is None


def test_unknown_coach():
    assert check(999) is None
