import json
from datetime import date
from pathlib import Path

import pytest

from mapgen.geo import BBox
from mapgen.jobs import CancelToken, Cancelled, EventLog, JobState
from mapgen.naming import PathTooLongError, build_package_paths, tiling_fingerprint
from mapgen.package import (
    SurveyRequest,
    estimate_geometry,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import (
    DuplicateSourceError,
    Estimate,
    clear_registry,
    get_source,
    register,
)
from mapgen.sources.osm import OsmSource
from mapgen.sources.overture import DEFAULT_OVERTURE_TYPES, OvertureSource

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
    deep = Path("C:/") / ("x" * 140)

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


def test_cancellation_stops_the_job(tmp_path):
    register(StubSource())
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        run_survey(_request(tmp_path), cancel=token)


def test_coordinate_stem_option_uses_the_coordinate_form(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, coordinate_stem=True))
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["urbano_stem"] == "51.39_51.38_-3.28_-3.29"


class FakeCompletedProcess:
    def __init__(self, returncode=0):
        self.returncode = returncode


class FakeBridgeRunner:
    """Stands in for subprocess.run: records commands, returns a fixed exit
    code, never actually shells out to dotnet. run_survey resolves the real
    tools/UrbanoBridge/UrbanoBridge.csproj (it genuinely exists in this
    repo), so run_bridge reaches this runner exactly as it would reach the
    real dotnet executable in production.
    """

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return FakeCompletedProcess(self.returncode)


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


def test_register_default_sources_registers_the_three_phase_one_sources():
    register_default_sources()
    from mapgen.sources.base import available_sources

    assert sorted(s.id for s in available_sources()) == ["elevation", "osm", "overture"]


def test_register_default_sources_called_twice_is_a_no_op():
    register_default_sources()
    first_osm = get_source("osm")
    register_default_sources()
    assert get_source("osm") is first_osm
    from mapgen.sources.base import available_sources

    assert sorted(s.id for s in available_sources()) == ["elevation", "osm", "overture"]


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
    """

    def __init__(self):
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        output = Path(command[command.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
        return type("FakeCompleted", (), {"returncode": 0, "stdout": "", "stderr": ""})()


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


# --- Coordinator finding: the node-cap retry ladder must not fire for a
# run that configure() has already routed to Overpass, since the 50000-
# node cap is the map API's own limit and Overpass is not subject to it.
# Confirmed through the full orchestration, not only at OsmSource's own
# unit level. ---------------------------------------------------------


class _NodeCapShapedOverpassSession:
    """An Overpass session that answers every POST with a 400 body
    SHAPED like the map API's own node-cap error text, to prove
    orchestration-level coherence: even if such a response somehow came
    back from Overpass, OsmSource's own use_overpass-scoped check (unit-
    tested directly in test_sources_osm.py) means this must never be
    classified as a NodeCapExceededError, so run_survey's retry ladder
    must never engage for it either.
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


def test_a_node_cap_shaped_failure_on_overpass_does_not_trigger_the_tile_size_retry(tmp_path):
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
    # No retry ladder event at all: this failure was never eligible for
    # one in the first place, not merely "retried until exhausted".
    assert not any(e["event"] == "tile_size_retry" for e in log.events)
    # Confirms the request never even reached a second, smaller-tile
    # attempt: max_retries (4, the default) POSTs for the one and only
    # tile_size_m this ran at, not 4 attempts repeated across a ladder of
    # sizes it should never have entered.
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

    # Overture: 6 of the 8 tile/type combinations already on disk; r01_c01
    # is the one tile the crash caught mid-way, missing both types, the
    # same "most tiles done, one caught mid-flight" shape as the real
    # package's 34-out-of-however-many files.
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

    # Only the two genuinely missing tile/type combinations were fetched,
    # not all 8: resume actually skips completed work rather than
    # refetching it.
    assert len(overture_runner.commands) == 2, (
        f"expected exactly the 2 missing tile/type combinations refetched, "
        f"got {len(overture_runner.commands)}: {overture_runner.commands}"
    )
    fetched_types = {cmd[cmd.index("--type") + 1] for cmd in overture_runner.commands}
    assert fetched_types == {"water", "building"}

    # OSM never contacted a live endpoint: every tile was already done.
    osm_entry = next(s for s in result.survey["sources"] if s["id"] == "osm")
    assert osm_entry["endpoints_used"] == [], (
        "OSM re-contacted an endpoint even though every tile already had a "
        "successful file on disk"
    )

    # The 6 pre-existing Overture files were reused untouched, not
    # re-downloaded and silently overwritten.
    for tile_id, overture_type in preexisting:
        text = (
            paths.work_dir / "raw" / "overture" / overture_type / f"{tile_id}.geojson"
        ).read_text(encoding="utf-8")
        assert f"preexisting-{tile_id}-{overture_type}" in text, (
            f"the already-fetched {tile_id}/{overture_type} file was overwritten during resume"
        )

    # The second, stale fingerprint directory was never read: its poison
    # tile is untouched on disk and never reached the merged output.
    assert stale_osm_file.read_text(encoding="utf-8") == poison_xml
    merged_osm_text = (paths.root / f"{paths.stem}.osm").read_text(encoding="utf-8")
    assert "999999" not in merged_osm_text, (
        "the stale second fingerprint directory leaked into the merged output"
    )


