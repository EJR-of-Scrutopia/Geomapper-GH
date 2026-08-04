"""Excluded from the default run. Invoke with: pytest -m live"""

import json
import locale
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mapgen.geo import BBox, Tile
from mapgen.package import SurveyRequest, register_default_sources, run_survey
from mapgen.sources.base import NullProgress
from mapgen.sources.overture import OvertureSource


@pytest.mark.live
def test_a_small_welsh_extent_downloads_end_to_end(tmp_path):
    register_default_sources()
    request = SurveyRequest(
        bbox=BBox.parse("-3.29,51.38,-3.285,51.385"),
        region="South Wales",
        site="Smoke Test",
        output_root=tmp_path,
        tile_size_m=1000.0,
        overlap_m=50.0,
        source_ids=("osm",),
        run_bridge_step=False,
    )
    result = run_survey(request)
    assert result.complete is True
    # The merged OSM file is named after the package stem, not a bare
    # "all.osm" (see OsmSource.merge and Task 20 finding 2): the brief's
    # draft of this test predated that rename and would fail here on a
    # file that no longer exists under that name.
    assert (result.paths.root / f"{result.paths.stem}.osm").stat().st_size > 0
    assert result.paths.survey_json.exists()


# --- overturemaps 0.20.0 specifically ----------------------------------
#
# The unit tests for both 0.20.0 defects drive a stand-in built from what
# 0.20.0 was observed doing (see OvertureCli0200 in
# test_sources_overture.py). A stand-in can only ever be as good as the
# observation behind it, and this project's recurring defect is a test that
# passes because a stub was kinder than the real thing. These two run the
# real 0.20.0 executable against the real release so that claim is checked
# rather than trusted.
#
# 0.20.0 is deliberately located by path rather than by shutil.which. On
# this machine PATH resolves 0.19.0 from C:\Python313\Scripts, which has
# neither defect, so a test that took whatever which() found would pass
# without exercising either one.

_VENV_OVERTUREMAPS = Path(sys.executable).parent / "overturemaps.exe"

# A Barry extent whose `place` features carry Welsh names containing the
# y-circumflex, which has no cp1252 mapping and is what kills 0.20.0
# without PYTHONUTF8. This exact extent was verified to reproduce the crash
# (it stops at 337,454 of 1,056,802 bytes, failing at position 443); a
# smaller Barry extent tried first did not contain such a name at all and
# so proved nothing, which is what
# test_overturemaps_0200_really_does_fail_without_the_utf8_fix below exists
# to keep catching.
_WELSH_BBOX = "-3.2830,51.4000,-3.2530,51.4180"


def _overturemaps_version(executable):
    result = subprocess.run(
        [str(executable), "--version"], capture_output=True, text=True
    )
    return (result.stdout or "").strip()


def _require_0200():
    if not _VENV_OVERTUREMAPS.exists():
        pytest.skip(f"no overturemaps executable at {_VENV_OVERTUREMAPS}")
    version = _overturemaps_version(_VENV_OVERTUREMAPS)
    if "0.20." not in version:
        pytest.skip(
            f"these cover overturemaps 0.20.x specifically; {_VENV_OVERTUREMAPS} "
            f"reports {version!r}"
        )
    return _VENV_OVERTUREMAPS


@pytest.mark.live
def test_overturemaps_0200_survives_a_welsh_name_and_leaves_no_sidecar(tmp_path):
    executable = _require_0200()
    source = OvertureSource(
        types=["place"], executable_finder=lambda _name: str(executable)
    )
    bbox = BBox.parse(_WELSH_BBOX)
    tile = Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)

    source.fetch(bbox, [tile], tmp_path, NullProgress())

    downloaded = tmp_path / "place.geojson"
    text = downloaded.read_text(encoding="utf-8")
    # Finding A: without PYTHONUTF8 in the child environment this download
    # dies part way through with UnicodeEncodeError on U+0177 and leaves a
    # truncated file. Asserting on the character itself, not just on the
    # exit code, because a truncated file is still a file.
    assert "\u0177" in text, (
        "no y-circumflex in a download that reported success. If "
        "test_overturemaps_0200_really_does_fail_without_the_utf8_fix passes, "
        "this extent does contain one and this file is silently truncated. If "
        "that test skipped or failed too, the extent itself has changed and "
        "needs replacing rather than this being a real regression"
    )
    assert json.loads(text)["features"], "no features came back at all"

    # Finding B: 0.20.0 writes a .state sidecar beside whatever --output
    # names, and _download points that at a .part path.
    assert list(tmp_path.rglob("*.state")) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["place.geojson"]


@pytest.mark.live
def test_overturemaps_0200_really_does_fail_without_the_utf8_fix(tmp_path):
    # The other half, and the one that keeps the unit tests honest: prove
    # the defect is still there in the CLI, so the test above is passing
    # because of the fix rather than because 0.20.0 quietly stopped caring.
    # Runs the executable directly with PYTHONUTF8 stripped, which is the
    # environment a child inherited before procutil.child_environment.
    executable = _require_0200()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PYTHONUTF8", "PYTHONIOENCODING")
    }
    target = tmp_path / "place.geojson"
    result = subprocess.run(
        [
            str(executable), "download", f"--bbox={_WELSH_BBOX}",
            "-f", "geojson", "--type", "place", "--output", str(target),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=environment,
    )
    if locale.getpreferredencoding(False).lower().replace("-", "") == "utf8":
        pytest.skip(
            "this machine's locale is already UTF-8, so the defect cannot be "
            "reproduced here; it needs a cp1252-style ANSI codepage"
        )
    assert result.returncode != 0, (
        "overturemaps 0.20.0 completed a Welsh download without UTF-8 mode, so "
        "the defect procutil.child_environment works around may be fixed "
        "upstream; re-check the pin in pyproject.toml before relying on this"
    )
    assert "UnicodeEncodeError" in (result.stderr or "")
