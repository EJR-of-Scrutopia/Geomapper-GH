"""ETRS89 latitude and longitude to pseudo-British National Grid, on GRS80.

This is the projection half of the OSTN15 transform Ordnance Survey publishes
for converting ETRS89 (in practice, WGS84 to well under National Grid
accuracy) to British National Grid. OS splits the job in two: a Transverse
Mercator projection from GRS80 onto a grid whose true origin is 49 N 2 W,
producing what OS calls "pseudo-grid" coordinates, and then a small shift
grid (OSTN15) that nudges those coordinates onto the real National Grid to
correct for the fact that Great Britain's geodetic realisation predates
GRS80 and does not sit on it exactly. This module is the first half only.
The shift grid is a separate concern (see mapgen's Task 2 for that stage and
for the survey-grade validation against OS's own station test vectors); this
module's own tests pin internal consistency, namely that the true origin
projects to the false origin and that forward and inverse agree with each
other to a fraction of a millimetre, not agreement with OS to survey
accuracy.

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
"""

from __future__ import annotations

import math

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
    top of this in mapgen's Task 2, not here. This function alone is what OS
    calls the "cartesian coordinates" step of the pseudo-grid, its I through
    VI terms exactly as the guide states them.
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
        # failure to report, not a value to hand on to Task 2's shift grid.
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
    while abs(northing - _N0 - _meridional_arc(lat_prime)) >= 1e-5:
        lat_prime += (northing - _N0 - _meridional_arc(lat_prime)) / (_A * _F0)

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
