# naf-engine

The core NAF data engine. It downloads the NAF stats dump and turns it into coach, squad, county and tournament data.

It has no web or UI code. Apps import it, or call a service that does.

## Who uses it

| Consumer | How |
|---|---|
| County app and scout API (`bb-county-tool`) | Imports the package. Wiring still to do: see below. |
| englandbb.co.uk tournament calendar | Planned. A job reads upcoming UK tournaments from the engine and writes them to the site's database. |

## Install

The repository is private, so installing needs GitHub access (a token or SSH key).

```bash
pip install "naf-engine @ git+https://github.com/torquemad4/naf-engine@v0.1.0"
```

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

## Tournament data, as of the 23 Sep 2026 dump

- `tour_data` holds 10,379 tournaments, 435 of them upcoming; 130 of those are in England, Wales, Scotland or Northern Ireland.
- Useful fields: name, address, city, postcode (`tournamentzip`), nation, start and end dates, `naf_variantsid`, status, URL.
- The latitude and longitude fields are empty for UK events. The map will need coordinates from the postcode.
- The NAF tournament page is `https://member.thenaf.net/index.php?module=NAF&type=tournaments&func=view&id=<tournamentid>`.

## Origin

Extracted from `torquemad4/bb-county-tool` (`core/naf_scout`, commit `ebd013a`, 9 Aug 2026). The package was renamed from `naf_scout` to `naf_engine`; the code is unchanged.

## Still to do

1. Switch `bb-county-tool` to install this package instead of its own `core/` copy, and change its imports from `naf_scout` to `naf_engine`. Its Azure build will need read access to this private repository.
2. Add a tournaments module: upcoming tournaments by nation and date, with coordinates from the postcode.
3. A scheduled job that feeds those tournaments into the englandbb.co.uk database.
