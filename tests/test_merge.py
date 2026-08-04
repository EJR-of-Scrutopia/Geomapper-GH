import json

import pytest

from mapgen.merge import MergeError, assert_inputs_present, merge_geojson, merge_osm_xml

TILE_A = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="51.38" lon="-3.29"/>
  <node id="2" version="1" lat="51.39" lon="-3.28"/>
  <way id="10" version="1"><nd ref="1"/><nd ref="2"/></way>
</osm>
"""

TILE_B = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="2" version="1" lat="51.39" lon="-3.28"/>
  <node id="3" version="1" lat="51.40" lon="-3.27"/>
  <way id="10" version="1"><nd ref="1"/><nd ref="2"/></way>
</osm>
"""

TILE_C_NEWER = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="7" lat="51.3801" lon="-3.2901"/>
</osm>
"""


def _write(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def test_merge_osm_xml_deduplicates_across_tile_seams(tmp_path):
    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "b.osm", TILE_B)]
    count = merge_osm_xml(inputs, tmp_path / "all.osm")
    assert count == 4  # nodes 1, 2, 3 and way 10


def test_merge_osm_xml_keeps_every_distinct_id(tmp_path):
    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "b.osm", TILE_B)]
    out = tmp_path / "all.osm"
    merge_osm_xml(inputs, out)
    text = out.read_text(encoding="utf-8")
    for node_id in ("1", "2", "3"):
        assert 'id="' + node_id + '"' in text


def test_merge_osm_xml_prefers_the_highest_version(tmp_path):
    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "c.osm", TILE_C_NEWER)]
    out = tmp_path / "all.osm"
    merge_osm_xml(inputs, out)
    text = out.read_text(encoding="utf-8")
    assert 'version="7"' in text
    assert 'lat="51.3801"' in text


def test_merge_osm_xml_output_is_parseable(tmp_path):
    import xml.etree.ElementTree as ET

    inputs = [_write(tmp_path, "a.osm", TILE_A), _write(tmp_path, "b.osm", TILE_B)]
    out = tmp_path / "all.osm"
    merge_osm_xml(inputs, out)
    assert ET.parse(out).getroot().tag == "osm"


def test_merge_osm_xml_orders_nodes_before_ways(tmp_path):
    out = tmp_path / "all.osm"
    merge_osm_xml([_write(tmp_path, "a.osm", TILE_A)], out)
    text = out.read_text(encoding="utf-8")
    assert text.index("<node") < text.index("<way")


def _collection(*ids):
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {"type": "Feature", "id": fid, "properties": {}, "geometry": None}
                for fid in ids
            ],
        }
    )


def test_merge_geojson_deduplicates_by_feature_id(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection("f1", "f2"))
    b = _write(tmp_path, "b.geojson", _collection("f2", "f3"))
    assert merge_geojson([a, b], tmp_path / "merged.geojson") == 3


def test_merge_geojson_writes_a_valid_feature_collection(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection("f1"))
    out = tmp_path / "merged.geojson"
    merge_geojson([a], out)
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert parsed["type"] == "FeatureCollection"
    assert len(parsed["features"]) == 1


def test_merge_geojson_handles_an_empty_collection(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection())
    out = tmp_path / "merged.geojson"
    assert merge_geojson([a], out) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["features"] == []


def test_merge_leaves_no_output_when_an_input_is_corrupt(tmp_path):
    good = _write(tmp_path, "a.geojson", _collection())
    bad = _write(tmp_path, "b.geojson", "{not json")
    out = tmp_path / "merged.geojson"
    with pytest.raises(Exception):
        merge_geojson([good, bad], out)
    assert not out.exists()


def test_assert_inputs_present_rejects_a_missing_file(tmp_path):
    present = _write(tmp_path, "a.osm", TILE_A)
    with pytest.raises(MergeError, match="b.osm"):
        assert_inputs_present([present, tmp_path / "b.osm"])


def test_assert_inputs_present_rejects_a_zero_length_file(tmp_path):
    present = _write(tmp_path, "a.osm", TILE_A)
    empty = _write(tmp_path, "b.osm", "")
    with pytest.raises(MergeError, match="b.osm"):
        assert_inputs_present([present, empty])


def test_assert_inputs_present_with_force_returns_only_usable_files(tmp_path):
    present = _write(tmp_path, "a.osm", TILE_A)
    assert assert_inputs_present([present, tmp_path / "b.osm"], force=True) == [present]


def test_assert_inputs_present_success_path_with_all_files_present(tmp_path):
    a = _write(tmp_path, "a.osm", TILE_A)
    b = _write(tmp_path, "b.osm", TILE_B)
    result = assert_inputs_present([a, b], force=False)
    assert result == [a, b]


def _overture_feature(feature_id, geometry_type="Point"):
    return {
        "type": "Feature",
        "geometry": {"type": geometry_type, "coordinates": [0, 0]},
        "properties": {"id": feature_id},
    }


def _overture_collection(*feature_ids):
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [_overture_feature(fid) for fid in feature_ids],
        }
    )


def test_merge_geojson_deduplicates_overture_features_with_properties_id(tmp_path):
    """Shared feature from overlapping tiles has id in properties, not top-level."""
    a = _write(tmp_path, "a.geojson", _overture_collection("f1", "f2"))
    b = _write(tmp_path, "b.geojson", _overture_collection("f2", "f3"))
    assert merge_geojson([a, b], tmp_path / "merged.geojson") == 3


def test_merge_geojson_prefers_top_level_id_when_present(tmp_path):
    """Legacy support: if top-level id exists, use it even if properties has one."""
    a = _write(
        tmp_path,
        "a.geojson",
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "id": "top-f1",
                        "geometry": {"type": "Point", "coordinates": [0, 0]},
                        "properties": {"id": "props-f1"},
                    }
                ],
            }
        ),
    )
    out = tmp_path / "merged.geojson"
    merge_geojson([a], out)
    parsed = json.loads(out.read_text(encoding="utf-8"))
    assert len(parsed["features"]) == 1


def test_merge_geojson_handles_falsy_feature_ids(tmp_path):
    """Falsy ids like 0 or empty string are real ids, not skipped."""
    a = _write(
        tmp_path,
        "a.geojson",
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [0, 0]},
                        "properties": {"id": 0},
                    },
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [1, 1]},
                        "properties": {"id": ""},
                    },
                ],
            }
        ),
    )
    out = tmp_path / "merged.geojson"
    assert merge_geojson([a], out) == 2


def test_merge_geojson_raises_on_missing_feature_id(tmp_path):
    """Feature with no id anywhere raises MergeError, not silently synthesized."""
    a = _write(
        tmp_path,
        "a.geojson",
        json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "geometry": {"type": "Point", "coordinates": [0, 0]},
                        "properties": {},
                    }
                ],
            }
        ),
    )
    out = tmp_path / "merged.geojson"
    with pytest.raises(MergeError, match="a.geojson.*feature 0"):
        merge_geojson([a], out)


def test_merge_osm_xml_handles_non_numeric_ids_in_sort(tmp_path):
    """OSM elements with non-numeric ids should not crash the sort."""
    xml_with_non_numeric = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="abc" version="1" lat="51.38" lon="-3.29"/>
  <node id="2" version="1" lat="51.39" lon="-3.28"/>
</osm>
"""
    out = tmp_path / "all.osm"
    merge_osm_xml([_write(tmp_path, "a.osm", xml_with_non_numeric)], out)
    assert out.exists()


