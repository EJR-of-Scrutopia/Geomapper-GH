import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from mapgen.naming import (
    FINGERPRINT_LENGTH,
    NamingError,
    PackagePaths,
    PathTooLongError,
    build_package_paths,
    check_path_length,
    slugify,
    tiling_fingerprint,
)

# A placeholder fingerprint for tests that only care about path construction,
# not about tiling_fingerprint's own behaviour, which has its own tests below.
FINGERPRINT = "a1b2c3d4"


def test_slugify_hyphenates_spaces():
    assert slugify("Barry Waterfront", "site") == "Barry-Waterfront"


def test_slugify_preserves_case():
    assert slugify("South Wales", "region") == "South-Wales"


def test_slugify_drops_punctuation():
    assert slugify("St. Mary's Quay!", "site") == "St-Marys-Quay"


def test_slugify_transliterates_non_ascii():
    assert slugify("Dŵr Cymru", "region") == "Dwr-Cymru"


def test_slugify_transliterates_hand_coded_table_ø():
    assert slugify("Søren", "site") == "Soren"


def test_slugify_transliterates_hand_coded_table_ß():
    assert slugify("Straße", "site") == "Strasse"


def test_slugify_transliterates_nfkd_decomposable_accents():
    assert slugify("Caerdydd Bâch", "site") == "Caerdydd-Bach"


def test_slugify_collapses_repeated_hyphens():
    assert slugify("Barry  --  Waterfront", "site") == "Barry-Waterfront"


def test_slugify_trims_leading_and_trailing_hyphens():
    assert slugify("  -Barry-  ", "site") == "Barry"


def test_slugify_caps_length_at_forty():
    result = slugify("A" * 60, "site")
    assert len(result) == 40


def test_slugify_does_not_end_on_a_hyphen_after_capping():
    result = slugify("A" * 39 + " Waterfront", "site")
    assert not result.endswith("-")


def test_slugify_rejects_empty_result_naming_the_field():
    with pytest.raises(NamingError, match="site"):
        slugify("!!!", "site")


def test_slugify_rejects_blank_input_naming_the_field():
    with pytest.raises(NamingError, match="region"):
        slugify("   ", "region")


def test_build_package_paths_uses_region_date_site_layout(tmp_path):
    paths = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert paths.root == tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    assert paths.stem == "Barry-Waterfront_2026-08-01"
    assert paths.layers_dir == paths.root / "layers"
    assert paths.work_dir == paths.root / "_work" / FINGERPRINT
    assert paths.survey_json == paths.root / "survey.json"
    assert paths.project_setting == paths.root / "Barry-Waterfront_2026-08-01_project_setting.json"


def test_build_package_paths_appends_02_on_collision(tmp_path):
    # A collision only forces a suffix when the existing folder is a
    # genuinely finished package. See the reuse tests below for the
    # complementary case, where an incomplete folder is reused instead.
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    first.root.mkdir(parents=True)
    first.survey_json.write_text(json.dumps({"complete": True}), encoding="utf-8")
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert second.root.name == "2026-08-01_Barry-Waterfront_02"
    assert second.stem == "Barry-Waterfront_2026-08-01_02"


def test_build_package_paths_appends_03_on_second_collision(tmp_path):
    for _ in range(2):
        paths = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1), FINGERPRINT)
        paths.root.mkdir(parents=True)
        paths.survey_json.write_text(json.dumps({"complete": True}), encoding="utf-8")
    third = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1), FINGERPRINT)
    assert third.root.name == "2026-08-01_Barry_03"


def test_build_package_paths_honours_a_stem_override(tmp_path):
    paths = build_package_paths(
        tmp_path,
        "South Wales",
        "Barry",
        date(2026, 8, 1),
        FINGERPRINT,
        stem_override="51.39_51.38_-3.28_-3.29",
    )
    assert paths.stem == "51.39_51.38_-3.28_-3.29"
    assert paths.root.name == "2026-08-01_Barry"
    assert paths.project_setting.name == "51.39_51.38_-3.28_-3.29_project_setting.json"


def test_build_package_paths_appends_collision_suffix_to_stem_override(tmp_path):
    first = build_package_paths(
        tmp_path,
        "South Wales",
        "Barry",
        date(2026, 8, 1),
        FINGERPRINT,
        stem_override="51.39_51.38",
    )
    first.root.mkdir(parents=True)
    first.survey_json.write_text(json.dumps({"complete": True}), encoding="utf-8")
    second = build_package_paths(
        tmp_path,
        "South Wales",
        "Barry",
        date(2026, 8, 1),
        FINGERPRINT,
        stem_override="51.39_51.38",
    )
    assert second.stem == "51.39_51.38_02"
    assert second.project_setting.name == "51.39_51.38_02_project_setting.json"


