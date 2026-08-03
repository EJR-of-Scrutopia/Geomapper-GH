import json
import subprocess
import sys

import pytest

from mapgen.cli import build_parser, main
from mapgen.sources.base import Estimate, clear_registry, register


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
