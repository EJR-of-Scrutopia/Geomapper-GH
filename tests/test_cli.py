import json
import subprocess
import sys
from pathlib import Path

import pytest

import mapgen.cli as cli
from mapgen.cli import build_parser, main
from mapgen.sources.base import Estimate, clear_registry, register
from mapgen.sources.elevation import ElevationError
from mapgen.sources.osm import NodeCapExceededError, OsmDownloadError
from mapgen.sources.overture import OvertureError


class StubSource:
    id = "stub"
    display_name = "Stub"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        out = out_dir / "stub.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


class AlwaysFailsStubSource:
    """Same shape as StubSource, but fetch() never produces any output.

    Used with --force so run_survey tolerates the failure instead of
    propagating it, landing on a genuinely incomplete package.
    """

    id = "stub"
    display_name = "Stub"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        raise RuntimeError("stub source failure, on purpose")

    def merge(self, parts, out_dir, stem):
        out = out_dir / "stub.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


@pytest.fixture(autouse=True)
def _isolated_registry():
    clear_registry()
    register(StubSource())
    yield
    clear_registry()


def test_parser_exposes_every_expected_subcommand():
    parser = build_parser()
    actions = [a for a in parser._actions if a.dest == "command"]
    assert set(actions[0].choices) >= {
        "survey",
        "estimate",
        "ui",
        "plan",
        "download",
        "merge",
        "urbano-package",
        # Task 29, the inverse of --skip-bridge.
        "bridge",
    }


def test_survey_requires_region_and_site():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["survey", "--bbox=-3.29,51.38,-3.28,51.39"])


def test_estimate_prints_tile_count(tmp_path, capsys):
    exit_code = main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
        ]
    )
    assert exit_code == 0
    assert "Tiles:" in capsys.readouterr().out


def test_a_sub_minute_estimate_is_printed_in_seconds_not_as_zero_minutes():
    # Task 25 refitted every source against the concurrent, untiled code
    # path, and a real single-tile survey came out at about 29 seconds.
    # The unconditional minutes format printed that as "0 min", which
    # reads as instant. Nothing had ever hit it before, because every
    # estimate was inflated enough to clear a minute.
    assert cli.format_estimated_duration(29.0) == "29 s"
    assert cli.format_estimated_duration(0.0) == "0 s"
    assert cli.format_estimated_duration(59.9) == "60 s"
    assert cli.format_estimated_duration(60.0) == "1 min"
    assert cli.format_estimated_duration(175.0) == "3 min"


def test_estimate_json_output_is_parseable(tmp_path, capsys):
    main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["tiles"] >= 1


def test_survey_creates_the_package_folder(tmp_path):
    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry Waterfront",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
            "--skip-bridge",
            "--date",
            "2026-08-01",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront" / "survey.json").exists()


def test_survey_reports_a_bad_bbox_without_a_traceback(tmp_path, capsys):
    exit_code = main(
        [
            "survey",
            "--bbox=nonsense",
            "--region=R",
            "--site=S",
            "--output-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 2
    assert "bbox" in capsys.readouterr().err.lower()


def test_survey_reports_a_path_that_is_too_long_without_a_traceback(capsys):
    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry Waterfront",
            "--output-root",
            "C:/" + "x" * 200,
            "--source",
            "stub",
        ]
    )
    assert exit_code == 1
    assert "240" in capsys.readouterr().err


def test_estimate_reports_a_zero_tile_size_without_a_traceback(tmp_path, capsys):
    # Review round 1: build_tiles now raises TilingError (a ValueError) for
    # tile_size_m <= 0 instead of an uncaught ZeroDivisionError. main()'s
    # except tuple needs it named explicitly, the same as BBoxError and
    # NamingError, or a CLI user hitting this gets a raw traceback instead
    # of the clean, one-line message every other validation error gets.
    exit_code = main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=R",
            "--site=S",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "0",
            "--source",
            "stub",
        ]
    )
    assert exit_code == 1
    assert "greater than zero" in capsys.readouterr().err


# --- A coordinator review's Important 2: main()'s except tuple covered a
# request that could never be BUILT (a bad name, bbox, source id or
# tiling), but not one that failed once it started actually downloading.
# OsmDownloadError (NodeCapExceededError included, it subclasses this),
# OvertureError, ElevationError and UnknownCategoryError all used to
# escape as a raw, unhandled Python traceback instead of the same plain,
# one-line message the browser path already gave the same failures. Each
# fake source below uses its own made-up id (never "osm"/"overture"/
# "elevation") so it sits alongside, not instead of, the real default
# sources main() itself registers, the same way StubSource and
# AlwaysFailsStubSource above already do. -------------------------------


class _RaisesOsmDownloadErrorSource:
    id = "raises-osm-download-error"
    display_name = "Raises OsmDownloadError"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        raise OsmDownloadError("a distinctive OsmDownloadError from a test source")

    def merge(self, parts, out_dir, stem):
        return []


