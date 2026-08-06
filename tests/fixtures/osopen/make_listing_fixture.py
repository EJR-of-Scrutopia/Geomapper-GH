"""Builds downloads_listing.json: a trimmed OS Data Hub downloads listing.

Run by hand whenever the fixture needs regenerating (there is no live
source this script reads; see below):

    PYTHONPATH=src python tests/fixtures/osopen/make_listing_fixture.py

The real endpoint this fixture stands in for is
`GET https://api.os.uk/downloads/v1/products/OpenGreenspace/downloads`,
probed live 2026-08-07 (see task-1-brief.md for this task). Only the SS
entry below is transcribed from that probe field for field, verbatim,
because that is the one entry the brief pins exactly. The GB and ST
entries are constructed to the identical field shape (same keys, same
value types, the same url query-string pattern with area substituted) so
the fixture holds three entries the way the real listing does, one
national file and two of the per-square tiles Open Greenspace also
publishes; their md5 and size values are illustrative, not independently
probed, and nothing in this project treats them as measured constants.
`downloads_listing.json` is committed alongside this script; nothing
about it is generated dynamically from a live call.
"""

from __future__ import annotations

import json
from pathlib import Path

_FIXTURE_PATH = Path(__file__).resolve().parent / "downloads_listing.json"

_URL_TEMPLATE = (
    "https://api.os.uk/downloads/v1/products/OpenGreenspace/downloads"
    "?area={area}&format=GML&subformat=3&redirect"
)

# The SS entry is the brief's own verbatim fixture entry. GB and ST are
# constructed to the same shape; see the module docstring.
_ENTRIES = [
    {
        "md5": "a13f2c9e6b4d0871c5e93a2f7d160c4b",
        "size": 8_741_209,
        "url": _URL_TEMPLATE.format(area="GB"),
        "format": "GML",
        "subformat": "3",
        "area": "GB",
        "fileName": "opgrsp_gml3_gb.zip",
    },
    {
        "md5": "e087721f040e06bdaad6a943c92d0539",
        "size": 631663,
        "url": _URL_TEMPLATE.format(area="SS"),
        "format": "GML",
        "subformat": "3",
        "area": "SS",
        "fileName": "opgrsp_gml3_ss.zip",
    },
    {
        "md5": "7d4a915e2b6c8f01934dabc5e1f0a9d2",
        "size": 2_205_311,
        "url": _URL_TEMPLATE.format(area="ST"),
        "format": "GML",
        "subformat": "3",
        "area": "ST",
        "fileName": "opgrsp_gml3_st.zip",
    },
]


def main() -> None:
    _FIXTURE_PATH.write_text(
        json.dumps(_ENTRIES, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Wrote {_FIXTURE_PATH} ({len(_ENTRIES)} entries).")


if __name__ == "__main__":
    main()
