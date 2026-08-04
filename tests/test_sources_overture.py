import concurrent.futures
import json
import threading
from pathlib import Path

import pytest

from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.sources import overture as overture_module
from mapgen.sources.base import NullProgress
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    LAYER_FILENAMES,
    MAX_CONCURRENT_TYPE_DOWNLOADS,
    OvertureError,
    OvertureSource,
)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    """Records commands and writes a stub GeoJSON at the requested output path.

    The lock is not superstition. Since Task 24 this runner is called from
    up to eight worker threads at once, and a test double that records its
    own calls unreliably is the fastest way to a test that passes or fails
    for reasons having nothing to do with the code under test.
    """

    def __init__(self, returncode=0, stderr=""):
        self.commands = []
        self.lock = threading.Lock()
        self._returncode = returncode
        self._stderr = stderr

    def record(self, command):
        with self.lock:
            self.commands.append(command)

    def __call__(self, command, **kwargs):
        self.record(command)
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


# --- a faithful stand-in for overturemaps 0.20.0 -----------------------
#
# 0.19.0 and 0.20.0 differ in two ways that both reach the owner's package,
# and the venv holds 0.20.0 while the copy PATH resolves today is 0.19.0.
# A stub written for convenience rather than from the real thing would
# prove nothing about either, so this is built from what 0.20.0 was
# observed doing on this machine, and test_live_smoke.py drives the real
# 0.20.0 to keep this honest.

# A real name from the benchmark extent. U+0177 has no cp1252 mapping,
# which is the whole problem.
WELSH_NAME = "Tŷ Hafan"


class OvertureCli0200(FakeRunner):
    """What overturemaps 0.20.0 does, as opposed to 0.19.0.

    1. It writes a "<--output>.state" sidecar every time --output is given.
       Its own state.get_state_path is Path(f"{output_path}.state") and
       there is no flag to suppress it. 0.19.0 writes no sidecar at all,
       which is why nothing in this suite caught it before.
    2. It writes the GeoJSON with a bare open(path, "w"), so it encodes in
       the process locale codepage. Under cp1252 a Welsh name kills it part
       way through with UnicodeEncodeError, leaving a truncated file and a
       non-zero exit. PYTHONUTF8=1 in the child environment prevents it.

    The encoding is decided from the env this runner is actually handed,
    exactly as a real child decides it from the env it is actually given,
    so no test here can pass by the stub simply being kinder than the CLI.
    """

    def __init__(self, locale_codepage="cp1252", **kwargs):
        super().__init__(**kwargs)
        self.locale_codepage = locale_codepage
        self.environments = []

    def __call__(self, command, **kwargs):
        self.record(command)
        environment = kwargs.get("env") or {}
        self.environments.append(environment)

        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        # Written whatever happens to the data below.
        output.with_name(output.name + ".state").write_text(
            '{"last_release":"2026-07-23.0","theme":"base","type":"place"}',
            encoding="utf-8",
        )

        if self._returncode != 0:
            return FakeCompleted(self._returncode, stderr=self._stderr)

        # ensure_ascii=False is load-bearing, not tidiness. The real CLI
        # builds its properties with orjson.dumps(...).decode(), and orjson
        # does NOT escape non-ASCII: the raw y-circumflex reaches the file
        # write, which is why the write is what explodes. Python's json
        # defaults to ensure_ascii=True, so a stub left on the default emits
        # six ASCII characters, encodes cleanly in cp1252, and quietly
        # proves the opposite of what it claims. That is exactly the
        # "stub more permissive than the real thing" failure this project
        # keeps producing, and this stub fell into it once already before
        # test_the_stub_really_does_fail_without_utf8_mode caught it.
        payload = json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": None,
                        "properties": {"id": "f1", "name": WELSH_NAME},
                    }
                ],
            },
            ensure_ascii=False,
        )
        encoding = "utf-8" if environment.get("PYTHONUTF8") == "1" else self.locale_codepage
        try:
            output.write_text(payload, encoding=encoding)
        except UnicodeEncodeError as exc:
            # The real failure shape: a truncated file on disk and a
            # non-zero exit carrying the traceback's last line.
            output.write_bytes(payload[: exc.start].encode(encoding, "ignore"))
            return FakeCompleted(
                1,
                stderr=(
                    f"UnicodeEncodeError: 'charmap' codec can't encode character "
                    f"{ascii(WELSH_NAME[1])} in position {exc.start}: character "
                    f"maps to <undefined>"
                ),
            )
        return FakeCompleted(0)


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


