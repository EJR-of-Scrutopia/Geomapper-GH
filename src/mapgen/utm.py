"""WGS84 latitude and longitude to UTM, in Urbano's own convention.

Urbano's world origin is two easting/northing pairs and a zone string such as
`30U`, and it does not recompute them: whatever a project setting file says is
what every piece of downstream geometry is placed against. So this has to
agree with Urbano rather than merely be a correct UTM projection, and the two
are not automatically the same thing. This module reproduces Urbano's own
`Urbano.Core.Helpers.GeoProjector`, which is:

  * a zone number of `floor((longitude + 180) / 6 + 1)`,
  * a latitude band letter indexed out of `CDEFGHJKLMNPQRSTUVWX` at
    `floor(latitude / 8 + 10)`,
  * the standard Norway and Svalbard zone exceptions,
  * and then DotSpatial's `TransverseMercator`, which is a port of PROJ.4's
    `tmerc`, with the WGS84 ellipsoid, scale factor 0.9996, a false easting
    of 500000 and a false northing of 10000000 in the southern bands.

Written out here rather than taken from a library because mapgen has no
third-party dependencies and is not going to grow one for 90 lines of
arithmetic, and because a library would give the right answer in ITS
convention, which is the one thing that would not help.

Two things confirm it rather than one, since a projection tested only against
itself proves nothing:

  * Urbano's own numbers. `docs/urbano/sample_project_setting.json` came out
    of Urbano's serialiser for a London bounding box, and Urbano's embedded
    project setting template carries a New York one. Both are reproduced to
    within a nanometre. See tests/test_utm.py.
  * An independent formulation. The tests also carry a Krueger n-series
    transverse Mercator, which shares no term with the series below, and the
    two agree to within 16 micrometres everywhere they have been compared,
    including at zone edges and in the southern hemisphere.
"""

from __future__ import annotations

import math

# WGS84. The same numbers DotSpatial's own spheroid holds, and the ones every
# UTM definition uses; they are not a mapgen choice.
EQUATORIAL_RADIUS_M = 6378137.0
FLATTENING = 1.0 / 298.257223563
SCALE_FACTOR = 0.9996
FALSE_EASTING_M = 500000.0
FALSE_NORTHING_SOUTH_M = 10000000.0

# Urbano indexes this string directly, so it is the whole of what a zone
# letter can be. C to M are the southern bands and N to X the northern ones,
# which is how the hemisphere is decided: from the BAND, exactly as Urbano
# does it, never from the sign of a latitude that has already been turned
# into a band.
LATITUDE_BANDS = "CDEFGHJKLMNPQRSTUVWX"
SOUTHERN_BANDS = LATITUDE_BANDS[: LATITUDE_BANDS.index("N")]

# The band table covers -80 to +80 in eight degree steps and stops there.
# Urbano's own guard says it accepts up to +84, which the letter X nominally
# reaches, but its band lookup indexes position 20 of a 20 character string
# for anything at +80 or above and throws before it ever projects. Refused
# here with a sentence instead, because a survey that far north is not
# something this tool has any other reason to support and quietly producing
# a coordinate Urbano itself cannot produce would be worse than refusing.
MIN_LATITUDE = -80.0
MAX_LATITUDE = 80.0


class ProjectionError(ValueError):
    """Raised when a coordinate cannot be projected into Urbano's UTM.

    A ValueError subclass for the same reason every other refusal in this
    project is one: it is a statement that the input cannot be honoured, and
    it carries one plain sentence rather than a traceback.
    """


def _zone_number(latitude: float, longitude: float, band: str) -> int:
    """The UTM zone number, including the Norway and Svalbard exceptions.

    The exceptions are real UTM, not an Urbano quirk: zone 32V is widened
    west to cover south west Norway, and the X band zones are rearranged
    around Svalbard. They are copied from Urbano's own code in the same order
    and with the same comparisons, because the whole point of this module is
    to produce the string Urbano would have produced. Nothing mapgen is used
    for goes near them; they cost seven lines and remove a class of silent
    disagreement.
    """
    zone = int(math.floor((longitude + 180.0) / 6.0 + 1.0))
    if zone == 31 and band == "V" and longitude >= 3.0:
        zone += 1
    if zone == 32 and band == "X" and longitude < 9.0:
        zone -= 1
    if zone == 32 and band == "X" and longitude >= 9.0:
        zone += 1
    if zone == 34 and band == "X" and longitude < 21.0:
        zone -= 1
    if zone == 34 and band == "X" and longitude >= 21.0:
        zone += 1
    if zone == 36 and band == "X" and longitude < 33.0:
        zone -= 1
    if zone == 36 and band == "X" and longitude >= 33.0:
        zone += 1
    return zone


