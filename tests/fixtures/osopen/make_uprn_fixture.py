"""Builds uprn_sample.csv: a tiny, BOM-carrying OpenUPRN CSV fixture for
test_os_shards.py's write_uprn_shards/uprn_in tests.

Run by hand whenever the fixture needs regenerating:

    PYTHONPATH=src python tests/fixtures/osopen/make_uprn_fixture.py

The real OpenUPRN header, verified live against the 2026-08 product
(this task's own plan, "Owner ground truth" section): `UPRN,
X_COORDINATE,Y_COORDINATE,LATITUDE,LONGITUDE`, one row per address, a
leading UTF-8 BOM, rows ordered by UPRN rather than spatially.

Row 1 is the real first row of that same live download, copied verbatim
(UPRN 1, easting 358260.99, northing 172796.83, the OS-computed WGS84
latitude/longitude alongside it): this project's own square ST, the
Cowbridge/Vale of Glamorgan reference area's eastern square. Rows 2-6 are
constructed (illustrative UPRNs and coordinates, not five more real
downloads), placed by hand three in square SS and three in ST so the
fixture straddles the SS/ST seam the way the brief's own Step 5 asks:
SS spans easting 200000-300000, ST spans 300000-400000, both restricted
here to northing 100000-200000, so every row sits inside one of the two
squares without needing to touch a third.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

_FIXTURE_PATH = Path(__file__).resolve().parent / "uprn_sample.csv"

_HEADER = ["UPRN", "X_COORDINATE", "Y_COORDINATE", "LATITUDE", "LONGITUDE"]

# (UPRN, easting, northing, latitude, longitude). Row 1 is the real live
# row this task's own brief names verbatim; the rest are constructed to
# the same field shape, three per square.
_ROWS = [
    (1, 358260.99, 172796.83, 51.4526038, -2.6020703),  # real, ST
    (2, 299500.12, 179500.55, 51.4980112, -3.1500223),  # constructed, SS
    (3, 301200.40, 179200.75, 51.4970050, -3.1300110),  # constructed, ST
    (4, 295000.50, 165000.25, 51.3850040, -3.1950075),  # constructed, SS
    (5, 320000.75, 190000.10, 51.5800030, -2.9450060),  # constructed, ST
    (6, 285000.33, 155000.66, 51.3400020, -3.2200015),  # constructed, SS
]


def main() -> None:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_HEADER)
    for row in _ROWS:
        writer.writerow(row)

    # utf-8-sig, not utf-8: this fixture must actually carry the BOM byte
    # sequence (EF BB BF) at its start, matching the real OpenUPRN file,
    # since the whole point of the "BOM stripped" test is a file that
    # genuinely has one.
    _FIXTURE_PATH.write_text(buffer.getvalue(), encoding="utf-8-sig", newline="")
    print(f"Wrote {_FIXTURE_PATH} ({_FIXTURE_PATH.stat().st_size} bytes).")


if __name__ == "__main__":
    main()
