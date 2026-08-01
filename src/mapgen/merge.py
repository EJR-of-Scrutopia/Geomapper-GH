"""Merging overlapping tile downloads into single deduplicated outputs.

Tiles are downloaded with an overlap margin so features near a seam are not
clipped, which means the same feature arrives more than once. OSM elements are
keyed on type and id, keeping the highest version. GeoJSON features are keyed on
feature id, keeping the first seen.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from collections import OrderedDict
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from mapgen.fsutil import atomic_writer

OSM_TYPE_ORDER = ("node", "way", "relation")


class MergeError(RuntimeError):
    """Raised when the inputs to a merge are incomplete or unusable."""


def assert_inputs_present(expected: Sequence[Path], force: bool = False) -> list[Path]:
    usable: list[Path] = []
    problems: list[str] = []
    for path in expected:
        if not path.exists():
            problems.append(f"missing: {path.name}")
        elif path.stat().st_size == 0:
            problems.append(f"empty: {path.name}")
        else:
            usable.append(path)

    if problems and not force:
        joined = "\n  ".join(problems)
        raise MergeError(
            f"Refusing to merge an incomplete tile set:\n  {joined}\n"
            f"Re-run the download to fill the gaps, or pass force to merge anyway "
            f"and record the package as incomplete."
        )
    return usable


def _element_rank(element: ET.Element) -> int:
    try:
        return int(element.get("version", "0"))
    except ValueError:
        return 0


def _element_id_or_zero(element: ET.Element) -> int:
    try:
        return int(element.get("id", "0"))
    except ValueError:
        return 0


def merge_osm_xml(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, ET.Element] = OrderedDict()

    for input_path in input_paths:
        root = ET.parse(input_path).getroot()
        for element in root:
            if element.tag not in OSM_TYPE_ORDER:
                continue
            element_id = element.get("id")
            if element_id is None:
                continue
            key = f"{element.tag}/{element_id}"
            existing = deduped.get(key)
            if existing is None or _element_rank(element) > _element_rank(existing):
                deduped[key] = element

    ordered = sorted(
        deduped.values(),
        key=lambda el: (OSM_TYPE_ORDER.index(el.tag), _element_id_or_zero(el)),
    )

    with atomic_writer(output_path) as handle:
        handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        handle.write('<osm version="0.6" generator="mapgen">\n')
        for element in ordered:
            handle.write("  ")
            handle.write(ET.tostring(element, encoding="unicode"))
            handle.write("\n")
        handle.write("</osm>\n")

    return len(ordered)


def _iter_features(path: Path) -> Iterator[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    for index, feature in enumerate(payload.get("features", [])):
        top_level_id = feature.get("id")
        if top_level_id is not None:
            feature_id = top_level_id
        else:
            props = (feature.get("properties") or {})
            feature_id = props.get("id")
            if feature_id is None:
                raise MergeError(
                    f"{path.name} feature {index} has no id at top level or in properties"
                )
        yield str(feature_id), json.dumps(feature, separators=(",", ":"))


def merge_geojson(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, str] = OrderedDict()
    for input_path in input_paths:
        for feature_id, compact in _iter_features(input_path):
            deduped.setdefault(feature_id, compact)

    with atomic_writer(output_path) as handle:
        handle.write('{"type":"FeatureCollection","features":[')
        for index, compact in enumerate(deduped.values()):
            if index:
                handle.write(",")
            handle.write(compact)
        handle.write("]}\n")

    return len(deduped)
