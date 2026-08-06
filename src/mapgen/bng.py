"""ETRS89 latitude and longitude to pseudo-British National Grid, on GRS80.

This is the projection half of the OSTN15 transform Ordnance Survey publishes
for converting ETRS89 (in practice, WGS84 to well under National Grid
accuracy) to British National Grid. OS splits the job in two: a Transverse
Mercator projection from GRS80 onto a grid whose true origin is 49 N 2 W,
producing what OS calls "pseudo-grid" coordinates, and then a small shift
grid (OSTN15) that nudges those coordinates onto the real National Grid to
correct for the fact that Great Britain's geodetic realisation predates
GRS80 and does not sit on it exactly. `tm_forward`/`tm_inverse` below are
the projection half alone, and their own tests pin internal consistency,
namely that the true origin projects to the false origin and that forward
and inverse agree with each other to a fraction of a millimetre, not
agreement with OS to survey accuracy. `to_bng`/`from_bng`, further down,
are the projection plus OSTN15 together, and it is those two that OS's own
station test vectors validate to survey accuracy (2 mm); see "The second
half: OSTN15" below.

Deliberately not built on `mapgen.utm`, even though both are Transverse
Mercator. `utm.py`'s whole reason to exist is to reproduce Urbano's own
projection numbers term for term; changing its ellipsoid, origin, or false
easting to National Grid's would break the thing it is pinned to prove. This
module needs GRS80 and OS's own origin, not WGS84 and Urbano's zone
convention, so it is its own copy of the same kind of arithmetic rather than
a parameterisation of that one.

The series below is Ordnance Survey's own, "A guide to coordinate systems in
Great Britain", transcribed with the guide's own term names (I, II, III,
IIIA, IV, V, VI for the forward projection and VII through XIIA for the
inverse) so the code can be read directly against the PDF rather than against
some other transverse Mercator's derivation.

## The second half: OSTN15

`to_bng`/`from_bng` below layer OSTN15 on top of `tm_forward`/`tm_inverse`,
which is the small, spatially varying correction that turns the pseudo-grid
above into the real British National Grid: a shift grid of east/north
corrections at every 1 km node of a 701 by 1251 rectangle (0 to 700 km
east, 0 to 1250 km north), bilinearly interpolated between the four nodes
around a point. The nodes come from Ordnance Survey's own developers pack
(`OSTN15_URL`), a 41 MB CSV of one row per node; that pack is downloaded
once, parsed once, and cached from then on as a 7 MB binary
(`ensure_ostn15`/`load_ostn15`), because 876,951 lines of text is not
something a survey run should pay to parse twice.

The geoid height shift column in that CSV is skipped entirely: mapgen's
LiDAR heights are already Ordnance Datum Newlyn orthometric, so there is
no ellipsoidal height anywhere in this pipeline for OSGM15 to correct.

Some nodes near the coast and offshore carry OS's own datum flag 16,
"outside transformation area": OS extrapolates a numeric shift there but
does not stand behind it as OSTN15 proper. This module treats a flag 16
node exactly like a node the file never mentioned at all (both end up NaN
in the cached grid), so `OutsideOstn15Error` only has to check for one
thing, an absent corner, to cover both cases.

Licence. The pack's own release notice,
`OSGM15_Notice_of_release_for_developers.pdf`, announces the OSGM02 to
OSGM15 and OSTN02 to OSTN15 updates but states no licence terms itself;
the operative sentence is in the pack's own
`Transformations_and_OSGM15_User_Guide.pdf`: "All three transformation
models are licensed to users pursuant to the terms of the Open Source
Initiative BSD Licence." Software incorporating the transformation must
carry the pack's own attribution: "Copyright and database rights Ordnance
Survey Limited 2016, Crown copyright and database rights Land & Property
Services 2016 and/or Ordnance Survey Ireland, 2016. All rights reserved."
"""

from __future__ import annotations

import array
import io
import math
import struct
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

import requests

from mapgen.config import CONFIG_PATH
from mapgen.fsutil import atomic_write_bytes
from mapgen.geo import BBox

