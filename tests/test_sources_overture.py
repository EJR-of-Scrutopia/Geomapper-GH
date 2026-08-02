import json

import pytest

from mapgen.geo import BBox, Tile
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
                            {"type": "Feature", "id": "f1", "properties": {}, "geometry": None}
                        ],
                    },
                    handle,
                )
        return FakeCompleted(self._returncode, stderr=self._stderr)


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


def test_layer_filenames_map_the_three_phase_one_layers():
    assert LAYER_FILENAMES == {
        "water": "water.geojson",
        "land_cover": "vegetation.geojson",
        "land_use": "landuse.geojson",
    }


def test_fetch_calls_the_cli_once_per_tile_and_type(tmp_path):
    runner = FakeRunner()
    source = _source(runner, types=("water", "building"))
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"),
        [_tile("r00_c00"), _tile("r00_c01")],
        tmp_path,
        NullProgress(),
    )
    assert len(runner.commands) == 4
    assert len(paths) == 4


def test_fetch_writes_into_a_per_type_subfolder(tmp_path):
    source = _source(FakeRunner())
    paths = source.fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert paths[0] == tmp_path / "water" / "r00_c00.geojson"


def test_fetch_passes_the_query_bbox_to_the_cli(tmp_path):
    runner = FakeRunner()
    _source(runner).fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert "--bbox=-3.2900000,51.3800000,-3.2800000,51.3900000" in runner.commands[0]


def test_fetch_skips_a_type_and_tile_already_downloaded(tmp_path):
    target = tmp_path / "water" / "r00_c00.geojson"
    target.parent.mkdir(parents=True)
    target.write_text('{"type":"FeatureCollection","features":[]}', encoding="utf-8")
    runner = FakeRunner()
    _source(runner).fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    assert runner.commands == []


def test_fetch_reports_a_cli_failure_with_its_stderr(tmp_path):
    runner = FakeRunner(returncode=1, stderr="release not found")
    with pytest.raises(OvertureError, match="release not found"):
        _source(runner).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_fetch_points_the_cli_at_a_part_file_not_the_final_path(tmp_path):
    runner = FakeRunner()
    _source(runner).fetch(
        BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
    )
    written_to = runner.commands[0][runner.commands[0].index("--output") + 1]
    assert written_to.endswith(".part")


def test_fetch_leaves_no_partial_file_when_the_cli_fails(tmp_path):
    runner = FakeRunner(returncode=1, stderr="boom")
    with pytest.raises(OvertureError):
        _source(runner).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )
    assert not (tmp_path / "water" / "r00_c00.geojson").exists()
    assert list((tmp_path / "water").glob("*.part")) == []


def test_fetch_fails_loudly_when_the_cli_exits_cleanly_without_writing(tmp_path):
    class SilentRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            self.commands.append(command)
            return FakeCompleted(0)

    with pytest.raises(OvertureError, match="wrote nothing"):
        _source(SilentRunner()).fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_fetch_reports_a_missing_cli_clearly(tmp_path):
    source = OvertureSource(
        types=["water"], runner=FakeRunner(), executable_finder=lambda _name: None
    )
    with pytest.raises(OvertureError, match="overturemaps"):
        source.fetch(
            BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress()
        )


def test_fetch_includes_the_release_when_configured(tmp_path):
    runner = FakeRunner()
    source = OvertureSource(
        types=["water"],
        release="2026-02-18.0",
        runner=runner,
        executable_finder=lambda _name: "overturemaps",
    )
    source.fetch(BBox.parse("-3.29,51.38,-3.28,51.39"), [_tile()], tmp_path, NullProgress())
    assert "--release" in runner.commands[0]
    assert "2026-02-18.0" in runner.commands[0]


def test_merge_writes_one_file_per_type(tmp_path):
    work = tmp_path / "work"
    for overture_type in ("water", "building"):
        folder = work / overture_type
        folder.mkdir(parents=True)
        (folder / "r00_c00.geojson").write_text(
            '{"type":"FeatureCollection","features":[]}', encoding="utf-8"
        )

    source = OvertureSource(types=["water", "building"])
    parts = [
        work / "water" / "r00_c00.geojson",
        work / "building" / "r00_c00.geojson",
    ]
    outputs = source.merge(parts, tmp_path / "out")
    assert sorted(p.name for p in outputs) == ["building.geojson", "water.geojson"]


def test_estimate_scales_with_tiles_and_types():
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    one_type = OvertureSource(types=["water"]).estimate(bbox, [_tile()])
    two_types = OvertureSource(types=["water", "building"]).estimate(bbox, [_tile()])
    assert two_types.bytes_estimate > one_type.bytes_estimate
