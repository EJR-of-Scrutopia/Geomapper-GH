"""Builds parcels_sample.gml: a tiny, real slice of an INSPIRE Index
Polygons download, for test_inspire.py's offline parser tests.

Run by hand, once, against a real authority zip already sitting on disk
(`inspire.fetch_authority_zip` or a browser download both produce the
right shape):

    PYTHONPATH=src python tests/fixtures/inspire/make_gml_fixture.py \
        --source /path/to/Vale_of_Glamorgan_Council.zip

Provenance of the committed fixture: `Vale_of_Glamorgan_Council.zip`,
downloaded 2026-08-06 (13,689,747 bytes, the same live handshake
`fetch_authority_zip` uses), whose `Land_Registry_Cadastral_Parcels.gml`
member carries `numberMatched="65276"` and `timeStamp=
"2026-08-02T03:47:41.090Z"` (the file's own August 2026 first-Sunday
publication run). Both are copied into the fixture's own root verbatim,
so test_inspire.py's assertion on `.timestamp_year` is checking a real
date rather than one this script invented.

## Which members, and why

The first 9 `wfs:member` elements in that file, plus member 28 (1-indexed
in document order), for two different reasons:

- Members 1, 2 and 3 (INSPIREID 16721537, 16721605, 57560830) are three
  neighbouring parcels that share edges in both directions: 1 and 2 share
  the edge between (313548.47, 168920.19) and (313538.26, 168925.58)
  (present in parcel 1's ring as ...313548.47 168920.19, 313538.26
  168925.58... and in parcel 2's ring, reversed, as ...313533.65 168928.01,
  313538.26 168925.58, 313548.47 168920.19, closing the ring); 2 and 3
  share a second edge the same way, between (313550.93, 168927.41) and
  (313540.67, 168932.99). Either pair is exactly the "two parcels sharing
  an edge" fixture Task 3's dedup test needs; both are kept for margin.
- Member 28 (INSPIREID 16722605) is the first parcel in the file with a
  `gml:interior` ring: a small internal ring, a real courtyard shape, is
  what exercises this module's own interior-ring parsing and Task 3's
  "an interior ring survives as its own closed curve" test. Found by a
  one-off scan of the real file (not re-run by this script, which only
  ever reads the specific member indices below), recorded here so a
  future regeneration from a different authority knows what it is looking
  for if it needs to re-find one.

Taking a short, mostly-contiguous prefix of the real file rather than
picking scattered members: `iterparse` reads members in document order and
this script exits as soon as it has read past the highest index it needs
(28), so building the fixture costs one short pass over the front of the
zip, never the full 81 MB the whole file actually holds.

## What is deliberately dropped from the real root's attributes

The real file's root also carries `xmlns:xs`, `xmlns:xsi` and an
`xsi:schemaLocation` pointing at HM Land Registry's own internal GeoServer
host. None of the three is read by `inspire.py`'s parser (namespace
declarations for schema validation this project never does), so they are
left off the fixture's own root to keep it a plain, small file with
nothing in it this project has a reason to touch. `xmlns:wfs`, `xmlns:gml`,
`xmlns:LR`, `numberMatched`, `numberReturned` and `timeStamp` are kept
because the parser and its tests read every one of them.
"""

from __future__ import annotations

import argparse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

WFS_NS = "http://www.opengis.net/wfs/2.0"
GML_NS = "http://www.opengis.net/gml/3.2"
LR_NS = "www.landregistry.gov.uk"

_MEMBER_TAG = f"{{{WFS_NS}}}member"
GML_MEMBER_NAME = "Land_Registry_Cadastral_Parcels.gml"

_FIXTURE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = _FIXTURE_DIR / "parcels_sample.gml"

# 1-indexed positions in the source file's own document order. See the
# module docstring for what each one is chosen for.
_FIRST_N_MEMBERS = 9
_EXTRA_MEMBER_INDEX = 28


def _register_namespaces() -> None:
    # Without this, ET.tostring invents ns0/ns1/ns2 prefixes: valid XML,
    # but not the wfs:/gml:/LR: shape inspire.py's own Clark-notation
    # constants are written against, and not what a human comparing this
    # fixture to the real file would expect to see.
    ET.register_namespace("wfs", WFS_NS)
    ET.register_namespace("gml", GML_NS)
    ET.register_namespace("LR", LR_NS)


def extract_members(source_zip: Path) -> tuple[str, list[ET.Element]]:
    """The root's timeStamp string, and the chosen members' Elements, read
    from the real zip's GML member with a short iterparse pass.

    Stops as soon as `_EXTRA_MEMBER_INDEX` has been read, never touching
    the rest of the file: see the module docstring's "why a short prefix"
    section.
    """
    members: list[ET.Element] = []
    with zipfile.ZipFile(source_zip) as archive, archive.open(GML_MEMBER_NAME) as stream:
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)
        time_stamp = root.get("timeStamp")
        if not time_stamp:
            raise ValueError(f"{source_zip}'s root has no timeStamp attribute.")

        count = 0
        for event, elem in context:
            if event != "end" or elem.tag != _MEMBER_TAG:
                continue
            count += 1
            if count <= _FIRST_N_MEMBERS or count == _EXTRA_MEMBER_INDEX:
                members.append(elem)
            if count >= _EXTRA_MEMBER_INDEX:
                break
    return time_stamp, members


def build_fixture_tree(time_stamp: str, members: list[ET.Element]) -> ET.ElementTree:
    _register_namespaces()
    root = ET.Element(
        f"{{{WFS_NS}}}FeatureCollection",
        {
            "numberMatched": str(len(members)),
            "numberReturned": str(len(members)),
            "timeStamp": time_stamp,
        },
    )
    for member in members:
        root.append(member)
    return ET.ElementTree(root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", required=True, type=Path, help="A real HMLR INSPIRE authority zip."
    )
    args = parser.parse_args()

    time_stamp, members = extract_members(args.source)
    if len(members) < _FIRST_N_MEMBERS:
        raise SystemExit(
            f"{args.source} has only {len(members)} members before position "
            f"{_EXTRA_MEMBER_INDEX}; expected at least {_FIRST_N_MEMBERS}."
        )

    tree = build_fixture_tree(time_stamp, members)
    tree.write(OUTPUT_PATH, encoding="utf-8", xml_declaration=True)
    print(f"Wrote {OUTPUT_PATH} ({len(members)} members, timeStamp {time_stamp!r}).")


if __name__ == "__main__":
    main()
