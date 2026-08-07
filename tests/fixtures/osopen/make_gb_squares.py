"""Prints GB_SQUARES: every 100 km National Grid square OS Open's own
per-square products actually serve, for pasting into os_shards.py by hand.

Run by hand whenever this needs regenerating (there is no live source this
project reads at runtime; the printed literal is committed into os_shards.py
itself, not into a data file this project loads), the same "hits a real
service once, commits the output" shape
tests/fixtures/inspire/make_authority_index.py already establishes for its
own 318-authority index:

    PYTHONPATH=src python tests/fixtures/osopen/make_gb_squares.py

Hits the one real, keyless endpoint every OS Open per-square product in this
project already reads through (os_downloads.py's own module docstring: "OS
Open... serves keyless"), `GET https://api.os.uk/downloads/v1/products/
OpenMapLocal/downloads`, and keeps only the entries whose `format` is `"GML"`
(the one format this project's own os_open.py/os_uprn.py ever download; the
same listing also carries ESRI Shapefile, GeoPackage and GeoTIFF entries for
each area, which would not change the square set but would be counting
something this project never fetches). Probed live 2026-08-07: 169 total
entries across four formats, 56 of them GML, one named area "GB" (the whole-
country shapefile/GML/GeoPackage/GeoTIFF bundle OpenMapLocal also publishes
as a single national file, not a 100 km square), leaving 55 real squares.

OS Open UPRN (`os_uprn.py`) is not queried separately: it publishes one
national CSV file only, with no per-square entries at all (see that
module's own docstring, "One national file, not one per square"), so it has
no area codes of its own to contribute to this set; its own `covers()`
(Task 7 of the tier resolver plan) reads this same GB_SQUARES constant
because the national file it serves covers exactly the same 55 squares
OpenMapLocal's own per-square listing does, the whole of Great Britain as
OS's own National Grid actually tiles it, not because it was itself probed
here.

OpenRoads and OpenGreenspace are not queried either: os_open.py's own
PRODUCTS tuple downloads all three products over the SAME set of squares a
survey's own padded extent touches (`squares_for`), and `_BYTES_PER_SQUARE`
already prices every one of them per square: nothing in this project ever
asks "which squares does OpenRoads serve" as a question separate from "which
squares does OpenMapLocal serve", so a second, third and fourth probe here
would only re-confirm the same 55, not find a different set.
"""

from __future__ import annotations

import requests

_ENDPOINT = "https://api.os.uk/downloads/v1/products/OpenMapLocal/downloads"


def fetch_gb_squares(session: requests.Session) -> list[str]:
    response = session.get(_ENDPOINT, timeout=30)
    response.raise_for_status()
    entries = response.json()
    areas = {entry["area"] for entry in entries if entry.get("format") == "GML"}
    areas.discard("GB")
    return sorted(areas)


def main() -> None:
    squares = fetch_gb_squares(requests.Session())
    print(f"{len(squares)} squares (excluding the national 'GB' entry).")
    print("Paste this into os_shards.py's own GB_SQUARES:")
    print()
    print("GB_SQUARES = frozenset(")
    print("    {")
    for i in range(0, len(squares), 11):
        row = ", ".join(f'"{square}"' for square in squares[i : i + 11])
        print(f"        {row},")
    print("    }")
    print(")")


if __name__ == "__main__":
    main()