class _RaisesNodeCapExceededErrorSource:
    """NodeCapExceededError specifically, not just its OsmDownloadError
    parent: this is the one main() would actually meet in practice (a tile
    still over the node cap after being split as far as splitting goes),
    so it gets its own test rather than trusting the inheritance
    relationship alone.
    """

    id = "raises-node-cap-error"
    display_name = "Raises NodeCapExceededError"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        # Constructed the way the real raise sites construct it, tile and
        # all: NodeCapExceededError has no tile-less form, so a stub that
        # could omit it would be a more permissive thing than the code it
        # stands in for.
        raise NodeCapExceededError(
            "a distinctive NodeCapExceededError from a test source", tile=tiles[0]
        )

    def merge(self, parts, out_dir, stem):
        return []


class _RaisesOvertureErrorSource:
    id = "raises-overture-error"
    display_name = "Raises OvertureError"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        raise OvertureError("a distinctive OvertureError from a test source")

    def merge(self, parts, out_dir, stem):
        return []


class _RaisesElevationErrorSource:
    id = "raises-elevation-error"
    display_name = "Raises ElevationError"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        raise ElevationError("a distinctive ElevationError from a test source")

    def merge(self, parts, out_dir, stem):
        return []


def _survey_error_case_args(tmp_path, source_id, **extra):
    args = [
        "survey",
        "--bbox=-3.29,51.38,-3.28,51.39",
        "--region=South Wales",
        "--site=Barry Waterfront",
        "--output-root",
        str(tmp_path),
        "--source",
        source_id,
        "--skip-bridge",
    ]
    for flag, value in extra.items():
        args.extend([f"--{flag.replace('_', '-')}", str(value)])
    return args


def test_survey_reports_an_osm_download_error_without_a_traceback(tmp_path, capsys):
    register(_RaisesOsmDownloadErrorSource())
    exit_code = main(_survey_error_case_args(tmp_path, "raises-osm-download-error"))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "a distinctive OsmDownloadError from a test source" in err
    assert "Traceback" not in err


def test_survey_reports_a_node_cap_exceeded_error_without_a_traceback(tmp_path, capsys):
    # The realistic case: a tile still over the cap after OsmSource has
    # split it as far as it is allowed to, which is the one way this
    # error reaches main() at all.
    register(_RaisesNodeCapExceededErrorSource())
    exit_code = main(_survey_error_case_args(tmp_path, "raises-node-cap-error", tile_size_m=1000))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "a distinctive NodeCapExceededError from a test source" in err
    assert "Traceback" not in err


def test_survey_reports_an_overture_error_without_a_traceback(tmp_path, capsys):
    register(_RaisesOvertureErrorSource())
    exit_code = main(_survey_error_case_args(tmp_path, "raises-overture-error"))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "a distinctive OvertureError from a test source" in err
    assert "Traceback" not in err


def test_survey_reports_an_elevation_error_without_a_traceback(tmp_path, capsys):
    register(_RaisesElevationErrorSource())
    exit_code = main(_survey_error_case_args(tmp_path, "raises-elevation-error"))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "a distinctive ElevationError from a test source" in err
    assert "Traceback" not in err


def test_survey_reports_an_unknown_category_without_a_traceback(tmp_path, capsys):
    # No fake source needed: this is raised by SurveyRequest.__post_init__
    # (see mapgen.categories.validate_categories) before any source is
    # ever touched, from the CLI's own --category flag, the exact live
    # reproduction a coordinator review used for Critical 1 ("building"
    # for the real id "buildings").
    exit_code = main(_survey_error_case_args(tmp_path, "stub", category="building"))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Unknown category" in err
    assert "buildings" in err
    assert "Traceback" not in err


def test_categories_command_lists_every_leaf_id(capsys):
    from mapgen.categories import ALL_CATEGORY_IDS

    exit_code = main(["categories"])
    assert exit_code == 0
    out = capsys.readouterr().out
    for category_id in ALL_CATEGORY_IDS:
        assert category_id in out, f"expected {category_id!r} to be listed"


def test_sources_command_lists_every_source_with_its_licence(capsys):
    # Review finding N6: command_sources never executed in any test, and
    # it is the command that prints each source's licence, which is the
    # thing this project is most careful about everywhere else. Read off
    # the registry rather than a hand-listed expectation, so a fourth
    # source in phase 2 is covered by this without anyone remembering.
    from mapgen.package import register_default_sources
    from mapgen.sources.base import available_sources

    register_default_sources()
    exit_code = main(["sources"])
    assert exit_code == 0
    out = capsys.readouterr().out
    sources = available_sources()
    assert sources, "the registry was empty, so this proved nothing"
    for source in sources:
        assert source.id in out, f"expected {source.id!r} to be listed"
        assert source.display_name in out
        assert source.licence in out, f"{source.id} was listed without its licence"
        if source.requires_api_key:
            assert "needs an API key" in out


def test_category_flag_reaches_the_survey_request(tmp_path):
    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
            "--skip-bridge",
            "--date",
            "2026-08-01",
            "--category",
            "buildings",
            "--category",
            "water",
        ]
    )
    assert exit_code == 0
    payload = json.loads(
        (tmp_path / "South-Wales" / "2026-08-01_Barry" / "survey.json").read_text(
            encoding="utf-8"
        )
    )
    assert sorted(payload["categories"]) == ["buildings", "water"]