def test_a_type_still_queued_behind_the_cap_is_skipped_when_a_stop_lands(tmp_path):
    # Was test_fetch_stops_before_the_next_type_once_cancelled_mid_loop,
    # which cancelled from inside the first type's runner and asserted the
    # second type never ran. That assertion is no longer true and cannot be
    # made true: with two types and a cap of eight, BOTH are submitted and
    # running before either one finishes, so a stop raised by the first has
    # nothing left to prevent. See the test below, which pins that changed
    # behaviour deliberately rather than leaving it as a silent deletion.
    #
    # What survives, and what this covers instead, is the case a pool
    # genuinely has: more types asked for than the cap allows, so some are
    # queued. A stop landing while the first eight run must skip the queued
    # ones rather than run them, which is what the check at the top of each
    # worker is for.
    #
    # Deterministic, not a race the test hopes to win. Every one of the
    # first eight workers waits at the same barrier, and the barrier's own
    # action cancels the token when it trips, which is before ANY waiter is
    # released and therefore before any thread can finish its task and pull
    # a queued one. Twelve types, eight downloads, every time.
    token = CancelToken()
    cap = MAX_CONCURRENT_TYPE_DOWNLOADS
    all_started = threading.Barrier(cap, token.cancel, 30)
    types = tuple(f"type{index:02d}" for index in range(cap + 4))

    class BarrierRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            all_started.wait()
            return super().__call__(command, **kwargs)

    runner = BarrierRunner()
    with pytest.raises(Cancelled):
        _source(runner, types=types).fetch(
            REQUEST_BBOX, [_tile()], tmp_path, NullProgress(), cancel=token
        )

    assert len(runner.commands) == cap, (
        f"expected exactly the {cap} in-flight downloads, got "
        f"{len(runner.commands)}"
    )
    # The in-flight ones finished and were kept, per the cancel convention.
    for name in types[:cap]:
        assert (tmp_path / f"{name}.geojson").exists(), f"{name} was thrown away"
    # The queued ones were never started.
    for name in types[cap:]:
        assert not (tmp_path / f"{name}.geojson").exists(), f"{name} ran anyway"


def test_a_stop_with_every_selected_type_already_in_flight_lets_them_all_finish(tmp_path):
    # The behaviour change the test above replaced, stated outright so it
    # is a decision on the record rather than something a reader has to
    # infer from a missing test. The default selection is eight types and
    # the cap is eight, so on the owner's ordinary run every type is in
    # flight within milliseconds of fetch() starting. From that moment a
    # Stop cannot skip any of them: fetch() runs to completion and returns
    # normally, and it is package.py's own checkpoint after the source that
    # ends the run. That is the LayerSource cancel convention applied
    # honestly rather than a hole in it, but it does mean a stop now costs
    # the owner the whole Overture download rather than the remainder of
    # one type, which is the trade this task makes.
    token = CancelToken()
    inner = FakeRunner()

    def cancelling_runner(command, **kwargs):
        result = inner(command, **kwargs)
        token.cancel()
        return result

    source = _source(cancelling_runner, types=("water", "building"))
    paths = source.fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress(), cancel=token
    )

    assert len(inner.commands) == 2
    assert paths == [tmp_path / "water.geojson", tmp_path / "building.geojson"]
    assert token.is_cancelled() is True


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
            self.record(command)
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
    """Task 24: emitted into from several worker threads at once, so it
    keeps its own lock rather than trusting that list.append happens to be
    atomic. Event ORDER is not asserted anywhere against this double, and
    must not be: under a pool it is whatever the network decided."""

    def __init__(self):
        self.events = []
        self.lock = threading.Lock()

    def emit(self, event, **fields):
        with self.lock:
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
        self.record(command)
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
        self.record(command)
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
# --- Task 25: and it counts BATCHES of types for time, not types -------


def test_the_byte_estimate_scales_with_the_type_count():
    # Bytes still multiply out, and always will: every type really is
    # downloaded and really does land on disk, whatever order the pool
    # ran them in.
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one_type = OvertureSource(types=["water"]).estimate(bbox, [_tile()])
    two_types = OvertureSource(types=["water", "building"]).estimate(bbox, [_tile()])

    assert two_types.bytes_estimate == 2 * one_type.bytes_estimate


