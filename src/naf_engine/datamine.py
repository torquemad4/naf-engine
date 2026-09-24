"""Core NAF data engine.

Migrated and tidied from the V1 repo-root ``naf_datamine.py``. Responsibilities:

* acquire the NAF stats dump (download + extract into a configurable data dir),
* load the CSVs into pandas,
* compute per-coach and per-squad statistics under a configurable filter.

Design notes:

* Data location is an explicit ``data_dir`` (constructor arg) - nothing is written
  next to the source.
* Queries are parameterised by a :class:`GameFilter` (games scope, date range,
  established-opponent ELO threshold) rather than hardcoded constants.
* Each coach's *entire* annotated game history (over the main ruleset lineage) is
  built and cached once; scope/date/threshold are then cheap in-memory slices, so
  squad-wide ranking stays fast even with custom filters.
* ELO figures use the NAF "Regular" rating (variant 16), which is the combined
  competitive rating spanning BB2020 + BB2025 - both for a coach's own Max/Race
  ELO and for judging whether an opponent is "established".
* Statistic methods return raw numbers via the ``Tally`` dataclass; formatting is
  a frontend concern.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from .rosters import RACE_IDS, RACE_NAMES

NAF_URL = "https://member.thenaf.net/glicko/nafstat-tmp-name.zip"

# Dump folders are named e.g. "naf-stat-dump-2025-11-14-0914"; the date part is
# what the NAF publishes daily, and what we treat as the data's freshness stamp.
_DUMP_DATE_RE = re.compile(r"naf-stat-dump-(\d{4}-\d{2}-\d{2})")

# NAF ruleset variant ids (naf_variants.csv).
VARIANT_CLASSIC = 1  # pre-BB2020
VARIANT_BB2020 = 13
VARIANT_BB2025 = 15
# The "Regular" ranking (variant 16) is a rankings-only aggregate combining
# BB2020 + BB2025 - the competitive ELO the NAF publishes. It is not a playable
# ruleset, so it never appears in the games table; used for ELO figures only.
VARIANT_REGULAR_ELO = 16

# The main ruleset lineage we ever score games over (Classic -> 2020 -> 2025).
# A coach's cached history is restricted to these, since every scope is a subset.
LINEAGE_VARIANTS = (VARIANT_CLASSIC, VARIANT_BB2020, VARIANT_BB2025)

# Named, cumulative games-scope presets: key -> (label, variant ids).
GAMES_SCOPES = {
    "2025": ("Blood Bowl 2025 only", (VARIANT_BB2025,)),
    "competitive": ("2025 + 2020", (VARIANT_BB2020, VARIANT_BB2025)),
    "all": ("All-time (incl. Classic)", LINEAGE_VARIANTS),
}
DEFAULT_SCOPE = "competitive"

# Default "established opponent" ELO threshold (arbitrary, now user-configurable).
DEFAULT_ESTABLISHED_ELO = 200.0

# Stats that get_coach_stat / squad rankings can produce.
AVAILABLE_STATS = [
    "Winrate",
    "Winrate vs established",
    "Race ELO",
    "Max ELO",
    "No Loss",
    "No Loss vs established",
]

# Fallback ELO for a coach with no ranked games on a race.
DEFAULT_ELO = 150

# Default opponent-ELO band edges for the individual drilldown cross-tab.
DEFAULT_ELO_BANDS = (175, 200, 225)

_DUMP_FILES = {
    "game": "naf_game.csv",
    "race": "naf_race.csv",
    "tournament": "naf_tournament.csv",
    "tournament_coach": "naf_tournamentcoach.csv",
    "coach_export": "CoachExport.csv",
    "coach_ranking": "naf_coachranking_variant.csv",
    "tournament_stats": "naf_tournament_statistics_group.csv",
}

# typeID in naf_tournament_statistics_group marking the tournament winner.
WINNER_STAT_TYPE = 1


@dataclass(frozen=True)
class GameFilter:
    """Which games count, and what "established" means, for a query.

    * ``scope``: a key into :data:`GAMES_SCOPES` (which rulesets to include).
    * ``date_from`` / ``date_to``: inclusive ISO date bounds (``None`` = unbounded).
    * ``established_elo``: an opponent counts as "established" once their peak
      Regular rating reaches this.
    * ``established_only``: when True, games against non-established opponents are
      dropped outright, so *every* query honours the threshold instead of only the
      reports that carry an explicit "vs established" split. Off by default, which
      preserves the historic behaviour (the threshold is a split, not a filter).

    With ``established_only`` on, a report's ``overall`` and ``vs_established``
    tallies are by definition identical.
    """

    scope: str = DEFAULT_SCOPE
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    established_elo: float = DEFAULT_ESTABLISHED_ELO
    established_only: bool = False

    @property
    def variants(self) -> Tuple[int, ...]:
        try:
            return GAMES_SCOPES[self.scope][1]
        except KeyError:
            raise ValueError(f"Unknown scope: {self.scope!r}. Known: {list(GAMES_SCOPES)}")

    def describe(self) -> dict:
        return {
            "scope": self.scope,
            "variants": list(self.variants),
            "date_from": self.date_from,
            "date_to": self.date_to,
            "established_elo": self.established_elo,
            "established_only": self.established_only,
        }


@dataclass(frozen=True)
class Tally:
    """A win/draw/loss record and the rates derived from it."""

    games: int
    wins: int
    draws: int
    losses: int

    @property
    def winrate(self) -> float:
        """Percentage, counting a draw as half a win. 0.0 when no games."""
        if not self.games:
            return 0.0
        return round((self.wins + self.draws * 0.5) / self.games * 100, 2)

    @property
    def no_loss_rate(self) -> float:
        """Percentage of games not lost (win or draw). 0.0 when no games."""
        if not self.games:
            return 0.0
        return round((self.wins + self.draws) / self.games * 100, 2)

    def to_dict(self) -> dict:
        return {
            "games": self.games,
            "wins": self.wins,
            "draws": self.draws,
            "losses": self.losses,
            "winrate": self.winrate,
            "no_loss_rate": self.no_loss_rate,
        }


class DataNotLoaded(RuntimeError):
    """Raised when a query is attempted before load() has succeeded."""


class NafDatamine:
    def __init__(self, data_dir: str | Path = ".naf_data"):
        self.data_dir = Path(data_dir)
        self.zip_path = self.data_dir / "nafstat-tmp-name.zip"
        self.extract_dir = self.data_dir / "extracted"
        self._loaded = False
        self._coach_games_cache: dict[int, pd.DataFrame] = {}

    # ------------------------------------------------------------------ #
    # Data acquisition & loading
    # ------------------------------------------------------------------ #
    def download_latest(self) -> Path:
        """Download the current NAF dump into the data dir. Returns the zip path."""
        import requests  # local import: only needed when actually downloading

        self.data_dir.mkdir(parents=True, exist_ok=True)
        with requests.get(NAF_URL, stream=True, headers={"user-agent": "naf-scout"}) as response:
            response.raise_for_status()
            with open(self.zip_path, "wb") as handle:
                for chunk in response.iter_content(chunk_size=8192):
                    handle.write(chunk)
        return self.zip_path

    @staticmethod
    def _dump_date(name: str) -> Optional[date]:
        """Parse the date out of a ``naf-stat-dump-YYYY-MM-DD-...`` name."""
        match = _DUMP_DATE_RE.search(name)
        if not match:
            return None
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()

    def local_dump_date(self) -> Optional[date]:
        """Date of the newest dump we have locally (extracted or still zipped)."""
        dates = []
        if self.extract_dir.exists():
            dates = [
                d
                for d in (self._dump_date(p.name) for p in self.extract_dir.iterdir() if p.is_dir())
                if d is not None
            ]
        if not dates and self.zip_path.exists():
            with zipfile.ZipFile(self.zip_path) as archive:
                tops = {name.split("/")[0] for name in archive.namelist()}
            dates = [d for d in (self._dump_date(t) for t in tops) if d is not None]
        return max(dates) if dates else None

    def is_current(self) -> bool:
        """True if our latest local dump is today's (or newer)."""
        latest = self.local_dump_date()
        return latest is not None and latest >= date.today()

    def ensure_current(self, force: bool = False) -> bool:
        """Download a fresh dump only if a newer one is available on the server.

        The NAF database publishes once a day, so there's no point re-downloading
        when the server has nothing newer than we hold. Returns True if a download
        happened. Falls back to downloading if we can't determine remote freshness.
        """
        if force or self.update_available() is not False:
            self.download_latest()
            return True
        return False

    def data_age_days(self) -> Optional[int]:
        """How many days old our newest local dump is (None if we have no data)."""
        local = self.local_dump_date()
        return (date.today() - local).days if local is not None else None

    def remote_pack_date(self, timeout: float = 30.0) -> Optional[date]:
        """Date of the latest pack on the NAF server, via a HEAD request.

        Reads ``Last-Modified`` without downloading the body. Returns None if the
        server can't be reached or doesn't report a modification time.
        """
        import requests  # local import: only needed for the network check
        from email.utils import parsedate_to_datetime

        try:
            response = requests.head(
                NAF_URL, headers={"user-agent": "naf-scout"}, allow_redirects=True, timeout=timeout
            )
            response.raise_for_status()
            last_modified = response.headers.get("Last-Modified")
            if not last_modified:
                return None
            return parsedate_to_datetime(last_modified).date()
        except Exception:  # noqa: BLE001 - the check is best-effort
            return None

    def update_available(self) -> Optional[bool]:
        """Whether the server has a pack newer than our local one.

        Returns True/False when determinable, or None if the server is
        unreachable or we have no local data to compare against.
        """
        remote = self.remote_pack_date()
        local = self.local_dump_date()
        if remote is None or local is None:
            return None
        return remote > local

    def _latest_dump_dir(self) -> Path:
        """The newest ``naf-stat-dump-*`` folder under the extraction dir."""
        dumps = sorted(p for p in self.extract_dir.iterdir() if p.is_dir())
        if not dumps:
            raise DataNotLoaded(f"No NAF dump folders found under {self.extract_dir}")
        return dumps[-1]

    def load(self, download: bool = False, refresh: bool = False) -> "NafDatamine":
        """Extract the dump and load the CSVs into memory.

        ``download=True`` always fetches the latest dump first. ``refresh=True``
        fetches only when the local dump isn't today's (see ``ensure_current``).
        """
        if download:
            self.download_latest()
        elif refresh:
            self.ensure_current()
        if not self.zip_path.exists():
            raise FileNotFoundError(
                f"No NAF data zip at {self.zip_path}. "
                f"Call load(download=True), or place the dump zip there."
            )

        self.extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(self.zip_path, "r") as archive:
            archive.extractall(self.extract_dir)

        dump = self._latest_dump_dir()

        def read(key: str) -> pd.DataFrame:
            return pd.read_csv(dump / _DUMP_FILES[key], sep=";")

        self.game_data = read("game")
        self.race_data = read("race")
        self.tour_data = read("tournament")
        self.tour_coach_data = read("tournament_coach")
        self.coach_data = read("coach_export")
        self.coach_rankings = read("coach_ranking")
        self.tournament_stats = read("tournament_stats")

        self.coach_data_indexed = self.coach_data.set_index("NAF Nr")
        self.tour_data_indexed = self.tour_data.set_index("tournamentid").sort_values(
            by=["tournamentstartdate"]
        )
        self.race_data_indexed = self.race_data.set_index("raceid")
        self.coach_data_by_name = self.coach_data.set_index("NAF name")

        # Precompute peak Regular-rating lookups so per-opponent queries are an
        # O(1) dict hit rather than repeated full-frame scans.
        regular = self.coach_rankings[self.coach_rankings["variantID"] == VARIANT_REGULAR_ELO]
        self._max_ranking_by_coach = regular.groupby("coachID")["ranking"].max()
        self._race_ranking = regular.groupby(["coachID", "raceID"])["ranking"].max()

        # Attendance per tournament (number of coaches) for tournament-win detail.
        self._attendance = self.tour_coach_data.groupby("naftournament").size()

        self._coach_games_cache.clear()
        self._loaded = True
        return self

    @property
    def loaded(self) -> bool:
        return self._loaded

    def _require_loaded(self) -> None:
        if not self._loaded:
            raise DataNotLoaded("NAF data not loaded; call load() first.")

    # ------------------------------------------------------------------ #
    # Ranking lookups (all on the Regular / competitive rating)
    # ------------------------------------------------------------------ #
    def coach_max_ranking(self, coach_id: int) -> float:
        """Peak Regular rating across all races (NaN if none)."""
        self._require_loaded()
        try:
            return float(self._max_ranking_by_coach.loc[coach_id])
        except KeyError:
            return float("nan")

    def coach_race_ranking(self, coach_id: int, race_id: int) -> float:
        """Peak Regular rating with a specific race (NaN if none)."""
        self._require_loaded()
        try:
            return float(self._race_ranking.loc[(coach_id, race_id)])
        except KeyError:
            return float("nan")

    def coach_name(self, coach_id: int) -> str:
        """Display name for a coach id, or ``#<id>`` if not in the export."""
        self._require_loaded()
        try:
            return str(self.coach_data_indexed.loc[coach_id]["NAF name"])
        except KeyError:
            return f"#{coach_id}"

    # ------------------------------------------------------------------ #
    # Per-coach games & tallies
    # ------------------------------------------------------------------ #
    def _all_games(self, coach_id: int) -> pd.DataFrame:
        """A coach's entire annotated history over the ruleset lineage.

        Cached per coach for the life of the process (the data is immutable), so
        that scope/date/threshold filtering downstream is a cheap in-memory slice.
        """
        self._require_loaded()
        if coach_id in self._coach_games_cache:
            return self._coach_games_cache[coach_id]

        played = self.game_data[
            (self.game_data["homecoachid"] == coach_id)
            | (self.game_data["awaycoachid"] == coach_id)
        ]
        played = played[played["naf_variantsid"].isin(LINEAGE_VARIANTS)]
        tour_lookup = self.tour_data[
            ["tournamentid", "tournamentname", "tournamentstartdate"]
        ].set_index("tournamentid")
        games = played.join(tour_lookup, on="tournamentid").sort_values(by=["tournamentstartdate"])

        is_home = games["homecoachid"] == coach_id
        result_conditions = [
            (games["goalshome"] == games["goalsaway"]),
            ((games["goalshome"] > games["goalsaway"]) & is_home),
            ((games["goalshome"] < games["goalsaway"]) & is_home),
            ((games["goalshome"] > games["goalsaway"]) & ~is_home),
            ((games["goalshome"] < games["goalsaway"]) & ~is_home),
        ]
        result_values = ["Draw", "Win", "Loss", "Loss", "Win"]
        side_conditions = [is_home, ~is_home]

        games = games.copy()
        games["result"] = np.select(result_conditions, result_values, default="")
        games["opponentid"] = np.select(
            side_conditions, [games["awaycoachid"], games["homecoachid"]]
        )
        games["opponentrace"] = np.select(
            side_conditions, [games["raceaway"], games["racehome"]]
        )
        games["coachrace"] = np.select(
            side_conditions, [games["racehome"], games["raceaway"]]
        )
        games["opponentmaxranking"] = games["opponentid"].map(self.coach_max_ranking)

        self._coach_games_cache[coach_id] = games
        return games

    def games_for(self, coach_id: int, filt: Optional[GameFilter] = None) -> pd.DataFrame:
        """A coach's games under a filter (scope, date range, established-only).

        This is the single choke point every query goes through, so applying
        ``established_only`` here is what makes the ELO threshold bite on the
        timeline, matchup grid and breakdown - not just on reports that happen to
        compute a separate "vs established" split.
        """
        filt = filt or GameFilter()
        games = self._all_games(coach_id)
        games = games[games["naf_variantsid"].isin(filt.variants)]
        if filt.date_from:
            games = games[games["date"] >= filt.date_from]
        if filt.date_to:
            games = games[games["date"] <= filt.date_to]
        if filt.established_only:
            games = self._established(games, filt)
        return games

    @staticmethod
    def _established(games: pd.DataFrame, filt: GameFilter) -> pd.DataFrame:
        return games[games["opponentmaxranking"] >= filt.established_elo]

    @staticmethod
    def tally(games: pd.DataFrame) -> Tally:
        """Count wins/draws/losses in an annotated games frame."""
        counts = games["result"].value_counts()
        wins = int(counts.get("Win", 0))
        draws = int(counts.get("Draw", 0))
        losses = int(counts.get("Loss", 0))
        return Tally(games=len(games), wins=wins, draws=draws, losses=losses)

    # ------------------------------------------------------------------ #
    # Reports & stats
    # ------------------------------------------------------------------ #
    def coach_report(
        self,
        coach_id: int,
        filt: Optional[GameFilter] = None,
        race_id: int | None = None,
        opponent_races: list[int] | None = None,
    ) -> dict:
        """Raw-number stat report for a coach under a filter.

        ``race_id`` (optional) adds a "with this race" breakdown; ``opponent_races``
        (optional) adds per-opponent-race breakdowns.
        """
        filt = filt or GameFilter()
        games = self.games_for(coach_id, filt)
        established = self._established(games, filt)

        max_elo = self.coach_max_ranking(coach_id)
        report: dict = {
            "coach_id": coach_id,
            "coach_name": self.coach_name(coach_id),
            "max_elo": None if np.isnan(max_elo) else round(max_elo, 1),
            "filter": filt.describe(),
            "overall": self.tally(games).to_dict(),
            "vs_established": self.tally(established).to_dict(),
        }

        if race_id is not None:
            with_race = games[games["coachrace"] == race_id]
            with_race_established = established[established["coachrace"] == race_id]
            race_elo = self.coach_race_ranking(coach_id, race_id)
            report["race_id"] = race_id
            report["race_elo"] = None if np.isnan(race_elo) else round(race_elo, 1)
            report["with_race"] = self.tally(with_race).to_dict()
            report["with_race_vs_established"] = self.tally(with_race_established).to_dict()

        if opponent_races:
            vs_races = {}
            for opp in opponent_races:
                vs = games[games["opponentrace"] == opp]
                vs_established = established[established["opponentrace"] == opp]
                vs_races[opp] = {
                    "overall": self.tally(vs).to_dict(),
                    "vs_established": self.tally(vs_established).to_dict(),
                }
            report["vs_races"] = vs_races

        return report

    def get_coach_stat(
        self,
        coach_id: int,
        race_name: str,
        stats: list[str],
        filt: Optional[GameFilter] = None,
    ) -> dict:
        """Compute a set of named stats for one coach (raw numbers) under a filter."""
        filt = filt or GameFilter()
        race_id = RACE_IDS[race_name]
        games = self.games_for(coach_id, filt)
        established = self._established(games, filt)

        values: dict[str, float] = {}
        for stat in stats:
            if stat == "Winrate":
                values[stat] = self.tally(games).winrate
            elif stat == "Winrate vs established":
                values[stat] = self.tally(established).winrate
            elif stat == "No Loss":
                values[stat] = self.tally(games).no_loss_rate
            elif stat == "No Loss vs established":
                values[stat] = self.tally(established).no_loss_rate
            elif stat == "Race ELO":
                elo = self.coach_race_ranking(coach_id, race_id)
                values[stat] = DEFAULT_ELO if np.isnan(elo) else elo
            elif stat == "Max ELO":
                elo = self.coach_max_ranking(coach_id)
                values[stat] = DEFAULT_ELO if np.isnan(elo) else elo
            else:
                raise ValueError(f"Unknown stat: {stat!r}. Known: {AVAILABLE_STATS}")
        return values

    # ------------------------------------------------------------------ #
    # Individual drilldown: opponent-race x opponent-ELO cross-tab
    # ------------------------------------------------------------------ #
    @staticmethod
    def _band_labels(edges) -> list:
        e = list(edges)
        labels = [f"<{e[0]}"]
        labels += [f"{a}-{b}" for a, b in zip(e, e[1:])]
        labels.append(f"{e[-1]}+")
        return labels

    def coach_breakdown(
        self,
        coach_id: int,
        filt: Optional[GameFilter] = None,
        band_edges=DEFAULT_ELO_BANDS,
        coach_race: Optional[int] = None,
    ) -> dict:
        """Cross-tabulate a coach's games by opponent race and opponent-ELO band.

        Opponent ELO is their peak Regular rating; unrated opponents fall in the
        lowest band. If ``coach_race`` is given, only that race's games are used.
        Returns per-cell tallies plus race/band marginals.
        """
        filt = filt or GameFilter()
        games = self.games_for(coach_id, filt).copy()
        if coach_race is not None:
            games = games[games["coachrace"] == coach_race]

        labels = self._band_labels(band_edges)
        bins = [-float("inf"), *band_edges, float("inf")]
        elo = games["opponentmaxranking"].fillna(-float("inf"))
        games["elo_band"] = pd.cut(elo, bins=bins, labels=labels, right=False)
        games["opp_race_name"] = games["opponentrace"].map(lambda r: RACE_NAMES.get(r, str(r)))

        def tally(sub) -> dict:
            return self.tally(sub).to_dict()

        by_race_and_band: dict = {}
        for race_name, race_games in games.groupby("opp_race_name"):
            cells = {}
            for band in labels:
                sub = race_games[race_games["elo_band"] == band]
                if len(sub):
                    cells[band] = tally(sub)
            by_race_and_band[race_name] = cells

        by_race = {name: tally(g) for name, g in games.groupby("opp_race_name")}
        by_band = {
            band: tally(games[games["elo_band"] == band])
            for band in labels
            if len(games[games["elo_band"] == band])
        }

        return {
            "coach_id": coach_id,
            "coach_name": self.coach_name(coach_id),
            "filter": filt.describe(),
            "bands": labels,
            "by_race_and_band": by_race_and_band,
            "by_race": by_race,
            "by_band": by_band,
            "overall": tally(games),
        }

    def search_coaches(self, query: str, limit: int = 20) -> list:
        """Find coaches whose NAF name contains ``query`` (case-insensitive)."""
        self._require_loaded()
        q = query.strip().lower()
        if not q:
            return []
        names = self.coach_data[["NAF Nr", "NAF name"]].dropna()
        mask = names["NAF name"].astype(str).str.lower().str.contains(q, regex=False)
        hits = names[mask].head(limit)
        return [{"id": int(row["NAF Nr"]), "name": str(row["NAF name"])} for _, row in hits.iterrows()]

    # ------------------------------------------------------------------ #
    # Individual career analytics
    # ------------------------------------------------------------------ #
    def coach_timeline(
        self, coach_id: int, filt: Optional[GameFilter] = None, race_id: int | None = None
    ) -> dict:
        """Per-tournament timeline: cumulative winrate and average opponent ELO.

        Games are grouped by tournament in chronological order. ``avg_opponent_elo``
        is the mean of opponents' *current* peak Regular rating (a strength-of-
        schedule proxy - not their rating on the day).
        """
        filt = filt or GameFilter()
        games = self.games_for(coach_id, filt)
        if race_id is not None:
            games = games[games["coachrace"] == race_id]

        points = []
        cum_w = cum_d = cum_l = 0
        # _all_games is already sorted by tournamentstartdate; sort=False keeps that order.
        for (tname, tdate), g in games.groupby(
            ["tournamentname", "tournamentstartdate"], sort=False
        ):
            t = self.tally(g)
            cum_w += t.wins
            cum_d += t.draws
            cum_l += t.losses
            cum_games = cum_w + cum_d + cum_l
            cum_wr = round((cum_w + cum_d * 0.5) / cum_games * 100, 2) if cum_games else 0.0
            opp_elo = g["opponentmaxranking"].dropna()
            points.append(
                {
                    "date": str(tdate)[:10],
                    "tournament": tname,
                    "games": t.games,
                    "winrate": t.winrate,
                    "cum_games": cum_games,
                    "cum_winrate": cum_wr,
                    "cum_wins": cum_w,
                    "cum_draws": cum_d,
                    "cum_losses": cum_l,
                    "avg_opponent_elo": round(float(opp_elo.mean()), 1) if len(opp_elo) else None,
                }
            )
        return {"coach_id": coach_id, "coach_name": self.coach_name(coach_id), "points": points}

    def head_to_head(
        self, coach_id: int, opponent_id: int, filt: Optional[GameFilter] = None
    ) -> dict:
        """Every game ``coach_id`` has played against ``opponent_id``, from
        ``coach_id``'s point of view.

        The tally's wins/draws/losses are the FIRST coach's. The second coach's
        record is the mirror image, so the caller doesn't need a second query.
        Honours the same filter as every other query, established-only included.
        """
        filt = filt or GameFilter()
        games = self.games_for(coach_id, filt)
        games = games[games["opponentid"] == opponent_id]

        def _score(value):
            # Forfeits and unrecorded games can carry a null score; int(NaN) raises.
            return None if pd.isna(value) else int(value)

        meetings = []
        for _, g in games.iterrows():
            is_home = g["homecoachid"] == coach_id
            meetings.append(
                {
                    "date": str(g["tournamentstartdate"])[:10],
                    "tournament": str(g["tournamentname"]),
                    "race": RACE_NAMES.get(int(g["coachrace"]), str(g["coachrace"])),
                    "opponent_race": RACE_NAMES.get(
                        int(g["opponentrace"]), str(g["opponentrace"])
                    ),
                    "result": str(g["result"]),
                    "score_for": _score(g["goalshome"] if is_home else g["goalsaway"]),
                    "score_against": _score(g["goalsaway"] if is_home else g["goalshome"]),
                }
            )

        return {
            "coach_id": coach_id,
            "coach_name": self.coach_name(coach_id),
            "opponent_id": opponent_id,
            "opponent_name": self.coach_name(opponent_id),
            "filter": filt.describe(),
            "record": self.tally(games).to_dict(),
            "meetings": meetings,
        }

    def current_peak_elo_by_race(self, coach_id: int) -> list:
        """Current peak Regular ELO for each race the coach is rated in (high to low)."""
        self._require_loaded()
        try:
            per_race = self._race_ranking.loc[coach_id]
        except KeyError:
            return []
        rows = [
            {"race": RACE_NAMES.get(int(rid), str(rid)), "elo": round(float(val), 1)}
            for rid, val in per_race.items()
        ]
        rows.sort(key=lambda r: r["elo"], reverse=True)
        return rows

    def tournament_wins(self, coach_id: int) -> list:
        """Tournaments the coach won, each with attendee count and avg attendee peak ELO."""
        self._require_loaded()
        wins = self.tournament_stats[
            (self.tournament_stats["typeID"] == WINNER_STAT_TYPE)
            & (self.tournament_stats["coachID"] == coach_id)
        ]
        results = []
        for _, win in wins.iterrows():
            tid = win["tournamentID"]
            attendees = int(self._attendance.get(tid, 0))
            attendee_ids = self.tour_coach_data.loc[
                self.tour_coach_data["naftournament"] == tid, "nafcoach"
            ]
            elos = attendee_ids.map(self._max_ranking_by_coach).dropna()
            avg_elo = round(float(elos.mean()), 1) if len(elos) else None
            try:
                trow = self.tour_data_indexed.loc[tid]
                name = str(trow["tournamentname"])
                tdate = str(trow["tournamentstartdate"])[:10]
            except KeyError:
                name = f"#{tid}"
                tdate = str(win.get("date", ""))[:10]
            race_id = win["raceID"]
            results.append(
                {
                    "tournament": name,
                    "date": tdate,
                    "race": RACE_NAMES.get(int(race_id), str(race_id)) if pd.notna(race_id) else None,
                    "attendees": attendees,
                    "avg_peak_elo": avg_elo,
                }
            )
        results.sort(key=lambda r: r["date"], reverse=True)
        return results

    def matchup_grid(self, coach_id: int, filt: Optional[GameFilter] = None) -> dict:
        """Winrate grid keyed by the coach's race then the opponent's race."""
        filt = filt or GameFilter()
        games = self.games_for(coach_id, filt)
        grid: dict = {}
        for (coach_r, opp_r), g in games.groupby(["coachrace", "opponentrace"]):
            crace = RACE_NAMES.get(int(coach_r), str(coach_r))
            orace = RACE_NAMES.get(int(opp_r), str(opp_r))
            grid.setdefault(crace, {})[orace] = self.tally(g).to_dict()
        return {"coach_id": coach_id, "coach_name": self.coach_name(coach_id), "grid": grid}

    def get_coach_postcodes(self, coach_id: int) -> list:
        """Postcodes of the coach's English tournaments (as entered in the NAF DB).

        Faithful to the bb-county-tool: any tournament the coach played a game at,
        filtered to ``tournamentnation == 'England'``, non-null zip.
        """
        self._require_loaded()
        coach_games = self.game_data[
            (self.game_data["homecoachid"] == coach_id)
            | (self.game_data["awaycoachid"] == coach_id)
        ]
        tournament_ids = coach_games["tournamentid"].unique()
        english = self.tour_data[
            (self.tour_data["tournamentid"].isin(tournament_ids))
            & (self.tour_data["tournamentnation"] == "England")
        ]
        return english["tournamentzip"].dropna().tolist()

    # ------------------------------------------------------------------ #
    # Squad-level aggregation
    # ------------------------------------------------------------------ #
    def squad_stat_average(
        self, squad: dict[str, dict], stat: str, filt: Optional[GameFilter] = None
    ) -> float:
        """Mean of a single stat across every coach in a squad."""
        scores = [
            self.get_coach_stat(coach["number"], coach["race"], [stat], filt)[stat]
            for coach in squad.values()
        ]
        return float(np.mean(scores)) if scores else 0.0

    def rank_squads_by_stat(
        self, squads: dict[str, dict], stat: str, filt: Optional[GameFilter] = None
    ) -> list[dict]:
        """Rank squads by squad-average of a stat, best first.

        Returns ``[{"squad": ..., "score": ...}, ...]``.
        """
        if stat not in AVAILABLE_STATS:
            raise ValueError(f"Unknown stat: {stat!r}. Known: {AVAILABLE_STATS}")
        ranked = [
            {"squad": squad_name, "score": round(self.squad_stat_average(roster, stat, filt), 2)}
            for squad_name, roster in squads.items()
        ]
        ranked.sort(key=lambda row: row["score"], reverse=True)
        return ranked
