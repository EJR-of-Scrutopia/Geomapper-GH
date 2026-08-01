from datetime import date
from pathlib import Path

import pytest

from mapgen.naming import (
    NamingError,
    PathTooLongError,
    build_package_paths,
    check_path_length,
    slugify,
)


def test_slugify_hyphenates_spaces():
    assert slugify("Barry Waterfront", "site") == "Barry-Waterfront"


def test_slugify_preserves_case():
    assert slugify("South Wales", "region") == "South-Wales"


def test_slugify_drops_punctuation():
    assert slugify("St. Mary's Quay!", "site") == "St-Marys-Quay"


def test_slugify_transliterates_non_ascii():
    assert slugify("Dwr Cymru", "region") == "Dwr-Cymru"


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
    paths = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    assert paths.root == tmp_path / "South-Wales" / "2026-08-01_Barry-Waterfront"
    assert paths.stem == "Barry-Waterfront_2026-08-01"
    assert paths.layers_dir == paths.root / "layers"
    assert paths.work_dir == paths.root / "_work"
    assert paths.survey_json == paths.root / "survey.json"
    assert paths.project_setting == paths.root / "Barry-Waterfront_2026-08-01_project_setting.json"


def test_build_package_paths_appends_02_on_collision(tmp_path):
    first = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    first.root.mkdir(parents=True)
    second = build_package_paths(tmp_path, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    assert second.root.name == "2026-08-01_Barry-Waterfront_02"
    assert second.stem == "Barry-Waterfront_2026-08-01_02"


def test_build_package_paths_appends_03_on_second_collision(tmp_path):
    for _ in range(2):
        build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1)).root.mkdir(
            parents=True
        )
    third = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1))
    assert third.root.name == "2026-08-01_Barry_03"


def test_build_package_paths_honours_a_stem_override(tmp_path):
    paths = build_package_paths(
        tmp_path, "South Wales", "Barry", date(2026, 8, 1), stem_override="51.39_51.38_-3.28_-3.29"
    )
    assert paths.stem == "51.39_51.38_-3.28_-3.29"
    assert paths.root.name == "2026-08-01_Barry"
    assert paths.project_setting.name == "51.39_51.38_-3.28_-3.29_project_setting.json"


def test_check_path_length_passes_for_a_short_root(tmp_path):
    paths = build_package_paths(tmp_path, "South Wales", "Barry", date(2026, 8, 1))
    check_path_length(paths, ["building", "infrastructure"])


def test_check_path_length_rejects_a_deep_root():
    deep = Path("C:/") / ("x" * 200)
    paths = build_package_paths(deep, "South Wales", "Barry Waterfront", date(2026, 8, 1))
    with pytest.raises(PathTooLongError) as excinfo:
        check_path_length(paths, ["infrastructure"])
    assert "240" in str(excinfo.value)
    assert "shorter output root" in str(excinfo.value)


def test_check_path_length_boundary_is_inclusive(tmp_path):
    paths = build_package_paths(tmp_path, "R", "S", date(2026, 8, 1))
    probe = len(str(paths.work_dir / "raw" / "overture" / "infrastructure" / "r00_c00.geojson"))
    check_path_length(paths, ["infrastructure"], limit=probe)
    with pytest.raises(PathTooLongError):
        check_path_length(paths, ["infrastructure"], limit=probe - 1)
