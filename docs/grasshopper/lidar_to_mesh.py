"""mapgen LiDAR GeoTIFF to Rhino mesh, for a Grasshopper Python component.

Reads the `<stem>_lidar_dtm.tif` / `<stem>_lidar_dsm.tif` a mapgen package
carries and builds a terrain mesh, with no libraries beyond the Python
standard one (numpy is used automatically when present; Rhino 8 ships it).
It reads exactly the TIFF dialect mapgen's own writer produces (classic
little-endian, one directory, 256 px deflate tiles, float32, nodata -9999)
and refuses anything else by name, so a random GeoTIFF failing here is the
script being honest, not broken.

Grasshopper use (Rhino 8, Python 3 component):
  paste this whole file into the component and add these inputs
  (names matter, types in brackets, all except path optional):

    run        (bool) gate: nothing computes until True. Default True
                      when the input is not added at all.
    path       (str)  full path to the _lidar_dtm.tif or _lidar_dsm.tif
    step       (int)  sample every Nth pixel; 1 = native, default 2.
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
                      False. Leave False when aligning with Urbano
                      layers.

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

import bisect
import json
import struct
import sys
import zlib
from pathlib import Path

try:
    import numpy as _np
except ImportError:
    _np = None

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


def _read_tags(data, name):
    if data[:4] != b"II\x2a\x00":
        raise MapgenTiffError(
            f"{name} is not a little-endian classic TIFF; this script "
            f"reads only the rasters mapgen itself writes."
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
    for wanted, label in (
        (_T_WIDTH, "image width"),
        (_T_HEIGHT, "image height"),
        (_T_TILE_OFFSETS, "tile offsets"),
        (_T_PIXEL_SCALE, "pixel scale"),
        (_T_TIEPOINT, "tiepoint"),
    ):
        if wanted not in tags:
            raise MapgenTiffError(
                f"{name} is missing its {label} tag; this script reads "
                f"only the rasters mapgen itself writes."
            )
    if tags.get(_T_BITS, [0])[0] != 32 or tags.get(_T_SAMPLE_FORMAT, [0])[0] != 3:
        raise MapgenTiffError(f"{name} is not float32; mapgen's LiDAR rasters are.")
    if tags.get(_T_COMPRESSION, [0])[0] != 8:
        raise MapgenTiffError(f"{name} is not deflate-compressed; mapgen's are.")
    return tags


def read_mapgen_tif(path):
    """Parse a mapgen-packaged LiDAR GeoTIFF.

    Returns (width, height, pixel_size, pixel_height, e_origin, n_top,
    values): values is a numpy float32 (height, width) array when numpy
    is present, else a flat row-major list; nodata is NaN either way.
    """
    data = Path(path).read_bytes()
    tags = _read_tags(data, Path(path).name)
    width = tags[_T_WIDTH][0]
    height = tags[_T_HEIGHT][0]
    tile_w = tags.get(_T_TILE_WIDTH, [_TILE])[0]
    tile_h = tags.get(_T_TILE_LENGTH, [_TILE])[0]
    pixel_size, pixel_height = tags[_T_PIXEL_SCALE][0], tags[_T_PIXEL_SCALE][1]
    e_origin, n_top = tags[_T_TIEPOINT][3], tags[_T_TIEPOINT][4]
    across = (width + tile_w - 1) // tile_w
    offsets = tags[_T_TILE_OFFSETS]
    counts = tags[_T_TILE_COUNTS]

    if _np is not None:
        grid = _np.full((height, width), _np.nan, dtype=_np.float32)
        for tile_index, (offset, count) in enumerate(zip(offsets, counts)):
            raw = zlib.decompress(data[offset:offset + count])
            block = _np.frombuffer(raw, dtype="<f4").reshape(tile_h, tile_w)
            tile_x = (tile_index % across) * tile_w
            tile_y = (tile_index // across) * tile_h
            rows = min(tile_h, height - tile_y)
            span = min(tile_w, width - tile_x)
            grid[tile_y:tile_y + rows, tile_x:tile_x + span] = block[:rows, :span]
        grid[grid == _NODATA] = _np.nan
        return width, height, pixel_size, pixel_height, e_origin, n_top, grid

    values = [float("nan")] * (width * height)
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


def _anchor_lattice(width, height, pixel_size, pixel_height, e_origin,
                    n_top, mapgen_src, tif_path):
    """Exact BNG-to-UTM transforms on an anchor lattice every 128 samples.

    Between anchors the mapping is bilinear; the worst measured error
    against exact per-point transforms is 0.7 mm (Cowbridge raster,
    2026-08-07), so the mesh is Urbano-exact for every practical
    purpose. Returns (anchor_cols, anchor_rows, eastings, northings,
    zone) with eastings/northings indexed [row][col].
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
    anchor_cols = sorted(set(list(range(0, width, lattice)) + [width - 1]))
    anchor_rows = sorted(set(list(range(0, height, lattice)) + [height - 1]))
    eastings = []
    northings = []
    for row in anchor_rows:
        northing = n_top - (row + 0.5) * pixel_height
        e_row = []
        n_row = []
        for col in anchor_cols:
            easting = e_origin + (col + 0.5) * pixel_size
            lat, lon = bng_module.from_bng(easting, northing, grid)
            e_utm, n_utm = utm_module.project(lat, lon, zone)
            e_row.append(e_utm)
            n_row.append(n_utm)
        eastings.append(e_row)
        northings.append(n_row)
    return anchor_cols, anchor_rows, eastings, northings, zone


