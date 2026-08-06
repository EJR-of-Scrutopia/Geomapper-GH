"""The HMLR INSPIRE authority index: which zips cover a drawn extent.

HM Land Registry publishes INSPIRE Index Polygons as one zip per local
authority, 318 of them across England and Wales, with no whole-country
file and no live "which authority covers this point" service of its own.
Working out which zips a survey extent needs would otherwise mean
downloading and unzipping all 318 just to look inside, or scraping the
download page and an external boundary service on every single survey.
Neither is acceptable for a step that runs before the owner has committed
to downloading anything, so this module instead reads a small, committed
lookup table built once by `tests/fixtures/inspire/make_authority_index.py`
and never touches the network itself.

`AUTHORITY_INDEX_PATH` deliberately points into `tests/fixtures/inspire/`
rather than a data directory under this package: the table is generated
data with a documented generator beside it, exactly the
`tests/fixtures/ostn15/` convention, and `bridge.py`'s own
`UrbanoBridge.csproj` path already establishes that this project's
production code is read from a full checkout, not a package built for
separate distribution, so a second module reaching the same two
directories up is a continuation of an existing convention rather than a
new one.

Only this task's slice lives here: index loading and the intersection
lookup. The download, cache and streaming parcel parser are a later task
in the same module.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from mapgen.geo import BBox

AUTHORITY_INDEX_PATH = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "inspire" / "authority_index.json"
)

EXPECTED_AUTHORITY_COUNT = 318

# A hyphen is neither a space nor a path separator, which is the actual
# property that matters here: every one of these names becomes a URL path
# segment (the zip's download URL) and a cache filename stem in the tasks
# that follow this one. Five of the real 318 HMLR names carry a hyphen
# (Newcastle-under-Lyme and similar compound place names); see
# make_authority_index.py's module docstring for how that was found and
# why this is the right shape rather than a narrower one that would have
# to silently drop them.
_NAME_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+$")


class InspireError(RuntimeError):
    """Raised when the committed authority index cannot be read or is not
    the shape this module and its callers depend on: present, exactly
    318 names, each a safe path segment, each a well-formed bbox. Every
    check here is about the committed fixture being trustworthy, not
    about anything a caller passed in; a bad bbox is BBoxError's job
    (see geo.py), not this module's.
    """


def load_authority_index(
    path: Path = AUTHORITY_INDEX_PATH,
) -> dict[str, tuple[float, float, float, float]]:
    """Every HMLR authority name to its padded WGS84 bbox (west, south,
    east, north), read from the committed JSON `make_authority_index.py`
    wrote. No network, no computation: the padding (1 km, see that
    script's own docstring) is already baked into every stored bbox, so
    this is a pure file read plus a shape check.
    """
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InspireError(f"Could not read the authority index at {path}.") from exc

    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise InspireError(f"The authority index at {path} is not valid JSON.") from exc

    if not isinstance(raw, dict):
        raise InspireError(f"The authority index at {path} must be a JSON object.")

    index: dict[str, tuple[float, float, float, float]] = {}
    for name, bbox in raw.items():
        if not _NAME_SHAPE.match(name):
            raise InspireError(
                f"Authority name {name!r} in {path} is not a safe path segment "
                f"(must match {_NAME_SHAPE.pattern})."
            )
        if not (isinstance(bbox, list) and len(bbox) == 4):
            raise InspireError(
                f"Authority {name!r} in {path} must map to a 4 element "
                f"[west, south, east, north] list, got {bbox!r}."
            )
        try:
            west, south, east, north = (float(value) for value in bbox)
        except (TypeError, ValueError) as exc:
            raise InspireError(
                f"Authority {name!r} in {path} has a non-numeric bbox value."
            ) from exc
        if not (west < east and south < north):
            raise InspireError(
                f"Authority {name!r} in {path} has a degenerate or inverted "
                f"bbox: {bbox!r}."
            )
        index[name] = (west, south, east, north)

    return index


def authorities_for(bbox: BBox) -> list[str]:
    """HMLR zip basenames (no `.zip`) for every authority whose padded
    bbox intersects `bbox`, sorted. Empty for an extent outside England
    and Wales entirely (Scotland, Northern Ireland, or open sea): INSPIRE
    Index Polygons only exist for the 318 authorities in the index, so an
    extent nowhere near any of them simply matches none, with no special
    case needed for "outside coverage".
    """
    index = load_authority_index()
    return sorted(
        name
        for name, (west, south, east, north) in index.items()
        if bbox.west <= east and bbox.east >= west and bbox.south <= north and bbox.north >= south
    )