# --- Task 19 item 5: the whole-run retry at a smaller tile size on a node-
# cap failure, restored by owner ruling after Task 17's audit found the
# superseded script had it and mapgen initially dropped it. -----------------


class NodeCapThenSucceedsSource:
    """Raises NodeCapExceededError on its first fetch() call (as if the
    tile at the ORIGINALLY requested size were too dense), then succeeds
    on every call after that (as if a smaller size cleared it). Does not
    model real node-counting: the orchestration under test (does run_survey
    retry at all, does it stop, does it record the right size) does not
    depend on the stub actually knowing anything about tile density.
    """

    id = "osm"
    display_name = "Stub OSM"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self):
        self.attempt = 0
        self.fetch_calls: list[list[str]] = []

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100 * len(tiles), seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        self.attempt += 1
        self.fetch_calls.append([t.tile_id for t in tiles])
        if self.attempt == 1:
            from mapgen.sources.osm import NodeCapExceededError

            raise NodeCapExceededError("Tile r00_c00 exceeded the OSM API 50000-node limit.")
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8")
        return [out]


class AlwaysNodeCapSource(NodeCapThenSucceedsSource):
    """Raises NodeCapExceededError on every single attempt, to test the
    end of the retry ladder rather than a successful step down it.
    """

    def fetch(self, bbox, tiles, work_dir, progress):
        self.attempt += 1
        self.fetch_calls.append([t.tile_id for t in tiles])
        from mapgen.sources.osm import NodeCapExceededError

        raise NodeCapExceededError(f"Tile exceeded the OSM API 50000-node limit (attempt {self.attempt}).")


