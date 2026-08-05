import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from mapgen.naming import (
    DEFAULT_PATH_LIMIT,
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


# A coordinator review's Important 1: tiling_fingerprint used to take only
# the six tiling numbers, so two requests over the same bbox and tiling but
# a DIFFERENT category or Overture type selection landed in the SAME
# _work/ fingerprint directory, resuming into a package that silently mixed
# old, differently-filtered tiles with a survey.json describing only the
# new selection. categories and overture_types are now required parameters
# (see the function's own docstring for the full finding), always the
# EFFECTIVE, already-resolved selection. ALL_CATEGORY_IDS/full defaults
# below stand in for "no filtering, today's behaviour" the same way
# SurveyRequest.effective_categories/effective_overture_types resolve an
# unfiltered request; the tests that are not about categories or Overture
# types themselves hold both fixed, so what they each claim to isolate
# (tile size, overlap, bbox) is still the only thing that changed.
_FULL_CATEGORIES = ("buildings", "water")
_FULL_OVERTURE_TYPES = ("building", "water")


def test_tiling_fingerprint_has_a_fixed_length():
    fingerprint = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    assert len(fingerprint) == FINGERPRINT_LENGTH


def test_tiling_fingerprint_is_deterministic(tmp_path):
    args = (-3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES)
    assert tiling_fingerprint(*args) == tiling_fingerprint(*args)


def test_tiling_fingerprint_is_stable_across_processes():
    # hashlib.sha256 over a fixed-format string has no dependency on
    # per-process state (unlike Python's built-in hash(), which is salted
    # per process by default), so two separate interpreter processes given
    # the same inputs must produce the same 8 characters.
    script = (
        "from mapgen.naming import tiling_fingerprint;"
        "print(tiling_fingerprint(-3.29, 51.38, -3.28, 51.39, 600.0, 50.0,"
        " ['buildings', 'water'], ['building', 'water']))"
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
    a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    b = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 1200.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    assert a != b


def test_tiling_fingerprint_differs_for_a_different_overlap():
    a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    b = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 75.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    assert a != b


def test_tiling_fingerprint_differs_for_a_different_bbox():
    a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    b = tiling_fingerprint(
        -3.30, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    assert a != b


def test_tiling_fingerprint_differs_for_a_different_category_selection():
    # The heart of Important 1: this is what makes a resumed job land in a
    # fresh _work/ directory, rather than mixing tiles fetched under the
    # old selection with a survey.json that goes on to describe the new
    # one, when only the categories change and the tiling itself does not.
    a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, ("buildings",), _FULL_OVERTURE_TYPES
    )
    b = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, ("water",), _FULL_OVERTURE_TYPES
    )
    assert a != b


def test_tiling_fingerprint_differs_for_a_different_overture_type_selection():
    # The same property as above, for an explicit --overture-type selection
    # that diverges from what the category selection alone would imply.
    a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, ("building",)
    )
    b = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, ("water",)
    )
    assert a != b


def test_tiling_fingerprint_is_independent_of_selection_order():
    # A selection is a SET of ids, not a sequence with meaningful order:
    # the CLI's repeated --category flags and the browser's checklist can
    # each produce the same set in a different order without that being a
    # different selection, so it must not fingerprint as one.
    a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, ("buildings", "water"), ("building", "water")
    )
    b = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, ("water", "buildings"), ("water", "building")
    )
    assert a == b


