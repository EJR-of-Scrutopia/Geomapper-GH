import json

import pytest

from mapgen.wales_coverage import (
    COVERAGE_PATH,
    FULL_SHARE,
    councils_touching,
    coverage,
    coverage_share,
    load_areas,
    verdict,
)


def _square(west, south, east, north):
    return [[[west, south], [east, south], [east, north], [west, north], [west, south]]]


@pytest.fixture
def synthetic(tmp_path):
    """One council square west of lon 1, one inshore-waters square east of it."""
    path = tmp_path / "coverage.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"kind": "council", "name": "Testshire", "code": "T1"},
                        "geometry": {"type": "MultiPolygon", "coordinates": [_square(0, 50, 1, 51)]},
                    },
                    {
                        "type": "Feature",
                        "properties": {"kind": "inshore", "name": "Welsh inshore waters", "code": None},
                        "geometry": {"type": "MultiPolygon", "coordinates": [_square(1, 50, 2, 51)]},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_the_threshold_is_the_measured_98_percent():
    assert FULL_SHARE == 0.98


def test_verdict_bands():
    assert verdict(1.0) == "full"
    assert verdict(0.98) == "full"
    assert verdict(0.979) == "partial"
    assert verdict(0.0001) == "partial"
    assert verdict(0.0) == "none"


def test_a_rect_inside_a_council_is_full(synthetic):
    assert coverage((0.2, 50.2, 0.8, 50.8), synthetic) == "full"
    assert councils_touching((0.2, 50.2, 0.8, 50.8), synthetic) == ["Testshire"]


def test_a_coastal_rect_counts_the_inshore_waters(synthetic):
    rect = (0.8, 50.2, 1.2, 50.8)
    assert coverage_share(rect, synthetic) == pytest.approx(1.0)
    assert coverage(rect, synthetic) == "full"
    assert councils_touching(rect, synthetic) == ["Testshire"]


def test_a_rect_half_beyond_the_waters_is_partial(synthetic):
    assert coverage((1.8, 50.2, 2.2, 50.8), synthetic) == "partial"
    assert councils_touching((1.8, 50.2, 2.2, 50.8), synthetic) == []


def test_a_rect_wholly_elsewhere_is_none(synthetic):
    assert coverage((3.0, 50.0, 4.0, 51.0), synthetic) == "none"


def test_the_committed_file_holds_22_councils_and_the_inshore_waters():
    areas = load_areas(COVERAGE_PATH)
    assert sorted(area.kind for area in areas).count("council") == 22
    assert [area.name for area in areas if area.kind == "inshore"] == ["Welsh inshore waters"]


# Real extents, against the committed file. The spike that set the 98%
# threshold measured these (see the spec's "Coverage").

def test_cowbridge_is_full_and_in_the_vale():
    rect = (-3.4637, 51.4527, -3.4343, 51.4703)
    assert coverage(rect) == "full"
    assert councils_touching(rect) == ["Vale of Glamorgan"]


def test_a_coastal_extent_at_aberystwyth_is_full():
    assert coverage((-4.0950, 52.4080, -4.0750, 52.4200)) == "full"


def test_an_extent_across_the_english_border_at_chepstow_is_partial():
    assert coverage((-2.7000, 51.6300, -2.6400, 51.6500)) == "partial"


def test_bristol_is_none():
    assert coverage((-2.62, 51.44, -2.56, 51.47)) == "none"
