import json
import threading
import time
from datetime import date
from pathlib import Path

import pytest
import requests

from mapgen.geo import BBox, build_tiles
from mapgen.jobs import CancelToken, EventLog, JobState
from mapgen.naming import PathTooLongError, build_package_paths, tiling_fingerprint
from mapgen.package import (
    MAX_RETRY_AFTER_WAIT_SECONDS,
    IncompleteSurveyError,
    _FailureLedger,
    SurveyRequest,
    UnbridgeablePackageError,
    bridge_package,
    describe_tile_failures,
    estimate_geometry,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.elevation import ElevationSource
from mapgen.sources.base import (
    DuplicateSourceError,
    Estimate,
    TileFailure,
    clear_registry,
    get_source,
    register,
)
from mapgen.sources.osm import OsmSource
from mapgen.sources.overture import (
    DEFAULT_OVERTURE_TYPES,
    MAX_CONCURRENT_TYPE_DOWNLOADS,
    OvertureError,
    OvertureSource,
)

BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")


class StubSource:
    """Writes one predictable file per tile and merges them by concatenation."""

    def __init__(self, source_id="stub", fail_on=()):
        self.id = source_id
        self.display_name = f"Stub {source_id}"
        self.licence = "CC0"
        self.attribution = "nobody"
        self.requires_api_key = False
        self._fail_on = set(fail_on)

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            if tile.tile_id in self._fail_on:
                raise RuntimeError(f"stub failure on {tile.tile_id}")
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


class CallRecordingStubSource:
    """Like StubSource, but records which tile ids each fetch() call
    receives. Used to prove pending is recomputed rather than inherited
    when the tiling changes.
    """

    id = "stub"
    display_name = "Stub Call Recorder"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, fail_on=()):
        self.fetch_calls: list[list[str]] = []
        self._fail_on = set(fail_on)

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        self.fetch_calls.append([tile.tile_id for tile in tiles])
        paths = []
        for tile in tiles:
            if tile.tile_id in self._fail_on:
                raise RuntimeError(f"stub failure on {tile.tile_id}")
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


class SkipIfExistsStubSource:
    """Mimics OsmSource's and OvertureSource's real behaviour: if a tile's
    output file already exists and is non-empty, skip fetching it and reuse
    the file untouched, whatever it contains.

    This is exactly the behaviour that defeated the tile-id-filtering fix:
    a stale file from a different tiling, sitting at a path named only
    after (row, col), looks identical to a fresh one, so a source shaped
    like this "skips" ground it never actually covered. Content is stamped
    with a caller-provided run label so a test can tell which run's fetch()
    actually wrote a given file, as opposed to merely reused it.
    """

    id = "stub"
    display_name = "Stub Skip If Exists"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, run_label, fail_on=()):
        self.run_label = run_label
        self._fail_on = set(fail_on)

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            if path.exists() and path.stat().st_size > 0:
                progress.emit("tile_skipped", source=self.id, tile_id=tile.tile_id)
                paths.append(path)
                continue
            if tile.tile_id in self._fail_on:
                raise RuntimeError(f"stub failure on {tile.tile_id}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{self.run_label}:{tile.tile_id}", encoding="utf-8")
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


class SucceedsButWritesNothingSource:
    """A source whose fetch() returns cleanly without writing any file at
    all: no per-tile output, no whole-area output, nothing.

    Used to prove that a fetch() that does not raise is not, by itself,
    evidence that anything was actually produced.
    """

    id = "stub"
    display_name = "Stub Writes Nothing"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=0, seconds_estimate=0.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        return []

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


class ElevationShapedStubSource:
    """Writes exactly one whole-area file with no tile id in its name, like
    ElevationSource's single TIFF, regardless of how many tiles it is asked
    about. Used to confirm whole-area output is still gathered under the
    current-tile-id filtering added for the retiling fix.
    """

    id = "elevation"
    display_name = "Stub Elevation"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = True

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        output = work_dir / "elevation.tif"
        if output.exists() and output.stat().st_size > 0:
            return [output]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("whole-area-data", encoding="utf-8")
        progress.emit("tile_done", source=self.id, tile_id="whole-area")
        return [output]

    def merge(self, parts, out_dir, stem):
        # Mirrors ElevationSource's real merge: copy the whole-area file
        # into out_dir under the package stem rather than passing it
        # through unchanged at its old work_dir location.
        if not parts:
            return []
        output = out_dir / f"{stem}.tif"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(parts[0].read_text(encoding="utf-8"), encoding="utf-8")
        return [output]


class ResumeAwareSource:
    """Like StubSource, but raises if fetch() is ever asked to redo a tile
    that already has a successful output on disk.

    This is how the reviewer proved the resume bug: a source shaped like
    this blows up immediately if a fresh, empty folder is silently created
    on every run, because every tile looks pending again. A correctly
    resumed run never asks for a tile that already succeeded in the first
    place, so this never fires when resume is working.
    """

    id = "stub"
    display_name = "Stub Resume Aware"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, fail_on=()):
        self._fail_on = set(fail_on)
        self.fetch_calls: list[list[str]] = []

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        self.fetch_calls.append([tile.tile_id for tile in tiles])
        paths = []
        for tile in tiles:
            output = work_dir / f"{tile.tile_id}.txt"
            if output.exists() and output.stat().st_size > 0:
                raise AssertionError(
                    f"asked to re-fetch {tile.tile_id}, which already has a "
                    f"successful output; resume should have excluded it from pending"
                )
            if tile.tile_id in self._fail_on:
                raise RuntimeError(f"stub failure on {tile.tile_id}")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            paths.append(output)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


class OvertureShapedStubSource:
    """Shaped like OvertureSource's fetch/merge output: one file per type per
    tile, merged into one file per type named after the type.

    Used to test _write_layer_files without the real overturemaps CLI.
    """

    id = "overture"
    display_name = "Stub Overture"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, types=("water", "land_cover", "land_use", "building")):
        self.types = list(types)

    def estimate(self, bbox, tiles):
        units = len(tiles) * len(self.types)
        return Estimate(bytes_estimate=10 * units, seconds_estimate=1.0 * units)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            for overture_type in self.types:
                path = work_dir / overture_type / f"{tile.tile_id}.geojson"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
                paths.append(path)
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
        return paths

    def merge(self, parts, out_dir, stem):
        by_type: dict[str, list[Path]] = {}
        for part in parts:
            by_type.setdefault(part.parent.name, []).append(part)
        outputs = []
        for overture_type, type_parts in sorted(by_type.items()):
            out = out_dir / f"{stem}_{overture_type}.geojson"
            out.write_text(
                "\n".join(p.read_text(encoding="utf-8") for p in type_parts),
                encoding="utf-8",
            )
            outputs.append(out)
        return outputs


class PartialOvertureStubSource:
    """Shaped like OvertureSource: one type per subdirectory, tile-id stem.

    Deliberately skips writing one (tile_id, type) pair so tests can check
    that a tile with some but not all of its type files is not marked ok.
    """

    id = "overture"
    display_name = "Stub Partial Overture"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, types=("water", "building"), missing=()):
        self.types = list(types)
        self._missing = set(missing)

    def estimate(self, bbox, tiles):
        units = len(tiles) * len(self.types)
        return Estimate(bytes_estimate=10 * units, seconds_estimate=1.0 * units)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            for overture_type in self.types:
                if (tile.tile_id, overture_type) in self._missing:
                    continue
                path = work_dir / overture_type / f"{tile.tile_id}.geojson"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}", encoding="utf-8")
                paths.append(path)
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
        return paths

    def merge(self, parts, out_dir, stem):
        by_type: dict[str, list[Path]] = {}
        for part in parts:
            by_type.setdefault(part.parent.name, []).append(part)
        outputs = []
        for overture_type, type_parts in sorted(by_type.items()):
            out = out_dir / f"{stem}_{overture_type}.geojson"
            out.write_text(
                "\n".join(p.read_text(encoding="utf-8") for p in type_parts),
                encoding="utf-8",
            )
            outputs.append(out)
        return outputs


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registry()
    yield
    clear_registry()


def _request(tmp_path, **overrides):
    defaults = dict(
        bbox=BBOX,
        region="South Wales",
        site="Barry Waterfront",
        output_root=tmp_path,
        tile_size_m=600.0,
        overlap_m=50.0,
        source_ids=("stub",),
        survey_date=date(2026, 8, 1),
        run_bridge_step=False,
    )
    defaults.update(overrides)
    return SurveyRequest(**defaults)


def test_estimate_reports_tile_count_and_extent(tmp_path):
    register(StubSource())
    estimate = estimate_survey(_request(tmp_path))
    assert estimate["tiles"] >= 1
    assert estimate["extent_km"]["width"] > 0
    assert estimate["bytes_estimate"] > 0


def test_estimate_lists_each_selected_source(tmp_path):
    register(StubSource())
    estimate = estimate_survey(_request(tmp_path))
    assert [s["id"] for s in estimate["sources"]] == ["stub"]


def test_estimate_rejects_a_path_that_would_be_too_long(tmp_path):
    register(StubSource())
    deep = Path("C:/") / ("x" * 200)
    with pytest.raises(PathTooLongError):
        estimate_survey(_request(tmp_path, output_root=deep))


def test_estimate_does_not_reject_an_osm_only_job_over_an_unselected_overture_path(tmp_path):
    # Task 20 finding 3, reproduced with the real sources: a real
    # `--source osm` run was refused over a 287-character path shaped like
    # Overture's, though Overture was never selected. register_default_sources
    # pulls in the real OsmSource/OvertureSource/ElevationSource;
    # estimate_survey never touches the network here (source.estimate() is
    # pure arithmetic on tile counts), so this is safe without mocking
    # anything.
    register_default_sources()
    # _request's defaults are already region="South Wales", site="Barry
    # Waterfront", matching the real reported run; only output_root and
    # source_ids vary here.
    #
    # 148, not the 140 this used before Task 23: Overture's raw path lost
    # its "/rNN_cNN" segment when it stopped being tiled, so it is eight
    # characters shorter and 140 no longer trips the guard at all. The
    # window that isolates Overture is only a few characters wide (osm's
    # own path and project_setting both have to still fit), so the setup
    # asserts its own preconditions below rather than trusting the number
    # to stay right through the next change to any of the three shapes.
    deep = Path("C:/") / ("x" * 148)
    probe = build_package_paths(
        deep,
        "South Wales",
        "Barry Waterfront",
        date.today(),
        "abcdef01",
    )
    longest_type = max(DEFAULT_OVERTURE_TYPES, key=len)
    lengths = {
        "overture": len(str(probe.work_dir / "raw" / "overture" / f"{longest_type}.geojson")),
        "osm": len(str(probe.work_dir / "raw" / "osm" / "r00_c00.osm")),
        "project_setting": len(str(probe.project_setting)),
    }
    assert lengths["overture"] > 240, (
        f"test setup: this depth no longer trips the guard on Overture's own "
        f"path, so the test below would prove nothing: {lengths}"
    )
    assert lengths["osm"] <= 240 and lengths["project_setting"] <= 240, (
        f"test setup: at this depth something other than Overture is already "
        f"over the limit, so an osm-only run would be refused for a reason "
        f"this test is not about: {lengths}"
    )

    # Guard: at this depth the OLD (fixed) behaviour, and today's behaviour
    # whenever Overture genuinely is selected, must still raise. Otherwise
    # this test would prove nothing about the fix.
    with pytest.raises(PathTooLongError):
        estimate_survey(_request(tmp_path, output_root=deep, source_ids=("osm", "overture")))

    # osm only: Overture's path is not one this job will ever produce, so
    # the same depth must not be refused.
    estimate_survey(_request(tmp_path, output_root=deep, source_ids=("osm",)))


# --- Task 18 item 7: the folder preview must be the real composed path,
# produced by the exact same code that later creates it -------------------


def test_estimate_includes_the_exact_folder_naming_would_compose(tmp_path):
    register(StubSource())
    request = _request(tmp_path)
    estimate = estimate_survey(request)
    fingerprint = tiling_fingerprint(
        *request.bbox.as_tuple(),
        request.tile_size_m,
        request.overlap_m,
        request.effective_categories,
        request.effective_overture_types,
    )
    expected = build_package_paths(
        tmp_path, request.region, request.site, request.effective_date, fingerprint
    ).root
    assert estimate["folder"] == str(expected)


def test_estimate_folder_matches_the_folder_run_survey_actually_creates(tmp_path):
    # The stronger, end-to-end version of the test above: proves the
    # estimate's reported folder is not merely built from the same
    # function by inspection, but is identical to what a real download of
    # the SAME request actually creates on disk. This is the property the
    # brief calls the one thing not to get wrong: a path that merely
    # agrees today can still drift if the two call sites ever diverge,
    # this test would catch that the moment it happened.
    register(StubSource())
    request = _request(tmp_path)
    estimate = estimate_survey(request)
    result = run_survey(request)
    assert estimate["folder"] == str(result.paths.root)


def test_estimate_folder_reflects_an_existing_02_collision(tmp_path):
    # build_package_paths' own collision-avoidance (a genuinely complete
    # package already sitting at the plain name) must be visible in the
    # preview too, not just in the folder a real run would create: a
    # preview that always showed the un-suffixed name would mislead
    # exactly when it matters most, a second survey of the same site.
    register(StubSource())
    request = _request(tmp_path)
    first = run_survey(request)
    assert first.complete is True
    estimate = estimate_survey(request)
    assert estimate["folder"] == str(first.paths.root.parent / "2026-08-01_Barry-Waterfront_02")


# --- Task 18 item 6: /api/extent's geometry, reused not reimplemented -----


def test_estimate_geometry_matches_estimate_survey_for_the_same_inputs(tmp_path):
    # Review round 1: this compares two callers of the same shared
    # helper, so it cannot fail on its own if that helper's maths is
    # simply wrong in the same way for both callers (len(tiles) + 1,
    # rows/cols swapped, ...). Kept because it genuinely does prove the
    # two functions cannot silently diverge from each other, but paired
    # with the test below, which pins the actual numbers independently.
    register(StubSource())
    request = _request(tmp_path)
    survey_estimate = estimate_survey(request)
    geometry = estimate_geometry(request.bbox, request.tile_size_m, request.overlap_m)
    assert geometry == {
        "tiles": survey_estimate["tiles"],
        "rows": survey_estimate["rows"],
        "cols": survey_estimate["cols"],
        "extent_km": survey_estimate["extent_km"],
        "tile_grid": survey_estimate["tile_grid"],
    }


def test_estimate_geometry_reports_the_exact_known_tile_count_and_grid():
    # BBOX at 600m/50m is confirmed a 2x2 grid, four tiles named r00_c00
    # through r01_c01, independently by test_geo.py's own build_tiles
    # tests and by this file's per-tile tests elsewhere (e.g.
    # test_a_failing_tile_does_not_corrupt_the_status_of_tiles_that_
    # already_succeeded references all four by name). Asserted against
    # those known numbers directly: "tiles >= 1" would not have caught
    # _geometry_summary returning len(tiles) + 1, which passes every
    # other test in both suites.
    geometry = estimate_geometry(BBOX, 600.0, 50.0)
    assert geometry["tiles"] == 4
    assert geometry["rows"] == 2
    assert geometry["cols"] == 2


# --- Task 22: /api/extent's tile_grid, so the browser can draw the actual
# tiling as rectangles instead of a number ---------------------------------


def test_tile_grid_has_one_entry_per_tile_keyed_by_the_same_tile_id_progress_events_use():
    geometry = estimate_geometry(BBOX, 600.0, 50.0)
    tile_ids = {entry["tile_id"] for entry in geometry["tile_grid"]}
    assert tile_ids == {"r00_c00", "r00_c01", "r01_c00", "r01_c01"}
    assert len(geometry["tile_grid"]) == geometry["tiles"]


def test_tile_grid_entries_carry_the_core_non_overlapping_bounds():
    # core_bbox, not query_bbox: the grid on the map is meant to tile the
    # extent edge to edge, which is core_bbox's job (see build_tiles).
    # query_bbox tiles deliberately overlap their neighbours, and drawing
    # those instead would show rectangles that overlap rather than tile.
    tiles = build_tiles(BBOX, 600.0, 50.0)
    geometry = estimate_geometry(BBOX, 600.0, 50.0)
    by_id = {entry["tile_id"]: entry for entry in geometry["tile_grid"]}
    for tile in tiles:
        entry = by_id[tile.tile_id]
        assert entry["west"] == tile.core_bbox.to_dict()["west"]
        assert entry["south"] == tile.core_bbox.to_dict()["south"]
        assert entry["east"] == tile.core_bbox.to_dict()["east"]
        assert entry["north"] == tile.core_bbox.to_dict()["north"]


def test_estimate_geometry_needs_no_region_site_or_output_root():
    # The whole point: this must be answerable from a bbox and a tiling
    # alone, before a region or site exists to plan a real package path
    # from at all.
    geometry = estimate_geometry(BBOX, 600.0, 50.0)
    assert geometry["tiles"] >= 1
    assert geometry["extent_km"]["width"] > 0


def test_estimate_geometry_varies_with_tile_size():
    coarse = estimate_geometry(BBOX, 1200.0, 50.0)
    fine = estimate_geometry(BBOX, 300.0, 50.0)
    assert fine["tiles"] > coarse["tiles"]
    # Extent is a property of the bbox alone, unaffected by tiling.
    assert fine["extent_km"] == coarse["extent_km"]


# --- Task 18 item 5: the estimate warns about a missing elevation key,
# without failing the estimate itself, and generically for any source ------


def test_estimate_has_no_warnings_by_default(tmp_path):
    register(StubSource())
    estimate = estimate_survey(_request(tmp_path))
    assert estimate["warnings"] == []


def test_estimate_warns_when_a_source_reports_a_readiness_problem(tmp_path):
    class NeverReadySource(StubSource):
        def readiness_problem(self):
            return "this stub is never ready"

    register(NeverReadySource())
    estimate = estimate_survey(_request(tmp_path))
    assert estimate["warnings"] == ["this stub is never ready"]
    # A readiness problem is a warning, not a failure: the numeric estimate
    # still completes normally alongside it.
    assert estimate["tiles"] >= 1


def test_estimate_does_not_warn_when_a_sources_readiness_problem_returns_none(tmp_path):
    class AlwaysReadySource(StubSource):
        def readiness_problem(self):
            return None

    register(AlwaysReadySource())
    estimate = estimate_survey(_request(tmp_path))
    assert estimate["warnings"] == []