# GRS80 ellipsoid, which is what OSTN15's input frame (ETRS89) is defined on.
_A = 6378137.0
_B = 6356752.314140356
# National Grid parameters: scale on the central meridian, true origin at
# 49 N 2 W, false origin 400 km west and 100 km north of it.
_F0 = 0.9996012717
_LAT0 = math.radians(49.0)
_LON0 = math.radians(-2.0)
_E0 = 400_000.0
_N0 = -100_000.0


class BngError(RuntimeError):
    """Raised when a coordinate cannot be projected to or from the grid.

    A RuntimeError rather than a ValueError because the one thing this
    module refuses is a NaN or an infinity slipping through arithmetic that
    would otherwise turn it into another NaN or infinity several steps
    downstream, silently. Every message is one plain sentence.
    """


def _nu_rho_eta2(sin_lat: float) -> tuple[float, float, float]:
    """The transverse and meridional radii of curvature at a latitude, and
    `eta2`, OS's own name for the ratio between them minus one.

    Named after the guide's own symbols (nu, rho, eta squared) because every
    one of the I..VI and VII..XIIA terms below is stated in terms of exactly
    these three, and nothing else needs them.
    """
    e2 = (_A * _A - _B * _B) / (_A * _A)
    nu = _A * _F0 / math.sqrt(1.0 - e2 * sin_lat * sin_lat)
    rho = _A * _F0 * (1.0 - e2) / (1.0 - e2 * sin_lat * sin_lat) ** 1.5
    return nu, rho, nu / rho - 1.0


def _meridional_arc(lat: float) -> float:
    """Distance along the true origin's meridian from 49 N to `lat`, OS's
    own `M`.

    Used twice: once directly in the forward projection's `I` term, and once
    inside the inverse's Newton iteration, which repeatedly asks "how far
    short of this northing does a candidate latitude's arc fall" until the
    answer is under a tenth of a millimetre.
    """
    n = (_A - _B) / (_A + _B)
    n2, n3 = n * n, n * n * n
    d, s = lat - _LAT0, lat + _LAT0
    return _B * _F0 * (
        (1.0 + n + 1.25 * n2 + 1.25 * n3) * d
        - (3.0 * n + 3.0 * n2 + 2.625 * n3) * math.sin(d) * math.cos(s)
        + (1.875 * n2 + 1.875 * n3) * math.sin(2.0 * d) * math.cos(2.0 * s)
        - (35.0 / 24.0) * n3 * math.sin(3.0 * d) * math.cos(3.0 * s)
    )


def tm_forward(latitude: float, longitude: float) -> tuple[float, float]:
    """Pseudo-grid easting and northing of an ETRS89 point, in metres.

    "Pseudo" because this is GRS80 Transverse Mercator on the National Grid's
    own origin and false coordinates, not the National Grid itself: the
    small OSTN15 correction that makes it the National Grid is layered on
    top of this by `to_bng`, further down, not here. This function alone is
    what OS calls the "cartesian coordinates" step of the pseudo-grid, its
    I through VI terms exactly as the guide states them.
    """
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        raise BngError(
            f"Cannot project latitude {latitude}, longitude {longitude}: "
            f"both must be real numbers."
        )
    lat = math.radians(latitude)
    dl = math.radians(longitude) - _LON0

    sin_lat, cos_lat = math.sin(lat), math.cos(lat)
    tan_lat = math.tan(lat)
    nu, rho, eta2 = _nu_rho_eta2(sin_lat)

    term_i = _meridional_arc(lat) + _N0
    term_ii = (nu / 2.0) * sin_lat * cos_lat
    term_iii = (nu / 24.0) * sin_lat * cos_lat**3 * (5.0 - tan_lat**2 + 9.0 * eta2)
    term_iiia = (nu / 720.0) * sin_lat * cos_lat**5 * (
        61.0 - 58.0 * tan_lat**2 + tan_lat**4
    )
    term_iv = nu * cos_lat
    term_v = (nu / 6.0) * cos_lat**3 * (nu / rho - tan_lat**2)
    term_vi = (nu / 120.0) * cos_lat**5 * (
        5.0
        - 18.0 * tan_lat**2
        + tan_lat**4
        + 14.0 * eta2
        - 58.0 * tan_lat**2 * eta2
    )

    northing = term_i + term_ii * dl**2 + term_iii * dl**4 + term_iiia * dl**6
    easting = _E0 + term_iv * dl + term_v * dl**3 + term_vi * dl**5

    if not (math.isfinite(easting) and math.isfinite(northing)):
        # Unreachable for any input that got past the guard above, and
        # checked anyway: a coordinate this module cannot vouch for is a
        # failure to report, not a value to hand on to OSTN15's shift grid.
        raise BngError(
            f"Projecting latitude {latitude}, longitude {longitude} did not "
            f"produce a real coordinate."
        )
    return easting, northing


