import json

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

    def merge(self, parts, out_dir):
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

    def merge(self, parts, out_dir):
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