def test_run_creates_the_dated_region_folder(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    assert result.paths.root == tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    assert result.paths.root.is_dir()


def test_run_writes_survey_json_with_the_expected_shape(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["site"] == "Barry Waterfront"
    assert payload["region"] == "South Wales"
    assert payload["slug"] == {"site": "Barry-Waterfront", "region": "South-Wales"}
    assert payload["date"] == "2026-08-01"
    assert payload["urbano_stem"] == "Barry-Waterfront_2026-08-01"
    assert payload["bbox"]["west"] == pytest.approx(-3.29)
    assert payload["complete"] is True
    assert payload["started_at"].endswith("Z")
    assert payload["finished_at"].endswith("Z")


def test_survey_json_records_licence_and_attribution_per_source(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["sources"][0]["licence"] == "CC0"
    assert payload["sources"][0]["attribution"] == "nobody"


def test_survey_json_records_per_tile_outcomes(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert all(record["stub"] == "ok" for record in payload["tiles"])


def test_work_dir_is_removed_on_success(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    assert not result.paths.work_dir.exists()
    # work_dir is the fingerprint leaf (_work/<fp>/); the shared _work/
    # parent must go too, or a complete root, which is never reused, would
    # permanently carry an empty _work/ with nothing left to clean it up.
    assert not result.paths.work_dir.parent.exists()


def test_work_dir_is_kept_when_requested(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, keep_work=True))
    assert result.paths.work_dir.is_dir()


def test_a_failing_tile_marks_the_package_incomplete(tmp_path):
    # r01_c01 is the last tile build_tiles produces for this bbox and tile
    # size, so the earlier three genuinely succeed before the failure. A
    # source that fails on the very first tile could never show whether
    # earlier successes get corrupted by a later failure.
    register(StubSource(fail_on=("r01_c01",)))
    result = run_survey(_request(tmp_path, force=True))
    assert result.complete is False
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["complete"] is False


def test_a_failing_tile_does_not_corrupt_the_status_of_tiles_that_already_succeeded(tmp_path):
    register(StubSource(fail_on=("r01_c01",)))
    result = run_survey(_request(tmp_path, force=True))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    records = {record["tile_id"]: record["stub"] for record in payload["tiles"]}
    assert records["r00_c00"] == "ok"
    assert records["r00_c01"] == "ok"
    assert records["r01_c00"] == "ok"
    assert records["r01_c01"] == "failed"


def test_work_dir_is_retained_after_a_failure_so_the_job_can_resume(tmp_path):
    register(StubSource(fail_on=("r00_c00",)))
    result = run_survey(_request(tmp_path, force=True))
    assert result.paths.work_dir.is_dir()


def test_a_resumed_run_only_refetches_tiles_that_previously_failed(tmp_path):
    source = ResumeAwareSource(fail_on=("r01_c01",))
    register(source)

    first = run_survey(_request(tmp_path, force=True))
    assert first.complete is False

    second = run_survey(_request(tmp_path, force=True))

    # Same folder, not a fresh _02: an incomplete package is resumed.
    assert second.paths.root == first.paths.root
    assert second.paths.root.name == "2026-08-01_Barry-Waterfront"
    # Identical bbox, tile_size_m and overlap_m fingerprint identically, so
    # the second run's work_dir is not just under the same root, it is the
    # exact same directory, which is what lets it find the saved state.
    assert second.paths.work_dir == first.paths.work_dir
    # The first run attempted every tile; the second only the one that had
    # not yet succeeded. If resume were broken, fetch_calls[1] would list
    # all four tiles again, and ResumeAwareSource would have raised instead
    # of getting this far, since r00_c00 through r01_c00 already have output.
    assert source.fetch_calls[0] == ["r00_c00", "r00_c01", "r01_c00", "r01_c01"]
    assert source.fetch_calls[1] == ["r01_c01"]


def test_a_successful_resume_merges_previously_fetched_tiles_too(tmp_path):
    # ResumeAwareSource's fail_on never clears, so its second attempt always
    # fails again and only ever exercises the force-recovery branch. This
    # test lets the retried tile actually succeed, which is what exposes
    # merge() being handed just the newly-fetched file instead of every
    # output the source has ever produced for this package.
    source = StubSource(fail_on=("r01_c01",))
    register(source)

    first = run_survey(_request(tmp_path, force=True))
    assert first.complete is False

    source._fail_on.clear()
    second = run_survey(_request(tmp_path, force=True))

    assert second.complete is True
    assert second.paths.root == first.paths.root
    merged_text = (second.paths.root / "stub.txt").read_text(encoding="utf-8")
    for tile_id in ("r00_c00", "r00_c01", "r01_c00", "r01_c01"):
        assert tile_id in merged_text


def test_a_different_tile_size_gets_its_own_fingerprint_directory_and_refetches_everything(
    tmp_path,
):
    # r01_c01 fails so the package stays incomplete and the second run
    # reuses the same root (a complete package would get a fresh _02
    # instead). The 1200m retiling collapses the bbox to a single tile,
    # r00_c00, which gets its own work_dir, distinct from the 600m one, and
    # is fetched fresh rather than inherited from whatever the 600m run
    # left behind at the same tile id.
    register(CallRecordingStubSource(fail_on=("r01_c01",)))
    first = run_survey(_request(tmp_path, tile_size_m=600.0, overlap_m=50.0, force=True))
    assert first.complete is False

    clear_registry()
    source2 = CallRecordingStubSource()
    register(source2)
    second = run_survey(_request(tmp_path, tile_size_m=1200.0, overlap_m=50.0))

    assert second.paths.root == first.paths.root
    assert second.paths.work_dir != first.paths.work_dir
    assert second.complete is True
    assert source2.fetch_calls == [["r00_c00"]]
    merged_text = (second.paths.root / "stub.txt").read_text(encoding="utf-8")
    assert merged_text == "r00_c00"


def test_retiling_does_not_reuse_a_stale_file_from_a_skip_if_exists_source(tmp_path):
    # This is the specific case that defeated the previous, detection-based
    # fix: a source shaped like the real ones checks "does this tile's file
    # already exist" and reuses it untouched if so. A file named only after
    # (row, col) cannot tell a 600m tile from a 1200m tile at the same
    # position apart, so the earlier fix's tile-id filtering could not stop
    # a stale 600m file from being silently treated as the 1200m tile's
    # output. Giving each tiling its own fingerprinted directory means the
    # 1200m run's "does this file exist" check is asked about a path the
    # 600m run never wrote to in the first place.
    register(SkipIfExistsStubSource(run_label="RUN600", fail_on=("r01_c01",)))
    first = run_survey(_request(tmp_path, tile_size_m=600.0, overlap_m=50.0, force=True))
    assert first.complete is False

    clear_registry()
    register(SkipIfExistsStubSource(run_label="RUN1200"))
    second = run_survey(_request(tmp_path, tile_size_m=1200.0, overlap_m=50.0))

    assert second.paths.root == first.paths.root
    assert second.paths.work_dir != first.paths.work_dir
    assert second.complete is True
    merged_text = (second.paths.root / "stub.txt").read_text(encoding="utf-8")
    assert merged_text == "RUN1200:r00_c00"
    assert "RUN600" not in merged_text


def test_a_different_overlap_also_gets_its_own_fingerprint_directory_and_refetches(tmp_path):
    # The retiling tests above only ever vary tile_size_m. overlap_m feeds
    # the fingerprint too but, unlike tile_size_m, never changes the grid:
    # build_tiles's row and column count depends only on tile_size_m, so
    # all four tile ids are identical between the two runs here. That makes
    # this the sterner test of the two: every single tile id collides
    # between the 50m and 75m overlap runs, not just one of four.
    register(CallRecordingStubSource(fail_on=("r01_c01",)))
    first = run_survey(_request(tmp_path, tile_size_m=600.0, overlap_m=50.0, force=True))
    assert first.complete is False

    clear_registry()
    source2 = CallRecordingStubSource()
    register(source2)
    second = run_survey(_request(tmp_path, tile_size_m=600.0, overlap_m=75.0))

    assert second.paths.root == first.paths.root
    assert second.paths.work_dir != first.paths.work_dir
    assert second.complete is True
    # Same grid as the 50m-overlap run, so all four tiles are current, and
    # all four are fetched fresh rather than three of them being silently
    # inherited from the sibling tiling's work_dir.
    assert source2.fetch_calls == [["r00_c00", "r00_c01", "r01_c00", "r01_c01"]]


def test_a_later_success_at_a_different_tiling_cleans_up_a_failed_siblings_scratch(tmp_path):
    # Consequence 2 of the cleanup gap: a complete root is never reused (a
    # later request lands on a fresh _02), so if a failed sibling tiling's
    # scratch tree survived a later, different tiling's success on the same
    # root, nothing would ever remove it.
    register(StubSource(fail_on=("r01_c01",)))
    first = run_survey(_request(tmp_path, tile_size_m=600.0, overlap_m=50.0, force=True))
    assert first.complete is False
    assert (first.paths.work_dir / "raw" / "stub").is_dir()

    clear_registry()
    register(StubSource())
    second = run_survey(_request(tmp_path, tile_size_m=1200.0, overlap_m=50.0))

    assert second.paths.root == first.paths.root
    assert second.paths.work_dir != first.paths.work_dir
    assert second.complete is True
    # The abandoned 600m sibling's scratch tree is gone, not just the 1200m
    # run's own, now-also-cleaned-up fingerprint directory.
    assert not first.paths.work_dir.exists()
    assert not second.paths.work_dir.exists()
    assert not second.paths.work_dir.parent.exists()


def test_whole_area_outputs_with_no_tile_id_are_still_gathered(tmp_path):
    # elevation.tif's stem is not shaped like a tile id at all, so the
    # tile-id-shape filtering added for the retiling fix must not exclude
    # it. There are four current tiles here, none of which appear in the
    # filename, which is exactly the shape that must still be gathered.
    register(ElevationShapedStubSource())
    result = run_survey(_request(tmp_path, source_ids=("elevation",)))
    assert result.complete is True
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert all(record["elevation"] == "ok" for record in payload["tiles"])


def test_a_source_that_writes_nothing_is_marked_incomplete_not_ok(tmp_path):
    # fetch() returning without raising is not, by itself, evidence that
    # anything was produced. A source with no whole-area output and no
    # per-tile output must not be vacuously marked ok.
    register(SucceedsButWritesNothingSource())
    result = run_survey(_request(tmp_path))
    assert result.complete is False
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert all(record["stub"] == "failed" for record in payload["tiles"])


def test_force_run_excludes_leftover_part_files_from_a_hard_kill(tmp_path):
    # fsutil's atomic writer, and OvertureSource's own download-to-temp
    # convention, both leave a .part file behind if the process is killed
    # mid-write. A force-mode recovery sweep must not pick that debris up.
    from mapgen.categories import ALL_CATEGORY_IDS
    from mapgen.sources.overture import DEFAULT_OVERTURE_TYPES

    register(StubSource(fail_on=("r01_c01",)))
    # Must match what _request(tmp_path, force=True) below resolves to
    # internally (categories=None, overture_types=None, both defaulted),
    # since Important 1 folded the content selection into the fingerprint:
    # planting debris under the wrong fingerprint would plant it somewhere
    # run_survey's own _plan() never looks.
    fingerprint = tiling_fingerprint(
        *BBOX.as_tuple(), 600.0, 50.0, ALL_CATEGORY_IDS, DEFAULT_OVERTURE_TYPES
    )
    paths = build_package_paths(
        tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), fingerprint
    )
    stub_work = paths.work_dir / "raw" / "stub"
    stub_work.mkdir(parents=True, exist_ok=True)
    (stub_work / "r01_c01.txt.9999.1.deadbeef.part").write_text(
        "crash debris, not a real tile output", encoding="utf-8"
    )

    result = run_survey(_request(tmp_path, force=True))

    assert result.complete is False
    merged_text = (result.paths.root / "stub.txt").read_text(encoding="utf-8")
    assert "crash debris" not in merged_text
    assert "r00_c00" in merged_text


def test_layer_files_are_copied_for_the_three_named_overture_layers_only(tmp_path):
    register(OvertureShapedStubSource())
    result = run_survey(_request(tmp_path, source_ids=("overture",)))
    assert (result.paths.layers_dir / "water.geojson").is_file()
    assert (result.paths.layers_dir / "vegetation.geojson").is_file()
    assert (result.paths.layers_dir / "landuse.geojson").is_file()
    assert not (result.paths.layers_dir / "building.geojson").exists()


def test_a_tile_missing_one_of_several_type_directories_is_marked_failed(tmp_path):
    # water/r00_c00.geojson exists but building/r00_c00.geojson does not.
    # The tile must not be marked ok on the strength of water alone.
    register(PartialOvertureStubSource(missing={("r00_c00", "building")}))
    result = run_survey(_request(tmp_path, source_ids=("overture",)))

    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    records = {record["tile_id"]: record["overture"] for record in payload["tiles"]}
    assert records["r00_c00"] == "failed"
    assert records["r00_c01"] == "ok"
    assert records["r01_c00"] == "ok"
    assert records["r01_c01"] == "ok"

    # A reload must not treat the partially-fetched tile as done, or the
    # missing type would never get another chance to be retried.
    reloaded = JobState.load_or_create(result.paths.work_dir, list(records), ["overture"])
    assert reloaded.is_done("r00_c00", "overture") is False


def test_a_second_run_of_the_same_site_gets_an_02_suffix(tmp_path):
    register(StubSource())
    first = run_survey(_request(tmp_path))
    second = run_survey(_request(tmp_path))
    assert first.paths.root.name == "2026-08-01_Barry-Waterfront"
    assert second.paths.root.name == "2026-08-01_Barry-Waterfront_02"


def test_progress_events_reach_the_sink(tmp_path):
    register(StubSource())
    log = EventLog()
    run_survey(_request(tmp_path), progress=log)
    assert any(e["event"] == "tile_done" for e in log.events)
    assert any(e["event"] == "job_finished" for e in log.events)


def test_cancellation_before_any_source_starts_stops_the_job_without_raising(tmp_path):
    # Task 22: this used to raise Cancelled straight out of run_survey,
    # which JobManager's worker caught and turned into a bare "cancelled"
    # state with no survey.json and no merged output at all (see the
    # brief). A stop must instead behave like the Urbano bridge fix
    # (Task 20): return a normal SurveyResult, with the honest, resumable
    # package that implies.
    register(StubSource())
    token = CancelToken()
    token.cancel()
    result = run_survey(_request(tmp_path), cancel=token)
    assert result.stopped is True
    assert result.complete is False
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["stopped"] is True
    assert payload["complete"] is False
    # Nothing was ever fetched: the token was already cancelled before
    # the source's own turn even began, so _record_tile_outcomes never
    # ran for it and every tile is honestly still "pending", never
    # silently "ok" and never "failed" either (which would claim an
    # attempt was made when none was).
    assert all(record["stub"] == "pending" for record in payload["tiles"])


# --- Task 22: a source whose fetch() accepts the optional `cancel`
# keyword and checks it between tiles, exactly the convention
# sources/base.py documents for the three real sources. Cancels the
# token itself right after its first tile finishes, which is enough to
# prove run_survey's own handling of a Cancelled raised mid-fetch without
# needing a second thread: this is a synchronous, single-threaded test,
# so nothing else could cancel the token WHILE fetch() is running except
# fetch() itself choosing to, which is exactly what a real Stop click
# racing a real network call looks like from this function's point of
# view. ----------------------------------------------------------------


class CancelAwareStubSource:
    id = "stub"
    display_name = "Stub Cancel Aware"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self):
        self.fetched_tile_ids: list[str] = []

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress, cancel=None):
        for index, tile in enumerate(tiles):
            if cancel is not None:
                cancel.raise_if_cancelled()
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source=self.id, tile_id=tile.tile_id)
            self.fetched_tile_ids.append(tile.tile_id)
            if index == 0 and cancel is not None:
                # Models a Stop click landing the instant tile 0's own
                # "request" finishes: tile 0 is paid for and kept, tile 1
                # never starts.
                cancel.cancel()
        return [work_dir / f"{tile.tile_id}.txt" for tile in tiles]

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


def test_cancellation_reaches_the_per_tile_loop_and_stops_within_one_tile(tmp_path):
    source = CancelAwareStubSource()
    register(source)
    log = EventLog()
    result = run_survey(_request(tmp_path), progress=log)

    # Only the tile in flight when the stop landed was fetched: the loop
    # never even started a second tile, let alone all four.
    assert source.fetched_tile_ids == ["r00_c00"]
    assert result.stopped is True
    assert result.complete is False


def test_a_stopped_run_merges_the_tile_it_has_and_writes_a_truthful_survey_json(tmp_path):
    source = CancelAwareStubSource()
    register(source)
    result = run_survey(_request(tmp_path))

    # The merged output on disk is real and usable, not empty and not
    # discarded: exactly the "Grasshopper can read it" bar the brief sets.
    merged_text = (result.paths.root / "stub.txt").read_text(encoding="utf-8")
    assert merged_text == "r00_c00"

    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert payload["stopped"] is True
    records = {record["tile_id"]: record["stub"] for record in payload["tiles"]}
    # The tile actually fetched says so. The three the stop caught before
    # they were reached read "pending", not "failed": a coordinator
    # review's finding is that this is what "truthful" actually requires
    # here, since nothing about those three tiles ever failed, they were
    # simply never attempted. "failed" is reserved for a real attempt
    # that came up short, the same claim it makes on any other run.
    assert records["r00_c00"] == "ok"
    assert records["r00_c01"] == "pending"
    assert records["r01_c00"] == "pending"
    assert records["r01_c01"] == "pending"


def test_a_stopped_run_emits_no_tile_failed_for_tiles_it_never_reached(tmp_path):
    # The other half of the same finding: a tile the stop caught before it
    # was reached must not drive the browser's tile grid into its red,
    # sticky "failed" state either, since nothing about it actually
    # failed. tile_failed is reserved for the same real-attempt-came-up-
    # short case survey.json's own "failed" status now means.
    source = CancelAwareStubSource()
    register(source)
    log = EventLog()
    run_survey(_request(tmp_path), progress=log)
    failed_tile_ids = {e["tile_id"] for e in log.events if e["event"] == "tile_failed"}
    assert failed_tile_ids == set(), (
        f"expected no tile_failed events for tiles the stop never reached, got {failed_tile_ids}"
    )


def test_a_stopped_run_does_not_sweep_stale_outputs(tmp_path):
    # Mirrors test_an_incomplete_run_keeps_every_leftover: a stop is
    # exactly the same kind of incomplete-on-purpose run that must never
    # trigger the sweep, whatever state.complete happens to say.
    class CancelAwareSweepingStubSource(CancelAwareStubSource):
        def possible_outputs(self, stem):
            return [f"{self.id}.txt", f"{stem}_water.geojson", "layers/water.geojson"]

    register(CancelAwareSweepingStubSource())
    root, stem = _root_with_leftovers(tmp_path)
    result = run_survey(_request(tmp_path))
    assert result.stopped is True
    assert (root / f"{stem}_water.geojson").exists()
    assert (root / "layers" / "water.geojson").exists()


def test_a_stopped_run_skips_the_bridge_and_records_it_honestly(tmp_path):
    source = CancelAwareStubSource()
    register(source)
    log = EventLog()
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        progress=log,
        bridge_runner=FakeBridgeRunner(returncode=0),
    )
    assert result.stopped is True
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["bridge"] == {"attempted": False, "ok": None, "error": None}
    assert not any(e["event"] in ("bridge_started", "bridge_done", "bridge_failed") for e in log.events)


def test_a_stopped_run_keeps_work_dir_and_a_resume_completes_it(tmp_path):
    source = CancelAwareStubSource()
    register(source)
    first = run_survey(_request(tmp_path))
    assert first.stopped is True
    assert first.paths.work_dir.is_dir(), "expected _work/ kept so the job can resume"

    # A fresh, uncancelled request over the exact same extent resumes
    # rather than restarts: the one tile already fetched is never asked
    # for again, and the package finishes. A plain StubSource here, not
    # another CancelAwareStubSource: that class cancels itself after
    # whatever tile it sees first, which on a resume is r00_c01 (the
    # first tile still PENDING), not r00_c00, and would cut this second
    # run short again for a reason that has nothing to do with what this
    # test is actually proving.
    second_source = StubSource()
    clear_registry()
    register(second_source)
    second = run_survey(_request(tmp_path))

    assert second.paths.root == first.paths.root
    assert second.complete is True
    assert second.stopped is False
    merged_text = (second.paths.root / "stub.txt").read_text(encoding="utf-8")
    for tile_id in ("r00_c00", "r00_c01", "r01_c00", "r01_c01"):
        assert tile_id in merged_text
    payload = json.loads(second.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert payload["stopped"] is False


def test_a_resume_reports_the_tiles_it_skips_rather_than_saying_nothing(tmp_path):
    # Review finding I6. package.py filters tiles state.json already
    # records as ok out of `pending` BEFORE fetch() is called, so the
    # source never sees them and never emits anything for them. The
    # browser counts progress from events alone, so a Stop-then-resume,
    # which is the resume the owner actually performs, looked to the page
    # like a run with all the work still ahead of it: the bar climbed from
    # zero over the few tiles that were genuinely left and then jumped to
    # 100, and remainingLabel's estimate branch computed
    # staticSeconds * (1 - 0), quoting the whole run for a resume with a
    # sixth of it to do. app.js's own comment ("every tile already on disk
    # reports tile_skipped in the first second") described a world that
    # stopped existing when a clean Stop started marking landed tiles ok.
    source = CancelAwareStubSource()
    register(source)
    first = run_survey(_request(tmp_path))
    assert first.stopped is True
    first_state = {
        record["tile_id"]: record["stub"]
        for record in json.loads(first.paths.survey_json.read_text(encoding="utf-8"))["tiles"]
    }
    already_ok = sorted(t for t, status in first_state.items() if status == "ok")
    assert already_ok, "expected the stop to have kept at least one finished tile"

    clear_registry()
    register(StubSource())
    resumed = EventLog()
    second = run_survey(_request(tmp_path), progress=resumed)
    assert second.complete is True

    skipped = sorted(
        event["tile_id"]
        for event in resumed.snapshot()
        if event["event"] == "tile_skipped" and event.get("source") == "stub"
    )
    assert skipped == already_ok, (
        "every tile the resume skipped must say so, or the browser has no way "
        "to tell finished work from work still ahead of it"
    )
    # And exactly once each: a tile reported twice would inflate the same
    # numbers in the other direction.
    assert len(skipped) == len(set(skipped))
    # The tiles that genuinely still needed fetching are reported as
    # fetched work, not as skipped, which is the distinction the countdown
    # measures its rate from.
    fetched = {
        event["tile_id"]
        for event in resumed.snapshot()
        if event["event"] == "tile_done" and event.get("source") == "stub"
    }
    assert not (fetched & set(skipped))


class LegacyNoCancelStubSource(StubSource):
    """Predates Task 22: fetch(bbox, tiles, work_dir, progress), no
    `cancel` parameter at all. Cancels the very token package.py is using
    itself, via a reference handed to the constructor directly (never
    through fetch()'s own signature, which is the whole point), to model
    "the owner pressed Stop while this old, unmodified source was already
    running". Finishes fetching every one of its own tiles regardless,
    since a source shaped like this has no way to notice mid-loop: the
    between-sources checkpoint in package.py is the only one available to
    it, and this proves that checkpoint alone is enough to stop the run
    (never attempting the source after it) without needing every source
    to have been updated.
    """

    def __init__(self, token, source_id="stub"):
        super().__init__(source_id=source_id)
        self._token = token

    def fetch(self, bbox, tiles, work_dir, progress):
        result = super().fetch(bbox, tiles, work_dir, progress)
        self._token.cancel()
        return result


def test_a_source_with_no_cancel_parameter_still_stops_the_run_between_sources(tmp_path):
    token = CancelToken()
    first_source = LegacyNoCancelStubSource(token, source_id="first")
    second_source = StubSource(source_id="second")
    register(first_source)
    register(second_source)
    result = run_survey(
        _request(tmp_path, source_ids=("first", "second")), cancel=token
    )

    assert result.stopped is True
    assert result.complete is False
    # The legacy source finished every one of its own tiles (it had no
    # way to stop mid-loop) and its output is genuinely merged.
    assert (result.paths.root / "first.txt").is_file()
    merged_first = (result.paths.root / "first.txt").read_text(encoding="utf-8")
    for tile_id in ("r00_c00", "r00_c01", "r01_c00", "r01_c01"):
        assert tile_id in merged_first
    # The second source never started at all: the between-sources check
    # caught the cancellation the legacy source itself triggered. Its
    # tiles are honestly "pending" (never attempted), not "failed" (which
    # would claim an attempt was made and came up short). This is the same
    # rule test_a_stopped_run_merges_the_tile_it_has_and_writes_a_truthful_
    # survey_json pins for a source interrupted PARTWAY through: whether a
    # source never got a turn at all or was cut off mid-fetch, a tile it
    # never touched is never-attempted either way, and the two cases must
    # not disagree about what that reads as.
    assert not (result.paths.root / "second.txt").exists()
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    second_records = {r["tile_id"]: r["second"] for r in payload["tiles"]}
    assert all(status == "pending" for status in second_records.values())


def test_a_stop_noticed_only_after_every_tile_genuinely_finished_still_sweeps(
    tmp_path,
):
    # The one edge case where the control-flow `stopped` and state.complete
    # are both true at once: a single source that finishes every tile
    # normally (no exception anywhere, so state.complete is genuinely
    # True), but flips the token to cancelled right as its own fetch()
    # returns, landing exactly in the gap between the last source
    # finishing and run_survey's own post-loop cancellation check.
    #
    # This test used to assert the opposite, and justified it as the sweep
    # being "deferred to a later, ordinary run rather than risking removing
    # something a resume might still want". Review finding I1: there is no
    # later run. naming._survey_reports_complete reads complete: true, so
    # build_package_paths refuses to reuse this folder and the next survey
    # of the same site and date lands on _02. The stale layer stayed in a
    # finished package for good, which is the exact failure the sweep
    # exists to prevent, in a folder the owner reads straight into
    # Grasshopper.
    class SweepingLegacyNoCancelStubSource(LegacyNoCancelStubSource):
        def possible_outputs(self, stem):
            return [f"{self.id}.txt", f"{stem}_water.geojson", "layers/water.geojson"]

    token = CancelToken()
    register(SweepingLegacyNoCancelStubSource(token))
    root, stem = _root_with_leftovers(tmp_path)
    result = run_survey(_request(tmp_path), cancel=token)

    assert result.complete is True, "expected every tile to have genuinely finished"
    # And the published invariant, stated in survey.json's own comment and
    # in README's schema table long before anything implemented it:
    # stopped means a stop is the reason the run is SHORT. This run is not
    # short, so there is nothing for it to be honest about, and
    # JobManager's worker has always reported this same race as "done".
    assert result.stopped is False, "complete and stopped must never both be true"
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["complete"] is True
    assert payload["stopped"] is False
    # The stale outputs an earlier, wider attempt left in the root are
    # gone, because this is the last run that will ever see this folder.
    assert not (root / f"{stem}_water.geojson").exists(), "the sweep must have run"
    assert not (root / "layers" / "water.geojson").exists()
    # The bridge is still skipped: a Stop press means "start no further
    # external process", whether or not the tiles all happened to land
    # first. Recorded honestly rather than fabricated either way.
    assert payload["bridge"]["attempted"] is False
    assert payload["bridge"]["ok"] is None


def test_coordinate_stem_option_uses_the_coordinate_form(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, coordinate_stem=True))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["urbano_stem"] == "51.39_51.38_-3.28_-3.29"


class FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeBridgeRunner:
    """Stands in for subprocess.run: records commands, returns a fixed exit
    code, never actually shells out to dotnet. run_survey resolves the real
    tools/UrbanoBridge/UrbanoBridge.csproj (it genuinely exists in this
    repo), so run_bridge reaches this runner exactly as it would reach the
    real dotnet executable in production.

    It fails the way the real one fails, which is the point of it: a
    non-zero returncode and nothing else. run_bridge's own returncode
    check, its inspection of the output for a missing Urbano install, and
    the BridgeError it raises are all the real code, exercised here. A
    double that raised BridgeError itself would skip every one of them and
    would pass whatever run_bridge did.

    stderr is carried so a test can hand it UrbanoBridge's own real output
    (see REAL_MISSING_URBANO_OUTPUT in tests/test_bridge.py), which is what
    the owner's machine produces on every run.
    """

    def __init__(self, returncode=0, stderr=""):
        self.returncode = returncode
        self.stderr = stderr
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return FakeCompletedProcess(self.returncode, stderr=self.stderr)


_MINIMAL_OSM_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<osm version="0.6" generator="test">\n'
    '  <node id="1" version="1" lat="51.38" lon="-3.29"/>\n'
    "</osm>\n"
)


class _FakeOsmResponse:
    def __init__(self, status_code=200, text=_MINIMAL_OSM_XML):
        self.status_code = status_code
        self.text = text
        self.headers: dict = {}


class _FakeOsmSession:
    def __init__(self, responses):
        self._responses = list(responses)

    def get(self, url, **kwargs):
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        return self._responses.pop(0)


# --- Task 20 finding 1: a failing Urbano bridge must not destroy the rest
# of a survey. The owner has no Urbano installed, so run_bridge fails on
# every single run they attempt; before this fix, that raised BridgeError
# out of run_survey before survey.json was ever written, so a fully
# successful download left no trace the tool itself would recognise. ------


def test_a_bridge_failure_does_not_prevent_the_package_from_completing(tmp_path):
    register(StubSource())
    # The whole point of this test: run_survey must return normally rather
    # than let BridgeError propagate. If the fix were removed, pytest would
    # report this test as an ERROR (an uncaught BridgeError), not a failed
    # assertion.
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        bridge_runner=FakeBridgeRunner(returncode=1),
    )
    assert result.complete is True
    assert result.paths.survey_json.exists()
    assert (result.paths.root / "stub.txt").exists()


def test_a_bridge_failure_is_recorded_in_survey_json_in_plain_language(tmp_path):
    register(StubSource())
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        bridge_runner=FakeBridgeRunner(returncode=1),
    )
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["bridge"]["attempted"] is True
    assert payload["bridge"]["ok"] is False
    error = payload["bridge"]["error"]
    assert "exit code 1" in error
    # A plain sentence, not a stack trace.
    assert "Traceback" not in error
    assert 'File "' not in error
    # complete tracks the survey DATA only: unaffected by the bridge result.
    # See _build_survey_json's own comment for why the two are kept separate.
    assert payload["complete"] is True


def test_a_bridge_failure_is_emitted_through_the_progress_sink(tmp_path):
    register(StubSource())
    log = EventLog()
    run_survey(
        _request(tmp_path, run_bridge_step=True),
        progress=log,
        bridge_runner=FakeBridgeRunner(returncode=1),
    )
    failures = [e for e in log.events if e["event"] == "bridge_failed"]
    assert len(failures) == 1
    assert "exit code 1" in failures[0]["error"]
    assert any(e["event"] == "job_finished" for e in log.events)


def test_a_successful_bridge_is_recorded_as_ok(tmp_path):
    register(StubSource())
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        bridge_runner=FakeBridgeRunner(returncode=0),
    )
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["bridge"] == {"attempted": True, "ok": True, "error": None}


def test_skipping_the_bridge_records_that_it_was_never_attempted(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, run_bridge_step=False))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["bridge"] == {"attempted": False, "ok": None, "error": None}