def tm_inverse(easting: float, northing: float) -> tuple[float, float]:
    """ETRS89 latitude and longitude of a pseudo-grid point, in degrees.

    The exact inverse of `tm_forward`: it recovers the latitude by Newton
    iteration on `_meridional_arc` (there is no closed form, because the
    forward projection is not linear in latitude) and then applies OS's VII
    through XIIA terms at that latitude to reach the longitude and refine
    the latitude, exactly as the guide states them.
    """
    if not (math.isfinite(easting) and math.isfinite(northing)):
        raise BngError(
            f"Cannot unproject easting {easting}, northing {northing}: "
            f"both must be real numbers."
        )

    lat_prime = (northing - _N0) / (_A * _F0) + _LAT0
    # Capped at 10, the same Newton bound utm.py's own _angular_distance
    # uses, as a backstop rather than a budget: this fixed-point map's
    # contraction ratio is under 0.007 everywhere on the ellipsoid, so it
    # is within 1e-5 m of the true latitude in 2 or 3 rounds for any real
    # input, and the cap is never actually reached.
    for _ in range(10):
        residual = northing - _N0 - _meridional_arc(lat_prime)
        if abs(residual) < 1e-5:
            break
        lat_prime += residual / (_A * _F0)

    sin_lat = math.sin(lat_prime)
    nu, rho, eta2 = _nu_rho_eta2(sin_lat)
    t = math.tan(lat_prime)
    sec = 1.0 / math.cos(lat_prime)
    de = easting - _E0

    term_vii = t / (2.0 * rho * nu)
    term_viii = t / (24.0 * rho * nu**3) * (5.0 + 3.0 * t**2 + eta2 - 9.0 * t**2 * eta2)
    term_ix = t / (720.0 * rho * nu**5) * (61.0 + 90.0 * t**2 + 45.0 * t**4)
    term_x = sec / nu
    term_xi = sec / (6.0 * nu**3) * (nu / rho + 2.0 * t**2)
    term_xii = sec / (120.0 * nu**5) * (5.0 + 28.0 * t**2 + 24.0 * t**4)
    term_xiia = sec / (5040.0 * nu**7) * (
        61.0 + 662.0 * t**2 + 1320.0 * t**4 + 720.0 * t**6
    )

    lat = lat_prime - term_vii * de**2 + term_viii * de**4 - term_ix * de**6
    lon = _LON0 + term_x * de - term_xi * de**3 + term_xii * de**5 - term_xiia * de**7

    latitude, longitude = math.degrees(lat), math.degrees(lon)
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        raise BngError(
            f"Unprojecting easting {easting}, northing {northing} did not "
            f"produce a real coordinate."
        )
    return latitude, longitude


# --------------------------------------------------------------------------
# OSTN15: the shift grid, its cache, and the public to_bng/from_bng pair.
# --------------------------------------------------------------------------

OSTN15_URL = "https://www.ordnancesurvey.co.uk/documents/resources/OSTN15-OSGM15-DevelopersPack.zip"

