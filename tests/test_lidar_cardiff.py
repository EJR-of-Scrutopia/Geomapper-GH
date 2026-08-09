"""LidarCardiffSource's suite: pure arithmetic (covers, tier, detail,
estimate, routing_note, cache_dir), no fake COG and no fixture OSTN15
grid needed at all, since this module never opens either.

Every bbox-shaped test below builds its bbox from a BNG rectangle via
`_bbox_for_padded_bng_rect`, which round-trips through `tm_inverse` and
relies on `tm_forward`/`tm_inverse` being exact inverses of one another
on the pseudo-grid (`mapgen.bng`'s own docstrings for both). This only
works with NO cached OSTN15 grid in play (`best_effort_padded_bng_extent`
falling back to `approx_padded_bng_extent`, the gridless path): every
such test passes an isolated, freshly-created `tmp_path` as
`ostn15_cache_dir`, never `None` and never a shared path, because `None`
means "the machine's own real `~/.mapgen` cache", which may already be
warm on whatever machine runs this suite and would silently switch these
tests onto the real, OSTN15-shifted `padded_bng_extent` path instead,
where the exact rectangles this file pins would no longer land where the
comments say they do.

Coverage-tile geometry used throughout (see lidar_cardiff.py's own
module docstring for the fuller picture): the ten tiles form a solid 3
by 3 block of 500 m cells from (310500, 176500) to (312000, 178000),
plus one further cell, ST1277SW, hanging off the east side of the middle
row (312000-312500, 177000-177500). The two 500 m cells directly north
and south of ST1277SW, at (312000-312500, 176500-177000) and
(312000-312500, 177500-178000), are NOT covered: this is what makes the
whole block's own 2000 x 1500 m bounding rectangle a "partial" test, not
a "full" one.
"""

from __future__ import annotations

import socket

import pytest

from mapgen.bng import tm_inverse
from mapgen.cog import MAX_WINDOW_PIXELS
from mapgen.egrid import PAD_METRES
from mapgen.geo import BBox
from mapgen.sources import lidar_cardiff
from mapgen.sources.lidar_cardiff import (
    COVERAGE_TILES,
    DSM_ZIP_BYTES,
    DSM_ZIP_NAME,
    DSM_ZIP_URL,
    DTM_ZIP_BYTES,
    DTM_ZIP_NAME,
    DTM_ZIP_URL,
    FLOWN,
    PIXEL_METRES,
    LidarCardiffSource,
    _touched_lattice_cells,
    _window_pixels,
)


# --------------------------------------------------------------------------
# Building bboxes that land on exact BNG rectangles, with no OSTN15 grid.
# --------------------------------------------------------------------------


def _unpadded_corners(e_min: float, n_min: float, e_max: float, n_max: float):
    return e_min + PAD_METRES, n_min + PAD_METRES, e_max - PAD_METRES, n_max - PAD_METRES


def _bbox_for_padded_bng_rect(e_min: float, n_min: float, e_max: float, n_max: float) -> BBox:
    """A BBox whose own padded extent, read back with no cached OSTN15
    grid, reproduces (e_min, n_min, e_max, n_max) to a fraction of a
    millimetre. See the module docstring for why "no cached grid" matters.
    """
    e_sw, n_sw, e_ne, n_ne = _unpadded_corners(e_min, n_min, e_max, n_max)
    south, west = tm_inverse(e_sw, n_sw)
    north, east = tm_inverse(e_ne, n_ne)
    return BBox(west=west, south=south, east=east, north=north)


# Comfortably inside ST1177SW (311000-311500, 177000-177500), margin 40 m
# on every side: covers() == "full", and small enough to stay well under
# MAX_WINDOW_PIXELS.
_FULL_UNDER_BUDGET = (311040.0, 177040.0, 311460.0, 177460.0)

# Inside the solid 3x3 block (310500-312000, 176500-178000), margin
# 100-200 m from every real coverage edge: covers() == "full", but at
# 1200 x 1200 m the intersection with the coverage envelope clears
# MAX_WINDOW_PIXELS.
_FULL_OVER_BUDGET = (310600.0, 176600.0, 311800.0, 177800.0)