def test_work_dir_includes_the_fingerprint_segment(tmp_path):
    # The redesign's whole point: two different tilings must never resolve
    # to the same work_dir, and the guard below depends on this segment
    # actually being present, not merely intended.
    fp_a = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 600.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
    fp_b = tiling_fingerprint(
        -3.29, 51.38, -3.28, 51.39, 1200.0, 50.0, _FULL_CATEGORIES, _FULL_OVERTURE_TYPES
    )
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
    #
    # Overture's raw path is "raw/overture/<type>.geojson" since Task 23
    # stopped tiling it; it was "raw/overture/<type>/rNN_cNN.geojson". Every
    # probe in this file that composes that path by hand had to follow, or
    # it would go on measuring a path the tool can no longer produce and
    # claim eight characters of slack that are not really there.
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    overture_path = paths.work_dir / "raw" / "overture" / "infrastructure.geojson"
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

    overture_path = paths.work_dir / "raw" / "overture" / "infrastructure.geojson"
    overture_length = len(str(overture_path.resolve() if not overture_path.is_absolute() else overture_path))
    project_setting_length = len(str(paths.project_setting.resolve() if not paths.project_setting.is_absolute() else paths.project_setting))

    assert overture_length <= 240, f"Overture path must be under 240 to test new check: {overture_length}"
    assert project_setting_length > 240, f"Project setting path must be over 240 to isolate the new check: {project_setting_length}"

    # source_ids excludes lidar_wales (Task 6) deliberately: this test
    # isolates project_setting against Overture specifically, and
    # lidar_wales's own merged 0.25 m contour file is stem-based too, two
    # characters longer than project_setting's own suffix for the same
    # stem (see check_path_length's own docstring), which would otherwise
    # become the candidate this test's assertion is about.
    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(
            paths, ["infrastructure"], source_ids=["overture", "osm", "elevation"], limit=240
        )
    assert str(paths.project_setting) in str(excinfo.value)


def test_check_path_length_boundary_is_inclusive(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    probe = len(str(paths.work_dir / "raw" / "overture" / "infrastructure.geojson"))
    check_path_length(paths, ["infrastructure"], limit=probe)
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], limit=probe - 1)


# --- Task 20 finding 3: the guard must measure the job that will actually
# run, not every source the tool knows how to fetch. A real --source osm
# run was refused over a path shaped like Overture's, though Overture was
# never selected. ------------------------------------------------------


def test_check_path_length_ignores_overture_when_it_is_not_among_the_selected_sources(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    overture_path = paths.work_dir / "raw" / "overture" / "infrastructure.geojson"
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


# --- Task 28: elevation's raw file now carries the model's name ------------


def test_check_path_length_measures_the_chosen_elevation_models_own_filename(tmp_path):
    # The raw DEM used to be a fixed "elevation.tif". It is
    # "elevation_<model>.tif" now, which for the longest model in the
    # vocabulary is sixteen characters more, and a guard measuring the old
    # name would admit a job Windows then refuses.
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    default_path = paths.work_dir / "raw" / "elevation" / "elevation_COP30.tif"
    longest_path = paths.work_dir / "raw" / "elevation" / "elevation_GEBCOSubIceTopo.tif"
    limit = len(str(default_path))

    assert len(str(longest_path)) > limit, "test setup: the longer model must be longer"

    # Exactly at the limit for the default model, so this isolates the
    # filename itself rather than anything else about the path.
    check_path_length(paths, [], source_ids=["elevation"], limit=limit)

    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(
            paths,
            [],
            source_ids=["elevation"],
            limit=limit,
            elevation_demtype="GEBCOSubIceTopo",
        )
    # And it names the path that would actually be created, not the one
    # the guard used to assume.
    assert str(longest_path) in str(excinfo.value)


# --- Task 6: lidar_wales has two candidates of its own, and both must bind
# depending on the stem's own length (see check_path_length's own docstring)
# --------------------------------------------------------------------------


def test_check_path_length_ignores_lidar_wales_when_it_is_not_selected(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1), FINGERPRINT)
    raw_path = paths.work_dir / "raw" / "lidar_wales" / "lidar_dtm.tif"
    limit = len(str(raw_path)) - 1

    with pytest.raises(PathTooLongError):
        check_path_length(paths, [], source_ids=["lidar_wales"], limit=limit)

    # Not selected: the same limit must not be tripped by a path this job
    # will never produce.
    check_path_length(paths, [], source_ids=["osm"], limit=limit)