def utm_zone(latitude: float, longitude: float) -> str:
    """Urbano's zone string for a point, for example `30U`.

    This is `GeoProjector.FindUTM`, and the string it returns is what Urbano
    carries in `CoordinateReference.Utm` and hands to every downstream
    component that has to project anything else onto the same grid.
    """
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        raise ProjectionError(
            f"Cannot project latitude {latitude}, longitude {longitude}: "
            f"both must be real numbers."
        )
    if not MIN_LATITUDE <= latitude < MAX_LATITUDE:
        raise ProjectionError(
            f"Latitude {latitude} is outside the UTM band table, which covers "
            f"{MIN_LATITUDE} to {MAX_LATITUDE} degrees. Urbano cannot place a "
            f"survey there either."
        )
    if not -180.0 <= longitude < 180.0:
        raise ProjectionError(
            f"Longitude {longitude} is outside -180 to 180 degrees."
        )
    band = LATITUDE_BANDS[int(math.floor(latitude / 8.0 + 10.0))]
    return f"{_zone_number(latitude, longitude, band)}{band}"


def _meridional_coefficients(es: float) -> tuple[float, float, float, float, float]:
    """PROJ.4's `pj_enfn`, which is DotSpatial's `MeridionalDistance.GetEn`."""
    es2 = es * es
    return (
        1.0 - es * (0.25 + es * (3.0 / 64.0 + es * (5.0 / 256.0 + es * 0.01068115234375))),
        es * (0.75 - es * (3.0 / 64.0 + es * (5.0 / 256.0 + es * 0.01068115234375))),
        es2 * (15.0 / 32.0 - es * (5.0 / 384.0 + es * 0.007120768229166667)),
        (es2 * es) * (35.0 / 96.0 - es * 0.005696614583333333),
        es2 * es2 * 0.3076171875,
    )


def _meridional_length(
    phi: float, sin_phi: float, cos_phi: float, en: tuple[float, ...]
) -> float:
    """PROJ.4's `pj_mlfn`: distance along the meridian from the equator."""
    cos_phi = cos_phi * sin_phi
    sin_phi = sin_phi * sin_phi
    return en[0] * phi - cos_phi * (
        en[1] + sin_phi * (en[2] + sin_phi * (en[3] + sin_phi * en[4]))
    )


def _angular_distance(arg: float, es: float, en: tuple[float, ...]) -> float:
    """PROJ.4's `pj_inv_mlfn`, which is DotSpatial's
    `MeridionalDistance.AngularDistance`: the latitude whose meridional
    length is `arg`, by Newton iteration on _meridional_length.

    Ten iterations at most and an absolute tolerance of 1e-11 radians, both
    of them Urbano's own numbers rather than a choice made here. Urbano
    returns the last iterate rather than raising when it does not converge,
    and so does this: a projection that quietly disagreed with Urbano about
    when to give up would be worse than one that agrees with it about
    everything.
    """
    one_minus_es = 1.0 / (1.0 - es)
    phi = arg
    for _ in range(10):
        sin_phi = math.sin(phi)
        con = 1.0 - es * sin_phi * sin_phi
        step = (
            (_meridional_length(phi, sin_phi, math.cos(phi), en) - arg)
            * (con * math.sqrt(con))
            * one_minus_es
        )
        phi -= step
        if abs(step) < 1e-11:
            return phi
    return phi


def _parse_zone(zone: str) -> tuple[int, str]:
    """A zone string such as `30U` as its number and its band letter.

    One function rather than two copies, because project and unproject have
    to refuse exactly the same set of strings: a zone one of them accepts
    and the other does not would be a pair that cannot round trip.
    """
    band = zone[-1:].upper()
    try:
        zone_number = int(zone[:-1])
    except ValueError:
        raise ProjectionError(f"{zone!r} is not a UTM zone string such as 30U.") from None
    if band not in LATITUDE_BANDS or not 1 <= zone_number <= 60:
        raise ProjectionError(f"{zone!r} is not a UTM zone string such as 30U.")
    return zone_number, band


