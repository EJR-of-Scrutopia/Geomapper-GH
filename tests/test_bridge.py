from pathlib import Path

import pytest

from mapgen.bridge import BridgeError, BridgeRequest, build_command, run_bridge
from mapgen.geo import BBox

BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")


class FakeCompleted:
    def __init__(self, returncode=0):
        self.returncode = returncode


class FakeRunner:
    def __init__(self, returncode=0):
        self.commands = []
        self._returncode = returncode

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        return FakeCompleted(self._returncode)


def _request(tmp_path, **overrides):
    defaults = dict(
        bbox=BBOX,
        output_dir=tmp_path / "out",
        file_name_stem="Barry-Waterfront_2026-08-01",
    )
    defaults.update(overrides)
    return BridgeRequest(**defaults)


def _project(tmp_path):
    project = tmp_path / "UrbanoBridge.csproj"
    project.write_text("<Project/>", encoding="utf-8")
    return project


def test_command_invokes_dotnet_run_on_the_project(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    assert command[:3] == ["dotnet", "run", "--project"]
    assert command[3].endswith("UrbanoBridge.csproj")
    assert "--" in command


def test_command_passes_the_bbox_as_west_south_east_north(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    index = command.index("--bbox")
    assert command[index + 1] == "-3.2900000,51.3800000,-3.2800000,51.3900000"


def test_command_passes_the_file_name_stem(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    index = command.index("--file-name-stem")
    assert command[index + 1] == "Barry-Waterfront_2026-08-01"


def test_command_omits_the_stem_when_none_so_the_bridge_uses_coordinates(tmp_path):
    command = build_command(_request(tmp_path, file_name_stem=None), _project(tmp_path))
    assert "--file-name-stem" not in command


def test_command_includes_skip_flags_that_are_set(tmp_path):
    command = build_command(
        _request(tmp_path, skip_blocks=True, skip_climate=True, skip_elevation=True),
        _project(tmp_path),
    )
    assert "--skip-blocks" in command
    assert "--skip-climate" in command
    assert "--skip-elevation" in command


def test_command_omits_skip_flags_that_are_not_set(tmp_path):
    command = build_command(
        _request(tmp_path, skip_blocks=False, skip_climate=False, skip_elevation=False),
        _project(tmp_path),
    )
    assert "--skip-blocks" not in command
    assert "--skip-climate" not in command
    assert "--skip-elevation" not in command


def test_command_passes_optional_file_paths(tmp_path):
    osm = tmp_path / "all.osm"
    dem = tmp_path / "elevation.tif"
    command = build_command(
        _request(tmp_path, osm_file_path=osm, elevation_tiff_path=dem), _project(tmp_path)
    )
    assert command[command.index("--osm-file-path") + 1] == str(osm)
    assert command[command.index("--elevation-tiff-path") + 1] == str(dem)


def test_run_bridge_invokes_the_runner(tmp_path):
    runner = FakeRunner()
    run_bridge(_request(tmp_path), _project(tmp_path), runner=runner)
    assert len(runner.commands) == 1


def test_run_bridge_raises_on_a_non_zero_exit(tmp_path):
    with pytest.raises(BridgeError, match="exit code 1"):
        run_bridge(_request(tmp_path), _project(tmp_path), runner=FakeRunner(returncode=1))


def test_run_bridge_reports_a_missing_project_clearly(tmp_path):
    missing = tmp_path / "absent" / "UrbanoBridge.csproj"
    with pytest.raises(BridgeError, match="UrbanoBridge.csproj"):
        run_bridge(_request(tmp_path), missing, runner=FakeRunner())
