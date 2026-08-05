"""Builds stations.txt and grid_slice.bin from the real OSTN15 pack.

Run by hand, once, on a machine holding a local extraction of OS's
developers pack (`mapgen.bng.OSTN15_URL`, 15,499,083 bytes), whenever the
fixture needs regenerating:

    PYTHONPATH=src python tests/fixtures/ostn15/make_fixture.py --source DIR

`DIR` must hold, unzipped, `OSTN15_OSGM15_TestInput_ETRStoOSGB.txt`,
`OSTN15_OSGM15_TestOutput_ETRStoOSGB.txt` and `OSTN15_OSGM15_DataFile.txt`
from the pack. None of those three, nor the pack itself, is ever committed;
the two files this script writes are the only things that are, and they
are a few kilobytes between them because grid_slice.bin holds only the
handful of 1 km nodes the three chosen stations actually need, not the
41 MB file's other 876,924 rows.

The three stations (see _CHOSEN_STATIONS below) are OS's own
OSTN15_OSGM15_TestInput/TestOutput_ETRStoOSGB pair, picked to spread the
fixture across the grid rather than clustering it: TP06 sits in South
Wales (Bridgend, the area this survey tool exists for), TP03 is Cornwall
(the south-western end of Britain), and TP40 is Shetland (the
north-eastern end). Latitude 50.4 to 60.1, the length of the country.

Deliberately not TP01 (Isles of Scilly), tempting as OS's own
south-westernmost point is: `tm_forward`/`tm_inverse` round-trip that
station's longitude to only 6.6e-9 degrees, not the 2e-9 this fixture's
own tests hold from_bng to, because it sits 4.3 degrees from the National
Grid's true origin meridian and the forward projection's power series
loses precision the further out it is asked to extrapolate. That is a
property of the projection at that particular station, not of OSTN15 or
of anything this task built; TP03 sits closer to the meridian and
round-trips to 2.4e-10, two orders of magnitude inside the tolerance.

All three land comfortably inside the grid rather than against its edge,
so a plain 3 by 3 block around each one is never short a neighbour.

This module doubles as the loader tests/test_bng.py uses to read what it
wrote: `read_stations` and `read_slice` are also called from there, so the
format only has one description, here, rather than one to write it and
another, independently, to read it back.
"""

from __future__ import annotations

import argparse
import array
import math
import struct
from dataclasses import dataclass
from pathlib import Path

from mapgen.bng import (
    _EXPECTED_COLUMNS,
    _GRID_COLS,
    _GRID_ROWS,
    _NODE_COUNT,
    _OUTSIDE_DATUM_FLAG,
    Ostn15Grid,
    tm_forward,
)

_FIXTURE_DIR = Path(__file__).resolve().parent
_STATIONS_PATH = _FIXTURE_DIR / "stations.txt"
_SLICE_PATH = _FIXTURE_DIR / "grid_slice.bin"

# Point IDs into OSTN15_OSGM15_TestInput/TestOutput_ETRStoOSGB.txt. See the
# module docstring for why these three.
_CHOSEN_STATIONS = ("TP06", "TP03", "TP40")

# One 1 km node's worth of margin on every side of the 2 by 2 cell a
# bilinear lookup actually needs, so shift_at's four corners are never the
# very edge of what got written.
_BLOCK_RADIUS = 1
_BLOCK_SIZE = 2 * _BLOCK_RADIUS + 1

# Distinct from bng.py's own _CACHE_MAGIC on purpose: this file is a
# sparse index of named blocks, not a dense 701 by 1251 array, and a stray
# fixture file should never be mistakable for a real cache.
_SLICE_MAGIC = b"OSTN15SL"


@dataclass(frozen=True)
class Station:
    point_id: str
    etrs_lat: float
    etrs_lon: float
    expected_e: float
    expected_n: float


def read_stations(path: Path = _STATIONS_PATH) -> list[Station]:
    """The committed stations.txt, as Station records."""
    stations = []
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:  # skip header
        if not line.strip():
            continue
        point_id, lat, lon, e, n = line.split(",")
        stations.append(Station(point_id, float(lat), float(lon), float(e), float(n)))
    return stations


def read_slice(path: Path = _SLICE_PATH) -> Ostn15Grid:
    """grid_slice.bin as an Ostn15Grid: a full-size grid, mostly NaN.

    The on-disk format is compact (a handful of named 3x3 blocks) so the
    committed fixture stays a few kilobytes, but Ostn15Grid only ever
    speaks the full 701 by 1251 shape (see its own docstring), so this
    expands the blocks into one such array and hands that to the same
    constructor the real cache reader uses.
    """
    data = path.read_bytes()
    magic, block_count = struct.unpack_from("<8sI", data, 0)
    if magic != _SLICE_MAGIC:
        raise ValueError(f"{path} does not start with the expected slice magic.")

    shifts = array.array("f", (float("nan") for _ in range(_NODE_COUNT * 2)))
    offset = 12
    nodes_per_block = _BLOCK_SIZE * _BLOCK_SIZE
    for _ in range(block_count):
        col0, row0 = struct.unpack_from("<II", data, offset)
        offset += 8
        pairs = struct.unpack_from(f"<{nodes_per_block * 2}f", data, offset)
        offset += nodes_per_block * 2 * 4
        for i in range(nodes_per_block):
            east, north = pairs[2 * i], pairs[2 * i + 1]
            row, col = row0 + i // _BLOCK_SIZE, col0 + i % _BLOCK_SIZE
            index = (row * _GRID_COLS + col) * 2
            shifts[index], shifts[index + 1] = east, north
    return Ostn15Grid(shifts)


