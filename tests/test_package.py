import json
from datetime import date
from pathlib import Path

import pytest

from mapgen.geo import BBox
from mapgen.jobs import CancelToken, Cancelled, EventLog
from mapgen.naming import PathTooLongError
from mapgen.package import (
    SurveyRequest,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import Estimate, clear_registry, register

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

    def merge(self, parts, out_dir):
        out = out_dir / f"{self.id}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


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


def test_work_dir_is_kept_when_requested(tmp_path):
    register(StubSource())
    result = run_survey(_request(tmp_path, keep_work=True))
    assert result.paths.work_dir.is_dir()


def test_a_failing_tile_marks_the_package_incomplete(tmp_path):
    register(StubSource(fail_on=("r00_c00",)))
    result = run_survey(_request(tmp_path, force=True))
    assert result.complete is False
    payload = json.loads(result.paths.survey_json.read_text(encoding="utf-8"))
    assert payload["complete"] is False


def test_work_dir_is_retained_after_a_failure_so_the_job_can_resume(tmp_path):
    register(StubSource(fail_on=("r00_c00",)))
    result = run_survey(_request(tmp_path, force=True))
    assert result.paths.work_dir.is_dir()


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


def test_register_default_sources_registers_the_three_phase_one_sources():
    register_default_sources()
    from mapgen.sources.base import available_sources

    assert sorted(s.id for s in available_sources()) == ["elevation", "osm", "overture"]