def test_coordinate_stem_flag_is_accepted():
    parser = build_parser()
    args = parser.parse_args(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=R",
            "--site=S",
            "--coordinate-stem",
        ]
    )
    assert args.coordinate_stem is True


def test_survey_reports_an_incomplete_package_on_stderr_and_exits_1(tmp_path, capsys):
    clear_registry()
    register(AlwaysFailsStubSource())
    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
            "--skip-bridge",
            "--force",
        ]
    )
    assert exit_code == 1
    assert "incomplete" in capsys.readouterr().err.lower()


# --- Coordinator follow-up on Task 20 finding 1: the summary must describe
# what is actually on disk, not what the bridge would have produced if it
# had succeeded. run_survey is faked here (rather than a real dotnet
# invocation) so this exercises exactly command_survey's own branching,
# fast and independent of whether dotnet or Urbano are present at all. ------


class _FakePaths:
    def __init__(self, root, project_setting):
        self.root = root
        self.project_setting = project_setting


class _FakeSurveyResult:
    def __init__(self, paths, complete, survey):
        self.paths = paths
        self.complete = complete
        self.survey = survey


def _survey_args(tmp_path):
    return [
        "survey",
        "--bbox=-3.29,51.38,-3.28,51.39",
        "--region=R",
        "--site=S",
        "--output-root",
        str(tmp_path),
        "--source",
        "stub",
    ]


def test_survey_summary_names_the_project_setting_when_the_bridge_succeeded(
    tmp_path, capsys, monkeypatch
):
    project_setting = tmp_path / "S_2026-08-01_project_setting.json"
    fake_result = _FakeSurveyResult(
        paths=_FakePaths(root=tmp_path, project_setting=project_setting),
        complete=True,
        survey={"bridge": {"attempted": True, "ok": True, "error": None}},
    )
    monkeypatch.setattr("mapgen.cli.run_survey", lambda *a, **k: fake_result)
    exit_code = main(_survey_args(tmp_path))
    out = capsys.readouterr().out
    assert exit_code == 0
    assert f"Urbano project setting: {project_setting.name}" in out


def test_survey_summary_does_not_claim_a_project_setting_when_the_bridge_failed(
    tmp_path, capsys, monkeypatch
):
    # The exact bug reported: after a failed bridge, the directory holds
    # only survey.json and the merged data, never the project setting, but
    # the old summary printed its name anyway.
    project_setting = tmp_path / "S_2026-08-01_project_setting.json"
    error = "The Urbano bridge failed with exit code 1. See the output above for details."
    fake_result = _FakeSurveyResult(
        paths=_FakePaths(root=tmp_path, project_setting=project_setting),
        complete=True,
        survey={"bridge": {"attempted": True, "ok": False, "error": error}},
    )
    monkeypatch.setattr("mapgen.cli.run_survey", lambda *a, **k: fake_result)
    exit_code = main(_survey_args(tmp_path))
    captured = capsys.readouterr()
    assert exit_code == 0
    assert project_setting.name not in captured.out
    assert "not produced" in captured.out
    assert f"Urbano bridge step failed: {error}" in captured.err


def test_survey_summary_says_the_bridge_was_skipped_when_it_was(tmp_path, capsys, monkeypatch):
    project_setting = tmp_path / "S_2026-08-01_project_setting.json"
    fake_result = _FakeSurveyResult(
        paths=_FakePaths(root=tmp_path, project_setting=project_setting),
        complete=True,
        survey={"bridge": {"attempted": False, "ok": None, "error": None}},
    )
    monkeypatch.setattr("mapgen.cli.run_survey", lambda *a, **k: fake_result)
    exit_code = main(_survey_args(tmp_path))
    captured = capsys.readouterr()
    assert exit_code == 0
    assert project_setting.name not in captured.out
    assert "skipped" in captured.out
    assert captured.err == ""


def test_estimate_with_no_output_root_survives_a_corrupt_config(tmp_path, capsys, monkeypatch):
    # The seam under test is _request_from_args falling through to
    # load_config() when --output-root is not passed. estimate is used
    # rather than survey because estimate_survey() only ever checks whether
    # a package folder exists, it never creates one, so this cannot write
    # anywhere on disk even though load_config() falls back to the real
    # home-directory default for output_root.
    corrupt = tmp_path / "corrupt_config.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", corrupt)

    exit_code = main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--tile-size-m",
            "600",
            "--source",
            "stub",
        ]
    )
    assert exit_code == 0
    assert "Tiles:" in capsys.readouterr().out


# --- Review round 2: the CLI path had no coverage at all, and it is the
# one place a leak-around-the-message actually reached a human. Captured
# from a real run, per the review: the [source_failed] line correctly
# showed the redacted key, and three lines below it, in the chained
# traceback Python's own default exception printer renders for an
# exception main() does not catch, the real key. main() is not expected
# to catch ElevationError specifically (a genuinely unanticipated source
# failure printing a traceback is reasonable CLI behaviour); what must be
# true regardless is that the traceback, however it gets printed, never
# contains the key. This runs the real CLI entry point as a real
# subprocess, so what is asserted against is exactly what a terminal
# would show, not a simulation of it.