def test_check_path_length_checks_lidar_waless_own_raw_work_file(tmp_path):
    # Isolated with a short, fixed stem (not one build_package_paths would
    # derive from a long site name) so the raw candidate, not the
    # stem-based contour one, is what trips the limit.
    work_dir = tmp_path / ("x" * 200) / "_work" / FINGERPRINT
    paths = PackagePaths(
        root=tmp_path,
        stem="S",
        layers_dir=tmp_path / "layers",
        work_dir=work_dir,
        survey_json=tmp_path / "survey.json",
        project_setting=tmp_path / "short.json",
    )
    raw_path = work_dir / "raw" / "lidar_wales" / "lidar_dtm.tif"
    contour_path = tmp_path / "S_contours_0.25m.geojson"
    raw_length = len(str(raw_path))
    assert raw_length > len(str(contour_path)), "test setup: the raw candidate must be the longer one here"

    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, [], source_ids=["lidar_wales"], limit=raw_length - 1)
    assert str(raw_path) in str(excinfo.value)

    check_path_length(paths, [], source_ids=["lidar_wales"], limit=raw_length)


def test_check_path_length_checks_lidar_waless_own_contour_file_for_a_long_stem(tmp_path):
    # A realistic long region/site name pushes the merged 0.25 m contour
    # file's own stem-based path past the raw work file's fixed nesting,
    # which is exactly the scenario check_path_length's own docstring
    # names: which of the two candidates binds depends on the stem.
    long_site = "A" * 40
    paths = build_package_paths(tmp_path, "R", long_site, date(2026, 8, 1), FINGERPRINT)
    raw_path = paths.work_dir / "raw" / "lidar_wales" / "lidar_dtm.tif"
    contour_path = paths.root / f"{paths.stem}_contours_0.25m.geojson"
    assert len(str(contour_path)) > len(str(raw_path)), (
        "test setup: the contour candidate must be the longer one here"
    )

    limit = len(str(contour_path)) - 1
    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, [], source_ids=["lidar_wales"], limit=limit)
    assert str(contour_path) in str(excinfo.value)

    check_path_length(paths, [], source_ids=["lidar_wales"], limit=len(str(contour_path)))


# --- what the 240 character limit does and does not cover ------------------
#
# check_path_length measures OSM at raw/osm/r00_c00.osm, and Task 26 gave a
# tile too dense for one request a set of quarters to keep, whose deepest
# file is raw/osm/_split/r00_c00_q11_q11.osm, fifteen characters longer.
# Neither is modelled in the guard: naming.py would have to import a
# constant from a source module to do it.
#
# The first version of these tests concluded that DEFAULT_PATH_LIMIT's own
# 20 characters of headroom below Windows' 260 absorbed those fifteen, and
# called the remainder five characters of slack. That was wrong, and wrong
# in the way this project keeps producing: a guard that reads like one and
# is not. The file the deepest quarter path names is never the file that
# gets created. Every write in mapgen goes through fsutil.atomic_writer,
# which writes to _temp_path first, and that appends
# .{pid}.{thread ident}.{8 hex}.part to the name.
#
# So the two tests below say the two true things separately, because they
# have different answers.


WINDOWS_MAX_PATH = 260


def _length_at_the_limit(tmp_path, relative_parts):
    """The length a path of this shape reaches for a job admitted at
    exactly DEFAULT_PATH_LIMIT, built for real under tmp_path rather than
    added up, so a separator miscount cannot hide in the arithmetic."""
    work_dir = tmp_path / "_work" / FINGERPRINT
    reference = work_dir / "raw" / "osm" / "r00_c00.osm"
    # One character of the padding pays for the separator the padding
    # segment itself introduces. Built and measured rather than added up
    # for exactly this reason.
    padding = DEFAULT_PATH_LIMIT - len(str(reference)) - 1
    assert padding > 0, "test setup: tmp_path is already longer than the limit"
    padded_work_dir = tmp_path / ("x" * padding) / "_work" / FINGERPRINT
    assert (
        len(str(padded_work_dir / "raw" / "osm" / "r00_c00.osm")) == DEFAULT_PATH_LIMIT
    ), "test setup: the reference tile path should land exactly on the limit"
    return padded_work_dir.joinpath("raw", "osm", *relative_parts)