def central_meridian(zone_number: int) -> float:
    """The longitude of a zone's central meridian, in degrees.

    Named rather than inlined because it is the only place the zone number
    enters the projection arithmetic at all, in either direction.
    """
    return -183.0 + 6.0 * zone_number


def project(latitude: float, longitude: float, zone: str) -> tuple[float, float]:
    """Easting and northing of a point on the grid of a given zone string.

    This is Urbano's three argument `GeoProjector.LatLongToUTM(lat, lon,
    utmZone)` overload, and taking the zone as an argument rather than
    deriving it per point is deliberate: see mapgen.urbano.world_origin for
    why the two corners of one bounding box must share a zone.

    The zone string's letter decides the hemisphere, exactly as Urbano's
    overload does, so a point can be projected onto a neighbouring zone's
    grid without the projection quietly changing its mind about the false
    northing.

    The series is PROJ.4's `tmerc` forward, term for term and in the same
    evaluation order as DotSpatial's port of it. The order matters more than
    it looks: rearranging arithmetically equivalent terms moves the last
    couple of digits, and the whole claim this module makes is that its
    numbers are Urbano's numbers.
    """
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        raise ProjectionError(
            f"Cannot project latitude {latitude}, longitude {longitude}: "
            f"both must be real numbers."
        )
    zone_number, band = _parse_zone(zone)

    es = FLATTENING * (2.0 - FLATTENING)
    en = _meridional_coefficients(es)
    esp = es / (1.0 - es)

    phi = math.radians(latitude)
    # The zone's central meridian, and the only place the zone number enters
    # the arithmetic at all.
    lam = math.radians(longitude) - math.radians(central_meridian(zone_number))

    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    t = (sin_phi / cos_phi) if abs(cos_phi) > 1e-10 else 0.0
    t *= t
    al = cos_phi * lam
    als = al * al
    al /= math.sqrt(1.0 - es * sin_phi * sin_phi)
    n = esp * cos_phi * cos_phi

    x = SCALE_FACTOR * al * (
        1.0
        + 1.0 / 6.0 * als * (
            1.0 - t + n
            + 0.05 * als * (
                5.0 + t * (t - 18.0) + n * (14.0 - 58.0 * t)
                + 1.0 / 42.0 * als * (61.0 + t * (t * (179.0 - t) - 479.0))
            )
        )
    )
    # phi0 is zero for every UTM zone, so PROJ's ml0 term is zero and is not
    # written out here. Said rather than silently dropped, because its
    # absence is the one place this departs textually from the code it is
    # reproducing.
    y = SCALE_FACTOR * (
        _meridional_length(phi, sin_phi, cos_phi, en)
        + sin_phi * al * lam * 0.5 * (
            1.0
            + 1.0 / 12.0 * als * (
                5.0 - t + n * (9.0 + 4.0 * n)
                + 1.0 / 30.0 * als * (
                    61.0 + t * (t - 58.0) + n * (270.0 - 330.0 * t)
                    + 1.0 / 56.0 * als * (1385.0 + t * (t * (543.0 - t) - 3111.0))
                )
            )
        )
    )

    easting = EQUATORIAL_RADIUS_M * x + FALSE_EASTING_M
    northing = EQUATORIAL_RADIUS_M * y + (
        FALSE_NORTHING_SOUTH_M if band in SOUTHERN_BANDS else 0.0
    )
    if not (math.isfinite(easting) and math.isfinite(northing)):
        # Unreachable for any input that got past the guards above, and
        # checked anyway: a NaN or an infinity written into a project
        # setting breaks both of Urbano's readers, which use default
        # JsonSerializer options and reject named float literals outright.
        # A number this module cannot vouch for is a failure to report, not
        # a value to hand on.
        raise ProjectionError(
            f"Projecting latitude {latitude}, longitude {longitude} into zone "
            f"{zone} did not produce a real coordinate."
        )
    return easting, northing