def test_a_leaking_elevation_failure_never_reaches_cli_stdout_or_stderr(tmp_path):
    secret = "sk-real-secret-should-never-leak-anywhere"
    output_root = tmp_path / "out"
    script = tmp_path / "leak_repro.py"
    script.write_text(
        f'''
import sys
import requests
from mapgen.sources.base import register, clear_registry
from mapgen.sources.elevation import ElevationSource
from mapgen.cli import main

SECRET = {secret!r}

class LeakySession:
    def get(self, url, **kwargs):
        leaky_url = url + "?API_Key=" + SECRET + "&demtype=COP30"
        raise requests.exceptions.ConnectionError(
            "Max retries exceeded with url: " + leaky_url
        )

clear_registry()
register(ElevationSource(api_key=SECRET, session=LeakySession()))
sys.exit(main([
    "survey", "--bbox=-3.29,51.38,-3.28,51.39", "--region=R", "--site=S",
    "--output-root", {str(output_root)!r}, "--source", "elevation", "--skip-bridge",
]))
''',
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert secret not in result.stdout, f"leaked into stdout:\n{result.stdout}"
    assert secret not in result.stderr, f"leaked into stderr:\n{result.stderr}"
    # Not a test that the command quietly did nothing: a genuinely
    # unhandled failure exits non-zero and says something recognisable.
    assert result.returncode != 0
    assert "ElevationError" in result.stderr or "Failed to download DEM" in result.stderr
    # And the chain really is suppressed in the real, rendered traceback,
    # not merely short by coincidence.
    assert "direct cause" not in result.stderr
    assert "another exception occurred" not in result.stderr


# --- Task 19 item 4: the windowless launch --------------------------------


def test_ui_windowless_flag_defaults_to_false():
    parser = build_parser()
    args = parser.parse_args(["ui"])
    assert args.windowless is False


def test_ui_windowless_flag_is_accepted():
    parser = build_parser()
    args = parser.parse_args(["ui", "--windowless"])
    assert args.windowless is True


def test_command_ui_passes_no_heartbeat_timeout_without_the_flag(monkeypatch):
    # mapgen ui from a terminal must keep behaving exactly as it does
    # today: no heartbeat wiring at all unless --windowless was given.
    captured = {}
    monkeypatch.setattr("mapgen.web.server.serve", lambda **kwargs: captured.update(kwargs))
    parser = build_parser()
    args = parser.parse_args(["ui", "--no-browser"])
    exit_code = cli.command_ui(args)
    assert exit_code == 0
    assert captured["heartbeat_timeout_seconds"] is None


def test_command_ui_passes_the_default_heartbeat_timeout_with_the_flag(monkeypatch):
    captured = {}
    monkeypatch.setattr("mapgen.web.server.serve", lambda **kwargs: captured.update(kwargs))
    parser = build_parser()
    args = parser.parse_args(["ui", "--no-browser", "--windowless"])
    exit_code = cli.command_ui(args)
    assert exit_code == 0
    from mapgen.web.server import DEFAULT_HEARTBEAT_TIMEOUT_SECONDS

    assert captured["heartbeat_timeout_seconds"] == DEFAULT_HEARTBEAT_TIMEOUT_SECONDS


def test_command_ui_reraises_on_failure_without_windowless(monkeypatch):
    def boom(**kwargs):
        raise OSError("port already in use")

    monkeypatch.setattr("mapgen.web.server.serve", boom)
    parser = build_parser()
    args = parser.parse_args(["ui", "--no-browser"])
    with pytest.raises(OSError, match="port already in use"):
        cli.command_ui(args)


def test_command_ui_reports_windowless_failure_instead_of_raising(monkeypatch, capsys):
    def boom(**kwargs):
        raise OSError("port already in use")

    monkeypatch.setattr("mapgen.web.server.serve", boom)
    reported = []
    monkeypatch.setattr(cli, "_report_windowless_failure", lambda exc: reported.append(exc))
    parser = build_parser()
    args = parser.parse_args(["ui", "--no-browser", "--windowless"])
    exit_code = cli.command_ui(args)
    assert exit_code == 1
    assert len(reported) == 1
    assert "port already in use" in str(reported[0])


def test_install_windowless_safety_is_a_noop_with_a_real_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.__stdout__, raising=False)
    monkeypatch.setattr(sys, "stderr", sys.__stderr__, raising=False)
    assert cli._install_windowless_safety() is False
    # Must not have touched either, having decided there was nothing to do.
    assert sys.stdout is sys.__stdout__
    assert sys.stderr is sys.__stderr__


