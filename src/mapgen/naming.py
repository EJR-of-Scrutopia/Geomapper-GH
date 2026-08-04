"""Slugs, package folder layout, and the Windows path length guard."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

from mapgen.elevation_models import DEFAULT_DEMTYPE, work_file_name

MAX_COMPONENT_LENGTH = 40
DEFAULT_PATH_LIMIT = 240
FINGERPRINT_LENGTH = 8

_DISALLOWED = re.compile(r"[^A-Za-z0-9-]")
_REPEATED_HYPHEN = re.compile(r"-{2,}")

# Characters unicodedata does not decompose into ASCII on its own.
_TRANSLITERATIONS = str.maketrans(
    {
        "ŵ": "w", "Ŵ": "W", "ŷ": "y", "Ŷ": "Y",
        "æ": "ae", "Æ": "AE", "ø": "o", "Ø": "O",
        "ß": "ss", "đ": "d", "Đ": "D", "ł": "l", "Ł": "L",
    }
)


class NamingError(ValueError):
    """Raised when a name cannot be turned into a usable path component."""


class PathTooLongError(NamingError):
    """Raised when a job would produce paths beyond the Windows limit."""


def slugify(value: str, field: str, max_length: int = MAX_COMPONENT_LENGTH) -> str:
    decomposed = unicodedata.normalize("NFKD", value.translate(_TRANSLITERATIONS))
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    hyphenated = re.sub(r"\s+", "-", ascii_only.strip())
    cleaned = _DISALLOWED.sub("", hyphenated)
    collapsed = _REPEATED_HYPHEN.sub("-", cleaned).strip("-")
    capped = collapsed[:max_length].strip("-")
    if not capped:
        raise NamingError(
            f"The {field} name {value!r} contains no usable characters. "
            f"Use letters or numbers."
        )
    return capped


def tiling_fingerprint(
    west: float,
    south: float,
    east: float,
    north: float,
    tile_size_m: float,
    overlap_m: float,
    categories: Sequence[str],
    overture_types: Sequence[str],
) -> str:
    """Short, stable identifier for a specific tiling AND content selection.

    A tile id such as r00_c00 names a row and column of some grid, but the
    string carries no memory of which grid: that depends on the bbox,
    tile_size_m and overlap_m, and two different tilings can and do produce
    the same row/column names for different ground. This fingerprint is
    what tells them apart at the filesystem level, so they are never able
    to share a directory in the first place, rather than being detected and
    rejected after the fact.

    A coordinator review's Important 1 finding: the six tiling numbers
    were never the whole story once Task 19 added category and Overture
    type filtering. Two requests over the same bbox at the same tiling
    but a DIFFERENT category selection used to land in the SAME _work/
    fingerprint directory, because nothing about the selection was ever
    part of what got hashed. Resuming a job after narrowing the
    selection (or widening it) then mixed old, differently-filtered
    tiles into a package whose survey.json went on to describe only the
    NEW selection, silently: a stale _water.geojson surviving a
    buildings-only rerun, or an OSM tile fetched under one Overpass
    filter reused under a since-changed one. categories and
    overture_types are the two REQUIRED parameters that close this:
    always the effective, already-resolved selection (see SurveyRequest.
    effective_categories/effective_overture_types), never the possibly-
    None raw request field, and always passed, with no default, so a
    future call site cannot forget them the way every existing one
    already had to be found and fixed once. Sorted before joining so the
    same selection fingerprints identically regardless of the order its
    caller happened to list it in.

    The canonical string is built with fixed-precision formatting rather
    than repr() or str(), which are not guaranteed stable across floats
    that are numerically equal but differently represented. The digest is
    truncated to FINGERPRINT_LENGTH hex characters: short enough to keep
    paths well inside the Windows limit, long enough that an accidental
    collision between genuinely different tilings is not a realistic
    concern for this tool's scale.
    """
    canonical = "|".join(
        [
            *(f"{value:.7f}" for value in (west, south, east, north, tile_size_m, overlap_m)),
            ",".join(sorted(categories)),
            ",".join(sorted(overture_types)),
        ]
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


@dataclass(frozen=True)
class PackagePaths:
    root: Path
    stem: str
    layers_dir: Path
    work_dir: Path
    survey_json: Path
    project_setting: Path


def _compose(root: Path, stem: str, fingerprint: str) -> PackagePaths:
    return PackagePaths(
        root=root,
        stem=stem,
        layers_dir=root / "layers",
        work_dir=root / "_work" / fingerprint,
        survey_json=root / "survey.json",
        project_setting=root / f"{stem}_project_setting.json",
    )


def build_package_paths(
    output_root: Path,
    region: str,
    site: str,
    survey_date: date,
    fingerprint: str,
    stem_override: str | None = None,
) -> PackagePaths:
    region_slug = slugify(region, "region")
    site_slug = slugify(site, "site")
    date_str = survey_date.isoformat()

    region_dir = Path(output_root) / region_slug
    base_name = f"{date_str}_{site_slug}"

    root = region_dir / base_name
    suffix = ""
    counter = 2
    # A candidate folder is only skipped if it is a genuinely finished
    # package. Anything else, absent, unreadable or incomplete survey.json,
    # means an earlier run stopped partway through, so that folder is reused:
    # same root, same stem. work_dir is keyed by fingerprint underneath that
    # root, so a reused root with a different tiling gets its own, entirely
    # separate _work/<fingerprint>/ rather than colliding with whatever an
    # earlier, differently-tiled attempt left behind. The completeness check
    # applies to every suffixed candidate in turn, so a _02 that is itself
    # incomplete gets reused rather than pushed on to _03.
    while root.exists() and _survey_reports_complete(root / "survey.json"):
        suffix = f"_{counter:02d}"
        root = region_dir / f"{base_name}{suffix}"
        counter += 1

    stem = f"{stem_override}{suffix}" if stem_override else f"{site_slug}_{date_str}{suffix}"
    return _compose(root, stem, fingerprint)


def _survey_reports_complete(survey_json: Path) -> bool:
    """True only when survey_json exists, parses, and says complete: true.

    Missing, unreadable, malformed json, or complete: false all collapse to
    the same answer, because build_package_paths only needs to distinguish
    "safe to reuse" from "must not touch this, it is someone else's finished
    work", and every one of those cases is the former.
    """
    try:
        payload = json.loads(survey_json.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("complete") is True


def check_path_length(
    paths: PackagePaths,
    overture_types: Sequence[str],
    source_ids: Sequence[str] | None = None,
    limit: int = DEFAULT_PATH_LIMIT,
    elevation_demtype: str = DEFAULT_DEMTYPE,
) -> None:
    """Raises PathTooLongError if this job's worst-case path would be too long.

    source_ids is the sources the job actually requested. Each source's raw
    path has a different, fixed shape, and only a selected source's shape is
    a path this job can ever produce. source_ids=None, the default, means
    "unknown, assume every source": every caller inside this codebase always
    knows the job's real source_ids and passes it, so None only arises from
    a caller (or a test) that has not been told, and the safe, conservative
    answer for an unknown job is to check all three rather than silently
    under counting one of them.

    Overture's shape is "raw/overture/<longest type>.geojson" (Task 23). It
    used to nest an extra "<type>/" segment above a per-tile
    "rNN_cNN.geojson", which made it reliably the longest of the three;
    untiled it is 8 characters shorter, and still the longest, since a type
    name plus ".geojson" beats "r00_c00.osm". Measured from the real shape
    rather than left at the old one deliberately: a guard that is merely
    conservative still names a path in its error message, and naming a path
    the tool can no longer produce sends the owner looking for a file that
    will never exist.

    elevation_demtype is here for the same "measure the real shape" reason
    (Task 28). Elevation's raw file used to be a fixed "elevation.tif";
    it now carries the chosen model's name, so the path this job can
    actually produce is up to sixteen characters longer than the one this
    guard used to measure, and a guard that under counts is a guard that
    admits a job Windows will refuse. Defaulted rather than required, so
    every existing caller and test keeps working and only gets a longer
    candidate when it actually says the job asked for a longer one.

    project_setting is always a candidate: it exists for a package
    regardless of which sources it contains, since naming.py builds it
    unconditionally.
    """
    selected = None if source_ids is None else set(source_ids)

    def _length(path: Path) -> int:
        return len(str(path.resolve() if not path.is_absolute() else path))

    # Ordered so a tie is broken exactly as it always has been: Overture's
    # nested shape wins a tie over project_setting, which in turn wins over
    # the (previously nonexistent) osm/elevation candidates added below.
    candidates: list[tuple[int, Path]] = []

    if selected is None or "overture" in selected:
        longest_type = max(overture_types, key=len) if overture_types else "overture"
        overture_path = paths.work_dir / "raw" / "overture" / f"{longest_type}.geojson"
        candidates.append((_length(overture_path), overture_path))

    candidates.append((_length(paths.project_setting), paths.project_setting))

    if selected is None or "osm" in selected:
        osm_path = paths.work_dir / "raw" / "osm" / "r00_c00.osm"
        candidates.append((_length(osm_path), osm_path))

    if selected is None or "elevation" in selected:
        elevation_path = (
            paths.work_dir / "raw" / "elevation" / work_file_name(elevation_demtype)
        )
        candidates.append((_length(elevation_path), elevation_path))

    length, longest_candidate = candidates[0]
    for candidate_length, candidate_path in candidates[1:]:
        if candidate_length > length:
            length, longest_candidate = candidate_length, candidate_path

    if length > limit:
        raise PathTooLongError(
            f"This job would produce paths of {length} characters, over the "
            f"{limit} character limit. The longest path would be:\n  {longest_candidate}\n"
            f"Choose a shorter output root, or shorten the region or site name."
        )