def test_the_time_estimate_does_not_scale_with_the_type_count():
    # Was asserted the other way (two types cost exactly twice one type)
    # and was true when the types downloaded in turn. Since Task 24 they
    # download together, and measurement says eight at once cost 4.2x
    # one, not 8x: 3.59s against 15.21s over the same 4.17 sq km extent.
    # The old assertion is what let a 6.5x overstatement pass as correct.
    bbox = BBox.parse("-3.2830,51.4000,-3.2530,51.4180")
    one_type = OvertureSource(types=["building"]).estimate(bbox, [])
    eight_types = OvertureSource().estimate(bbox, [])

    assert len(OvertureSource().types) == 8
    ratio = eight_types.seconds_estimate / one_type.seconds_estimate
    assert 2.0 < ratio < 6.0, (
        f"eight concurrent types measured 4.2x one type on this extent "
        f"(15.21s against 3.59s); the estimate makes it {ratio:.1f}x. Above 6 "
        f"is a per-type cost being multiplied out as though nothing ran "
        f"concurrently; below 2 is a batch cost that has forgotten that eight "
        f"downloads sharing a link slow each other down"
    )


def test_a_repeated_type_is_estimated_once_because_it_is_fetched_once():
    # --overture-type is repeatable and takes any string, so naming the
    # same type twice is reachable from the command line. fetch()
    # deduplicates before it submits anything (it has to: two workers
    # racing for one .part path would delete each other's download), so
    # an estimate that charged twice would be describing a run that does
    # not happen.
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    once = OvertureSource(types=["water"]).estimate(bbox, [])
    twice = OvertureSource(types=["water", "water"]).estimate(bbox, [])

    assert twice == once


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


def test_the_estimate_matches_the_eight_way_concurrent_run_it_was_fitted_to():
    # THE ANCHOR, and the thing every number in this file hangs on.
    #
    # Measured 2026-08-04 through OvertureSource.fetch itself, on the
    # shipping code path, against overturemaps 0.20.0 and the live
    # release, all 8 default types over this exact extent, with
    # MAX_CONCURRENT_TYPE_DOWNLOADS at 8, so every type was in flight at
    # the same time. Twelve samples: 12.42s to 20.09s, mean 15.21s, and a
    # byte total of 23,596,128 that came back identical on every one.
    #
    # The version this replaced asserted 60s to 120s, anchored to 77.84s
    # and 98.30s measured when Overture still downloaded SEQUENTIALLY. It
    # still passed after Task 24 made the same run take 13.54s, because
    # the estimate stayed inside a band that had stopped describing
    # anything real. A test that passes for the wrong reason is worse than
    # no test, so the band here is the measured range and nothing wider.
    assert overture_module.MAX_CONCURRENT_TYPE_DOWNLOADS == 8, (
        "the seconds constants were fitted at eight-way concurrency; if the "
        "cap has moved, every timing in this file has to be measured again "
        "rather than reasoned about"
    )
    assert len(OvertureSource().types) == 8
    eight_types = OvertureSource().estimate(
        BBox.parse("-3.2830,51.4000,-3.2530,51.4180"), []
    )
    assert 12.4 <= eight_types.seconds_estimate <= 20.1, (
        f"twelve samples of this exact run spanned 12.42s to 20.09s; the "
        f"estimate says {eight_types.seconds_estimate:.1f}s"
    )
    assert 20_000_000 <= eight_types.bytes_estimate <= 27_000_000, (
        f"measured 23,596,128 bytes for this run, every sample identical; "
        f"the estimate says {eight_types.bytes_estimate}"
    )


def test_the_estimate_matches_the_owners_own_barry_extent():
    # The case the owner actually hits, as opposed to the small one that
    # is convenient to measure: 16.66 x 15.58 km, 259.60 sq km, all eight
    # types. Six samples on 2026-08-04, 17.43s to 20.67s, mean 18.77s, and
    # 168,020,582 bytes on every sample.
    #
    # This is where the model that was here before was worst: it read
    # 659.9s and 375 MB, over by 34x and 2.2x. An extent term fitted at
    # one scale and never checked at the other is exactly how that
    # happened, so both ends are pinned now and neither can move alone.
    barry = OvertureSource().estimate(
        BBox.parse("-3.3400,51.3600,-3.1000,51.5000"), []
    )
    assert 17.4 <= barry.seconds_estimate <= 20.7, (
        f"six samples of this run spanned 17.43s to 20.67s; the estimate says "
        f"{barry.seconds_estimate:.1f}s"
    )
    assert 120_000_000 <= barry.bytes_estimate <= 220_000_000, (
        f"measured 168,020,582 bytes for this run; the estimate says "
        f"{barry.bytes_estimate}"
    )