def _write_slice(blocks: dict[tuple[int, int], list[tuple[float, float]]]) -> None:
    """blocks maps (col0, row0) to its 9 (east, north) pairs, row-major,
    northing outer, matching bng.py's own within-node convention.
    """
    body = bytearray()
    for (col0, row0), pairs in blocks.items():
        body += struct.pack("<II", col0, row0)
        for east, north in pairs:
            body += struct.pack("<ff", east, north)
    header = _SLICE_MAGIC + struct.pack("<I", len(blocks))
    _SLICE_PATH.write_bytes(header + bytes(body))


def _read_test_input(path: Path) -> dict[str, tuple[float, float]]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        point_id, lat, lon, _height = line.split(",")
        result[point_id] = (float(lat), float(lon))
    return result


def _read_test_output(path: Path) -> dict[str, tuple[float, float]]:
    result = {}
    for line in path.read_text(encoding="utf-8").splitlines()[1:]:
        fields = line.split(",")
        result[fields[0]] = (float(fields[1]), float(fields[2]))
    return result


def _write_stations(inputs: dict, outputs: dict) -> None:
    lines = ["PointID,ETRS_Lat,ETRS_Lon,Expected_E,Expected_N"]
    for point_id in _CHOSEN_STATIONS:
        lat, lon = inputs[point_id]
        expected_e, expected_n = outputs[point_id]
        lines.append(f"{point_id},{lat},{lon},{expected_e},{expected_n}")
    _STATIONS_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _block_origins(inputs: dict) -> list[tuple[int, int]]:
    """(col0, row0) of each station's block, in a stable, deduplicated order."""
    origins: list[tuple[int, int]] = []
    for point_id in _CHOSEN_STATIONS:
        lat, lon = inputs[point_id]
        easting, northing = tm_forward(lat, lon)
        col0 = math.floor(easting / 1000.0) - _BLOCK_RADIUS
        row0 = math.floor(northing / 1000.0) - _BLOCK_RADIUS
        if (col0, row0) not in origins:
            origins.append((col0, row0))
    return origins


def _extract_blocks(
    data_file: Path, origins: list[tuple[int, int]]
) -> dict[tuple[int, int], list[tuple[float, float]]]:
    """Read the 41 MB data file once, keeping only the wanted blocks' nodes."""
    # Every (col, row) any block needs, mapped back to which block(s) it
    # belongs to and its position within it.
    wanted: dict[tuple[int, int], list[tuple[tuple[int, int], int]]] = {}
    for origin in origins:
        col0, row0 = origin
        for i in range(_BLOCK_SIZE * _BLOCK_SIZE):
            col, row = col0 + i % _BLOCK_SIZE, row0 + i // _BLOCK_SIZE
            wanted.setdefault((col, row), []).append((origin, i))

    nodes_per_block = _BLOCK_SIZE * _BLOCK_SIZE
    found: dict[tuple[int, int], list[tuple[float, float] | None]] = {
        origin: [None] * nodes_per_block for origin in origins
    }

    with data_file.open("r", encoding="utf-8") as handle:
        header = handle.readline().rstrip("\n").split(",")
        if tuple(header[:7]) != _EXPECTED_COLUMNS:
            raise ValueError(f"{data_file}'s columns are not in the expected order.")
        remaining = sum(len(v) for v in wanted.values())
        for line in handle:
            fields = line.rstrip("\n").split(",")
            col = round(float(fields[1]) / 1000.0)
            row = round(float(fields[2]) / 1000.0)
            targets = wanted.get((col, row))
            if targets is None:
                continue
            if int(fields[6]) == _OUTSIDE_DATUM_FLAG:
                shift = (float("nan"), float("nan"))
            else:
                shift = (float(fields[3]), float(fields[4]))
            for origin, i in targets:
                found[origin][i] = shift
            remaining -= len(targets)
            if remaining <= 0:
                break

    result: dict[tuple[int, int], list[tuple[float, float]]] = {}
    for origin, nodes in found.items():
        missing = [i for i, node in enumerate(nodes) if node is None]
        if missing:
            raise ValueError(
                f"Block at {origin} is missing {len(missing)} of "
                f"{nodes_per_block} nodes; is the source data file complete?"
            )
        result[origin] = nodes
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        required=True,
        help="directory holding the pack's TestInput/TestOutput/DataFile .txt files",
    )
    args = parser.parse_args()

    inputs = _read_test_input(args.source / "OSTN15_OSGM15_TestInput_ETRStoOSGB.txt")
    outputs = _read_test_output(args.source / "OSTN15_OSGM15_TestOutput_ETRStoOSGB.txt")
    _write_stations(inputs, outputs)

    origins = _block_origins(inputs)
    blocks = _extract_blocks(args.source / "OSTN15_OSGM15_DataFile.txt", origins)
    _write_slice(blocks)

    print(f"Wrote {_STATIONS_PATH} and {_SLICE_PATH} for {_CHOSEN_STATIONS}.")


if __name__ == "__main__":
    main()
