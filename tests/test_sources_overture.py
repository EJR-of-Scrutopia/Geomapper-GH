import json

import pytest

from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.sources.base import NullProgress
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    OvertureError,
    OvertureSource,
)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records commands and writes a stub GeoJSON at the requested output path."""

    def __init__(self, returncode=0, stderr=""):
        self.commands = []
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self._returncode == 0:
            output = command[command.index("--output") + 1]
            with open(output, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": None,
                                "properties": {"id": "f1"},
                            }
                        ],
                    },
                    handle,
                )
        return FakeCompleted(self._returncode, stderr=self._stderr)


# The whole extent a request covers. Deliberately WIDER than any single
# _tile()'s own bbox below, so a test asserting fetch() queried this can
# only pass if fetch genuinely used the request bbox rather than a tile's
# (Task 23). Before that change the two were interchangeable and no test
# could have told them apart.
REQUEST_BBOX = BBox.parse("-3.30,51.37,-3.26,51.41")


def _tile(tile_id="r00_c00"):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=0, core_bbox=bbox, query_bbox=bbox)


def _source(runner, types=("water",)):
    return OvertureSource(
        types=list(types), runner=runner, executable_finder=lambda _name: "overturemaps"
    )


def test_declares_its_identity_and_licence():
    source = OvertureSource()
    assert source.id == "overture"
    assert source.licence
    assert source.attribution
    assert source.requires_api_key is False


def test_default_types_match_the_existing_script():
    assert DEFAULT_OVERTURE_TYPES == [
        "building",
        "place",
        "segment",
        "connector",
        "infrastructure",
        "land_use",
        "land_cover",
        "water",
    ]


def test_no_types_argument_at_all_uses_the_full_default():
    assert OvertureSource().types == DEFAULT_OVERTURE_TYPES


def test_types_none_explicitly_uses_the_full_default():
    assert OvertureSource(types=None).types == DEFAULT_OVERTURE_TYPES


def test_an_empty_type_list_is_taken_literally_not_treated_as_unspecified():
    # A coordinator review's Critical 2: `types or DEFAULT_OVERTURE_TYPES`
    # treated [] the same as no argument at all, since both are falsy.
    # types=[] is what overture_types_for_categories returns for a
    # category selection that maps to no Overture type at all, for
    # example ["rail"] or ["boundaries"] alone (see that function's own
    # docstring): a real, deliberate "fetch nothing", not "unspecified,
    # use the default". Reproduced by the coordinator's own mutation:
    # replacing this file's fix with the old `or` line left every test
    # that existed at the time green, because none of them constructed
    # an OvertureSource with types=[] and then checked self.types.
    assert OvertureSource(types=[]).types == []


def test_configure_with_an_empty_type_list_produces_a_source_that_fetches_nothing(tmp_path):
    # The end of Critical 2's actual failure path: configure() (see
    # package.py's _configured_sources) is what package.py calls with
    # request.effective_overture_types, so this is the shape a
    # --category rail or --category boundaries request actually sends.
    configured = OvertureSource().configure([])
    assert configured.types == []
    paths = configured.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths == []


def test_layer_filenames_map_the_three_phase_one_layers():
    assert LAYER_FILENAMES == {
        "water": "water.geojson",
        "land_cover": "vegetation.geojson",
        "land_use": "landuse.geojson",
    }


def test_fetch_calls_the_cli_once_per_type_however_many_tiles_there_are(tmp_path):
    # Task 23: this used to assert 4 calls for 2 tiles x 2 types. Overture
    # is a bbox-filtered parquet read with no node cap, so the tiling was
    # buying nothing and costing one process launch per pair. Asserted
    # against a MULTI-tile plan specifically, so a regression that
    # reintroduced the tile loop would show up as 4 rather than 2.
    runner = FakeRunner()
    source = _source(runner, types=("water", "building"))
    paths = source.fetch(
        REQUEST_BBOX,
        [_tile("r00_c00"), _tile("r00_c01")],
        tmp_path,
        NullProgress(),
    )
    assert len(runner.commands) == 2
    assert len(paths) == 2


def test_fetch_calls_the_cli_the_same_number_of_times_for_one_tile_or_twenty(tmp_path):
    # The property the assertion above implies but does not itself pin:
    # tile count has no bearing at all on how much work fetch does. The
    # owner's real Barry run is 20 tiles, which was 160 process launches
    # across 8 types and is now 8.
    one = FakeRunner()
    _source(one, types=("water", "building")).fetch(
        REQUEST_BBOX, [_tile("r00_c00")], tmp_path / "one", NullProgress()
    )
    twenty = FakeRunner()
    _source(twenty, types=("water", "building")).fetch(
        REQUEST_BBOX,
        [_tile(f"r00_c{index:02d}") for index in range(20)],
        tmp_path / "twenty",
        NullProgress(),
    )
    assert len(one.commands) == len(twenty.commands) == 2


# --- Task 22: fetch()'s optional `cancel` parameter ------------------


def test_fetch_with_no_cancel_argument_behaves_exactly_as_before(tmp_path):
    source = _source(FakeRunner())
    paths = source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert paths[0] == tmp_path / "water.geojson"


def test_fetch_stops_before_the_first_request_when_already_cancelled(tmp_path):
    token = CancelToken()
    token.cancel()
    runner = FakeRunner()
    source = _source(runner, types=("water", "building"))
    with pytest.raises(Cancelled):
        source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress(), cancel=token)
    assert runner.commands == []


def test_fetch_stops_before_the_next_type_once_cancelled_mid_loop(tmp_path):
    # Task 23: the checkpoint is now between types rather than between
    # (tile, type) pairs, because a type is downloaded exactly once. This
    # proves a stop landing right after the first type's download never
    # starts the second type's, and that the first type's file is kept,
    # which is the LayerSource cancel convention: an in-flight request
    # always finishes and is never thrown away.
    token = CancelToken()
    inner = FakeRunner()

    def cancelling_runner(command, **kwargs):
        result = inner(command, **kwargs)
        token.cancel()
        return result

    source = _source(cancelling_runner, types=("water", "building"))
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    with pytest.raises(Cancelled):
        source.fetch(REQUEST_BBOX, tiles, tmp_path, NullProgress(), cancel=token)

    assert len(inner.commands) == 1, "expected only the in-flight type's request"
    assert (tmp_path / "water.geojson").exists()
    assert not (tmp_path / "building.geojson").exists()


def test_fetch_writes_one_flat_file_per_type_not_a_file_per_tile(tmp_path):
    source = _source(FakeRunner(), types=("water", "building"))
    paths = source.fetch(
        REQUEST_BBOX, [_tile("r00_c00"), _tile("r00_c01")], tmp_path, NullProgress()
    )
    assert paths == [tmp_path / "water.geojson", tmp_path / "building.geojson"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "building.geojson",
        "water.geojson",
    ]


def test_fetch_passes_the_whole_request_bbox_to_the_cli_not_a_tiles(tmp_path):
    # REQUEST_BBOX is deliberately wider than _tile()'s own bbox, so this
    # fails if fetch reverts to querying tile.query_bbox (Task 23).
    runner = FakeRunner()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert "--bbox=-3.3000000,51.3700000,-3.2600000,51.4100000" in runner.commands[0]
    assert "--bbox=-3.2900000,51.3800000,-3.2800000,51.3900000" not in runner.commands[0]


def test_fetch_skips_a_type_already_downloaded(tmp_path):
    target = tmp_path / "water.geojson"
    target.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    runner = FakeRunner()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert runner.commands == []


def test_fetch_reports_a_cli_failure_with_its_stderr(tmp_path):
    runner = FakeRunner(returncode=1, stderr="release not found")
    with pytest.raises(OvertureError, match="release not found"):
        _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())


def test_a_cli_failure_names_the_type_since_there_is_no_tile_to_name(tmp_path):
    # The old message was "failed for tile r00_c04, type segment". There is
    # no per-tile request left to name, and naming one would be a lie about
    # what was actually attempted.
    runner = FakeRunner(returncode=1, stderr="boom")
    with pytest.raises(OvertureError, match="failed for type water"):
        _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())


def test_fetch_points_the_cli_at_a_part_file_not_the_final_path(tmp_path):
    runner = FakeRunner()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    written_to = runner.commands[0][runner.commands[0].index("--output") + 1]
    assert written_to.endswith(".part")
    assert written_to.endswith("water.geojson.part")


def test_fetch_leaves_no_partial_file_when_the_cli_fails(tmp_path):
    runner = FakeRunner(returncode=1, stderr="boom")
    with pytest.raises(OvertureError):
        _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert not (tmp_path / "water.geojson").exists()
    assert list(tmp_path.glob("*.part")) == []


def test_fetch_fails_loudly_when_the_cli_exits_cleanly_without_writing(tmp_path):
    class SilentRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            self.commands.append(command)
            return FakeCompleted(0)

    with pytest.raises(OvertureError, match="wrote nothing"):
        _source(SilentRunner()).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())


def test_fetch_reports_a_missing_cli_clearly(tmp_path):
    source = OvertureSource(
        types=["water"], runner=FakeRunner(), executable_finder=lambda _name: None
    )
    with pytest.raises(OvertureError, match="overturemaps"):
        source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())


def test_fetch_includes_the_release_when_configured(tmp_path):
    runner = FakeRunner()
    source = OvertureSource(
        types=["water"],
        release="2026-02-18.0",
        runner=runner,
        executable_finder=lambda _name: "overturemaps",
    )
    source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert "--release" in runner.commands[0]
    assert "2026-02-18.0" in runner.commands[0]


def test_fetch_omits_release_when_not_configured(tmp_path):
    runner = FakeRunner()
    source = OvertureSource(
        types=["water"],
        release=None,
        runner=runner,
        executable_finder=lambda _name: "overturemaps",
    )
    source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert "--release" not in runner.commands[0]


# --- Task 23: progress events stay per tile AND per type ---------------
#
# The browser's classifyTiles (web/static/app.js) ignores any event whose
# tile_id is not one of the grid's own, so switching to elevation's
# tile_id="whole-area" convention would leave an Overture-only run showing
# a dead grid from the first second to the last. These pin the shape that
# keeps the grid alive.


class RecordingProgress:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def test_fetch_emits_tile_done_for_every_tile_for_every_type(tmp_path):
    progress = RecordingProgress()
    source = _source(FakeRunner(), types=("water", "building"))
    tiles = [_tile("r00_c00"), _tile("r00_c01"), _tile("r01_c00")]
    source.fetch(REQUEST_BBOX, tiles, tmp_path, progress)

    assert [event for event, _ in progress.events] == ["tile_done"] * 6
    pairs = {(fields["tile_id"], fields["overture_type"]) for _, fields in progress.events}
    assert pairs == {
        (tile_id, overture_type)
        for tile_id in ("r00_c00", "r00_c01", "r01_c00")
        for overture_type in ("water", "building")
    }
    assert all(fields["source"] == "overture" for _, fields in progress.events)


def test_no_progress_event_ever_carries_a_tile_id_the_grid_would_not_know(tmp_path):
    # The specific regression this forbids: emitting tile_id="whole-area"
    # (ElevationSource's convention) would look tidier for a source that no
    # longer tiles, and would be silently dropped by classifyTiles's own
    # state.has(event.tile_id) guard, leaving the grid entirely dead.
    progress = RecordingProgress()
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    _source(FakeRunner(), types=("water", "building")).fetch(
        REQUEST_BBOX, tiles, tmp_path, progress
    )
    known = {tile.tile_id for tile in tiles}
    assert {fields["tile_id"] for _, fields in progress.events} <= known


def test_a_resumed_type_emits_tile_skipped_for_every_tile(tmp_path):
    (tmp_path / "water.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )
    progress = RecordingProgress()
    tiles = [_tile("r00_c00"), _tile("r00_c01")]
    _source(FakeRunner(), types=("water",)).fetch(REQUEST_BBOX, tiles, tmp_path, progress)

    assert [event for event, _ in progress.events] == ["tile_skipped", "tile_skipped"]
    assert {fields["tile_id"] for _, fields in progress.events} == {"r00_c00", "r00_c01"}


# --- Task 23: debris from the superseded per-tile layout ---------------


def test_fetch_removes_a_superseded_per_tile_directory(tmp_path):
    # work_dir is fingerprinted on the type selection, not on the layout,
    # so a package part-downloaded before this change resumes into the very
    # same directory with <type>/<tile_id>.geojson files still in it. Left
    # alone they are not merely orphaned: package.py hands every file under
    # work_dir to merge(), which now reads a file stem as a TYPE, so a
    # stale r00_c00.geojson becomes a <stem>_r00_c00.geojson in the
    # finished package. Swept before anything is downloaded or read.
    legacy = tmp_path / "water"
    legacy.mkdir()
    (legacy / "r00_c00.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )
    (legacy / "r00_c01.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )

    _source(FakeRunner()).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())

    assert not legacy.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["water.geojson"]


def test_the_superseded_sweep_only_touches_directories_named_after_a_type(tmp_path):
    # The closed list is the safety property, as it is for package.py's own
    # stale-output sweep: work_dir is mapgen's own scratch tree, but the
    # sweep still refuses to remove anything it cannot name in advance.
    bystander = tmp_path / "not_a_type"
    bystander.mkdir()
    (bystander / "keep_me.geojson").write_text("{}", encoding="utf-8")

    _source(FakeRunner(), types=("water",)).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
    )

    assert (bystander / "keep_me.geojson").exists()


def test_a_superseded_per_tile_file_never_becomes_a_merged_type(tmp_path):
    # The end of the failure path the sweep exists to close, asserted on
    # merge()'s actual output rather than on the sweep's own bookkeeping.
    work = tmp_path / "work"
    legacy = work / "water"
    legacy.mkdir(parents=True)
    (legacy / "r00_c00.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )

    source = _source(FakeRunner())
    source.fetch(REQUEST_BBOX, [_tile()], work, NullProgress())
    parts = sorted(p for p in work.rglob("*") if p.is_file())
    outputs = source.merge(parts, tmp_path / "out", "Barry-Waterfront_2026-08-01")

    assert [p.name for p in outputs] == ["Barry-Waterfront_2026-08-01_water.geojson"]
    assert not any("r00_c00" in p.name for p in outputs)


class FailingWriter(FakeRunner):
    """Runner that writes a partial file then fails, simulating a CLI crash."""

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        output = command[command.index("--output") + 1]
        with open(output, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "geometry": None,
                            "properties": {"id": "partial"},
                        }
                    ],
                },
                handle,
            )
        return FakeCompleted(1, stderr="CLI crashed mid-flight")


def test_fetch_cleans_partial_file_on_cli_failure(tmp_path):
    runner = FailingWriter()
    with pytest.raises(OvertureError):
        _source(runner).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert not (tmp_path / "water" / "r00_c00.geojson").exists()
    assert list((tmp_path / "water").glob("*.part")) == []


class SameFeatureEveryCall(FakeRunner):
    """Runner that writes the same feature ID every call, so a merge that
    stopped deduplicating would show up as a duplicated feature."""

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if self._returncode == 0:
            output = command[command.index("--output") + 1]
            with open(output, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "geometry": None,
                                "properties": {"id": "shared-feature-1"},
                            }
                        ],
                    },
                    handle,
                )
        return FakeCompleted(self._returncode, stderr=self._stderr)


def test_fetch_and_merge_produce_one_undeduplicated_file_per_type(tmp_path):
    # Was test_fetch_and_merge_deduplicates_across_tiles. Seam duplication
    # is what tiling created and merge_geojson then had to undo; with one
    # whole-extent call per type the duplicate is never fetched in the
    # first place, which is the point of Task 23 (the tiled run wrote
    # 956,404 bytes of overlapping duplicate data for the same 9,910
    # features). merge_geojson's own deduplication is unchanged and still
    # covered directly in test_merge.py; what is asserted here is the
    # property that replaced the old one: two tiles, one call, one part,
    # and the feature intact.
    runner = SameFeatureEveryCall()
    source = OvertureSource(
        types=["water"],
        runner=runner,
        executable_finder=lambda _name: "overturemaps",
    )
    paths = source.fetch(
        REQUEST_BBOX,
        [_tile("r00_c00"), _tile("r00_c01")],
        tmp_path,
        NullProgress(),
    )
    assert len(runner.commands) == 1
    assert paths == [tmp_path / "water.geojson"]

    outputs = source.merge(paths, tmp_path / "merged", "Barry-Waterfront_2026-08-01")
    assert len(outputs) == 1
    assert outputs[0].name == "Barry-Waterfront_2026-08-01_water.geojson"

    merged_content = json.loads(outputs[0].read_text(encoding="utf-8"))
    assert len(merged_content["features"]) == 1
    assert merged_content["features"][0]["properties"]["id"] == "shared-feature-1"


def test_fetch_re_downloads_a_zero_byte_existing_file(tmp_path):
    target = tmp_path / "water.geojson"
    target.write_bytes(b"")
    runner = FakeRunner()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert len(runner.commands) == 1


def test_merge_writes_one_file_per_type(tmp_path):
    work = tmp_path / "work"
    work.mkdir(parents=True)
    for overture_type in ("water", "building"):
        (work / f"{overture_type}.geojson").write_text(
            '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
        )

    source = OvertureSource(types=["water", "building"])
    parts = [work / "water.geojson", work / "building.geojson"]
    outputs = source.merge(parts, tmp_path / "out", "Barry-Waterfront_2026-08-01")
    assert [p.name for p in outputs] == [
        "Barry-Waterfront_2026-08-01_building.geojson",
        "Barry-Waterfront_2026-08-01_water.geojson",
    ]


def test_merge_groups_by_the_file_stem_not_its_parent_directory(tmp_path):
    # Task 23: the grouping key was part.parent.name, which was the type
    # subdirectory. With one flat file per type in one directory that key
    # is the same string for every part, so every type would merge into a
    # single output named after the work directory. Two types sharing one
    # parent is exactly the shape that would catch it.
    work = tmp_path / "work"
    work.mkdir(parents=True)
    for overture_type in ("water", "building"):
        (work / f"{overture_type}.geojson").write_text(
            '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
        )
    source = OvertureSource(types=["water", "building"])
    outputs = source.merge(
        [work / "water.geojson", work / "building.geojson"],
        tmp_path / "out",
        "Barry-Waterfront_2026-08-01",
    )
    assert len(outputs) == 2, "both types collapsed into one merged output"
    assert {p.name for p in outputs} == {
        "Barry-Waterfront_2026-08-01_building.geojson",
        "Barry-Waterfront_2026-08-01_water.geojson",
    }


def test_merge_names_the_output_after_whatever_stem_it_is_given(tmp_path):
    # Task 20 finding 2: the same fix as OsmSource.merge, applied
    # consistently, so two different surveys never collide on plain
    # "water.geojson" if their outputs are ever copied into one place.
    work = tmp_path / "work"
    work.mkdir(parents=True)
    (work / "water.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )
    source = OvertureSource(types=["water"])
    outputs = source.merge(
        [work / "water.geojson"], tmp_path / "out", "Cardiff-Bay_2026-09-01"
    )
    assert [p.name for p in outputs] == ["Cardiff-Bay_2026-09-01_water.geojson"]


# --- Task 23: the estimate counts types, not tiles x types -------------


def test_estimate_scales_with_types():
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one_type = OvertureSource(types=["water"]).estimate(bbox, [_tile()])
    two_types = OvertureSource(types=["water", "building"]).estimate(bbox, [_tile()])

    assert two_types.bytes_estimate == 2 * one_type.bytes_estimate
    assert two_types.seconds_estimate == pytest.approx(2 * one_type.seconds_estimate)


def test_estimate_does_not_multiply_by_the_tile_count():
    # Was test_estimate_scales_with_tiles_and_types, asserting the opposite.
    # fetch() makes one call per type over the whole bbox, so a plan with
    # twenty tiles costs exactly what a plan with one costs; leaving the
    # per-tile multiplier in place would overstate the owner's real Barry
    # run by a factor of twenty.
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one_tile = OvertureSource(types=["water"]).estimate(bbox, [_tile()])
    twenty_tiles = OvertureSource(types=["water"]).estimate(
        bbox, [_tile(f"r00_c{index:02d}") for index in range(20)]
    )

    assert twenty_tiles.bytes_estimate == one_tile.bytes_estimate
    assert twenty_tiles.seconds_estimate == one_tile.seconds_estimate


def test_estimate_grows_with_the_extent_not_the_tiling():
    # The area term. A bigger bbox at the same tiling genuinely does cost
    # more, which is the only thing left that moves the number.
    small = OvertureSource(types=["water"]).estimate(
        BBox.parse("-3.29,51.38,-3.28,51.39"), []
    )
    large = OvertureSource(types=["water"]).estimate(
        BBox.parse("-3.35,51.35,-3.20,51.475"), []
    )
    assert large.seconds_estimate > small.seconds_estimate
    assert large.bytes_estimate > small.bytes_estimate


def test_estimate_stays_close_to_the_eight_type_run_it_was_fitted_to():
    # The anchor: a full 8-type run over this exact extent, measured twice
    # back to back against the live release, at 77.84s and 98.30s for
    # 23,587,730 bytes. This is not a claim of accuracy, it is a guard, so
    # a future edit that moves the constants away from the only real
    # evidence there is fails here rather than quietly shipping a wrong
    # number to the estimate panel.
    #
    # The bounds span both samples with room around them, deliberately:
    # the two differ from each other by 26%, and run-to-run variance on a
    # single query has been seen at 4.66s against 28.48s. Anything tighter
    # would be pinning noise.
    eight_types = OvertureSource().estimate(
        BBox.parse("-3.2830,51.4000,-3.2530,51.4180"), []
    )
    assert len(OvertureSource().types) == 8
    assert 60.0 <= eight_types.seconds_estimate <= 120.0, (
        f"measured 77.84s and 98.30s for this run, estimate says "
        f"{eight_types.seconds_estimate:.1f}s"
    )
    assert 18_000_000 <= eight_types.bytes_estimate <= 30_000_000, (
        f"measured 23,587,730 bytes for this run, estimate says "
        f"{eight_types.bytes_estimate}"
    )


def test_estimate_is_not_calibrated_from_a_single_cheap_type():
    # The specific mistake this guards, because it was made once already
    # and cost a 2.2x underestimate: `building` alone over the anchor
    # extent takes 4.5s, and treating that as the per-type cost makes a
    # full 8-type run look like 36 seconds when it is nearer 90. A per-type
    # cost fitted to the average of eight has to be well above any single
    # cheap type's own measured time.
    per_type = OvertureSource(types=["building"]).estimate(
        BBox.parse("-3.2830,51.4000,-3.2530,51.4180"), []
    )
    assert per_type.seconds_estimate > 4.66, (
        "the per-type cost is at or below `building`'s own measured time on "
        "this extent, which means it was fitted to one cheap type rather "
        "than to the average of the eight"
    )


def test_estimate_for_a_large_extent_stays_in_the_right_order_of_magnitude():
    # Only one large-extent measurement exists (building alone over
    # 10 x 14 km: 43.73s, 55 MB), so this checks the order of magnitude
    # rather than a value. A full 8-type run over that extent should read
    # as minutes, not seconds and not hours.
    large = OvertureSource().estimate(BBox.parse("-3.35,51.35,-3.20,51.475"), [])
    assert 120.0 <= large.seconds_estimate <= 1800.0, (
        f"an 8-type run over 10 x 14 km should read as minutes, got "
        f"{large.seconds_estimate:.0f}s"
    )
    assert large.bytes_estimate > 8 * 55_000_000 * 0.3, (
        "building alone over this extent measured 55 MB, so eight types "
        "cannot plausibly be far below that"
    )


def test_possible_outputs_declares_every_default_type_even_when_narrowed():
    # The sweep's whole job is removing what a WIDER earlier attempt left
    # behind, so a narrowed instance must still declare the full namespace
    # it could ever have written, not just what this run asked for.
    narrowed = OvertureSource(types=["building"])
    names = narrowed.possible_outputs("Stem_2026-08-01")
    for overture_type in DEFAULT_OVERTURE_TYPES:
        assert f"Stem_2026-08-01_{overture_type}.geojson" in names
    assert "layers/water.geojson" in names
    assert "layers/vegetation.geojson" in names
    assert "layers/landuse.geojson" in names


def test_possible_outputs_also_covers_an_exotic_requested_type():
    # --overture-type accepts strings outside the default eight; a run that
    # fetched one must be sweepable by a later run that did not.
    exotic = OvertureSource(types=["building", "address"])
    assert "Stem_address.geojson" in [
        n for n in exotic.possible_outputs("Stem")
    ]


def test_the_cli_is_invoked_with_no_console_window(tmp_path):
    # Not a cosmetic preference. Under the windowless desktop shortcut this
    # is a console executable, and each window is real enough for the owner
    # to close, which kills the download inside it. That is exactly what
    # happened to them mid-survey, and the child died before writing
    # anything, so the failure reached the log as "overturemaps failed for
    # tile r00_c04, type segment:" with nothing after the colon. Task 23
    # makes it one window per type rather than one per tile per type, which
    # is fewer chances to hit this, not a reason to stop hiding them.
    import subprocess
    import sys

    calls = []
    inner = FakeRunner()

    def recording_runner(command, **kwargs):
        calls.append(kwargs)
        return inner(command, **kwargs)

    source = OvertureSource(
        types=["building"],
        runner=recording_runner,
        executable_finder=lambda name: "overturemaps",
    )
    source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )

    assert calls, "the CLI was never invoked"
    if sys.platform == "win32":
        assert calls[0]["creationflags"] == subprocess.CREATE_NO_WINDOW
    else:
        assert "creationflags" not in calls[0]