def test_install_windowless_safety_redirects_stdio_when_there_is_no_console(monkeypatch, tmp_path):
    log_path = tmp_path / "ui.log"
    monkeypatch.setattr(cli, "WINDOWLESS_LOG_PATH", log_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    try:
        assert cli._install_windowless_safety() is True
        assert sys.stdout is not None, "expected stdout redirected rather than left None"
        assert sys.stderr is not None, "expected stderr redirected rather than left None"
        # The specific failure this whole function exists to prevent: an
        # ordinary print() must not crash with AttributeError on a None
        # stream once this has run.
        print("hello from a windowless session", flush=True)
    finally:
        if sys.stdout is not None:
            sys.stdout.close()
    assert log_path.exists()
    content = log_path.read_text(encoding="utf-8")
    assert "started" in content
    assert "hello from a windowless session" in content


def test_report_windowless_failure_calls_the_injected_message_box(capsys):
    calls = []
    cli._report_windowless_failure(RuntimeError("boom"), message_box=calls.append)
    assert len(calls) == 1
    assert "boom" in calls[0]
    assert "mapgen ui failed to start" in calls[0]
    assert str(cli.WINDOWLESS_LOG_PATH) in calls[0]


def test_report_windowless_failure_swallows_a_failing_message_box(capsys):
    def failing_message_box(message):
        raise RuntimeError("no display available")

    # Must not raise: a message box failing must never mask the original
    # error or blow up a failure-reporting path with a second exception.
    cli._report_windowless_failure(RuntimeError("original problem"), message_box=failing_message_box)


# --- Task 24: ConsoleProgress is now written to from several threads ----


class _RecordingLock:
    """A lock that records that it was actually taken.

    Deliberately not a stress test. This project has already written one
    of those, many threads against an unguarded shared list, and it passed
    against the broken code because the interleaving it hoped to observe
    never happened to occur. Asserting that the write went out with the
    lock held is a fact about the code, not about the scheduler, so it
    fails for the stated reason every time or not at all.
    """

    def __init__(self):
        self.acquisitions = 0
        self.held = False

    def __enter__(self):
        self.acquisitions += 1
        self.held = True
        return self

    def __exit__(self, *exc_info):
        self.held = False
        return False


class _HeldWhenWritten:
    def __init__(self, lock):
        self._lock = lock
        self.writes = []

    def write(self, text):
        self.writes.append((text, self._lock.held))
        return len(text)

    def flush(self):
        return None


def test_console_progress_writes_every_event_with_its_lock_held(monkeypatch):
    # OvertureSource.fetch emits from up to eight worker threads at once
    # (Task 24). print() writes the text and the line ending as two
    # separate calls on sys.stdout, so without this lock a second thread
    # can land between them and put two events on one line.
    progress = cli.ConsoleProgress()
    lock = _RecordingLock()
    progress._lock = lock
    stream = _HeldWhenWritten(lock)
    monkeypatch.setattr(sys, "stdout", stream)

    progress.emit("tile_done", source="overture", tile_id="r00_c00")
    progress.emit("tile_done", source="overture", tile_id="r00_c01")

    assert lock.acquisitions == 2
    assert stream.writes, "nothing was written at all"
    assert all(held for _text, held in stream.writes), (
        f"a write went out with the lock released: {stream.writes}"
    )


def test_console_progress_still_prints_one_readable_line_per_event(capsys):
    # The lock must not have changed what the owner actually reads.
    progress = cli.ConsoleProgress()
    progress.emit("tile_done", source="overture", tile_id="r00_c00")
    lines = capsys.readouterr().out.splitlines()
    assert lines == ["[tile_done] source=overture tile_id=r00_c00"]


# --- Task 28: --demtype -------------------------------------------------

# ElevationSource refuses anything that is not a real TIFF (see is_tiff),
# so the fake transport below has to hand back one.
_TIFF_HEADER = b"II*\x00" + b"\x00" * 128


class _FakeDemResponse:
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
    def __init__(self):
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(kwargs)
        return _FakeDemResponse(_TIFF_HEADER)


def _register_real_elevation_source():
    """The real ElevationSource with a fake transport, not a stub.

    register_default_sources(), which main() calls on every invocation,
    skips an id already held by an instance of the same TYPE (see
    package.register_default_sources), so registering a real one here is
    what lets the CLI's own default registration leave it alone. A stub
    class would collide instead, and a stub that did not collide would
    prove the CLI passed a string somewhere rather than that the real
    source ended up configured with it.
    """
    from mapgen.sources.base import register as register_source
    from mapgen.sources.elevation import ElevationSource

    session = _FakeDemSession()
    register_source(ElevationSource(api_key="test-key", session=session))
    return session


def _estimate_args(tmp_path, *extra):
    return [
        "estimate",
        "--bbox=-3.29,51.38,-3.28,51.39",
        "--region=South Wales",
        "--site=Barry",
        "--output-root",
        str(tmp_path),
        "--tile-size-m",
        "600",
        "--source",
        "elevation",
        *extra,
    ]


def test_demtype_flag_reaches_the_source(tmp_path, capsys, monkeypatch):
    # `mapgen estimate` prints each selected source's display_name, and
    # only a source configure()d for this request names its model, so this
    # line is the whole chain: flag, SurveyRequest, _configured_sources,
    # ElevationSource.configure.
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "no-such-config.json")
    _register_real_elevation_source()

    exit_code = main(_estimate_args(tmp_path, "--demtype", "EU_DTM"))

    assert exit_code == 0
    assert "Elevation (OpenTopography EU_DTM)" in capsys.readouterr().out


