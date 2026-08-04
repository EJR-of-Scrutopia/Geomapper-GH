import pytest

from mapgen.bridge import (
    BridgeError,
    BridgeRequest,
    _missing_urbano_directory,
    build_command,
    run_bridge,
)
from mapgen.geo import BBox

BBOX = BBox.parse("-3.29,51.38,-3.28,51.39")

# The real text UrbanoBridge's Program.cs prints, captured verbatim from a
# real run on a machine with no Urbano install, stack trace included.
REAL_MISSING_URBANO_OUTPUT = (
    "System.IO.DirectoryNotFoundException: No installed Urbano package with "
    "Urbano.Core.dll and ProjectSetup.dll was found under "
    "C:\\Users\\Param\\AppData\\Roaming\\McNeel\\Rhinoceros\\packages\\8.0\\Urbano2.\n"
    "   at Program.<<Main>$>g__ResolvePackageDirectory|0_23(String explicitPackageDir)"
    " in C:\\Users\\Param\\mapgen-phase1\\tools\\UrbanoBridge\\Program.cs:line 924\n"
    "   at Program.<<Main>$>g__Execute|0_1(Options options) in "
    "C:\\Users\\Param\\mapgen-phase1\\tools\\UrbanoBridge\\Program.cs:line 43\n"
    "   at Program.<<Main>$>g__Run|0_0(String[] args) in "
    "C:\\Users\\Param\\mapgen-phase1\\tools\\UrbanoBridge\\Program.cs:line 21\n"
)


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeRunner:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.commands = []
        self.calls = []
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        self.calls.append((command, kwargs))
        return FakeCompleted(self._returncode, stdout=self._stdout, stderr=self._stderr)


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


# --- Coordinator follow-up on Task 20 finding 1: the owner has no Urbano
# and is not about to install it, so the raw DirectoryNotFoundException and
# C# stack trace would otherwise print before the clean message on EVERY
# single survey. UrbanoBridge already names this exact case; run_bridge now
# recognises it and reports one plain sentence instead of raw C# output. ---


def test_missing_urbano_directory_extracts_the_path_from_the_real_message():
    directory = _missing_urbano_directory(REAL_MISSING_URBANO_OUTPUT)
    assert directory == (
        "C:\\Users\\Param\\AppData\\Roaming\\McNeel\\Rhinoceros\\packages\\8.0\\Urbano2"
    )


def test_missing_urbano_directory_keeps_a_period_that_is_genuinely_part_of_the_path():
    # The real directory contains "8.0", a period that is not the sentence's
    # own terminator. A non-greedy match would stop at "...packages\8" and
    # silently drop "0\Urbano2" from the reported path.
    directory = _missing_urbano_directory(
        "No installed Urbano package with Urbano.Core.dll and ProjectSetup.dll "
        "was found under C:\\pkgs\\8.0\\Urbano2."
    )
    assert directory == "C:\\pkgs\\8.0\\Urbano2"


def test_missing_urbano_directory_returns_none_for_an_unrelated_failure():
    assert _missing_urbano_directory("Segmentation fault (core dumped)\n") is None


def test_run_bridge_does_not_blame_the_owners_rhino_install_for_the_bridges_own_fault(
    tmp_path,
):
    """Task 34 established that Urbano 2.2.1.2 IS installed on the owner's
    machine and simply does not ship Urbano.Core.dll or ProjectSetup.dll:
    both were merged into Urbano.SiteAnalysis.gha, along with every type
    this bridge asks for. The message used to read "Urbano is not
    installed", which sent the owner to check a Rhino install that was
    never broken, for a failure that is entirely the bridge's own out of
    date expectation.
    """
    runner = FakeRunner(returncode=1, stderr=REAL_MISSING_URBANO_OUTPUT)
    with pytest.raises(BridgeError) as excinfo:
        run_bridge(_request(tmp_path), _project(tmp_path), runner=runner)
    message = str(excinfo.value)

    assert "Urbano is not installed" not in message
    assert "Urbano.SiteAnalysis.gha" in message, "it must name where they really are"
    assert (
        "C:\\Users\\Param\\AppData\\Roaming\\McNeel\\Rhinoceros\\packages\\8.0\\Urbano2"
        in message
    ), "the directory it actually looked in is the useful half"
    # And it says what this failure now costs, which since task 35 is
    # nothing: mapgen writes the project setting itself.
    assert "project setting" in message
    # No C# framing anywhere in the message the caller actually raises with.
    assert "DirectoryNotFoundException" not in message
    assert "Program.cs" not in message
    assert "at Program" not in message


