"""os_shards.py's suite: GB grid maths and the derive-once shard store.

Grid maths tests (Step 1/2) pin exact anchors this task's own brief names,
each independently checkable against Ordnance Survey's own real square
names: SS and ST across their shared 100 km seam, HP (the Shetland square
this project's own OpenRoads probe named), and the 10 km cell of the real
GreenspaceSite sample os_gml.py's own fixture carries.

Shard store tests (Step 3/4) round-trip OsFeature records through
write_shards/features_in, and separately pin the exact behaviour a
reviewer named for verification (task-2-review.md's own note carried
forward in progress.md): Task 2's readers fail the WHOLE stream on any
malformed feature, an exception escaping mid-iteration, so write_shards
consuming such an iterator must (a) let that exception propagate after
closing every open gzip handle cleanly and (b) leave no meta.json behind,
which is what makes shards_complete's False verdict the safe, correct
signal for "rebuild this".

UPRN variant tests (Step 5) round-trip the fixture CSV (tests/fixtures/
osopen/uprn_sample.csv, built by make_uprn_fixture.py), including the one
real OpenUPRN row this task's own brief names verbatim.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest

from mapgen import os_shards
from mapgen.os_downloads import OsOpenError
from mapgen.os_gml import OsFeature

_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "osopen"
_UPRN_SAMPLE = _FIXTURE_DIR / "uprn_sample.csv"


# --------------------------------------------------------------------------
# Grid maths: grid_square, cell_10km, squares_for, cells_for.
# --------------------------------------------------------------------------


def test_grid_square_ss_below_the_seam():
    assert os_shards.grid_square(299000, 179000) == "SS"


def test_grid_square_st_above_the_seam():
    assert os_shards.grid_square(301000, 179000) == "ST"


def test_grid_square_hp_the_shetland_square():
    assert os_shards.grid_square(451000, 1215000) == "HP"


def test_cell_10km_matches_the_real_greenspace_sample():
    # easting 297393 -> tens-of-km digit 9, northing 100106 -> digit 0,
    # the real GreenspaceSite coordinate os_gml.py's own fixture carries.
    assert os_shards.cell_10km(297393, 100106) == "SS90"


def test_squares_for_across_the_ss_st_seam_returns_both():
    squares = os_shards.squares_for(299000, 179000, 301000, 179000)
    assert squares == ["SS", "ST"]


def test_squares_for_within_one_square_returns_only_that_square():
    squares = os_shards.squares_for(291000, 171000, 295000, 175000)
    assert squares == ["SS"]


def test_cells_for_across_a_10km_seam_returns_both_cells():
    # bbox 248000-252000 easting straddles the 250000 boundary between
    # cell digit 4 and digit 5 within square SS, northing held inside one
    # 10km band (digit 7).
    cells = os_shards.cells_for(248000, 170000, 252000, 172000)
    assert cells == ["SS47", "SS57"]


def test_cells_for_within_one_cell_returns_only_that_cell():
    # easting 291000-293000 -> tens-of-km digit 9 throughout; northing
    # 171000-173000 -> digit 7 throughout: one cell, "SS97".
    cells = os_shards.cells_for(291000, 171000, 293000, 173000)
    assert cells == ["SS97"]


def test_grid_square_out_of_the_representable_grid_raises():
    # Nothing fabricated: a coordinate arithmetic cannot place inside the
    # 5x5 letter grid must be reported, not silently mapped to a wrong
    # letter or truncated into range. 3,000,000 puts the 500km column
    # index (easting // 500000 == 6) six squares past the grid's own
    # rightmost column.
    with pytest.raises(ValueError):
        os_shards.grid_square(3_000_000, 179000)


# --------------------------------------------------------------------------
# Review round 1, Important: grid_square must validate against the National
# Grid's own usable envelope (0 <= easting < 700000, 0 <= northing <
# 1300000), not only against the arithmetic 5x5 super-grid. Floor division
# on a moderately out-of-range or negative coordinate frequently still
# lands inside the letter grid's own [0, 4] bounds and produces a
# plausible-looking, fabricated two-letter code instead of raising.
# --------------------------------------------------------------------------


def test_grid_square_negative_northing_raises():
    with pytest.raises(ValueError):
        os_shards.grid_square(300000, -50000)


def test_grid_square_northing_past_the_grid_raises():
    with pytest.raises(ValueError):
        os_shards.grid_square(300000, 1400000)


def test_grid_square_negative_easting_raises():
    with pytest.raises(ValueError):
        os_shards.grid_square(-50000, 300000)


def test_grid_square_valid_low_corner_is_sv():
    assert os_shards.grid_square(0, 0) == "SV"


def test_grid_square_valid_high_corner_does_not_raise():
    # The highest coordinate still inside the envelope on both axes
    # (700000, 1300000 are themselves the exclusive upper bounds).
    assert os_shards.grid_square(699999, 1299999) == "JM"


# --------------------------------------------------------------------------
# Fixtures for the shard store tests: five OsFeature records, two 10km
# cells (SS47, SS57), one polygon straddling both.
# --------------------------------------------------------------------------


def _feature(feature_id: str, feature_type: str, coordinates: list) -> OsFeature:
    return OsFeature(
        feature_id=feature_id,
        feature_type=feature_type,
        geometry={"type": "Polygon", "coordinates": [coordinates]},
        properties={"code": "15014"},
    )


def _five_features() -> list[OsFeature]:
    # Two features wholly inside SS47 (easting 240000-250000, northing
    # 170000-180000 band), two wholly inside SS57 (easting 250000-260000),
    # and one straddling both cells' shared seam at easting 250000.
    ss47_a = _feature(
        "idAAAA0001",
        "Building",
        [[241000, 171000], [242000, 171000], [242000, 172000], [241000, 172000], [241000, 171000]],
    )
    ss47_b = _feature(
        "idAAAA0002",
        "Building",
        [[243000, 171000], [244000, 171000], [244000, 172000], [243000, 172000], [243000, 171000]],
    )
    ss57_a = _feature(
        "idAAAA0003",
        "Building",
        [[251000, 171000], [252000, 171000], [252000, 172000], [251000, 172000], [251000, 171000]],
    )
    ss57_b = _feature(
        "idAAAA0004",
        "Building",
        [[253000, 171000], [254000, 171000], [254000, 172000], [253000, 172000], [253000, 171000]],
    )
    straddler = _feature(
        "idAAAA0005",
        "Building",
        [[248000, 170000], [252000, 170000], [252000, 172000], [248000, 172000], [248000, 170000]],
    )
    return [ss47_a, ss47_b, ss57_a, ss57_b, straddler]


def test_write_shards_meta_totals_and_cell_counts(tmp_path):
    meta = os_shards.write_shards(iter(_five_features()), tmp_path)
    assert meta == {"total": 5, "cells": {"SS47": 3, "SS57": 3}}


def test_write_shards_writes_gzip_ndjson_per_cell(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    assert (tmp_path / "SS47.ndjson.gz").exists()
    assert (tmp_path / "SS57.ndjson.gz").exists()
    with gzip.open(tmp_path / "SS47.ndjson.gz", "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    assert len(lines) == 3
    ids = {line["id"] for line in lines}
    assert ids == {"idAAAA0001", "idAAAA0002", "idAAAA0005"}


def test_write_shards_writes_meta_json_last(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    assert (tmp_path / "meta.json").exists()
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"total": 5, "cells": {"SS47": 3, "SS57": 3}}


def test_features_in_over_both_cells_dedups_the_straddler(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    results = list(os_shards.features_in(tmp_path, 240000, 170000, 260000, 172000))
    ids = [record["id"] for record in results]
    assert sorted(ids) == [
        "idAAAA0001",
        "idAAAA0002",
        "idAAAA0003",
        "idAAAA0004",
        "idAAAA0005",
    ]
    assert len(ids) == len(set(ids))


def test_features_in_over_one_cell_excludes_the_others(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    results = list(os_shards.features_in(tmp_path, 241000, 171000, 244000, 172000))
    ids = {record["id"] for record in results}
    # SS47's own two features come back; nothing from SS57 does, because
    # that cell's file is never even opened for a query bbox that never
    # reaches it.
    assert ids == {"idAAAA0001", "idAAAA0002"}


def test_features_in_yields_plain_dicts_with_the_four_expected_keys(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    record = next(os_shards.features_in(tmp_path, 241000, 171000, 242000, 172000))
    assert set(record) == {"id", "type", "geometry", "properties"}
    assert record["type"] == "Building"
    assert record["properties"] == {"code": "15014"}


def test_features_in_over_a_missing_cell_yields_nothing(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    # Far outside both SS47 and SS57: no cell file exists for this query.
    results = list(os_shards.features_in(tmp_path, 900000, 900000, 901000, 901000))
    assert results == []


# --------------------------------------------------------------------------
# Dedup contract: id "" is never deduped against another id "" feature.
# --------------------------------------------------------------------------


def test_two_distinct_empty_id_features_in_one_cell_both_come_through(tmp_path):
    a = _feature(
        "",
        "Woodland",
        [[241000, 171000], [241500, 171000], [241500, 171500], [241000, 171500], [241000, 171000]],
    )
    b = _feature(
        "",
        "Woodland",
        [[241600, 171000], [242000, 171000], [242000, 171500], [241600, 171500], [241600, 171000]],
    )
    os_shards.write_shards(iter([a, b]), tmp_path)
    results = list(os_shards.features_in(tmp_path, 240000, 170000, 243000, 172000))
    assert len(results) == 2
    assert all(record["id"] == "" for record in results)


# --------------------------------------------------------------------------
# shards_complete: the biconditional (meta.json's own existence, and every
# cell/square it names actually present on disk).
# --------------------------------------------------------------------------


def test_shards_complete_false_when_meta_json_is_absent(tmp_path):
    assert os_shards.shards_complete(tmp_path) is False


def test_shards_complete_true_on_the_happy_path(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    assert os_shards.shards_complete(tmp_path) is True


def test_shards_complete_false_when_meta_lists_a_missing_cell(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    (tmp_path / "SS47.ndjson.gz").unlink()
    assert os_shards.shards_complete(tmp_path) is False


def test_shards_complete_false_when_cell_files_exist_but_meta_does_not(tmp_path):
    os_shards.write_shards(iter(_five_features()), tmp_path)
    (tmp_path / "meta.json").unlink()
    assert os_shards.shards_complete(tmp_path) is False


# --------------------------------------------------------------------------
# The mid-stream failure a reviewer specifically flagged for verification:
# Task 2's readers fail the WHOLE stream on any malformed feature, so this
# is exactly the shape write_shards will actually be handed in production.
# --------------------------------------------------------------------------


def _features_that_fail_partway():
    features = _five_features()
    yield features[0]
    yield features[1]
    raise OsOpenError("this stream is corrupt partway through", kind="parse")


def test_a_mid_stream_os_open_error_propagates_out_of_write_shards(tmp_path):
    with pytest.raises(OsOpenError):
        os_shards.write_shards(_features_that_fail_partway(), tmp_path)


def test_a_mid_stream_failure_leaves_no_meta_json(tmp_path):
    with pytest.raises(OsOpenError):
        os_shards.write_shards(_features_that_fail_partway(), tmp_path)
    assert not (tmp_path / "meta.json").exists()


def test_a_mid_stream_failure_leaves_the_shard_dir_incomplete(tmp_path):
    with pytest.raises(OsOpenError):
        os_shards.write_shards(_features_that_fail_partway(), tmp_path)
    assert os_shards.shards_complete(tmp_path) is False


# --------------------------------------------------------------------------
# Review round 1, Critical: a fail-then-retry pair whose two attempts touch
# DIFFERING cells must never let the failed attempt's own orphaned cell
# file leak through a later, successful attempt's features_in/uprn_in, even
# though shards_complete only ever checks what the SUCCESSFUL attempt's own
# meta.json lists. Reproduces the reviewer's own repro exactly: first
# attempt writes into SS57 then raises (no meta.json, SS57.ndjson.gz stays
# on disk); retry into the SAME shard_dir writes only into SS47 and
# succeeds. Before the fix, shards_complete answered True and features_in
# over a bbox covering both cells returned the retry's own feature AND the
# first attempt's orphaned "phantom1".
# --------------------------------------------------------------------------


def _one_feature_in_ss57(feature_id: str) -> OsFeature:
    return _feature(
        feature_id,
        "Building",
        [[251000, 171000], [252000, 171000], [252000, 172000], [251000, 172000], [251000, 171000]],
    )


def _one_feature_in_ss47(feature_id: str) -> OsFeature:
    return _feature(
        feature_id,
        "Building",
        [[241000, 171000], [242000, 171000], [242000, 172000], [241000, 172000], [241000, 171000]],
    )


def _failed_first_attempt():
    yield _one_feature_in_ss57("phantom1")
    raise OsOpenError("this stream is corrupt partway through", kind="parse")


def test_retry_with_differing_cells_leaves_no_orphan_file_on_disk(tmp_path):
    with pytest.raises(OsOpenError):
        os_shards.write_shards(_failed_first_attempt(), tmp_path)
    assert (tmp_path / "SS57.ndjson.gz").exists()  # the failed attempt's own leftover

    os_shards.write_shards(iter([_one_feature_in_ss47("keepme")]), tmp_path)

    # The retry never touches SS57 at all; its own leftover file from the
    # failed attempt must not survive the retry's own fresh write.
    assert not (tmp_path / "SS57.ndjson.gz").exists()


def test_retry_with_differing_cells_reports_complete_and_leaks_no_phantom(tmp_path):
    with pytest.raises(OsOpenError):
        os_shards.write_shards(_failed_first_attempt(), tmp_path)

    meta = os_shards.write_shards(iter([_one_feature_in_ss47("keepme")]), tmp_path)
    assert meta == {"total": 1, "cells": {"SS47": 1}}
    assert os_shards.shards_complete(tmp_path) is True

    results = list(os_shards.features_in(tmp_path, 240000, 170000, 260000, 172000))
    ids = [record["id"] for record in results]
    assert ids == ["keepme"]
    assert "phantom1" not in ids


def test_features_in_ignores_a_cell_file_present_on_disk_but_not_listed_in_meta(tmp_path):
    # Isolates the READER's own half of the belt-and-braces fix from the
    # WRITER's own sweep: this file is planted directly, after a normal,
    # complete write_shards call, so no write_shards sweep ever runs
    # again to clear it. features_in must still refuse it purely because
    # meta.json does not name it, even though cells_for's own lattice walk
    # would otherwise find it and its bytes are perfectly valid gzip.
    os_shards.write_shards(iter([_one_feature_in_ss47("keepme")]), tmp_path)
    phantom_record = json.dumps(
        {"id": "phantom-planted", "type": "Building", "geometry": _one_feature_in_ss57("x").geometry, "properties": {}},
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    with gzip.open(tmp_path / "SS57.ndjson.gz", "wb") as handle:
        handle.write(phantom_record)

    results = list(os_shards.features_in(tmp_path, 240000, 170000, 260000, 172000))
    ids = [record["id"] for record in results]
    assert ids == ["keepme"]
    assert "phantom-planted" not in ids


def _failed_first_uprn_attempt():
    yield "UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE\n"
    yield "999,251000.0,171500.0,51.0,-3.0\n"  # square SS
    raise RuntimeError("this stream failed partway through")


def test_uprn_retry_with_differing_squares_leaves_no_orphan_file(tmp_path):
    with pytest.raises(RuntimeError):
        os_shards.write_uprn_shards(_failed_first_uprn_attempt(), tmp_path)
    assert (tmp_path / "SS.csv.gz").exists()  # the failed attempt's own leftover

    retry_text = (
        "UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE\n"
        "1,358260.99,172796.83,51.4526038,-2.6020703\n"  # square ST only
    )
    os_shards.write_uprn_shards(iter(retry_text.splitlines(keepends=True)), tmp_path)

    assert not (tmp_path / "SS.csv.gz").exists()


def test_uprn_retry_with_differing_squares_reports_complete_and_leaks_no_phantom(tmp_path):
    with pytest.raises(RuntimeError):
        os_shards.write_uprn_shards(_failed_first_uprn_attempt(), tmp_path)

    retry_text = (
        "UPRN,X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE\n"
        "1,358260.99,172796.83,51.4526038,-2.6020703\n"
    )
    meta = os_shards.write_uprn_shards(iter(retry_text.splitlines(keepends=True)), tmp_path)
    assert meta == {"total": 1, "squares": {"ST": 1}}
    assert os_shards.shards_complete(tmp_path) is True

    rows = list(os_shards.uprn_in(tmp_path, 0, 0, 900000, 900000))
    assert [row[0] for row in rows] == [1]


# --------------------------------------------------------------------------
# UPRN variant: write_uprn_shards, uprn_in, over the committed fixture CSV.
# --------------------------------------------------------------------------


def test_write_uprn_shards_splits_by_square_and_totals_correctly(tmp_path):
    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        meta = os_shards.write_uprn_shards(handle, tmp_path)
    assert meta == {"total": 6, "squares": {"SS": 3, "ST": 3}}
    assert (tmp_path / "SS.csv.gz").exists()
    assert (tmp_path / "ST.csv.gz").exists()
    assert (tmp_path / "meta.json").exists()


def test_write_uprn_shards_strips_the_bom_from_the_header(tmp_path):
    # A plain "utf-8" decode (not "utf-8-sig") leaves the BOM character as
    # the first character of the header's first field; write_uprn_shards
    # must not let that corrupt the header check or, worse, leak into a
    # data row's own UPRN field.
    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
    assert first_line.startswith("﻿"), "fixture must actually carry a BOM for this test to mean anything"

    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        os_shards.write_uprn_shards(handle, tmp_path)
    with gzip.open(tmp_path / "ST.csv.gz", "rt", encoding="utf-8") as handle:
        first_row = handle.readline()
    assert not first_row.startswith("﻿")
    assert first_row.startswith("1,")


def test_write_uprn_shards_meta_is_written_last(tmp_path):
    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        os_shards.write_uprn_shards(handle, tmp_path)
    meta = json.loads((tmp_path / "meta.json").read_text(encoding="utf-8"))
    assert meta == {"total": 6, "squares": {"SS": 3, "ST": 3}}


def test_uprn_in_returns_only_in_range_rows_typed(tmp_path):
    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        os_shards.write_uprn_shards(handle, tmp_path)
    # ST square only, a bbox tight around the real first row alone.
    rows = list(os_shards.uprn_in(tmp_path, 358000, 172000, 359000, 173000))
    assert rows == [(1, 358260.99, 172796.83, 51.4526038, -2.6020703)]
    uprn, easting, northing, lat, lon = rows[0]
    assert isinstance(uprn, int)
    assert isinstance(easting, float)
    assert isinstance(northing, float)
    assert isinstance(lat, float)
    assert isinstance(lon, float)


def test_uprn_in_over_the_full_extent_returns_all_six_rows(tmp_path):
    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        os_shards.write_uprn_shards(handle, tmp_path)
    rows = list(os_shards.uprn_in(tmp_path, 0, 0, 900000, 900000))
    assert len(rows) == 6
    assert {row[0] for row in rows} == {1, 2, 3, 4, 5, 6}


def test_uprn_in_over_a_missing_square_yields_nothing(tmp_path):
    with _UPRN_SAMPLE.open("r", encoding="utf-8") as handle:
        os_shards.write_uprn_shards(handle, tmp_path)
    rows = list(os_shards.uprn_in(tmp_path, 900000, 900000, 901000, 901000))
    assert rows == []
