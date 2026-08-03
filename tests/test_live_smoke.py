"""Excluded from the default run. Invoke with: pytest -m live"""

import pytest

from mapgen.geo import BBox
from mapgen.package import SurveyRequest, register_default_sources, run_survey


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