def test_a_type_list_longer_than_the_cap_costs_more_than_one_batch():
    # The cap is not decoration. --overture-type is repeatable and takes
    # any string the CLI accepts, so a caller can name more types than
    # MAX_CONCURRENT_TYPE_DOWNLOADS, and the pool then makes the ninth
    # wait for a free worker. An estimate that charged one batch however
    # long the list got would report the same number for nine types as
    # for eight.
    #
    # Measured rather than reasoned, on 2026-08-04 over the same 4.17 sq
    # km extent, against real Overture types so the pool did real work:
    # the eight defaults plus building_part took 16.58s, 16.71s and
    # 18.12s (mean 17.14s), and plus address, division and land as well,
    # twelve in all, took 20.48s and 24.14s.
    #
    # The counts here are LITERAL and not derived from the cap, on
    # purpose. Deriving them would move the test's own input whenever the
    # cap moved, so it would keep passing while saying nothing, which is
    # the defect the anchor test above exists to stop.
    bbox = BBox.parse("-3.2830,51.4000,-3.2530,51.4180")
    exotic = [f"unlikely_type_{index}" for index in range(9)]
    eight = OvertureSource(types=exotic[:8]).estimate(bbox, [])
    nine = OvertureSource(types=exotic).estimate(bbox, [])

    assert nine.seconds_estimate > eight.seconds_estimate * 1.15, (
        f"nine types cannot cost what eight cost when only "
        f"{overture_module.MAX_CONCURRENT_TYPE_DOWNLOADS} download at once; "
        f"eight reads {eight.seconds_estimate:.1f}s and nine "
        f"{nine.seconds_estimate:.1f}s"
    )
    # A second batch, not a second whole run.
    assert nine.seconds_estimate < 2 * eight.seconds_estimate
    # And within a stated distance of what nine types actually took. The
    # upper bound is deliberately the loose side: charging a whole second
    # batch for one leftover type overstates a pool, which starts the
    # ninth download the moment any of the first eight finishes rather
    # than waiting for all eight (see estimate()'s own docstring, which
    # says so and says why). Measured at 1.14x, and bounded at 1.20 so
    # that pessimism stays a rounding matter rather than becoming a
    # second whole run charged for one leftover type. Lowering the cap to
    # four is what this catches: nine types would then be three batches
    # and read 1.23x, which is no longer a rounding matter.
    assert 1.0 <= nine.seconds_estimate / 17.14 <= 1.20, (
        f"nine types measured 17.14s on average; the estimate says "
        f"{nine.seconds_estimate:.1f}s"
    )


def test_the_time_estimate_is_not_calibrated_from_a_single_type():
    # Was test_estimate_is_not_calibrated_from_a_single_cheap_type, and
    # guarded a real past mistake: fitting a per-type cost to `building`
    # alone, which is the cheapest of the eight, then multiplying by
    # eight. Its old form asserted that a ONE-type estimate must exceed
    # `building`'s own measured solo time, which was right while every
    # type cost the same whatever else was running, and is wrong now: a
    # one-type run really does cost about what one type costs (3.59s
    # measured, 3.45s estimated), and asserting otherwise would demand a
    # deliberate overstatement.
    #
    # The mistake survives in its modern form, which is fitting the batch
    # from a one-type run and assuming batch cost is flat in how many
    # types share it. That would make a full eight-type run read as 3.6s
    # when it measures 15.21s. So the guard moves to the eight-type end,
    # where the error would now show up.
    bbox = BBox.parse("-3.2830,51.4000,-3.2530,51.4180")
    one_type = OvertureSource(types=["building"]).estimate(bbox, [])
    eight_types = OvertureSource().estimate(bbox, [])

    assert one_type.seconds_estimate < 6.0, (
        f"`building` alone over this extent measured 3.52s to 3.70s; an "
        f"estimate of {one_type.seconds_estimate:.1f}s for it is the old "
        f"eight-type cost being charged to a one-type run"
    )
    assert eight_types.seconds_estimate > 3 * one_type.seconds_estimate, (
        f"eight types measured 4.2x one type (15.21s against 3.59s); an "
        f"estimate that makes them nearly equal was fitted to a single type "
        f"and assumes concurrency is free"
    )


def test_the_estimate_covers_the_middle_of_the_range_it_was_fitted_over():
    # Two points make a line whatever the truth is. These are the two
    # intermediate extents from the same 2026-08-04 sweep, six samples
    # each, and they are here so a future refit cannot satisfy both ends
    # while bending badly in between.
    #
    #    38.63 sq km   15.03s to 20.71s,  41,491,052 bytes
    #   144.92 sq km   16.56s to 19.45s,  70,398,078 bytes
    mid = OvertureSource().estimate(BBox.parse("-3.32,51.40,-3.22,51.45"), [])
    big = OvertureSource().estimate(BBox.parse("-3.35,51.35,-3.20,51.475"), [])

    assert 15.0 <= mid.seconds_estimate <= 20.8
    assert 16.5 <= big.seconds_estimate <= 19.5
    # Bytes get a wider band than seconds, and honestly so: they are
    # deterministic per extent but depend on what is on the ground rather
    # than how much ground there is. 144.92 sq km of mostly Bristol
    # Channel returns less than half what 259.60 sq km of Barry and
    # Cardiff does, so an area-only model reads 1.34x high here and no
    # slope fixes that. See BYTES_PER_TYPE_PER_SQ_KM.
    assert 0.6 <= mid.bytes_estimate / 41_491_052 <= 1.5
    assert 0.6 <= big.bytes_estimate / 70_398_078 <= 1.5


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


