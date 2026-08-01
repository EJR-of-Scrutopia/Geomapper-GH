from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

import requests

try:
    from overturemaps import get_all_overture_types
except ImportError:  # pragma: no cover - handled at runtime
    get_all_overture_types = None


EARTH_RADIUS_M = 6378137.0
DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
DEFAULT_OVERPASS_FALLBACK_URLS = [
    "https://overpass.private.coffee/api/interpreter",
]
DEFAULT_OSM_API_URL = "https://api.openstreetmap.org/api/0.6/map"
DEFAULT_OPENTOPOGRAPHY_URL = "https://portal.opentopography.org/API/globaldem"
DEFAULT_USER_AGENT = "MoveLayerTool-OSM-Overture-Downloader/0.1"
DEFAULT_OVERTURE_TYPES = [
    "building",
    "place",
    "segment",
    "connector",
    "infrastructure",
    "land_use",
    "land_cover",
    "water",
]
OSM_TYPE_ORDER = ("node", "way", "relation")
SCRIPT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Tile:
    tile_id: str
    row: int
    col: int
    core_bbox: tuple[float, float, float, float]
    query_bbox: tuple[float, float, float, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "tile_id": self.tile_id,
            "row": self.row,
            "col": self.col,
            "core_bbox": bbox_to_dict(self.core_bbox),
            "query_bbox": bbox_to_dict(self.query_bbox),
        }


def parse_bbox(value: str) -> tuple[float, float, float, float]:
    parts = [part.strip() for part in value.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            "BBox must contain 4 comma-separated numbers: west,south,east,north"
        )

    try:
        raw_west, raw_second, raw_east, raw_fourth = [float(part) for part in parts]
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid bbox: {value}") from exc

    west, east = sorted((raw_west, raw_east))
    south, north = sorted((raw_second, raw_fourth))
    validate_bbox((west, south, east, north))
    return west, south, east, north


def validate_bbox(bbox: tuple[float, float, float, float]) -> None:
    west, south, east, north = bbox
    if not (-180.0 <= west <= 180.0 and -180.0 <= east <= 180.0):
        raise argparse.ArgumentTypeError("Longitude values must be between -180 and 180.")
    if not (-90.0 <= south <= 90.0 and -90.0 <= north <= 90.0):
        raise argparse.ArgumentTypeError("Latitude values must be between -90 and 90.")
    if west == east or south == north:
        raise argparse.ArgumentTypeError("BBox has zero width or height.")


def bbox_to_dict(bbox: tuple[float, float, float, float]) -> dict[str, float]:
    west, south, east, north = bbox
    return {
        "west": round(west, 7),
        "south": round(south, 7),
        "east": round(east, 7),
        "north": round(north, 7),
    }


def bbox_to_str(bbox: tuple[float, float, float, float]) -> str:
    west, south, east, north = bbox
    return ",".join(f"{value:.7f}" for value in (west, south, east, north))


def dedupe_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = value.strip()
        if not cleaned or cleaned in seen:
            continue
        result.append(cleaned)
        seen.add(cleaned)
    return result


def expand_overpass_urls(raw_value: str) -> list[str]:
    configured = dedupe_strings(raw_value.split(","))
    if not configured:
        configured = [DEFAULT_OVERPASS_URL]
    if configured == [DEFAULT_OVERPASS_URL]:
        configured = dedupe_strings([DEFAULT_OVERPASS_URL, *DEFAULT_OVERPASS_FALLBACK_URLS])
    return configured


def dict_to_bbox(raw_bbox: dict[str, float]) -> tuple[float, float, float, float]:
    return raw_bbox["west"], raw_bbox["south"], raw_bbox["east"], raw_bbox["north"]


def lonlat_to_local_meters(lon: float, lat: float, ref_lat: float) -> tuple[float, float]:
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(ref_lat))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def local_meters_to_lonlat(x: float, y: float, ref_lat: float) -> tuple[float, float]:
    lon = math.degrees(x / (EARTH_RADIUS_M * math.cos(math.radians(ref_lat))))
    lat = math.degrees(y / EARTH_RADIUS_M)
    return lon, lat


