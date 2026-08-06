import re

import pytest

from mapgen.geo import BBox
from mapgen.sources.inspire import (
    AUTHORITY_INDEX_PATH,
    InspireError,
    authorities_for,
    load_authority_index,
)

# See make_authority_index.py's module docstring: a hyphen is neither a
# space nor a path separator, and five real HMLR authority names carry
# one (Newcastle-under-Lyme and friends), so the safety property this
# checks is the one the brief's own parenthetical states, not its
# hyphen-free character class.
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+$")


def test_llantwit_major_bbox_returns_only_vale_of_glamorgan():
    bbox = BBox(west=-3.495, south=51.395, east=-3.475, north=51.410)
    assert authorities_for(bbox) == ["Vale_of_Glamorgan_Council"]


def test_bbox_spanning_cardiff_vale_border_returns_both():
    bbox = BBox(west=-3.263442, south=51.438073, east=-3.243442, north=51.458073)
    assert authorities_for(bbox) == ["Cardiff_Council", "Vale_of_Glamorgan_Council"]


def test_scottish_bbox_returns_no_authorities():
    # INSPIRE Index Polygons cover England and Wales only. Central
    # Edinburgh, nowhere near any England/Wales authority even with the
    # 1 km pad.
    bbox = BBox(west=-3.30, south=55.90, east=-3.10, north=56.00)
    assert authorities_for(bbox) == []


def test_result_is_sorted():
    # Cardiff then Vale of Glamorgan alphabetically, not fetch order.
    bbox = BBox(west=-3.263442, south=51.438073, east=-3.243442, north=51.458073)
    result = authorities_for(bbox)
    assert result == sorted(result)


def test_index_file_has_318_entries():
    index = load_authority_index()
    assert len(index) == 318


def test_every_name_is_a_safe_path_segment_and_filename():
    index = load_authority_index()
    bad = [name for name in index if not _NAME_SHAPE.match(name)]
    assert bad == []


def test_every_bbox_is_four_floats_west_south_east_north():
    index = load_authority_index()
    for name, bbox in index.items():
        assert len(bbox) == 4, name
        west, south, east, north = bbox
        assert west < east, name
        assert south < north, name


def test_the_four_home_councils_are_present_with_plausible_geography():
    index = load_authority_index()

    # South Wales, this tool's own reference area. Roughly ordered west
    # to east: Bridgend, Vale of Glamorgan/Cardiff (Cardiff to the east
    # of Vale), Merthyr Tydfil to the north of both.
    bridgend = index["Bridgend_County_Borough_Council"]
    vale = index["Vale_of_Glamorgan_Council"]
    cardiff = index["Cardiff_Council"]
    merthyr = index["Merthyr_Tydfil_County_Borough_Council"]

    assert bridgend[2] < cardiff[0]  # Bridgend's east sits west of Cardiff's west
    assert vale[3] < merthyr[3]  # Vale sits south of Merthyr Tydfil's north
    assert cardiff[0] < cardiff[2] and cardiff[1] < cardiff[3]


def test_authorities_for_intersecting_bbox_but_no_authority_returns_empty_list():
    # Mid-Atlantic: not a malformed bbox, just genuinely nowhere near
    # England or Wales.
    bbox = BBox(west=-30.0, south=40.0, east=-29.0, north=41.0)
    assert authorities_for(bbox) == []


def test_authority_index_path_points_at_the_committed_fixture():
    assert AUTHORITY_INDEX_PATH.name == "authority_index.json"
    assert AUTHORITY_INDEX_PATH.exists()


def test_load_authority_index_raises_inspire_error_on_missing_file(tmp_path):
    missing = tmp_path / "does_not_exist.json"
    with pytest.raises(InspireError):
        load_authority_index(missing)


def test_load_authority_index_raises_inspire_error_on_malformed_json(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    with pytest.raises(InspireError):
        load_authority_index(bad)


def test_load_authority_index_raises_inspire_error_on_wrong_shape(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"Some_Council": [1.0, 2.0, 3.0]}', encoding="utf-8")
    with pytest.raises(InspireError):
        load_authority_index(bad)
