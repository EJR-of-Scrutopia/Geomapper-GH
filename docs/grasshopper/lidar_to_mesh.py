"""mapgen LiDAR GeoTIFF to Rhino mesh, for a Grasshopper Python component.

Reads the `<stem>_lidar_dtm.tif` / `<stem>_lidar_dsm.tif` a mapgen package
carries and builds a terrain mesh, with no libraries beyond the Python
standard one. It reads exactly the TIFF dialect mapgen's own writer
produces (classic little-endian, one directory, 256 px deflate tiles,
float32, nodata -9999) and refuses anything else by name, so a random
GeoTIFF failing here is the script being honest, not broken.

Grasshopper use (Rhino 8, GHPython 3 component):
  paste this whole file into the component and add these inputs
  (names matter, types in brackets, all except path optional):

    path       (str)  full path to the _lidar_dtm.tif or _lidar_dsm.tif
    step       (int)  sample every Nth pixel; 1 = native 1 m, default 2.
                      A whole town at step 1 is millions of vertices;
                      start at 2 or 4 and refine once it works.
    frame      (str)  "urbano" (default) or "bng".
                      urbano: vertices in the same absolute UTM metres
                        Urbano places the package's layers in, so the
                        mesh lands aligned with the imported survey.
                        Needs mapgen_src.
                      bng: raw British National Grid metres, EPSG:27700.
    mapgen_src (str)  path to mapgen's src directory (the folder holding
                      the "mapgen" package), only for frame "urbano".
                      Example: C:\\Users\\Param\\mapgen-phase1\\src
    at_origin  (bool) translate the mesh so its min corner sits at 0,0
                      (the true offset is reported in `origin`). Default
                      False. Useful for standalone studies; leave False
                      when aligning with Urbano layers.

  and outputs: mesh, info, origin.

Command-line self-test (no Rhino needed):
  python lidar_to_mesh.py <path-to-tif> [step] [frame] [mapgen_src]
  parses the raster, applies the frame transform, and prints the stats
  without building Rhino geometry.

Holes are holes: nodata samples get no vertex and no face. Nothing is
interpolated or fabricated where the LiDAR has nothing.

The "urbano" frame goes through mapgen's own bng.py (OSTN15) and utm.py
(the projection matched to Urbano to 1e-9 m), so there is exactly one
implementation of those transforms. The per-vertex cost is kept sane by
transforming an anchor lattice every 128 samples exactly and bilinearly
interpolating between anchors; measured against exact per-point
transforms over the real Cowbridge raster (200 random samples), the
worst interpolation error was 0.7 mm (2026-08-07).
"""

import json
import math
import struct
import sys
import zlib
from pathlib import Path

_NODATA = -9999.0
_TILE = 256

# TIFF tag numbers, matching mapgen's geotiff_write.py exactly.
_T_WIDTH = 256
_T_HEIGHT = 257
_T_BITS = 258
_T_COMPRESSION = 259
_T_TILE_WIDTH = 322
_T_TILE_LENGTH = 323
_T_TILE_OFFSETS = 324
_T_TILE_COUNTS = 325
_T_SAMPLE_FORMAT = 339
_T_PIXEL_SCALE = 33550
_T_TIEPOINT = 33922

_TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 12: 8}
_TYPE_CODES = {3: "H", 4: "I", 12: "d"}


class MapgenTiffError(ValueError):
    """This is not a raster mapgen wrote, and the message says why."""


