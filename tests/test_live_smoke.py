"""Excluded from the default run. Invoke with: pytest -m live"""

import json
import locale
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from mapgen.config import load_config
from mapgen.geo import BBox, Tile
from mapgen.package import SurveyRequest, register_default_sources, run_survey
from mapgen.sources.base import NullProgress
from mapgen.sources.elevation import MissingApiKeyError, resolve_api_key
from mapgen.sources.overture import OvertureSource


# A Barry extent with real streets and buildings in it, verified against
# the live map API before it was chosen: 3,364 nodes, 595 ways and 17
# relations in 1.1 MB.
#
# It replaces -3.29,51.38,-3.285,51.385 (Task 30), which is off the coast
# and contains NOTHING. The map API answers that extent 200 with a 385-byte
# document holding a <bounds> element and no features at all, which this
# test never noticed, because mapgen used to write the merged XML envelope
# regardless and the assertion below was on a file being non-empty. What it
# actually proved for its whole life was that an 85-byte envelope gets
# written for empty water. Now that no file is written for an extent with
# nothing in it, that extent has a test of its own, below, saying so on
# purpose.
_POPULATED_BBOX = "-3.285,51.395,-3.280,51.400"

# The empty one, kept deliberately. It is a real place, it really does come
# back empty, and it is the case the ruling is about.
_EMPTY_BBOX = "-3.29,51.38,-3.285,51.385"


def _smoke_request(tmp_path, bbox, site):
    return SurveyRequest(
        bbox=BBox.parse(bbox),
        region="South Wales",
        site=site,
        output_root=tmp_path,
        tile_size_m=1000.0,
        overlap_m=50.0,
        source_ids=("osm",),
        run_bridge_step=False,
    )


@pytest.mark.live
def test_a_small_welsh_extent_downloads_end_to_end(tmp_path):
    register_default_sources()
    result = run_survey(_smoke_request(tmp_path, _POPULATED_BBOX, "Smoke Test"))
    assert result.complete is True
    # The merged OSM file is named after the package stem, not a bare
    # "all.osm" (see OsmSource.merge and Task 20 finding 2): the brief's
    # draft of this test predated that rename and would fail here on a
    # file that no longer exists under that name.
    merged = result.paths.root / f"{result.paths.stem}.osm"
    assert merged.stat().st_size > 0
    # Real elements, not merely a non-empty file. Size alone was what let
    # this test pass for years on an envelope with nothing in it.
    root = ET.parse(merged).getroot()
    assert sum(1 for element in root if element.tag == "node") > 100
    assert any(element.tag == "way" for element in root)
    entry = next(s for s in result.survey["sources"] if s["id"] == "osm")
    assert entry["features_merged"] > 100
    assert entry["merged_files"] == [merged.name]
    assert result.survey["tile_failures"] == []
    assert result.paths.survey_json.exists()


@pytest.mark.live
def test_a_real_extent_with_nothing_in_it_is_a_success_with_no_file(tmp_path):
    # Task 30's ruling, against the real API rather than a fixture: this
    # extent is open water, the map API answers 200 with no features, and
    # the honest package is one that says so. Complete, every tile ok, no
    # merged file, and nothing reported as a failure.
    register_default_sources()
    result = run_survey(_smoke_request(tmp_path, _EMPTY_BBOX, "Empty Water"))

    assert result.complete is True
    assert all(record["osm"] == "ok" for record in result.survey["tiles"])
    assert not (result.paths.root / f"{result.paths.stem}.osm").exists()
    entry = next(s for s in result.survey["sources"] if s["id"] == "osm")
    assert entry["merged_files"] == []
    assert entry["features_merged"] == 0
    assert result.survey["tile_failures"] == []


# --- Task 9: the whole Wales LiDAR chain, proven live end to end -------
#
# The same 400 x 400 m Barry extent test_lidar_wales.py's own live test
# already proved has real LiDAR coverage under it (roughly 51.395, -3.27),
# and independently confirmed (a raw map API probe, outside this suite) to
# hold 445 `building=*` ways, so "no fused height anywhere in the file"
# would mean this step broke, not that the extent had nothing to fuse.

_END_TO_END_BBOX = "-3.272,51.393,-3.268,51.397"  # roughly 400 x 400 m, Barry