# The whole ten-tile block's own bounding rectangle: touches all twelve
# 500 m cells in its 4x3 span, ten of them real tiles and two of them the
# gap either side of ST1277SW (see the module docstring). 2000 x 1500 m
# at 0.25 m is 48,000,000 pixels, over MAX_WINDOW_PIXELS (16,777,216).
_WHOLE_BLOCK_PARTIAL_OVER_BUDGET = (310500.0, 176500.0, 312500.0, 178000.0)

# Straddles the coverage block's own west edge (e=310500) while staying
# inside a single covered n-band (176500-177000, ST1076NE's own band):
# touches one covered cell and one uncovered cell to its west.
_STRADDLES_WEST_EDGE_PARTIAL_UNDER_BUDGET = (310290.0, 176540.0, 310710.0, 176960.0)

# Straddles the boundary between the uncovered cell south of ST1076NE
# (310500-311000, 176000-176500) and ST1076NE itself
# (310500-311000, 176500-177000), the concave corner the brief calls out
# by name: an extent spanning both must never read "full".
_CONCAVE_CORNER_PARTIAL = (310540.0, 176290.0, 310960.0, 176710.0)

# Nowhere near the coverage block at all (about 10 km south-west of it).
_FAR_AWAY_NONE = (299800.0, 169800.0, 300220.0, 170220.0)


# --------------------------------------------------------------------------
# _touched_lattice_cells(): the pure lattice arithmetic covers() stands on.
# --------------------------------------------------------------------------


def test_touched_lattice_cells_returns_exactly_one_cell_for_a_single_tile():
    cells = _touched_lattice_cells(311000.0, 176500.0, 311500.0, 177000.0)
    assert cells == [(311000.0, 176500.0, 311500.0, 177000.0)]


def test_touched_lattice_cells_excludes_the_cell_starting_at_the_far_edge():
    # A rectangle ending EXACTLY at 311000 touches only the cell below
    # that edge, never the one that starts there: half-open intervals,
    # matching the module docstring's own claim.
    cells = _touched_lattice_cells(310999.0, 176500.0, 311000.0, 177000.0)
    assert cells == [(310500.0, 176500.0, 311000.0, 177000.0)]


def test_touched_lattice_cells_includes_the_cell_starting_at_the_near_edge():
    cells = _touched_lattice_cells(311000.0, 176500.0, 311001.0, 176501.0)
    assert cells == [(311000.0, 176500.0, 311500.0, 177000.0)]


def test_touched_lattice_cells_spans_a_two_by_two_block():
    cells = _touched_lattice_cells(310800.0, 176800.0, 311200.0, 177200.0)
    assert set(cells) == {
        (310500.0, 176500.0, 311000.0, 177000.0),
        (310500.0, 177000.0, 311000.0, 177500.0),
        (311000.0, 176500.0, 311500.0, 177000.0),
        (311000.0, 177000.0, 311500.0, 177500.0),
    }


# --------------------------------------------------------------------------
# covers(): full / partial / none, including the concave corner.
# --------------------------------------------------------------------------


def test_covers_is_full_wholly_inside_one_tile(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)
    assert source.covers(bbox) == "full"


def test_covers_is_full_over_the_solid_three_by_three_block(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_OVER_BUDGET)
    assert source.covers(bbox) == "full"


def test_covers_is_partial_straddling_the_blocks_west_edge(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_STRADDLES_WEST_EDGE_PARTIAL_UNDER_BUDGET)
    assert source.covers(bbox) == "partial"


def test_covers_is_partial_over_the_whole_blocks_bounding_rectangle(tmp_path):
    # The bounding rectangle of the union is NOT the same shape as the
    # union itself (see the module docstring): this is the test that
    # would wrongly read "full" if covers() ever regressed to a
    # bounding-rectangle test instead of the lattice, cell-for-cell one.
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)
    assert source.covers(bbox) == "partial"


def test_covers_is_partial_never_full_at_the_concave_corner(tmp_path):
    # The brief's own named case: an extent touching the uncovered cell
    # south of ST1076NE together with ST1076NE itself must never be
    # "full", by the exact, cell-for-cell lattice test.
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_CONCAVE_CORNER_PARTIAL)
    result = source.covers(bbox)
    assert result != "full"
    assert result == "partial"


