"""naf_engine - the core NAF data engine.

Downloads the NAF stats dump and turns it into coach, squad, county and
tournament data. Framework-agnostic: no web or UI dependencies live here.

Consumers import this package directly (the county app / scout API) or call a
service that does. Nothing in this package knows who is calling it.

Extracted from torquemad4/bb-county-tool (core/naf_scout, commit ebd013a).
"""

__version__ = "0.2.0"
