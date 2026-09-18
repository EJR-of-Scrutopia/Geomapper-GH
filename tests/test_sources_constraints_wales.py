import json
from pathlib import Path

import pytest
import requests

from mapgen.cog import USER_AGENT
from mapgen.constraints import LAYERS, padded_rect
from mapgen.geo import BBox, Tile
from mapgen.jobs import CancelToken, Cancelled
from mapgen.sources import constraints_wales
from mapgen.sources.constraints_wales import (
    FEATURE_CEILING,
    REQUEST_TIMEOUT_SECONDS,
    WFS_URL,
    WORK_FEATURES,
    WORK_META,
    ConstraintsWalesError,
    ConstraintsWalesSource,
)

FIXTURES = Path(__file__).parent / "fixtures" / "constraints"
COWBRIDGE = BBox.parse("-3.46,51.455,-3.438,51.468")
BRISTOL = BBox.parse("-2.62,51.44,-2.56,51.47")


def _tiles(bbox=COWBRIDGE):
    return [Tile(tile_id="r00_c00", row=0, col=0, core_bbox=bbox, query_bbox=bbox)]


class _Response:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return json.loads(self.text)


def _collection(features=(), matched=None):
    features = list(features)
    return _Response(payload={
        "type": "FeatureCollection", "features": features,
        "numberMatched": len(features) if matched is None else matched, "numberReturned": len(features),
    })


class _RoutedSession:
    """Answers each GetFeature by its typeNames; an answer may be a
    response, an exception to raise, or a list consumed one per call."""

    def __init__(self, answers=None):
        self.answers = dict(answers or {})
        self.calls = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}), "timeout": timeout})
        answer = self.answers.get(params["typeNames"], _collection())
        if isinstance(answer, list):
            answer = answer.pop(0) if len(answer) > 1 else answer[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Recorder:
    def __init__(self):
        self.events = []

    def emit(self, event, **fields):
        self.events.append((event, fields))


def _fixture(name):
    return _Response(text=(FIXTURES / name).read_text(encoding="utf-8"))


def test_one_request_per_layer_in_table_order_with_the_verified_shape(tmp_path):
    session = _RoutedSession()
    source = ConstraintsWalesSource(session=session)
    source.fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    assert [call["params"]["typeNames"] for call in session.calls] == [layer.type_name for layer in LAYERS]
    call = session.calls[0]
    west, south, east, north = padded_rect((COWBRIDGE.west, COWBRIDGE.south, COWBRIDGE.east, COWBRIDGE.north))
    assert call["url"] == WFS_URL
    assert call["params"]["srsName"] == "EPSG:4326"
    assert call["params"]["outputFormat"] == "application/json"
    # lon,lat order: the verified trap is that lat,lon silently returns nothing.
    assert call["params"]["bbox"] == f"{west},{south},{east},{north},EPSG:4326"
    assert call["params"]["startIndex"] == "0"
    assert call["headers"]["User-Agent"] == USER_AGENT
    assert call["timeout"] == REQUEST_TIMEOUT_SECONDS


def test_a_full_page_asks_for_the_next(tmp_path, monkeypatch):
    monkeypatch.setattr(constraints_wales, "PAGE_SIZE", 2)
    point = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [-3.45, 51.46]}, "properties": {"Name": "A", "Grade": "II"}}
    session = _RoutedSession({"inspire-wg:Cadw_ListedBuildings": [_collection([point, point], matched=3), _collection([point], matched=3)]})
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    listed = [c for c in session.calls if c["params"]["typeNames"] == "inspire-wg:Cadw_ListedBuildings"]
    assert [c["params"]["startIndex"] for c in listed] == ["0", "2"]
    rows = [json.loads(line) for line in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines()]
    assert len(next(r for r in rows if r["type_name"] == "inspire-wg:Cadw_ListedBuildings")["features"]) == 3