def test_merge_osm_xml_includes_relations(tmp_path):
    """Relations should be included in merge output and sort after ways."""
    xml_with_relation = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" version="1" lat="51.38" lon="-3.29"/>
  <way id="10" version="1"><nd ref="1"/></way>
  <relation id="100" version="1"><member type="node" ref="1" role=""/></relation>
</osm>
"""
    out = tmp_path / "all.osm"
    merge_osm_xml([_write(tmp_path, "a.osm", xml_with_relation)], out)
    text = out.read_text(encoding="utf-8")
    assert '<relation id="100"' in text
    assert text.index("<way") < text.index("<relation")


# --- Task 30: an empty merge can decline to write anything ----------------
#
# The switch, not the ruling. Which callers pass False, and why the source
# layer also removes an earlier attempt's file, is in OsmSource.merge and
# OvertureSource.merge; these two only hold the primitive to what it
# promises, in both positions, because the default is load-bearing for the
# subdivision recombine.


def test_merge_osm_xml_writes_nothing_for_an_empty_merge_when_asked(tmp_path):
    empty = _write(tmp_path, "a.osm", '<?xml version="1.0"?>\n<osm version="0.6"></osm>\n')
    out = tmp_path / "merged.osm"
    assert merge_osm_xml([empty], out, write_when_empty=False) == 0
    assert not out.exists()


def test_merge_osm_xml_still_writes_an_empty_file_by_default(tmp_path):
    # The default is what OsmSource._fetch_tile relies on when it
    # recombines the quarters of a subdivided tile: that file's presence
    # is what package.py reads as "this tile arrived".
    empty = _write(tmp_path, "a.osm", '<?xml version="1.0"?>\n<osm version="0.6"></osm>\n')
    out = tmp_path / "merged.osm"
    assert merge_osm_xml([empty], out) == 0
    assert out.exists()


def test_merge_osm_xml_writes_normally_when_there_is_something_to_write(tmp_path):
    a = _write(tmp_path, "a.osm", TILE_A)
    out = tmp_path / "merged.osm"
    assert merge_osm_xml([a], out, write_when_empty=False) > 0
    assert out.exists()


def test_merge_geojson_writes_nothing_for_an_empty_merge_when_asked(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection())
    out = tmp_path / "merged.geojson"
    assert merge_geojson([a], out, write_when_empty=False) == 0
    assert not out.exists()


def test_merge_geojson_still_writes_an_empty_collection_by_default(tmp_path):
    a = _write(tmp_path, "a.geojson", _collection())
    out = tmp_path / "merged.geojson"
    assert merge_geojson([a], out) == 0
    assert out.exists()