def test_covers_is_none_far_from_the_block(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FAR_AWAY_NONE)
    assert source.covers(bbox) == "none"


def test_covers_is_none_in_barry():
    # Real-world sanity check beside the synthetic BNG-rectangle tests
    # above: Barry Island, used elsewhere in this suite's own lidar_wales
    # tests, is many kilometres from Creigiau/Pentyrch.
    source = LidarCardiffSource()
    barry_bbox = BBox.parse("-3.272,51.393,-3.268,51.397")
    assert source.covers(barry_bbox) == "none"


# --------------------------------------------------------------------------
# _window_pixels(): the budget arithmetic detail() previews.
# --------------------------------------------------------------------------


def test_window_pixels_is_zero_when_the_extent_never_reaches_the_envelope(tmp_path):
    bbox = _bbox_for_padded_bng_rect(*_FAR_AWAY_NONE)
    assert _window_pixels(bbox, tmp_path) == 0


def test_window_pixels_matches_the_full_padded_area_inside_one_tile(tmp_path):
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)
    e_min, n_min, e_max, n_max = _FULL_UNDER_BUDGET
    expected = ((e_max - e_min) / PIXEL_METRES) * ((n_max - n_min) / PIXEL_METRES)
    assert _window_pixels(bbox, tmp_path) == pytest.approx(expected, rel=1e-5)


def test_window_pixels_clips_to_the_coverage_envelope_not_the_whole_padded_extent(tmp_path):
    # The straddling bbox pads out west of the coverage envelope
    # entirely; _window_pixels must count only the slice that actually
    # falls inside the envelope, not the whole padded rectangle.
    bbox = _bbox_for_padded_bng_rect(*_STRADDLES_WEST_EDGE_PARTIAL_UNDER_BUDGET)
    e_min, n_min, e_max, n_max = _STRADDLES_WEST_EDGE_PARTIAL_UNDER_BUDGET
    clipped_width = e_max - 310500.0  # envelope's own west edge
    clipped_height = n_max - n_min
    expected = (clipped_width / PIXEL_METRES) * (clipped_height / PIXEL_METRES)
    assert _window_pixels(bbox, tmp_path) == pytest.approx(expected, rel=1e-4)


def test_window_pixels_over_the_whole_block_is_48_million_and_over_budget(tmp_path):
    bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)
    pixels = _window_pixels(bbox, tmp_path)
    assert pixels == pytest.approx(48_000_000, rel=1e-5)
    assert pixels > MAX_WINDOW_PIXELS


# --------------------------------------------------------------------------
# detail(): the four outcomes, verbatim.
# --------------------------------------------------------------------------


def test_detail_full_and_within_budget(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)
    assert source.covers(bbox) == "full"
    assert source.detail(bbox) == "25 cm at this extent, flown 2011"


def test_detail_full_but_over_budget(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_OVER_BUDGET)
    assert source.covers(bbox) == "full"
    assert source.detail(bbox) == (
        "25 cm needs an extent under about 1 x 1 km here (flown 2011)"
    )


def test_detail_partial_under_budget_has_no_appended_clause(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_STRADDLES_WEST_EDGE_PARTIAL_UNDER_BUDGET)
    assert source.covers(bbox) == "partial"
    assert source.detail(bbox) == "25 cm over part of this extent, flown 2011"


def test_detail_partial_and_over_budget_appends_the_extra_sentence(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)
    assert source.covers(bbox) == "partial"
    assert source.detail(bbox) == (
        "25 cm over part of this extent, flown 2011; "
        "25 cm needs an extent under about 1 x 1 km here"
    )


def test_detail_is_none_when_covers_is_none(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FAR_AWAY_NONE)
    assert source.covers(bbox) == "none"
    assert source.detail(bbox) is None


def test_detail_at_the_concave_corner_is_the_partial_sentence(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_CONCAVE_CORNER_PARTIAL)
    assert source.detail(bbox) == "25 cm over part of this extent, flown 2011"


