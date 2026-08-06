"""Builds authority_index.json: HMLR INSPIRE zip name to padded WGS84 bbox.

Run by hand, once, whenever HMLR's authority list or ONS's boundary vintage
changes (like `tests/fixtures/ostn15/make_fixture.py` for OSTN15):

    PYTHONPATH=src python tests/fixtures/inspire/make_authority_index.py

Two live services, hit once each per authority, never touched by the test
suite (test_inspire.py reads only the committed JSON this script writes):

1. HMLR's own download page, `HMLR_DOWNLOAD_PAGE` below, scraped for every
   `/datasets/inspire/download/<Name>.zip` href. Confirmed live 2026-08-06:
   318 hrefs, five of them (Newcastle-under-Lyme, Southend-on-Sea,
   Stockton-on-Tees, Stoke-on-Trent, Stratford-on-Avon) carry a hyphen in
   the place name itself, which is why the name shape this project checks
   is `^[A-Za-z0-9_.-]+$`, not the hyphen-free `^[A-Za-z_.]+$` a first
   reading of the brief suggests: a hyphen is neither a space nor a path
   separator, which is the actual property that matters for a URL path
   segment and a cache filename, and dropping five real authorities to fit
   a stricter class than the one the risk requires would violate "nothing
   is silently dropped" one line after it is stated. A cookieless fetch of
   this page 302-redirects to itself forever; `requests.Session` carries
   the cookie the redirect sets and the retry succeeds, which is why this
   script (like inspire.py's future fetch()) uses a Session throughout
   rather than bare `requests.get`. The service also 403s a client with no
   User-Agent at all (including urllib's default); it does not require a
   browser-shaped one, so this uses an honest, identifying string instead
   of pretending to be a browser.

2. The ONS Open Geography Portal's ArcGIS REST API, `ONS_QUERY_URL` below:
   `Local_Authority_Districts_DEC_2025_Boundaries_UK_BFC`, FeatureServer
   layer 0, fields `LAD25CD` (ONS code, "E"/"W" prefix is England/Wales)
   and `LAD25NM` (name). December 2025 is the latest vintage live on the
   service as of the date above; Wales's 22 principal areas and England's
   districts/boroughs/unitaries are one typology in ONS's data (no second
   Welsh-specific layer needed). Licensed Open Government Licence v3.0,
   same licence the plan already carries for the INSPIRE polygons
   themselves. Every request asks `returnExtentOnly=true` with
   `returnGeometry=false` and `outSR=4326` (WGS84 degrees directly, no BNG
   round trip needed): the response is one small JSON object per
   authority, never the polygon that produced it, which is what keeps a
   319-request run (one name list plus 318 per-authority extents) light on
   a free public service.

## Name matching, and why 314 of 318 need no hand list at all

HMLR's own filenames describe an authority's official TYPE in the name
("Cardiff_Council", "Bridgend_County_Borough_Council",
"London_Borough_of_Camden"); ONS's LAD25NM is the bare place name
("Cardiff", "Bridgend", "Camden"). `_normalise_hmlr` strips every prefix
and suffix in `_HMLR_PREFIXES`/`_HMLR_SUFFIXES` it finds, repeatedly (not
once): "Rutland_County_Council_District_Council" is a real HMLR filename
(Rutland was a district council, then took on the county council's
functions, and the zip name carries both remnants), and stripping only
the first suffix that matches leaves "Rutland_County_Council" rather than
continuing on to "Rutland"; the loop restarts the scan from the first
prefix/suffix every time either list finds one, so this and similar
worst-case double-suffixed names still resolve. `_normalise_ons` handles
the three ONS names that carry a formal ", City of" / ", County of" tail
("Bristol, City of") by dropping the tail entirely, since HMLR's own name
for those never contains the words "city" or "county" once its own suffix
is stripped, and folds out apostrophes and full stops so "King's Lynn"
and "St. Helens" match HMLR's "Kings_Lynn" and "St_Helens".

What is left after both normalisers run, live 2026-08-06, is exactly four
names, each a genuine rename or a sui generis entity rather than a
mechanical mismatch, resolved by hand in `_HAND_RESOLVED_ONS_NAMES` below:

- `City_of_London_Corporation`: the City of London is not a district,
  borough or county in HMLR's own naming scheme at all; ONS's LAD25NM for
  it is plainly "City of London".
- `Durham_County_Council`: the unitary created in 2009 is ONS's "County
  Durham", word order reversed from HMLR's own file name.
- `Hull_City_Council`: ONS's formal LAD25NM is "Kingston upon Hull, City
  of"; HMLR's file uses the city's common name.
- `Newcastle_City_Council`: ONS's LAD25NM is "Newcastle upon Tyne"; same
  shape of mismatch.

A future regeneration that finds a name neither normaliser resolves AND
that is not in `_HAND_RESOLVED_ONS_NAMES` is refused (see `main`): printed
in full and never written into the JSON short, because a silently
short index is a survey that quietly never asks for an authority's
boundaries rather than one that visibly refuses to run.

## The 1 km pad, and why it is applied here rather than at lookup time

`inspire.py`'s `authorities_for` does a plain rectangle intersection
against whatever this script writes; the padding is baked into the stored
bbox rather than added at lookup time so that function stays a pure,
argument-free lookup with no constant of its own to keep in sync with this
script's. `_pad_bbox` reuses `mapgen.geo`'s own equirectangular
projection (`lonlat_to_local_metres`/`local_metres_to_lonlat`) rather than
inventing a second one: the same approximation the rest of mapgen already
relies on for small-area metre arithmetic, referenced to each authority's
own centre latitude, which is the right choice for a single, small,
compact-ish local authority and not for a bbox spanning many degrees of
latitude (none of the 318 do).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from mapgen.geo import lonlat_to_local_metres, local_metres_to_lonlat  # noqa: E402

_FIXTURE_DIR = Path(__file__).resolve().parent
AUTHORITY_INDEX_PATH = _FIXTURE_DIR / "authority_index.json"

_HEADERS = {
    "User-Agent": (
        "mapgen-authority-index-generator/1.0 "
        "(offline survey tool, run by hand once; see make_authority_index.py)"
    )
}

HMLR_DOWNLOAD_PAGE = "https://use-land-property-data.service.gov.uk/datasets/inspire/download"

# Local_Authority_Districts_DEC_2025_Boundaries_UK_BFC, FeatureServer layer
# 0, on ONS's Open Geography Portal ArcGIS hosting. BFC ("Full extent,
# Clipped to the coastline") rather than one of the generalised variants
# (BUC/BGC/BSC): the response this script asks for is only the extent of
# each authority's geometry, so the resolution of the polygon itself costs
# nothing extra, and the full-extent version's envelope is the true one
# rather than one a simplification pass may have shrunk.
ONS_QUERY_URL = (
    "https://services1.arcgis.com/ESMARspQHYMw9BZ9/arcgis/rest/services/"
    "Local_Authority_Districts_DEC_2025_Boundaries_UK_BFC/FeatureServer/0/query"
)

EXPECTED_AUTHORITY_COUNT = 318

# See the module docstring's "1 km pad" section: applied once, here, into
# the committed bboxes.
PAD_METRES = 1000.0

_HMLR_SUFFIXES = [
    "_County_Borough_Council",
    "_Metropolitan_Borough_Council",
    "_Metropolitan_District_Council",
    "_City_and_District_Council",
    "_County_Council",
    "_Borough_Council",
    "_District_Council",
    "_City_Council",
    "_Council",
]

_HMLR_PREFIXES = [
    "London_Borough_of_",
    "Royal_Borough_of_",
    "Borough_Council_of_",
    "Council_of_the_",
    "City_of_",
    "The_",
]

# HMLR name -> exact ONS LAD25NM, for the handful _normalise_hmlr and
# _normalise_ons cannot bring together mechanically. See the module
# docstring for why each one is a genuine rename or a sui generis entity,
# not a normalisation this script failed to write.
_HAND_RESOLVED_ONS_NAMES = {
    "City_of_London_Corporation": "City of London",
    "Durham_County_Council": "County Durham",
    "Hull_City_Council": "Kingston upon Hull, City of",
    "Newcastle_City_Council": "Newcastle upon Tyne",
}

_ONS_OF_SUFFIX = re.compile(r"^(.*),\s*(?:City|County) of$", re.IGNORECASE)


def _normalise_hmlr(name: str) -> str:
    """"Cardiff_Council" -> "cardiff"; see the module docstring for why
    this loops rather than strips once.
    """
    changed = True
    while changed:
        changed = False
        for prefix in _HMLR_PREFIXES:
            if name.startswith(prefix):
                name = name[len(prefix) :]
                changed = True
                break
        for suffix in _HMLR_SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                changed = True
                break
    return re.sub(r"\s+", " ", name.replace("_", " ")).strip().lower()


def _normalise_ons(name: str) -> str:
    """"Bristol, City of" -> "bristol"; "King's Lynn..." -> "kings lynn..."."""
    name = name.replace("'", "").replace(".", "")
    match = _ONS_OF_SUFFIX.match(name.strip())
    if match:
        name = match.group(1)
    name = name.replace(",", " ")
    return re.sub(r"\s+", " ", name).strip().lower()


def fetch_hmlr_names(session: requests.Session) -> list[str]:
    response = session.get(HMLR_DOWNLOAD_PAGE, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    hrefs = re.findall(r'href="([^"]*inspire/download/[^"]*\.zip)"', response.text)
    return sorted(href.rsplit("/", 1)[1][:-4] for href in hrefs)


def fetch_ons_names(session: requests.Session) -> list[tuple[str, str]]:
    """Every England or Wales LAD as (LAD25CD, LAD25NM)."""
    params = {
        "where": "LAD25CD LIKE 'E%' OR LAD25CD LIKE 'W%'",
        "outFields": "LAD25CD,LAD25NM",
        "returnGeometry": "false",
        "resultRecordCount": 2000,
        "f": "json",
    }
    response = session.get(ONS_QUERY_URL, params=params, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    features = response.json()["features"]
    return [(f["attributes"]["LAD25CD"], f["attributes"]["LAD25NM"]) for f in features]


def fetch_ons_extent(
    session: requests.Session, ons_name: str
) -> tuple[float, float, float, float]:
    """(west, south, east, north) in WGS84 degrees, unpadded."""
    escaped = ons_name.replace("'", "''")
    params = {
        "where": f"LAD25NM = '{escaped}'",
        "outFields": "LAD25CD,LAD25NM",
        "returnGeometry": "false",
        "returnExtentOnly": "true",
        "outSR": 4326,
        "f": "json",
    }
    response = session.get(ONS_QUERY_URL, params=params, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    payload = response.json()
    extent = payload.get("extent")
    if not extent or extent.get("xmin") is None:
        raise ValueError(f"ONS returned no extent for {ons_name!r}: {payload}")
    return extent["xmin"], extent["ymin"], extent["xmax"], extent["ymax"]


def _pad_bbox(
    west: float, south: float, east: float, north: float, pad_metres: float = PAD_METRES
) -> tuple[float, float, float, float]:
    ref_lat = (south + north) / 2.0
    x_min, y_min = lonlat_to_local_metres(west, south, ref_lat)
    x_max, y_max = lonlat_to_local_metres(east, north, ref_lat)
    padded_west, padded_south = local_metres_to_lonlat(
        x_min - pad_metres, y_min - pad_metres, ref_lat
    )
    padded_east, padded_north = local_metres_to_lonlat(
        x_max + pad_metres, y_max + pad_metres, ref_lat
    )
    return (
        round(padded_west, 7),
        round(padded_south, 7),
        round(padded_east, 7),
        round(padded_north, 7),
    )


def resolve_ons_names(
    hmlr_names: list[str], ons_names: list[tuple[str, str]]
) -> tuple[dict[str, str], list[str]]:
    """hmlr name -> chosen ONS LAD25NM, and the list that resolved to
    neither a unique automatic match nor a hand-resolved entry.
    """
    by_key: dict[str, list[str]] = {}
    for _code, name in ons_names:
        by_key.setdefault(_normalise_ons(name), []).append(name)

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for hmlr_name in hmlr_names:
        if hmlr_name in _HAND_RESOLVED_ONS_NAMES:
            resolved[hmlr_name] = _HAND_RESOLVED_ONS_NAMES[hmlr_name]
            continue
        candidates = by_key.get(_normalise_hmlr(hmlr_name))
        if candidates and len(candidates) == 1:
            resolved[hmlr_name] = candidates[0]
        else:
            unresolved.append(hmlr_name)
    return resolved, unresolved


def main() -> None:
    session = requests.Session()

    hmlr_names = fetch_hmlr_names(session)
    print(f"HMLR download page: {len(hmlr_names)} authority zips.")

    ons_names = fetch_ons_names(session)
    print(f"ONS {ONS_QUERY_URL}: {len(ons_names)} England/Wales LADs.")

    resolved, unresolved = resolve_ons_names(hmlr_names, ons_names)
    if unresolved:
        print(
            f"{len(unresolved)} HMLR name(s) matched no ONS LAD and are not in "
            f"_HAND_RESOLVED_ONS_NAMES; add them there and rerun. Never written "
            f"out short:"
        )
        for name in unresolved:
            print(f"  {name}")
        raise SystemExit(1)

    index: dict[str, list[float]] = {}
    for hmlr_name in hmlr_names:
        ons_name = resolved[hmlr_name]
        west, south, east, north = fetch_ons_extent(session, ons_name)
        padded = _pad_bbox(west, south, east, north)
        index[hmlr_name] = list(padded)
        print(f"{hmlr_name} -> {ons_name!r}: {padded}")

    if len(index) != EXPECTED_AUTHORITY_COUNT:
        raise SystemExit(
            f"Resolved {len(index)} authorities, expected {EXPECTED_AUTHORITY_COUNT}. "
            f"Refusing to write a short index."
        )

    AUTHORITY_INDEX_PATH.write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"Wrote {AUTHORITY_INDEX_PATH} ({len(index)} authorities).")


if __name__ == "__main__":
    main()
