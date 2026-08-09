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
import zipfile
from datetime import date
from pathlib import Path

import pytest
import requests

from mapgen.bng import tm_inverse
from mapgen.cog import MAX_WINDOW_PIXELS, CogReader, FileByteSource, read_full_window
from mapgen.egrid import PAD_METRES
from mapgen.geo import BBox, Tile
from mapgen.package import IncompleteSurveyError, SurveyRequest, run_survey
from mapgen.sources import lidar_cardiff
from mapgen.sources.base import (
    FAILURE_NODE_CAP,
    FAILURE_UNREACHABLE,
    Estimate,
    NullProgress,
    clear_registry,
    register,
)
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
    LidarCardiffError,
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


def _write_right_size_stub(path, size: int) -> None:
    """Plain padding bytes, sized to match one of the archive's own byte
    constants exactly: a fixture for any warm-cache check that only ever
    looks at `st_size` (`_cache_is_warm`, `_ensure_zip`'s own warm
    branch) and is never opened as a zip. A short, arbitrary string used
    to stand in for "already downloaded" here before `_cache_is_warm`
    was tightened to an exact-size test; this is its exact-size
    replacement, shared by the estimate()/routing_note() fixtures below
    and the fetch() fixtures further down.
    """
    path.write_bytes(b"\x00" * size)


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
    dsm_path = tmp_path / DSM_ZIP_NAME
    dtm_path = tmp_path / DTM_ZIP_NAME
    _write_right_size_stub(dsm_path, DSM_ZIP_BYTES)
    _write_right_size_stub(dtm_path, DTM_ZIP_BYTES)
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)

    estimate = source.estimate(_any_bbox(), [])

    assert estimate.bytes_estimate == 0
    assert estimate.seconds_estimate == pytest.approx(lidar_cardiff.SECONDS_FLOOR)

    # Consistency, the review's own Minor finding: estimate() calling this
    # cache warm must agree with what fetch() itself does with the exact
    # same files, not merely with _cache_is_warm's own, separately
    # maintained check. A network-refusing session proves fetch() also
    # treats this fixture as warm: if the two ever disagreed again (one
    # loosened back to a >0 check, say), this half would fail even though
    # the assertions above still pass.
    warm_source = LidarCardiffSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    result = warm_source.fetch(_any_bbox(), [], tmp_path / "work", NullProgress())
    assert result == [dsm_path, dtm_path]


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
    _write_right_size_stub(tmp_path / DSM_ZIP_NAME, DSM_ZIP_BYTES)
    _write_right_size_stub(tmp_path / DTM_ZIP_NAME, DTM_ZIP_BYTES)
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


# --------------------------------------------------------------------------
# fetch(): the two zips, cached once, verified. Every offline test below
# patches lidar_cardiff.cache_dir to an isolated tmp_path (never the real
# ~/.mapgen/lidar_cardiff) and never lets a stub session serve fewer or
# more bytes than a real Content-Length answer would, since fetch() checks
# length exactly. "Already warm" fixtures below are plain padding bytes,
# never real zip content: _ensure_zip's own warm branch (dest already the
# right size) returns before it ever opens the file with zipfile, the same
# way _cache_is_warm above only ever checks size, so a warm fixture's own
# content is never read at all and does not need to be a real zip.
# --------------------------------------------------------------------------