def test_without_the_flag_the_saved_model_is_used(tmp_path, capsys, monkeypatch):
    # The browser saves this setting; the CLI has to honour it, or the two
    # halves of one tool disagree about what a plain `mapgen survey` does.
    config = tmp_path / "config.json"
    config.write_text('{"elevation_demtype": "NASADEM"}', encoding="utf-8")
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", config)
    _register_real_elevation_source()

    exit_code = main(_estimate_args(tmp_path))

    assert exit_code == 0
    assert "Elevation (OpenTopography NASADEM)" in capsys.readouterr().out


def test_the_flag_wins_over_the_saved_model_without_overwriting_it(tmp_path, capsys, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text('{"elevation_demtype": "NASADEM"}', encoding="utf-8")
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", config)
    _register_real_elevation_source()

    main(_estimate_args(tmp_path, "--demtype", "SRTMGL3"))

    assert "Elevation (OpenTopography SRTMGL3)" in capsys.readouterr().out
    # A flag passed for one run is that run's choice, never a new default.
    assert json.loads(config.read_text(encoding="utf-8"))["elevation_demtype"] == "NASADEM"


def test_the_chosen_model_is_what_gets_downloaded(tmp_path, monkeypatch):
    # And not merely what gets printed: the same flag, through `survey`
    # this time, ends up as the demtype parameter on the actual request.
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "no-such-config.json")
    session = _register_real_elevation_source()

    exit_code = main(
        [
            "survey",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "elevation",
            "--skip-bridge",
            "--demtype",
            "AW3D30",
        ]
    )

    assert exit_code == 0
    assert session.calls, "the elevation source was never asked for anything"
    assert session.calls[0]["params"]["demtype"] == "AW3D30"


def test_survey_reports_an_unknown_demtype_without_a_traceback(tmp_path, capsys, monkeypatch):
    # Raised by SurveyRequest.__post_init__ before any source is touched,
    # exactly as an unknown --category is, and reported the same plain
    # one-line way rather than as twenty lines of Python.
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", tmp_path / "no-such-config.json")
    exit_code = main(_survey_error_case_args(tmp_path, "stub", demtype="COP-30"))
    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Unknown elevation model" in err
    assert "COP-30" in err
    assert "COP30" in err
    assert "Traceback" not in err


def test_a_hand_edited_config_with_a_bad_model_fails_plainly_too(tmp_path, capsys, monkeypatch):
    # The other way a bad value arrives: not a typed flag, a saved file.
    # load_config only checks the TYPE of a field, so a bad string reaches
    # SurveyRequest, which is the single place that knows the vocabulary.
    config = tmp_path / "config.json"
    config.write_text('{"elevation_demtype": "nonsense"}', encoding="utf-8")
    monkeypatch.setattr("mapgen.config.CONFIG_PATH", config)

    exit_code = main(
        [
            "estimate",
            "--bbox=-3.29,51.38,-3.28,51.39",
            "--region=South Wales",
            "--site=Barry",
            "--output-root",
            str(tmp_path),
            "--tile-size-m",
            "600",
            "--source",
            "stub",
        ]
    )

    assert exit_code == 1
    err = capsys.readouterr().err
    assert "Unknown elevation model" in err
    assert "Traceback" not in err


def test_the_demtype_help_does_not_promise_higher_resolution():
    # The claim this task was originally justified with, and which turned
    # out to be wrong: OpenTopography's global API serves nothing finer
    # than 30 m anywhere, so nothing offered here is sharper than the
    # default. Read off the parser rather than out of --help's rendered
    # output, which argparse wraps at the terminal width and would break
    # this phrase across a line.
    parser = build_parser()
    survey = parser._subparsers._group_actions[0].choices["survey"]
    action = next(a for a in survey._actions if "--demtype" in a.option_strings)
    assert "higher resolution than COP30" in action.help
    assert "COP30" in action.help


# --- Task 29: `mapgen bridge <package-dir>`. The refusals go through the
# real bridge_package, because a refusal never starts a process and is
# exactly what must not arrive as a traceback. The two summary tests fake
# bridge_package the way the survey summary tests above fake run_survey,
# so they exercise command_bridge's own branching without needing dotnet
# or Urbano to be present. ------------------------------------------------


def test_bridge_takes_a_package_directory_and_nothing_else():
    parser = build_parser()
    args = parser.parse_args(["bridge", r"C:\Surveys\South-Wales\2026-08-01_Barry"])
    assert args.package_dir == Path(r"C:\Surveys\South-Wales\2026-08-01_Barry")
    assert args.func is cli.command_bridge


def test_bridge_needs_a_package_directory():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["bridge"])


def test_bridge_reports_a_folder_that_is_not_there_without_a_traceback(tmp_path, capsys):
    exit_code = main(["bridge", str(tmp_path / "not-a-package")])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "not-a-package" in err
    assert "Traceback" not in err
    assert err.strip().count("\n") == 0, "a refusal is one plain line"