# The one file this module reads out of the pack. The pack also carries an
# Ireland/Northern Ireland geoid pair, four PDFs and OS's own test input and
# output files; none of those are needed at runtime, only at fixture-build
# time (see tests/fixtures/ostn15/make_fixture.py).
_DATA_FILE_NAME = "OSTN15_OSGM15_DataFile.txt"

# Confirmed against the pack's own header line rather than trusted from
# memory: Point_ID, the node's own pseudo-grid position (not a
# measurement), the two horizontal shifts this module uses, a geoid
# height shift this module has no use for, and the datum flag.
_EXPECTED_COLUMNS = (
    "Point_ID",
    "ETRS89_Easting",
    "ETRS89_Northing",
    "ETRS89_OSGB36_EShift",
    "ETRS89_OSGB36_NShift",
    "ETRS89_ODN_HeightShift",
    "Height_Datum_Flag",
)

# OS's own datum flag for "outside transformation area" (see the module
# docstring): a node with this flag carries a number but not OS's backing,
# and is dropped exactly like a node the file never mentioned.
_OUTSIDE_DATUM_FLAG = 16

# The grid rectangle: 701 nodes east (0 to 700 km) by 1251 nodes north
# (0 to 1250 km), 1 km apart.
_GRID_COLS = 701
_GRID_ROWS = 1251
_NODE_COUNT = _GRID_COLS * _GRID_ROWS
_GRID_SPACING_M = 1000.0

_CACHE_FILENAME = "ostn15_shifts.bin"
_CACHE_MAGIC = b"OSTN15\x00\x01"

_DOWNLOAD_TIMEOUT_SECONDS = 120.0

# from_bng's iteration: OS's own published method for inverting a
# transform that has no closed form (see from_bng's own docstring).
_FROM_BNG_TOLERANCE_M = 1e-4
_FROM_BNG_MAX_ROUNDS = 10


class OutsideOstn15Error(BngError):
    """Raised when a point has no OSTN15 shift to give it.

    Either it lies beyond the 701 by 1251 node rectangle altogether, or
    one of the four nodes around it is a node OS itself flagged "outside
    transformation area" (see the module docstring). Both look identical
    by the time shift_at sees them: a missing corner, not a bad number to
    catch downstream.
    """


def _bilinear(sw: float, se: float, nw: float, ne: float, t: float, u: float) -> float:
    """OS's own bilinear form over the four nodes enclosing a point.

    `t` and `u` are the point's fractional position between the south-west
    node and the next one east and north respectively, each in [0, 1).
    Named after compass corners rather than S0..S3 (OS's own test output
    column names) because this function is one bilinear interpolation
    used twice, for east shift and north shift both, and the corner it is
    reading from at each call site is what a reader needs to check against
    the grid, not which column OS happened to print it in.
    """
    return (
        (1.0 - t) * (1.0 - u) * sw
        + t * (1.0 - u) * se
        + (1.0 - t) * u * nw
        + t * u * ne
    )


