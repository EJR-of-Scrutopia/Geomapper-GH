import pytest

from mapgen.geo import BBox, Tile
from mapgen.sources.base import (
    Estimate,
    NullProgress,
    UnknownSourceError,
    available_sources,
    clear_registry,
    get_source,
    register,
)


class FakeSource:
    id = "fake"
    display_name = "Fake Source"
    licence = "CC0"
    attribution = "nobody"
    requires_api_key = False

    def estimate(self, bbox, tiles):
        return Estimate(bytes_estimate=1024 * len(tiles), seconds_estimate=2.0 * len(tiles))

    def fetch(self, bbox, tiles, work_dir, progress):
        paths = []
        for tile in tiles:
            path = work_dir / f"{tile.tile_id}.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tile.tile_id, encoding="utf-8")
            progress.emit("tile_done", source="fake", tile_id=tile.tile_id)
            paths.append(path)
        return paths

    def merge(self, parts, out_dir):
        out = out_dir / "fake.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "\n".join(p.read_text(encoding="utf-8") for p in parts), encoding="utf-8"
        )
        return [out]


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _tile(tile_id, col=0):
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    return Tile(tile_id=tile_id, row=0, col=col, core_bbox=bbox, query_bbox=bbox)


def test_registry_starts_empty():
    assert available_sources() == []


def test_register_then_get_returns_the_same_object():
    source = FakeSource()
    register(source)
    assert get_source("fake") is source


def test_available_sources_lists_registered_sources():
    register(FakeSource())
    assert [s.id for s in available_sources()] == ["fake"]


def test_get_source_raises_a_named_error_for_an_unknown_id():
    with pytest.raises(UnknownSourceError, match="nope"):
        get_source("nope")


def test_unknown_source_error_lists_what_is_available():
    register(FakeSource())
    with pytest.raises(UnknownSourceError, match="fake"):
        get_source("nope")


def test_registering_a_duplicate_id_replaces_the_entry():
    register(FakeSource())
    replacement = FakeSource()
    register(replacement)
    assert get_source("fake") is replacement
    assert len(available_sources()) == 1


def test_null_progress_accepts_any_event_without_error():
    NullProgress().emit("anything", a=1, b="two")


def test_a_conforming_source_round_trips_through_the_protocol(tmp_path):
    source = FakeSource()
    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    tiles = [_tile("r00_c00", 0), _tile("r00_c01", 1)]

    assert source.estimate(bbox, tiles).bytes_estimate == 2048

    parts = source.fetch(bbox, tiles, tmp_path / "work", NullProgress())
    assert len(parts) == 2

    outputs = source.merge(parts, tmp_path / "out")
    assert outputs[0].read_text(encoding="utf-8") == "r00_c00\nr00_c01"


def test_progress_sink_receives_emitted_events(tmp_path):
    events = []

    class Recorder:
        def emit(self, event, **fields):
            events.append((event, fields))

    bbox = BBox.parse("-3.29,51.38,-3.28,51.39")
    FakeSource().fetch(bbox, [_tile("r00_c00")], tmp_path / "work", Recorder())

    assert events == [("tile_done", {"source": "fake", "tile_id": "r00_c00"})]