# --- overturemaps 0.20.0, finding A: Welsh names ------------------------


def test_the_stub_really_does_fail_without_utf8_mode(tmp_path):
    # Guard on every test below it. If this passes, the stub is a stub that
    # cannot fail, and everything asserting the fix works would be asserting
    # nothing. Calls the runner directly with an environment that has no
    # PYTHONUTF8, which is what a child inherited before this fix.
    runner = OvertureCli0200()
    target = tmp_path / "place.geojson"
    result = runner(
        ["overturemaps", "download", "--type", "place", "--output", str(target)],
        env={},
    )
    assert result.returncode == 1
    # The real traceback names the character as a Python escape rather than
    # as itself, because that is what a repr in a traceback looks like:
    #   UnicodeEncodeError: 'charmap' codec can't encode character
    #   '\u0177' in position 443: character maps to <undefined>
    assert "charmap" in result.stderr
    assert "\\u0177" in result.stderr
    assert target.stat().st_size < len(WELSH_NAME.encode("utf-8")) + 200, (
        "expected the truncated file a real crash leaves, not a whole one"
    )


def test_a_welsh_name_no_longer_kills_the_download(tmp_path):
    # Finding A end to end through the real _download and the real
    # run_hidden: the only thing standing between this and the failure
    # above is procutil.child_environment putting PYTHONUTF8 in the child's
    # environment.
    runner = OvertureCli0200()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())

    written = (tmp_path / "water.geojson").read_text(encoding="utf-8")
    assert WELSH_NAME in written, "the Welsh name did not survive the download"


def test_the_cli_is_handed_an_environment_that_forces_utf8(tmp_path):
    runner = OvertureCli0200()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert runner.environments[0].get("PYTHONUTF8") == "1"
    # Not a bare one-key dict: env REPLACES the child's environment, so
    # stripping PATH would stop the CLI starting at all.
    assert len(runner.environments[0]) > 1


# --- overturemaps 0.20.0, finding B: the .state sidecar -----------------


def _sidecars(directory):
    return sorted(p.name for p in directory.rglob("*.state"))


def test_the_state_sidecar_is_removed_after_a_successful_download(tmp_path):
    runner = OvertureCli0200()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert (tmp_path / "water.geojson").exists()
    assert _sidecars(tmp_path) == [], "0.20.0's .state sidecar survived a clean run"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["water.geojson"]


def test_the_state_sidecar_is_removed_when_the_cli_fails(tmp_path):
    # The failure path matters as much as the success path: the sidecar is
    # written before the CLI decides how it is going to end, so a run that
    # raises would otherwise leave one behind with no real output beside it,
    # and the next resume would merge it.
    runner = OvertureCli0200(returncode=1, stderr="release not found")
    with pytest.raises(OvertureError):
        _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert _sidecars(tmp_path) == [], "0.20.0's .state sidecar survived a failed run"