class Ostn15Grid:
    """A loaded OSTN15 shift grid, backed by one flat array.

    The array always covers the full 701 by 1251 rectangle, regardless of
    how much of it is actually populated: the real cache (parsed from OS's
    41 MB data file, finite almost everywhere) and a test fixture slice
    (NaN everywhere except a handful of named blocks, see
    tests/fixtures/ostn15/make_fixture.py) are built to the same shape on
    purpose, so shift_at treats them identically and code under test
    cannot tell which one it was handed.
    """

    def __init__(self, shifts: array.array) -> None:
        if len(shifts) != _NODE_COUNT * 2:
            raise BngError(
                "An OSTN15 grid must hold exactly one east/north shift pair "
                "for every node of the 701 by 1251 rectangle."
            )
        self._shifts = shifts

    def _node(self, col: int, row: int) -> tuple[float, float] | None:
        if not (0 <= col < _GRID_COLS and 0 <= row < _GRID_ROWS):
            return None
        index = (row * _GRID_COLS + col) * 2
        east, north = self._shifts[index], self._shifts[index + 1]
        if not (math.isfinite(east) and math.isfinite(north)):
            return None
        return east, north

    def shift_at(self, easting: float, northing: float) -> tuple[float, float]:
        """The (east, north) OSTN15 shift at a pseudo-grid point, in metres.

        Bilinear over the 2 by 2 nodes enclosing the point, in OS's own
        south-west/south-east/north-east/north-west order, verified
        against OS's own published intermediate values (RecNoS0..S3 in
        OSTN15_OSGM15_TestOutput_ETRStoOSGB.txt) rather than assumed:
        S0 is the south-west node, S1 south-east, S2 north-east, S3
        north-west, and the same ordering is how tests/fixtures/ostn15's
        stations were reproduced by hand to build this module's own
        tests.
        """
        col0 = math.floor(easting / _GRID_SPACING_M)
        row0 = math.floor(northing / _GRID_SPACING_M)
        t = easting / _GRID_SPACING_M - col0
        u = northing / _GRID_SPACING_M - row0
        sw = self._node(col0, row0)
        se = self._node(col0 + 1, row0)
        ne = self._node(col0 + 1, row0 + 1)
        nw = self._node(col0, row0 + 1)
        if sw is None or se is None or ne is None or nw is None:
            raise OutsideOstn15Error(
                "This coordinate has no OSTN15 shift: it falls outside the "
                "grid's 701 by 1251 node rectangle, or one of the nodes "
                "around it is outside OS's own transformation area."
            )
        east_shift = _bilinear(sw[0], se[0], nw[0], ne[0], t, u)
        north_shift = _bilinear(sw[1], se[1], nw[1], ne[1], t, u)
        return east_shift, north_shift


def _parse_data_file(lines: Iterable[str]) -> Ostn15Grid:
    """OSTN15's data file, as text, into an Ostn15Grid.

    One record per 1 km node: Point_ID, the node's own pseudo-grid
    easting and northing (not a measurement; it names which node this
    row is), the east and north shift, a geoid height shift this parser
    never reads (see the module docstring), and the datum flag. The
    column order is checked against the file's own header rather than
    trusted, so a future repackaging that reorders columns fails loudly
    here instead of silently swapping east and north.

    Works equally on the real 876,951-line file and on a handful of
    synthetic lines (see test_cache_roundtrip): every node this iterable
    does not mention starts, and stays, NaN.
    """
    iterator = iter(lines)
    header = next(iterator).rstrip("\n").split(",")
    if tuple(header[:7]) != _EXPECTED_COLUMNS:
        raise BngError(
            "The OSTN15 data file's columns are not in the order this "
            "parser expects."
        )

    shifts = array.array("f", (float("nan") for _ in range(_NODE_COUNT * 2)))
    for line in iterator:
        if not line.strip():
            continue
        fields = line.rstrip("\n").split(",")
        col = round(float(fields[1]) / _GRID_SPACING_M)
        row = round(float(fields[2]) / _GRID_SPACING_M)
        if not (0 <= col < _GRID_COLS and 0 <= row < _GRID_ROWS):
            raise BngError(
                "The OSTN15 data file names a node outside the expected "
                "701 by 1251 rectangle."
            )
        if int(fields[6]) == _OUTSIDE_DATUM_FLAG:
            continue  # Left as NaN: outside the transformation area.
        index = (row * _GRID_COLS + col) * 2
        shifts[index] = float(fields[3])
        shifts[index + 1] = float(fields[4])
    return Ostn15Grid(shifts)


def _cache_path(cache_dir: Path | None) -> Path:
    # CONFIG_PATH.parent rather than a second `Path.home() / ".mapgen"`:
    # config.py already owns this directory's location, and mapgen only
    # gets to change that in one place if nothing else re-derives it.
    return (cache_dir if cache_dir is not None else CONFIG_PATH.parent) / _CACHE_FILENAME


def _to_little_endian(shifts: array.array) -> array.array:
    if sys.byteorder == "little":
        return shifts
    swapped = array.array("f", shifts)
    swapped.byteswap()
    return swapped


