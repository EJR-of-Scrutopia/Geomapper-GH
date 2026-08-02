import json

import pytest

from mapgen.jobs import CancelToken, Cancelled, EventLog, JobState


def _state(tmp_path, tiles=("r00_c00", "r00_c01"), sources=("osm",)):
    return JobState.load_or_create(tmp_path, list(tiles), list(sources))


def test_new_state_starts_every_tile_pending(tmp_path):
    state = _state(tmp_path)
    assert state.status("r00_c00", "osm") == "pending"
    assert state.is_done("r00_c00", "osm") is False


def test_marking_ok_makes_a_tile_done(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    assert state.is_done("r00_c00", "osm") is True


def test_marking_failed_does_not_make_a_tile_done(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "failed")
    assert state.is_done("r00_c00", "osm") is False
    assert state.status("r00_c00", "osm") == "failed"


def test_state_is_written_to_disk_on_mark(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    payload = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert payload["tiles"]["r00_c00"]["osm"] == "ok"


def test_state_is_reloaded_from_disk(tmp_path):
    _state(tmp_path).mark("r00_c00", "osm", "ok")
    reloaded = _state(tmp_path)
    assert reloaded.is_done("r00_c00", "osm") is True
    assert reloaded.is_done("r00_c01", "osm") is False


def test_reload_adds_tiles_that_were_not_in_the_saved_state(tmp_path):
    _state(tmp_path, tiles=("r00_c00",)).mark("r00_c00", "osm", "ok")
    widened = _state(tmp_path, tiles=("r00_c00", "r00_c01"))
    assert widened.is_done("r00_c00", "osm") is True
    assert widened.status("r00_c01", "osm") == "pending"


def test_reload_adds_sources_that_were_not_in_the_saved_state(tmp_path):
    _state(tmp_path, sources=("osm",)).mark("r00_c00", "osm", "ok")
    widened = _state(tmp_path, sources=("osm", "overture"))
    assert widened.status("r00_c00", "overture") == "pending"


def test_complete_is_false_while_anything_is_pending(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    assert state.complete is False


def test_complete_is_false_when_anything_failed(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    state.mark("r00_c01", "osm", "failed")
    assert state.complete is False


def test_complete_is_true_when_everything_is_ok(tmp_path):
    state = _state(tmp_path)
    state.mark("r00_c00", "osm", "ok")
    state.mark("r00_c01", "osm", "ok")
    assert state.complete is True


def test_as_tile_records_matches_the_survey_json_shape(tmp_path):
    state = _state(tmp_path, tiles=("r00_c00",), sources=("osm", "overture"))
    state.mark("r00_c00", "osm", "ok")
    state.mark("r00_c00", "overture", "failed")
    assert state.as_tile_records() == [
        {"tile_id": "r00_c00", "osm": "ok", "overture": "failed"}
    ]


def test_a_corrupt_state_file_is_discarded_rather_than_crashing(tmp_path):
    (tmp_path / "state.json").write_text("{not json", encoding="utf-8")
    state = _state(tmp_path)
    assert state.status("r00_c00", "osm") == "pending"


def test_cancel_token_starts_uncancelled():
    assert CancelToken().is_cancelled() is False


def test_cancel_token_records_cancellation():
    token = CancelToken()
    token.cancel()
    assert token.is_cancelled() is True


def test_raise_if_cancelled_is_silent_when_not_cancelled():
    CancelToken().raise_if_cancelled()


def test_raise_if_cancelled_raises_once_cancelled():
    token = CancelToken()
    token.cancel()
    with pytest.raises(Cancelled):
        token.raise_if_cancelled()


def test_event_log_records_events():
    log = EventLog()
    log.emit("tile_done", tile_id="r00_c00")
    assert log.events == [{"event": "tile_done", "tile_id": "r00_c00"}]


def test_event_log_forwards_to_a_listener():
    seen = []
    log = EventLog(listener=seen.append)
    log.emit("tile_done", tile_id="r00_c00")
    assert seen == [{"event": "tile_done", "tile_id": "r00_c00"}]


def test_event_log_survives_a_listener_that_raises():
    def bad_listener(_payload):
        raise RuntimeError("browser disconnected")

    log = EventLog(listener=bad_listener)
    log.emit("tile_done", tile_id="r00_c00")
    assert len(log.events) == 1
