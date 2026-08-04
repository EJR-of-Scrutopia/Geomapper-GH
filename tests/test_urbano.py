"""The Urbano project setting file mapgen now writes itself.

The reference throughout is `docs/urbano/sample_project_setting.json`, which
Urbano's own serialiser produced (task 34), and the decompiled behaviour of
`Urbano.Grasshopper.ProjectSettingComponent` and
`Urbano.Core.Data.UrbanoProjectSetting.TryLoad`, which are what actually read
the file.
"""

import json
import math
from pathlib import Path

import pytest

from mapgen.geo import BBox
from mapgen.urbano import (
    DEFAULT_GRANULARITY,
    LAYER_ORDER,
    PROJECT_SETTING_SUFFIX,
    ProjectSettingError,
    build_project_setting,
    describe_layers,
    project_setting_path,
    resolve_data_files,
    world_origin,
    write_project_setting,
)

SAMPLE = (
    Path(__file__).resolve().parents[1] / "docs" / "urbano" / "sample_project_setting.json"
)

# The owner's own ground. Barry Waterfront, which is the extent the README's
# worked example and the real survey both use.
BARRY = BBox(west=-3.2900, south=51.3800, east=-3.2400, north=51.4000)
STEM = "Barry-Waterfront_2026-08-03"


def package(root: Path, *names: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name in names:
        (root / name).write_text("data", encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# The coordinate reference.
# --------------------------------------------------------------------------


def test_the_london_sample_is_reproduced_field_for_field():
    """The whole coordinate reference block, against the one Urbano wrote.

    utm.py's own tests check the projection; this checks that the block is
    assembled the way Urbano assembles it, which is a separate mistake to
    make: the two corners are the bottom left and the bottom RIGHT, both at
    the southern edge, and swapping in a northern latitude or an eastern
    corner produces numbers that look entirely reasonable and are wrong.
    """
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    bbox = BBox(
        west=sample["Left"],
        south=sample["Bottom"],
        east=sample["Right"],
        north=sample["Top"],
    )
    reference = world_origin(bbox)
    expected = sample["CoordinateReference"]

    assert reference["Utm"] == expected["Utm"]
    for corner in ("BottomLeftUtm", "BottomRightUtm"):
        assert reference[corner]["Item1"] == pytest.approx(expected[corner]["Item1"], abs=1e-9)
        assert reference[corner]["Item2"] == pytest.approx(expected[corner]["Item2"], abs=1e-9)


def test_both_corners_share_one_zone_when_the_extent_crosses_a_boundary():
    """Urbano projects each corner into its own zone and keeps the right
    corner's zone string, which for an extent crossing the prime meridian
    leaves the two eastings measured from different central meridians while
    naming only one of them. mapgen puts both on the zone Urbano's own last
    assignment keeps, so the pair is consistent with the zone downstream
    components project everything else with.

    A London extent from Chelsea to Greenwich crosses the 30/31 boundary at
    longitude 0, so this is a real case and not a contrived one.
    """
    reference = world_origin(BBox(west=-0.2, south=51.47, east=0.05, north=51.52))
    assert reference["Utm"] == "31U"
    left = reference["BottomLeftUtm"]["Item1"]
    right = reference["BottomRightUtm"]["Item1"]
    # A quarter of a degree of longitude at this latitude is about 17 km.
    # Faithfully reproducing Urbano would have put these 400 km apart.
    assert 15000.0 < right - left < 20000.0


def test_the_two_corners_are_the_south_edge_not_the_north():
    """`WorldOrigin(left, bottom, right)` takes one latitude, the bottom
    one, for both corners. A northing computed from `north` would be a few
    hundred metres out and would look perfectly plausible.
    """
    reference = world_origin(BARRY)
    assert reference["BottomLeftUtm"]["Item2"] == pytest.approx(
        reference["BottomRightUtm"]["Item2"], abs=100.0
    )
    south_only = world_origin(BBox(west=-3.29, south=51.38, east=-3.24, north=51.99))
    assert south_only["BottomLeftUtm"]["Item2"] == pytest.approx(
        reference["BottomLeftUtm"]["Item2"], abs=1e-9
    )


# --------------------------------------------------------------------------
# What the file says about the package.
# --------------------------------------------------------------------------


def test_a_real_welsh_package_produces_every_field_urbano_reads(tmp_path):
    root = package(
        tmp_path / "2026-08-03_Barry-Waterfront",
        f"{STEM}.osm",
        f"{STEM}.tif",
        f"{STEM}_building.geojson",
        f"{STEM}_water.geojson",
    )
    setting = build_project_setting(BARRY, root, STEM)

    assert list(setting) == [
        "Folder", "FileNameStr", "Granularity", "CoordinateReference",
        "Top", "Bottom", "Left", "Right",
        "OsmFilePath", "BlockFilePath", "ElevationFilePath", "OvertureFilePath",
        "Layers",
    ]
    assert setting["Folder"] == str(root)
    assert setting["FileNameStr"] == STEM
    assert setting["Granularity"] == "Block"
    assert setting["Top"] == 51.40
    assert setting["Bottom"] == 51.38
    assert setting["Left"] == -3.29
    assert setting["Right"] == -3.24
    assert setting["OsmFilePath"] == str(root / f"{STEM}.osm")
    assert setting["BlockFilePath"] == ""
    assert setting["ElevationFilePath"] == str(root / f"{STEM}.tif")
    assert setting["OvertureFilePath"] == str(root / f"{STEM}_building.geojson")
    assert setting["Layers"] == ["osm", "elevation", "overture"]
    assert setting["CoordinateReference"]["Utm"] == "30U"


def test_file_name_str_is_mapgens_stem_not_a_coordinate_string(tmp_path):
    """The single most consequential field, and the one the reference sample
    is misleading about.

    Urbano rebuilds every data path as Folder + FileNameStr + its own fixed
    extension and skips what is already there, so the stem is what makes a
    mapgen package a no-download pass-through. Urbano's own convention for
    this field is `top_bottom_right_left`, and nothing checks the two against
    each other: `TryLoad` matches on the bound string rebuilt from Top,
    Bottom, Right and Left, never on FileNameStr.
    """
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    setting = build_project_setting(BARRY, root, STEM)
    assert setting["FileNameStr"] == STEM
    assert "51.4" not in setting["FileNameStr"]
    # And the path Urbano would rebuild from it is the file that is there.
    rebuilt = Path(setting["Folder"]) / f"{setting['FileNameStr']}.osm"
    assert rebuilt.is_file()


def test_only_the_layers_the_package_actually_holds_are_named(tmp_path):
    """Naming a layer commits Urbano to having it: an elevation layer with
    no file sends it to a United States only elevation service. Every layer
    listed here has to be one the folder can answer for.
    """
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    setting = build_project_setting(BARRY, root, STEM)
    assert setting["Layers"] == ["osm"]
    assert setting["ElevationFilePath"] == ""
    assert setting["OvertureFilePath"] == ""
    assert setting["BlockFilePath"] == ""


def test_layers_are_listed_in_urbanos_own_order(tmp_path):
    root = package(
        tmp_path / "pkg",
        f"{STEM}_water.geojson",
        f"{STEM}.tif",
        f"{STEM}.osm",
        f"{STEM}.blocks",
    )
    setting = build_project_setting(BARRY, root, STEM)
    assert setting["Layers"] == list(LAYER_ORDER)


def test_climate_is_never_listed(tmp_path):
    """The one layer Urbano re-fetches with no file check at all, from a
    United States only EPW lookup. mapgen has nothing to put in it and
    naming it could only ever cost a failed download.
    """
    root = package(tmp_path / "pkg", f"{STEM}.osm", f"{STEM}.tif")
    setting = build_project_setting(BARRY, root, STEM)
    assert "climate" not in setting["Layers"]


def test_urbanos_own_native_formats_win_over_mapgens_when_both_are_there(tmp_path):
    """If the bridge ever does succeed it leaves a `.osm.pbf`, a `.egrid`
    and a `.parquet`, which are the formats Urbano reads without being
    asked. Preferring them is what stops this writer being a downgrade of a
    successful bridge run.

    OSM is the exception and follows Urbano's own preference order, which
    checks `.osm` before `.osm.pbf`.
    """
    root = package(
        tmp_path / "pkg",
        f"{STEM}.osm", f"{STEM}.osm.pbf",
        f"{STEM}.tif", f"{STEM}.egrid",
        f"{STEM}_building.geojson", f"{STEM}_overture.parquet",
    )
    setting = build_project_setting(BARRY, root, STEM)
    assert setting["OsmFilePath"].endswith(f"{STEM}.osm")
    assert setting["ElevationFilePath"].endswith(".egrid")
    assert setting["OvertureFilePath"].endswith(".parquet")


def test_a_pbf_only_package_is_named_as_a_pbf(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm.pbf")
    setting = build_project_setting(BARRY, root, STEM)
    assert setting["OsmFilePath"].endswith(".osm.pbf")
    assert setting["Layers"] == ["osm"]


def test_overture_falls_back_to_whatever_type_the_package_holds(tmp_path):
    """The field takes one string and a package holds one file per Overture
    type. Buildings are preferred because building heights are the one thing
    Urbano is known to read Overture for; a package without them still names
    a real file rather than nothing.
    """
    root = package(tmp_path / "pkg", f"{STEM}.osm", f"{STEM}_water.geojson",
                   f"{STEM}_land_use.geojson")
    setting = build_project_setting(BARRY, root, STEM)
    assert setting["OvertureFilePath"].endswith("_land_use.geojson")
    assert "overture" in setting["Layers"]


def test_an_empty_directory_is_refused_rather_than_described(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    with pytest.raises(ProjectSettingError, match="would name nothing"):
        build_project_setting(BARRY, root, STEM)


def test_another_surveys_files_are_not_claimed(tmp_path):
    """Everything is matched on this package's own stem. A folder holding a
    different survey's files has nothing for this one.
    """
    root = package(tmp_path / "pkg", "Someone-Else_2026-01-01.osm",
                   "Someone-Else_2026-01-01_building.geojson")
    with pytest.raises(ProjectSettingError, match="would name nothing"):
        build_project_setting(BARRY, root, STEM)


def test_a_directory_is_not_mistaken_for_a_data_file(tmp_path):
    root = tmp_path / "pkg"
    (root / f"{STEM}.osm").mkdir(parents=True)
    with pytest.raises(ProjectSettingError, match="would name nothing"):
        build_project_setting(BARRY, root, STEM)


def test_an_unknown_granularity_is_refused(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    with pytest.raises(ProjectSettingError, match="not an Urbano granularity"):
        build_project_setting(BARRY, root, STEM, granularity="Parish")


@pytest.mark.parametrize("granularity", ["Block", "Block Group", "Tract"])
def test_urbanos_three_granularities_are_accepted(tmp_path, granularity):
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    setting = build_project_setting(BARRY, root, STEM, granularity=granularity)
    assert setting["Granularity"] == granularity


def test_resolve_data_files_reports_paths_by_layer(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm", f"{STEM}.tif")
    files = resolve_data_files(root, STEM)
    assert set(files) == {"osm", "elevation"}
    assert files["osm"].name == f"{STEM}.osm"


# --------------------------------------------------------------------------
# Never a NaN, never a half-written file.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_an_unreal_bound_is_refused_rather_than_written(tmp_path, bad):
    """Task 34's trap. Urbano's `ToJson` writes named float literals happily
    and both of its readers use default JsonSerializer options, which reject
    them, so one NaN anywhere breaks the file for the software it is for.
    """
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    with pytest.raises(ProjectSettingError):
        build_project_setting(
            BBox(west=-3.29, south=51.38, east=-3.24, north=bad), root, STEM
        )


def test_an_extent_outside_the_utm_band_table_is_refused_not_projected(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    with pytest.raises(ProjectSettingError, match="coordinate reference could not"):
        build_project_setting(
            BBox(west=-3.29, south=81.0, east=-3.24, north=81.5), root, STEM
        )


def test_the_written_file_holds_no_named_float_literal(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm", f"{STEM}.tif")
    written = write_project_setting(BARRY, root, STEM)
    text = written.read_text(encoding="utf-8")
    assert "NaN" not in text
    assert "Infinity" not in text
    # And it parses under a reader that refuses them, which is what Urbano's
    # own readers are.
    reparsed = json.loads(text, parse_constant=_refuse)
    assert reparsed["FileNameStr"] == STEM


def _refuse(name):
    raise AssertionError(f"the file carried a named float literal: {name}")


def test_a_nan_that_slipped_past_every_earlier_guard_still_never_reaches_disk(
    tmp_path, monkeypatch
):
    """The last guard, tested by defeating the ones in front of it.

    Nothing reachable can put a NaN in a coordinate reference today: the
    projection refuses one and the bounds are checked separately. That is
    exactly why this has to be tested by force. `json.dumps(allow_nan=False)`
    is the line between "mapgen has a bug" and "the owner has a package that
    looks finished and breaks in Grasshopper", and a guard nothing exercises
    is a guard nobody notices the removal of.
    """
    import mapgen.urbano as urbano

    root = package(tmp_path / "pkg", f"{STEM}.osm")
    monkeypatch.setattr(
        urbano,
        "world_origin",
        lambda bbox: {
            "Utm": "30U",
            "BottomLeftUtm": {"Item1": float("nan"), "Item2": 0.0},
            "BottomRightUtm": {"Item1": 0.0, "Item2": 0.0},
        },
    )
    with pytest.raises(ProjectSettingError, match="Urbano cannot read"):
        urbano.write_project_setting(BARRY, root, STEM)
    assert not (root / f"{STEM}{PROJECT_SETTING_SUFFIX}").exists()
    assert not list(root.glob("*.part"))


def test_every_number_in_the_file_is_finite(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    written = write_project_setting(BARRY, root, STEM)
    payload = json.loads(written.read_text(encoding="utf-8"))
    numbers = [payload["Top"], payload["Bottom"], payload["Left"], payload["Right"]]
    for corner in ("BottomLeftUtm", "BottomRightUtm"):
        numbers.extend(payload["CoordinateReference"][corner].values())
    assert all(math.isfinite(value) for value in numbers)


# --------------------------------------------------------------------------
# Writing it.
# --------------------------------------------------------------------------


def test_the_file_is_named_the_one_thing_urbano_looks_for(tmp_path):
    """`*_project_setting.json`, established by black-box experiment against
    Urbano's own TryLoad: any prefix, but the separating underscore is
    required, so `project_setting.json` does not match and this does.
    """
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    written = write_project_setting(BARRY, root, STEM)
    assert written.name == f"{STEM}_project_setting.json"
    assert written.name.endswith(PROJECT_SETTING_SUFFIX)
    assert written == project_setting_path(root, STEM)
    assert written.parent == root


def test_writing_twice_replaces_rather_than_appends(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    write_project_setting(BARRY, root, STEM)
    package(root, f"{STEM}.tif")
    written = write_project_setting(BARRY, root, STEM)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["Layers"] == ["osm", "elevation"]


def test_no_temporary_file_is_left_beside_it(tmp_path):
    root = package(tmp_path / "pkg", f"{STEM}.osm")
    write_project_setting(BARRY, root, STEM)
    assert not list(root.glob("*.part"))


def test_the_folder_field_is_absolute_even_for_a_relative_root(tmp_path, monkeypatch):
    """Urbano rebuilds every path from Folder. A relative one would resolve
    against whatever directory Rhino happens to be running in.
    """
    package(tmp_path / "pkg", f"{STEM}.osm")
    monkeypatch.chdir(tmp_path)
    setting = build_project_setting(BARRY, Path("pkg"), STEM)
    assert Path(setting["Folder"]).is_absolute()
    assert Path(setting["OsmFilePath"]).is_absolute()


def test_nothing_is_written_when_there_is_nothing_to_describe(tmp_path):
    root = tmp_path / "pkg"
    root.mkdir()
    with pytest.raises(ProjectSettingError):
        write_project_setting(BARRY, root, STEM)
    assert not list(root.iterdir())


# --------------------------------------------------------------------------
# The sentence the terminal says.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "layers,expected",
    [
        ([], "no layers"),
        (["osm"], "osm"),
        (["osm", "elevation"], "osm and elevation"),
        (["osm", "elevation", "overture"], "osm, elevation and overture"),
    ],
)
def test_describe_layers_reads_as_a_sentence(layers, expected):
    assert describe_layers(layers) == expected


def test_the_default_granularity_is_urbanos_own_default():
    assert DEFAULT_GRANULARITY == "Block"