def test_build_package_paths_reuses_a_folder_with_no_survey_json(tmp_path):
    # No survey.json at all means no run ever finished here, so it is
    # treated the same as an incomplete one: reused, not suffixed past.
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    first.root.mkdir(parents=True)
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert second.root == first.root
    assert second.stem == first.stem


def test_build_package_paths_reuses_an_incomplete_package(tmp_path):
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    first.root.mkdir(parents=True)
    first.survey_json.write_text(json.dumps({"complete": False}), encoding="utf-8")
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert second.root == first.root
    assert second.stem == first.stem


def test_build_package_paths_reuses_a_folder_with_a_corrupt_survey_json(tmp_path):
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    first.root.mkdir(parents=True)
    first.survey_json.write_text("{not valid json at all", encoding="utf-8")
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert second.root == first.root
    assert second.stem == first.stem


def test_build_package_paths_suffixes_a_genuinely_complete_package(tmp_path):
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    first.root.mkdir(parents=True)
    first.survey_json.write_text(json.dumps({"complete": True}), encoding="utf-8")
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert second.root.name == "2026-08-01_Barry-Waterfront_02"


def test_build_package_paths_can_resume_a_suffixed_folder_too(tmp_path):
    # The rule applies at every step of the collision walk, not just the
    # first: a _02 that is itself incomplete must be reused rather than
    # pushed on to _03.
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    first.root.mkdir(parents=True)
    first.survey_json.write_text(json.dumps({"complete": True}), encoding="utf-8")

    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    second.root.mkdir(parents=True)
    second.survey_json.write_text(json.dumps({"complete": False}), encoding="utf-8")

    third = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT)
    assert third.root == second.root
    assert third.root.name == "2026-08-01_Barry-Waterfront_02"


def test_tiling_fingerprint_has_a_fixed_length():
    fingerprint = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0)
    assert len(fingerprint) == FINGERPRINT_LENGTH


def test_tiling_fingerprint_is_deterministic(tmp_path):
    args = (-3.29, 51.38, -3.28, 51.39, 600.0, 50.0)
    assert tiling_fingerprint(*args) == tiling_fingerprint(*args)


def test_tiling_fingerprint_is_stable_across_processes():
    # hashlib.sha256 over a fixed-format string has no dependency on
    # per-process state (unlike Python's built-in hash(), which is salted
    # per process by default), so two separate interpreter processes given
    # the same six numbers must produce the same 8 characters.
    script = (
        "from mapgen.naming import tiling_fingerprint;"
        "print(tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0))"
    )
    results = {
        subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(results) == 1
    assert len(next(iter(results))) == FINGERPRINT_LENGTH


def test_tiling_fingerprint_differs_for_a_different_tile_size():
    a = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0)
    b = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 1200.0, 50.0)
    assert a != b


def test_tiling_fingerprint_differs_for_a_different_overlap():
    a = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0)
    b = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 75.0)
    assert a != b


def test_tiling_fingerprint_differs_for_a_different_bbox():
    a = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0)
    b = tiling_fingerprint(-3.30, 51.38, -3.28, 51.39, 600.0, 50.0)
    assert a != b


def test_work_dir_includes_the_fingerprint_segment(tmp_path):
    # The redesign's whole point: two different tilings must never resolve
    # to the same work_dir, and the guard below depends on this segment
    # actually being present, not merely intended.
    fp_a = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0)
    fp_b = tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 1200.0, 50.0)
    paths_a = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), fp_a)
    paths_b = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), fp_b)
    assert paths_a.work_dir != paths_b.work_dir
    assert paths_a.work_dir == paths_a.root / "_work" / fp_a


def test_check_path_length_passes_for_a_short_root(tmp_path):
    paths = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1), FINGERPRINT)
    check_path_length(paths, ["building", "infrastructure"])


def test_check_path_length_accounts_for_the_fingerprint_segment(tmp_path):
    # This matters specifically because the guard exists to keep paths well
    # inside the Windows limit: silently under-measuring by the fingerprint
    # segment's length is exactly the failure it was built to prevent.
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    overture_path = paths.work_dir / "raw" / "overture" / "infrastructure" / "r00_c00.geojson"
    actual_length = len(str(overture_path))
    unfingerprinted_length = actual_length - (len(FINGERPRINT) + 1)

    check_path_length(paths, ["infrastructure"], limit=actual_length)
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], limit=unfingerprinted_length)