def test_the_state_sidecar_is_removed_when_the_cli_writes_nothing(tmp_path):
    class SilentButStateful(OvertureCli0200):
        def __call__(self, command, **kwargs):
            self.record(command)
            output = Path(command[command.index("--output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.with_name(output.name + ".state").write_text("{}", encoding="utf-8")
            return FakeCompleted(0)

    with pytest.raises(OvertureError, match="wrote nothing"):
        _source(SilentButStateful()).fetch(
            REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
        )
    assert _sidecars(tmp_path) == []


def test_a_state_sidecar_from_an_earlier_run_is_removed_on_a_resume(tmp_path):
    # _download cleans up the sidecar it creates, but _download does not run
    # at all for a type already on disk, which is exactly what a resume is.
    # A sidecar written by an unfixed version would otherwise sit there
    # forever, because nothing else ever looks at it again.
    (tmp_path / "water.geojson").write_text(
        '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
    )
    (tmp_path / "water.geojson.part.state").write_text("{}", encoding="utf-8")

    runner = OvertureCli0200()
    _source(runner).fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())

    assert runner.commands == [], "the resume should not have re-downloaded anything"
    assert _sidecars(tmp_path) == [], "the earlier run's sidecar survived the resume"


def test_a_state_sidecar_never_becomes_a_merged_layer(tmp_path):
    # The end of the failure path, asserted on merge()'s real output rather
    # than on the cleanup's own bookkeeping. Without the fix this writes a
    # second file called "<stem>_water.geojson.part.geojson" into the
    # package root: 43 bytes of empty FeatureCollection, named like a layer
    # Grasshopper should read, and not declared by possible_outputs() so the
    # stale-output sweep never removes it either.
    work = tmp_path / "work"
    source = _source(OvertureCli0200())
    source.fetch(REQUEST_BBOX, [_tile()], work, NullProgress())

    parts = sorted(p for p in work.rglob("*") if p.is_file())
    outputs = source.merge(parts, tmp_path / "out", "Barry_2026-08-03")

    assert [p.name for p in outputs] == ["Barry_2026-08-03_water.geojson"]
    assert not any(".part" in p.name for p in outputs), (
        f"a .state sidecar reached the package as a merged layer: "
        f"{[p.name for p in outputs]}"
    )


def test_the_sidecar_cleanup_only_names_files_this_source_could_produce(tmp_path):
    # The same closed-list property the superseded-layout sweep has: mapgen
    # owns this scratch directory, but the cleanup still refuses to remove
    # anything it cannot name in advance.
    bystander = tmp_path / "someone_elses.state"
    bystander.write_text("keep me", encoding="utf-8")

    _source(OvertureCli0200(), types=("water",)).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
    )
    assert bystander.exists()


# --- Task 24: the types download concurrently --------------------------
#
# Deliberately no stress tests here. This project has already written one,
# many threads against an unguarded shared list, and it passed against the
# broken code because the corruption it hoped to observe never happened to
# occur. Every test below asserts something about what was called, in what
# order, or with what argument, so it fails for its stated reason every
# time or not at all. Where concurrency itself has to be proved it is
# proved with a barrier, which can only trip if the overlap is real and
# times out with a clear message if it is not.


def test_the_cap_is_eight_and_is_a_named_module_constant():
    # Named and reachable, not buried in a call. The number is justified
    # from measurement in the constant's own comment: eight default types,
    # and eight-way already reaches the floor set by the slowest download.
    assert overture_module.MAX_CONCURRENT_TYPE_DOWNLOADS == 8


def _recording_pool(monkeypatch):
    captured = {}
    real = concurrent.futures.ThreadPoolExecutor

    def recording(*args, **kwargs):
        captured["max_workers"] = kwargs.get("max_workers")
        return real(*args, **kwargs)

    monkeypatch.setattr(concurrent.futures, "ThreadPoolExecutor", recording)
    return captured


def test_the_pool_is_capped_at_the_module_constant_however_many_types_are_asked_for(
    tmp_path, monkeypatch
):
    # --overture-type is repeatable and takes any string, so a caller can
    # name far more than the default eight. Without the cap that is an
    # unbounded number of CLI processes launched at once.
    captured = _recording_pool(monkeypatch)
    types = tuple(f"type{index:02d}" for index in range(12))
    _source(FakeRunner(), types=types).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
    )
    assert captured["max_workers"] == MAX_CONCURRENT_TYPE_DOWNLOADS


def test_the_pool_never_starts_more_threads_than_there_are_types(tmp_path, monkeypatch):
    # The other side of the same min(): a two-type request must not stand
    # up eight threads to do two things.
    captured = _recording_pool(monkeypatch)
    _source(FakeRunner(), types=("water", "building")).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
    )
    assert captured["max_workers"] == 2


def test_the_eight_default_types_really_do_download_at_the_same_time(tmp_path):
    # The claim the whole task rests on, checked rather than assumed. The
    # barrier can only trip if all eight downloads are genuinely in flight
    # at the same instant; sequential code never trips it and fails on the
    # timeout instead of passing by luck.
    barrier = threading.Barrier(len(DEFAULT_OVERTURE_TYPES), timeout=30)
    inside = []
    lock = threading.Lock()

    class BarrierRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            barrier.wait()
            with lock:
                inside.append(command[command.index("--type") + 1])
            return super().__call__(command, **kwargs)

    source = _source(BarrierRunner(), types=tuple(DEFAULT_OVERTURE_TYPES))
    try:
        source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    except Exception as exc:  # noqa: BLE001
        pytest.fail(
            f"the eight types did not overlap, so they are not running "
            f"concurrently: {exc}"
        )
    assert sorted(inside) == sorted(DEFAULT_OVERTURE_TYPES)