def _write_cache(path: Path, grid: Ostn15Grid) -> None:
    body = _to_little_endian(grid._shifts).tobytes()
    header = _CACHE_MAGIC + struct.pack("<Q", _NODE_COUNT)
    atomic_write_bytes(path, header + body)


def _read_cache(path: Path) -> Ostn15Grid:
    data = path.read_bytes()
    if len(data) < 16:
        raise BngError("The OSTN15 shift-grid cache is too short to be valid.")
    magic, count = struct.unpack_from("<8sQ", data, 0)
    expected_length = 16 + count * 2 * 4
    if magic != _CACHE_MAGIC or count != _NODE_COUNT or len(data) != expected_length:
        raise BngError("The OSTN15 shift-grid cache is not in the expected format.")
    shifts = array.array("f")
    shifts.frombytes(data[16:])
    if sys.byteorder != "little":
        shifts.byteswap()
    return Ostn15Grid(shifts)


def load_ostn15(cache_dir: Path | None = None) -> Ostn15Grid | None:
    """The cached OSTN15 grid, or None if it has never been fetched.

    Never touches the network. Split out from ensure_ostn15 so a caller
    that only wants to know whether a grid is already on disk, or a test
    proving a download never happened, does not have to hand in a session
    at all.
    """
    path = _cache_path(cache_dir)
    if not path.exists():
        return None
    return _read_cache(path)


def _download_and_parse(session: object) -> Ostn15Grid:
    """The developers pack, streamed to a temp file, extracted, parsed.

    Everything about the URL stays out of any message this raises: a
    connection failure's own text from `requests` can carry it, and a
    test asserting on this module's own wording should never end up
    depending on that string too.

    Every failure on this path becomes a BngError, matching every other
    failure path in this module (parsing, cache read, grid construction,
    projection all do the same): a corrupt or truncated download raises
    `zipfile.BadZipFile`, and a pack that no longer contains the expected
    member (renamed, or moved, upstream) raises `KeyError` from
    `archive.open`; a caller catching BngError to report one clean
    "OSTN15 fetch failed" should not have to also know about two
    unrelated stdlib exception types to catch the same failure.
    """
    try:
        with tempfile.TemporaryDirectory() as work_dir:
            zip_path = Path(work_dir) / "ostn15.zip"
            with session.get(
                OSTN15_URL, stream=True, timeout=_DOWNLOAD_TIMEOUT_SECONDS
            ) as response:
                response.raise_for_status()
                with zip_path.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            try:
                with zipfile.ZipFile(zip_path) as archive, archive.open(
                    _DATA_FILE_NAME
                ) as member:
                    return _parse_data_file(io.TextIOWrapper(member, encoding="utf-8"))
            except zipfile.BadZipFile as exc:
                raise BngError(
                    "The OSTN15 developers pack download is not a valid zip file."
                ) from exc
            except KeyError as exc:
                raise BngError(
                    "The OSTN15 developers pack does not contain the expected "
                    "data file."
                ) from exc
    except requests.RequestException as exc:
        raise BngError("Failed to download the OSTN15 shift grid.") from exc


def ensure_ostn15(
    cache_dir: Path | None = None, session: object | None = None
) -> Ostn15Grid:
    """The OSTN15 grid: from cache if one exists, fetched and cached if not.

    The fetch happens at most once per cache_dir: after this call the 41 MB
    data file is gone (it lived only in a temp directory) and the 7 MB
    binary cache is what every later call, in this process or the next
    one, reads instead.

    A cache file that exists but fails `_read_cache`'s own magic, node
    count or length checks (`BngError`, raised through `load_ostn15`) is
    treated as though no cache were there at all, not let escape: whether
    that file loads is a fact about that file, not about the network, and
    letting the load failure propagate here would report every later call
    as a download problem while leaving the actual, sufficient fix,
    refetching and overwriting that one file, undone. The alternative is a
    failure sticky until the owner is told, somewhere, to go and delete
    `~/.mapgen/ostn15_shifts.bin` by hand; self-healing costs one refetch
    and needs telling no one anything.
    """
    try:
        cached = load_ostn15(cache_dir)
    except BngError:
        cached = None
    if cached is not None:
        return cached
    active_session = session if session is not None else requests.Session()
    grid = _download_and_parse(active_session)
    _write_cache(_cache_path(cache_dir), grid)
    return grid