def test_a_node_cap_failure_automatically_retries_at_the_next_smaller_tile_size(tmp_path):
    source = NodeCapThenSucceedsSource()
    register(source)
    log = EventLog()
    result = run_survey(
        _request(tmp_path, tile_size_m=2000.0, overlap_m=100.0, source_ids=("osm",)),
        progress=log,
    )
    assert result.complete is True
    assert source.attempt == 2, "expected exactly one retry: fail once, then succeed"
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    # The tile size ACTUALLY used, one rung down from what was requested,
    # not the originally requested 2000m: a package must describe itself
    # accurately.
    assert payload["tiling"]["tile_size_m"] == 1500.0
    retry_events = [e for e in log.events if e["event"] == "tile_size_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["previous_tile_size_m"] == 2000.0
    assert retry_events[0]["next_tile_size_m"] == 1500.0


def test_a_node_cap_retry_gets_its_own_fingerprinted_work_dir_not_the_failed_attempts(tmp_path):
    # "Safe by construction", confirmed directly rather than assumed: the
    # retry must land in a DIFFERENT work_dir, and the original, larger-
    # tile-size attempt's own work_dir must not still exist afterwards
    # (cleaned up by the later success, the same property already proven
    # for a sibling scenario by
    # test_a_later_success_at_a_different_tiling_cleans_up_a_failed_siblings_scratch).
    source = NodeCapThenSucceedsSource()
    register(source)
    request_2000 = _request(tmp_path, tile_size_m=2000.0, overlap_m=100.0, source_ids=("osm",))
    # The retry ladder only ever changes tile_size_m/overlap_m (see
    # run_survey), never the category or Overture type selection, so both
    # fingerprints below share request_2000's own effective selection.
    fp_2000 = tiling_fingerprint(
        *request_2000.bbox.as_tuple(),
        2000.0,
        100.0,
        request_2000.effective_categories,
        request_2000.effective_overture_types,
    )
    # Overlap after the retry: min(original 100, max(50, 1500 / 10)) =
    # min(100, 150) = 100, unchanged, since the original overlap is
    # already below the new tile size's own 10% floor.
    fp_1500 = tiling_fingerprint(
        *request_2000.bbox.as_tuple(),
        1500.0,
        100.0,
        request_2000.effective_categories,
        request_2000.effective_overture_types,
    )
    assert fp_2000 != fp_1500

    result = run_survey(request_2000)

    assert result.complete is True
    assert result.paths.work_dir.name == fp_1500
    assert not result.paths.work_dir.exists(), "expected the successful run's own work_dir cleaned up too"
    assert not result.paths.work_dir.parent.exists(), (
        "expected the shared _work/ parent gone entirely, including the abandoned 2000m attempt"
    )


def test_a_node_cap_failure_stops_at_the_smallest_size_and_fails_with_the_existing_message(tmp_path):
    source = AlwaysNodeCapSource()
    register(source)
    log = EventLog()
    with pytest.raises(Exception) as excinfo:
        run_survey(
            _request(tmp_path, tile_size_m=1500.0, overlap_m=75.0, source_ids=("osm",)),
            progress=log,
        )
    # The internal retry signal must never leak to a caller: only the
    # ORIGINAL, existing exception the brief calls "the existing clear
    # message" should ever be visible outside run_survey.
    from mapgen.package import _RetryAtSmallerTileSize
    from mapgen.sources.osm import NodeCapExceededError

    assert not isinstance(excinfo.value, _RetryAtSmallerTileSize)
    assert isinstance(excinfo.value, NodeCapExceededError)
    assert "50000-node limit" in str(excinfo.value)
    # Exactly one retry (1500 -> 1000, the only rung left below 1500), then
    # the second failure at 1000 has nowhere smaller to go and is fatal.
    assert source.attempt == 2
    retry_events = [e for e in log.events if e["event"] == "tile_size_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["next_tile_size_m"] == 1000.0


def test_a_node_cap_failure_at_the_smallest_size_with_force_completes_incomplete_rather_than_raising(
    tmp_path,
):
    # force's existing promise, "continue past a failure and mark the
    # package incomplete rather than stop", must still hold once the
    # retry ladder itself is exhausted: the ladder deciding there is
    # nothing smaller left to try is not a reason to ignore force at that
    # final attempt.
    source = AlwaysNodeCapSource()
    register(source)
    result = run_survey(
        _request(tmp_path, tile_size_m=1000.0, overlap_m=50.0, source_ids=("osm",), force=True)
    )
    assert result.complete is False
    # tile_size_m=1000 has no smaller rung at all, so this must be the
    # very first and only attempt: force never gets a chance to matter
    # unless the ladder was correctly recognised as already exhausted.
    assert source.attempt == 1


def test_a_non_node_cap_failure_is_never_retried_even_with_room_in_the_ladder(tmp_path):
    # The retry is specifically for NodeCapExceededError. Any other
    # failure, even at a tile size with room left in the ladder, must
    # behave exactly as it always has: request.force decides, this
    # mechanism does not get involved at all.
    register(StubSource(fail_on=("r00_c00", "r00_c01", "r01_c00", "r01_c01")))
    with pytest.raises(RuntimeError, match="stub failure"):
        run_survey(_request(tmp_path, tile_size_m=2000.0, overlap_m=100.0, source_ids=("stub",)))


def test_a_node_cap_retry_scales_overlap_down_when_it_would_dwarf_the_smaller_tile(tmp_path):
    # A 900m overlap on a 1000m tile would be nearly the whole tile; the
    # retry scales overlap down to at most 10% of the new tile size (with
    # a 50m floor), matching the superseded script's own formula, rather
    # than carrying a now-disproportionate overlap through unchanged.
    source = NodeCapThenSucceedsSource()
    register(source)
    result = run_survey(
        _request(tmp_path, tile_size_m=2000.0, overlap_m=900.0, source_ids=("osm",))
    )
    assert result.complete is True
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["tiling"]["tile_size_m"] == 1500.0
    # max(50, 1500 / 10) = 150; min(900, 150) = 150.
    assert payload["tiling"]["overlap_m"] == 150.0


def test_next_smaller_node_cap_tile_size_steps_down_the_fixed_ladder():
    from mapgen.package import _next_smaller_node_cap_tile_size

    assert _next_smaller_node_cap_tile_size(2000.0) == 1500.0
    assert _next_smaller_node_cap_tile_size(1500.0) == 1000.0
    assert _next_smaller_node_cap_tile_size(1000.0) is None
    # A request already below every ladder rung has nowhere to go either.
    assert _next_smaller_node_cap_tile_size(800.0) is None
    # A size between two rungs steps to the largest rung still smaller
    # than it, not the smallest overall.
    assert _next_smaller_node_cap_tile_size(1800.0) == 1500.0


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