def test_progress_is_emitted_from_the_worker_threads_not_the_calling_one(tmp_path):
    # This is the fact that makes ProgressSink thread safety load bearing,
    # so it is pinned rather than left to be re-derived by whoever next
    # writes a sink. ConsoleProgress was given a lock in this same task
    # because of it; EventLog already had one; NullProgress holds no state.
    class ThreadRecordingProgress:
        def __init__(self):
            self.lock = threading.Lock()
            self.threads = set()

        def emit(self, event, **fields):
            with self.lock:
                self.threads.add(threading.current_thread().name)

    progress = ThreadRecordingProgress()
    _source(FakeRunner(), types=("water", "building")).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, progress
    )

    assert progress.threads, "nothing was emitted at all"
    assert threading.current_thread().name not in progress.threads
    assert all(name.startswith("mapgen-overture") for name in progress.threads), (
        f"emitted from something other than this source's own pool: "
        f"{progress.threads}"
    )


def test_fetch_returns_paths_in_the_requested_order_not_completion_order(tmp_path):
    # The first type asked for is deliberately made to finish LAST: its
    # download does not begin until the second type has already emitted,
    # which is that worker's final act before its future resolves. A fetch
    # that built its result from completion order comes back reversed here.
    #
    # Requested order, not alphabetical order. Alphabetical would be stable
    # too, but requested order is what the sequential loop returned before
    # this change, so nothing downstream shifts by a single element.
    building_emitted = threading.Event()
    waited = []

    class GateOnBuilding(RecordingProgress):
        def emit(self, event, **fields):
            super().emit(event, **fields)
            if fields.get("overture_type") == "building":
                building_emitted.set()

    class WaterWaitsForBuilding(FakeRunner):
        def __call__(self, command, **kwargs):
            if command[command.index("--type") + 1] == "water":
                waited.append(building_emitted.wait(timeout=30))
            return super().__call__(command, **kwargs)

    source = _source(WaterWaitsForBuilding(), types=("water", "building"))
    paths = source.fetch(REQUEST_BBOX, [_tile()], tmp_path, GateOnBuilding())

    assert waited == [True], (
        "building never finished ahead of water, so this proved nothing "
        "about ordering"
    )
    assert paths == [tmp_path / "water.geojson", tmp_path / "building.geojson"]


def test_a_type_named_twice_is_downloaded_once_rather_than_raced_for(tmp_path):
    # --overture-type is repeatable and nothing upstream deduplicates it,
    # so two --overture-type water flags reach here as ["water", "water"].
    # Sequentially that was harmless: the second pass found the first
    # pass's file and skipped it. Concurrently both copies would go for the
    # same water.geojson.part, and the first thing _download does with that
    # path is unlink it, so one thread would delete the other's
    # half-written download and both would rename over the same output.
    runner = FakeRunner()
    paths = _source(runner, types=("water", "water", "building")).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
    )
    fetched = sorted(
        command[command.index("--type") + 1] for command in runner.commands
    )
    assert fetched == ["building", "water"]
    assert paths == [tmp_path / "water.geojson", tmp_path / "building.geojson"]


def test_the_debris_sweep_finishes_before_any_download_starts(tmp_path):
    # The sidecar cleanup inside _download is per type and every name is
    # distinct, so that one cannot race. This sweep is different: it walks
    # the WHOLE selection, so running it alongside the workers would mean
    # unlinking one type's sidecar while another type's download was busy
    # creating it. Observed from inside a download rather than read off the
    # source, since "it is written above the pool" is exactly the kind of
    # thing a later edit moves without noticing.
    legacy = tmp_path / "water"
    legacy.mkdir()
    (legacy / "r00_c00.geojson").write_text("{}", encoding="utf-8")
    stale_sidecar = tmp_path / "building.geojson.part.state"
    stale_sidecar.write_text("{}", encoding="utf-8")

    seen = []
    lock = threading.Lock()

    class ObservingRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            with lock:
                seen.append((legacy.exists(), stale_sidecar.exists()))
            return super().__call__(command, **kwargs)

    _source(ObservingRunner(), types=("water", "building")).fetch(
        REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
    )

    assert len(seen) == 2, "expected both types to have been downloaded"
    assert seen == [(False, False), (False, False)], (
        f"a download started while earlier-version debris was still on "
        f"disk: {seen}"
    )


# --- Task 24: one type failing must not throw the others away ----------


class _FailingTypes(FakeRunner):
    """Fails for the named types, succeeds for every other one."""

    def __init__(self, bad, **kwargs):
        super().__init__(**kwargs)
        self.bad = set(bad)

    def __call__(self, command, **kwargs):
        overture_type = command[command.index("--type") + 1]
        if overture_type in self.bad:
            self.record(command)
            return FakeCompleted(1, stderr=f"no data for {overture_type}")
        return super().__call__(command, **kwargs)


