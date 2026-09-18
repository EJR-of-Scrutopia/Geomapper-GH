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
