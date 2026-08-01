"""Slugs, package folder layout, and the Windows path length guard."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Sequence

MAX_COMPONENT_LENGTH = 40
DEFAULT_PATH_LIMIT = 240

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


@dataclass(frozen=True)
class PackagePaths:
    root: Path
    stem: str
    layers_dir: Path
    work_dir: Path
    survey_json: Path
    project_setting: Path


def _compose(root: Path, stem: str) -> PackagePaths:
    return PackagePaths(
        root=root,
        stem=stem,
        layers_dir=root / "layers",
        work_dir=root / "_work",
        survey_json=root / "survey.json",
        project_setting=root / f"{stem}_project_setting.json",
    )


def build_package_paths(
    output_root: Path,
    region: str,
    site: str,
    survey_date: date,
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
    while root.exists():
        suffix = f"_{counter:02d}"
        root = region_dir / f"{base_name}{suffix}"
        counter += 1

    stem = f"{stem_override}{suffix}" if stem_override else f"{site_slug}_{date_str}{suffix}"
    return _compose(root, stem)


def check_path_length(
    paths: PackagePaths,
    overture_types: Sequence[str],
    limit: int = DEFAULT_PATH_LIMIT,
) -> None:
    longest_type = max(overture_types, key=len) if overture_types else "overture"
    overture_path = paths.work_dir / "raw" / "overture" / longest_type / "r00_c00.geojson"
    overture_length = len(str(overture_path.resolve() if not overture_path.is_absolute() else overture_path))

    project_setting_length = len(str(paths.project_setting.resolve() if not paths.project_setting.is_absolute() else paths.project_setting))

    longest_candidate = overture_path if overture_length >= project_setting_length else paths.project_setting
    length = max(overture_length, project_setting_length)

    if length > limit:
        raise PathTooLongError(
            f"This job would produce paths of {length} characters, over the "
            f"{limit} character limit. The longest path would be:\n  {longest_candidate}\n"
            f"Choose a shorter output root, or shorten the region or site name."
        )