def test_detail_touches_no_network(tmp_path):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)
    # No session exists on this source at all yet (see the module
    # docstring's "No network in this module yet" section); reaching
    # this line without a network call being possible is itself part of
    # the proof, reinforced by the socket-level test further down.
    assert source.detail(bbox) == "25 cm at this extent, flown 2011"


# --------------------------------------------------------------------------
# tier(): terrain only, at 0.
# --------------------------------------------------------------------------


def test_tier_is_zero_for_terrain():
    source = LidarCardiffSource()
    assert source.tier("terrain") == 0
    # type(...) is int, not merely == 0: matches this project's own
    # is_overview-vs-bool caution (test_lidar_wales.py), applied here to
    # the same footgun (False == 0 in Python).
    assert type(source.tier("terrain")) is int


@pytest.mark.parametrize("category", ["contours", "heights", "addresses", "buildings", "roofs"])
def test_tier_is_none_for_every_other_category(category):
    source = LidarCardiffSource()
    assert source.tier(category) is None


# --------------------------------------------------------------------------
# estimate(): cold vs warm, via a monkeypatched cache_dir(). No bbox- or
# tile-dependence: this archive is a flat, whole-zip, one-time cost.
# --------------------------------------------------------------------------


def _any_bbox() -> BBox:
    return BBox.parse("-3.272,51.393,-3.268,51.397")


def test_estimate_is_cold_when_the_cache_dir_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    estimate = source.estimate(_any_bbox(), [])

    assert estimate.bytes_estimate == DSM_ZIP_BYTES + DTM_ZIP_BYTES
    expected_seconds = max(
        (DSM_ZIP_BYTES + DTM_ZIP_BYTES) / lidar_cardiff.BYTES_PER_SECOND_ESTIMATE,
        lidar_cardiff.SECONDS_FLOOR,
    )
    assert estimate.seconds_estimate == pytest.approx(expected_seconds)


def test_estimate_is_cold_when_only_the_dsm_zip_is_present(tmp_path, monkeypatch):
    (tmp_path / DSM_ZIP_NAME).write_bytes(b"partial-download")
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    estimate = source.estimate(_any_bbox(), [])

    assert estimate.bytes_estimate == DSM_ZIP_BYTES + DTM_ZIP_BYTES


def test_estimate_treats_an_empty_zip_file_as_cold_not_a_valid_resume(tmp_path, monkeypatch):
    (tmp_path / DSM_ZIP_NAME).write_bytes(b"real-bytes")
    (tmp_path / DTM_ZIP_NAME).write_bytes(b"")  # zero bytes: not a real download
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    estimate = source.estimate(_any_bbox(), [])

    assert estimate.bytes_estimate == DSM_ZIP_BYTES + DTM_ZIP_BYTES


def test_estimate_is_warm_when_both_zips_are_present(tmp_path, monkeypatch):
    (tmp_path / DSM_ZIP_NAME).write_bytes(b"already-downloaded-dsm")
    (tmp_path / DTM_ZIP_NAME).write_bytes(b"already-downloaded-dtm")
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    estimate = source.estimate(_any_bbox(), [])

    assert estimate.bytes_estimate == 0
    assert estimate.seconds_estimate == pytest.approx(lidar_cardiff.SECONDS_FLOOR)


def test_estimate_ignores_bbox_and_tiles_entirely(tmp_path, monkeypatch):
    # A flat, whole-archive cost, the os_uprn.py shape: a tiny extent and
    # a huge one must price identically once the cache state is fixed.
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    small = source.estimate(_bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET), [])
    large = source.estimate(_bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET), [])

    assert small.bytes_estimate == large.bytes_estimate
    assert small.seconds_estimate == pytest.approx(large.seconds_estimate)


# --------------------------------------------------------------------------
# routing_note(): present until both zips are cached, gone once they are.
# --------------------------------------------------------------------------


def test_routing_note_warns_when_the_cache_is_cold(tmp_path, monkeypatch):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    assert source.routing_note() == (
        "LiDAR terrain (Creigiau and Pentyrch): first use downloads two "
        "zip files (about 84 MB total, cached for every later survey)."
    )