def unproject(easting: float, northing: float, zone: str) -> tuple[float, float]:
    """Latitude and longitude of a point given as a grid coordinate.

    The exact inverse of `project`, and it exists for one reason: a UTM
    elevation grid is built by walking its own nodes and asking a WGS84 DEM
    what is under each of them, so every node needs its latitude and
    longitude. See mapgen.egrid.

    This is Urbano's `GeoProjector.UTMToLatLong(easting, northing, utmZone)`,
    which hands the pair to DotSpatial's `Reproject.ReprojectPoints` and gets
    back `TransverseMercator.EllipticalInverse`. That is PROJ.4's `tmerc`
    inverse, term for term, and it is written out below in DotSpatial's own
    evaluation order for the same reason `project` is: the claim this module
    makes is that its numbers are Urbano's numbers, and rearranging
    arithmetically equivalent terms moves the last couple of digits.

    Nothing here is a second projection. The series is the inverse of the one
    above, it takes the same ellipsoid constants and the same zone
    arithmetic, and `tests/test_utm.py` pins the round trip both ways as well
    as checking every value against Urbano's own `UTMToLatLong`.

    The latitude that comes back is NOT range checked, unlike `project`'s
    input. A grid node is a place to sample a raster, not a coordinate
    reference mapgen has to stand behind, and Urbano's own inverse returns
    whatever the series gives for a northing far outside the band table. The
    zone string is checked, because a zone this cannot read is a caller
    error rather than a datum.
    """
    if not (math.isfinite(easting) and math.isfinite(northing)):
        raise ProjectionError(
            f"Cannot unproject easting {easting}, northing {northing}: "
            f"both must be real numbers."
        )
    zone_number, band = _parse_zone(zone)

    es = FLATTENING * (2.0 - FLATTENING)
    en = _meridional_coefficients(es)
    esp = es / (1.0 - es)

    x = (easting - FALSE_EASTING_M) / EQUATORIAL_RADIUS_M
    y = (
        northing - (FALSE_NORTHING_SOUTH_M if band in SOUTHERN_BANDS else 0.0)
    ) / EQUATORIAL_RADIUS_M

    # phi0 is zero for every UTM zone, so PROJ's ml0 term is zero and is not
    # written out here, exactly as in `project`.
    phi = _angular_distance(y / SCALE_FACTOR, es, en)
    if abs(phi) >= math.pi / 2.0:
        # A northing past the pole. Urbano returns the pole and a longitude
        # of zero rather than raising, and so does this.
        phi = -math.pi / 2.0 if y < 0.0 else math.pi / 2.0
        lam = 0.0
    else:
        sin_phi = math.sin(phi)
        cos_phi = math.cos(phi)
        t = (sin_phi / cos_phi) if abs(cos_phi) > 1e-10 else 0.0
        n = esp * cos_phi * cos_phi
        con = 1.0 - es * sin_phi * sin_phi
        d = x * math.sqrt(con) / SCALE_FACTOR
        con *= t
        t *= t
        ds = d * d
        phi -= (
            con * ds / (1.0 - es) * 0.5 * (
                1.0
                - ds * (1.0 / 12.0) * (
                    5.0 + t * (3.0 - 9.0 * n) + n * (1.0 - 4.0 * n)
                    - ds * (1.0 / 30.0) * (
                        61.0 + t * (90.0 - 252.0 * n + 45.0 * t) + 46.0 * n
                        - ds * (1.0 / 56.0) * (
                            1385.0 + t * (3633.0 + t * (4095.0 + 1574.0 * t))
                        )
                    )
                )
            )
        )
        lam = d * (
            1.0
            - ds * (1.0 / 6.0) * (
                1.0 + 2.0 * t + n
                - ds * 0.05 * (
                    5.0 + t * (28.0 + 24.0 * t + 8.0 * n) + 6.0 * n
                    - ds * (1.0 / 42.0) * (
                        61.0 + t * (662.0 + t * (1320.0 + 720.0 * t))
                    )
                )
            )
        ) / cos_phi

    latitude = math.degrees(phi)
    longitude = math.degrees(lam) + central_meridian(zone_number)
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        raise ProjectionError(
            f"Unprojecting easting {easting}, northing {northing} from zone "
            f"{zone} did not produce a real coordinate."
        )
    return latitude, longitude