def test_a_layer_over_the_ceiling_is_not_checked(tmp_path):
    session = _RoutedSession({"inspire-nrw:NRW_SSSI": _collection([], matched=FEATURE_CEILING + 1)})
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    rows = {r["type_name"]: r for r in (json.loads(l) for l in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines())}
    assert rows["inspire-nrw:NRW_SSSI"]["ok"] is False
    assert "too many" in rows["inspire-nrw:NRW_SSSI"]["reason"]


def test_a_layer_that_fails_once_is_retried_after_the_others(tmp_path):
    session = _RoutedSession({"inspire-nrw:NRW_SAC": [requests.ConnectionError("down"), _collection()]})
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    names = [c["params"]["typeNames"] for c in session.calls]
    assert names.count("inspire-nrw:NRW_SAC") == 2
    assert names[-1] == "inspire-nrw:NRW_SAC"
    rows = {r["type_name"]: r for r in (json.loads(l) for l in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines())}
    assert rows["inspire-nrw:NRW_SAC"]["ok"] is True


def test_a_layer_that_fails_twice_is_reported_without_a_url(tmp_path):
    failure = requests.ConnectionError("failed reaching https://example.invalid/secret")
    session = _RoutedSession({"inspire-nrw:NRW_SAC": failure})
    recorder = _Recorder()
    ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, recorder)
    rows = {r["type_name"]: r for r in (json.loads(l) for l in (tmp_path / WORK_FEATURES).read_text(encoding="utf-8").splitlines())}
    assert rows["inspire-nrw:NRW_SAC"]["ok"] is False
    assert rows["inspire-nrw:NRW_SAC"]["reason"] == "DataMapWales could not be reached"
    assert "example.invalid" not in json.dumps(recorder.events)
    assert ("constraint_layer_not_checked", {"layer": "Special Area of Conservation", "reason": "DataMapWales could not be reached"}) in recorder.events


def test_every_layer_failing_is_a_real_failure(tmp_path):
    session = _RoutedSession({layer.type_name: _Response(status_code=503, text="") for layer in LAYERS})
    source = ConstraintsWalesSource(session=session)
    with pytest.raises(ConstraintsWalesError):
        source.fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    assert [f.tile_id for f in source.tile_failures] == ["r00_c00"]
    assert source.tile_failures[0].source == "constraints_wales"
    assert "503" in source.tile_failures[0].reason
    assert not (tmp_path / WORK_FEATURES).exists()


def test_cancel_stops_before_the_next_layer(tmp_path):
    token = CancelToken()
    token.cancel()
    session = _RoutedSession()
    with pytest.raises(Cancelled):
        ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder(), cancel=token)
    assert session.calls == []


def test_work_files_and_a_resume_that_touches_nothing(tmp_path):
    session = _RoutedSession({"geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json")})
    recorder = _Recorder()
    parts = ConstraintsWalesSource(session=session).fetch(COWBRIDGE, _tiles(), tmp_path, recorder)
    assert [p.name for p in parts] == [WORK_FEATURES, WORK_META]
    meta = json.loads((tmp_path / WORK_META).read_text(encoding="utf-8"))
    assert meta["councils"] == ["Vale of Glamorgan"]
    assert meta["coverage"] == "full"
    assert meta["margin_m"] == 250.0
    assert any(event == "constraint_on_site" and fields["line"].startswith("Conservation area: Cowbridge") for event, fields in recorder.events)
    assert ("tile_done", {"source": "constraints_wales", "tile_id": "whole-area"}) in recorder.events

    class _Poisoned:
        def get(self, *args, **kwargs):
            raise AssertionError("a resume over the same extent must not fetch")

    again = ConstraintsWalesSource(session=_Poisoned()).fetch(COWBRIDGE, _tiles(), tmp_path, _Recorder())
    assert [p.name for p in again] == [WORK_FEATURES, WORK_META]


def test_covers_tier_detail_estimate_never_touch_the_network():
    class _Poisoned:
        def get(self, *args, **kwargs):
            raise AssertionError("no network here")

    source = ConstraintsWalesSource(session=_Poisoned())
    assert source.covers(COWBRIDGE) == "full"
    assert source.covers(BRISTOL) == "none"
    assert source.tier("constraints") == 1
    assert source.tier("buildings") is None
    assert source.detail(COWBRIDGE) == "Welsh designations as published, indicative"
    estimate = source.estimate(COWBRIDGE, _tiles())
    assert estimate.bytes_estimate == len(LAYERS) * constraints_wales.BYTES_PER_LAYER_ESTIMATE