def test_routing_note_is_none_once_both_zips_are_cached(tmp_path, monkeypatch):
    (tmp_path / DSM_ZIP_NAME).write_bytes(b"already-downloaded-dsm")
    (tmp_path / DTM_ZIP_NAME).write_bytes(b"already-downloaded-dtm")
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    assert source.routing_note() is None


# --------------------------------------------------------------------------
# cache_dir(): ~/.mapgen/lidar_cardiff, resolved from CONFIG_PATH.
# --------------------------------------------------------------------------


def test_cache_dir_is_under_the_mapgen_home(monkeypatch, tmp_path):
    fake_config_path = tmp_path / ".mapgen" / "config.json"
    monkeypatch.setattr(lidar_cardiff, "CONFIG_PATH", fake_config_path)
    assert lidar_cardiff.cache_dir() == tmp_path / ".mapgen" / "lidar_cardiff"


# --------------------------------------------------------------------------
# No network, ever: a socket-refusing fixture, since this module has no
# session to inject a refusing stub into at all (see the module
# docstring's "No network in this module yet" section).
# --------------------------------------------------------------------------


class _RefusesToOpenASocket:
    def __call__(self, *args, **kwargs):
        raise AssertionError(
            "lidar_cardiff's covers/tier/detail/estimate/routing_note must "
            "never open a network socket"
        )


def test_covers_tier_detail_estimate_and_routing_note_never_touch_the_network(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(socket, "socket", _RefusesToOpenASocket())
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)

    assert source.covers(bbox) == "full"
    assert source.tier("terrain") == 0
    assert source.detail(bbox) == "25 cm at this extent, flown 2011"
    estimate = source.estimate(bbox, [])
    assert estimate.bytes_estimate == DSM_ZIP_BYTES + DTM_ZIP_BYTES
    assert source.routing_note() is not None


# --------------------------------------------------------------------------
# Class attributes and module constants, verbatim from the brief.
# --------------------------------------------------------------------------


def test_declares_the_briefs_exact_strings():
    source = LidarCardiffSource()
    assert source.id == "lidar_cardiff"
    assert source.display_name == (
        "LiDAR terrain (Creigiau and Pentyrch, north-west Cardiff, 25 cm, "
        "flown 2011)"
    )
    assert source.requires_api_key is False


def test_flown_string_is_pinned():
    assert FLOWN == "flown 23 March 2011"


def test_urls_and_byte_counts_are_pinned():
    assert DSM_ZIP_URL == "https://lle.blob.core.windows.net/lidar/25cm_res_ST17_2011_dsm.zip"
    assert DTM_ZIP_URL == "https://lle.blob.core.windows.net/lidar/25cm_res_ST17_2011_dtm.zip"
    assert DSM_ZIP_BYTES == 45_011_591
    assert DTM_ZIP_BYTES == 38_784_302
    assert PIXEL_METRES == 0.25
    assert lidar_cardiff.BYTES_PER_SECOND_ESTIMATE == 2_000_000


def test_coverage_tiles_is_exactly_the_ten_probed_envelopes():
    assert COVERAGE_TILES == (
        (310500.0, 176500.0, 311000.0, 177000.0),
        (310500.0, 177000.0, 311000.0, 177500.0),
        (310500.0, 177500.0, 311000.0, 178000.0),
        (311000.0, 176500.0, 311500.0, 177000.0),
        (311000.0, 177000.0, 311500.0, 177500.0),
        (311000.0, 177500.0, 311500.0, 178000.0),
        (311500.0, 176500.0, 312000.0, 177000.0),
        (311500.0, 177000.0, 312000.0, 177500.0),
        (311500.0, 177500.0, 312000.0, 178000.0),
        (312000.0, 177000.0, 312500.0, 177500.0),
    )
    assert len(set(COVERAGE_TILES)) == 10


def test_no_url_ever_appears_in_a_user_facing_string():
    source = LidarCardiffSource()
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)
    strings = [
        source.display_name,
        source.detail(bbox) or "",
        source.routing_note() or "",
    ]
    for text in strings:
        assert "http" not in text.lower()