def read_mapgen_tif(path):
    """Parse a mapgen-packaged LiDAR GeoTIFF.

    Returns (width, height, pixel_size, pixel_height, e_origin, n_top,
    values) with values a flat row-major list of floats, NaN for nodata.
    """
    data = Path(path).read_bytes()
    if data[:4] != b"II\x2a\x00":
        raise MapgenTiffError(
            f"{Path(path).name} is not a little-endian classic TIFF; "
            f"this script reads only the rasters mapgen itself writes."
        )
    (ifd_offset,) = struct.unpack_from("<I", data, 4)
    (entry_count,) = struct.unpack_from("<H", data, ifd_offset)
    tags = {}
    for index in range(entry_count):
        base = ifd_offset + 2 + index * 12
        tag, kind, count = struct.unpack_from("<HHI", data, base)
        size = _TYPE_SIZES.get(kind, 0) * count
        if size <= 4:
            value_offset = base + 8
        else:
            (value_offset,) = struct.unpack_from("<I", data, base + 8)
        if kind in _TYPE_CODES:
            code = _TYPE_CODES[kind]
            tags[tag] = list(
                struct.unpack_from("<" + code * count, data, value_offset)
            )
    for wanted, name in (
        (_T_WIDTH, "image width"),
        (_T_HEIGHT, "image height"),
        (_T_TILE_OFFSETS, "tile offsets"),
        (_T_PIXEL_SCALE, "pixel scale"),
        (_T_TIEPOINT, "tiepoint"),
    ):
        if wanted not in tags:
            raise MapgenTiffError(
                f"{Path(path).name} is missing its {name} tag; this "
                f"script reads only the rasters mapgen itself writes."
            )
    if tags.get(_T_BITS, [0])[0] != 32 or tags.get(_T_SAMPLE_FORMAT, [0])[0] != 3:
        raise MapgenTiffError(
            f"{Path(path).name} is not float32; mapgen's LiDAR rasters are."
        )
    if tags.get(_T_COMPRESSION, [0])[0] != 8:
        raise MapgenTiffError(
            f"{Path(path).name} is not deflate-compressed; mapgen's are."
        )
    width = tags[_T_WIDTH][0]
    height = tags[_T_HEIGHT][0]
    tile_w = tags.get(_T_TILE_WIDTH, [_TILE])[0]
    tile_h = tags.get(_T_TILE_LENGTH, [_TILE])[0]
    pixel_size, pixel_height = tags[_T_PIXEL_SCALE][0], tags[_T_PIXEL_SCALE][1]
    e_origin, n_top = tags[_T_TIEPOINT][3], tags[_T_TIEPOINT][4]

    across = (width + tile_w - 1) // tile_w
    values = [float("nan")] * (width * height)
    offsets = tags[_T_TILE_OFFSETS]
    counts = tags[_T_TILE_COUNTS]
    for tile_index, (offset, count) in enumerate(zip(offsets, counts)):
        raw = zlib.decompress(data[offset:offset + count])
        floats = struct.unpack("<" + "f" * (tile_w * tile_h), raw)
        tile_x = (tile_index % across) * tile_w
        tile_y = (tile_index // across) * tile_h
        rows = min(tile_h, height - tile_y)
        span = min(tile_w, width - tile_x)
        for row in range(rows):
            dest = (tile_y + row) * width + tile_x
            src = row * tile_w
            segment = floats[src:src + span]
            values[dest:dest + span] = [
                float("nan") if v == _NODATA else v for v in segment
            ]
    return width, height, pixel_size, pixel_height, e_origin, n_top, values


def _load_mapgen(mapgen_src):
    if not mapgen_src:
        raise MapgenTiffError(
            "frame 'urbano' needs mapgen_src: the path to mapgen's src "
            "directory, for example C:\\Users\\Param\\mapgen-phase1\\src"
        )
    src = str(Path(mapgen_src))
    if src not in sys.path:
        sys.path.insert(0, src)
    if "requests" not in sys.modules:
        try:
            import requests  # noqa: F401
        except ImportError:
            # bng.py imports requests at module level for its OSTN15
            # download path. This script never downloads: the grid must
            # already be cached (any mapgen survey run caches it), so a
            # placeholder satisfies the import without installing
            # anything into Rhino's own Python.
            import types
            sys.modules["requests"] = types.ModuleType("requests")
    from mapgen import bng, utm
    return bng, utm


def _zone_for(path, bng_module, utm_module, grid, e_origin, n_top):
    """The UTM zone Urbano uses for this package.

    survey.json sits beside the raster in every mapgen package and its
    bbox is what the pipeline itself derives the zone from; fall back to
    the raster's own origin corner when the file is missing (a raster
    copied out of its package).
    """
    survey = Path(path).parent / "survey.json"
    if survey.exists():
        bbox = json.loads(survey.read_text(encoding="utf-8")).get("bbox", {})
        if bbox:
            lat = (bbox["south"] + bbox["north"]) / 2.0
            lon = (bbox["west"] + bbox["east"]) / 2.0
            return utm_module.utm_zone(lat, lon)
    lat, lon = bng_module.from_bng(e_origin, n_top, grid)
    return utm_module.utm_zone(lat, lon)


def _urbano_lattice(width, height, pixel_size, pixel_height, e_origin,
                    n_top, step, mapgen_src, tif_path):
    """Exact BNG-to-UTM transform on an anchor lattice, for interpolation.

    Anchors every 128 samples in each direction (plus the far edge).
    Between anchors the mapping is bilinear; the worst measured error
    against exact per-point transforms is 0.7 mm (Cowbridge raster,
    2026-08-07), so the mesh is Urbano-exact for every practical
    purpose.
    """
    bng_module, utm_module = _load_mapgen(mapgen_src)
    grid = bng_module.load_ostn15()
    if grid is None:
        raise MapgenTiffError(
            "the OSTN15 grid is not cached on this machine; run any mapgen "
            "survey once (it downloads the grid), then retry."
        )
    zone = _zone_for(tif_path, bng_module, utm_module, grid, e_origin, n_top)

    lattice = 128
    cols = list(range(0, width, step))
    rows = list(range(0, height, step))
    anchor_cols = sorted(set(list(range(0, width, lattice)) + [cols[-1]]))
    anchor_rows = sorted(set(list(range(0, height, lattice)) + [rows[-1]]))
    exact = {}
    for row in anchor_rows:
        northing = n_top - (row + 0.5) * pixel_height
        for col in anchor_cols:
            easting = e_origin + (col + 0.5) * pixel_size
            lat, lon = bng_module.from_bng(easting, northing, grid)
            exact[(col, row)] = utm_module.project(lat, lon, zone)

    def locate(seq, value):
        for index in range(len(seq) - 1):
            if seq[index] <= value <= seq[index + 1]:
                return seq[index], seq[index + 1]
        return seq[-2], seq[-1]

    def transform(col, row):
        c0, c1 = locate(anchor_cols, col)
        r0, r1 = locate(anchor_rows, row)
        fc = 0.0 if c1 == c0 else (col - c0) / float(c1 - c0)
        fr = 0.0 if r1 == r0 else (row - r0) / float(r1 - r0)
        e00, n00 = exact[(c0, r0)]
        e10, n10 = exact[(c1, r0)]
        e01, n01 = exact[(c0, r1)]
        e11, n11 = exact[(c1, r1)]
        top_e = e00 + (e10 - e00) * fc
        bot_e = e01 + (e11 - e01) * fc
        top_n = n00 + (n10 - n00) * fc
        bot_n = n01 + (n11 - n01) * fc
        return top_e + (bot_e - top_e) * fr, top_n + (bot_n - top_n) * fr

    return transform, zone


def build_grid(path, step=2, frame="urbano", mapgen_src=None):
    """Everything Rhino-free: sampled vertex grid plus stats.

    Returns (points, cols, rows, info, origin) where points is a
    row-major list of (x, y, z) or None per sampled cell.
    """
    if step is None or int(step) < 1:
        step = 1
    step = int(step)
    frame = (frame or "urbano").lower()
    if frame not in ("urbano", "bng"):
        raise MapgenTiffError(
            f"frame '{frame}' is not one this script knows: urbano or bng."
        )
    (width, height, pixel_size, pixel_height,
     e_origin, n_top, values) = read_mapgen_tif(path)

    if frame == "urbano":
        transform, zone = _urbano_lattice(
            width, height, pixel_size, pixel_height, e_origin, n_top,
            step, mapgen_src, path)
    else:
        zone = None

        def transform(col, row):
            return (e_origin + (col + 0.5) * pixel_size,
                    n_top - (row + 0.5) * pixel_height)

    cols = list(range(0, width, step))
    rows = list(range(0, height, step))
    points = []
    z_min, z_max = float("inf"), float("-inf")
    kept = 0
    for row in rows:
        for col in cols:
            z = values[row * width + col]
            if z != z:
                points.append(None)
                continue
            x, y = transform(col, row)
            points.append((x, y, z))
            kept += 1
            if z < z_min:
                z_min = z
            if z > z_max:
                z_max = z
    if kept == 0:
        raise MapgenTiffError(
            f"{Path(path).name} has no data samples at step {step}; "
            f"nothing to mesh."
        )
    origin = (
        min(p[0] for p in points if p),
        min(p[1] for p in points if p),
    )
    info = (
        f"{Path(path).name}: {width} x {height} px at "
        f"{pixel_size:g} m, sampled every {step} px, frame {frame}"
        f"{' zone ' + zone if zone else ''}; {kept:,} data points, "
        f"z {z_min:.2f} to {z_max:.2f} m"
    )
    return points, len(cols), len(rows), info, origin


def build_rhino_mesh(points, cols, rows, at_origin, origin):
    """The Rhino half: only importable inside Rhino/Grasshopper."""
    import Rhino.Geometry as rg

    shift_x, shift_y = (origin if at_origin else (0.0, 0.0))
    mesh = rg.Mesh()
    index_of = [-1] * len(points)
    for position, point in enumerate(points):
        if point is None:
            continue
        index_of[position] = mesh.Vertices.Add(
            point[0] - shift_x, point[1] - shift_y, point[2]
        )
    for row in range(rows - 1):
        for col in range(cols - 1):
            a = index_of[row * cols + col]
            b = index_of[row * cols + col + 1]
            c = index_of[(row + 1) * cols + col + 1]
            d = index_of[(row + 1) * cols + col]
            if -1 not in (a, b, c, d):
                mesh.Faces.AddFace(a, b, c, d)
    mesh.Normals.ComputeNormals()
    mesh.Compact()
    return mesh


def _run_component():
    """Grasshopper entry: reads the component's inputs from globals."""
    tif_path = globals().get("path")
    if not tif_path:
        return None, "connect path: the packaged _lidar_dtm.tif or _lidar_dsm.tif", None
    points, cols, rows, info, origin = build_grid(
        tif_path,
        step=globals().get("step") or 2,
        frame=globals().get("frame") or "urbano",
        mapgen_src=globals().get("mapgen_src"),
    )
    shift = bool(globals().get("at_origin"))
    built = build_rhino_mesh(points, cols, rows, shift, origin)
    if shift:
        info += f"; moved to origin, true min corner at {origin[0]:.2f}, {origin[1]:.2f}"
    return built, info, "{:.3f}, {:.3f}".format(*origin)


def _in_rhino():
    """Rhino 8's script component runs this module AS __main__, so the
    classic __name__ check cannot tell Grasshopper from a terminal; the
    Rhino module can, and only Rhino has it."""
    try:
        import Rhino  # noqa: F401
        return True
    except Exception:
        return False


if _in_rhino():
    # Grasshopper: the component's inputs arrive as globals; missing path
    # yields a hint on the info output instead of an error.
    mesh, info, origin = _run_component()
elif __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    arg_path = sys.argv[1]
    arg_step = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    arg_frame = sys.argv[3] if len(sys.argv) > 3 else "bng"
    arg_src = sys.argv[4] if len(sys.argv) > 4 else None
    grid_points, grid_cols, grid_rows, grid_info, grid_origin = build_grid(
        arg_path, arg_step, arg_frame, arg_src)
    faces = sum(
        1
        for row in range(grid_rows - 1)
        for col in range(grid_cols - 1)
        if all(
            grid_points[r * grid_cols + c] is not None
            for r, c in (
                (row, col), (row, col + 1),
                (row + 1, col + 1), (row + 1, col),
            )
        )
    )
    print(grid_info)
    print(f"mesh would carry {faces:,} quad faces; "
          f"min corner {grid_origin[0]:.2f}, {grid_origin[1]:.2f}")