def _opentopography_key_available() -> bool:
    """Resolved exactly the way ElevationSource itself resolves a key
    (explicit, then environment, then the saved config), never a second,
    looser check: a key this function calls "available" and the source
    then fails to find would be a false positive this test could not
    afford, since the whole point is to run the real chain rather than
    assume it.

    Deliberately reads no value out for logging or assertion, only
    whether resolution succeeds: the key itself is the owner's, kept out
    of this file exactly as resolve_api_key's own callers keep it out of
    survey.json and the tile_failed event.
    """
    try:
        resolve_api_key(None, None, load_config().opentopography_api_key or None)
    except MissingApiKeyError:
        return False
    return True


@pytest.mark.live
def test_the_whole_lidar_wales_chain_proves_itself_over_a_real_barry_extent(tmp_path):
    """Task 9's end-to-end proof: one real `run_survey`, osm + overture +
    lidar_wales, plus elevation if and only if the owner's own
    OpenTopography key is actually configured or set in the environment
    right now. A missing key is not something this test may fabricate or
    hardcode one to work around (the brief's own ruling): it changes which
    sources are selected and which `elevation_grid.source` is honest to
    expect, and both branches are asserted accordingly rather than the
    test silently skipping either way.
    """
    register_default_sources()
    key_available = _opentopography_key_available()
    source_ids = (
        ("osm", "overture", "elevation", "lidar_wales")
        if key_available
        else ("osm", "overture", "lidar_wales")
    )
    request = SurveyRequest(
        bbox=BBox.parse(_END_TO_END_BBOX),
        region="South Wales",
        site="Barry End To End",
        output_root=tmp_path,
        tile_size_m=1000.0,
        overlap_m=50.0,
        source_ids=source_ids,
        run_bridge_step=False,
    )

    started = time.monotonic()
    result = run_survey(request)
    elapsed = time.monotonic() - started

    assert result.complete is True
    root, stem = result.paths.root, result.paths.stem

    # The .osm holds at least one way this run itself fused, not one that
    # already carried a height from OSM: fuse_building_heights only ever
    # writes source:height alongside a height it wrote in the same pass
    # (see mapgen.heights), so the pair together is what tells "fused"
    # apart from "already there".
    osm_path = root / f"{stem}.osm"
    assert osm_path.exists() and osm_path.stat().st_size > 0
    fused_tags = None
    for way in ET.parse(osm_path).getroot().findall("way"):
        tags = {tag.get("k"): tag.get("v") for tag in way.findall("tag")}
        if "height" in tags and "source:height" in tags:
            fused_tags = tags
            break
    assert fused_tags is not None, (
        "no way in the .osm carries a fused height/source:height pair; "
        "either the extent's buildings changed or heights.py regressed"
    )
    assert "LiDAR" in fused_tags["source:height"]

    dtm_path = root / f"{stem}_lidar_dtm.tif"
    dsm_path = root / f"{stem}_lidar_dsm.tif"
    assert dtm_path.exists() and dtm_path.stat().st_size > 0
    assert dsm_path.exists() and dsm_path.stat().st_size > 0

    contour_5m = root / f"{stem}_contours_5m.geojson"
    contour_1m = root / f"{stem}_contours_1m.geojson"
    assert contour_5m.exists() and contour_5m.stat().st_size > 0
    assert contour_1m.exists() and contour_1m.stat().st_size > 0

    egrid_path = root / f"{stem}.egrid"
    assert egrid_path.exists() and egrid_path.stat().st_size > 0
    assert result.survey["elevation_grid"]["written"] is True
    expected_source = "lidar_wales+opentopography" if key_available else "lidar_wales"
    assert result.survey["elevation_grid"]["source"] == expected_source

    lidar_entry = next(s for s in result.survey["sources"] if s["id"] == "lidar_wales")
    assert lidar_entry["licence"] == "Open Government Licence v3.0"
    assert lidar_entry["attribution"] == (
        "Contains Welsh Government and Natural Resources Wales information "
        "licensed under the Open Government Licence v3.0"
    )

    sizes = {
        "osm": osm_path.stat().st_size,
        "lidar_dtm": dtm_path.stat().st_size,
        "lidar_dsm": dsm_path.stat().st_size,
        "contours_5m": contour_5m.stat().st_size,
        "contours_1m": contour_1m.stat().st_size,
        "egrid": egrid_path.stat().st_size,
    }
    print(
        f"\nTask 9 end-to-end proof: {elapsed:.2f}s wall, "
        f"key_available={key_available}, sizes={sizes}"
    )


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