def _brackets(anchors, samples):
    """Per sample: (lower anchor index, blend fraction toward the upper)."""
    out = []
    for value in samples:
        upper = bisect.bisect_right(anchors, value)
        low = min(max(upper - 1, 0), len(anchors) - 2)
        span = anchors[low + 1] - anchors[low]
        out.append((low, 0.0 if span == 0 else (value - anchors[low]) / span))
    return out


def build_geometry(path, step=2, frame="urbano", mapgen_src=None):
    """Everything Rhino-free: vertices, quad faces, stats.

    Returns (vertices, faces, info, origin): vertices is a list of
    (x, y, z), faces a list of (a, b, c, d) vertex indices.
    """
    if step is None or int(step) < 1:
        step = 1
    step = int(step)
    frame = str(frame or "urbano").lower()
    # A Grasshopper File Path parameter turns the text "urbano" into a
    # full path ending in \urbano (it resolves relative to the document
    # folder). The intent is unambiguous either way, so take the last
    # path segment rather than erroring on a wiring choice.
    frame = frame.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].strip()
    if frame not in ("urbano", "bng"):
        raise MapgenTiffError(
            f"frame '{frame}' is not one this script knows: urbano or bng "
            f"(feed it from a text panel, or leave it unconnected for "
            f"urbano)."
        )
    (width, height, pixel_size, pixel_height,
     e_origin, n_top, values) = read_mapgen_tif(path)
    cols = list(range(0, width, step))
    rows = list(range(0, height, step))

    if frame == "urbano":
        (anchor_cols, anchor_rows, lattice_e, lattice_n,
         zone) = _anchor_lattice(width, height, pixel_size, pixel_height,
                                 e_origin, n_top, mapgen_src, path)
    else:
        zone = None

    if _np is not None:
        vertices, faces, kept, z_min, z_max = _geometry_numpy(
            values, cols, rows, frame, pixel_size, pixel_height,
            e_origin, n_top,
            None if frame == "bng" else (anchor_cols, anchor_rows,
                                         lattice_e, lattice_n))
    else:
        vertices, faces, kept, z_min, z_max = _geometry_pure(
            values, width, cols, rows, frame, pixel_size, pixel_height,
            e_origin, n_top,
            None if frame == "bng" else (anchor_cols, anchor_rows,
                                         lattice_e, lattice_n))
    if kept == 0:
        raise MapgenTiffError(
            f"{Path(path).name} has no data samples at step {step}; "
            f"nothing to mesh."
        )
    origin = (min(v[0] for v in vertices), min(v[1] for v in vertices))
    info = (
        f"{Path(path).name}: {width} x {height} px at "
        f"{pixel_size:g} m, sampled every {step} px, frame {frame}"
        f"{' zone ' + zone if zone else ''}; {kept:,} vertices, "
        f"{len(faces):,} faces, z {z_min:.2f} to {z_max:.2f} m"
        f"{'' if _np is not None else ' (numpy not found: slow path)'}"
    )
    return vertices, faces, info, origin