def test_a_subdivided_tiles_own_file_stays_inside_max_path(tmp_path):
    # The FINAL name, which is the one that persists: it is what a resume
    # looks for, what the recombine reads, and what anyone opening _work/
    # sees. For a job admitted at exactly the limit it lands at 255, so
    # raising DEFAULT_PATH_LIMIT or deepening MAX_SUBDIVISION_DEPTH fails
    # here rather than on a real package.
    from mapgen.sources.osm import MAX_SUBDIVISION_DEPTH, SPLIT_DIR_NAME

    quarter = _length_at_the_limit(
        tmp_path,
        (SPLIT_DIR_NAME, f"r00_c00{'_q11' * MAX_SUBDIVISION_DEPTH}.osm"),
    )
    assert len(str(quarter)) <= WINDOWS_MAX_PATH, (
        f"a job the guard admits at {DEFAULT_PATH_LIMIT} characters keeps a "
        f"quarter file {len(str(quarter))} characters long, past Windows' "
        f"own {WINDOWS_MAX_PATH}"
    )


def test_the_path_limit_does_not_cover_the_temp_path_a_write_really_creates(tmp_path):
    """Characterisation, not a promise: DEFAULT_PATH_LIMIT does not protect
    against MAX_PATH at all once the temp suffix is counted, and mapgen
    depends on long paths being enabled.

    fsutil._temp_path appends .{pid}.{thread ident}.{8 hex}.part, which
    measured 26 characters on this machine. A job admitted at exactly the
    240 character limit therefore CREATES a path of about 266 characters
    for an ordinary tile and about 281 for a subdivided tile's deepest
    quarter. Both are past 260. The guard's 20 characters of headroom
    cannot cover a suffix of this size: 16 of those characters are fixed
    (four dots, eight hex, "part"), leaving four for the pid and the
    thread id together, which no real pair of those fits in.

    This predates Task 26 and is not about subdivision. Every OSM tile
    mapgen has ever written took the same 26 characters, so the ordinary
    tile above was already 266 before quarters existed; subdivision adds
    fifteen to a figure that was already over. It is also not a live
    failure here: LongPathsEnabled is 1 on this machine, Python has been
    long path aware since 3.6, and a write through a 288 character temp
    path was confirmed to succeed. What the 240 limit really buys is that
    the paths which PERSIST, the ones Grasshopper and Explorer and a
    resume all have to deal with, stay inside 260 (see the test above),
    and that a job is refused before it starts rather than part way
    through.

    Deliberately NOT done, and named so the next reader does not re-derive
    them: lowering DEFAULT_PATH_LIMIT (it would reject jobs that work
    today), \\\\?\\ prefixing every write, and shortening the split
    directory to save characters. All three are out of proportion to
    something that does not currently bite, and none of them is the
    binding constraint. If a machine without long path support ever runs
    this, that is the moment, and this test is the note explaining why.
    """
    from mapgen.fsutil import _temp_path
    from mapgen.sources.osm import MAX_SUBDIVISION_DEPTH, SPLIT_DIR_NAME

    tile = _length_at_the_limit(tmp_path, ("r00_c00.osm",))
    quarter = _length_at_the_limit(
        tmp_path,
        (SPLIT_DIR_NAME, f"r00_c00{'_q11' * MAX_SUBDIVISION_DEPTH}.osm"),
    )
    assert len(str(tile)) == DEFAULT_PATH_LIMIT, "test setup: the tile should sit on the limit"

    tile_written = len(str(_temp_path(tile)))
    quarter_written = len(str(_temp_path(quarter)))
    suffix = tile_written - len(str(tile))

    # The headroom the first version of this test claimed, against the
    # suffix that actually gets appended.
    assert suffix > WINDOWS_MAX_PATH - DEFAULT_PATH_LIMIT, (
        f"the temp suffix is {suffix} characters and the guard holds back "
        f"{WINDOWS_MAX_PATH - DEFAULT_PATH_LIMIT}; if that has stopped being "
        f"true, the docstring above needs rewriting, not this assertion"
    )
    # An ORDINARY tile, with no subdivision anywhere near it, is already
    # over. This is the sentence that matters.
    assert tile_written > WINDOWS_MAX_PATH
    # And subdivision's own contribution is the fifteen characters between
    # the two, not the overrun itself.
    assert quarter_written - tile_written == 15
    assert quarter_written > WINDOWS_MAX_PATH