def test_check_path_length_rejects_a_deep_root():
    deep = Path("C:/") / ("x" * 200)
    paths = build_package_paths(
        deep, "South Wales", "Barry Waterfront", date(2026, 8, 1), FINGERPRINT
    )
    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, ["infrastructure"])
    assert "240" in str(excinfo.value)
    assert "shorter output root" in str(excinfo.value)


def test_check_path_length_rejects_long_site_name_pushing_project_setting_over_limit():
    deep = Path("C:/") / ("x" * 112)
    long_site = "A" * 40
    paths = build_package_paths(deep, "R", long_site, date(2026, 8, 1), FINGERPRINT)

    overture_path = paths.work_dir / "raw" / "overture" / "infrastructure" / "r00_c00.geojson"
    overture_length = len(str(overture_path.resolve() if not overture_path.is_absolute() else overture_path))
    project_setting_length = len(str(paths.project_setting.resolve() if not paths.project_setting.is_absolute() else paths.project_setting))

    assert overture_length <= 240, f"Overture path must be under 240 to test new check: {overture_length}"
    assert project_setting_length > 240, f"Project setting path must be over 240 to isolate the new check: {project_setting_length}"

    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, ["infrastructure"], limit=240)
    assert str(paths.project_setting) in str(excinfo.value)


def test_check_path_length_boundary_is_inclusive(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    probe = len(str(paths.work_dir / "raw" / "overture" / "infrastructure" / "r00_c00.geojson"))
    check_path_length(paths, ["infrastructure"], limit=probe)
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], limit=probe - 1)


# --- Task 20 finding 3: the guard must measure the job that will actually
# run, not every source the tool knows how to fetch. A real --source osm
# run was refused over a path shaped like Overture's, though Overture was
# never selected. ------------------------------------------------------


def test_check_path_length_ignores_overture_when_it_is_not_among_the_selected_sources(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    overture_path = paths.work_dir / "raw" / "overture" / "infrastructure" / "r00_c00.geojson"
    osm_path = paths.work_dir / "raw" / "osm" / "r00_c00.osm"
    overture_length = len(str(overture_path))
    osm_length = len(str(osm_path))
    project_setting_length = len(str(paths.project_setting))
    limit = overture_length - 1

    # Guards: this scenario only isolates the fix if osm's own path and
    # project_setting both genuinely fit under the limit that trips Overture.
    assert osm_length <= limit, "test setup: osm path must fit under the probe limit"
    assert project_setting_length <= limit, "test setup: project_setting must fit under the probe limit"

    # Unselected-or-unknown still raises: this is what the guard has always
    # done, and must keep doing, when Overture genuinely is (or might be)
    # part of the job.
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], limit=limit)
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], source_ids=["osm", "overture"], limit=limit)

    # osm-only must NOT raise at the same limit: Overture's path is not one
    # this job will ever produce.
    check_path_length(paths, ["infrastructure"], source_ids=["osm"], limit=limit)


def test_check_path_length_still_checks_osms_own_path_when_it_is_selected(tmp_path):
    # With Overture excluded, osm's own raw path must still be an
    # independent candidate, proving exclusion did not turn the guard into
    # a no-op for the sources that ARE selected. project_setting is
    # deliberately given a short, fixed name here (rather than one derived
    # from a long stem, as build_package_paths would produce) so it cannot
    # be the one tripping the limit: this isolates the osm candidate
    # specifically, the same way test_check_path_length_accounts_for_the_
    # fingerprint_segment isolates the fingerprint segment.
    work_dir = tmp_path / ("x" * 200) / "_work" / FINGERPRINT
    paths = PackagePaths(
        root=tmp_path,
        stem="S",
        layers_dir=tmp_path / "layers",
        work_dir=work_dir,
        survey_json=tmp_path / "survey.json",
        project_setting=tmp_path / "short.json",
    )
    osm_path = work_dir / "raw" / "osm" / "r00_c00.osm"
    osm_length = len(str(osm_path))
    project_setting_length = len(str(paths.project_setting))
    assert osm_length > project_setting_length, "test setup: osm's path must be the longer one here"

    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, [], source_ids=["osm"], limit=osm_length - 1)
    assert str(osm_path) in str(excinfo.value)

    # And it must not be checked at all when osm itself is not selected.
    check_path_length(paths, [], source_ids=["stub"], limit=osm_length - 1)