def clamp_bbox(
    bbox: tuple[float, float, float, float],
    bounds: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    west, south, east, north = bbox
    min_west, min_south, max_east, max_north = bounds
    return (
        max(min_west, west),
        max(min_south, south),
        min(max_east, east),
        min(max_north, north),
    )


def build_tiles(
    bbox: tuple[float, float, float, float],
    tile_size_m: float,
    overlap_m: float,
) -> list[Tile]:
    west, south, east, north = bbox
    ref_lat = (south + north) / 2.0
    x_min, y_min = lonlat_to_local_meters(west, south, ref_lat)
    x_max, y_max = lonlat_to_local_meters(east, north, ref_lat)

    cols = math.ceil((x_max - x_min) / tile_size_m)
    rows = math.ceil((y_max - y_min) / tile_size_m)
    tiles: list[Tile] = []

    for row in range(rows):
        core_y_min = y_min + row * tile_size_m
        core_y_max = min(core_y_min + tile_size_m, y_max)

        for col in range(cols):
            core_x_min = x_min + col * tile_size_m
            core_x_max = min(core_x_min + tile_size_m, x_max)

            core_west, core_south = local_meters_to_lonlat(core_x_min, core_y_min, ref_lat)
            core_east, core_north = local_meters_to_lonlat(core_x_max, core_y_max, ref_lat)
            core_bbox = (core_west, core_south, core_east, core_north)

            query_x_min = max(x_min, core_x_min - overlap_m)
            query_y_min = max(y_min, core_y_min - overlap_m)
            query_x_max = min(x_max, core_x_max + overlap_m)
            query_y_max = min(y_max, core_y_max + overlap_m)
            query_west, query_south = local_meters_to_lonlat(query_x_min, query_y_min, ref_lat)
            query_east, query_north = local_meters_to_lonlat(query_x_max, query_y_max, ref_lat)
            query_bbox = clamp_bbox((query_west, query_south, query_east, query_north), bbox)

            tiles.append(
                Tile(
                    tile_id=f"r{row:02d}_c{col:02d}",
                    row=row,
                    col=col,
                    core_bbox=core_bbox,
                    query_bbox=query_bbox,
                )
            )

    return tiles


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def create_timestamped_dir(root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = root / stamp
    suffix = 1
    while candidate.exists():
        candidate = root / f"{stamp}_{suffix:02d}"
        suffix += 1
    return candidate


def format_filename_coord(value: float) -> str:
    return f"{value:.7f}".rstrip("0").rstrip(".")


def urbano_file_stem(bbox: tuple[float, float, float, float]) -> str:
    west, south, east, north = bbox
    return "_".join(
        (
            format_filename_coord(north),
            format_filename_coord(south),
            format_filename_coord(east),
            format_filename_coord(west),
        )
    )


def is_probably_us_bbox(bbox: tuple[float, float, float, float]) -> bool:
    west, south, east, north = bbox
    return -170.0 <= west <= -60.0 and -170.0 <= east <= -60.0 and 18.0 <= south <= 72.0 and 18.0 <= north <= 72.0


def run_urbano_bridge(
    args: argparse.Namespace,
    output_dir: Path,
    osm_file_path: Path | None = None,
    elevation_tiff_path: Path | None = None,
) -> None:
    bridge_project = SCRIPT_DIR / "tools" / "UrbanoBridge" / "UrbanoBridge.csproj"
    if not bridge_project.exists():
        raise FileNotFoundError(f"Urbano bridge project not found at {bridge_project}")

    command = [
        "dotnet",
        "run",
        "--project",
        str(bridge_project),
        "--",
        "--bbox",
        bbox_to_str(args.bbox),
        "--output-folder",
        str(output_dir),
        "--granularity",
        args.granularity,
    ]
    if args.urbano_package_dir:
        command.extend(["--package-dir", args.urbano_package_dir])
    if osm_file_path is not None:
        command.extend(["--osm-file-path", str(osm_file_path)])
    if elevation_tiff_path is not None:
        command.extend(["--elevation-tiff-path", str(elevation_tiff_path)])
    if args.traveler_model:
        command.extend(["--traveler-model", args.traveler_model])
    if args.skip_climate:
        command.append("--skip-climate")
    if args.skip_blocks:
        command.append("--skip-blocks")
    if args.skip_block_meta:
        command.append("--skip-block-meta")
    if args.skip_elevation:
        command.append("--skip-elevation")
    if args.skip_overture:
        command.append("--skip-overture")

    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise RuntimeError("Urbano bridge failed. See the output above for details.")


def resolve_opentopography_api_key(args: argparse.Namespace) -> str:
    api_key = (
        args.opentopography_api_key
        or os.environ.get("OPENTOPOGRAPHY_API_KEY")
        or os.environ.get("OPENTOPO_API_KEY")
    )
    if not api_key:
        raise RuntimeError(
            "OpenTopography elevation download requires an API key. "
            "Pass --opentopography-api-key or set OPENTOPOGRAPHY_API_KEY."
        )
    return api_key


def is_tiff_header(header: bytes) -> bool:
    return header.startswith(b"II*\x00") or header.startswith(b"MM\x00*")


def download_opentopography_dem_tiff(
    args: argparse.Namespace,
    output_dir: Path,
) -> Path:
    stem = urbano_file_stem(args.bbox)
    demtype = args.opentopography_demtype.upper()
    output_path = output_dir / f"{stem}_{demtype.lower()}.tif"
    if output_path.exists() and output_path.stat().st_size > 0:
        print(f"Reusing elevation GeoTIFF: {output_path}", flush=True)
        return output_path

    api_key = resolve_opentopography_api_key(args)
    params = {
        "demtype": demtype,
        "south": f"{args.bbox[1]:.7f}",
        "north": f"{args.bbox[3]:.7f}",
        "west": f"{args.bbox[0]:.7f}",
        "east": f"{args.bbox[2]:.7f}",
        "outputFormat": "GTiff",
        "API_Key": api_key,
    }
    temp_path = output_path.with_suffix(".tif.download")
    if temp_path.exists():
        temp_path.unlink()

    print(
        f"Downloading elevation GeoTIFF from OpenTopography ({demtype}) to {output_path}",
        flush=True,
    )
    with requests.get(
        args.opentopography_url,
        params=params,
        headers={"User-Agent": args.user_agent},
        stream=True,
        timeout=args.elevation_timeout_seconds,
    ) as response:
        response.raise_for_status()
        ensure_dir(output_path.parent)
        with temp_path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)

    header = temp_path.read_bytes()[:16]
    if not is_tiff_header(header):
        preview = temp_path.read_text(encoding="utf-8", errors="replace")[:300]
        temp_path.unlink(missing_ok=True)
        raise RuntimeError(
            "OpenTopography did not return a TIFF. "
            f"First bytes were {header!r}. Response preview: {preview}"
        )

    temp_path.replace(output_path)
    return output_path


def build_tiled_osm_fallback(args: argparse.Namespace, output_dir: Path) -> Path:
    attempt_sizes = [args.tile_size_m]
    for candidate in (2000.0, 1500.0, 1000.0):
        if candidate < args.tile_size_m and candidate not in attempt_sizes:
            attempt_sizes.append(candidate)

    legacy_tile_work_dir = output_dir / "_tilework"
    if legacy_tile_work_dir.exists():
        best_effort_rmtree(legacy_tile_work_dir)

    last_error: Exception | None = None
    for attempt_index, tile_size_m in enumerate(attempt_sizes, start=1):
        attempt_overlap_m = min(args.overlap_m, max(50.0, tile_size_m / 10.0))
        tile_work_dir = output_dir / f"_tilework_{int(tile_size_m)}m"
        if tile_work_dir.exists():
            if tilework_manifest_matches(tile_work_dir, args.bbox, tile_size_m, attempt_overlap_m):
                print(
                    f"Resuming fallback tilework in {tile_work_dir}",
                    flush=True,
                )
            else:
                best_effort_rmtree(tile_work_dir)
                if tile_work_dir.exists():
                    tile_work_dir = output_dir / f"{tile_work_dir.name}_{int(time.time())}"
                    print(
                        f"Starting a fresh fallback tilework folder at {tile_work_dir}",
                        flush=True,
                    )

        print(
            f"Fallback OSM attempt {attempt_index}/{len(attempt_sizes)} with "
            f"{tile_size_m:.0f} m tiles and {attempt_overlap_m:.0f} m overlap",
            flush=True,
        )

        try:
            return build_tiled_osm_fallback_attempt(
                args,
                output_dir,
                tile_size_m,
                attempt_overlap_m,
                tile_work_dir,
            )
        except RuntimeError as exc:
            last_error = exc
            if not should_retry_with_smaller_osm_tiles(exc):
                raise
            if attempt_index == len(attempt_sizes):
                break

            print(
                f"Retrying fallback OSM with smaller tiles because {exc}",
                file=sys.stderr,
                flush=True,
            )

    raise RuntimeError("Fallback OSM download failed after trying smaller tile sizes.") from last_error


def build_tiled_osm_fallback_attempt(
    args: argparse.Namespace,
    output_dir: Path,
    tile_size_m: float,
    overlap_m: float,
    tile_work_dir: Path,
) -> Path:
    raw_osm_dir = tile_work_dir / "raw" / "osm"
    tiles = build_tiles(args.bbox, tile_size_m, overlap_m)

    write_manifest(tile_work_dir, args.bbox, tile_size_m, overlap_m, tiles)
    ensure_dir(raw_osm_dir)

    session = requests.Session()
    session.headers.update({"User-Agent": args.user_agent})

    for index, tile in enumerate(tiles, start=1):
        output_path = raw_osm_dir / f"{tile.tile_id}.osm"
        if output_path.exists() and output_path.stat().st_size > 0:
            print(f"[fallback osm {index}/{len(tiles)}] reusing {tile.tile_id}", flush=True)
            continue

        print(f"[fallback osm {index}/{len(tiles)}] downloading {tile.tile_id}", flush=True)
        download_osm_tile(
            session=session,
            overpass_url=args.overpass_url,
            osm_api_url=args.osm_api_url,
            tile=tile,
            output_path=output_path,
            timeout_seconds=args.osm_timeout_seconds,
            max_retries=args.max_retries,
            osm_format="xml",
            use_overpass_for_xml=True,
        )
        time.sleep(args.sleep_seconds)

    merged_path = output_dir / f"{urbano_file_stem(args.bbox)}.osm"
    unclipped_path = output_dir / f"{urbano_file_stem(args.bbox)}.unclipped.osm"
    merge_osm_xml_files(sorted(raw_osm_dir.glob("*.osm")), unclipped_path, args.bbox)
    clip_osm_xml_to_bbox(unclipped_path, merged_path, args.bbox)
    try:
        unclipped_path.unlink()
    except PermissionError:
        print(
            f"Warning: could not remove temporary file {unclipped_path}.",
            file=sys.stderr,
            flush=True,
        )
    print(f"Fallback OSM: {merged_path}", flush=True)
    best_effort_rmtree(tile_work_dir)
    return merged_path


def exception_chain_text(exc: BaseException) -> str:
    parts: list[str] = []
    current: BaseException | None = exc
    while current is not None:
        parts.append(str(current))
        current = current.__cause__ or current.__context__
    return " | ".join(part for part in parts if part)


def best_effort_rmtree(path: Path) -> None:
    try:
        shutil.rmtree(path)
    except PermissionError:
        print(
            f"Warning: could not remove temporary folder {path}. "
            "Continuing with a fresh attempt folder.",
            file=sys.stderr,
            flush=True,
        )


def tilework_manifest_matches(
    tile_work_dir: Path,
    bbox: tuple[float, float, float, float],
    tile_size_m: float,
    overlap_m: float,
) -> bool:
    manifest_path = tile_work_dir / "manifest.json"
    if not manifest_path.exists():
        return False

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest_bbox = dict_to_bbox(manifest["study_bbox"])
        return (
            all(math.isclose(left, right, abs_tol=1e-7) for left, right in zip(manifest_bbox, bbox))
            and math.isclose(float(manifest["tile_size_m"]), tile_size_m, abs_tol=1e-6)
            and math.isclose(float(manifest["overlap_m"]), overlap_m, abs_tol=1e-6)
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def should_retry_with_smaller_osm_tiles(exc: BaseException) -> bool:
    text = exception_chain_text(exc).lower()
    return "50000-node limit" in text or "http 504" in text or "http 502" in text


def compute_osm_retry_sleep_seconds(exc: BaseException, attempt: int) -> int:
    text = exception_chain_text(exc).lower()
    if "http 429" in text:
        return min(300, 30 * attempt)
    if "http 504" in text or "http 502" in text:
        return min(240, 20 * attempt)
    if "remote end closed" in text or "connection aborted" in text or "timed out" in text:
        return min(180, 15 * attempt)
    return min(120, 5 * attempt)


def write_manifest(
    output_dir: Path,
    bbox: tuple[float, float, float, float],
    tile_size_m: float,
    overlap_m: float,
    tiles: list[Tile],
) -> Path:
    ensure_dir(output_dir)
    manifest_path = output_dir / "manifest.json"

    rows = max((tile.row for tile in tiles), default=-1) + 1
    cols = max((tile.col for tile in tiles), default=-1) + 1
    manifest = {
        "study_bbox": bbox_to_dict(bbox),
        "tile_size_m": tile_size_m,
        "overlap_m": overlap_m,
        "tile_count": len(tiles),
        "rows": rows,
        "cols": cols,
        "tiles": [tile.to_dict() for tile in tiles],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest_path


def load_manifest(output_dir: Path) -> dict[str, object]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"Manifest not found at {manifest_path}. Run the plan or download command first."
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def iter_manifest_tiles(manifest: dict[str, object]) -> list[Tile]:
    result: list[Tile] = []
    for tile_data in manifest["tiles"]:
        core = tile_data["core_bbox"]
        query = tile_data["query_bbox"]
        result.append(
            Tile(
                tile_id=tile_data["tile_id"],
                row=tile_data["row"],
                col=tile_data["col"],
                core_bbox=(core["west"], core["south"], core["east"], core["north"]),
                query_bbox=(query["west"], query["south"], query["east"], query["north"]),
            )
        )
    return result


def summarise_extent(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    west, south, east, north = bbox
    lat_mid = (south + north) / 2.0
    width_m = math.radians(east - west) * EARTH_RADIUS_M * math.cos(math.radians(lat_mid))
    height_m = math.radians(north - south) * EARTH_RADIUS_M
    return abs(width_m), abs(height_m)


def resolve_overture_types(args: argparse.Namespace) -> list[str]:
    if not (args.source and "overture" in args.source):
        return []

    if args.all_overture_types:
        if get_all_overture_types is None:
            raise RuntimeError(
                "overturemaps is not importable in Python, so --all-overture-types is unavailable."
            )
        return list(get_all_overture_types())

    if args.overture_type:
        return list(dict.fromkeys(args.overture_type))

    return list(DEFAULT_OVERTURE_TYPES)


def select_tiles(
    tiles: list[Tile],
    max_tiles: int | None,
    tile_ids: list[str] | None,
) -> list[Tile]:
    selected = tiles
    if tile_ids:
        requested = set(tile_ids)
        selected = [tile for tile in tiles if tile.tile_id in requested]
        missing = requested.difference({tile.tile_id for tile in selected})
        if missing:
            raise RuntimeError(f"Unknown tile ids: {', '.join(sorted(missing))}")

    if max_tiles is not None:
        selected = selected[:max_tiles]
    return selected


def element_score(element: dict[str, object]) -> tuple[int, int, int]:
    geom_len = len(element.get("geometry", []))
    members_len = len(element.get("members", []))
    tags_len = len(element.get("tags", {}))
    return geom_len, members_len, tags_len


def xml_element_score(element: ET.Element) -> tuple[int, int, int]:
    child_count = len(element)
    tag_count = sum(1 for child in element if child.tag == "tag")
    try:
        version = int(element.attrib.get("version", "0"))
    except ValueError:
        version = 0
    return child_count, tag_count, version


def osm_extension(osm_format: str) -> str:
    return ".osm" if osm_format == "xml" else ".json"


def overture_extension(overture_format: str) -> str:
    return ".geojson" if overture_format == "geojson" else ".geojsonseq"


def build_overpass_query(
    bbox: tuple[float, float, float, float],
    timeout_seconds: int,
    osm_format: str,
) -> str:
    west, south, east, north = bbox
    if osm_format == "xml":
        return f"""
[out:xml][timeout:{timeout_seconds}];
(
  node({south:.7f},{west:.7f},{north:.7f},{east:.7f});
  way({south:.7f},{west:.7f},{north:.7f},{east:.7f});
  relation({south:.7f},{west:.7f},{north:.7f},{east:.7f});
);
(._;>;);
out meta;
""".strip()

    return f"""
[out:json][timeout:{timeout_seconds}];
(
  node({south:.7f},{west:.7f},{north:.7f},{east:.7f});
  way({south:.7f},{west:.7f},{north:.7f},{east:.7f});
  relation({south:.7f},{west:.7f},{north:.7f},{east:.7f});
);
out body geom;
""".strip()


def download_osm_tile(
    session: requests.Session,
    overpass_url: str,
    osm_api_url: str,
    tile: Tile,
    output_path: Path,
    timeout_seconds: int,
    max_retries: int,
    osm_format: str,
    use_overpass_for_xml: bool = False,
) -> None:
    last_error: Exception | None = None
    overpass_urls = expand_overpass_urls(overpass_url)

    for attempt in range(1, max_retries + 1):
        current_overpass_url = overpass_urls[(attempt - 1) % len(overpass_urls)]
        try:
            if osm_format == "xml" and not use_overpass_for_xml:
                response = session.get(
                    osm_api_url,
                    params={"bbox": bbox_to_str(tile.query_bbox)},
                    timeout=(30, timeout_seconds + 60),
                )
            else:
                query = build_overpass_query(tile.query_bbox, timeout_seconds, osm_format)
                response = session.post(
                    current_overpass_url,
                    data=query.encode("utf-8"),
                    headers={"Content-Type": "text/plain; charset=utf-8"},
                    timeout=(30, timeout_seconds + 60),
                )
            if response.status_code == 200:
                output_path.write_text(response.text, encoding="utf-8")
                return

            if (
                osm_format == "xml"
                and response.status_code == 400
                and "too many nodes" in response.text.lower()
            ):
                raise RuntimeError(
                    "OSM XML tile exceeded the OSM API 50000-node limit. "
                    "Reduce --tile-size-m, for example to 1500 or 2000 in dense urban areas."
                )

            if response.status_code in {429, 502, 504}:
                raise RuntimeError(
                    f"OSM source returned HTTP {response.status_code} for tile {tile.tile_id} "
                    f"via {current_overpass_url}."
                )

            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - runtime resilience
            if "50000-node limit" in str(exc):
                raise
            last_error = exc
            sleep_seconds = compute_osm_retry_sleep_seconds(exc, attempt)
            print(
                f"OSM retry {attempt}/{max_retries} for {tile.tile_id}: {exc}. "
                f"Sleeping {sleep_seconds}s...",
                file=sys.stderr,
            )
            time.sleep(sleep_seconds)
    if last_error is not None and "50000-node limit" in str(last_error):
        raise RuntimeError(str(last_error)) from last_error
    raise RuntimeError(f"Failed to download OSM tile {tile.tile_id}") from last_error


def download_overture_tile(
    tile: Tile,
    overture_type: str,
    output_path: Path,
    release: str | None,
    stac: bool,
    overture_format: str,
) -> None:
    executable = shutil.which("overturemaps")
    if not executable:
        raise RuntimeError("Could not find the overturemaps CLI on PATH.")

    command = [
        executable,
        "download",
        f"--bbox={bbox_to_str(tile.query_bbox)}",
        "-f",
        overture_format,
        "--type",
        overture_type,
        "--output",
        str(output_path),
    ]
    if release:
        command.extend(["--release", release])
    if stac:
        command.append("--stac")

    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"overturemaps failed for tile {tile.tile_id}, type {overture_type}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def merge_osm_files(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, dict[str, object]] = OrderedDict()

    for input_path in input_paths:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        for element in payload.get("elements", []):
            key = f"{element.get('type')}:{element.get('id')}"
            existing = deduped.get(key)
            if existing is None or element_score(element) > element_score(existing):
                deduped[key] = element

    merged = {
        "version": 0.6,
        "generator": "osm_overture_tiles.py",
        "elements": list(deduped.values()),
    }
    ensure_dir(output_path.parent)
    output_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    return len(deduped)


def merge_osm_xml_files(
    input_paths: Iterable[Path],
    output_path: Path,
    study_bbox: tuple[float, float, float, float],
) -> int:
    deduped: dict[str, OrderedDict[str, ET.Element]] = {
        element_type: OrderedDict() for element_type in OSM_TYPE_ORDER
    }

    for input_path in input_paths:
        root = ET.parse(input_path).getroot()
        for element in root:
            if element.tag not in OSM_TYPE_ORDER:
                continue

            element_id = element.attrib.get("id")
            if not element_id:
                continue

            existing = deduped[element.tag].get(element_id)
            candidate = ET.fromstring(ET.tostring(element, encoding="unicode"))
            if existing is None or xml_element_score(candidate) > xml_element_score(existing):
                deduped[element.tag][element_id] = candidate

    west, south, east, north = study_bbox
    merged_root = ET.Element(
        "osm",
        attrib={
            "version": "0.6",
            "generator": "osm_overture_tiles.py",
        },
    )
    ET.SubElement(
        merged_root,
        "bounds",
        attrib={
            "minlat": f"{south:.7f}",
            "minlon": f"{west:.7f}",
            "maxlat": f"{north:.7f}",
            "maxlon": f"{east:.7f}",
        },
    )

    total = 0
    for element_type in OSM_TYPE_ORDER:
        for element in deduped[element_type].values():
            merged_root.append(element)
            total += 1

    ET.indent(merged_root, space="  ")
    ensure_dir(output_path.parent)
    tree = ET.ElementTree(merged_root)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return total


def clip_osm_xml_to_bbox(
    input_path: Path,
    output_path: Path,
    study_bbox: tuple[float, float, float, float],
) -> int:
    west, south, east, north = study_bbox
    root = ET.parse(input_path).getroot()

    nodes_by_id: dict[str, ET.Element] = {}
    ways_by_id: dict[str, ET.Element] = {}
    relations_by_id: dict[str, ET.Element] = {}
    ordered_nodes: list[str] = []
    ordered_ways: list[str] = []
    ordered_relations: list[str] = []

    for child in root:
        element_id = child.attrib.get("id")
        if not element_id:
            continue
        if child.tag == "node":
            nodes_by_id[element_id] = child
            ordered_nodes.append(element_id)
        elif child.tag == "way":
            ways_by_id[element_id] = child
            ordered_ways.append(element_id)
        elif child.tag == "relation":
            relations_by_id[element_id] = child
            ordered_relations.append(element_id)

    initial_node_ids: set[str] = set()
    for node_id, node in nodes_by_id.items():
        lat = float(node.attrib["lat"])
        lon = float(node.attrib["lon"])
        if west <= lon <= east and south <= lat <= north:
            initial_node_ids.add(node_id)

    keep_way_ids: set[str] = set()
    keep_node_ids: set[str] = set(initial_node_ids)
    for way_id, way in ways_by_id.items():
        refs = [nd.attrib.get("ref") for nd in way.findall("nd")]
        if any(ref in initial_node_ids for ref in refs):
            keep_way_ids.add(way_id)
            keep_node_ids.update(ref for ref in refs if ref in nodes_by_id)

    keep_relation_ids: set[str] = set()
    changed = True
    while changed:
        changed = False
        for relation_id, relation in relations_by_id.items():
            if relation_id in keep_relation_ids:
                continue

            for member in relation.findall("member"):
                member_type = member.attrib.get("type")
                member_ref = member.attrib.get("ref")
                if (
                    (member_type == "node" and member_ref in keep_node_ids)
                    or (member_type == "way" and member_ref in keep_way_ids)
                    or (member_type == "relation" and member_ref in keep_relation_ids)
                ):
                    keep_relation_ids.add(relation_id)
                    changed = True
                    break

    clipped_root = ET.Element(
        "osm",
        attrib={
            "version": root.attrib.get("version", "0.6"),
            "generator": root.attrib.get("generator", "osm_overture_tiles.py"),
        },
    )
    ET.SubElement(
        clipped_root,
        "bounds",
        attrib={
            "minlat": f"{south:.7f}",
            "minlon": f"{west:.7f}",
            "maxlat": f"{north:.7f}",
            "maxlon": f"{east:.7f}",
        },
    )

    total = 0
    for node_id in ordered_nodes:
        if node_id in keep_node_ids:
            clipped_root.append(ET.fromstring(ET.tostring(nodes_by_id[node_id], encoding="unicode")))
            total += 1

    for way_id in ordered_ways:
        if way_id in keep_way_ids:
            clipped_root.append(ET.fromstring(ET.tostring(ways_by_id[way_id], encoding="unicode")))
            total += 1

    for relation_id in ordered_relations:
        if relation_id not in keep_relation_ids:
            continue

        relation = ET.fromstring(ET.tostring(relations_by_id[relation_id], encoding="unicode"))
        for member in list(relation.findall("member")):
            member_type = member.attrib.get("type")
            member_ref = member.attrib.get("ref")
            keep_member = (
                (member_type == "node" and member_ref in keep_node_ids)
                or (member_type == "way" and member_ref in keep_way_ids)
                or (member_type == "relation" and member_ref in keep_relation_ids)
            )
            if not keep_member:
                relation.remove(member)

        if relation.findall("member"):
            clipped_root.append(relation)
            total += 1

    ET.indent(clipped_root, space="  ")
    ensure_dir(output_path.parent)
    ET.ElementTree(clipped_root).write(output_path, encoding="utf-8", xml_declaration=True)
    return total


def iter_geojsonseq_features(path: Path) -> Iterable[tuple[str, str]]:
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("\x1e"):
                line = line[1:].lstrip()
            feature = json.loads(line)
            feature_id = feature.get("id") or feature.get("properties", {}).get("id")
            if not feature_id:
                raise RuntimeError(f"Feature in {path} is missing an id field.")
            yield str(feature_id), json.dumps(feature, separators=(",", ":"))


def iter_geojson_features(path: Path) -> Iterable[tuple[str, str]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("type") != "FeatureCollection":
        raise RuntimeError(f"{path} is not a GeoJSON FeatureCollection.")

    for feature in payload.get("features", []):
        feature_id = feature.get("id") or feature.get("properties", {}).get("id")
        if not feature_id:
            raise RuntimeError(f"Feature in {path} is missing an id field.")
        yield str(feature_id), json.dumps(feature, separators=(",", ":"))


def merge_overture_files(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, str] = OrderedDict()

    for input_path in input_paths:
        for feature_id, compact_json in iter_geojsonseq_features(input_path):
            deduped.setdefault(feature_id, compact_json)

    ensure_dir(output_path.parent)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for compact_json in deduped.values():
            handle.write(compact_json)
            handle.write("\n")

    return len(deduped)


def merge_overture_geojson_files(input_paths: Iterable[Path], output_path: Path) -> int:
    deduped: OrderedDict[str, str] = OrderedDict()

    for input_path in input_paths:
        for feature_id, compact_json in iter_geojson_features(input_path):
            deduped.setdefault(feature_id, compact_json)

    ensure_dir(output_path.parent)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type":"FeatureCollection","features":[')
        first = True
        for compact_json in deduped.values():
            if not first:
                handle.write(",")
            handle.write(compact_json)
            first = False
        handle.write("]}\n")

    return len(deduped)


def command_plan(args: argparse.Namespace) -> int:
    tiles = build_tiles(args.bbox, args.tile_size_m, args.overlap_m)
    manifest_path = write_manifest(args.output_dir, args.bbox, args.tile_size_m, args.overlap_m, tiles)
    width_m, height_m = summarise_extent(args.bbox)

    print(f"Study bbox: {bbox_to_str(args.bbox)}")
    print(f"Approx extent: {width_m / 1000:.2f} km x {height_m / 1000:.2f} km")
    print(f"Tile size: {args.tile_size_m:.0f} m with {args.overlap_m:.0f} m overlap")
    print(f"Tiles: {len(tiles)}")
    print(f"Manifest: {manifest_path}")
    return 0


def command_download(args: argparse.Namespace) -> int:
    tiles = build_tiles(args.bbox, args.tile_size_m, args.overlap_m)
    manifest_path = write_manifest(args.output_dir, args.bbox, args.tile_size_m, args.overlap_m, tiles)
    selected_tiles = select_tiles(tiles, args.max_tiles, args.tile_id)
    overture_types = resolve_overture_types(args)

    print(f"Manifest written to {manifest_path}")
    print(f"Downloading {len(selected_tiles)} of {len(tiles)} tiles")

    if "osm" in args.source:
        ensure_dir(args.output_dir / "raw" / "osm")
        session = requests.Session()
        session.headers.update({"User-Agent": args.user_agent})

        for index, tile in enumerate(selected_tiles, start=1):
            output_path = args.output_dir / "raw" / "osm" / f"{tile.tile_id}{osm_extension(args.osm_format)}"
            if output_path.exists() and not args.force:
                print(f"[osm {index}/{len(selected_tiles)}] skipping {tile.tile_id}")
                continue

            print(f"[osm {index}/{len(selected_tiles)}] downloading {tile.tile_id}")
            download_osm_tile(
                session=session,
                overpass_url=args.overpass_url,
                osm_api_url=args.osm_api_url,
                tile=tile,
                output_path=output_path,
                timeout_seconds=args.osm_timeout_seconds,
                max_retries=args.max_retries,
                osm_format=args.osm_format,
            )
            time.sleep(args.sleep_seconds)

    if "overture" in args.source:
        for overture_type in overture_types:
            ensure_dir(args.output_dir / "raw" / "overture" / overture_type)
            for index, tile in enumerate(selected_tiles, start=1):
                output_path = (
                    args.output_dir
                    / "raw"
                    / "overture"
                    / overture_type
                    / f"{tile.tile_id}{overture_extension(args.overture_format)}"
                )
                if output_path.exists() and not args.force:
                    print(
                        f"[overture:{overture_type} {index}/{len(selected_tiles)}] "
                        f"skipping {tile.tile_id}"
                    )
                    continue

                print(
                    f"[overture:{overture_type} {index}/{len(selected_tiles)}] "
                    f"downloading {tile.tile_id}"
                )
                download_overture_tile(
                    tile=tile,
                    overture_type=overture_type,
                    output_path=output_path,
                    release=args.overture_release,
                    stac=args.overture_stac,
                    overture_format=args.overture_format,
                )
                time.sleep(args.sleep_seconds)

    return 0


def command_merge(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.output_dir)
    study_bbox = dict_to_bbox(manifest["study_bbox"])

    osm_root = args.output_dir / "raw" / "osm"
    osm_xml_inputs = sorted(osm_root.glob("*.osm"))
    osm_json_inputs = sorted(osm_root.glob("*.json"))
    if osm_xml_inputs:
        osm_output = args.output_dir / "merged" / "osm" / "all.osm"
        element_count = merge_osm_xml_files(osm_xml_inputs, osm_output, study_bbox)
        print(f"Merged {len(osm_xml_inputs)} OSM files into {osm_output} ({element_count} unique elements)")
    elif osm_json_inputs:
        osm_output = args.output_dir / "merged" / "osm" / "all.osm.json"
        element_count = merge_osm_files(osm_json_inputs, osm_output)
        print(f"Merged {len(osm_json_inputs)} OSM files into {osm_output} ({element_count} unique elements)")

    overture_root = args.output_dir / "raw" / "overture"
    if overture_root.exists():
        for overture_type_dir in sorted(path for path in overture_root.iterdir() if path.is_dir()):
            overture_geojson_inputs = sorted(overture_type_dir.glob("*.geojson"))
            overture_seq_inputs = sorted(overture_type_dir.glob("*.geojsonseq"))
            if overture_geojson_inputs:
                overture_output = args.output_dir / "merged" / "overture" / f"{overture_type_dir.name}.geojson"
                feature_count = merge_overture_geojson_files(overture_geojson_inputs, overture_output)
                print(
                    f"Merged {len(overture_geojson_inputs)} Overture files for {overture_type_dir.name} "
                    f"into {overture_output} ({feature_count} unique features)"
                )
            elif overture_seq_inputs:
                overture_output = args.output_dir / "merged" / "overture" / f"{overture_type_dir.name}.geojsonseq"
                feature_count = merge_overture_files(overture_seq_inputs, overture_output)
                print(
                    f"Merged {len(overture_seq_inputs)} Overture files for {overture_type_dir.name} "
                    f"into {overture_output} ({feature_count} unique features)"
                )

    return 0


def command_urbano_package(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.resolve() if args.output_dir else create_timestamped_dir(args.output_root.resolve())
    ensure_dir(output_dir)
    is_us_bbox = is_probably_us_bbox(args.bbox)
    use_tiled_osm_fallback = args.skip_blocks and args.skip_climate and not is_us_bbox

    unsupported_non_us_blocks = not args.skip_blocks
    unsupported_non_us_climate = not args.skip_climate
    unsupported_non_us_elevation = (
        not args.skip_elevation
        and args.elevation_tiff_path is None
        and args.elevation_source != "opentopography"
    )
    if (unsupported_non_us_blocks or unsupported_non_us_climate or unsupported_non_us_elevation) and not is_us_bbox:
        print(
            "Warning: Urbano native block and climate generation is U.S.-specific. "
            "For non-U.S. areas, skip blocks/climate. Elevation requires --elevation-source opentopography.",
            file=sys.stderr,
        )

    print(f"Output folder: {output_dir}", flush=True)
    osm_file_path = None
    elevation_tiff_path = None
    if use_tiled_osm_fallback:
        print(
            "Using tiled OSM XML fallback for this non-U.S. package run. "
            "The native Urbano OSM/PBF path is being skipped.",
            file=sys.stderr,
            flush=True,
        )
        osm_file_path = build_tiled_osm_fallback(args, output_dir)

    if args.elevation_tiff_path is not None:
        elevation_tiff_path = args.elevation_tiff_path.resolve()
        if not elevation_tiff_path.exists() or elevation_tiff_path.stat().st_size == 0:
            raise FileNotFoundError(
                f"Supplied elevation TIFF was not found or was empty: {elevation_tiff_path}"
            )
    elif not args.skip_elevation and not is_us_bbox:
        if args.elevation_source != "opentopography":
            raise RuntimeError(
                "Non-U.S. elevation packaging requires either --elevation-tiff-path or "
                "--elevation-source opentopography (or use --skip-elevation)."
            )
        elevation_tiff_path = download_opentopography_dem_tiff(args, output_dir)

    run_urbano_bridge(args, output_dir, osm_file_path, elevation_tiff_path)

    if use_tiled_osm_fallback and not args.keep_tilework:
        legacy_tilework_dir = output_dir / "_tilework"
        if legacy_tilework_dir.exists():
            best_effort_rmtree(legacy_tilework_dir)
        for tile_work_dir in sorted(output_dir.glob("_tilework*")):
            best_effort_rmtree(tile_work_dir)

    project_setting_path = output_dir / f"{urbano_file_stem(args.bbox)}_project_setting.json"
    print(f"Project setting: {project_setting_path}", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Tile a bbox and download matching OSM and Overture datasets."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common_parser = argparse.ArgumentParser(add_help=False)
    common_parser.add_argument(
        "--bbox",
        type=parse_bbox,
        required=True,
        help="BBox as west,south,east,north. If south/north are reversed, they will be normalised.",
    )
    common_parser.add_argument(
        "--tile-size-m",
        type=float,
        default=5000.0,
        help="Core tile width and height in meters in a local planar grid.",
    )
    common_parser.add_argument(
        "--overlap-m",
        type=float,
        default=250.0,
        help="Extra overlap added around each tile query to catch edge features.",
    )
    common_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "osm-overture-run",
        help="Directory for manifest, raw downloads, and merged outputs.",
    )

    plan_parser = subparsers.add_parser("plan", parents=[common_parser], help="Write the tile manifest.")
    plan_parser.set_defaults(func=command_plan)

    download_parser = subparsers.add_parser(
        "download",
        parents=[common_parser],
        help="Download OSM and/or Overture data for each tile.",
    )
    download_parser.add_argument(
        "--source",
        action="append",
        choices=["osm", "overture"],
        required=True,
        help="Repeat for each source you want to download.",
    )
    download_parser.add_argument(
        "--overture-type",
        action="append",
        help="Repeatable Overture type. Defaults to a map-oriented set if omitted.",
    )
    download_parser.add_argument(
        "--all-overture-types",
        action="store_true",
        help="Download every Overture type exposed by the installed package.",
    )
    download_parser.add_argument(
        "--overture-release",
        help="Optional explicit Overture release, for example 2026-02-18.0.",
    )
    download_parser.add_argument(
        "--overture-stac",
        action="store_true",
        help="Pass --stac to overturemaps download.",
    )
    download_parser.add_argument(
        "--overture-format",
        choices=["geojson", "geojsonseq"],
        default="geojson",
        help="Overture download format. Use geojson for standard GeoJSON files.",
    )
    download_parser.add_argument(
        "--overpass-url",
        default=DEFAULT_OVERPASS_URL,
        help="Overpass interpreter endpoint. You can also provide a comma-separated list of mirrors.",
    )
    download_parser.add_argument(
        "--osm-api-url",
        default=DEFAULT_OSM_API_URL,
        help="OSM API map endpoint used for xml .osm downloads.",
    )
    download_parser.add_argument(
        "--osm-format",
        choices=["xml", "json"],
        default="xml",
        help="OSM download format. Use xml for standard .osm files.",
    )
    download_parser.add_argument(
        "--osm-timeout-seconds",
        type=int,
        default=300,
        help="Timeout value passed to the Overpass query and HTTP client.",
    )
    download_parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Retry count for transient Overpass failures.",
    )
    download_parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=2.0,
        help="Pause between tile requests to reduce API pressure.",
    )
    download_parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent string for Overpass requests.",
    )
    download_parser.add_argument(
        "--max-tiles",
        type=int,
        help="Only process the first N tiles. Useful for smoke tests.",
    )
    download_parser.add_argument(
        "--tile-id",
        action="append",
        help="Repeatable tile id filter, for example r01_c05.",
    )
    download_parser.add_argument(
        "--force",
        action="store_true",
        help="Redownload files even if the target already exists.",
    )
    download_parser.set_defaults(func=command_download)

    merge_parser = subparsers.add_parser(
        "merge",
        help="Merge downloaded tiles into deduplicated OSM and Overture outputs.",
    )
    merge_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data") / "osm-overture-run",
        help="Directory used by the download command.",
    )
    merge_parser.set_defaults(func=command_merge)

    urbano_parser = subparsers.add_parser(
        "urbano-package",
        help="Create a timestamped Urbano-compatible project folder with OSM, blocks, elevation, Overture, and project settings.",
    )
    urbano_parser.add_argument(
        "--bbox",
        type=parse_bbox,
        required=True,
        help="BBox as west,south,east,north. If south/north are reversed, they will be normalised.",
    )
    urbano_parser.add_argument(
        "--output-root",
        type=Path,
        default=SCRIPT_DIR,
        help="Parent directory for timestamped project folders. Defaults to the script folder.",
    )
    urbano_parser.add_argument(
        "--output-dir",
        type=Path,
        help="Explicit project folder. If omitted, a timestamped folder is created under --output-root.",
    )
    urbano_parser.add_argument(
        "--granularity",
        default="Block",
        help="Urbano project granularity string. Default: Block.",
    )
    urbano_parser.add_argument(
        "--tile-size-m",
        type=float,
        default=5000.0,
        help="Tile size in meters used by the non-U.S. OSM fallback path. Default: 5000.",
    )
    urbano_parser.add_argument(
        "--overlap-m",
        type=float,
        default=250.0,
        help="Tile overlap in meters used by the non-U.S. OSM fallback path. Default: 250.",
    )
    urbano_parser.add_argument(
        "--urbano-package-dir",
        help="Explicit installed Urbano package directory.",
    )
    urbano_parser.add_argument(
        "--traveler-model",
        help="Explicit path to the Urbano traveler ONNX model.",
    )
    urbano_parser.add_argument(
        "--skip-climate",
        action="store_true",
        help="Skip EPW climate lookup. Requires --skip-block-meta or --skip-blocks.",
    )
    urbano_parser.add_argument(
        "--skip-blocks",
        action="store_true",
        help="Skip .blocks generation.",
    )
    urbano_parser.add_argument(
        "--skip-block-meta",
        action="store_true",
        help="Skip the native block metadata enrichment pass.",
    )
    urbano_parser.add_argument(
        "--skip-elevation",
        action="store_true",
        help="Skip .egrid generation.",
    )
    urbano_parser.add_argument(
        "--elevation-tiff-path",
        type=Path,
        help="Existing GeoTIFF to use when building .egrid. If omitted for non-U.S. runs, use --elevation-source opentopography.",
    )
    urbano_parser.add_argument(
        "--elevation-source",
        choices=("none", "opentopography"),
        default="none",
        help="Elevation GeoTIFF source for non-U.S. runs. Use opentopography to fetch a GTiff before building .egrid.",
    )
    urbano_parser.add_argument(
        "--opentopography-api-key",
        help="OpenTopography API key. Can also be provided via OPENTOPOGRAPHY_API_KEY.",
    )
    urbano_parser.add_argument(
        "--opentopography-demtype",
        default="COP30",
        help="OpenTopography demtype used for non-U.S. elevation downloads. Default: COP30.",
    )
    urbano_parser.add_argument(
        "--opentopography-url",
        default=DEFAULT_OPENTOPOGRAPHY_URL,
        help="OpenTopography global DEM endpoint used for non-U.S. GeoTIFF downloads.",
    )
    urbano_parser.add_argument(
        "--elevation-timeout-seconds",
        type=int,
        default=1200,
        help="Timeout for non-U.S. elevation GeoTIFF downloads. Default: 1200.",
    )
    urbano_parser.add_argument(
        "--skip-overture",
        action="store_true",
        help="Skip Overture geoparquet generation.",
    )
    urbano_parser.add_argument(
        "--overpass-url",
        default=DEFAULT_OVERPASS_URL,
        help="Overpass interpreter endpoint used by the non-U.S. OSM fallback path. You can also provide a comma-separated list of mirrors.",
    )
    urbano_parser.add_argument(
        "--osm-api-url",
        default=DEFAULT_OSM_API_URL,
        help="OSM API endpoint reserved for compatibility with the shared OSM downloader.",
    )
    urbano_parser.add_argument(
        "--osm-timeout-seconds",
        type=int,
        default=300,
        help="Timeout value used by the non-U.S. OSM fallback path.",
    )
    urbano_parser.add_argument(
        "--max-retries",
        type=int,
        default=5,
        help="Retry count used by the non-U.S. OSM fallback path.",
    )
    urbano_parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=2.0,
        help="Pause between non-U.S. fallback OSM tile requests.",
    )
    urbano_parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="User-Agent string used by the non-U.S. OSM fallback path.",
    )
    urbano_parser.add_argument(
        "--keep-tilework",
        action="store_true",
        help="Keep the temporary _tilework folder created by the non-U.S. OSM fallback path.",
    )
    urbano_parser.set_defaults(func=command_urbano_package)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