def test_a_failing_type_does_not_discard_the_types_that_succeeded(tmp_path):
    # The same principle as the Urbano bridge fix in Task 20 and the stop
    # path in Task 22: work that is paid for is never thrown away because
    # a parallel step failed. On the owner's real extent the seven that
    # worked are most of 168 MB of downloaded data.
    runner = _FailingTypes(bad={"segment"})
    source = _source(runner, types=("building", "segment", "water"))
    with pytest.raises(OvertureError, match="no data for segment"):
        source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())

    assert len(runner.commands) == 3, (
        "the pool was not drained: a failure cancelled the other types "
        "instead of letting them finish"
    )
    assert (tmp_path / "building.geojson").exists()
    assert (tmp_path / "water.geojson").exists()
    assert not (tmp_path / "segment.geojson").exists()


def test_a_partial_failure_records_the_types_that_actually_landed(tmp_path):
    # Review finding I2. Seven types landing and an eighth failing leaves
    # seven real layers merged into the package, and survey.json went on
    # listing all eight under `types`, which README documents as "the
    # actual Overture types fetched". The folder and the record
    # disagreed, in the same shape as finding I8's licence line.
    #
    # Recorded before the exception is raised, which is the whole point:
    # the raising path is the partial one.
    runner = _FailingTypes(bad={"segment"})
    source = _source(runner, types=("building", "segment", "water"))
    assert source.fetched_types is None, (
        "nothing fetched yet must not read as nothing landed"
    )
    with pytest.raises(OvertureError):
        source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())

    assert source.fetched_types == ["building", "water"]
    # The configured selection itself is untouched: possible_outputs and
    # the debris sweep both read it and both need the full closed list,
    # not just what landed.
    assert source.types == ["building", "segment", "water"]


def test_a_clean_run_records_every_type_as_fetched(tmp_path):
    source = _source(FakeRunner(), types=("building", "water"))
    source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert source.fetched_types == ["building", "water"]


def test_a_type_already_on_disk_counts_as_fetched(tmp_path):
    # A resume skips a type it already has and the package holds it just
    # the same, so survey.json must still name it.
    (tmp_path / "water.geojson").write_text("{}", encoding="utf-8")
    runner = FakeRunner()
    source = _source(runner, types=("building", "water"))
    source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())
    assert source.fetched_types == ["building", "water"]
    assert len(runner.commands) == 1, "the type already on disk was downloaded again"


def test_every_failing_type_is_named_not_only_the_first(tmp_path):
    # Six types failing and one type failing are very different
    # situations, most likely a dead network against one bad type name,
    # and a report naming only whichever future was inspected first gives
    # the owner no way to tell them apart.
    runner = _FailingTypes(bad={"segment", "connector", "water"})
    source = _source(runner, types=("building", "segment", "connector", "water"))
    with pytest.raises(OvertureError) as excinfo:
        source.fetch(REQUEST_BBOX, [_tile()], tmp_path, NullProgress())

    message = str(excinfo.value)
    assert "3 types" in message
    for name in ("segment", "connector", "water"):
        assert name in message, f"{name} failed and was not named: {message}"
        assert f"no data for {name}" in message, (
            f"{name}'s own detail was dropped, which is what says whether "
            f"this was a dead network or a bad type name: {message}"
        )
    assert "building" not in message, "a type that succeeded was blamed"


def test_a_single_failure_still_reads_exactly_as_it_always_did(tmp_path):
    # Aggregating several failures must not have changed the message for
    # the ordinary case of one.
    runner = FakeRunner(returncode=1, stderr="release not found")
    with pytest.raises(OvertureError) as excinfo:
        _source(runner, types=("water",)).fetch(
            REQUEST_BBOX, [_tile()], tmp_path, NullProgress()
        )
    assert str(excinfo.value) == (
        "overturemaps failed for type water: release not found"
    )


def test_a_real_failure_is_reported_rather_than_swallowed_by_a_stop(tmp_path):
    # A stop is the owner's own decision and they already know about it. A
    # download that broke is news, and reporting the stop instead would
    # leave package.py marking the tiles pending and the owner with no idea
    # a type is missing for a reason that will still be there next run.
    token = CancelToken()
    cap = MAX_CONCURRENT_TYPE_DOWNLOADS
    all_started = threading.Barrier(cap, token.cancel, 30)
    types = tuple(f"type{index:02d}" for index in range(cap + 2))

    class BarrierRunner(_FailingTypes):
        def __call__(self, command, **kwargs):
            all_started.wait()
            return super().__call__(command, **kwargs)

    runner = BarrierRunner(bad={"type03"})
    with pytest.raises(OvertureError, match="no data for type03"):
        _source(runner, types=types).fetch(
            REQUEST_BBOX, [_tile()], tmp_path, NullProgress(), cancel=token
        )