def to_bng(latitude: float, longitude: float, grid: Ostn15Grid) -> tuple[float, float]:
    """ETRS89 latitude/longitude to real British National Grid easting/northing.

    The whole transform in two steps: project to pseudo-grid, then add
    OSTN15's shift at that pseudo-grid point. Raises OutsideOstn15Error,
    via grid.shift_at, for a point OSTN15 does not cover.
    """
    easting, northing = tm_forward(latitude, longitude)
    east_shift, north_shift = grid.shift_at(easting, northing)
    return easting + east_shift, northing + north_shift


def from_bng(easting: float, northing: float, grid: Ostn15Grid) -> tuple[float, float]:
    """British National Grid easting/northing back to ETRS89 latitude/longitude.

    OSTN15 has no closed-form inverse: the shift to subtract depends on
    the pseudo-grid position, which is the very thing being solved for.
    OS's own published method is this fixed-point iteration: sample the
    shift at the current estimate of the pseudo-grid point, subtract it
    from the real coordinate to get the next estimate, and repeat until
    it stops moving. It settles to well under a millimetre in 2 rounds
    for every point OS publishes a test vector for; the cap of 10 is a
    backstop against a pathological input, not a budget this ever spends.
    """
    e_prime, n_prime = easting, northing
    for _ in range(_FROM_BNG_MAX_ROUNDS):
        east_shift, north_shift = grid.shift_at(e_prime, n_prime)
        next_e = easting - east_shift
        next_n = northing - north_shift
        converged = (
            abs(next_e - e_prime) < _FROM_BNG_TOLERANCE_M
            and abs(next_n - n_prime) < _FROM_BNG_TOLERANCE_M
        )
        e_prime, n_prime = next_e, next_n
        if converged:
            break
    return tm_inverse(e_prime, n_prime)


def padded_bng_extent(
    bbox: BBox, grid: Ostn15Grid, pad_metres: float
) -> tuple[float, float, float, float]:
    """`bbox`'s two corners projected to BNG, padded by `pad_metres` on
    every side, as `(e_min, n_min, e_max, n_max)`.

    Promoted here from `mapgen.sources.lidar_wales._padded_bng_extent`
    (Task 4 of the INSPIRE curves plan), which needed the identical
    arithmetic for a second caller (`mapgen.sources.inspire`): the extent
    `parcels_in` filters against has to be built the same way, corner
    projection, min/max, and pad, or the two sources would disagree about
    where "the survey extent" actually is for what is otherwise the same
    bbox. One home rather than two copies drifting apart.

    `pad_metres` is a parameter, not a name this module imports, on
    purpose: `egrid.PAD_METRES`, the value both current callers pass, is
    the elevation grid's own padding constant, and `egrid.py` already
    imports FROM this module (`BngError`, `Ostn15Grid`); importing
    `PAD_METRES` back the other way would make the two modules mutually
    dependent for no reason this function needs. This module (the
    lowest-level BNG/OSTN15 layer) stays free of that, and every caller
    is explicit about which pad it means.

    Both corners are projected and min/maxed, not just (south, west)
    assumed to be the low corner: OSTN15's shift is spatially varying, so
    a "rotated-ish" projection can in principle leave either corner the
    more easterly or northerly of the two. Raises `BngError` (via
    `to_bng`) for a corner OSTN15 does not cover.
    """
    e1, n1 = to_bng(bbox.south, bbox.west, grid)
    e2, n2 = to_bng(bbox.north, bbox.east, grid)
    e_min, e_max = min(e1, e2) - pad_metres, max(e1, e2) + pad_metres
    n_min, n_max = min(n1, n2) - pad_metres, max(n1, n2) + pad_metres
    return e_min, n_min, e_max, n_max
