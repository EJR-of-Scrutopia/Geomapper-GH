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
        *request.bbox.as_tuple(), request.tile_size_m, request.overlap_m
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
    register(StubSource(fail_on=("r01_c01",)))
    fingerprint = tiling_fingerprint(*BBOX.as_tuple(), 600.0, 50.0)
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