def test_an_unforced_run_that_is_ending_short_never_starts_the_bridge(tmp_path):
    """Task 30's third condition on bridge_attempted, and the one nothing
    was asserting: dropping `and not unrecoverable` passed the whole suite.

    This run is about to raise. Before task 30 it raised from inside the
    source loop and never reached the bridge at all, so starting an
    external process on data mapgen is in the middle of refusing to stand
    behind would be a new behaviour rather than a preserved one.

    survey.json is written before the raise, deliberately, which is what
    makes the record readable here at all.
    """
    # A source that gives a per-tile ACCOUNT, so the run defers its
    # decision, retries, and reaches the bridge step still short. A source
    # that fails as a whole re-raises from inside the loop and never gets
    # this far, which is the shape this condition is preserving.
    _register_flaky_osm(_FlakyOsmSession(failures_by_tile={"r01_c01": 99}))
    runner = FakeBridgeRunner(returncode=0)

    with pytest.raises(IncompleteSurveyError):
        run_survey(
            _osm_only_request(tmp_path, run_bridge_step=True), bridge_runner=runner
        )

    payload = json.loads(
        (
            tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront" / "survey.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["complete"] is False
    assert payload["stopped"] is False, "this run failed, it was not stopped"
    # Not attempted, and recorded the honest way rather than as a
    # fabricated failure: the same shape --skip-bridge produces.
    assert payload["bridge"] == {"attempted": False, "ok": None, "error": None}
    # And no process was started, which is the half survey.json cannot say.
    assert runner.calls == []


def test_a_bridge_failure_still_correctly_attributes_the_osm_endpoint_used(tmp_path):
    # Ties Task 20 findings 1 and 4 together. Before finding 1 was fixed,
    # the only way a survey.json was ever produced for a real osm run on
    # this machine was a SECOND, resumed invocation, because the first
    # attempt's crash never reached survey.json. A resumed run correctly
    # and intentionally records no endpoint for tiles it skips because they
    # already exist (see OsmSource's own class docstring and
    # test_sources_osm.py's test_endpoints_used_is_empty_when_every_tile_is_skipped):
    # that is documented, deliberate behaviour, not the bug. But it meant
    # nobody ever saw a survey.json written on the SAME run that actually
    # did the downloading. With the bridge failure no longer fatal, the
    # very first attempt reaches survey.json, on the run that genuinely
    # contacted the endpoint, so attribution is intact without changing
    # anything about how endpoints_used is populated.
    source = OsmSource(
        session=_FakeOsmSession([_FakeOsmResponse() for _ in range(4)]),
        sleeper=lambda _seconds: None,
        min_interval_seconds=0.0,
    )
    register(source)
    result = run_survey(
        _request(tmp_path, source_ids=("osm",), run_bridge_step=True),
        bridge_runner=FakeBridgeRunner(returncode=1),
    )
    assert result.complete is True
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["bridge"]["ok"] is False
    osm_entry = next(s for s in payload["sources"] if s["id"] == "osm")
    assert osm_entry["endpoints_used"] == [source.osm_api_url]


def test_merged_osm_output_on_disk_carries_the_package_stem(tmp_path):
    # Task 20 finding 2, end to end: the file the owner actually finds in
    # the package folder must carry the site/date stem, not a bare
    # "all.osm" that gives no clue which survey it belongs to or which
    # file Urbano needs to read.
    source = OsmSource(
        session=_FakeOsmSession([_FakeOsmResponse() for _ in range(4)]),
        sleeper=lambda _seconds: None,
        min_interval_seconds=0.0,
    )
    register(source)
    result = run_survey(_request(tmp_path, source_ids=("osm",), run_bridge_step=False))
    expected = result.paths.root / f"{result.paths.stem}.osm"
    assert expected.is_file()
    assert result.paths.stem == "Barry-Waterfront_2026-08-01"


def test_register_default_sources_registers_the_four_default_sources():
    register_default_sources()
    from mapgen.sources.base import available_sources

    assert sorted(s.id for s in available_sources()) == [
        "elevation", "lidar_wales", "osm", "overture",
    ]


def test_register_default_sources_called_twice_is_a_no_op():
    register_default_sources()
    first_osm = get_source("osm")
    register_default_sources()
    assert get_source("osm") is first_osm
    from mapgen.sources.base import available_sources

    assert sorted(s.id for s in available_sources()) == [
        "elevation", "lidar_wales", "osm", "overture",
    ]


def test_register_default_sources_raises_when_a_foreign_object_squats_on_a_default_id():
    class Decoy:
        id = "osm"
        display_name = "Decoy, not OsmSource"
        licence = "none"
        attribution = "nobody"
        requires_api_key = False

    decoy = Decoy()
    register(decoy)
    with pytest.raises(DuplicateSourceError, match="'osm'"):
        register_default_sources()
    # The decoy must still be the one in the registry: register_default_sources
    # raised before replacing anything, so a caller who ignores the exception
    # would not silently end up with a wrong source either.
    assert get_source("osm") is decoy


# --- Task 20's out-of-scope finding, fixed here: --overture-type (and
# SurveyRequest.overture_types generally) had no effect on what was
# actually fetched. register_default_sources() builds one OvertureSource
# with the 8-type default and registers it once; every request, whatever
# its own selection, fetched through that same shared instance's fixed
# .types. The fix is _configured_sources(), which asks a source exposing
# the optional `configure` extension for a fresh, request-scoped copy
# instead of ever mutating the registered one. -----------------------


class _FakeOvertureRunner:
    """Records every command run and writes a minimal valid GeoJSON at
    the requested --output path: enough for OvertureSource.fetch's own
    rename-from-.part-on-success step and run_survey's merge step to
    both succeed, without ever shelling out to the real overturemaps CLI.

    Minimal, but no longer EMPTY (Task 30). It used to write a
    feature-less collection, which was a double more permissive than the
    real thing in exactly the way this project keeps finding: a real
    overturemaps download of a selected type over a real extent has
    features in it, and a download that genuinely has none is now a
    distinct, deliberately handled case (no merged file is written for
    it, per the owner's no-fabricated-empty-output ruling). A double that
    only ever produced the empty case would have every test that uses it
    silently asserting against that case instead of the ordinary one.
    """

    def __init__(self):
        self.commands: list[list[str]] = []
        # Task 24: OvertureSource.fetch calls this from up to eight worker
        # threads at once, and a double that records its own calls
        # unreliably is the fastest route to a test that fails for reasons
        # having nothing to do with the code under test.
        self.lock = threading.Lock()

    def __call__(self, command, **kwargs):
        with self.lock:
            self.commands.append(command)
        overture_type = command[command.index("--type") + 1]
        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": f"{overture_type}-1",
                            "geometry": None,
                            "properties": {},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return type("FakeCompleted", (), {"returncode": 0, "stdout": "", "stderr": ""})()


class _PartiallyFailingOvertureRunner(_FakeOvertureRunner):
    """Fails exactly one Overture type and writes real output for the
    rest, which since Task 24 is the ordinary shape of an Overture
    failure rather than an exotic one: a single bad type name, one type
    503ing, one type slow enough to time out."""

    def __init__(self, bad):
        super().__init__()
        self.bad = bad

    def __call__(self, command, **kwargs):
        if command[command.index("--type") + 1] == self.bad:
            with self.lock:
                self.commands.append(command)
            return type(
                "FakeCompleted", (), {"returncode": 1, "stdout": "", "stderr": "boom"}
            )()
        return super().__call__(command, **kwargs)


def test_survey_json_names_only_the_overture_types_the_package_actually_holds(tmp_path):
    # Review finding I2, through the REAL OvertureSource. One type of
    # eight fails on a forced run: seven layers are merged into the
    # package and the record used to go on naming all eight, which README
    # documents as "the actual Overture types fetched". The folder and the
    # record contradicted each other.
    runner = _PartiallyFailingOvertureRunner(bad="segment")
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))
    result = run_survey(_request(tmp_path, source_ids=("overture",), force=True))

    assert result.complete is False, "a missing layer is a package that is short"
    entry = next(s for s in result.survey["sources"] if s["id"] == "overture")
    assert "segment" not in entry["types"], (
        "survey.json names a type that is not in the folder"
    )
    assert len(entry["types"]) == 7
    on_disk = sorted(
        path.name[len(f"{result.paths.stem}_"):-len(".geojson")]
        for path in result.paths.root.glob(f"{result.paths.stem}_*.geojson")
    )
    assert sorted(entry["types"]) == on_disk, (
        "the record and the folder must agree about which layers this package has"
    )

    # And the per-tile record deliberately stays "failed" for every tile.
    # A type's download covers the whole extent, so it delivered the same
    # thing to every tile, and there is no finer per-tile fact to report.
    # The only other status available does not emit tile_failed, and the
    # grid then settles these tiles to "done" off Overture's own per-type
    # tile_done events: a package missing a layer would read as finished.
    # See _record_tile_outcomes' own docstring for the full argument.
    statuses = {record["overture"] for record in result.survey["tiles"]}
    assert statuses == {"failed"}


def test_a_stop_mid_overture_keeps_finished_types_marks_tiles_pending_and_resumes(tmp_path):
    # Task 22's stop path, driven through the REAL OvertureSource rather
    # than through a stub that could be more forgiving than the real thing.
    #
    # Task 24 changed the mechanism this test needs, not the rules it
    # asserts. It used to cancel from inside the first of two types'
    # downloads and expect the second never to run. Two types with a cap of
    # eight are both in flight before either of them finishes, so a stop
    # raised by the first has nothing left to prevent, and keeping the old
    # shape would only have been possible by asserting something that had
    # stopped being true. What a pool genuinely has instead is types QUEUED
    # behind its cap, so this asks for one more type than the cap allows
    # and stops while the first eight are in flight. The ninth is the one
    # the stop is able to skip.
    #
    # Every Task 22 rule below is asserted exactly as before: the types in
    # flight are kept and merged, a tile the stop never reached reads
    # "pending" and not "failed", no tile_failed event is emitted for it,
    # and the run resumes onto precisely the type that was missed.
    #
    # Deterministic rather than hopeful. The barrier's own action cancels
    # the token the instant all eight in-flight downloads have arrived at
    # it, which is before any waiter is released and therefore before any
    # worker can return and pull the queued ninth off the executor.
    token = CancelToken()
    inner = _FakeOvertureRunner()
    cap = MAX_CONCURRENT_TYPE_DOWNLOADS
    all_started = threading.Barrier(cap, token.cancel, 30)
    # "address" is a real Overture type outside the default eight, which is
    # exactly what --overture-type exists to reach.
    types = tuple(DEFAULT_OVERTURE_TYPES) + ("address",)

    def cancelling_runner(command, **kwargs):
        all_started.wait()
        return inner(command, **kwargs)

    register(OvertureSource(runner=cancelling_runner, executable_finder=lambda _n: "overturemaps"))
    log = EventLog()
    request = _request(
        tmp_path,
        source_ids=("overture",),
        overture_types=types,
        run_bridge_step=False,
        keep_work=True,
    )
    result = run_survey(request, progress=log, cancel=token)

    assert result.stopped is True
    assert result.complete is False
    assert len(inner.commands) == cap, (
        f"the stop should have landed with exactly the {cap} in-flight "
        f"downloads running, got {len(inner.commands)} calls"
    )

    # The in-flight types finished and were kept, per the cancel convention.
    overture_work = result.paths.work_dir / "raw" / "overture"
    for overture_type in types[:cap]:
        assert (overture_work / f"{overture_type}.geojson").exists(), (
            f"{overture_type} was in flight when the stop landed and was "
            f"thrown away"
        )
    assert not (overture_work / "address.geojson").exists()
    assert (result.paths.root / f"{result.paths.stem}_water.geojson").exists()

    # Task 22's rule, unchanged: never reached is pending, not failed.
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["stopped"] is True
    statuses = {record["overture"] for record in payload["tiles"]}
    assert statuses == {"pending"}, (
        f"a stop that never reached a tile must leave it pending, got {statuses}"
    )
    assert [e for e in log.events if e["event"] == "tile_failed"] == []

    # And it genuinely resumes: the kept types are skipped, only the
    # missing one is fetched, and the package completes.
    resume_runner = _FakeOvertureRunner()
    clear_registry()
    register(OvertureSource(runner=resume_runner, executable_finder=lambda _n: "overturemaps"))
    resumed = run_survey(request)

    assert resumed.complete is True
    assert resumed.paths.root == result.paths.root
    fetched = {cmd[cmd.index("--type") + 1] for cmd in resume_runner.commands}
    assert fetched == {"address"}, (
        f"the resume should have refetched only the missing type, got {fetched}"
    )


# --- Task 23 item 5: with no tile-stamped files left, Overture now
# contributes no tile-stamped directory to _record_tile_outcomes, so it
# falls into the `elif files:` branch and the batch outcome applies
# uniformly to every tile, exactly as ElevationSource's single whole-area
# file already does. That needed no change to package.py, which is a claim
# worth confirming by test rather than by reading, and against the REAL
# OvertureSource rather than a stub that could easily be more permissive
# than the real thing. -----------------------------------------------


def test_an_overture_only_run_that_succeeds_marks_every_tile_ok(tmp_path):
    runner = _FakeOvertureRunner()
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))
    result = run_survey(
        _request(tmp_path, source_ids=("overture",), overture_types=("water",))
    )

    assert result.complete is True
    records = result.survey["tiles"]
    assert len(records) == 4, "the plan's own four tiles should all be recorded"
    assert all(record["overture"] == "ok" for record in records), records


def test_an_overture_only_run_whose_fetch_raises_marks_every_tile_failed(tmp_path):
    # The other half. A whole-extent download either lands or it does not,
    # so there is no per-tile signal to be had and every tile is failed
    # together, which is honest: none of them has data.
    #
    # Task 32 changed WHICH exception an unforced run ends on, and only
    # that. Overture now says why it failed, per tile, so the run defers
    # the decision to the end (exactly as it has done for OSM since Task
    # 30) instead of dying inside the source loop, and what raises is
    # IncompleteSurveyError. Everything this test was written to pin is
    # asserted here still, plus the two things the deferral buys: the
    # package writes a survey.json that explains itself, and the CLI's own
    # words are not lost, they arrive through source_failed.
    class ExplodingRunner:
        def __init__(self):
            self.commands = []

        def __call__(self, command, **kwargs):
            self.commands.append(command)
            return type(
                "FakeCompleted",
                (),
                {"returncode": 1, "stdout": "", "stderr": "the release is on fire"},
            )()

    register(
        OvertureSource(runner=ExplodingRunner(), executable_finder=lambda _n: "overturemaps")
    )
    failures = []
    source_errors = []

    class Sink:
        def emit(self, event, **fields):
            if event == "tile_failed":
                failures.append(fields["tile_id"])
            elif event == "source_failed":
                source_errors.append(fields["error"])

    request = _request(tmp_path, source_ids=("overture",), overture_types=("water",))
    with pytest.raises(IncompleteSurveyError) as excinfo:
        run_survey(request, progress=Sink())

    # Every tile, together, unchanged.
    assert sorted(set(failures)) == ["r00_c00", "r00_c01", "r01_c00", "r01_c01"]
    assert "overture" in str(excinfo.value)

    # The CLI's own message is still delivered, in the one place that can
    # carry it without putting a third party's output into survey.json.
    assert any("the release is on fire" in error for error in source_errors)

    # And the package now explains itself instead of being scratch files.
    root = tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    payload = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert all(record["overture"] == "failed" for record in payload["tiles"])
    assert {r["source"] for r in payload["tile_failures"]} == {"overture"}
    assert {r["kind"] for r in payload["tile_failures"]} == {"refused"}

    tile_ids = ["r00_c00", "r00_c01", "r01_c00", "r01_c01"]
    assert sorted(failures) == tile_ids, (
        "every tile should be reported failed, since none of them got data"
    )

    # Recorded to state.json before the re-raise, so a resume knows nothing
    # landed rather than trusting the batch call's own silence.
    paths = build_package_paths(
        tmp_path,
        request.region,
        request.site,
        request.effective_date,
        tiling_fingerprint(
            *request.bbox.as_tuple(),
            request.tile_size_m,
            request.overlap_m,
            request.effective_categories,
            request.effective_overture_types,
        ),
    )
    state = JobState.load_or_create(paths.work_dir, tile_ids, ["overture"])
    for tile_id in tile_ids:
        assert state.is_done(tile_id, "overture") is False


def test_overture_type_selection_actually_reaches_the_fetch_not_just_the_registry(tmp_path):
    runner = _FakeOvertureRunner()
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))
    result = run_survey(
        _request(tmp_path, source_ids=("overture",), overture_types=("water", "building"))
    )
    assert result.complete is True
    fetched_types = {cmd[cmd.index("--type") + 1] for cmd in runner.commands}
    assert fetched_types == {"water", "building"}, (
        f"expected only the requested types to be fetched, got {fetched_types}"
    )


def test_overture_type_selection_does_not_mutate_the_registered_instance(tmp_path):
    # The property that makes this safe against a concurrent /api/estimate
    # for a different selection while a job using this instance is
    # running: the registered singleton must come out the other side of a
    # narrowed-selection run exactly as it went in.
    runner = _FakeOvertureRunner()
    registered = OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps")
    register(registered)
    run_survey(_request(tmp_path, source_ids=("overture",), overture_types=("water",)))
    assert registered.types == DEFAULT_OVERTURE_TYPES
    assert get_source("overture") is registered


def test_estimate_reflects_a_narrowed_overture_type_selection(tmp_path):
    register(OvertureSource())
    full = estimate_survey(_request(tmp_path, source_ids=("overture",)))
    narrowed = estimate_survey(
        _request(tmp_path, source_ids=("overture",), overture_types=("water",))
    )
    assert narrowed["bytes_estimate"] < full["bytes_estimate"]


def test_survey_json_records_the_overture_types_actually_fetched(tmp_path):
    runner = _FakeOvertureRunner()
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))
    result = run_survey(
        _request(tmp_path, source_ids=("overture",), overture_types=("water", "building"))
    )
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    overture_entry = next(s for s in payload["sources"] if s["id"] == "overture")
    assert sorted(overture_entry["types"]) == ["building", "water"]


