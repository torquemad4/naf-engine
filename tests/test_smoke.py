"""Fast checks that need no NAF download."""

import naf_engine
from naf_engine import counties
from naf_engine.datamine import GAMES_SCOPES, GameFilter, NafDatamine
from naf_engine.rosters import race_id


def test_version():
    assert naf_engine.__version__


def test_game_filter_defaults():
    f = GameFilter()
    assert f.variants == GAMES_SCOPES["competitive"][1]


def test_race_lookup():
    assert race_id("Human") > 0


def test_patch_levels_are_monotonic():
    levels = [counties.patch_level(n) for n in range(0, 49)]
    assert levels == sorted(levels)


def test_datamine_not_loaded_until_asked(tmp_path):
    dm = NafDatamine(data_dir=tmp_path)
    assert not dm.loaded