def _geometry_numpy(grid, cols, rows, frame, pixel_size, pixel_height,
                    e_origin, n_top, lattice):
    np = _np
    ci = np.asarray(cols)
    ri = np.asarray(rows)
    z = grid[np.ix_(ri, ci)].astype(np.float64)
    mask = np.isfinite(z)
    kept = int(mask.sum())
    if kept == 0:
        return [], [], 0, 0.0, 0.0

    if frame == "bng":
        x_line = e_origin + (ci + 0.5) * pixel_size
        y_line = n_top - (ri + 0.5) * pixel_height
        x = np.broadcast_to(x_line, z.shape)
        y = np.broadcast_to(y_line[:, None], z.shape)
    else:
        anchor_cols, anchor_rows, lattice_e, lattice_n = lattice
        ac = np.asarray(anchor_cols)
        ar = np.asarray(anchor_rows)
        le = np.asarray(lattice_e)
        ln = np.asarray(lattice_n)
        c_low = np.clip(np.searchsorted(ac, ci, side="right") - 1, 0, len(ac) - 2)
        r_low = np.clip(np.searchsorted(ar, ri, side="right") - 1, 0, len(ar) - 2)
        c_span = ac[c_low + 1] - ac[c_low]
        r_span = ar[r_low + 1] - ar[r_low]
        fc = np.where(c_span == 0, 0.0, (ci - ac[c_low]) / c_span)
        fr = np.where(r_span == 0, 0.0, (ri - ar[r_low]) / r_span)
        FC = fc[None, :]
        FR = fr[:, None]
        x = (le[np.ix_(r_low, c_low)] * (1 - FC) * (1 - FR)
             + le[np.ix_(r_low, c_low + 1)] * FC * (1 - FR)
             + le[np.ix_(r_low + 1, c_low)] * (1 - FC) * FR
             + le[np.ix_(r_low + 1, c_low + 1)] * FC * FR)
        y = (ln[np.ix_(r_low, c_low)] * (1 - FC) * (1 - FR)
             + ln[np.ix_(r_low, c_low + 1)] * FC * (1 - FR)
             + ln[np.ix_(r_low + 1, c_low)] * (1 - FC) * FR
             + ln[np.ix_(r_low + 1, c_low + 1)] * FC * FR)

    index = np.full(z.shape, -1, dtype=np.int64)
    index[mask] = np.arange(kept)
    stacked = np.column_stack((
        np.asarray(x)[mask], np.asarray(y)[mask], z[mask]))
    corner = (mask[:-1, :-1] & mask[:-1, 1:] & mask[1:, 1:] & mask[1:, :-1])
    a = index[:-1, :-1][corner]
    b = index[:-1, 1:][corner]
    c = index[1:, 1:][corner]
    d = index[1:, :-1][corner]
    # .tolist() matters: it yields NATIVE Python ints and floats. Rhino's
    # MeshFace constructor refuses numpy int64 outright (numpy floats pass
    # only because np.float64 subclasses float), so handing numpy scalars
    # onward breaks exactly and only inside Grasshopper.
    vertices = [tuple(v) for v in stacked.tolist()]
    faces = [tuple(f) for f in np.column_stack((a, b, c, d)).tolist()]
    return vertices, faces, kept, float(np.nanmin(z)), float(np.nanmax(z))