def _fetched(tmp_path, answers):
    work = tmp_path / "work"
    work.mkdir()
    source = ConstraintsWalesSource(session=_RoutedSession(answers))
    parts = source.fetch(COWBRIDGE, _tiles(), work, _Recorder())
    out = tmp_path / "package"
    out.mkdir()
    return source, parts, out


def test_merge_writes_only_groups_with_features_and_always_the_summary(tmp_path):
    source, parts, out = _fetched(tmp_path, {
        "geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json"),
        "inspire-wg:Cadw_ListedBuildings": _fixture("cowbridge_listed_buildings.json"),
    })
    written = source.merge(parts, out, "Cowbridge_2026-09-18")
    names = sorted(p.name for p in written)
    assert names == [
        "Cowbridge_2026-09-18_constraints.txt",
        "Cowbridge_2026-09-18_constraints_heritage.geojson",
        "Cowbridge_2026-09-18_constraints_listed_buildings.geojson",
    ]
    heritage = json.loads((out / "Cowbridge_2026-09-18_constraints_heritage.geojson").read_text(encoding="utf-8"))
    assert {f["properties"]["name"] for f in heritage["features"]} >= {"Cowbridge"}
    assert {f["properties"]["where"] for f in heritage["features"]} <= {"site", "margin"}


def test_merge_summary_and_record(tmp_path):
    source, parts, out = _fetched(tmp_path, {
        "geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json"),
        "inspire-nrw:NRW_SAC": requests.ConnectionError("down"),
    })
    source.merge(parts, out, "Cowbridge_2026-09-18")
    text = (out / "Cowbridge_2026-09-18_constraints.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    today = json.loads(parts[1].read_text(encoding="utf-8"))["fetched_on"]
    assert "Confirm with the council (Vale of Glamorgan)." in text
    assert "- Special Area of Conservation: DataMapWales could not be reached" in lines
    # Never under "Checked, none found": the line after that heading.
    assert "Special Area of Conservation" not in lines[lines.index("Checked, none found") + 1]
    # Cadw's layers answered (found or not), so Cadw's statement is in
    # Sources, carrying the fetch date.
    assert f"The Welsh Historic Environment Service (Cadw), {today}," in text
    record = source.constraints_record
    assert record["checked"] is True
    assert record["summary_file"] == "Cowbridge_2026-09-18_constraints.txt"
    assert record["layers"]["inspire-nrw:NRW_SAC"]["status"] == "failed"
    assert "Natural Resources Wales" in source.attribution
    assert f"(Cadw), {today}," in source.attribution


def test_merge_with_no_work_files_writes_nothing(tmp_path):
    source = ConstraintsWalesSource(session=_RoutedSession())
    assert source.merge([], tmp_path, "X_2026-09-18") == []
    assert source.constraints_record is None


def test_possible_outputs_is_the_closed_list():
    assert ConstraintsWalesSource(session=_RoutedSession()).possible_outputs("S") == [
        "S_constraints_flood.geojson",
        "S_constraints_nature.geojson",
        "S_constraints_landscape.geojson",
        "S_constraints_heritage.geojson",
        "S_constraints_listed_buildings.geojson",
        "S_constraints.txt",
    ]


def test_merge_with_no_work_files_clears_earlier_records(tmp_path):
    source, parts, out = _fetched(tmp_path, {
        "geonode:conservation_areas_wales": _fixture("cowbridge_conservation_areas.json"),
    })
    source.merge(parts, out, "First_2026-09-18")
    assert source.constraints_record is not None
    earlier_attribution = source.attribution
    assert "Natural Resources Wales" in earlier_attribution
    assert earlier_attribution != ConstraintsWalesSource.attribution
    source.merge([], out, "Again_2026-09-18")
    assert source.constraints_record is None
    assert source.attribution == ConstraintsWalesSource.attribution