def test_run_bridge_does_not_print_the_raw_trace_for_a_recognised_missing_urbano_install(
    tmp_path, capsys
):
    runner = FakeRunner(returncode=1, stderr=REAL_MISSING_URBANO_OUTPUT)
    with pytest.raises(BridgeError):
        run_bridge(_request(tmp_path), _project(tmp_path), runner=runner)
    captured = capsys.readouterr()
    assert "DirectoryNotFoundException" not in captured.out
    assert "DirectoryNotFoundException" not in captured.err
    assert "Program.cs" not in captured.out
    assert "Program.cs" not in captured.err


def test_run_bridge_still_prints_raw_output_for_an_unrecognised_failure(tmp_path, capsys):
    # The one thing that must NOT happen: an unexpected failure must not be
    # silently swallowed the way the recognised case deliberately is.
    runner = FakeRunner(returncode=1, stderr="Some other totally unexpected C# crash\n")
    with pytest.raises(BridgeError, match="exit code 1"):
        run_bridge(_request(tmp_path), _project(tmp_path), runner=runner)
    captured = capsys.readouterr()
    assert "Some other totally unexpected C# crash" in captured.err


def test_run_bridge_reports_a_missing_project_clearly(tmp_path):
    missing = tmp_path / "absent" / "UrbanoBridge.csproj"
    with pytest.raises(BridgeError, match="UrbanoBridge.csproj"):
        run_bridge(_request(tmp_path), missing, runner=FakeRunner())


def test_command_passes_the_output_folder(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    index = command.index("--output-folder")
    assert command[index + 1] == str(tmp_path / "out")


def test_command_passes_granularity_with_default_and_custom_values(tmp_path):
    command_default = build_command(_request(tmp_path), _project(tmp_path))
    assert "--granularity" in command_default
    index = command_default.index("--granularity")
    assert command_default[index + 1] == "Block"

    command_custom = build_command(
        _request(tmp_path, granularity="Zone"), _project(tmp_path)
    )
    index = command_custom.index("--granularity")
    assert command_custom[index + 1] == "Zone"


def test_command_places_separator_immediately_after_project_path(tmp_path):
    command = build_command(_request(tmp_path), _project(tmp_path))
    project_index = command.index("--project")
    separator_index = command.index("--")
    assert separator_index == project_index + 2
    assert command[project_index + 1].endswith("UrbanoBridge.csproj")


def test_run_bridge_does_not_invoke_runner_when_project_is_missing(tmp_path):
    runner = FakeRunner()
    missing = tmp_path / "absent" / "UrbanoBridge.csproj"
    with pytest.raises(BridgeError):
        run_bridge(_request(tmp_path), missing, runner=runner)
    assert runner.commands == []


def test_run_bridge_missing_project_error_includes_build_command(tmp_path):
    missing = tmp_path / "absent" / "UrbanoBridge.csproj"
    with pytest.raises(BridgeError, match="dotnet build"):
        run_bridge(_request(tmp_path), missing, runner=FakeRunner())


def test_run_bridge_passes_check_false_to_subprocess(tmp_path):
    runner = FakeRunner()
    run_bridge(_request(tmp_path), _project(tmp_path), runner=runner)
    assert len(runner.calls) == 1
    command, kwargs = runner.calls[0]
    assert kwargs.get("check") is False


def test_command_with_mixed_skip_flags(tmp_path):
    command = build_command(
        _request(tmp_path, skip_blocks=True, skip_climate=False, skip_elevation=True),
        _project(tmp_path),
    )
    assert "--skip-blocks" in command
    assert "--skip-climate" not in command
    assert "--skip-elevation" in command


def test_command_omits_file_name_stem_when_empty_string(tmp_path):
    command = build_command(_request(tmp_path, file_name_stem=""), _project(tmp_path))
    assert "--file-name-stem" not in command
