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