def _geometry_pure(values, width, cols, rows, frame, pixel_size,
                   pixel_height, e_origin, n_top, lattice):
    if frame == "urbano":
        anchor_cols, anchor_rows, lattice_e, lattice_n = lattice
        col_bracket = _brackets(anchor_cols, cols)
        row_bracket = _brackets(anchor_rows, rows)
    vertices = []
    index = [[-1] * len(cols) for _ in rows]
    z_min, z_max = float("inf"), float("-inf")
    for r_pos, row in enumerate(rows):
        base = row * width
        for c_pos, col in enumerate(cols):
            z = values[base + col]
            if z != z:
                continue
            if frame == "bng":
                x = e_origin + (col + 0.5) * pixel_size
                y = n_top - (row + 0.5) * pixel_height
            else:
                c0, fc = col_bracket[c_pos]
                r0, fr = row_bracket[r_pos]
                def blend(table):
                    top = table[r0][c0] + (table[r0][c0 + 1] - table[r0][c0]) * fc
                    bot = (table[r0 + 1][c0]
                           + (table[r0 + 1][c0 + 1] - table[r0 + 1][c0]) * fc)
                    return top + (bot - top) * fr
                x = blend(lattice_e)
                y = blend(lattice_n)
            index[r_pos][c_pos] = len(vertices)
            vertices.append((x, y, z))
            if z < z_min:
                z_min = z
            if z > z_max:
                z_max = z
    faces = []
    for r_pos in range(len(rows) - 1):
        top_row = index[r_pos]
        bottom_row = index[r_pos + 1]
        for c_pos in range(len(cols) - 1):
            a = top_row[c_pos]
            b = top_row[c_pos + 1]
            c = bottom_row[c_pos + 1]
            d = bottom_row[c_pos]
            if a >= 0 and b >= 0 and c >= 0 and d >= 0:
                faces.append((a, b, c, d))
    return vertices, faces, len(vertices), z_min, z_max


def build_rhino_mesh(vertices, faces, at_origin, origin):
    """The Rhino half: only importable inside Rhino/Grasshopper.

    Batched adds: one interop call for all vertices and one for all
    faces, instead of one per element, which is where the old version
    spent most of its time.
    """
    import Rhino.Geometry as rg

    shift_x, shift_y = (origin if at_origin else (0.0, 0.0))
    mesh = rg.Mesh()
    mesh.Vertices.AddVertices(
        [rg.Point3d(x - shift_x, y - shift_y, z) for (x, y, z) in vertices]
    )
    mesh.Faces.AddFaces([rg.MeshFace(a, b, c, d) for (a, b, c, d) in faces])
    mesh.Normals.ComputeNormals()
    mesh.Compact()
    return mesh


def _run_component():
    """Grasshopper entry: reads the component's inputs from globals."""
    if "run" in globals() and not globals().get("run"):
        return None, "run is False; set it to True to compute", None
    tif_path = globals().get("path")
    if not tif_path:
        return None, "connect path: the packaged _lidar_dtm.tif or _lidar_dsm.tif", None
    vertices, faces, info, origin = build_geometry(
        tif_path,
        step=globals().get("step") or 2,
        frame=globals().get("frame") or "urbano",
        mapgen_src=globals().get("mapgen_src"),
    )
    shift = bool(globals().get("at_origin"))
    built = build_rhino_mesh(vertices, faces, shift, origin)
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
    import time
    started = time.perf_counter()
    cli_vertices, cli_faces, cli_info, cli_origin = build_geometry(
        sys.argv[1],
        int(sys.argv[2]) if len(sys.argv) > 2 else 2,
        sys.argv[3] if len(sys.argv) > 3 else "bng",
        sys.argv[4] if len(sys.argv) > 4 else None,
    )
    elapsed = time.perf_counter() - started
    print(cli_info)
    print(f"built in {elapsed:.2f}s; min corner "
          f"{cli_origin[0]:.2f}, {cli_origin[1]:.2f}")
