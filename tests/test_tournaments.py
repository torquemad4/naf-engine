import pandas as pd
import requests

from naf_engine.tournaments import (
    PostcodeGeocoder,
    list_tournaments,
    normalise_postcode,
    variant_name,
)


def _table():
    return pd.DataFrame(
        [
            # id, name, start, end, nation, status, variant, zip, url
            (1, "Past Bowl", "2026-01-10", "2026-01-11", "England", "APPROVED", 15, "LS28 5LY", None),
            (2, "  Fail\tBowl 3 ", "2026-10-10", None, "England", "APPROVED", 15, "ls285ly", "talkfantasyfootball.org/x"),
            (3, "Dragon Cup", "2026-11-07", "2026-11-08", "Wales", "APPROVED", 4, "CF11 8AB", "https://example.org"),
            (4, "Pending Cup", "2026-11-14", "2026-11-14", "England", "NOTAPPROVED", 15, "PO2 9DD", None),
            (5, "Euro Open", "2026-12-01", "2026-12-02", "Spain", "APPROVED", 15, None, None),
            (6, "Mystery", "2027-01-05", "2027-01-05", "Scotland", "APPROVED", 0, "not a postcode", "see facebook"),
            (7, "Ongoing", "2026-09-20", "2026-09-30", "England", "APPROVED", 13, "PR5 0AG", None),
        ],
        columns=[
            "tournamentid", "tournamentname", "tournamentstartdate", "tournamentenddate",
            "tournamentnation", "tournamentstatus", "naf_variantsid", "tournamentzip", "tournamenturl",
        ],
    )


def test_variant_names():
    assert variant_name(15) == "Blood Bowl 2025"
    assert variant_name(4) == "Sevens"
    assert variant_name(0) == "Not stated"
    assert variant_name(None) == "Not stated"


def test_normalise_postcode():
    assert normalise_postcode("ls285ly") == "LS28 5LY"
    assert normalise_postcode(" pr5 0ag ") == "PR5 0AG"
    assert normalise_postcode("CF11") == "CF11"
    assert normalise_postcode("not a postcode") is None
    assert normalise_postcode(float("nan")) is None


def test_list_tournaments_filters_and_cleans():
    rows = list_tournaments(_table(), date_from="2026-09-24")
    ids = [r["naf_id"] for r in rows]
    # past, unapproved and non-UK are out; the event under way is in
    assert ids == [7, 2, 3, 6]
    fail = rows[1]
    assert fail["name"] == "Fail Bowl 3"
    assert fail["end_date"] == "2026-10-10"  # missing end date falls back to start
    assert fail["postcode"] == "LS28 5LY"
    assert fail["website"] == "https://talkfantasyfootball.org/x"
    assert fail["naf_url"].endswith("id=2")
    assert rows[3]["variant"] == "Not stated" and rows[3]["website"] is None


def test_list_tournaments_date_window_and_all_nations():
    rows = list_tournaments(_table(), nations=None, date_from="2026-11-01", date_to="2026-12-31")
    assert [r["naf_id"] for r in rows] == [3, 5]


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class _FakeSession:
    def __init__(self):
        self.posts = 0
        self.gets = []

    def post(self, url, json, timeout):
        self.posts += 1
        results = []
        for q in json["postcodes"]:
            known = {"LS28 5LY": (53.8, -1.66)}
            res = {"latitude": known[q][0], "longitude": known[q][1]} if q in known else None
            results.append({"query": q, "result": res})
        return _Resp(200, {"result": results})

    def get(self, url, timeout, params=None):
        self.gets.append(url)
        if url.endswith("/places") and params["q"] == "Folkestone":
            return _Resp(200, {"result": [{"latitude": 51.08, "longitude": 1.17}]})
        if url.endswith("/places"):
            return _Resp(200, {"result": []})
        if url.endswith("/outcodes/PR5"):
            return _Resp(200, {"result": {"latitude": 53.74, "longitude": -2.64}})
        return _Resp(404, {"error": "not found"})


def test_geocoder_bulk_fallback_and_cache(tmp_path):
    session = _FakeSession()
    geo = PostcodeGeocoder(tmp_path / "geo.json", session=session)
    rows = geo.add_coordinates(
        [
            {"postcode": "LS28 5LY"},
            {"postcode": "PR5 0AG"},
            {"postcode": "ZZ9 9ZZ"},
            {"postcode": None, "town": "Folkestone."},
            {"postcode": None, "town": "303 Nowhere Road"},
        ]
    )
    assert rows[0]["lat"] == 53.8
    assert rows[1]["lat"] == 53.74  # fell back to the outward code
    assert rows[2].get("lat") is None
    assert rows[3]["lat"] == 51.08  # no postcode: placed by town
    assert rows[4].get("lat") is None
    assert session.posts == 1

    # Second instance reads the cache: the placed postcodes need no network.
    session2 = _FakeSession()
    geo2 = PostcodeGeocoder(tmp_path / "geo.json", session=session2)
    assert geo2.lookup(["LS28 5LY", "PR5 0AG"]) == {"LS28 5LY": [53.8, -1.66], "PR5 0AG": [53.74, -2.64]}
    assert session2.posts == 0 and session2.gets == []


def test_geocoder_survives_network_failure(tmp_path):
    class Down:
        def post(self, *a, **k):
            raise requests.ConnectionError("down")

        def get(self, *a, **k):
            raise requests.ConnectionError("down")

    geo = PostcodeGeocoder(tmp_path / "geo.json", session=Down())
    assert geo.lookup(["LS28 5LY"]) == {}
