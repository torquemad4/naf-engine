# naf-engine

The core NAF data engine. It downloads the NAF stats dump and turns it into coach, squad, county and tournament data.

It has no web or UI code. Apps import it, or call a service that does.

## Who uses it

| Consumer | How |
|---|---|
| County app and scout API (`bb-county-tool`) | Imports the package. Wiring still to do: see below. |
| englandbb.co.uk community votes | Through the NAF API (`/coaches/{naf_number}/eligibility`), which calls `voters`. |
| englandbb.co.uk tournament calendar | Planned. A job reads upcoming UK tournaments from the engine and writes them to the site's database. |

## Install

The repository is private, so installing needs GitHub access (a token or SSH key).

```bash
pip install "naf-engine @ git+https://github.com/torquemad4/naf-engine@main"
```

Pin to a release tag once one exists (for example `@v0.1.0`), so a change here cannot break a deployed app unannounced.

For local development:

```bash
pip install -e ".[test]"
pytest -q
```

## Use

```python
from naf_engine.datamine import NafDatamine, GameFilter

dm = NafDatamine(data_dir=".naf_data").load(download=True)  # skips the download if today's dump is already there
dm.tour_data          # every NAF tournament (pandas DataFrame)
dm.coach_report(31866, GameFilter(scope="competitive"))
```

Modules:

- `datamine`: downloads and loads the dump; coach stats, head-to-head, timelines, tournament wins.
- `counties`: postcode to county lookup, and the Albion Wanderers patch tiers.
- `squads`: Eurobowl squad sets.
- `rosters`: NAF race ids and names.
- `tournaments`: tournament listings for calendars and maps, with coordinates.
- `voters`: Team England voter eligibility (Charter point 9): English, NAF membership not expired, and an approved non-online NAF tournament in the last 12 months. `NafDatamine.voter_eligibility(naf_number, on=date)`.

```python
from naf_engine.tournaments import list_tournaments, PostcodeGeocoder

rows = list_tournaments(dm)                     # upcoming, UK, approved; see the docstring for filters
PostcodeGeocoder(".naf_data/geocode_cache.json").add_coordinates(rows)
```

## Tournament data, as of the 23 Sep 2026 dump

- `tour_data` holds 10,379 tournaments, 435 of them upcoming; 130 of those are in England, Wales, Scotland or Northern Ireland.
- Useful fields: name, address, city, postcode (`tournamentzip`), nation, start and end dates, `naf_variantsid`, status, URL.
- The latitude and longitude fields are empty for UK events, so `PostcodeGeocoder` places them through postcodes.io (free, no key): full postcode, then outward code, then town name. It caches each result on disk. On the 24 Sep 2026 data it placed all 129 upcoming UK events.
- Variant names come from `VARIANT_NAMES`, read from the NAF tournament pages (for example 15 is "Blood Bowl 2025", 4 is "Sevens").
- The NAF tournament page is `https://member.thenaf.net/index.php?module=NAF&type=tournaments&func=view&id=<tournamentid>`.

## Origin

Extracted from `torquemad4/bb-county-tool` (`core/naf_scout`, commit `ebd013a`, 9 Aug 2026). The package was renamed from `naf_scout` to `naf_engine`; the code is unchanged.

## Still to do

1. Switch `bb-county-tool` to install this package instead of its own `core/` copy, and change its imports from `naf_scout` to `naf_engine`. Its Azure build will need read access to this private repository.
2. Serve `tournaments` from the county app API (`/tournaments`), so englandbb.co.uk can sync it daily.