def _build_padded_zip(path: Path, member_name: str, total_size: int) -> bytes:
    """A real, `zipfile`-openable zip at `path`, one stored member, padded
    so the whole file is exactly `total_size` bytes. Two passes: the
    first writes an empty member to measure this zip's own fixed
    overhead (local header + central directory + end-of-central-
    directory, all independent of a STORED member's own payload), the
    second writes the exact padding needed to reach the target. Returns
    the finished file's own bytes, for a stub session to serve back.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(member_name, b"")
    overhead = path.stat().st_size
    payload_size = total_size - overhead
    assert payload_size >= 0, "target size too small for this member name"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        archive.writestr(member_name, b"\x00" * payload_size)
    data = path.read_bytes()
    assert len(data) == total_size
    return data


class _FakeStreamResponse:
    """A minimal stand-in for `requests.Response` under `stream=True`:
    only the members `LidarCardiffSource._ensure_zip` actually calls.
    """

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int):
        body = self._body
        for start in range(0, len(body), chunk_size):
            yield body[start : start + chunk_size]


class _ScriptedSession:
    """Answers `.get(url, ...)` with whatever `bodies[url]` holds, and
    records every URL asked for. Raises `AssertionError` for a URL not in
    `bodies`, so a test cannot pass by accident on the wrong archive.
    """

    def __init__(self, bodies: dict[str, bytes]) -> None:
        self._bodies = bodies
        self.requested_urls: list[str] = []

    def get(self, url, headers=None, stream=None, timeout=None):
        self.requested_urls.append(url)
        if url not in self._bodies:
            raise AssertionError(f"unexpected request for {url!r}")
        return _FakeStreamResponse(self._bodies[url])


class _RefusesToConnect:
    """A session whose `.get()` always fails the test: proves fetch()
    never opens a network connection when both zips are already cached at
    the right size, the fetch()-era successor to this suite's own
    socket-refusing fixture above (this class is what a session-shaped
    seam gives fetch() that covers/tier/detail/estimate/routing_note
    never had, per the module docstring's own "No network except fetch()"
    section).
    """

    def get(self, *args, **kwargs):
        raise AssertionError(
            "lidar_cardiff's fetch() must not touch the network when both "
            "zips are already cached at the right size"
        )


def test_fetch_does_not_download_when_both_zips_are_already_the_right_size(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    dsm_path = tmp_path / DSM_ZIP_NAME
    dtm_path = tmp_path / DTM_ZIP_NAME
    _write_right_size_stub(dsm_path, DSM_ZIP_BYTES)
    _write_right_size_stub(dtm_path, DTM_ZIP_BYTES)

    source = LidarCardiffSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    result = source.fetch(_any_bbox(), [], tmp_path / "work", NullProgress())

    assert result == [dsm_path, dtm_path]


def test_fetch_redownloads_a_wrong_size_cached_zip(tmp_path, monkeypatch):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    dsm_path = tmp_path / DSM_ZIP_NAME
    dtm_path = tmp_path / DTM_ZIP_NAME
    dsm_path.write_bytes(b"stale-partial-download")  # wrong size on purpose
    _write_right_size_stub(dtm_path, DTM_ZIP_BYTES)  # already warm, right size

    fresh_dsm_bytes = _build_padded_zip(tmp_path / "fresh_dsm_source.zip", "dsm.asc", DSM_ZIP_BYTES)
    session = _ScriptedSession({DSM_ZIP_URL: fresh_dsm_bytes})

    source = LidarCardiffSource(session=session, ostn15_cache_dir=tmp_path)
    result = source.fetch(_any_bbox(), [], tmp_path / "work", NullProgress())

    assert result == [dsm_path, dtm_path]
    # DTM was already the right size: never requested.
    assert session.requested_urls == [DSM_ZIP_URL]
    assert dsm_path.stat().st_size == DSM_ZIP_BYTES
    assert dsm_path.read_bytes() == fresh_dsm_bytes
    assert list(tmp_path.glob("*.part")) == []


def test_fetch_raises_download_kind_with_no_url_on_a_size_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    dtm_path = tmp_path / DTM_ZIP_NAME
    _write_right_size_stub(dtm_path, DTM_ZIP_BYTES)  # already warm, right size

    short_body = b"x" * (DSM_ZIP_BYTES - 1)  # one byte short of the constant
    session = _ScriptedSession({DSM_ZIP_URL: short_body})

    source = LidarCardiffSource(session=session, ostn15_cache_dir=tmp_path)
    with pytest.raises(LidarCardiffError) as excinfo:
        source.fetch(_any_bbox(), [], tmp_path / "work", NullProgress())

    assert excinfo.value.kind == "download"
    assert "http" not in str(excinfo.value).lower()
    assert DSM_ZIP_URL not in str(excinfo.value)
    assert not (tmp_path / DSM_ZIP_NAME).exists()
    assert list(tmp_path.glob("*.part")) == []


def test_fetch_fails_validation_with_the_sources_own_error_for_a_non_zip_payload(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    dtm_path = tmp_path / DTM_ZIP_NAME
    _write_right_size_stub(dtm_path, DTM_ZIP_BYTES)  # already warm, right size

    garbage = b"x" * DSM_ZIP_BYTES  # right length, not a zip at all
    session = _ScriptedSession({DSM_ZIP_URL: garbage})

    source = LidarCardiffSource(session=session, ostn15_cache_dir=tmp_path)
    with pytest.raises(LidarCardiffError) as excinfo:
        source.fetch(_any_bbox(), [], tmp_path / "work", NullProgress())

    # The source's own exception type, not a raw zipfile.BadZipFile
    # escaping unwrapped.
    assert excinfo.value.kind == "parse"
    assert not (tmp_path / DSM_ZIP_NAME).exists()
    assert list(tmp_path.glob("*.part")) == []


# --------------------------------------------------------------------------
# fetch()'s own budget gate (review Important #2): the primary refusal now
# lives here, before either zip is downloaded, because package.py's
# run_survey wraps fetch() in the deferred-failure/retry machinery but does
# not currently wrap merge() in anything, so a merge()-only refusal would
# never reach the owner through the ordinary source-failure event. merge()
# keeps its own copy of the same gate as a second line of defence (tested
# separately, further down); this section proves the fetch()-side one.
# --------------------------------------------------------------------------


def test_fetch_refuses_over_budget_before_downloading_anything(tmp_path, monkeypatch):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)
    pixels = _window_pixels(bbox, tmp_path)
    assert pixels > MAX_WINDOW_PIXELS

    tiles = [
        Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox),
        Tile(tile_id="r00_c01", row=0, col=1, core_bbox=bbox, query_bbox=bbox),
    ]
    # A socket-refusing session: if the gate ran after either zip started
    # downloading rather than before both, this fixture is what turns
    # that regression into a test failure rather than a slow real request.
    source = LidarCardiffSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)

    with pytest.raises(LidarCardiffError) as excinfo:
        source.fetch(bbox, tiles, tmp_path / "work", NullProgress())

    assert excinfo.value.kind == "budget"
    # Pin the substitution, not just the prose (the item B lesson, same
    # as the merge()-side test): re-derive the exact pixel count rather
    # than hard-coding a number that can drift with tm_inverse/tm_forward
    # round-trip noise.
    assert str(excinfo.value) == (
        f"this extent needs {pixels:,} pixels at 25 cm and the raster "
        f"budget is 16,777,216; extents under about 1 x 1 km inside the "
        f"covered block come back at 25 cm"
    )

    assert source.tile_failures
    assert {failure.tile_id for failure in source.tile_failures} == {"r00_c00", "r00_c01"}
    for failure in source.tile_failures:
        assert failure.source == "lidar_cardiff"
        # Never retryable: asking again cannot shrink the extent.
        assert failure.kind == FAILURE_NODE_CAP
        assert failure.reason == str(excinfo.value)
        assert "http" not in failure.reason.lower()

    # Neither zip exists: the gate ran before _ensure_zip touched either.
    assert not (tmp_path / DSM_ZIP_NAME).exists()
    assert not (tmp_path / DTM_ZIP_NAME).exists()
    assert list(tmp_path.glob("*.part")) == []


# --------------------------------------------------------------------------
# tile_failures: the pipeline-safety account. Review finding (Critical):
# LidarCardiffSource never set this, unlike every other production
# LayerSource (lidar_wales, os_uprn, os_open, elevation, osm, overture,
# inspire), so a fetch() failure took package.py's "whole layer failed,
# nothing to retry" branch and re-raised immediately regardless of
# request.force, ending the survey before any later-ordered source ever
# ran. Both tests below prove the fix: the unit-level test that a
# transport failure records a classified, URL-free TileFailure per tile,
# and the pipeline-level test that this is what actually lets a real
# run_survey defer the decision instead of dying on the spot.
# --------------------------------------------------------------------------


class _TransportFailureSession:
    """A session whose `.get()` always raises a transport-shaped
    `requests.RequestException`, proving `_ensure_zip`'s own `except
    requests.RequestException` branch, not only the length-mismatch and
    zip-validation branches the earlier tests above already cover.
    """

    def get(self, *args, **kwargs):
        raise requests.ConnectionError("simulated connection failure")


def test_fetch_records_a_classified_tile_failure_with_no_url_on_a_transport_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    _write_right_size_stub(tmp_path / DTM_ZIP_NAME, DTM_ZIP_BYTES)  # already warm

    bbox = _any_bbox()
    tiles = [
        Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox),
        Tile(tile_id="r00_c01", row=0, col=1, core_bbox=bbox, query_bbox=bbox),
    ]
    source = LidarCardiffSource(session=_TransportFailureSession(), ostn15_cache_dir=tmp_path)

    with pytest.raises(LidarCardiffError) as excinfo:
        source.fetch(bbox, tiles, tmp_path / "work", NullProgress())

    assert excinfo.value.kind == "download"
    assert source.tile_failures
    assert {failure.tile_id for failure in source.tile_failures} == {"r00_c00", "r00_c01"}
    for failure in source.tile_failures:
        assert failure.source == "lidar_cardiff"
        # classify_lidar_cardiff_error's own mapping: a "download" kind
        # LidarCardiffError is retryable, FAILURE_UNREACHABLE.
        assert failure.kind == FAILURE_UNREACHABLE
        assert "http" not in failure.reason.lower()
        assert DSM_ZIP_URL not in failure.reason
        assert DTM_ZIP_URL not in failure.reason


class _SecondSourceRecordingFetch:
    """A minimal second LayerSource, registered alongside lidar_cardiff in
    the pipeline-safety test below: its own `fetch_called` flag is the
    proof that `run_survey` actually reached it, which it never would if
    lidar_cardiff's own failure had re-raised immediately instead of
    being recorded and deferred.
    """

    id = "stub_second_source"
    display_name = "Stub Second Source"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self) -> None:
        self.fetch_called = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=0, seconds_estimate=0.0)

    def fetch(self, bbox, tiles, work_dir, progress, cancel=None):
        self.fetch_called = True
        return []

    def merge(self, parts, out_dir, stem):
        return []


def test_a_fetch_failure_is_deferred_not_fatal_through_a_real_run_survey(tmp_path, monkeypatch):
    """The review's own pipeline-safety proof, reproduced as a test: with
    tile_failures populated, a lidar_cardiff download failure must not
    take package.py's immediate-reraise path (SurveyRequest.force
    defaults to False). The second, later-ordered source must still run,
    and the run must end afterward with the deferred IncompleteSurveyError,
    the same contract lidar_wales.py and os_uprn.py already honour.
    """
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path / "lidar_cardiff_cache")
    clear_registry()
    try:
        register(LidarCardiffSource(session=_TransportFailureSession(), ostn15_cache_dir=tmp_path))
        second_source = _SecondSourceRecordingFetch()
        register(second_source)

        request = SurveyRequest(
            bbox=_any_bbox(),
            region="Test Region",
            site="Test Site",
            output_root=tmp_path / "packages",
            tile_size_m=2000.0,
            overlap_m=50.0,
            source_ids=("lidar_cardiff", "stub_second_source"),
            survey_date=date(2026, 8, 1),
            run_bridge_step=False,
        )

        with pytest.raises(IncompleteSurveyError) as excinfo:
            run_survey(request)

        assert second_source.fetch_called
        assert "http" not in str(excinfo.value).lower()
    finally:
        clear_registry()


# --------------------------------------------------------------------------
# merge(): archive members assembled into 25 cm rasters, budget refused
# honestly. Every test below builds its own small, real, zipfile-openable
# zip with synthetic ESRI ASCII grid members on the 0.25 m lattice (real
# xllcorner/yllcorner integer metres, matching the archive's own
# convention), never a 2000x2000 real-size member: the paste arithmetic
# is exercised for real regardless of a member's own size, only its
# corners and its overlap with the window matter.
#
# `source._bbox` is set directly rather than through a real fetch() call:
# merge()'s own protocol (sources/base.py's LayerSource) carries no bbox
# parameter, and lidar_cardiff.py's own module docstring ("merge(): no
# bbox in its own signature") documents fetch() as the one place that
# normally sets this same attribute. test_fetch_stores_bbox_on_self_for_
# merge_to_read_later above proves that wiring on its own, so setting it
# directly here tests merge() in isolation from fetch()'s own network and
# cache mechanics without testing anything fetch() has not already been
# proven to do.
# --------------------------------------------------------------------------


def _format_asc_member(
    xllcorner: float,
    yllcorner: float,
    ncols: int,
    nrows: int,
    cellsize: float,
    value_mm: float,
    nodata: float = -9999.0,
) -> str:
    """A real ESRI ASCII grid member's text: CRLF terminated like the
    archive's own members, `ncols * nrows` cells, every one of them
    `value_mm` millimetres uniformly (the archive's own units;
    `parse_asc(text, value_scale=0.001)` is what turns this back into
    metres). `xllcorner`/`yllcorner` are always whole numbers of metres
    in every fixture built with this helper, matching the real archive's
    own convention and the 0.25 m lattice `merge()` assumes throughout.
    """
    header = (
        f"ncols {ncols}\r\n"
        f"nrows {nrows}\r\n"
        f"xllcorner {xllcorner}\r\n"
        f"yllcorner {yllcorner}\r\n"
        f"cellsize {cellsize}\r\n"
        f"NODATA_value {nodata}\r\n"
    )
    row = " ".join([str(value_mm)] * ncols)
    rows = "\r\n".join([row] * nrows)
    return header + rows + "\r\n"


def _format_corrupt_asc_member(
    xllcorner: float, yllcorner: float, ncols: int, nrows: int, cellsize: float
) -> str:
    """A syntactically header-valid member whose own value rows hold a
    non-numeric token in every cell: `parse_asc_header` (which never
    looks past the header) reads it without complaint, but the full
    `parse_asc` parse must fail the moment it tries to convert the first
    token to a float.
    """
    header = (
        f"ncols {ncols}\r\n"
        f"nrows {nrows}\r\n"
        f"xllcorner {xllcorner}\r\n"
        f"yllcorner {yllcorner}\r\n"
        f"cellsize {cellsize}\r\n"
        f"NODATA_value -9999\r\n"
    )
    row = " ".join(["not-a-number"] * ncols)
    rows = "\r\n".join([row] * nrows)
    return header + rows + "\r\n"


def _build_member_zip(path: Path, members: dict[str, str]) -> None:
    """A real, `zipfile`-openable zip at `path`, one stored member per
    `members` (name -> already-formatted `.asc` text): the exact shape
    `merge()` itself opens with `zipfile.ZipFile` and walks via
    `namelist()`.
    """
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as archive:
        for name, text in members.items():
            archive.writestr(name, text)


def test_fetch_stores_bbox_on_self_for_merge_to_read_later(tmp_path, monkeypatch):
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path)
    _write_right_size_stub(tmp_path / DSM_ZIP_NAME, DSM_ZIP_BYTES)
    _write_right_size_stub(tmp_path / DTM_ZIP_NAME, DTM_ZIP_BYTES)
    source = LidarCardiffSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)

    source.fetch(bbox, [], tmp_path / "work", NullProgress())

    assert source._bbox is bbox


# --------------------------------------------------------------------------
# Task 5's bridge: fetch() writes a tiny pointer file per zip into
# work_dir, and merge() resolves a pointer back to the real cache_dir()
# path when it is not handed the zip directly by name. See the module
# docstring's own "Task 5's bridge" section for why this exists at all:
# package.py's real pipeline builds merge()'s parts from a work_dir
# LISTING (`_existing_output_files`), never fetch()'s own return value,
# and this source's two zips live in a national cache outside any one
# survey's own work_dir.
# --------------------------------------------------------------------------


def test_fetch_writes_a_cache_pointer_file_per_zip_into_work_dir(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: cache_dir)
    _write_right_size_stub(cache_dir / DSM_ZIP_NAME, DSM_ZIP_BYTES)
    _write_right_size_stub(cache_dir / DTM_ZIP_NAME, DTM_ZIP_BYTES)
    source = LidarCardiffSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    bbox = _bbox_for_padded_bng_rect(*_FULL_UNDER_BUDGET)
    work_dir = tmp_path / "work"

    result = source.fetch(bbox, [], work_dir, NullProgress())

    dsm_pointer = work_dir / lidar_cardiff._DSM_CACHE_POINTER_NAME
    dtm_pointer = work_dir / lidar_cardiff._DTM_CACHE_POINTER_NAME
    assert dsm_pointer.is_file()
    assert dtm_pointer.is_file()
    # Never empty: mapgen.merge.assert_inputs_present's own empty-file
    # check would otherwise mistake a pointer for a missing part.
    assert dsm_pointer.stat().st_size > 0
    assert dtm_pointer.stat().st_size > 0
    assert Path(dsm_pointer.read_text(encoding="utf-8").strip()) == result[0]
    assert Path(dtm_pointer.read_text(encoding="utf-8").strip()) == result[1]
    assert result == [cache_dir / DSM_ZIP_NAME, cache_dir / DTM_ZIP_NAME]


def test_fetch_writes_no_pointer_file_when_the_budget_gate_refuses(tmp_path, monkeypatch):
    """No partial output on a refusal path: the pointer files are written
    only after both `_ensure_zip` calls have already succeeded, so a
    budget refusal (which raises before either zip is even downloaded)
    must leave `work_dir` without either one.
    """
    monkeypatch.setattr(lidar_cardiff, "cache_dir", lambda: tmp_path / "cache")
    bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)
    source = LidarCardiffSource(session=_RefusesToConnect(), ostn15_cache_dir=tmp_path)
    work_dir = tmp_path / "work"

    with pytest.raises(LidarCardiffError) as excinfo:
        source.fetch(
            bbox,
            [Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)],
            work_dir,
            NullProgress(),
        )

    assert excinfo.value.kind == "budget"
    assert not work_dir.exists()


def test_merge_resolves_a_cache_pointer_back_to_the_real_zip_path(tmp_path, monkeypatch):
    """`parts` holding the two work_dir pointer files, exactly what
    package.py's real pipeline hands `merge()` today, resolves back to
    the real zips in `cache_dir()` and produces the identical output a
    direct-by-name call (every other merge() test in this file) does.
    """
    _patch_padded_extent(monkeypatch, _SEAM_WINDOW_RECT)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True)
    dsm_zip, dtm_zip = _seam_test_zips(cache_dir)
    dsm_pointer = tmp_path / "work" / lidar_cardiff._DSM_CACHE_POINTER_NAME
    dtm_pointer = tmp_path / "work" / lidar_cardiff._DTM_CACHE_POINTER_NAME
    dsm_pointer.parent.mkdir(parents=True)
    dsm_pointer.write_text(str(dsm_zip), encoding="utf-8")
    dtm_pointer.write_text(str(dtm_zip), encoding="utf-8")

    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()

    package_dir = tmp_path / "package"
    outputs = source.merge([dsm_pointer, dtm_pointer], package_dir, "TestSite")

    assert outputs == [
        package_dir / "TestSite_lidar25_dsm.tif",
        package_dir / "TestSite_lidar25_dtm.tif",
    ]
    dsm_window = read_full_window(CogReader.open(FileByteSource(outputs[0])))
    # Well inside the west member, away from the seam (1.0 m, per
    # _seam_test_zips): the identical point
    # test_merge_pastes_two_members_with_an_exact_seam already samples
    # for the direct-by-name path, run here through the pointer path
    # instead.
    assert dsm_window.sample_bng(311498.5, 177011.0) == pytest.approx(1.0, abs=1e-4)


def test_merge_raises_a_named_error_for_a_stale_pointer_whose_target_is_gone(tmp_path):
    """A review finding: a pointer file that is present and non-empty
    (so `mapgen.merge.assert_inputs_present` sees no problem at all) but
    whose own recorded cache target has since been deleted (the exact
    cross-process gap this bridge exists to survive: some other process
    cleared `cache_dir()` between a `fetch()` and a later, resumed
    `merge()`) used to surface as a bare, un-kinded `FileNotFoundError`
    two calls later, deep inside `_assemble_window`'s own
    `zipfile.ZipFile()` call. Now it raises a named `LidarCardiffError`,
    kind `"download"` (the healing action is a fresh `fetch()`, not
    something `merge()` alone can fix), before either zip is ever opened.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True)
    dsm_pointer = work_dir / lidar_cardiff._DSM_CACHE_POINTER_NAME
    dtm_pointer = work_dir / lidar_cardiff._DTM_CACHE_POINTER_NAME
    dsm_pointer.write_text(str(tmp_path / "cache" / "gone_dsm.zip"), encoding="utf-8")
    dtm_pointer.write_text(str(tmp_path / "cache" / "gone_dtm.zip"), encoding="utf-8")

    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()

    with pytest.raises(LidarCardiffError) as excinfo:
        source.merge([dsm_pointer, dtm_pointer], tmp_path / "package", "TestSite")

    assert excinfo.value.kind == "download"
    assert "http" not in str(excinfo.value).lower()
    # Names the pointer's own real file name, never the corruption- or
    # attacker-controlled path text it recorded.
    assert lidar_cardiff._DSM_CACHE_POINTER_NAME in str(excinfo.value)
    assert "gone_dsm.zip" not in str(excinfo.value)


def test_merge_raises_a_named_error_for_a_pointer_with_garbage_content(tmp_path):
    """The second review-found failure mode: a pointer file whose own
    text is not a usable path at all (an embedded NUL byte, the one case
    this project's own filesystem calls refuse outright) used to surface
    as a bare, un-kinded `ValueError`. Now it raises a named
    `LidarCardiffError`, kind `"parse"`, checked explicitly before the
    text is ever handed to a filesystem call: `Path.is_file()` itself
    answers a NUL-bearing path with a plain `False` on this project's own
    Windows target rather than raising (probed directly against this
    interpreter), which would otherwise fold this case into `"download"`
    and lose the distinction.
    """
    work_dir = tmp_path / "work"
    work_dir.mkdir(parents=True)
    dsm_pointer = work_dir / lidar_cardiff._DSM_CACHE_POINTER_NAME
    dtm_pointer = work_dir / lidar_cardiff._DTM_CACHE_POINTER_NAME
    dsm_pointer.write_text("bad\x00path.zip", encoding="utf-8")
    dtm_pointer.write_text(str(tmp_path / "cache" / DTM_ZIP_NAME), encoding="utf-8")

    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()

    with pytest.raises(LidarCardiffError) as excinfo:
        source.merge([dsm_pointer, dtm_pointer], tmp_path / "package", "TestSite")

    assert excinfo.value.kind == "parse"
    assert "http" not in str(excinfo.value).lower()
    assert lidar_cardiff._DSM_CACHE_POINTER_NAME in str(excinfo.value)


def _patch_padded_extent(monkeypatch, rect: tuple[float, float, float, float]) -> None:
    """Forces `merge()`'s own `best_effort_padded_bng_extent(bbox,
    PAD_METRES, ...)` call to answer `rect` regardless of `bbox`.

    `_bbox_for_padded_bng_rect` (top of this file) reproduces a target
    rectangle by SUBTRACTING `PAD_METRES` from every edge and relying on
    the real padding arithmetic to add it back; that only holds up for a
    rectangle comfortably larger than `2 * PAD_METRES` (400 m) in both
    dimensions; PAD_METRES itself is 200 m). Every merge() fixture below
    is a handful of metres across, deliberately small so its own NaN
    array and paste loop stay cheap, so this patches the one function
    that turns a bbox into a BNG rectangle directly instead, which is
    exact regardless of how small the target window is and needs no
    real bbox at all: `source._bbox` can be set to anything once this is
    in place, since nothing downstream of it looks at the bbox's own
    coordinates any more.
    """
    monkeypatch.setattr(
        lidar_cardiff,
        "best_effort_padded_bng_extent",
        lambda bbox, pad, cache_dir=None: rect,
    )


# A window spanning two members, meeting exactly at e=311500 (inside
# ST1177SW, 311000-311500 x 177000-177500, whose own east neighbour
# ST1177SE, 311500-312000 x 177000-177500, is also a real COVERAGE_TILES
# entry): 4 x 2 m at 0.25 m is 16 x 8 pixels, columns 0-7 from the west
# member and 8-15 from the east one.
_SEAM_WINDOW_RECT = (311498.0, 177010.0, 311502.0, 177012.0)


def _seam_test_zips(tmp_path: Path) -> tuple[Path, Path]:
    west = _format_asc_member(311498.0, 177010.0, 8, 8, 0.25, 1000)  # -> 1.0 m
    east = _format_asc_member(311500.0, 177010.0, 8, 8, 0.25, 2000)  # -> 2.0 m
    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    _build_member_zip(dsm_zip, {"dsm_west.asc": west, "dsm_east.asc": east})
    _build_member_zip(dtm_zip, {"dtm_west.asc": west, "dtm_east.asc": east})
    return dsm_zip, dtm_zip


def test_merge_pastes_two_members_with_an_exact_seam(tmp_path, monkeypatch):
    _patch_padded_extent(monkeypatch, _SEAM_WINDOW_RECT)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()
    dsm_zip, dtm_zip = _seam_test_zips(tmp_path)

    package_dir = tmp_path / "package"
    outputs = source.merge([dsm_zip, dtm_zip], package_dir, "TestSite")

    dsm_output = package_dir / "TestSite_lidar25_dsm.tif"
    dtm_output = package_dir / "TestSite_lidar25_dtm.tif"
    assert outputs == [dsm_output, dtm_output]

    reader = CogReader.open(FileByteSource(dsm_output))
    window = read_full_window(reader)
    assert window.width == 16
    assert window.height == 8

    # Well inside each member, away from the seam, where every
    # surrounding pixel belongs to the same one.
    assert window.sample_bng(311498.5, 177011.0) == pytest.approx(1.0, abs=1e-4)
    assert window.sample_bng(311501.5, 177011.0) == pytest.approx(2.0, abs=1e-4)

    # Exactly on the seam (e=311500, the members' own shared boundary):
    # bilinear interpolation splits evenly between the west member's own
    # last column (1.0) and the east member's own first column (2.0).
    # This is the proof the seam itself is exact: a duplicated or a
    # missing column here would land on 1.0, on 2.0, or on a different
    # split, never precisely 1.5.
    assert window.sample_bng(311500.0, 177011.0) == pytest.approx(1.5, abs=1e-4)


# 4 x 4 m inside ST1177SW, comfortably clear of every tile edge, sized to
# match the fixture member exactly (16 x 16 px) so the window holds no
# NaN at all and the sampled point below is not near any boundary.
_MM_SCALE_WINDOW_RECT = (311100.0, 177100.0, 311104.0, 177104.0)


def test_merge_converts_millimetres_to_metres(tmp_path, monkeypatch):
    _patch_padded_extent(monkeypatch, _MM_SCALE_WINDOW_RECT)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()
    member = _format_asc_member(311100.0, 177100.0, 16, 16, 0.25, 85321)
    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    _build_member_zip(dsm_zip, {"dsm.asc": member})
    _build_member_zip(dtm_zip, {"dtm.asc": member})

    package_dir = tmp_path / "package"
    source.merge([dsm_zip, dtm_zip], package_dir, "TestSite")

    reader = CogReader.open(FileByteSource(package_dir / "TestSite_lidar25_dsm.tif"))
    window = read_full_window(reader)
    assert window.sample_bng(311102.0, 177102.0) == pytest.approx(85.321, rel=1e-5)


# 4 x 4 m inside ST1177SW, well away from _SKIP_TEST fixtures above.
_SKIP_TEST_WINDOW_RECT = (311050.0, 177050.0, 311054.0, 177054.0)


def test_merge_never_fully_parses_a_member_wholly_outside_the_window(tmp_path, monkeypatch):
    _patch_padded_extent(monkeypatch, _SKIP_TEST_WINDOW_RECT)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()

    inside = _format_asc_member(311050.0, 177050.0, 16, 16, 0.25, 1000)
    # Inside ST1076NE (310500-311000 x 176500-177000), nowhere near the
    # window above: parse_asc_header must still be allowed to read this
    # member's own header (that is what tells merge() to skip it), but
    # the full, float-converting parse_asc must never run on it.
    outside = _format_asc_member(310600.0, 176600.0, 8, 8, 0.25, 2000)

    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    _build_member_zip(dsm_zip, {"dsm_inside.asc": inside, "dsm_outside.asc": outside})
    _build_member_zip(dtm_zip, {"dtm_inside.asc": inside, "dtm_outside.asc": outside})

    real_parse_asc = lidar_cardiff.parse_asc
    calls: list[str] = []

    def _counting_parse_asc(text, value_scale=1.0):
        calls.append(text)
        return real_parse_asc(text, value_scale=value_scale)

    monkeypatch.setattr(lidar_cardiff, "parse_asc", _counting_parse_asc)

    source.merge([dsm_zip, dtm_zip], tmp_path / "package", "TestSite")

    # One call per zip (the "inside" member only); the "outside" member
    # is skipped on its header alone in both.
    assert len(calls) == 2
    assert all("1000" in text and "2000" not in text for text in calls)


def test_merge_refuses_over_budget_with_the_pixel_count_substituted(tmp_path):
    bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)
    pixels = _window_pixels(bbox, tmp_path)
    # Sanity: this fixture really is the ~48 million pixel, over-budget
    # extent test_window_pixels_over_the_whole_block_is_48_million_and_
    # over_budget already pins; the exact int can drift by a handful of
    # pixels from the tm_inverse/tm_forward round trip, which is why the
    # message assertion below re-derives it rather than hard-coding
    # "48,000,000".
    assert pixels == pytest.approx(48_000_000, rel=1e-3)
    assert pixels > MAX_WINDOW_PIXELS

    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = bbox
    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME

    with pytest.raises(LidarCardiffError) as excinfo:
        source.merge([dsm_zip, dtm_zip], tmp_path / "package", "TestSite")

    assert excinfo.value.kind == "budget"
    # The item B lesson: pin the SUBSTITUTION, not just the surrounding
    # prose. The thousands separator is part of the pinned reason string
    # itself, not incidental formatting.
    assert "," in f"{pixels:,}"
    assert str(excinfo.value) == (
        f"this extent needs {pixels:,} pixels at 25 cm and the raster "
        f"budget is 16,777,216; extents under about 1 x 1 km inside the "
        f"covered block come back at 25 cm"
    )


def test_merge_refuses_over_budget_before_opening_either_zip_and_writes_no_file(
    tmp_path, monkeypatch
):
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _bbox_for_padded_bng_rect(*_WHOLE_BLOCK_PARTIAL_OVER_BUDGET)

    calls: list[str] = []
    monkeypatch.setattr(
        lidar_cardiff, "parse_asc", lambda text, value_scale=1.0: calls.append(text)
    )

    # Never created: proof the budget gate never even tries to open
    # either zip, let alone parse a member out of it.
    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    assert not dsm_zip.exists()
    assert not dtm_zip.exists()

    package_dir = tmp_path / "package"
    with pytest.raises(LidarCardiffError) as excinfo:
        source.merge([dsm_zip, dtm_zip], package_dir, "TestSite")

    assert excinfo.value.kind == "budget"
    assert calls == []
    assert not (package_dir / "TestSite_lidar25_dsm.tif").exists()
    assert not (package_dir / "TestSite_lidar25_dtm.tif").exists()


# Straddles ST1277SW's own south edge (n=177000, 312000-312500 x
# 177000-177500 is real coverage) into the 500 m cell south of it
# (312000-312500 x 176500-177000), which the module docstring names as
# one of the two gaps beside ST1277SW that COVERAGE_TILES does not
# include: no archive member is ever published for it.
_PARTIAL_COVERAGE_WINDOW_RECT = (312100.0, 176996.0, 312104.0, 177004.0)


def test_merge_leaves_nan_exactly_where_no_member_covers_the_window(tmp_path, monkeypatch):
    _patch_padded_extent(monkeypatch, _PARTIAL_COVERAGE_WINDOW_RECT)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()

    # Covers only the window's own northern half (n=177000-177004, inside
    # ST1277SW); the southern half (176996-177000) has no member at all.
    member = _format_asc_member(312100.0, 177000.0, 16, 16, 0.25, 100_000)  # 100.0 m
    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    _build_member_zip(dsm_zip, {"dsm.asc": member})
    _build_member_zip(dtm_zip, {"dtm.asc": member})

    package_dir = tmp_path / "package"
    source.merge([dsm_zip, dtm_zip], package_dir, "TestSite")

    reader = CogReader.open(FileByteSource(package_dir / "TestSite_lidar25_dsm.tif"))
    window = read_full_window(reader)

    # Well inside the covered northern half.
    assert window.sample_bng(312102.0, 177002.0) == pytest.approx(100.0, abs=1e-3)

    # Well inside the uncovered gap: every one of its four surrounding
    # pixels is NaN, so sample_bng answers None rather than a fabricated
    # height.
    assert window.sample_bng(312102.0, 176998.0) is None


# 4 x 4 m inside ST1177SW, well clear of every other fixture above.
_CORRUPT_MEMBER_WINDOW_RECT = (311200.0, 177200.0, 311204.0, 177204.0)


def test_merge_fails_the_whole_merge_on_a_corrupt_member(tmp_path, monkeypatch):
    _patch_padded_extent(monkeypatch, _CORRUPT_MEMBER_WINDOW_RECT)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()

    good_member = _format_asc_member(311200.0, 177200.0, 16, 16, 0.25, 1000)
    corrupt_member = _format_corrupt_asc_member(311200.0, 177200.0, 16, 16, 0.25)

    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    # The DSM zip's own member is corrupt; the DTM zip's is fine. Both
    # outputs must still end up missing: neither is written until both
    # have been fully, successfully assembled.
    _build_member_zip(dsm_zip, {"dsm.asc": corrupt_member})
    _build_member_zip(dtm_zip, {"dtm.asc": good_member})

    package_dir = tmp_path / "package"
    with pytest.raises(LidarCardiffError) as excinfo:
        source.merge([dsm_zip, dtm_zip], package_dir, "TestSite")

    assert excinfo.value.kind == "parse"
    assert not (package_dir / "TestSite_lidar25_dsm.tif").exists()
    assert not (package_dir / "TestSite_lidar25_dtm.tif").exists()


def test_merge_without_a_prior_fetch_raises_a_named_error_not_a_bare_crash(tmp_path):
    # Review Important #3: a fresh source, never fetch()ed, used to crash
    # merge() with a bare AttributeError from deep inside the BNG
    # projection code (self._bbox is None, and best_effort_padded_bng_
    # extent has no bbox to read a corner off). Every other failure mode
    # in this module raises a named LidarCardiffError; this one must too.
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    assert source._bbox is None

    with pytest.raises(LidarCardiffError) as excinfo:
        source.merge(
            [tmp_path / DSM_ZIP_NAME, tmp_path / DTM_ZIP_NAME],
            tmp_path / "package",
            "TestSite",
        )

    assert excinfo.value.kind == "parse"
    assert "fetch" in str(excinfo.value).lower()
    assert "http" not in str(excinfo.value).lower()


# 4 x 4 m inside ST1177SW, matching the mm-scale fixture's own member
# exactly: reused here only because its geometry is already proven
# gap-free, not because this test cares about the mm scaling itself.
_WRITE_FAILURE_WINDOW_RECT = _MM_SCALE_WINDOW_RECT


def test_merge_leaves_no_files_when_the_second_write_fails(tmp_path, monkeypatch):
    # Review Important #1: write_bng_geotiff's own atomic-write contract
    # covers ONE file; a failure between the DSM write and the DTM write
    # used to leave a lone, complete DSM tif on disk with no DTM sibling,
    # contradicting this module's own "never one without the other"
    # claim. Simulates the failure the owner's own machine has hit
    # before (disk full, an antivirus lock) by monkeypatching
    # write_bng_geotiff to fail on its second call only, with both
    # member parses genuinely successful beforehand.
    _patch_padded_extent(monkeypatch, _WRITE_FAILURE_WINDOW_RECT)
    source = LidarCardiffSource(ostn15_cache_dir=tmp_path)
    source._bbox = _any_bbox()
    member = _format_asc_member(311100.0, 177100.0, 16, 16, 0.25, 85321)
    dsm_zip = tmp_path / DSM_ZIP_NAME
    dtm_zip = tmp_path / DTM_ZIP_NAME
    _build_member_zip(dsm_zip, {"dsm.asc": member})
    _build_member_zip(dtm_zip, {"dtm.asc": member})

    real_write_bng_geotiff = lidar_cardiff.write_bng_geotiff
    calls: list[Path] = []

    def _fail_on_second_write(path, window):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("simulated disk-full on the second write")
        real_write_bng_geotiff(path, window)

    monkeypatch.setattr(lidar_cardiff, "write_bng_geotiff", _fail_on_second_write)

    package_dir = tmp_path / "package"
    with pytest.raises(OSError):
        source.merge([dsm_zip, dtm_zip], package_dir, "TestSite")

    assert len(calls) == 2  # both writes were attempted; the second failed.
    assert not (package_dir / "TestSite_lidar25_dsm.tif").exists()
    assert not (package_dir / "TestSite_lidar25_dtm.tif").exists()
    # No temp litter either: the first write's own temp file is cleaned
    # up when the second one fails, and a failed write_bng_geotiff never
    # leaves its own destination behind (fsutil.atomic_write_bytes' own
    # contract), so package_dir holds nothing at all.
    leftover = list(package_dir.iterdir()) if package_dir.exists() else []
    assert leftover == []


@pytest.mark.live
def test_live_fetch_downloads_both_real_zips_into_the_real_cache():
    # No cache_dir monkeypatch here, deliberately: this is the one test in
    # this file that touches the owner's real ~/.mapgen/lidar_cardiff, on
    # purpose (the brief's own "into the REAL cache directory" wording),
    # so a later run of the full suite (this test still deselected by
    # "not live") finds both zips already cached.
    directory = lidar_cardiff.cache_dir()
    source = LidarCardiffSource()

    result = source.fetch(_any_bbox(), [], directory, NullProgress())

    assert result == [directory / DSM_ZIP_NAME, directory / DTM_ZIP_NAME]
    assert result[0].stat().st_size == DSM_ZIP_BYTES
    assert result[1].stat().st_size == DTM_ZIP_BYTES
    with zipfile.ZipFile(result[0]) as archive:
        assert len(archive.namelist()) == 10
    with zipfile.ZipFile(result[1]) as archive:
        assert len(archive.namelist()) == 10

    # Subsequent runs find them cached: re-fetching with a network-
    # refusing session must succeed without downloading anything again.
    cached_source = LidarCardiffSource(session=_RefusesToConnect())
    result_again = cached_source.fetch(_any_bbox(), [], directory, NullProgress())
    assert result_again == result