def test_bridge_reports_a_package_with_no_survey_json_without_a_traceback(tmp_path, capsys):
    root = tmp_path / "2026-08-01_Barry"
    root.mkdir()
    exit_code = main(["bridge", str(root)])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "survey.json" in err
    assert "resume" in err
    assert "Traceback" not in err


def test_bridge_names_the_project_setting_when_it_succeeds(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "mapgen.cli.bridge_package",
        lambda *a, **k: {
            "urbano_stem": "Barry-Waterfront_2026-08-01",
            "bridge": {"attempted": True, "ok": True, "error": None, "ran_at": "2026-08-04T10:00:00Z"},
        },
    )
    exit_code = main(["bridge", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Urbano project setting: Barry-Waterfront_2026-08-01_project_setting.json" in captured.out
    assert captured.err == ""


def test_bridge_exits_1_and_says_why_when_the_bridge_ran_and_failed(tmp_path, capsys, monkeypatch):
    # The owner's normal case today, and the reason this exits 1 where
    # `mapgen survey` exits 0 for the same failure: a survey that cannot
    # bridge still delivered its data, and this command was asked for
    # nothing else.
    error = (
        "Urbano is not installed: no Urbano.Core.dll or ProjectSetup.dll was found "
        "under C:\\Users\\Param\\AppData\\Roaming\\McNeel\\Rhinoceros\\packages\\8.0\\Urbano2. "
        "Urbano is optional; the rest of the package does not need it."
    )
    monkeypatch.setattr(
        "mapgen.cli.bridge_package",
        lambda *a, **k: {
            "urbano_stem": "Barry-Waterfront_2026-08-01",
            "bridge": {"attempted": True, "ok": False, "error": error, "ran_at": "2026-08-04T10:00:00Z"},
        },
    )
    exit_code = main(["bridge", str(tmp_path)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "not produced" in captured.out
    assert "Barry-Waterfront_2026-08-01_project_setting.json" not in captured.out
    assert error in captured.err
    assert "Traceback" not in captured.err


# --- Task 30: the command line is the third place a failure has to show ---
#
# "the progress log, so the owner sees it while watching; survey.json, so
# the package explains itself later; the command line summary, so a
# scripted run is not silent about it". The first two are asserted in
# test_package.py; these are the same records, through the real command.


class _CollectsFailuresSource:
    """A source shaped like the tiled ones: it reports which tiles failed
    and why, through the tile_failures convention, and raises at the end
    of its loop rather than on the first bad tile.

    Deliberately NOT a source that merely raises. That shape is already
    covered above and takes a different path through run_survey entirely;
    what these tests are about is the account a tiled source produces,
    which is the thing the terminal has to print.
    """

    id = "collects-failures"
    display_name = "Collects Failures"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self, fail_on=("r00_c01",)):
        from mapgen.sources.base import TileFailure

        self._fail_on = set(fail_on)
        self._TileFailure = TileFailure
        self.tile_failures = []

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        self.tile_failures = []
        paths = []
        for tile in tiles:
            if tile.tile_id in self._fail_on:
                self.tile_failures.append(
                    self._TileFailure(
                        source=self.id,
                        tile_id=tile.tile_id,
                        kind="not_authorised",
                        reason="the service refused the request as not allowed (HTTP 401).",
                    )
                )
                continue
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        if self.tile_failures:
            raise RuntimeError("collects-failures: 1 tile failed")
        return paths

    def merge(self, parts, out_dir, stem):
        # Returns no file when there was nothing to merge, like every real
        # source does since Task 30. A double that wrote a file regardless
        # would make the "found nothing" line unreachable for a failed
        # layer for the wrong reason, and a test asserting it is never
        # printed would then be asserting nothing at all.
        self.merged_features = len(parts)
        if not parts:
            return []
        out = out_dir / "collects-failures.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


class _FindsNothingSource:
    """Fetches every tile successfully and merges to nothing, which is
    what an OSM run over open sea does."""

    id = "finds-nothing"
    display_name = "Finds Nothing"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def __init__(self):
        self.merged_features = None

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=100, seconds_estimate=1.0)

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        return paths

    def merge(self, parts, out_dir, stem):
        self.merged_features = 0
        return []


def test_a_forced_run_names_every_tile_it_could_not_get(tmp_path, capsys):
    register(_CollectsFailuresSource())
    exit_code = main(
        _survey_error_case_args(
            tmp_path, "collects-failures", tile_size_m=600, overlap_m=50
        )
        + ["--force"]
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "1 tile did not download:" in captured.err
    assert "collects-failures r00_c01" in captured.err
    assert "HTTP 401" in captured.err
    assert "Traceback" not in captured.err


def test_an_unforced_run_says_the_same_thing_through_its_own_error(tmp_path, capsys):
    # Same records, same composer, different route to the terminal: the
    # forced run prints them, the unforced one carries them out in the
    # exception main() turns into plain lines.
    register(_CollectsFailuresSource())
    exit_code = main(
        _survey_error_case_args(
            tmp_path, "collects-failures", tile_size_m=600, overlap_m=50
        )
    )
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "1 tile did not download:" in captured.err
    assert "collects-failures r00_c01" in captured.err
    assert "HTTP 401" in captured.err
    assert "--force" in captured.err
    assert "Traceback" not in captured.err


def test_a_layer_that_found_nothing_says_so_rather_than_leaving_a_gap(tmp_path, capsys):
    # No file is written for it, by ruling, so without this line the owner
    # opens the folder and finds something missing with nothing on screen
    # accounting for it.
    register(_FindsNothingSource())
    exit_code = main(_survey_error_case_args(tmp_path, "finds-nothing"))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "finds-nothing: nothing was found in this extent" in captured.out
    assert "did not download" not in captured.err


def test_a_layer_that_failed_is_never_described_as_having_found_nothing(tmp_path, capsys):
    # The two must not collapse into one sentence. This source merges no
    # file either, but for the opposite reason.
    register(_CollectsFailuresSource(fail_on=("r00_c00", "r00_c01", "r01_c00", "r01_c01")))
    main(
        _survey_error_case_args(
            tmp_path, "collects-failures", tile_size_m=600, overlap_m=50
        )
        + ["--force"]
    )
    captured = capsys.readouterr()
    assert "nothing was found in this extent" not in captured.out
    assert "did not download" in captured.err


def test_an_ordinary_complete_run_says_nothing_new_at_all(tmp_path, capsys):
    # StubSource is registered by the autouse fixture above.
    exit_code = main(_survey_error_case_args(tmp_path, "stub"))
    assert exit_code == 0
    captured = capsys.readouterr()
    assert "did not download" not in captured.err
    assert "nothing was found" not in captured.out


# --- Task 32: the terminal says when a run only completed on a retry ------


class _RecoversOnRetrySource(_CollectsFailuresSource):
    """Fails a named tile a FIXED number of times, with a retryable kind,
    then serves it.

    A double that fails forever tests the give-up path and nothing else,
    and one that never fails tests neither. This one is told how many
    refusals to give, so the same class covers a run that recovers and a
    run that does not.
    """

    id = "recovers-on-retry"
    display_name = "Recovers On Retry"

    def __init__(self, fail_on=("r00_c01",), failures=1):
        super().__init__(fail_on=fail_on)
        self._failures_left = {tile_id: failures for tile_id in fail_on}

    def fetch(self, bbox, tiles, work_dir, progress):
        self.tile_failures = []
        paths = []
        for tile in tiles:
            if self._failures_left.get(tile.tile_id, 0) > 0:
                self._failures_left[tile.tile_id] -= 1
                self.tile_failures.append(
                    self._TileFailure(
                        source=self.id,
                        tile_id=tile.tile_id,
                        kind="service_error",
                        reason="the service answered HTTP 503.",
                    )
                )
                continue
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            paths.append(path)
        if self.tile_failures:
            raise RuntimeError("recovers-on-retry: a tile failed")
        return paths

    def merge(self, parts, out_dir, stem):
        self.merged_features = len(parts)
        if not parts:
            return []
        out = out_dir / "recovers-on-retry.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("merged", encoding="utf-8")
        return [out]


def test_a_run_that_only_completed_on_a_retry_says_so_on_the_terminal(tmp_path, capsys):
    # The complete run that was not complete first time. Without this line
    # it reads on screen exactly like one that never stumbled, because a
    # recovered tile is correctly dropped from tile_failures.
    register(_RecoversOnRetrySource())
    exit_code = main(
        _survey_error_case_args(
            tmp_path, "recovers-on-retry", tile_size_m=600, overlap_m=50
        )
    )
    assert exit_code == 0, "a run whose retry worked is a complete run"
    captured = capsys.readouterr()
    assert "1 tile arrived only on a retry (recovers-on-retry)" in captured.err
    assert "did not download" not in captured.err
    assert "Traceback" not in captured.err


def test_a_clean_run_says_nothing_about_retries(tmp_path, capsys):
    register(_RecoversOnRetrySource(failures=0))
    exit_code = main(
        _survey_error_case_args(
            tmp_path, "recovers-on-retry", tile_size_m=600, overlap_m=50
        )
    )
    assert exit_code == 0
    assert "arrived only on a retry" not in capsys.readouterr().err


def test_a_whole_layer_failure_is_one_terminal_line_not_one_per_tile(
    tmp_path, capsys
):
    # Elevation and Overture record one whole-extent failure against every
    # planned tile, so the owner's own extent would otherwise print
    # seventy-two copies of one sentence.
    register(
        _CollectsFailuresSource(fail_on=("r00_c00", "r00_c01", "r01_c00", "r01_c01"))
    )
    main(
        _survey_error_case_args(
            tmp_path, "collects-failures", tile_size_m=600, overlap_m=50
        )
        + ["--force"]
    )
    captured = capsys.readouterr()
    assert "collects-failures, all 4 tiles:" in captured.err
    assert "HTTP 401" in captured.err
    # The four per-tile lines are gone, not merely joined by a summary.
    assert "collects-failures r00_c00" not in captured.err