def test_survey_json_source_entry_omits_types_for_a_source_with_no_such_concept(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert "types" not in payload["sources"][0]


# --- Task 19: category selection, end to end through SurveyRequest/
# _configured_sources/survey.json. The category vocabulary's own mapping
# logic (osm_tag_clauses, overture_types_for_categories) is covered
# directly in tests/test_categories.py; these tests are about the
# ORCHESTRATION: does a request's category selection actually reach the
# sources it should, and get recorded truthfully. ---------------------


def test_effective_categories_defaults_to_everything(tmp_path):
    from mapgen.categories import ALL_CATEGORY_IDS

    request = _request(tmp_path)
    assert request.categories is None
    assert sorted(request.effective_categories) == sorted(ALL_CATEGORY_IDS)


def test_survey_request_rejects_an_unknown_category_at_construction(tmp_path):
    # A coordinator review's Critical 1, reproduced upstream of this test
    # in test_categories.py directly: --category building (missing its
    # "s") used to construct a SurveyRequest that ran to completion,
    # downloading nothing, while looking exactly like a legitimate,
    # deliberate "nothing selected" request. SurveyRequest itself is where
    # both the CLI and the web path already meet, so validating here, in
    # __post_init__, is what makes the rejection reach both without
    # either needing its own copy of the check.
    from mapgen.categories import UnknownCategoryError

    with pytest.raises(UnknownCategoryError, match="building"):
        _request(tmp_path, categories=("building",))


def test_survey_request_rejects_an_empty_category_selection_at_construction(tmp_path):
    # Task 21: unticking every category in the browser (or otherwise
    # constructing a request with categories=()) used to construct a
    # SurveyRequest that ran to completion, downloading nothing from
    # either OSM or Overture, and reporting complete: true with no
    # indication anything was wrong. Same construction-time code path as
    # the unknown-id case directly above (both go through validate_
    # categories inside SurveyRequest.__post_init__), so the CLI and the
    # browser both get this without either needing its own copy of the
    # check.
    from mapgen.categories import EmptyCategorySelectionError

    with pytest.raises(EmptyCategorySelectionError, match="category is needed"):
        _request(tmp_path, categories=())

    # categories=None (never asked about at all) must still mean every
    # category, exactly as it always has: the two must never collapse
    # into each other.
    _request(tmp_path, categories=None)


def test_survey_request_rejects_an_empty_layer_selection_at_construction(tmp_path):
    # Review finding C1, and the exact counterpart of the empty-category
    # case above. server.py built source_ids with
    # `payload.get("sources") or ("osm", "overture")`, so a browser that
    # sent sources: [] got the full default set downloaded: 72 rate-
    # limited OSM tiles and eight whole-extent Overture downloads of the
    # data the owner had just switched off. Refused at construction, the
    # one place --source and the browser's checklist meet, so neither
    # entry point can grow a way round it.
    from mapgen.sources.base import EmptySourceSelectionError

    with pytest.raises(EmptySourceSelectionError, match="layer is needed"):
        _request(tmp_path, source_ids=())


def test_effective_overture_types_prefers_an_explicit_overture_type_over_categories(tmp_path):
    # Two different ways of choosing the same thing must not both apply
    # at once: the CLI's own original, lower-level --overture-type wins
    # outright over a category selection, rather than the two being
    # merged in some guessed-at way.
    request = _request(
        tmp_path, overture_types=("building",), categories=("water",)
    )
    assert request.effective_overture_types == ["building"]


def test_effective_overture_types_derives_from_categories_when_no_explicit_overture_type(tmp_path):
    request = _request(tmp_path, categories=("buildings",))
    assert request.effective_overture_types == ["building"]


def test_effective_overture_types_defaults_to_the_full_set_with_neither(tmp_path):
    request = _request(tmp_path)
    assert request.effective_overture_types == list(DEFAULT_OVERTURE_TYPES)


class _RecordingOsmSession:
    """Like _FakeOsmSession, but records every POST body: what actually
    proves configure() was reached is the QUERY SENT, not merely that
    the run completed (a fake session returns canned XML regardless of
    what query it was asked for, so completion alone cannot tell a
    configured, tag-filtered fetch apart from an unconfigured one).
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.post_calls: list[bytes] = []

    def get(self, url, **kwargs):
        return self._responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append(kwargs["data"])
        return self._responses.pop(0)


def test_run_survey_configures_the_real_osm_source_with_the_requests_categories(tmp_path):
    # Proves the wiring reaches OsmSource specifically, using the real
    # class (not a stub) so a change to OsmSource.configure's own
    # signature would be caught here too.
    #
    # Registered BARE, exactly as register_default_sources() actually
    # constructs it (use_overpass defaults False): a coordinator review
    # found that an earlier version of this test registered the source
    # with use_overpass=True set MANUALLY, which sidestepped the exact
    # gap being tested for and let the real bug (configure() never
    # actually turning a category restriction into a working filter)
    # pass unnoticed. This must now route to Overpass on its own.
    session = _RecordingOsmSession([_FakeOsmResponse() for _ in range(4)])
    registered = OsmSource(session=session, sleeper=lambda _seconds: None, min_interval_seconds=0.0)
    register(registered)
    result = run_survey(
        _request(tmp_path, source_ids=("osm",), categories=("buildings",), run_bridge_step=False)
    )
    assert result.complete is True
    # The query actually sent must be the tag-filtered form: this is what
    # a mutation that skips calling configure() for "osm" cannot fake,
    # since an unconfigured registered instance (categories=None) sends
    # the original unfiltered query instead.
    assert all(b'["building"]' in body for body in session.post_calls)
    assert all(b"node(" not in body for body in session.post_calls)
    # The registered singleton itself must come out unchanged, the same
    # property already pinned for Overture.
    assert registered.categories is None
    assert registered.use_overpass is False, "must not mutate the registered instance's routing either"
    assert get_source("osm") is registered


def test_survey_json_records_the_overpass_endpoint_and_why_for_a_filtered_osm_run(tmp_path):
    # Coordinator finding: endpoints_used must show the endpoint a
    # filtered run ACTUALLY hit (an Overpass URL), not the map API's, and
    # survey.json must also say why, since a category filter silently
    # changing which shared service a run depends on is exactly the kind
    # of thing an audit trail should not require cross-referencing code
    # to work out.
    session = _RecordingOsmSession([_FakeOsmResponse() for _ in range(4)])
    registered = OsmSource(session=session, sleeper=lambda _seconds: None, min_interval_seconds=0.0)
    register(registered)
    result = run_survey(
        _request(tmp_path, source_ids=("osm",), categories=("buildings",), run_bridge_step=False)
    )
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    osm_entry = next(s for s in payload["sources"] if s["id"] == "osm")
    assert osm_entry["endpoints_used"] == [registered.overpass_urls[0]]
    assert "map API" not in " ".join(osm_entry["endpoints_used"])
    assert "Overpass" in osm_entry["routing_note"]
    assert "category filter" in osm_entry["routing_note"]


def test_survey_json_records_the_map_api_endpoint_with_no_routing_note_when_unfiltered(tmp_path):
    responses = [_FakeOsmResponse() for _ in range(4)]
    registered = OsmSource(
        session=_FakeOsmSession(responses), sleeper=lambda _seconds: None, min_interval_seconds=0.0
    )
    register(registered)
    result = run_survey(_request(tmp_path, source_ids=("osm",), run_bridge_step=False))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    osm_entry = next(s for s in payload["sources"] if s["id"] == "osm")
    assert osm_entry["endpoints_used"] == [registered.osm_api_url]
    assert "routing_note" not in osm_entry


# --- Coordinator finding: the node-cap response must not fire for a run
# that configure() has already routed to Overpass, since the 50000-node
# cap is the map API's own limit and Overpass is not subject to it.
# Confirmed through the full orchestration, not only at OsmSource's own
# unit level. (Task 26 replaced the whole-run retry ladder this originally
# guarded with per-tile subdivision; the coherence being checked is the
# same one, against whatever the response happens to be.) --------------


class _NodeCapShapedOverpassSession:
    """An Overpass session that answers every POST with a 400 body
    SHAPED like the map API's own node-cap error text, to prove
    orchestration-level coherence: even if such a response somehow came
    back from Overpass, OsmSource's own use_overpass-scoped check (unit-
    tested directly in test_sources_osm.py) means this must never be
    classified as a NodeCapExceededError, so nothing reserved for a
    node-cap failure may engage for it either.
    """

    def __init__(self):
        self.post_calls = 0

    def get(self, url, **kwargs):
        raise AssertionError("expected the Overpass (POST) path, not the map API (GET) one")

    def post(self, url, **kwargs):
        self.post_calls += 1
        return type(
            "R", (), {"status_code": 400, "text": "You requested too many nodes", "headers": {}}
        )()


def test_a_node_cap_shaped_failure_on_overpass_is_not_treated_as_a_node_cap(tmp_path):
    session = _NodeCapShapedOverpassSession()
    registered = OsmSource(session=session, sleeper=lambda _seconds: None, min_interval_seconds=0.0)
    register(registered)
    log = EventLog()
    with pytest.raises(Exception) as excinfo:
        run_survey(
            _request(
                tmp_path,
                tile_size_m=2000.0,
                overlap_m=100.0,
                source_ids=("osm",),
                categories=("buildings",),  # routes to Overpass
                run_bridge_step=False,
            ),
            progress=log,
        )
    from mapgen.sources.osm import NodeCapExceededError

    assert not isinstance(excinfo.value, NodeCapExceededError)
    # Exactly max_retries (4, the default) POSTs for the one and only
    # tile this reached, and not one request more: an ordinary retried-
    # then-reported failure, with nothing node-cap-specific happening on
    # top of it.
    assert session.post_calls == 4


def test_estimate_states_the_overpass_routing_for_a_narrowed_osm_category_selection(tmp_path):
    # Coordinator finding: a narrowed selection now genuinely routes OSM
    # through Overpass (see OsmSource.configure), so the estimate's
    # message changed from a caveat about a broken control ("no effect")
    # to a statement of fact about which endpoint this run depends on.
    register(OsmSource())  # use_overpass=False; configure() upgrades it
    estimate = estimate_survey(_request(tmp_path, source_ids=("osm",), categories=("buildings",)))
    assert any("Overpass" in w and "category filter" in w for w in estimate["warnings"])


def test_estimate_has_no_osm_routing_note_when_every_category_is_selected(tmp_path):
    register(OsmSource())
    estimate = estimate_survey(_request(tmp_path, source_ids=("osm",)))
    assert estimate["warnings"] == []


def test_survey_json_records_the_resolved_category_selection(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, categories=("buildings", "water")))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert sorted(payload["categories"]) == ["buildings", "water"]


def test_survey_json_records_every_category_by_default(tmp_path):
    from mapgen.categories import ALL_CATEGORY_IDS

    register(StubSource())
    result = run_survey(_request(tmp_path))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert sorted(payload["categories"]) == sorted(ALL_CATEGORY_IDS)


def test_a_category_selection_that_maps_to_no_overture_type_fetches_nothing_from_overture(
    tmp_path,
):
    # A coordinator review's Critical 2, end to end: "rail" alone maps to
    # no Overture type at all (overture_types_for_categories(["rail"])
    # is [], see test_categories.py), which used to reach OvertureSource
    # as `types or DEFAULT_OVERTURE_TYPES` and silently fetch the full
    # 8-type default instead of the nothing the selection actually asked
    # for. runner.commands staying empty is the real-run-shaped proof:
    # not just that .types looks right in isolation (test_sources_
    # overture.py covers that directly), but that the orchestration this
    # request actually drives never once shells out to overturemaps.
    runner = _FakeOvertureRunner()
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))
    result = run_survey(_request(tmp_path, source_ids=("overture",), categories=("rail",)))
    # complete is False here, correctly, for a reason this fix does not
    # touch: _record_tile_outcomes' own pre-existing "vacuous is not
    # done" rule (see its docstring) marks every tile FAILED when a
    # source's work directory ends up with no files on disk at all,
    # rather than assume zero files must mean zero was correct. Still
    # finishes and writes survey.json rather than raising either way:
    # assert_inputs_present never objects to an empty expected list, with
    # or without force, since there is nothing in it to call a gap.
    assert result.complete is False
    assert runner.commands == [], f"expected no overturemaps calls at all, got {runner.commands}"
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    overture_entry = next(s for s in payload["sources"] if s["id"] == "overture")
    assert overture_entry["types"] == []


# --- A coordinator review's Important 1: resuming an incomplete package
# after changing the category selection used to reuse the SAME _work/
# fingerprint directory (nothing about the selection was part of the hash),
# so a source's old output from the FIRST selection was still sitting
# there, indistinguishable from real output for the SECOND, when merge()
# went looking for "everything on disk for this source". Fixed by folding
# effective_categories/effective_overture_types into tiling_fingerprint
# (see naming.py and test_naming.py's own direct tests of that function);
# this is the end-to-end proof, through run_survey twice, of the actual
# failure mode the coordinator reproduced live: a stale _water.geojson
# surviving a buildings-only rerun. -------------------------------------


def test_run_survey_gives_a_resumed_request_a_different_work_dir_when_only_categories_changed(
    tmp_path,
):
    # The most direct wiring proof: both work_dir's below come from two
    # REAL run_survey calls, never from a value this test recomputed
    # itself and compared against. That distinction matters: a mutation
    # that has _plan() call tiling_fingerprint with the right SHAPE of
    # arguments but the wrong, or a constant, VALUE (for example
    # hardcoding () instead of passing request.effective_categories)
    # would still coincidentally satisfy an assertion built by
    # recomputing "the expected fingerprint" the same wrong way outside
    # it; two independent real calls, compared only against each other,
    # cannot pass by that accident.
    stub = StubSource(fail_on=("r00_c00",))
    register(stub)
    first = run_survey(_request(tmp_path, categories=("water",), force=True, keep_work=True))
    assert first.complete is False

    stub._fail_on.clear()
    second = run_survey(_request(tmp_path, categories=("buildings",), force=True, keep_work=True))
    assert second.complete is True

    # Same root: an incomplete package is always resumed at the same
    # root (root is not fingerprinted, only work_dir is).
    assert second.paths.root == first.paths.root
    # A genuinely different work_dir all the same, because the category
    # selection differed between the two calls.
    assert second.paths.work_dir != first.paths.work_dir


def test_a_resumed_run_with_a_different_category_selection_gets_a_fresh_work_dir(tmp_path):
    # The mechanism itself, proven the same way
    # test_force_run_excludes_leftover_part_files_from_a_hard_kill proves
    # its converse: that one plants debris at the fingerprint a request
    # WOULD use and shows it IS picked up; this plants debris at the
    # fingerprint a DIFFERENT, water-only request would have used, and
    # shows a buildings-only request, resuming the very same package root
    # (root is not fingerprinted, only work_dir is; a root with no
    # survey.json is always resumed, per build_package_paths), never
    # even looks there. mkdir(parents=True) below also brings the shared
    # root into existence, which is what makes the buildings-only
    # request's own build_package_paths call resume it rather than mint
    # a fresh _02: no survey.json exists yet, so _survey_reports_complete
    # is False and the existing, still-empty-of-a-survey root is reused,
    # exactly as an interrupted real first attempt would leave it.
    water_only = _request(tmp_path, source_ids=("overture",), categories=("water",))
    stale_fingerprint = tiling_fingerprint(
        *water_only.bbox.as_tuple(),
        water_only.tile_size_m,
        water_only.overlap_m,
        water_only.effective_categories,
        water_only.effective_overture_types,
    )
    stale_paths = build_package_paths(
        tmp_path, water_only.region, water_only.site, water_only.effective_date, stale_fingerprint
    )
    stale_water_file = stale_paths.work_dir / "raw" / "overture" / "water" / "r00_c00.geojson"
    stale_water_file.parent.mkdir(parents=True, exist_ok=True)
    stale_water_file.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")

    runner = _FakeOvertureRunner()
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))
    # keep_work=True so a successful run's own cleanup (which deletes the
    # WHOLE _work/ parent, sibling fingerprints included, once the job is
    # complete: see run_survey's own comment on best_effort_rmtree) does
    # not remove the planted file before this test gets to look for it.
    buildings_only = _request(
        tmp_path, source_ids=("overture",), categories=("buildings",), keep_work=True
    )
    result = run_survey(buildings_only)

    assert result.complete is True
    # The resume premise: the very same package root the water-only
    # request's own paths would have used.
    assert result.paths.root == stale_paths.root
    # Important 1's actual fix: a DIFFERENT work_dir all the same, because
    # the category selection is now part of the fingerprint. Before this
    # fix these were equal, and the stale water file above would have sat
    # inside THIS run's own work_dir, exactly where _existing_output_files
    # looks, and been handed to merge() alongside the real building output.
    assert result.paths.work_dir != stale_paths.work_dir
    assert stale_water_file.exists(), "the planted file should still be exactly where it was left"

    # The two consequences that matter: no water fetch happened at all
    # (the buildings-only OvertureSource this run configured has no
    # reason to ever look at a water/ directory, stale or otherwise)...
    for command in runner.commands:
        assert "water" not in command, f"a water fetch happened for a buildings-only run: {command}"
    # ...and no water output landed in the finished package: the stale
    # file sat in a work_dir this run's own merge() never scanned.
    assert (result.paths.root / f"{result.paths.stem}_building.geojson").is_file()
    assert not (result.paths.root / f"{result.paths.stem}_water.geojson").exists()

    # What survey.json now says in the resume case: this run's own
    # selection, matching what is actually on disk above.
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["categories"] == ["buildings"]
    overture_entry = next(s for s in payload["sources"] if s["id"] == "overture")
    assert overture_entry["types"] == ["building"]


# --- Task 21 defect 3: does resume actually work on the owner's real,
# interrupted package? C:\Users\Param\Surveys\Vale-of-Glamorgan\
# 2026-08-03_Barry holds 20 completed OSM tiles and 34 Overture files
# under _work/1327f515/, a second, unrelated fingerprint directory
# 43d56d4d/ left over from an earlier, different selection, one merged
# .osm already sitting in the root, no .part files, and no survey.json:
# the process died mid Overture fetch, after OSM had already finished and
# been merged. This replicates that exact shape at a smaller scale (4
# tiles rather than however many the real bbox produces) using the REAL
# OsmSource and OvertureSource, not a stub shaped to be more permissive
# than the real thing, and a hand-written state.json matching what
# _record_tile_outcomes would genuinely have written by that point in the
# crash. Nothing here touches the owner's actual folder. ------------------


def test_resume_replicates_the_owners_real_interrupted_package_and_completes(tmp_path):
    request = _request(
        tmp_path,
        source_ids=("osm", "overture"),
        overture_types=("water", "building"),
        run_bridge_step=False,
        # Kept so the planted debris and raw tiles survive to inspect
        # after the run: a normal complete run removes the whole _work/
        # parent, which would otherwise erase the evidence this test
        # exists to check.
        keep_work=True,
    )
    fingerprint = tiling_fingerprint(
        *request.bbox.as_tuple(),
        request.tile_size_m,
        request.overlap_m,
        request.effective_categories,
        request.effective_overture_types,
    )
    paths = build_package_paths(
        tmp_path, request.region, request.site, request.effective_date, fingerprint
    )
    # BBOX at 600m tiles / 50m overlap (this file's own _request defaults)
    # produces exactly these four tile ids; computed once, independently,
    # in test_naming.py/test_geo.py's own tests, hardcoded here the same
    # way test_a_resumed_run_only_refetches_tiles_that_previously_failed
    # already hardcodes them above.
    tile_ids = ["r00_c00", "r00_c01", "r01_c00", "r01_c01"]

    # OSM: every tile already fetched and merged, exactly as it would be
    # after a clean first stage that finished before the crash.
    osm_raw = paths.work_dir / "raw" / "osm"
    osm_raw.mkdir(parents=True)
    for tile_id in tile_ids:
        (osm_raw / f"{tile_id}.osm").write_text(_MINIMAL_OSM_XML, encoding="utf-8")
    paths.root.mkdir(parents=True, exist_ok=True)
    (paths.root / f"{paths.stem}.osm").write_text(_MINIMAL_OSM_XML, encoding="utf-8")

    # Deliberately NO state.json planted here, even though a real crash
    # this far in would very likely have left one recording osm as ok
    # for every tile (_record_tile_outcomes calls state.save() after each
    # tile is marked). Proving resume purely from the raw files on disk,
    # with no bookkeeping to lean on, is the stronger and more honest
    # claim: an early version of this test that pre-marked osm "ok" in a
    # hand-written state.json passed even with OsmSource's own per-file
    # skip check disabled, because pending came back empty before that
    # check was ever reached. Leaving state.json out entirely means
    # pending is every osm tile again, and the only thing that can still
    # stop a redundant download is OsmSource.fetch's own existence check,
    # which is the real behaviour this test exists to confirm.
    assert not (paths.work_dir / "state.json").exists()

    # Overture: 6 of the 8 tile/type combinations already on disk, written
    # in the PER-TILE layout that Task 23 superseded, because that is the
    # layout the owner's one part-downloaded package on disk actually
    # holds. r01_c01 is the one tile the crash caught mid-way, missing both
    # types, the same "most tiles done, one caught mid-flight" shape as the
    # real package's 34-out-of-however-many files. What happens to these
    # files on a resume under the new layout is asserted on directly at the
    # end of this test.
    preexisting = {
        (tile_id, overture_type)
        for tile_id in ("r00_c00", "r00_c01", "r01_c00")
        for overture_type in ("water", "building")
    }
    for tile_id, overture_type in preexisting:
        part_dir = paths.work_dir / "raw" / "overture" / overture_type
        part_dir.mkdir(parents=True, exist_ok=True)
        (part_dir / f"{tile_id}.geojson").write_text(
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": f"preexisting-{tile_id}-{overture_type}",
                            "geometry": None,
                            "properties": {},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    # A second, unrelated fingerprint directory, exactly like the real
    # package's 43d56d4d/: debris from a different tiling that a resume
    # must never read from. Poisoned with a tile id that also exists in
    # the CURRENT plan, so any future regression that scanned the wrong
    # fingerprint's raw/osm/ (instead of paths.work_dir's own) would pick
    # this up rather than being missed by coincidence.
    stale_fingerprint = tiling_fingerprint(
        *request.bbox.as_tuple(),
        2000.0,
        100.0,
        request.effective_categories,
        request.effective_overture_types,
    )
    assert stale_fingerprint != fingerprint
    stale_osm_dir = paths.root / "_work" / stale_fingerprint / "raw" / "osm"
    stale_osm_dir.mkdir(parents=True)
    poison_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osm version="0.6" generator="test">\n'
        '  <node id="999999" version="1" lat="0.0" lon="0.0"/>\n'
        "</osm>\n"
    )
    stale_osm_file = stale_osm_dir / "r00_c00.osm"
    stale_osm_file.write_text(poison_xml, encoding="utf-8")

    # No survey.json, no .part files: nothing else is written. This is the
    # incomplete state resume exists for.
    assert not paths.survey_json.exists()

    # Any real call at all pops from an empty list and, after exhausting
    # retries, raises OsmDownloadError: every OSM tile is already
    # complete, so a correctly resumed run must never touch this session.
    osm_session = _FakeOsmSession([])
    register(OsmSource(session=osm_session, sleeper=lambda _s: None, min_interval_seconds=0.0))
    overture_runner = _FakeOvertureRunner()
    register(OvertureSource(runner=overture_runner, executable_finder=lambda _n: "overturemaps"))

    result = run_survey(request)

    assert result.complete is True
    assert result.paths.root == paths.root
    assert result.paths.work_dir == paths.work_dir
    assert result.paths.survey_json.exists()

    # Task 23: two calls, one per type, over the WHOLE request bbox.
    #
    # This assertion survived Task 23 unchanged in its number and would
    # have been a test passing for the wrong reason if left at that: it
    # used to mean "the 2 of 8 missing tile/type pairs were refetched and
    # the other 6 were resumed", and it now means "each of the 2 types was
    # fetched exactly once, and the per-tile files were not resumed at
    # all". Same 2, completely different claim. The --bbox check below is
    # what actually tells the two apart, so it is not optional decoration.
    assert len(overture_runner.commands) == 2, (
        f"expected exactly one call per type, got "
        f"{len(overture_runner.commands)}: {overture_runner.commands}"
    )
    fetched_types = {cmd[cmd.index("--type") + 1] for cmd in overture_runner.commands}
    assert fetched_types == {"water", "building"}
    for command in overture_runner.commands:
        assert f"--bbox={request.bbox.to_query_string()}" in command, (
            "Overture queried something other than the whole request bbox, "
            "which is what a per-tile query would look like"
        )

    # OSM never contacted a live endpoint: every tile was already done.
    osm_entry = next(s for s in result.survey["sources"] if s["id"] == "osm")
    assert osm_entry["endpoints_used"] == [], (
        "OSM re-contacted an endpoint even though every tile already had a "
        "successful file on disk"
    )

    # The 34-files-under-a-per-tile-layout question, answered on the real
    # shape rather than in prose (Task 23).
    #
    # Those files are orphaned: the resume check looks for <type>.geojson
    # and will never find <type>/<tile_id>.geojson, so Overture refetches
    # from scratch. That is accepted, not worked around: refetching untiled
    # is 2 calls here and 8 on the owner's real package, where resuming the
    # old layout would have been 160. What is NOT accepted is leaving the
    # orphans on disk, which was the defect this found. package.py hands
    # every file under work_dir to merge() and to _record_tile_outcomes,
    # and merge() now reads a file stem as a TYPE, so a surviving
    # r00_c00.geojson becomes a <stem>_r00_c00.geojson sitting in the
    # finished package beside the real layers, while _record_tile_outcomes
    # sees a tile-stamped directory again and marks r01_c01, the tile the
    # crash never reached, FAILED forever. Both were reproduced before
    # OvertureSource's sweep was written; both are asserted against here.
    for overture_type in ("water", "building"):
        assert not (paths.work_dir / "raw" / "overture" / overture_type).exists(), (
            f"the superseded per-tile directory for {overture_type} survived the resume"
        )
    assert sorted(
        p.name for p in (paths.work_dir / "raw" / "overture").iterdir()
    ) == ["building.geojson", "water.geojson"]

    root_outputs = sorted(p.name for p in paths.root.iterdir() if p.is_file())
    assert root_outputs == [
        f"{paths.stem}.osm",
        f"{paths.stem}_building.geojson",
        # Task 35. The owner's own resumed package now ends with the one
        # file Urbano 2 is actually pointed at, which is the whole reason
        # that task exists: every package they already have was produced by
        # a run whose bridge failed, so none of them had it.
        f"{paths.stem}_project_setting.json",
        f"{paths.stem}_water.geojson",
        "survey.json",
    ], f"a stale tile id reached the package root as a merged output: {root_outputs}"

    # r01_c01 is the tile the crash caught mid-flight, with neither type on
    # disk. The whole-extent download covers it like every other tile, so
    # it is ok, and no tile is failed.
    tile_records = {record["tile_id"]: record for record in result.survey["tiles"]}
    assert tile_records["r01_c01"]["overture"] == "ok"
    assert all(record["overture"] == "ok" for record in result.survey["tiles"])

    # The second, stale fingerprint directory was never read: its poison
    # tile is untouched on disk and never reached the merged output.
    assert stale_osm_file.read_text(encoding="utf-8") == poison_xml
    merged_osm_text = (paths.root / f"{paths.stem}.osm").read_text(encoding="utf-8")
    assert "999999" not in merged_osm_text, (
        "the stale second fingerprint directory leaked into the merged output"
    )


# --- Task 26: a subdivided tile is an ordinary tile from out here ----------
#
# The point of putting subdivision inside OsmSource.fetch is that nothing
# in this file has to know about it. These are the tests that hold that
# claim to account through the real orchestration: the tile is recorded
# ok, the quarters never reach merge(), and a stop landing mid-split
# leaves the tile pending rather than failed or half-finished.


class _DenseTileOsmSession:
    """An OSM map API that refuses any request wider than max_span degrees
    with the real "too many nodes" 400, and answers anything smaller with
    a node stamped with the bbox it came from.

    Density by area, which is what actually drives the node cap: a tile
    fails and its quarters do not, without the session having to count
    calls or know anything about subdivision.
    """

    def __init__(self, max_span=0.006):
        # 0.006 degrees sits between the tile these tests use (about
        # 0.0094 degrees of longitude across, including its overlap) and
        # its quarters (about 0.0050), so every tile splits exactly once
        # and no quarter splits again. One level is enough to prove the
        # orchestration; the recursion itself is covered in
        # test_sources_osm.py against a model of the ground.
        self.max_span = max_span
        self.bboxes = []

    def get(self, url, **kwargs):
        west, south, east, north = (
            float(value) for value in kwargs["params"]["bbox"].split(",")
        )
        self.bboxes.append((west, south, east, north))
        if (east - west) > self.max_span:
            return _FakeOsmResponse(status_code=400, text="You requested too many nodes")
        node_id = abs(hash((round(west, 7), round(south, 7)))) % 10_000_000
        return _FakeOsmResponse(
            text=(
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                '<osm version="0.6" generator="test">\n'
                f'  <node id="{node_id}" version="1" lat="{south}" lon="{west}"/>\n'
                "</osm>\n"
            )
        )

    def post(self, url, **kwargs):
        raise AssertionError("expected the map API (GET) path")


def _osm_only_request(tmp_path, **overrides):
    defaults = dict(
        source_ids=("osm",),
        tile_size_m=600.0,
        overlap_m=50.0,
        run_bridge_step=False,
    )
    defaults.update(overrides)
    return _request(tmp_path, **defaults)


def test_a_subdivided_tile_is_recorded_ok_like_any_other_tile(tmp_path):
    session = _DenseTileOsmSession()
    register(OsmSource(session=session, sleeper=lambda _s: None, min_interval_seconds=0.0))

    result = run_survey(_osm_only_request(tmp_path))

    assert result.complete is True
    assert all(record["osm"] == "ok" for record in result.survey["tiles"]), (
        f"a subdivided tile was not recorded ok: {result.survey['tiles']}"
    )
    # The tiling in survey.json is the one that was asked for. The old
    # ladder used to rewrite it, so a package could say 1000 m when the
    # owner asked for 2000; subdivision leaves the plan alone.
    assert result.survey["tiling"]["tile_size_m"] == 600.0
    assert result.survey["tiling"]["overlap_m"] == 50.0
    # And every tile really did have to split, so this is not passing by
    # never exercising the path.
    assert any(
        (east - west) > session.max_span for west, _s, east, _n in session.bboxes
    )


def test_a_file_named_for_a_tile_of_another_tiling_is_never_offered_to_merge(tmp_path):
    """The stale-tiling filter itself, which nothing distinguished: disabling
    the _TILE_ID_SHAPE exclusion entirely passed the whole suite.

    The two tests that look like they cover it do not. One asserts the
    opposite direction, that a whole-area file with no tile id in its name is
    still gathered; the other, the retiling one, is satisfied by the work_dir
    fingerprint putting the old run's files in a different folder altogether.
    Neither notices whether this filter is there.

    What it is for: a tile id is only (row, col) and carries no memory of the
    tile_size_m or overlap_m it was computed under, so r00_c00 of a 2000 m
    tiling and r00_c00 of a 600 m one are the same string over different
    ground. Directly on the function, because the whole point is that the
    layers above it happen to shield it today.
    """
    from mapgen.package import _existing_output_files

    source_work = tmp_path / "raw" / "stub"
    source_work.mkdir(parents=True)
    for name in ("r00_c00.osm", "r09_c09.osm", "Barry_2026-08-01.tif"):
        (source_work / name).write_text("data", encoding="utf-8")

    offered = [path.name for path in _existing_output_files(source_work, ["r00_c00"])]

    assert "r00_c00.osm" in offered, "this tiling's own tile"
    assert "r09_c09.osm" not in offered, (
        "a tile id from another tiling was offered to merge, under a name "
        "this tiling would reuse"
    )
    # No tile id in the name, so no tiling assumption to be stale. This is
    # elevation's whole-area DEM and it always belongs.
    assert "Barry_2026-08-01.tif" in offered


def test_the_quarters_of_a_subdivided_tile_never_reach_the_package(tmp_path):
    # _existing_output_files feeds both merge() and the per-tile outcome
    # check. A quarter is real .osm data sitting in the source's work
    # directory, so without the scratch-directory rule it would be merged
    # into the finished package as though it were a tile's own output.
    session = _DenseTileOsmSession()
    register(OsmSource(session=session, sleeper=lambda _s: None, min_interval_seconds=0.0))

    result = run_survey(_osm_only_request(tmp_path, keep_work=True))

    from mapgen.package import _existing_output_files
    from mapgen.sources.osm import SPLIT_DIR_NAME

    source_work = result.paths.work_dir / "raw" / "osm"
    split_dir = source_work / SPLIT_DIR_NAME
    assert split_dir.is_dir(), "the quarters should still be on disk for a resume"
    assert list(split_dir.iterdir()), "the quarters should not have been deleted"

    tile_ids = [record["tile_id"] for record in result.survey["tiles"]]
    offered = _existing_output_files(source_work, tile_ids)
    assert offered, "the tiles themselves must still be offered to merge"
    assert all(path.parent == source_work for path in offered), (
        f"a scratch file was offered to merge as though it were tile output: {offered}"
    )
    assert sorted(p.stem for p in offered) == sorted(tile_ids)


def test_a_stop_mid_subdivision_leaves_the_tile_pending_not_failed(tmp_path):
    # Task 22's rule, one level further in than it was written: a tile a
    # stop never finished is pending, never failed, and the quarters it
    # did fetch must not be recombined into a tile file that would then
    # read as complete.
    token = CancelToken()

    class _StopsAfterOneQuarter(_DenseTileOsmSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if len(self.bboxes) == 2:  # the tile, then its first quarter
                token.cancel()
            return response

    session = _StopsAfterOneQuarter()
    register(OsmSource(session=session, sleeper=lambda _s: None, min_interval_seconds=0.0))

    result = run_survey(_osm_only_request(tmp_path, keep_work=True), cancel=token)

    assert result.stopped is True
    assert result.complete is False
    statuses = {record["tile_id"]: record["osm"] for record in result.survey["tiles"]}
    assert "failed" not in statuses.values(), (
        f"a stop mid-subdivision marked a tile failed: {statuses}"
    )
    assert "pending" in statuses.values()

    from mapgen.sources.osm import SPLIT_DIR_NAME

    source_work = result.paths.work_dir / "raw" / "osm"
    interrupted = [
        tile_id for tile_id, status in statuses.items() if status == "pending"
    ]
    for tile_id in interrupted:
        assert not (source_work / f"{tile_id}.osm").exists(), (
            "a half-fetched set of quarters was recombined into a tile file"
        )
    # The quarter that was paid for is kept, so the resume starts from it.
    assert list((source_work / SPLIT_DIR_NAME).iterdir())


def test_a_run_resumed_after_a_stop_mid_subdivision_completes(tmp_path):
    token = CancelToken()

    class _StopsAfterOneQuarter(_DenseTileOsmSession):
        def get(self, url, **kwargs):
            response = super().get(url, **kwargs)
            if len(self.bboxes) == 2:
                token.cancel()
            return response

    stopped_session = _StopsAfterOneQuarter()
    register(
        OsmSource(
            session=stopped_session, sleeper=lambda _s: None, min_interval_seconds=0.0
        )
    )
    stopped = run_survey(_osm_only_request(tmp_path, keep_work=True), cancel=token)
    assert stopped.complete is False
    # The tile's own request, then the one quarter that landed before the
    # stop: that quarter's bbox is what must not be asked for twice.
    assert len(stopped_session.bboxes) == 2
    already_paid_for = stopped_session.bboxes[1]

    clear_registry()
    resumed_session = _DenseTileOsmSession()
    register(
        OsmSource(session=resumed_session, sleeper=lambda _s: None, min_interval_seconds=0.0)
    )
    resumed = run_survey(_osm_only_request(tmp_path, keep_work=True))

    assert resumed.complete is True
    assert resumed.paths.root == stopped.paths.root, "the resume must reuse the same folder"
    assert all(record["osm"] == "ok" for record in resumed.survey["tiles"])
    # Resumed INTO the subdivision, not restarted: the quarter already on
    # disk is never asked for again, while its three siblings are.
    assert already_paid_for not in resumed_session.bboxes, (
        "the resumed run refetched a quarter that was already on disk"
    )
    assert sum(1 for box in resumed_session.bboxes if box[0] == already_paid_for[0]) >= 1, (
        "the resumed run should still have fetched the tile and the "
        "remaining quarters that share this westing"
    )


# --- the stale-output sweep (final review residual, HANDOFF item 4) ---------
#
# The fingerprinted work directory isolates each selection's RAW tiles, but
# merged outputs land in the shared package root, so a failed wide attempt
# followed by a completed narrow one left the wide attempt's merged file
# beside a survey.json that never mentioned it. These pin the sweep that
# closes that: only names a source declares in possible_outputs(stem) are
# candidates, only a COMPLETE run sweeps, and files the user put in the
# folder are untouchable because they can never match the closed list.


class SweepingStubSource(StubSource):
    """StubSource plus the possible_outputs declaration the sweep reads.

    Declares more names than merge() ever writes, the way OvertureSource
    declares all eight types while a narrowed run merges two: the extra
    names are exactly the ones a previous, wider attempt could have left
    behind.
    """

    def possible_outputs(self, stem):
        return [
            f"{self.id}.txt",
            f"{stem}_water.geojson",
            "layers/water.geojson",
        ]


def _root_with_leftovers(tmp_path):
    """The exact root _request()'s defaults compose, pre-seeded with one
    stale pair a wider attempt could have merged plus two files only a
    user would have put there."""
    root = tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    stem = "Barry-Waterfront_2026-08-01"
    (root / "layers").mkdir(parents=True)
    (root / f"{stem}_water.geojson").write_text("stale wide merge", encoding="utf-8")
    (root / "layers" / "water.geojson").write_text("stale layer copy", encoding="utf-8")
    (root / f"{stem}_notes.txt").write_text("the owner's own note", encoding="utf-8")
    (root / "random.txt").write_text("also the owner's", encoding="utf-8")
    return root, stem


def test_a_complete_run_sweeps_stale_merged_outputs_and_nothing_else(tmp_path):
    register(SweepingStubSource())
    root, stem = _root_with_leftovers(tmp_path)
    log = EventLog()

    result = run_survey(_request(tmp_path), progress=log)

    assert result.complete is True
    # Reused the seeded root rather than suffixing to _02: no survey.json
    # meant an unfinished earlier attempt, which is the reuse rule.
    assert result.paths.root == root
    # The stale pair is gone, root and layers/ both.
    assert not (root / f"{stem}_water.geojson").exists()
    assert not (root / "layers" / "water.geojson").exists()
    # This run's own merge survived the sweep.
    assert (root / "stub.txt").read_text(encoding="utf-8") != ""
    # The user's files were never candidates.
    assert (root / f"{stem}_notes.txt").read_text(encoding="utf-8") == "the owner's own note"
    assert (root / "random.txt").read_text(encoding="utf-8") == "also the owner's"
    # And the run said what it removed, so the log is a complete account.
    removed = sorted(
        e["name"] for e in log.events if e["event"] == "stale_output_removed"
    )
    assert removed == [f"{stem}_water.geojson", "layers/water.geojson"]


def test_an_incomplete_run_keeps_every_leftover(tmp_path):
    # Deleting the only merged copy of anything mid-failure helps nobody:
    # survey.json already says complete false, and the resume that finishes
    # the job sweeps then. force=True is what lets a source failure mark
    # the package incomplete instead of raising, same as the other
    # incomplete-package tests above.
    register(SweepingStubSource(fail_on=("r01_c01",)))
    root, stem = _root_with_leftovers(tmp_path)

    result = run_survey(_request(tmp_path, force=True))

    assert result.complete is False
    assert (root / f"{stem}_water.geojson").exists()
    assert (root / "layers" / "water.geojson").exists()


def test_a_source_without_the_declaration_is_skipped_not_crashed(tmp_path):
    # possible_outputs is an optional extension like readiness_problem and
    # routing_note; a plain StubSource has no such method and the sweep
    # must read it defensively rather than assume the protocol grew.
    register(StubSource())
    root, stem = _root_with_leftovers(tmp_path)

    result = run_survey(_request(tmp_path))

    assert result.complete is True
    # Nothing declared, so nothing swept, and nothing raised.
    assert (root / f"{stem}_water.geojson").exists()


# --- Task 28: which elevation model, validated where categories are ------


class _FakeDemResponse:
    """The shape ElevationSource.fetch actually uses of a requests
    response: a context manager with a status code and iter_content.
    Deliberately the real ElevationSource in the tests below rather than a
    stub with a `demtype` attribute: a stub would prove that package.py
    passes a string to something, not that the source it passes it to
    fetches, merges and reports with it.
    """

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def iter_content(self, chunk_size=None):
        return iter([self._payload])

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FakeDemSession:
    def __init__(self, payload=b"II*\x00" + b"\x00" * 128):
        self._payload = payload
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return _FakeDemResponse(self._payload)


def _register_fake_elevation():
    from mapgen.sources.elevation import ElevationSource

    session = _FakeDemSession()
    register(ElevationSource(api_key="test-key", session=session))
    return session


def test_the_default_elevation_model_is_unchanged(tmp_path):
    assert _request(tmp_path).elevation_demtype == "COP30"


def test_survey_request_rejects_an_unknown_elevation_model_at_construction(tmp_path):
    # The same place, and the same reason, as the category checks above:
    # SurveyRequest is where the CLI's --demtype and the browser's select
    # already meet, so one check covers both. Left to the source, this
    # would only surface once OpenTopography had answered an error page
    # instead of a TIFF, as "did not return a TIFF", which names neither
    # the typo nor the valid values.
    from mapgen.elevation_models import UnknownDemTypeError

    with pytest.raises(UnknownDemTypeError, match="COP-30"):
        _request(tmp_path, elevation_demtype="COP-30")


def test_the_chosen_model_reaches_the_actual_download(tmp_path):
    session = _register_fake_elevation()

    run_survey(_request(tmp_path, source_ids=("elevation",), elevation_demtype="EU_DTM"))

    assert session.calls, "the elevation source was never asked for anything"
    assert session.calls[0]["params"]["demtype"] == "EU_DTM"


def test_survey_json_records_the_model_the_package_actually_holds(tmp_path):
    _register_fake_elevation()

    result = run_survey(
        _request(tmp_path, source_ids=("elevation",), elevation_demtype="EU_DTM")
    )

    entry = next(s for s in result.survey["sources"] if s["id"] == "elevation")
    assert entry["demtype"] == "EU_DTM"
    # And the terms recorded are that model's own, not the Copernicus
    # ones this source carried when COP30 was the only possibility.
    assert entry["licence"] == "CC BY 4.0"
    assert "Airbus" not in entry["attribution"]


def test_a_resumed_run_that_changed_model_downloads_rather_than_relabelling(tmp_path):
    # The failure the model-bearing filename exists to prevent, end to end
    # through run_survey rather than at the source in isolation: work_dir
    # is fingerprinted by TILING, so an earlier attempt's DEM really is
    # still sitting there when only the model changed.
    session = _register_fake_elevation()
    first = _request(tmp_path, source_ids=("elevation",), keep_work=True)
    run_survey(first)
    assert len(session.calls) == 1

    result = run_survey(
        _request(
            tmp_path,
            source_ids=("elevation",),
            elevation_demtype="EU_DTM",
            keep_work=True,
        )
    )

    assert len(session.calls) == 2, "the COP30 file was reused for an EU_DTM run"
    assert session.calls[1]["params"]["demtype"] == "EU_DTM"
    entry = next(s for s in result.survey["sources"] if s["id"] == "elevation")
    assert entry["demtype"] == "EU_DTM"


class _FailingDemSession(_FakeDemSession):
    """Answers the first request with a real TIFF and every one after it
    with a 401, which is what OpenTopography returns for a key that has
    been revoked or has run out of quota."""

    def __init__(self, payload=b"II*\x00COP30-COPERNICUS-DATA"):
        super().__init__(payload=payload)
        self.fail_from = 1

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) > self.fail_from:
            response = _FakeDemResponse(b"Unauthorized")
            response.status_code = 401
            return response
        return _FakeDemResponse(self._payload)


def test_a_forced_run_whose_dem_fails_does_not_leave_the_previous_models_tif(tmp_path):
    # Review finding I8, reproduced as the reviewer described it. An
    # incomplete package holds a COP30 DEM; the owner changes the model to
    # EU_DTM in Settings and re-runs with --force; OpenTopography answers
    # 401. merge() returns [] when no part matches this run's model, and
    # justified that as "a package with no DEM, and a survey.json that
    # says so, is a better outcome than one holding a DEM of a model it
    # does not name". Nothing produced that outcome: returning []
    # deleted nothing, and the stale-output sweep never runs on an
    # incomplete package, so the previous model's <stem>.tif stayed in
    # the root while _source_provenance recorded the CONFIGURED model and
    # __init__ had already swapped in that model's licence and citation.
    # The result was a licence statement about the wrong dataset.
    from mapgen.sources.elevation import ElevationSource

    # A second, always-failing source keeps the package INCOMPLETE, which
    # is what the finding is about: a complete package is never reused, so
    # a second run would land on _02 and never see the first one's DEM.
    # This is the owner's own situation, an attempt that did not finish
    # being re-run with --force.
    session = _FailingDemSession()
    register(ElevationSource(api_key="test-key", session=session))
    register(SucceedsButWritesNothingSource())
    first_request = _request(
        tmp_path, source_ids=("elevation", "stub"), keep_work=True, force=True
    )
    first = run_survey(first_request)
    assert first.complete is False
    stale = first.paths.root / f"{first.paths.stem}.tif"
    assert stale.is_file(), "expected the first run to have produced a COP30 DEM"
    assert stale.read_bytes().endswith(b"COP30-COPERNICUS-DATA")

    clear_registry()
    register(ElevationSource(api_key="test-key", session=session))
    register(SucceedsButWritesNothingSource())
    second = run_survey(
        _request(
            tmp_path,
            source_ids=("elevation", "stub"),
            elevation_demtype="EU_DTM",
            keep_work=True,
            force=True,
        )
    )

    assert second.paths.root == first.paths.root
    assert second.complete is False, "the DEM download failed, so this run is short"
    entry = next(s for s in second.survey["sources"] if s["id"] == "elevation")
    assert entry["demtype"] == "EU_DTM"
    # The whole finding in one line: the record names EU_DTM, so a COP30
    # TIFF must not be sitting beside it under the licence and citation
    # of a dataset it is not.
    assert not stale.exists(), (
        "the previous model's DEM is still in the package while survey.json "
        f"records {entry['demtype']} and its terms"
    )
    assert not any(second.paths.root.glob("*.tif"))


def test_a_run_whose_dem_lands_still_gets_its_tif(tmp_path):
    # The other side of the removal above: a successful run must not have
    # its own DEM swept out from under it, and a second successful run
    # over the same package replaces rather than deletes.
    _register_fake_elevation()
    result = run_survey(_request(tmp_path, source_ids=("elevation",)))
    assert (result.paths.root / f"{result.paths.stem}.tif").is_file()


def test_the_estimate_names_the_model_this_request_will_use(tmp_path):
    _register_fake_elevation()

    estimate = estimate_survey(
        _request(tmp_path, source_ids=("elevation",), elevation_demtype="SRTMGL1")
    )

    summary = next(s for s in estimate["sources"] if s["id"] == "elevation")
    assert summary["display_name"] == "Elevation (OpenTopography SRTMGL1)"
    # While the registered instance, which is what the layer checklist
    # reads, still claims no model at all.
    assert get_source("elevation").display_name == "Elevation (OpenTopography)"


# --- Task 29: `mapgen bridge <package-dir>`, the inverse of --skip-bridge.
# Two live situations, neither of them exotic. The owner has no Urbano
# install, so the bridge has failed on every run they have ever made and
# every package they own is missing its _project_setting.json. And review
# finding I1's deliberate residual: a Stop landing after the last tile
# leaves complete: true with bridge.attempted: false, in a folder that is
# never reused, so the Urbano files were previously unreachable without
# downloading the whole extent again. ------------------------------------


def _package_on_disk(
    tmp_path,
    *,
    name="2026-08-01_Barry-Waterfront",
    stem="Barry-Waterfront_2026-08-01",
    source_ids=("osm",),
    files=None,
    bridge=None,
    complete=True,
    stopped=False,
    bbox=BBOX,
    tiles=None,
):
    """A finished package folder, written directly rather than surveyed.

    Used for the refusals and for the survey.json shapes, which are about
    packages a run either could not produce or produced long ago. The
    happy paths below go through run_survey instead, so nothing here is
    the only evidence that this works on a real package.

    files defaults to exactly the merged output each named source writes,
    so a test that wants one missing removes it by name rather than by
    knowing the whole set.
    """
    root = tmp_path / "South-Wales" / name
    root.mkdir(parents=True, exist_ok=True)
    default_files = {"osm": f"{stem}.osm", "elevation": f"{stem}.tif"}
    if files is None:
        files = [default_files[s] for s in source_ids if s in default_files]
    for filename in files:
        (root / filename).write_text("merged", encoding="utf-8")
    payload = {
        "schema_version": 1,
        "tool_version": "0.0.0-test",
        "site": "Barry Waterfront",
        "region": "South Wales",
        "slug": {"site": "Barry-Waterfront", "region": "South-Wales"},
        "date": "2026-08-01",
        "urbano_stem": stem,
        "bbox": bbox.to_dict(),
        "extent_km": {"width": 0.7, "height": 1.11},
        "tiling": {"tile_size_m": 600.0, "overlap_m": 50.0, "rows": 2, "cols": 2},
        "sources": [{"id": s, "licence": "CC0", "attribution": "nobody"} for s in source_ids],
        "categories": ["buildings"],
        "tiles": (
            [{"tile_id": "r00_c00", **{s: "ok" for s in source_ids}}]
            if tiles is None
            else tiles
        ),
        "complete": complete,
        "stopped": stopped,
        "bridge": (
            {"attempted": True, "ok": False, "error": "Urbano is not installed"}
            if bridge is None
            else bridge
        ),
        "started_at": "2026-08-01T09:00:00Z",
        "finished_at": "2026-08-01T09:05:00Z",
    }
    (root / "survey.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return root


def _bridge_block(root):
    return json.loads((root / "survey.json").read_text(encoding="utf-8"))["bridge"]


def test_bridge_package_refuses_a_folder_that_is_not_there(tmp_path):
    register_default_sources()
    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(tmp_path / "nothing-here")
    message = str(excinfo.value)
    assert "no folder" in message
    assert "nothing-here" in message
    # One plain line, the way every other refusal in cli.py's tuple is.
    assert "\n" not in message


def test_bridge_package_refuses_a_package_with_no_survey_json_and_says_to_resume(tmp_path):
    register_default_sources()
    root = tmp_path / "half-done"
    root.mkdir()
    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(root)
    message = str(excinfo.value)
    assert "survey.json" in message
    # The brief's requirement, and the only useful thing to say: an
    # unfinished package needs the survey running again, not this.
    assert "resume" in message
    assert "\n" not in message


def test_bridge_package_refuses_a_survey_json_it_cannot_read(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path)
    (root / "survey.json").write_text("{not json at all", encoding="utf-8")
    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(root)
    # Told apart from the missing case deliberately: a resume does not fix
    # a damaged record of a package that may be entirely intact.
    assert "could not be read" in str(excinfo.value)
    assert "resume" not in str(excinfo.value)


def test_bridge_package_names_the_input_missing_from_the_package_root(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("osm",), files=[])
    runner = FakeBridgeRunner(returncode=0)
    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(root, bridge_runner=runner)
    message = str(excinfo.value)
    assert "Barry-Waterfront_2026-08-01.osm" in message
    assert "osm layer" in message
    # Refused BEFORE anything is started, not after a bridge has run and
    # produced a project setting missing a layer the record names.
    assert runner.calls == []
    assert _bridge_block(root) == {
        "attempted": True,
        "ok": False,
        "error": "Urbano is not installed",
    }, "a refusal must not rewrite the package's own record of its download"


def test_bridge_package_names_every_missing_input_not_only_the_first(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("osm", "elevation"), files=[])
    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(root)
    message = str(excinfo.value)
    assert "Barry-Waterfront_2026-08-01.osm" in message
    assert "Barry-Waterfront_2026-08-01.tif" in message


def test_bridge_package_refuses_a_record_with_no_bbox(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path)
    payload = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    del payload["bbox"]
    (root / "survey.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UnbridgeablePackageError, match="bbox"):
        bridge_package(root)


def test_bridge_package_refuses_a_record_with_no_urbano_stem(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path)
    payload = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    del payload["urbano_stem"]
    (root / "survey.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UnbridgeablePackageError, match="urbano_stem"):
        bridge_package(root)


def test_bridge_package_refuses_a_layer_that_does_not_declare_its_outputs(tmp_path):
    # possible_outputs is an optional LayerSource extension, so this is
    # reachable, and it must refuse rather than quietly hand the bridge no
    # OSM file at all: that would be the exact "test passes for the wrong
    # reason" failure, a bridge that ran and produced nothing useful.
    class NoDeclaredOutputsSource(StubSource):
        pass

    source = NoDeclaredOutputsSource(source_id="osm")
    register(source)
    root = _package_on_disk(tmp_path, source_ids=("osm",))
    with pytest.raises(UnbridgeablePackageError, match="does not declare"):
        bridge_package(root)


def test_bridge_package_hands_the_bridge_the_package_and_its_merged_osm_file(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("osm",))
    runner = FakeBridgeRunner(returncode=0)

    bridge_package(root, bridge_runner=runner)

    command = runner.calls[0]
    assert command[command.index("--output-folder") + 1] == str(root)
    assert command[command.index("--file-name-stem") + 1] == "Barry-Waterfront_2026-08-01"
    assert command[command.index("--osm-file-path") + 1] == str(
        root / "Barry-Waterfront_2026-08-01.osm"
    )
    # The extent comes from the record, never from the folder name, which
    # carries a site and a date and no coordinates at all.
    assert command[command.index("--bbox") + 1] == BBOX.to_query_string()


def test_bridge_package_hands_over_the_elevation_tiff_when_the_package_holds_one(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("osm", "elevation"))
    runner = FakeBridgeRunner(returncode=0)

    bridge_package(root, bridge_runner=runner)

    command = runner.calls[0]
    assert command[command.index("--elevation-tiff-path") + 1] == str(
        root / "Barry-Waterfront_2026-08-01.tif"
    )
    assert "--skip-elevation" not in command


def test_bridge_package_skips_elevation_for_a_package_that_holds_none(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("osm",))
    runner = FakeBridgeRunner(returncode=0)

    bridge_package(root, bridge_runner=runner)

    command = runner.calls[0]
    assert "--skip-elevation" in command
    assert "--elevation-tiff-path" not in command


def test_bridge_package_bridges_a_package_that_holds_no_osm_layer(tmp_path):
    # An overture-only package is a real selection, and the bridge fetches
    # its own buildings geoparquet, so there is nothing to refuse here.
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("overture",))
    runner = FakeBridgeRunner(returncode=0)

    payload = bridge_package(root, bridge_runner=runner)

    assert "--osm-file-path" not in runner.calls[0]
    assert payload["bridge"]["ok"] is True


def test_bridge_package_records_a_success_and_when_it_happened(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path)

    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=0))

    block = payload["bridge"]
    assert block["attempted"] is True
    assert block["ok"] is True
    assert block["error"] is None
    # A timestamp is what tells a reader this did not happen during the
    # download. Its shape is the one every other timestamp in this file
    # uses, so nothing has to learn a second format.
    assert block["ran_at"].endswith("Z")
    assert len(block["ran_at"]) == len("2026-08-01T09:00:00Z")
    assert _bridge_block(root) == block, "the record on disk must say the same thing"


def test_bridge_package_records_a_failure_in_plain_language_and_never_raises(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path)

    # Task 20's ruling applies here too: a bridge that ran and failed is
    # recorded, never propagated over the package that already exists.
    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=1))

    block = payload["bridge"]
    assert block["attempted"] is True
    assert block["ok"] is False
    assert "exit code 1" in block["error"]
    assert "Traceback" not in block["error"]


def test_bridge_package_records_the_owner_s_real_missing_urbano_message(tmp_path):
    # The case the owner actually hits, on every single run: the bridge
    # process starts, loads nothing, and exits non-zero with UrbanoBridge's
    # own DirectoryNotFoundException and C# stack trace on stderr. What
    # reaches survey.json must be the one plain sentence, not the trace.
    from tests.test_bridge import REAL_MISSING_URBANO_OUTPUT

    register_default_sources()
    root = _package_on_disk(tmp_path)

    payload = bridge_package(
        root,
        bridge_runner=FakeBridgeRunner(returncode=1, stderr=REAL_MISSING_URBANO_OUTPUT),
    )

    error = payload["bridge"]["error"]
    assert "Urbano.SiteAnalysis.gha" in error
    assert "Urbano is not installed" not in error, (
        "task 34 established that it is, and the message used to say otherwise"
    )
    assert "DirectoryNotFoundException" not in error
    assert "Program.cs" not in error
    # And the package still gets the file it is actually for, from mapgen,
    # on the very run whose bridge failed this way.
    assert payload["project_setting"]["written"] is True


def test_a_later_success_keeps_the_download_s_own_failed_attempt(tmp_path):
    # The brief's headline case, and the one the owner reaches the day
    # they install Urbano: a package whose bridge failed at download time,
    # bridged successfully weeks later. survey.json must not end up
    # claiming the bridge succeeded during a run where it did not.
    register_default_sources()
    root = _package_on_disk(
        tmp_path,
        bridge={
            "attempted": True,
            "ok": False,
            "error": "Urbano is not installed: no Urbano.Core.dll ...",
        },
    )

    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=0))

    block = payload["bridge"]
    assert block["ok"] is True, "the package has its Urbano files now"
    assert block["during_download"] == {
        "attempted": True,
        "ok": False,
        "error": "Urbano is not installed: no Urbano.Core.dll ...",
    }, "and the download's own failure is still on the record, untouched"
    assert block["ran_at"] != payload["finished_at"]


def test_a_second_bridge_run_still_reports_the_download_not_the_previous_one(tmp_path):
    # during_download is the ORIGINAL, not the previous. Two runs of this
    # command must not walk the record forward one attempt at a time until
    # the download's own result has been shifted out of it entirely.
    register_default_sources()
    root = _package_on_disk(
        tmp_path,
        bridge={"attempted": False, "ok": None, "error": None},
    )

    bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=1))
    first_attempt = _bridge_block(root)
    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=0))

    assert first_attempt["ok"] is False
    assert payload["bridge"]["ok"] is True
    assert payload["bridge"]["during_download"] == {
        "attempted": False,
        "ok": None,
        "error": None,
    }


def test_a_package_whose_record_has_no_bridge_block_gets_a_null_during_download(tmp_path):
    """README promises a null here rather than a fabricated false, and this
    is the shape that produces one: a package old enough that its
    survey.json never carried a bridge block at all, which is exactly what
    is sitting on the owner's disk.

    There is nothing for during_download to keep, and `false` would claim a
    download-time attempt that nothing witnessed. The distinction is the
    whole reason the field is nullable.
    """
    register_default_sources()
    root = _package_on_disk(tmp_path)
    payload = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    del payload["bridge"]
    (root / "survey.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    updated = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=0))

    assert updated["bridge"]["during_download"] is None
    assert updated["bridge"]["attempted"] is True
    assert updated["bridge"]["ok"] is True
    assert updated["bridge"]["ran_at"]
    # And it survives the trip to disk, which is where a reader meets it.
    assert _bridge_block(root)["during_download"] is None


def test_a_bridge_run_changes_nothing_in_survey_json_but_its_urbano_blocks(tmp_path):
    # survey.json is the package's record of ITSELF, and this command was
    # not there for the download. complete and stopped describe that
    # download and nothing else; so do tiles, sources and both timestamps.
    #
    # Three blocks are the exception rather than one, and each was added by
    # the task that made this command produce something: `bridge`;
    # `project_setting` (task 35), which mapgen now writes whether or not the
    # bridge is working; and `elevation_grid` (task 39), the DEM converted
    # into the format Urbano reads terrain from, which every package
    # downloaded before that task is missing. Everything else is still read
    # and never written.
    register_default_sources()
    root = _package_on_disk(tmp_path, complete=False, stopped=True)
    before = json.loads((root / "survey.json").read_text(encoding="utf-8"))

    bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=0))

    after = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    assert after["complete"] is False
    assert after["stopped"] is True
    for changed in ("bridge", "project_setting", "elevation_grid"):
        before.pop(changed, None)
        after.pop(changed, None)
    assert after == before
    assert list(after) == list(before), "field order is part of a file a person reads"


def test_bridge_package_emits_the_same_progress_events_a_survey_does(tmp_path):
    register_default_sources()
    root = _package_on_disk(tmp_path)
    log = EventLog()

    bridge_package(root, progress=log, bridge_runner=FakeBridgeRunner(returncode=1))

    # The project setting is written after the bridge attempt and regardless
    # of it, which is exactly what run_survey does, so the two commands go
    # on emitting the same vocabulary in the same order (task 35).
    assert [e["event"] for e in log.events] == [
        "bridge_started",
        "bridge_failed",
        "project_setting_started",
        "project_setting_written",
    ]
    assert "exit code 1" in log.events[1]["error"]


def test_a_stop_that_landed_at_the_very_end_can_have_its_urbano_files_afterwards(tmp_path):
    """Review finding I1's residual, end to end and through run_survey.

    A stop noticed only after every tile has genuinely finished leaves
    complete: true, stopped: false and bridge.attempted: false, and
    naming._survey_reports_complete means that folder is never opened by a
    survey again. Before this command that package could never get its
    Urbano files. Task 22's ruling that a stop starts no further external
    process is untouched: the bridge still does not run during that survey.
    """
    token = CancelToken()
    register(LegacyNoCancelStubSource(token))
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        cancel=token,
        bridge_runner=FakeBridgeRunner(returncode=0),
    )
    assert result.complete is True
    assert result.stopped is False
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["bridge"]["attempted"] is False, "the stop skipped the bridge, as ruled"

    after = bridge_package(result.paths.root, bridge_runner=FakeBridgeRunner(returncode=0))

    assert after["bridge"]["ok"] is True
    assert after["bridge"]["during_download"] == {
        "attempted": False,
        "ok": None,
        "error": None,
    }
    assert after["complete"] is True
    assert after["stopped"] is False


def test_bridge_package_and_run_survey_hand_the_bridge_the_same_command(tmp_path):
    """The brief's "one path, not two" made checkable rather than asserted.

    Same package, same files, same stem: if the two callers ever start
    composing different BridgeRequests, this is what says so, and it fails
    on the argument that differs rather than on a count.
    """
    source = OsmSource(
        session=_FakeOsmSession([_FakeOsmResponse() for _ in range(4)]),
        sleeper=lambda _seconds: None,
        min_interval_seconds=0.0,
    )
    register(source)
    during_download = FakeBridgeRunner(returncode=0)
    result = run_survey(
        _request(tmp_path, source_ids=("osm",), run_bridge_step=True),
        bridge_runner=during_download,
    )
    assert result.complete is True

    afterwards = FakeBridgeRunner(returncode=0)
    bridge_package(result.paths.root, bridge_runner=afterwards)

    assert afterwards.calls[0] == during_download.calls[0]


def test_bridge_package_works_on_a_real_surveyed_package_with_an_osm_layer(tmp_path):
    # The same package the previous test compares commands over, checked
    # from the other end: the file the bridge is pointed at is the merged
    # OSM output actually sitting in the folder, not a name composed here.
    source = OsmSource(
        session=_FakeOsmSession([_FakeOsmResponse() for _ in range(4)]),
        sleeper=lambda _seconds: None,
        min_interval_seconds=0.0,
    )
    register(source)
    result = run_survey(
        _request(tmp_path, source_ids=("osm",), run_bridge_step=False)
    )
    runner = FakeBridgeRunner(returncode=0)

    payload = bridge_package(result.paths.root, bridge_runner=runner)

    merged = result.paths.root / f"{result.paths.stem}.osm"
    assert merged.is_file()
    assert runner.calls[0][runner.calls[0].index("--osm-file-path") + 1] == str(merged)
    assert payload["bridge"]["ok"] is True
    assert payload["bridge"]["during_download"] == {
        "attempted": False,
        "ok": None,
        "error": None,
    }


# --- Task 30: verify the run, retry what failed, and say why --------------
#
# The owner's ruling, in their words: "if a tile has no data then it has no
# data, we should not add something random. we should be doing a verify at
# the end of the run to confirm all tiles are there, then retry certain
# tiles that failed, if nothing then it should say."
#
# These go through the REAL OsmSource against a fake HTTP session, not
# through a stub source, because almost everything being asserted here is
# a collaboration: the source classifies the failure, package.py decides
# whether that kind is worth retrying, the source's own inner backoff runs
# again underneath the retry, and the verify pass reads the disk both of
# them wrote to. A stub in the middle of that would be asserting the test's
# own idea of the contract rather than the contract.

_EMPTY_OSM_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<osm version="0.6" generator="test">\n'
    "</osm>\n"
)


def _osm_xml_for(tile_id):
    """A one-node document stamped with the tile it came from, so a merged
    output can be checked for the presence of a SPECIFIC tile's data rather
    than merely for being non-empty."""
    node_id = abs(hash(tile_id)) % 10_000_000
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<osm version="0.6" generator="test">\n'
        f'  <node id="{node_id}" version="1" lat="51.38" lon="-3.29"/>\n'
        "</osm>\n"
    )


class _FlakyOsmSession:
    """A fake OSM map API that refuses NAMED tiles a fixed number of times
    and then serves them.

    The count is the whole point of it. A double that fails forever
    exercises the give-up path and nothing else, and one that succeeds
    immediately exercises neither; the brief for this task named that trap
    directly. This one is told how many refusals each tile gets before it
    starts working, so the same class covers "recovered by the retry"
    (four, one whole inner budget) and "still failing after the retry"
    (any number no budget reaches), and each test says which it means in
    its own arguments.

    It answers by TILE, which it recovers by looking the request's bbox up
    in the plan build_tiles produces for the request under test. Nothing
    here counts calls or knows anything about retrying: it only knows what
    it has been asked for and how many times.
    """

    def __init__(self, failures_by_tile=(), status_code=503, text="unavailable",
                 empty_tiles=(), bbox=BBOX, tile_size_m=600.0, overlap_m=50.0):
        self.failures_left = dict(failures_by_tile)
        self.status_code = status_code
        self.text = text
        self.empty_tiles = set(empty_tiles)
        self.requests_by_tile: dict[str, int] = {}
        self._by_query = {
            tile.query_bbox.to_query_string(): tile.tile_id
            for tile in build_tiles(bbox, tile_size_m, overlap_m)
        }

    def get(self, url, **kwargs):
        tile_id = self._by_query[kwargs["params"]["bbox"]]
        self.requests_by_tile[tile_id] = self.requests_by_tile.get(tile_id, 0) + 1
        if self.failures_left.get(tile_id, 0) > 0:
            self.failures_left[tile_id] -= 1
            return _FakeOsmResponse(status_code=self.status_code, text=self.text)
        if tile_id in self.empty_tiles:
            return _FakeOsmResponse(text=_EMPTY_OSM_XML)
        return _FakeOsmResponse(text=_osm_xml_for(tile_id))

    def post(self, url, **kwargs):
        raise AssertionError("expected the map API (GET) path")


class _CancellingSink:
    """An EventLog that presses Stop the first time it sees a named event.

    The only way to land a stop at an exact point inside a run from
    outside it. Used to interrupt the retry pass specifically.
    """

    def __init__(self, token, stop_on):
        self.token = token
        self.stop_on = stop_on
        self.events: list[dict] = []

    def emit(self, event, **fields):
        self.events.append({"event": event, **fields})
        if event == self.stop_on:
            self.token.cancel()


def _register_flaky_osm(session):
    register(OsmSource(session=session, sleeper=lambda _s: None, min_interval_seconds=0.0))


def _events_named(log, name):
    return [event for event in log.events if event["event"] == name]


def test_an_empty_but_successful_tile_is_ok_never_retried_and_not_a_problem(tmp_path):
    # The distinction everything else in this task depends on. Sea,
    # moorland and empty farmland legitimately return nothing, and if
    # "empty" and "failed" ever collapse into one state the retry loop
    # hammers empty countryside forever while the map paints correct
    # results red.
    session = _FlakyOsmSession(empty_tiles=("r00_c01",))
    _register_flaky_osm(session)
    log = EventLog()

    result = run_survey(_osm_only_request(tmp_path), progress=log)

    assert result.complete is True
    records = {r["tile_id"]: r["osm"] for r in result.survey["tiles"]}
    assert records["r00_c01"] == "ok"
    assert result.survey["tile_failures"] == []
    assert session.requests_by_tile["r00_c01"] == 1, "an empty tile was asked for twice"
    assert _events_named(log, "tile_retrying") == []
    assert _events_named(log, "tile_failed") == []
    # And the rest of the extent is unaffected: this is one thin tile in a
    # package that is otherwise ordinary.
    assert (result.paths.root / f"{result.paths.stem}.osm").is_file()


def test_a_tile_that_fails_its_whole_inner_budget_is_recovered_by_the_retry(tmp_path):
    # Four refusals is exactly OsmSource's own max_retries, so this tile
    # exhausts the inner backoff and comes back as a failure, and then
    # succeeds on the first request of the retry pass. Five requests in
    # total for it, which is also the arithmetic that says the two layers
    # are not multiplying each other.
    session = _FlakyOsmSession(failures_by_tile={"r00_c01": 4})
    _register_flaky_osm(session)
    log = EventLog()

    result = run_survey(_osm_only_request(tmp_path), progress=log)

    assert result.complete is True
    assert all(record["osm"] == "ok" for record in result.survey["tiles"])
    assert result.survey["tile_failures"] == [], (
        "a tile that was recovered is still being reported as a problem"
    )
    assert session.requests_by_tile["r00_c01"] == 5
    retried = _events_named(log, "tile_retrying")
    assert [event["tile_id"] for event in retried] == ["r00_c01"]
    assert retried[0]["kind"] == "service_error"
    assert "503" in retried[0]["reason"]
    # The recovered tile's data really is in the package, not merely its
    # status in the record.
    merged = (result.paths.root / f"{result.paths.stem}.osm").read_text(encoding="utf-8")
    assert str(abs(hash("r00_c01")) % 10_000_000) in merged


def test_a_permanent_failure_survives_the_budget_and_is_reported_everywhere(tmp_path):
    # Two tiles that never recover, on a forced run. 4 attempts inside the
    # first fetch plus 4 inside the retry pass is 8 requests each, and no
    # more: the budget is one extra PASS, not one extra attempt and not an
    # unbounded loop.
    session = _FlakyOsmSession(failures_by_tile={"r00_c01": 99, "r01_c00": 99})
    _register_flaky_osm(session)
    log = EventLog()

    result = run_survey(_osm_only_request(tmp_path, force=True), progress=log)

    assert result.complete is False
    assert session.requests_by_tile["r00_c01"] == 8
    assert session.requests_by_tile["r01_c00"] == 8

    # 1. survey.json, so the package explains itself later.
    failures = result.survey["tile_failures"]
    assert sorted(record["tile_id"] for record in failures) == ["r00_c01", "r01_c00"]
    for record in failures:
        assert record["source"] == "osm"
        assert record["kind"] == "service_error"
        assert "503" in record["reason"]
        assert record["retried"] == 1
    records = {r["tile_id"]: r["osm"] for r in result.survey["tiles"]}
    assert records["r00_c01"] == "failed"
    assert records["r00_c00"] == "ok"

    # 2. the progress log, so the owner sees it while watching.
    failed_events = _events_named(log, "tile_failed")
    assert {event["tile_id"] for event in failed_events} == {"r00_c01", "r01_c00"}
    assert all("503" in event["reason"] for event in failed_events)
    assert all(event["kind"] == "service_error" for event in failed_events)
    # Twice per tile: once when the first pass resolved it, and again
    # after the retry pass came back with the same answer. The second one
    # says so, which is what tells a watcher that this is settled rather
    # than merely current.
    assert [event["retried"] for event in failed_events] == [0, 0, 1, 1]
    verified = _events_named(log, "verify_done")
    assert [event["phase"] for event in verified] == ["after_fetch", "after_retry"]
    assert verified[-1]["failed"] == 2
    assert verified[-1]["ok"] == 2

    # 3. the command line summary: composed from these same records, and
    # asserted through the real command in test_cli.py.
    lines = describe_tile_failures(failures)
    assert lines[0] == "2 tiles did not download:"
    assert any("r00_c01" in line and "503" in line for line in lines)

    # And every recoverable tile is still in the package, which is what
    # collecting instead of aborting bought.
    merged = (result.paths.root / f"{result.paths.stem}.osm").read_text(encoding="utf-8")
    assert str(abs(hash("r00_c00")) % 10_000_000) in merged


def test_without_force_a_permanent_failure_still_raises_and_still_writes_the_record(tmp_path):
    session = _FlakyOsmSession(failures_by_tile={"r01_c01": 99})
    _register_flaky_osm(session)

    with pytest.raises(IncompleteSurveyError) as excinfo:
        run_survey(_osm_only_request(tmp_path))

    assert "r01_c01" in str(excinfo.value)
    assert "503" in str(excinfo.value)
    assert "--force" in str(excinfo.value)

    root = tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    payload = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    assert payload["complete"] is False
    assert payload["stopped"] is False
    assert [r["tile_id"] for r in payload["tile_failures"]] == ["r01_c01"]
    # Unchanged from before this task: no merged output for a layer that
    # is short on an unforced run, and the work directory is kept so the
    # next run resumes rather than restarts.
    assert not (root / "Barry-Waterfront_2026-08-01.osm").exists()
    assert (root / "_work").is_dir()


def test_a_forced_run_and_an_unforced_one_differ_only_in_raising(tmp_path):
    # The same failure, twice, once each way. Both leave the same package
    # state for the tiles that did land; only one of them raises.
    session = _FlakyOsmSession(failures_by_tile={"r01_c01": 99})
    _register_flaky_osm(session)
    with pytest.raises(IncompleteSurveyError):
        run_survey(_osm_only_request(tmp_path))

    clear_registry()
    second = _FlakyOsmSession(failures_by_tile={"r01_c01": 99})
    _register_flaky_osm(second)
    result = run_survey(_osm_only_request(tmp_path, force=True))

    assert result.complete is False
    records = {r["tile_id"]: r["osm"] for r in result.survey["tiles"]}
    assert records["r01_c01"] == "failed"
    assert records["r00_c00"] == "ok"
    assert (result.paths.root / f"{result.paths.stem}.osm").is_file()
    # The second run resumed: the three tiles the first run got are not
    # asked for again, so only the failing one is.
    assert set(second.requests_by_tile) == {"r01_c01"}


def test_a_tile_over_the_node_cap_subdivides_and_is_never_retried(tmp_path):
    # Task 26's mechanism is separate from this task's and must stay
    # separate. A tile that is still too dense after being split as far as
    # splitting goes is a real failure with a real reason, but retrying it
    # would ask an identical question and get an identical refusal, and
    # would quietly turn a bounded subdivision into an unbounded re-ask.
    session = _DenseTileOsmSession(max_span=0.0)
    register(OsmSource(session=session, sleeper=lambda _s: None, min_interval_seconds=0.0))
    log = EventLog()

    result = run_survey(_osm_only_request(tmp_path, force=True), progress=log)

    assert result.complete is False
    assert _events_named(log, "tile_retrying") == [], "a node cap failure was retried"
    # Three requests per tile and no more: the tile itself, its first
    # quarter, and that quarter's first sixteenth, which is at the depth
    # cap and raises rather than splitting again. Nothing asks a second
    # time. A retry pass would show up here as another multiple of four
    # before anything else in this test noticed.
    assert len(session.bboxes) == 3 * 4
    failures = result.survey["tile_failures"]
    assert len(failures) == 4
    assert {record["kind"] for record in failures} == {"node_cap"}
    assert all("smaller extent" in record["reason"] for record in failures)
    assert _events_named(log, "tile_subdivided") != []


def test_a_stop_never_starts_the_retry_pass_at_all(tmp_path):
    """Stop must not start a retry, on a control this project's brief calls
    load-bearing, and until now that was asserted nowhere.

    The distinction from the test below is where the stop lands. There it
    arrives once the retry is already going and the interruption is what is
    being watched; here it arrives during the FIRST pass, with retryable
    failures already on the books, and the whole second pass must not
    happen. The event log is what says so: not merely no request made, but
    no retry announced and no retry reported as done.

    retry_done is the assertion that bites. _retry_failed_tiles has its own
    cancellation guard, so removing the outer gate still stops the retry at
    the first source it looks at; what it leaves behind is a retry_done
    saying a pass ran and recovered nothing, on a run that was told not to
    start one.
    """
    session = _FlakyOsmSession(failures_by_tile={"r00_c00": 99})
    _register_flaky_osm(session)
    token = CancelToken()
    sink = _CancellingSink(token, stop_on="tile_failed")

    result = run_survey(_osm_only_request(tmp_path), progress=sink, cancel=token)

    assert result.stopped is True
    assert result.complete is False
    # A retryable failure really is on the books, so this is not passing by
    # having nothing to retry in the first place.
    assert {r["kind"] for r in result.survey["tile_failures"]} == {"service_error"}
    assert _events_named(sink, "tile_retrying") == []
    assert _events_named(sink, "retry_done") == []
    assert result.survey["retries"] == []
    assert session.requests_by_tile["r00_c00"] == 4, "the failed tile was asked again"


def test_a_stop_interrupts_the_retry_pass_as_promptly_as_the_first_attempt(tmp_path):
    session = _FlakyOsmSession(failures_by_tile={"r01_c01": 99})
    _register_flaky_osm(session)
    token = CancelToken()
    sink = _CancellingSink(token, stop_on="tile_retrying")

    result = run_survey(_osm_only_request(tmp_path), progress=sink, cancel=token)

    assert result.stopped is True
    assert result.complete is False
    # The retry was announced and then never made a request: 4 attempts
    # from the first pass and nothing from the second.
    assert session.requests_by_tile["r01_c01"] == 4
    records = {r["tile_id"]: r["osm"] for r in result.survey["tiles"]}
    # Failed, NOT pending. Task 22's rule is that a tile a stop never
    # REACHED is pending; this one was reached, a whole pass ago, and it
    # failed. Recording it pending because an optional second attempt did
    # not happen would erase a failure the run genuinely observed.
    assert records["r01_c01"] == "failed"
    assert records["r00_c00"] == "ok"
    # A stop is not an error, so this returned rather than raising even
    # without --force, and what landed was merged rather than discarded.
    assert (result.paths.root / f"{result.paths.stem}.osm").is_file()


def test_the_verify_pass_raises_a_record_to_ok_when_the_file_is_really_there(tmp_path):
    """The first of the verify pass's three documented rules, and the one no
    test executed.

    It reads as redundant, because _record_tile_outcomes reaches the same
    answer first on every path this code can produce today: the retry pass
    calls it with the retried tiles, and a hard-kill resume is marked ok by
    the same call when the source skips a tile whose file is present.

    It is not redundant, and that is the whole design of this pass. It is a
    SECOND opinion, walked over every planned tile from a standing start,
    believing only the filesystem, which is worth nothing if it can only
    ever agree downwards. Remove this rule and the pass can lower a record
    and never raise one, so a state.json that lost a tile to a hard kill
    stays wrong and the run redownloads ground it already has.

    Directly on the function, because the point is precisely that the layers
    above it get there first.
    """
    from mapgen.jobs import FAILED, OK
    from mapgen.package import _verify_tiles

    tiles = build_tiles(BBOX, 600.0, 50.0)
    tile_ids = [tile.tile_id for tile in tiles]
    source_work = tmp_path / "raw" / "stub"
    source_work.mkdir(parents=True)
    for tile_id in tile_ids:
        (source_work / f"{tile_id}.txt").write_text(tile_id, encoding="utf-8")

    state = JobState(tmp_path / "state.json", tile_ids, ["stub"])
    for tile_id in tile_ids:
        state.mark(tile_id, "stub", OK)
    # One tile the record gave up on, with its file sitting on disk all
    # along. The file is the thing that decides.
    state.mark(tile_ids[0], "stub", FAILED)

    verified = _verify_tiles(
        state,
        [(StubSource(), source_work)],
        tiles,
        tile_ids,
        _FailureLedger(),
        EventLog(),
        "after_fetch",
    )

    assert state.status(tile_ids[0], "stub") == OK
    assert verified["failures"] == []
    assert verified["ok"] == len(tile_ids)
    assert [
        (record["tile_id"], record["was"], record["now"])
        for record in verified["corrections"]
    ] == [(tile_ids[0], FAILED, OK)]


def test_the_verify_pass_corrects_a_recorded_ok_with_no_file_behind_it(tmp_path):
    # The belt to _record_tile_outcomes' braces, on the one thing that
    # function cannot catch: it only ever looks at the tiles the fetch it
    # follows was handed, and a tile recorded ok by an EARLIER run is
    # never in that set. survey.json is the package's record of itself and
    # the owner is expected to trust it, so a record claiming a file that
    # is not there has to be found and corrected.
    source = StubSource(fail_on=("r01_c01",))
    register(source)
    first = run_survey(_request(tmp_path, force=True))
    assert first.complete is False

    vanished = first.paths.work_dir / "raw" / "stub" / "r00_c00.txt"
    assert vanished.is_file()
    vanished.unlink()

    source._fail_on.clear()
    log = EventLog()
    second = run_survey(_request(tmp_path, force=True), progress=log)

    records = {r["tile_id"]: r["stub"] for r in second.survey["tiles"]}
    assert records["r00_c00"] == "failed", (
        "survey.json still claims a tile whose file is not on disk"
    )
    assert records["r01_c01"] == "ok"
    assert second.complete is False
    corrections = second.survey["verified"]["corrections"]
    assert corrections == [
        {"source": "stub", "tile_id": "r00_c00", "was": "ok", "now": "failed"}
    ]
    reasons = {r["tile_id"]: r["reason"] for r in second.survey["tile_failures"]}
    assert "no file for it is on disk" in reasons["r00_c00"]
    assert any(
        event["tile_id"] == "r00_c00" for event in _events_named(log, "tile_failed")
    )


def test_the_verify_pass_counts_every_planned_tile_of_every_fetched_source(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path))
    assert result.survey["verified"] == {
        "checked": 4,
        "ok": 4,
        "failed": 0,
        "pending": 0,
        "corrections": [],
    }


def test_a_source_that_merged_nothing_writes_no_file_and_the_record_says_why(tmp_path):
    # merge.py used to write the XML envelope unconditionally, so a run
    # that merged nothing produced an 85-byte <osm></osm> in the package
    # root, reported complete, and read in Grasshopper as a layer that is
    # present and empty. The absence of a file now has to be explained by
    # the record instead.
    session = _FlakyOsmSession(empty_tiles=("r00_c00", "r00_c01", "r01_c00", "r01_c01"))
    _register_flaky_osm(session)

    result = run_survey(_osm_only_request(tmp_path))

    assert result.complete is True
    assert all(record["osm"] == "ok" for record in result.survey["tiles"])
    assert not (result.paths.root / f"{result.paths.stem}.osm").exists()
    entry = next(s for s in result.survey["sources"] if s["id"] == "osm")
    assert entry["merged_files"] == []
    assert entry["features_merged"] == 0
    assert result.survey["tile_failures"] == [], (
        "an empty extent is not a failure and must not be reported as one"
    )


def test_merged_nothing_not_selected_and_failed_are_three_different_readings(tmp_path):
    # A reader of survey.json has to be able to tell them apart, and this
    # is the run that puts all three in one file: osm answered and found
    # nothing, elevation failed outright, overture was never asked for.
    class _FailingElevation:
        id = "elevation"
        display_name = "Stub Elevation"
        licence = "CC0"
        attribution = "nobody"
        requires_api_key = True

        def estimate(self, bbox, tiles):
            return Estimate(bytes_estimate=0, seconds_estimate=0.0)

        def fetch(self, bbox, tiles, work_dir, progress):
            raise RuntimeError("Failed to download DEM: HTTP 401")

        def merge(self, parts, out_dir, stem):
            return []

    session = _FlakyOsmSession(empty_tiles=("r00_c00", "r00_c01", "r01_c00", "r01_c01"))
    _register_flaky_osm(session)
    register(_FailingElevation())

    result = run_survey(
        _request(tmp_path, source_ids=("osm", "elevation"), force=True)
    )

    entries = {entry["id"]: entry for entry in result.survey["sources"]}
    assert set(entries) == {"osm", "elevation"}, "a source nobody asked for is recorded"

    # Merged nothing: no file, a zero count, and no failure against its name.
    assert entries["osm"]["merged_files"] == []
    assert entries["osm"]["features_merged"] == 0
    assert not any(r["source"] == "osm" for r in result.survey["tile_failures"])

    # Failed: no file either, but every tile recorded failed and every one
    # of them carrying the layer's own reason.
    assert entries["elevation"]["merged_files"] == []
    elevation_failures = [
        r for r in result.survey["tile_failures"] if r["source"] == "elevation"
    ]
    assert len(elevation_failures) == 4
    assert all("HTTP 401" in r["reason"] for r in elevation_failures)
    assert all(r["elevation"] == "failed" for r in result.survey["tiles"])


def test_an_authentication_failure_is_never_retried(tmp_path):
    # The one keyed source's most likely failure, and the one a retry can
    # only ever make worse. Asserted through the OSM path because it is
    # the one that classifies statuses; the rule itself is package.py's.
    session = _FlakyOsmSession(
        failures_by_tile={"r00_c00": 99}, status_code=401, text="no key"
    )
    _register_flaky_osm(session)
    log = EventLog()

    result = run_survey(_osm_only_request(tmp_path, force=True), progress=log)

    assert session.requests_by_tile["r00_c00"] == 4, "an unauthorised tile was retried"
    assert _events_named(log, "tile_retrying") == []
    failure = result.survey["tile_failures"][0]
    assert failure["kind"] == "not_authorised"
    assert failure["retried"] == 0


def test_a_whole_layer_failing_without_force_still_raises_its_own_error(tmp_path):
    # Unchanged behaviour, pinned because this task moved the OTHER kind
    # of failure's raise to the end of the run. A source that fails as a
    # whole and cannot say which tiles has nothing to retry and nothing
    # finer to report, so it still ends the run immediately, with its own
    # exception rather than IncompleteSurveyError.
    register(StubSource(fail_on=("r00_c00",)))
    with pytest.raises(RuntimeError, match="stub failure on r00_c00"):
        run_survey(_request(tmp_path))


# --- Task 30 section 6: bridging a package whose elevation layer failed ---
#
# Task 29's implementer flagged that `mapgen bridge` is stricter than
# run_survey in exactly one case the owner will certainly hit, and
# proposed the narrowing themselves. Elevation is the only keyed source,
# so it fails whenever the key is missing, wrong, rate limited or the
# service is down, and refusing there leaves the owner with a package they
# can never produce Urbano files for without downloading it all again.


def _all_tiles_failed_for(source_ids, elevation="failed"):
    return [
        {
            "tile_id": tile_id,
            **{s: ("ok" if s != "elevation" else elevation) for s in source_ids},
        }
        for tile_id in ("r00_c00", "r00_c01")
    ]


def test_bridge_package_proceeds_when_every_elevation_tile_is_recorded_failed(tmp_path):
    register_default_sources()
    root = _package_on_disk(
        tmp_path,
        source_ids=("osm", "elevation"),
        files=["Barry-Waterfront_2026-08-01.osm"],
        complete=False,
        tiles=_all_tiles_failed_for(("osm", "elevation")),
    )
    runner = FakeBridgeRunner(returncode=0)

    bridge_package(root, bridge_runner=runner)

    command = runner.calls[0]
    assert "--skip-elevation" in command
    assert "--elevation-tiff-path" not in command
    # The rest of the package is bridged exactly as it would have been.
    assert command[command.index("--osm-file-path") + 1] == str(
        root / "Barry-Waterfront_2026-08-01.osm"
    )
    assert _bridge_block(root)["ok"] is True


def test_bridge_package_still_refuses_when_only_some_elevation_tiles_failed(tmp_path):
    # A DEM that half arrived and then vanished from the root is not
    # explained by the record. This is the case the narrowing is narrow
    # for: the file's absence has to be positively stated, not merely
    # consistent with something.
    register_default_sources()
    mixed = _all_tiles_failed_for(("osm", "elevation"))
    mixed[0]["elevation"] = "ok"
    root = _package_on_disk(
        tmp_path,
        source_ids=("osm", "elevation"),
        files=["Barry-Waterfront_2026-08-01.osm"],
        complete=False,
        tiles=mixed,
    )
    runner = FakeBridgeRunner(returncode=0)

    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(root, bridge_runner=runner)

    assert "elevation" in str(excinfo.value)
    assert runner.calls == [], "a dotnet process was started at a package it refused"


def test_bridge_package_still_refuses_when_elevation_tiles_are_merely_pending(tmp_path):
    # Pending is "never attempted", which a stop produces in quantity. It
    # is not the package saying the layer was tried and did not arrive.
    register_default_sources()
    root = _package_on_disk(
        tmp_path,
        source_ids=("osm", "elevation"),
        files=["Barry-Waterfront_2026-08-01.osm"],
        complete=False,
        stopped=True,
        tiles=_all_tiles_failed_for(("osm", "elevation"), elevation="pending"),
    )
    runner = FakeBridgeRunner(returncode=0)

    with pytest.raises(UnbridgeablePackageError):
        bridge_package(root, bridge_runner=runner)
    assert runner.calls == []


def test_bridge_package_still_refuses_a_record_with_no_tile_rows_at_all(tmp_path):
    # "No evidence against" is not "positively stated". An empty tiles
    # list says nothing, and a hand-edited or truncated record must not
    # be read as permission.
    register_default_sources()
    root = _package_on_disk(
        tmp_path,
        source_ids=("osm", "elevation"),
        files=["Barry-Waterfront_2026-08-01.osm"],
        tiles=[],
    )
    runner = FakeBridgeRunner(returncode=0)

    with pytest.raises(UnbridgeablePackageError):
        bridge_package(root, bridge_runner=runner)
    assert runner.calls == []


def test_bridge_package_never_extends_the_same_leniency_to_a_missing_osm_file(tmp_path):
    # A missing <stem>.osm has no benign reading, and nothing about a
    # failed OSM layer makes the Urbano geometry worth producing without
    # it. Same record shape as the elevation case, opposite answer.
    register_default_sources()
    root = _package_on_disk(
        tmp_path,
        source_ids=("osm",),
        files=[],
        complete=False,
        tiles=[{"tile_id": "r00_c00", "osm": "failed"}],
    )
    runner = FakeBridgeRunner(returncode=0)

    with pytest.raises(UnbridgeablePackageError) as excinfo:
        bridge_package(root, bridge_runner=runner)
    assert "osm" in str(excinfo.value)
    assert runner.calls == []


def test_a_real_survey_whose_elevation_failed_can_be_bridged_afterwards(tmp_path):
    # End to end on a package this project actually produces, rather than
    # one written by hand: a forced run whose DEM 401s, then the command
    # over the folder it left. This is the owner's real sequence.
    class _FailingElevation:
        id = "elevation"
        display_name = "Stub Elevation"
        licence = "CC0"
        attribution = "nobody"
        requires_api_key = True

        def estimate(self, bbox, tiles):
            return Estimate(bytes_estimate=0, seconds_estimate=0.0)

        def fetch(self, bbox, tiles, work_dir, progress):
            raise RuntimeError("Failed to download DEM: HTTP 401")

        def merge(self, parts, out_dir, stem):
            return []

        def possible_outputs(self, stem):
            return [f"{stem}.tif"]

    session = _FlakyOsmSession()
    _register_flaky_osm(session)
    register(_FailingElevation())

    result = run_survey(
        _request(tmp_path, source_ids=("osm", "elevation"), force=True)
    )
    assert result.complete is False
    assert not (result.paths.root / f"{result.paths.stem}.tif").exists()

    runner = FakeBridgeRunner(returncode=0)
    payload = bridge_package(result.paths.root, bridge_runner=runner)

    assert "--skip-elevation" in runner.calls[0]
    assert payload["bridge"]["ok"] is True
    # The download's own record is untouched by the later command.
    assert payload["complete"] is False
    assert all(record["elevation"] == "failed" for record in payload["tiles"])


# --- Task 32: retry every layer, not just OSM ------------------------------
#
# Task 30 built the verify pass, the failure classification and the retry,
# and wired the retry to OSM alone: elevation and Overture failed as whole
# layers with no per-tile cause, and a failure with no kind is never
# retried. The owner asked for the rest, and named the constraint
# themselves: "only if they specifically fail".
#
# These go through the REAL OvertureSource and the REAL ElevationSource
# against fake transports, never through a stub layer, for the reason the
# Task 30 block above gives: almost everything asserted here is a
# collaboration between the source's classification and package.py's
# policy, and a stub in the middle would assert the test's own idea of the
# contract rather than the contract.
#
# Every double below fails a SPECIFIC number of times and then works. A
# double that fails forever exercises the give-up path and nothing else;
# one that succeeds immediately exercises neither.


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FlakyOvertureRunner(_FakeOvertureRunner):
    """An overturemaps that refuses NAMED types a fixed number of times and
    then serves them, in the shape 0.20.0 actually fails in: exit code
    zero, no output file, and its own account of the failure printed to
    its own stdout.

    Counted per type, because that is the unit Task 23 and Task 24 made
    Overture fail in and the unit Task 32 retries.
    """

    def __init__(self, failures_by_type=(), returncode=0):
        super().__init__()
        self.failures_left = dict(failures_by_type)
        self.returncode = returncode

    def __call__(self, command, **kwargs):
        overture_type = command[command.index("--type") + 1]
        with self.lock:
            remaining = self.failures_left.get(overture_type, 0)
            failing = remaining > 0
            if failing:
                self.failures_left[overture_type] = remaining - 1
                self.commands.append(command)
        if failing:
            return _FakeCompleted(
                self.returncode,
                stdout=(
                    f"Error reading data from path "
                    f"s3://overturemaps/{overture_type}: timed out"
                ),
            )
        return super().__call__(command, **kwargs)

    def types_requested(self):
        return [command[command.index("--type") + 1] for command in self.commands]


def _overture_request(tmp_path, types=("water", "building"), **overrides):
    defaults = dict(
        source_ids=("overture",),
        overture_types=types,
        tile_size_m=600.0,
        overlap_m=50.0,
        run_bridge_step=False,
    )
    defaults.update(overrides)
    return _request(tmp_path, **defaults)


def _register_overture(runner):
    register(OvertureSource(runner=runner, executable_finder=lambda _n: "overturemaps"))


def test_an_overture_type_recovered_by_a_retry_leaves_its_siblings_untouched(tmp_path):
    # The case the owner will actually hit: several concurrent reads of a
    # cloud store on a home link, and one of them dies part way through.
    runner = _FlakyOvertureRunner(failures_by_type={"water": 1})
    _register_overture(runner)
    log = EventLog()

    result = run_survey(_overture_request(tmp_path), progress=log)

    assert result.complete is True
    assert result.survey["tile_failures"] == []
    # Three downloads for two types: building once, water twice. The retry
    # did not restart the batch, and nothing that landed was fetched again.
    assert sorted(runner.types_requested()) == ["building", "water", "water"]
    retried = _events_named(log, "tile_retrying")
    assert {event["source"] for event in retried} == {"overture"}
    # And the package really holds both layers.
    assert (result.paths.root / f"{result.paths.stem}_water.geojson").is_file()
    assert (result.paths.root / f"{result.paths.stem}_building.geojson").is_file()


def test_an_overture_type_that_fails_permanently_is_asked_exactly_twice(tmp_path):
    # The end-to-end attempt count for this source, which must not
    # silently grow. Overture has no per-request retry of its own, so one
    # first attempt plus one retry pass is two, and no more.
    runner = _FlakyOvertureRunner(failures_by_type={"water": 99})
    _register_overture(runner)
    log = EventLog()

    result = run_survey(_overture_request(tmp_path, force=True), progress=log)

    assert result.complete is False
    assert runner.types_requested().count("water") == 2, (
        "the retry budget is one extra pass, not an unbounded loop"
    )
    assert runner.types_requested().count("building") == 1

    failures = result.survey["tile_failures"]
    assert {record["source"] for record in failures} == {"overture"}
    assert all(record["retried"] == 1 for record in failures)
    assert all("water" in record["reason"] for record in failures)
    # The type that landed is still merged and still in the package: a
    # failure never discards work that was paid for.
    assert (result.paths.root / f"{result.paths.stem}_building.geojson").is_file()
    assert not (result.paths.root / f"{result.paths.stem}_water.geojson").exists()


def test_an_overture_type_the_cli_refused_is_never_retried(tmp_path):
    # A non-zero exit is treated conservatively, so the layer is asked
    # once and reported. The count is the assertion: a retry would show up
    # as a second command before anything else here noticed.
    runner = _FlakyOvertureRunner(failures_by_type={"water": 99}, returncode=2)
    _register_overture(runner)
    log = EventLog()

    result = run_survey(_overture_request(tmp_path, force=True), progress=log)

    assert result.complete is False
    assert runner.types_requested().count("water") == 1
    assert _events_named(log, "tile_retrying") == []
    assert {r["kind"] for r in result.survey["tile_failures"]} == {"refused"}
    assert all(r["retried"] == 0 for r in result.survey["tile_failures"])


# --- elevation ------------------------------------------------------------


class _FakeElevationResponse:
    def __init__(self, status_code, payload, headers):
        self.status_code = status_code
        self.headers = dict(headers)
        self._payload = payload

    def iter_content(self, chunk_size=None):
        return iter([self._payload] if self._payload else [])

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class _FlakyElevationSession:
    """An OpenTopography that answers a fixed number of times with a named
    failure and then serves the DEM.

    Counts its own calls, which is the arithmetic every attempt-count
    assertion below rests on.
    """

    TIFF = b"II*\x00" + b"\x00" * 128

    def __init__(self, failures=0, status_code=503, headers=None, exception=None):
        self.failures_left = failures
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.exception = exception
        self.calls = 0

    def get(self, url, **kwargs):
        self.calls += 1
        if self.failures_left > 0:
            self.failures_left -= 1
            if self.exception is not None:
                raise self.exception
            return _FakeElevationResponse(self.status_code, b"", self.headers)
        return _FakeElevationResponse(200, self.TIFF, {})


class _RecordingWaitToken(CancelToken):
    """A token whose wait() records what it was asked for and returns at
    once, so a test can assert the PERIOD without spending it."""

    def __init__(self):
        super().__init__()
        self.waits = []

    def wait(self, seconds):
        self.waits.append(seconds)
        return False


def _elevation_request(tmp_path, **overrides):
    defaults = dict(
        source_ids=("elevation",),
        tile_size_m=600.0,
        overlap_m=50.0,
        run_bridge_step=False,
    )
    defaults.update(overrides)
    return _request(tmp_path, **defaults)


def _register_elevation(session, api_key="test-key"):
    register(ElevationSource(api_key=api_key, session=session))


def test_an_elevation_timeout_is_retried_and_the_dem_arrives(tmp_path):
    session = _FlakyElevationSession(
        failures=1, exception=requests.exceptions.ReadTimeout("slow")
    )
    _register_elevation(session)
    log = EventLog()

    result = run_survey(_elevation_request(tmp_path), progress=log)

    assert result.complete is True
    assert result.survey["tile_failures"] == []
    assert session.calls == 2, "one first attempt plus one retry, no more"
    retried = _events_named(log, "tile_retrying")
    assert {event["source"] for event in retried} == {"elevation"}
    assert all(event["kind"] == "timeout" for event in retried)
    assert (result.paths.root / f"{result.paths.stem}.tif").is_file()


def test_an_elevation_401_is_never_retried_and_the_error_is_not_delayed(tmp_path):
    # The failure the owner meets most often on the one keyed source, and
    # the one a retry can only make worse: it fails identically every
    # time, it spends their own quota to find that out, and it delays the
    # honest error.
    session = _FlakyElevationSession(failures=99, status_code=401)
    _register_elevation(session)
    log = EventLog()

    result = run_survey(_elevation_request(tmp_path, force=True), progress=log)

    assert result.complete is False
    assert session.calls == 1, "a rejected key was asked again"
    assert _events_named(log, "tile_retrying") == []
    failures = result.survey["tile_failures"]
    assert {record["kind"] for record in failures} == {"not_authorised"}
    assert all(record["retried"] == 0 for record in failures)
    assert result.survey["retries"] == []


def test_an_elevation_5xx_that_never_clears_is_asked_exactly_twice(tmp_path):
    session = _FlakyElevationSession(failures=99, status_code=503)
    _register_elevation(session)

    result = run_survey(_elevation_request(tmp_path, force=True), progress=EventLog())

    assert result.complete is False
    assert session.calls == 2, "the budget is one extra pass, not an unbounded loop"
    assert {r["kind"] for r in result.survey["tile_failures"]} == {"service_error"}
    assert all(r["retried"] == 1 for r in result.survey["tile_failures"])


def test_a_rate_limit_waits_the_period_the_service_asked_for(tmp_path):
    session = _FlakyElevationSession(
        failures=1, status_code=429, headers={"Retry-After": "7"}
    )
    _register_elevation(session)
    log = EventLog()
    token = _RecordingWaitToken()

    result = run_survey(_elevation_request(tmp_path), progress=log, cancel=token)

    assert result.complete is True
    assert token.waits == [7.0], f"the service asked for 7 seconds and got {token.waits}"
    waiting = _events_named(log, "retry_waiting")
    assert [event["seconds"] for event in waiting] == [7.0]
    assert session.calls == 2


def test_a_retry_after_longer_than_the_ceiling_is_not_retried_at_all(tmp_path):
    # An exhausted daily quota can answer with an hour. A survey the owner
    # is watching must not silently stop for an hour inside a step that
    # presents itself as automatic, and coming back early would spend
    # their quota to be told the same thing again.
    session = _FlakyElevationSession(
        failures=99, status_code=429, headers={"Retry-After": "3600"}
    )
    _register_elevation(session)
    log = EventLog()
    token = _RecordingWaitToken()

    result = run_survey(
        _elevation_request(tmp_path, force=True), progress=log, cancel=token
    )

    assert result.complete is False
    assert token.waits == [], "the run paused for longer than its own ceiling"
    assert session.calls == 1, "the retry happened anyway"
    postponed = _events_named(log, "retry_postponed")
    assert len(postponed) == 1
    assert postponed[0]["source"] == "elevation"
    assert postponed[0]["seconds_requested"] == 3600.0
    assert postponed[0]["seconds_ceiling"] == MAX_RETRY_AFTER_WAIT_SECONDS
    assert _events_named(log, "tile_retrying") == []


def test_a_stop_during_a_retry_after_wait_lands_at_once(tmp_path):
    # Cancellation must interrupt a retry as promptly as it interrupts a
    # first attempt, and a wait built on time.sleep would have made a Stop
    # press do nothing at all for up to a minute.
    session = _FlakyElevationSession(
        failures=99, status_code=429, headers={"Retry-After": "30"}
    )
    _register_elevation(session)
    log = EventLog()

    class _StoppingToken(CancelToken):
        def wait(self, seconds):
            # What the real CancelToken.wait does when a stop lands during
            # the wait: it returns True rather than sleeping out the rest.
            self.cancel()
            return True

    result = run_survey(
        _elevation_request(tmp_path), progress=log, cancel=_StoppingToken()
    )

    assert result.stopped is True
    assert session.calls == 1, "the retry was made after the stop landed"
    assert _events_named(log, "tile_retrying") == []


def test_the_real_cancel_token_wakes_from_a_wait_the_instant_a_stop_lands():
    # The token above is a double, so the real thing is pinned separately
    # rather than assumed. A ten second wait, cancelled from another
    # thread, must return in a small fraction of it.
    token = CancelToken()
    threading.Timer(0.05, token.cancel).start()
    started = time.monotonic()
    interrupted = token.wait(10.0)
    elapsed = time.monotonic() - started

    assert interrupted is True
    assert elapsed < 2.0, f"the wait ignored the stop for {elapsed:.1f}s"


def test_a_wait_of_zero_returns_at_once_and_reports_the_standing_answer():
    token = CancelToken()
    assert token.wait(0) is False
    token.cancel()
    assert token.wait(0) is True


def test_the_ledger_waits_the_longest_period_any_of_the_tiles_was_given():
    # Asserted at the unit rather than through a run, and deliberately so.
    # No source today gives two of its tiles DIFFERENT periods: elevation
    # records one whole-extent answer against all of them, Overture
    # records none, and OSM leaves the field alone because its own inner
    # loop has already served the wait. So a run cannot currently tell
    # max from min, and the rule would sit unpinned until the first
    # source that can, which is exactly when getting it wrong would
    # matter. The rule itself is not arbitrary: these tiles are about to
    # be fetched in ONE call, so one wait covers all of them, and coming
    # back before the latest deadline the service set ignores it for the
    # tiles it applied to.
    ledger = _FailureLedger()
    ledger.add_tile_failures(
        [
            TileFailure(
                source="elevation", tile_id="r00_c00", kind="rate_limited",
                reason="slow down", retry_after_seconds=5.0,
            ),
            TileFailure(
                source="elevation", tile_id="r00_c01", kind="rate_limited",
                reason="slow down", retry_after_seconds=45.0,
            ),
            TileFailure(
                source="elevation", tile_id="r01_c00", kind="rate_limited",
                reason="slow down", retry_after_seconds=12.0,
            ),
        ]
    )

    assert ledger.retry_after_for("elevation", ["r00_c00", "r00_c01", "r01_c00"]) == 45.0
    assert ledger.retry_after_for("elevation", ["r00_c00", "r01_c00"]) == 12.0
    # A source nobody recorded a period for waits for nothing at all,
    # rather than for zero seconds dressed up as an instruction.
    assert ledger.retry_after_for("osm", ["r00_c00"]) is None


def test_a_later_failure_with_no_period_clears_an_earlier_one(tmp_path):
    # The retry pass re-records a tile's failure after asking again. A
    # service that asked for thirty seconds the first time and said
    # nothing the second has withdrawn the instruction, and carrying the
    # stale one forward would pause a run on the strength of a header
    # that is no longer being sent.
    ledger = _FailureLedger()
    ledger.add_tile_failures(
        [
            TileFailure(
                source="elevation", tile_id="r00_c00", kind="rate_limited",
                reason="slow down", retry_after_seconds=30.0,
            )
        ]
    )
    assert ledger.retry_after_for("elevation", ["r00_c00"]) == 30.0

    ledger.add_tile_failures(
        [
            TileFailure(
                source="elevation", tile_id="r00_c00", kind="service_error",
                reason="answered HTTP 503",
            )
        ]
    )
    assert ledger.retry_after_for("elevation", ["r00_c00"]) is None


def test_a_stop_interrupts_an_overture_retry_before_it_downloads(tmp_path):
    runner = _FlakyOvertureRunner(failures_by_type={"water": 99})
    _register_overture(runner)
    token = CancelToken()
    sink = _CancellingSink(token, stop_on="tile_retrying")

    result = run_survey(_overture_request(tmp_path), progress=sink, cancel=token)

    assert result.stopped is True
    assert result.complete is False
    assert runner.types_requested().count("water") == 1, (
        "the retry downloaded after the stop had landed"
    )
    # Failed, not pending: this layer was reached and it did fail. Task
    # 22's rule is about a tile a stop never reached.
    records = {r["tile_id"]: r["overture"] for r in result.survey["tiles"]}
    assert set(records.values()) == {"failed"}


# --- the record of what was retried ---------------------------------------


def test_a_run_that_only_completed_because_of_a_retry_says_so(tmp_path):
    # The whole of section 4. tile_failures is empty for a recovered tile,
    # deliberately and correctly, so without this a fragile run and a
    # clean one are indistinguishable afterwards.
    session = _FlakyElevationSession(failures=1, status_code=503)
    _register_elevation(session)
    log = EventLog()

    result = run_survey(_elevation_request(tmp_path), progress=log)

    assert result.complete is True
    assert result.survey["tile_failures"] == []

    retries = result.survey["retries"]
    assert len(retries) == 4, "one record per planned tile of the retried layer"
    for record in retries:
        assert record["source"] == "elevation"
        assert record["pass_number"] == 1
        assert record["kind"] == "service_error"
        assert record["recovered"] is True
        assert "503" in record["reason"]

    done = _events_named(log, "retry_done")
    assert len(done) == 1
    assert done[0] == {
        "event": "retry_done",
        "pass_number": 1,
        "of": 1,
        "attempted": 4,
        "recovered": 4,
        "still_failing": 0,
    }


def test_a_whole_layer_asked_again_is_one_event_and_the_record_says_so(tmp_path):
    """Review I2. Elevation downloads the whole extent in one request, so a
    failure of its is recorded against every planned tile and the retry
    asks for every planned tile back in a single call.

    Announced per tile, one DEM hiccup is four messages here and seventy
    two on the owner's own Barry extent, which is exactly the reading task
    32 already ruled wrong on the failure side of the same run.
    """
    session = _FlakyElevationSession(failures=1, status_code=503)
    _register_elevation(session)
    log = EventLog()

    result = run_survey(_elevation_request(tmp_path), progress=log)

    assert result.complete is True
    retrying = _events_named(log, "tile_retrying")
    assert len(retrying) == 1, "one request asked again is one thing happening"
    assert retrying[0]["source"] == "elevation"
    assert retrying[0]["tiles"] == 4
    # No tile id, the way retry_postponed carries none: stamping this with
    # one of the four squares would name a square for work spanning the
    # whole extent.
    assert "tile_id" not in retrying[0]
    # survey.json keeps every tile the one request covered, because only
    # the final verdict can say which of them arrived, and marks them as
    # the single event they are.
    retries = result.survey["retries"]
    assert len(retries) == 4
    assert all(record["whole_layer"] is True for record in retries)


def test_one_tile_asked_again_is_not_dressed_up_as_a_whole_layer(tmp_path):
    """The other half of the same rule, and the one that would hide which
    tiles were fragile if it were got wrong. One OSM tile of four coming
    back on a retry is one tile, and the record has to keep saying so.
    """
    session = _FlakyOsmSession(failures_by_tile={"r00_c01": 4})
    _register_flaky_osm(session)
    log = EventLog()

    result = run_survey(_osm_only_request(tmp_path), progress=log)

    assert result.complete is True
    retrying = _events_named(log, "tile_retrying")
    assert [event["tile_id"] for event in retrying] == ["r00_c01"]
    retries = result.survey["retries"]
    assert [record["tile_id"] for record in retries] == ["r00_c01"]
    assert all(record["whole_layer"] is False for record in retries)


def test_a_clean_run_records_no_retries_at_all(tmp_path):
    session = _FlakyElevationSession(failures=0)
    _register_elevation(session)
    log = EventLog()

    result = run_survey(_elevation_request(tmp_path), progress=log)

    assert result.complete is True
    assert result.survey["retries"] == []
    assert _events_named(log, "retry_done") == []
    assert _events_named(log, "tile_retrying") == []


def test_a_retry_that_did_not_work_is_recorded_as_not_recovered(tmp_path):
    session = _FlakyElevationSession(failures=99, status_code=503)
    _register_elevation(session)

    result = run_survey(_elevation_request(tmp_path, force=True), progress=EventLog())

    retries = result.survey["retries"]
    assert retries, "a retry that failed is still a retry and must be recorded"
    assert all(record["recovered"] is False for record in retries)


# --- the account the owner reads ------------------------------------------


def test_a_whole_layer_failure_is_one_line_not_one_line_per_tile():
    # An elevation or Overture failure is recorded against every planned
    # tile, because that is what a whole-extent download covers. On the
    # owner's own Barry extent that would be seventy-two copies of one
    # sentence for one thing that went wrong once.
    records = [
        {
            "source": "elevation",
            "tile_id": f"r00_c{n:02d}",
            "kind": "not_authorised",
            "reason": (
                "Failed to download DEM: OpenTopography refused the request "
                "as not allowed (HTTP 401)."
            ),
            "retried": 0,
        }
        for n in range(4)
    ]
    lines = describe_tile_failures(records, planned_tiles=4)

    assert lines[0] == "4 tiles did not download:"
    assert len(lines) == 2
    assert lines[1].startswith("  elevation, all 4 tiles:")
    assert "HTTP 401" in lines[1]


def test_a_partial_failure_is_still_named_tile_by_tile():
    # The rule is "all of them, identically", never "several of them". Two
    # OSM tiles of four is two tiles, and collapsing it would hide which.
    records = [
        {
            "source": "osm",
            "tile_id": "r00_c01",
            "kind": "service_error",
            "reason": "the OpenStreetMap map API answered HTTP 503, on all 4 attempts.",
            "retried": 1,
        },
        {
            "source": "osm",
            "tile_id": "r01_c00",
            "kind": "service_error",
            "reason": "the OpenStreetMap map API answered HTTP 503, on all 4 attempts.",
            "retried": 1,
        },
    ]
    lines = describe_tile_failures(records, planned_tiles=4)

    assert len(lines) == 3
    assert any("r00_c01" in line for line in lines)
    assert any("r01_c00" in line for line in lines)


def test_a_caller_that_does_not_know_the_plan_gets_the_output_it_always_got():
    records = [
        {
            "source": "elevation",
            "tile_id": f"r00_c{n:02d}",
            "kind": "unknown",
            "reason": "boom",
            "retried": 0,
        }
        for n in range(3)
    ]
    assert len(describe_tile_failures(records)) == 4


# --- one layer failing must not cost another layer that recovered ---------


def test_a_layer_that_recovered_is_merged_even_when_another_failed(tmp_path):
    # Reachable only since this task, because elevation and Overture now
    # defer their failures to the end of the run the way OSM already did.
    # Withholding a layer that is complete and paid for because a
    # DIFFERENT layer failed throws away work for nothing: the run is
    # about to raise and say what is missing in any case.
    osm_session = _FlakyOsmSession(failures_by_tile={"r00_c01": 4})
    _register_flaky_osm(osm_session)
    _register_elevation(_FlakyElevationSession(failures=99, status_code=401))

    request = _request(
        tmp_path,
        source_ids=("osm", "elevation"),
        tile_size_m=600.0,
        overlap_m=50.0,
        run_bridge_step=False,
    )
    with pytest.raises(IncompleteSurveyError) as excinfo:
        run_survey(request, progress=EventLog())

    root = tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    assert (root / "Barry-Waterfront_2026-08-01.osm").is_file(), (
        "an OSM layer whose retry recovered every tile was withheld because "
        "elevation failed"
    )
    assert not (root / "Barry-Waterfront_2026-08-01.tif").exists()
    payload = json.loads((root / "survey.json").read_text(encoding="utf-8"))
    assert all(record["osm"] == "ok" for record in payload["tiles"])
    assert all(record["elevation"] == "failed" for record in payload["tiles"])
    # And the OSM tile that needed a second attempt is recorded as having
    # needed one, even though the package holds it.
    assert {r["source"] for r in payload["retries"]} == {"osm"}
    assert all(r["recovered"] is True for r in payload["retries"])
    # One line for the whole elevation layer, four tiles' worth of it.
    assert "elevation, all 4 tiles" in str(excinfo.value)


def test_an_elevation_failure_no_longer_costs_osm_its_retry(tmp_path):
    # Task 30's own concern 2, closed by this task. Elevation used to
    # raise out of the source loop the moment it failed, so a run with
    # both an OSM tile failure and an elevation 401 ended on the elevation
    # error with the OSM retry never attempted.
    osm_session = _FlakyOsmSession(failures_by_tile={"r00_c01": 4})
    _register_flaky_osm(osm_session)
    _register_elevation(_FlakyElevationSession(failures=99, status_code=401))

    request = _request(
        tmp_path,
        source_ids=("elevation", "osm"),
        tile_size_m=600.0,
        overlap_m=50.0,
        run_bridge_step=False,
        force=True,
    )
    result = run_survey(request, progress=EventLog())

    assert osm_session.requests_by_tile["r00_c01"] == 5, (
        "the OSM retry never happened because elevation failed first"
    )
    records = {r["tile_id"]: r["osm"] for r in result.survey["tiles"]}
    assert all(status == "ok" for status in records.values())


# --- the key, through the whole run ---------------------------------------


def test_no_api_key_reaches_survey_json_or_the_event_stream_by_any_route(tmp_path):
    # The brief's own requirement, asserted where it actually matters: not
    # at the source's own boundary, which test_sources_elevation.py
    # already covers, but in the file the package keeps and in the event
    # stream the browser reads. Both outlive the run.
    secret = "sk-real-secret-should-never-leak"
    leaky_url = f"https://portal.opentopography.org/API/globaldem?API_Key={secret}"

    class _LeakingSession:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            raise requests.exceptions.ConnectionError(
                f"Max retries exceeded with url: {leaky_url}"
            )

    session = _LeakingSession()
    _register_elevation(session, api_key=secret)
    log = EventLog()

    result = run_survey(_elevation_request(tmp_path, force=True), progress=log)

    # Not vacuous: the run really did fail, was really retried, and really
    # did record it.
    assert session.calls == 2
    assert result.survey["tile_failures"]
    assert result.survey["retries"]

    written = (result.paths.root / "survey.json").read_text(encoding="utf-8")
    assert secret not in written
    assert "API_Key" not in written
    for event in log.events:
        assert secret not in json.dumps(event, default=str)


# ---------------------------------------------------------------------------
# Task 35: the Urbano project setting, written by mapgen on every run.
# ---------------------------------------------------------------------------


class UrbanoReadableStubSource(StubSource):
    """A stub whose merged output carries the names Urbano reads.

    StubSource writes `stub.txt`, which Urbano has no idea what to do with,
    so a plain stub run correctly produces no project setting at all. This
    one names its outputs the way OsmSource, ElevationSource and
    OvertureSource name theirs, so the wiring can be exercised end to end
    through run_survey without a network call or a real OSM parser.
    """

    def __init__(self, source_id="stub", suffixes=(".osm",), **kwargs):
        super().__init__(source_id=source_id, **kwargs)
        self._suffixes = suffixes

    def merge(self, parts, out_dir, stem):
        outputs = []
        for suffix in self._suffixes:
            out = out_dir / f"{stem}{suffix}"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
            )
            outputs.append(out)
        return outputs


def _project_setting(result):
    path = result.paths.root / f"{result.paths.stem}_project_setting.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_run_writes_the_urbano_project_setting_without_the_bridge(tmp_path):
    """The whole of task 35 in one test.

    Before it, this file was written in exactly one place, the C# bridge,
    which fails on every run on the owner's machine, so the file had never
    once been produced. A real survey went into Grasshopper with only
    survey.json in the folder, and Urbano answered that no data was
    collected.
    """
    register(UrbanoReadableStubSource())
    result = run_survey(_request(tmp_path, run_bridge_step=False))

    written = result.paths.root / f"{result.paths.stem}_project_setting.json"
    assert written.is_file()
    setting = json.loads(written.read_text(encoding="utf-8"))
    assert setting["FileNameStr"] == result.paths.stem
    assert setting["Folder"] == str(result.paths.root)
    assert setting["OsmFilePath"] == str(result.paths.root / f"{result.paths.stem}.osm")
    assert setting["Layers"] == ["osm"]
    assert setting["CoordinateReference"]["Utm"] == "30U"
    assert setting["Top"] == BBOX.north
    assert setting["Bottom"] == BBOX.south


def test_the_project_setting_is_written_even_when_the_bridge_fails(tmp_path):
    """The requirement in the owner's own words: a run where the bridge
    fails must still produce a usable project setting. That is every run
    they have ever made.
    """
    register(UrbanoReadableStubSource())
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        bridge_runner=FakeBridgeRunner(returncode=1),
    )

    assert result.survey["bridge"]["ok"] is False
    assert result.survey["project_setting"]["written"] is True
    assert _project_setting(result)["FileNameStr"] == result.paths.stem


def test_the_project_setting_is_written_when_the_bridge_step_is_skipped(tmp_path):
    register(UrbanoReadableStubSource())
    result = run_survey(_request(tmp_path, run_bridge_step=False))
    assert result.survey["bridge"]["attempted"] is False
    assert result.survey["project_setting"]["written"] is True


def test_mapgens_project_setting_is_the_one_that_survives_a_bridge_run(tmp_path):
    """Which writer wins, made checkable rather than asserted in a comment.

    Both write the same file name. mapgen's step runs after the bridge, so
    a bridge that wrote its own version is overwritten by one that names
    the files actually in the folder rather than the ones the bridge meant
    to produce.
    """
    register(UrbanoReadableStubSource())
    written_by_the_bridge = []

    class _BridgeThatWritesItsOwn(FakeBridgeRunner):
        def __call__(self, command, **kwargs):
            output_folder = Path(command[command.index("--output-folder") + 1])
            stem = command[command.index("--file-name-stem") + 1]
            target = output_folder / f"{stem}_project_setting.json"
            target.write_text(
                json.dumps({"FileNameStr": "written by the bridge"}), encoding="utf-8"
            )
            written_by_the_bridge.append(target)
            return super().__call__(command, **kwargs)

    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        bridge_runner=_BridgeThatWritesItsOwn(returncode=0),
    )

    assert written_by_the_bridge, "the fake bridge wrote nothing, so this proves nothing"
    setting = _project_setting(result)
    assert setting["FileNameStr"] == result.paths.stem
    assert "OsmFilePath" in setting


def test_survey_json_records_the_project_setting_beside_the_bridge(tmp_path):
    register(UrbanoReadableStubSource(suffixes=(".osm", "_building.geojson")))
    result = run_survey(_request(tmp_path, run_bridge_step=False))

    record = result.survey["project_setting"]
    assert record == {
        "written": True,
        "file": f"{result.paths.stem}_project_setting.json",
        "layers": ["osm", "overture"],
        "error": None,
    }
    # Beside the bridge block rather than inside it: whether the package has
    # the file Urbano is pointed at, and whether the bridge ran, are now two
    # different questions.
    assert "project_setting" not in result.survey["bridge"]


def test_a_package_urbano_cannot_read_says_so_and_still_finishes(tmp_path):
    """A stub run produces stub.txt, which is not one of Urbano's four data
    files, so there is nothing for a project setting to point at. Recorded
    as a plain sentence rather than written as a file describing nothing,
    and the survey itself is untouched by it.
    """
    register(StubSource())
    result = run_survey(_request(tmp_path, run_bridge_step=False))

    assert result.complete is True
    record = result.survey["project_setting"]
    assert record["written"] is False
    assert record["file"] is None
    assert "would name nothing" in record["error"]
    assert not list(result.paths.root.glob("*_project_setting.json"))


def test_a_project_setting_failure_never_costs_the_survey(tmp_path, monkeypatch):
    """The same ruling task 20 made for the bridge: real work already on
    disk is never discarded because one late step failed.
    """
    import mapgen.package as package_module

    register(UrbanoReadableStubSource())

    def _explode(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(package_module, "write_project_setting", _explode)
    log = EventLog()
    result = run_survey(_request(tmp_path, run_bridge_step=False), progress=log)

    assert result.complete is True
    assert (result.paths.root / f"{result.paths.stem}.osm").is_file()
    assert result.survey["project_setting"]["written"] is False
    assert "the disk is full" in result.survey["project_setting"]["error"]
    assert [e["event"] for e in log.events if e["event"].startswith("project_setting")] == [
        "project_setting_started",
        "project_setting_failed",
    ]


def test_the_project_setting_names_every_layer_the_package_actually_holds(tmp_path):
    """Every layer the package holds AND Urbano can open from that field.

    The `.tif` is on disk here and is deliberately absent from the setting:
    Urbano's elevation field is a protobuf `ElevationGrid`, not a raster
    path, so naming a GeoTIFF there crashes the components that read it.
    See tests/test_urbano.py::test_a_geotiff_is_never_named_in_the_elevation_field.
    """
    register(UrbanoReadableStubSource(suffixes=(".osm", ".tif", "_building.geojson")))
    result = run_survey(_request(tmp_path, run_bridge_step=False))

    setting = _project_setting(result)
    assert setting["Layers"] == ["osm", "overture"]
    assert setting["ElevationFilePath"] == ""
    assert (result.paths.root / f"{result.paths.stem}.tif").is_file()
    assert setting["OvertureFilePath"].endswith("_building.geojson")
    assert setting["BlockFilePath"] == ""
    for field in ("OsmFilePath", "OvertureFilePath"):
        assert Path(setting[field]).is_file(), f"{field} names a file that is not there"


def test_bridge_package_produces_the_project_setting_a_package_never_had(tmp_path):
    """Every package the owner already has is in exactly this state: real
    data, a survey.json recording a failed bridge, and no project setting.
    """
    register_default_sources()
    root = _package_on_disk(tmp_path, source_ids=("osm", "elevation"))
    assert not list(root.glob("*_project_setting.json"))

    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=1))

    assert payload["bridge"]["ok"] is False
    assert payload["project_setting"]["written"] is True
    setting = json.loads(
        (root / payload["project_setting"]["file"]).read_text(encoding="utf-8")
    )
    assert setting["FileNameStr"] == payload["urbano_stem"]
    # `elevation` is not among them although the package holds a `.tif`: that
    # field is a protobuf ElevationGrid and a GeoTIFF in it crashes Urbano.
    assert setting["Layers"] == ["osm"]
    assert Path(setting["OsmFilePath"]).is_file()


def test_bridge_package_reads_the_extent_from_the_package_not_the_folder_name(tmp_path):
    """The coordinate reference is the one thing a wrong answer here would
    silently misplace, so it comes from the package's own recorded bbox.
    """
    register_default_sources()
    far_away = BBox.parse("151.20,-33.88,151.22,-33.86")
    root = _package_on_disk(tmp_path, bbox=far_away)

    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=1))

    setting = json.loads(
        (root / payload["project_setting"]["file"]).read_text(encoding="utf-8")
    )
    assert setting["CoordinateReference"]["Utm"] == "56H"
    assert setting["Bottom"] == pytest.approx(-33.88)


def test_a_stop_at_the_very_end_still_leaves_a_project_setting(tmp_path):
    """Task 22's ruling is that a stop starts no further external process,
    which is why such a run has no bridge attempt and, before this, no
    project setting. A complete folder is never surveyed again (see
    naming._survey_reports_complete), so for that package the gap was
    permanent. Writing a local file is not starting a process, so it happens
    anyway, and `mapgen bridge` is no longer the only way to recover one.
    """
    token = CancelToken()

    class _ReadableLegacyStub(LegacyNoCancelStubSource, UrbanoReadableStubSource):
        """Stops the run the way a source written before task 22 does, and
        names its merged output the way the real sources do."""

        def __init__(self, cancel_token):
            UrbanoReadableStubSource.__init__(self)
            self._token = cancel_token

    register(_ReadableLegacyStub(token))
    result = run_survey(
        _request(tmp_path, run_bridge_step=True),
        cancel=token,
        bridge_runner=FakeBridgeRunner(returncode=0),
    )

    assert result.complete is True
    assert result.survey["bridge"]["attempted"] is False, "the stop skipped the bridge, as ruled"
    assert result.survey["project_setting"]["written"] is True
    assert _project_setting(result)["Layers"] == ["osm"]


# --------------------------------------------------------------------------
# The elevation grid (task 39): the DEM in the only format Urbano reads
# terrain in.
# --------------------------------------------------------------------------

# The bounds the real Barry DEM in tests/data was downloaded for. A grid built
# for any other extent covers none of it and is correctly refused, which is
# what makes the refusal test below a real one.
DEM_BBOX = BBox.parse("-3.276,51.393,-3.268,51.398")
REAL_DEM = Path(__file__).resolve().parent / "data" / "Barry-Full_2026-08-04.tif"


class RealDemStubSource(UrbanoReadableStubSource):
    """A stub that leaves a real OpenTopography DEM in the package.

    Copied byte for byte from tests/data rather than synthesised, so the
    conversion under test is the conversion the owner's own packages get.
    """

    def __init__(self, source_id="stub", dem=REAL_DEM, **kwargs):
        super().__init__(source_id=source_id, suffixes=(".osm",), **kwargs)
        self._dem = Path(dem)

    def merge(self, parts, out_dir, stem):
        outputs = super().merge(parts, out_dir, stem)
        target = out_dir / f"{stem}.tif"
        target.write_bytes(self._dem.read_bytes())
        return outputs + [target]


def test_a_run_with_a_dem_writes_the_egrid_beside_it(tmp_path):
    """The whole of task 39 in one test. Before it, a package's terrain was
    a GeoTIFF, which is a file no Urbano component can open: the field it
    would have to be named in goes straight to a protobuf deserialiser.
    """
    register(RealDemStubSource())
    result = run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False))

    written = result.paths.root / f"{result.paths.stem}.egrid"
    assert written.is_file()
    record = result.survey["elevation_grid"]
    assert record["written"] is True
    assert record["file"] == written.name
    assert record["nodes"] == 12432
    assert record["covered"] == 3763
    assert record["error"] is None


def test_the_egrid_is_what_the_project_setting_names_and_never_the_raster(tmp_path):
    """The ordering the two steps depend on. resolve_data_files reads the
    layer list off the disk, so the .egrid has to exist before the project
    setting is written or the setting says this package has no terrain.
    """
    register(RealDemStubSource())
    result = run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False))

    setting = _project_setting(result)
    assert "elevation" in setting["Layers"]
    assert setting["ElevationFilePath"] == str(
        result.paths.root / f"{result.paths.stem}.egrid"
    )
    # And never the raster, which is still in the folder and still named in
    # survey.json's sources.
    assert (result.paths.root / f"{result.paths.stem}.tif").is_file()
    assert not setting["ElevationFilePath"].endswith(".tif")


def test_a_dem_that_cannot_be_converted_costs_the_package_nothing(tmp_path):
    """A conversion failure is recorded and the run finishes. Losing a real
    survey's OSM and Overture data because a raster could not be read would
    be the mistake task 20 already fixed once for the bridge.
    """

    class BadDemStub(UrbanoReadableStubSource):
        def merge(self, parts, out_dir, stem):
            outputs = super().merge(parts, out_dir, stem)
            target = out_dir / f"{stem}.tif"
            target.write_bytes(b'{"type": "FeatureCollection"}')
            return outputs + [target]

    register(BadDemStub())
    result = run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False))

    assert result.complete is True
    record = result.survey["elevation_grid"]
    # Same five keys as a success and as a package with no DEM at all, so
    # a reader following README's schema can index any of them.
    assert set(record) == {"written", "file", "nodes", "covered", "error"}
    assert record["written"] is False
    assert record["covered"] is None
    assert "byte order mark" in record["error"]
    assert not (result.paths.root / f"{result.paths.stem}.egrid").exists()
    # The rest of the package is untouched, and the project setting says so
    # rather than claiming a layer it has not got.
    assert (result.paths.root / f"{result.paths.stem}.osm").is_file()
    assert result.survey["project_setting"]["written"] is True
    assert "elevation" not in _project_setting(result)["Layers"]


def test_a_run_with_no_dem_at_all_says_nothing_went_wrong(tmp_path):
    """`written` false with a null error is "this package has no DEM", which
    is a different statement from "the DEM could not be converted" and has to
    read differently in survey.json.

    All five keys, including `covered`, whatever happened. README describes
    the block as five keys, and a record whose KEYS move with the outcome is
    one only the reader who trips over it ever finds.
    """
    register(UrbanoReadableStubSource())
    result = run_survey(_request(tmp_path, run_bridge_step=False))

    record = result.survey["elevation_grid"]
    assert record == {
        "written": False, "file": None, "nodes": None, "covered": None,
        "error": None,
    }


def test_an_egrid_left_by_a_previous_attempt_goes_when_the_dem_does(tmp_path):
    """Staleness is this step's own job. The stale-output sweep only ever
    deletes names a source's possible_outputs declares, and the .egrid is
    deliberately not one of them (package.py's _bridge_input_file reads the
    same list to choose the file it hands the C# bridge as its TIFF path).
    So an .egrid describing a DEM the package no longer holds is removed
    here, on every run and on every `mapgen bridge`.
    """
    register(UrbanoReadableStubSource())
    result = run_survey(_request(tmp_path, run_bridge_step=False))
    stale = result.paths.root / f"{result.paths.stem}.egrid"
    stale.write_bytes(b"a previous attempt's grid")

    payload = bridge_package(
        result.paths.root, bridge_runner=FakeBridgeRunner(returncode=0)
    )

    assert not stale.exists()
    assert payload["elevation_grid"]["written"] is False
    assert payload["elevation_grid"]["error"] is None


def test_mapgen_bridge_converts_the_dem_of_a_package_already_on_disk(tmp_path):
    """The reason this matters: every package the owner already has was
    downloaded before mapgen could write a .egrid, so every one of them has
    a DEM Urbano cannot open. This fixes them in place, with no download.
    """
    register(RealDemStubSource())
    result = run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False))
    written = result.paths.root / f"{result.paths.stem}.egrid"
    written.unlink()

    payload = bridge_package(
        result.paths.root, bridge_runner=FakeBridgeRunner(returncode=0)
    )

    assert written.is_file()
    assert payload["elevation_grid"]["written"] is True
    assert payload["elevation_grid"]["nodes"] == 12432


def test_the_egrid_step_finds_the_dem_under_the_name_elevation_declares(tmp_path):
    """_write_elevation_grid_step reads `<stem>.tif` from a literal of its
    own, which is a third copy of a filename ElevationSource already states
    twice, in possible_outputs and in merge.

    _bridge_input_file's docstring gives the rule this file works to: what
    package.py must not start knowing separately is the sources' filenames,
    because two copies of that fact drift. Drift here would be silent. The
    DEM would be sitting in the package, the step would find nothing, and
    survey.json would report "this package has no DEM" with no error
    anywhere and no terrain in Grasshopper.

    So the literal is exercised through the source's own declaration rather
    than compared to it, and a rename on either side fails here.
    """
    from mapgen.package import _write_elevation_grid_step
    from mapgen.sources.elevation import ElevationSource

    stem = "Barry-Waterfront_2026-08-01"
    names = [str(name) for name in ElevationSource().possible_outputs(stem)]
    assert len(names) == 1, "elevation declares one output; this assumes which"
    (tmp_path / names[0]).write_bytes(REAL_DEM.read_bytes())

    record = _write_elevation_grid_step(
        bbox=DEM_BBOX, root=tmp_path, stem=stem, sink=EventLog()
    )

    assert record["written"] is True, (
        "package.py is looking for the DEM under a name ElevationSource does "
        "not write"
    )
    assert (tmp_path / f"{stem}.egrid").is_file()


def test_mapgen_bridge_names_the_egrid_it_just_wrote_in_the_project_setting(tmp_path):
    """The same ordering run_survey's copy is pinned for, on the one command
    whose entire purpose is retrofitting terrain onto a package that has none.

    resolve_data_files reads the layer list off the disk, so the .egrid has
    to be there before the setting is composed. Transposed, this command
    writes a perfectly good .egrid and then a setting saying the package has
    no terrain, and survey.json records the omission without flagging it:
    the owner points Urbano at the folder and gets no ground, with the file
    it wanted sitting beside the file it read.
    """
    register(RealDemStubSource())
    result = run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False))
    root, stem = result.paths.root, result.paths.stem
    # The shape of a package the owner already has: the DEM, and neither of
    # the two files this command exists to produce.
    (root / f"{stem}.egrid").unlink()
    (root / f"{stem}_project_setting.json").unlink()

    payload = bridge_package(root, bridge_runner=FakeBridgeRunner(returncode=0))

    setting = json.loads(
        (root / f"{stem}_project_setting.json").read_text(encoding="utf-8")
    )
    assert setting["ElevationFilePath"] == str(root / f"{stem}.egrid")
    assert "elevation" in setting["Layers"]
    # And survey.json agrees with the file, rather than recording a layer
    # the setting does not name.
    assert payload["project_setting"]["layers"] == setting["Layers"]
    assert payload["elevation_grid"]["written"] is True


def test_the_egrid_is_written_before_the_project_setting(tmp_path):
    """The ordering, pinned through the events rather than by reading the
    source, so reversing the two calls fails here.
    """
    register(RealDemStubSource())
    log = EventLog()
    run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False), progress=log)
    names = [event["event"] for event in log.snapshot()]
    assert names.index("elevation_grid_written") < names.index("project_setting_written")


def test_the_progress_events_name_the_file_and_how_much_of_it_has_data(tmp_path):
    """The browser and the terminal both read these. `covered` is well under
    `nodes` on a coastal survey, because the DEM is smaller than the padded
    extent Urbano's own grid geometry asks for, and that is worth being able
    to see without opening Grasshopper.
    """
    register(RealDemStubSource())
    log = EventLog()
    result = run_survey(
        _request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False), progress=log
    )
    written = [e for e in log.snapshot() if e["event"] == "elevation_grid_written"]
    assert len(written) == 1
    assert written[0]["file"] == f"{result.paths.stem}.egrid"
    assert written[0]["nodes"] == 12432
    assert written[0]["covered"] == 3763


def test_the_egrid_a_run_writes_is_the_grid_it_says_it_is(tmp_path):
    """A run's .egrid, decoded by the test suite's own protobuf reader, is
    the grid mapgen says it is. This is the end to end shape check; the
    comparison against Urbano's own serialiser is in tests/test_egrid.py.
    """
    from tests.test_egrid import decode

    register(RealDemStubSource())
    result = run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False))
    grid = decode((result.paths.root / f"{result.paths.stem}.egrid").read_bytes())
    assert (grid.nx, grid.ny) == (112, 111)
    assert len(grid.heights) == 12432
    assert 400_000 < grid.x0 < 500_000
    assert 5_600_000 < grid.y0 < 5_800_000


def test_a_failed_conversion_is_announced_and_not_only_written_down(tmp_path):
    """The browser reads the events, not survey.json, while a run is going.
    A failure recorded in the file but never emitted is one the owner does
    not see until they go looking, and the whole point of reporting it is
    that the folder otherwise looks complete.
    """

    class BadDemStub(UrbanoReadableStubSource):
        def merge(self, parts, out_dir, stem):
            outputs = super().merge(parts, out_dir, stem)
            (out_dir / f"{stem}.tif").write_bytes(b"not a tiff")
            return outputs + [out_dir / f"{stem}.tif"]

    register(BadDemStub())
    log = EventLog()
    run_survey(_request(tmp_path, bbox=DEM_BBOX, run_bridge_step=False), progress=log)

    failed = [e for e in log.snapshot() if e["event"] == "elevation_grid_failed"]
    assert len(failed) == 1
    assert "byte order mark" in failed[0]["error"]
    assert not any(e["event"] == "elevation_grid_written" for e in log.snapshot())
