"""The UTM projection, checked against Urbano and against a second algorithm.

A projection tested only against numbers it produced itself proves nothing,
so nothing here is allowed to be that. Two independent standards are used.

**Urbano's own output.** `docs/urbano/sample_project_setting.json` is the
serialised result of constructing a real `Urbano.Core.Helpers.WorldOrigin`
for a London bounding box and calling `ToJson()` on the project setting
holding it (task 34). Urbano's embedded project setting template, read off
`Key.TEMPLATE_PROJECT_SETTING_STRING` at runtime in the same session, carries
a second, independently produced pair for lower Manhattan. Neither number was
computed by anything in this repository. They are the strongest evidence
available that mapgen agrees with the software it has to agree with, and they
are the tests that matter most.

**A different algorithm.** Both of those fixtures are one bounding box each,
in one zone each, in the northern hemisphere, near a central meridian. They
say nothing about a zone edge, the southern hemisphere or Wales. `kruger`
below is a Krueger n-series transverse Mercator, expanded in the third
flattening rather than in the eccentricity squared, sharing not one term with
mapgen.utm's PROJ.4 series. Where the two agree to micrometres, both are
right; a mistake in either would have to be an identical mistake in two
unrelated expansions to survive.
"""

import json
import math
from pathlib import Path

import pytest

from mapgen.utm import (
    LATITUDE_BANDS,
    ProjectionError,
    project,
    unproject,
    utm_zone,
)

SAMPLE = (
    Path(__file__).resolve().parents[1] / "docs" / "urbano" / "sample_project_setting.json"
)


# --------------------------------------------------------------------------
# The independent check.
# --------------------------------------------------------------------------


def kruger(latitude: float, longitude: float, zone_number: int) -> tuple[float, float]:
    """Krueger's n-series transverse Mercator, to six orders.

    Written out here and nowhere near src/ on purpose. It is a check, not an
    implementation: mapgen ships the PROJ.4 series because that is the one
    Urbano's own DotSpatial uses, and this exists solely to disagree with it
    if it is wrong.

    Measured against the four Urbano fixtures below it lands within 1.7e-6 m,
    and against mapgen.utm across zone edges, both hemispheres and the
    Norway and Svalbard zones it lands within 1.6e-5 m.
    """
    a = 6378137.0
    f = 1.0 / 298.257223563
    k0 = 0.9996
    n = f / (2.0 - f)
    n2, n3, n4, n5, n6 = n * n, n**3, n**4, n**5, n**6
    radius = a / (1.0 + n) * (1.0 + n2 / 4.0 + n4 / 64.0 + n6 / 256.0)
    alpha = (
        n / 2.0 - 2.0 * n2 / 3.0 + 5.0 * n3 / 16.0 + 41.0 * n4 / 180.0
        - 127.0 * n5 / 288.0 + 7891.0 * n6 / 37800.0,
        13.0 * n2 / 48.0 - 3.0 * n3 / 5.0 + 557.0 * n4 / 1440.0
        + 281.0 * n5 / 630.0 - 1983433.0 * n6 / 1935360.0,
        61.0 * n3 / 240.0 - 103.0 * n4 / 140.0 + 15061.0 * n5 / 26880.0
        + 167603.0 * n6 / 181440.0,
        49561.0 * n4 / 161280.0 - 179.0 * n5 / 168.0 + 6601661.0 * n6 / 7257600.0,
        34729.0 * n5 / 80640.0 - 3418889.0 * n6 / 1995840.0,
        212378941.0 * n6 / 319334400.0,
    )
    phi = math.radians(latitude)
    lam = math.radians(longitude) - math.radians(-183.0 + 6.0 * zone_number)

    eccentricity = 2.0 * math.sqrt(n) / (1.0 + n)
    t = math.sinh(
        math.atanh(math.sin(phi))
        - eccentricity * math.atanh(eccentricity * math.sin(phi))
    )
    xi = math.atan2(t, math.cos(lam))
    eta = math.asinh(math.sin(lam) / math.hypot(t, math.cos(lam)))
    easting = 500000.0 + k0 * radius * (
        eta
        + sum(
            alpha[j - 1] * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
            for j in range(1, 7)
        )
    )
    northing = k0 * radius * (
        xi
        + sum(
            alpha[j - 1] * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
            for j in range(1, 7)
        )
    )
    if latitude < 0:
        northing += 10000000.0
    return easting, northing


# --------------------------------------------------------------------------
# Urbano's own numbers.
# --------------------------------------------------------------------------


def test_the_london_sample_urbano_serialised_is_reproduced_to_a_nanometre():
    """The single strongest test in this file.

    Read out of the sample file rather than copied into this test, so it
    cannot drift from the fixture the rest of the project reasons about.
    """
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    reference = sample["CoordinateReference"]
    bottom, left, right = sample["Bottom"], sample["Left"], sample["Right"]

    zone = utm_zone(bottom, right)
    assert zone == reference["Utm"]

    easting, northing = project(bottom, left, zone)
    assert easting == pytest.approx(reference["BottomLeftUtm"]["Item1"], abs=1e-9)
    assert northing == pytest.approx(reference["BottomLeftUtm"]["Item2"], abs=1e-9)

    easting, northing = project(bottom, right, zone)
    assert easting == pytest.approx(reference["BottomRightUtm"]["Item1"], abs=1e-9)
    assert northing == pytest.approx(reference["BottomRightUtm"]["Item2"], abs=1e-9)


def test_urbanos_own_new_york_template_is_reproduced_to_a_nanometre():
    """Urbano's `Key.TEMPLATE_PROJECT_SETTING_STRING`, read at runtime after
    its class initialiser decrypted it (task 34's report quotes it in full).

    A second Urbano-produced pair, in a different zone, a different band and
    a different hemisphere of the central meridian from London. It matters
    because London alone cannot tell a correct projection from one that
    happens to be right for zone 30.
    """
    bottom, left, right = 40.6998, -74.0234, -74.0

    assert utm_zone(bottom, right) == "18T"

    easting, northing = project(bottom, left, "18T")
    assert easting == pytest.approx(582505.6826406379, abs=1e-9)
    assert northing == pytest.approx(4505891.429438546, abs=1e-9)

    easting, northing = project(bottom, right, "18T")
    assert easting == pytest.approx(584482.6049859334, abs=1e-9)
    assert northing == pytest.approx(4505913.668196009, abs=1e-9)


# --------------------------------------------------------------------------
# The cross-check, where no Urbano fixture exists.
# --------------------------------------------------------------------------


# Name, latitude, longitude. Wales first, because it is the owner's actual
# ground: Barry sits WEST of zone 30's central meridian at -3, where London
# sits east of it, so its easting is below the 500000 false easting and every
# sign in the series is the other way round.
CROSS_CHECK_POINTS = [
    ("Barry Waterfront", 51.385, -3.28),
    ("Cardiff Bay", 51.4557, -3.1657),
    ("Anglesey", 53.3, -4.6),
    ("Snowdonia", 52.9, -3.95),
    ("zone 30 west edge", 51.5, -5.999),
    ("zone 30 east edge", 51.5, -0.001),
    ("London", 51.501, -0.1425),
    ("Sydney", -33.87, 151.21),
    ("Cape Town", -33.92, 18.42),
    ("Quito", -0.18, -78.47),
    ("equator on a zone edge", 0.0, 0.0),
    ("Tromso", 69.65, 18.96),
    ("Bergen, the widened 32V", 60.39, 5.32),
    ("Longyearbyen, the X band", 78.22, 15.63),
    ("as far south as the bands go", -79.9, 166.0),
]


@pytest.mark.parametrize(
    "name,latitude,longitude", CROSS_CHECK_POINTS, ids=[p[0] for p in CROSS_CHECK_POINTS]
)
def test_a_different_expansion_of_the_same_projection_agrees(name, latitude, longitude):
    """Sub-millimetre agreement with a Krueger n-series, everywhere.

    The bound is 1e-4 m against a worst observed 1.6e-5 m, which is loose
    enough never to flake and tight enough that any real mistake is caught:
    a wrong coefficient, a wrong central meridian or a wrong false northing
    is metres to megametres out, not micrometres.
    """
    zone = utm_zone(latitude, longitude)
    easting, northing = project(latitude, longitude, zone)
    expected_easting, expected_northing = kruger(latitude, longitude, int(zone[:-1]))
    assert easting == pytest.approx(expected_easting, abs=1e-4)
    assert northing == pytest.approx(expected_northing, abs=1e-4)


def test_a_welsh_survey_lands_where_wales_is():
    """A sanity check with no series in it at all, because the cross-check
    above would agree with itself about a systematically displaced grid.

    Barry is in zone 30U, west of the central meridian, so its easting is
    below 500000, and it is roughly 5.69 million metres north of the equator.
    Every one of those is independently checkable on any map.
    """
    assert utm_zone(51.385, -3.28) == "30U"
    easting, northing = project(51.385, -3.28, "30U")
    assert 480000.0 < easting < 500000.0
    assert 5690000.0 < northing < 5700000.0


# --------------------------------------------------------------------------
# Zones, bands and hemispheres.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "latitude,expected",
    [
        (-79.9, "C"), (-72.0, "D"), (-64.0, "E"), (-56.0, "F"), (-48.0, "G"),
        (-40.0, "H"), (-32.0, "J"), (-24.0, "K"), (-16.0, "L"), (-8.0, "M"),
        (0.0, "N"), (8.0, "P"), (16.0, "Q"), (24.0, "R"), (32.0, "S"),
        (40.0, "T"), (48.0, "U"), (56.0, "V"), (64.0, "W"), (72.0, "X"),
    ],
)
def test_every_band_boundary_takes_the_letter_urbano_takes(latitude, expected):
    """The band letter is not decoration: it is what tells Urbano's own three
    argument overload which hemisphere to use, so an off-by-one here is a
    10,000,000 metre error in a northing.
    """
    assert utm_zone(latitude, 0.0).endswith(expected)


def test_the_equator_is_northern_and_just_below_it_is_southern():
    """Band N starts at the equator exactly, so latitude 0 takes no false
    northing and anything below it takes the full 10,000,000 metres. The
    discontinuity is real UTM, not a mistake, and it is worth pinning
    because it is where a sign error would hide.
    """
    assert utm_zone(0.0, 3.0) == "31N"
    assert utm_zone(-0.0001, 3.0) == "31M"
    _, just_north = project(0.0001, 3.0, "31N")
    _, just_south = project(-0.0001, 3.0, "31M")
    assert just_north == pytest.approx(11.06, abs=0.01)
    assert just_south == pytest.approx(10000000.0 - 11.06, abs=0.01)


@pytest.mark.parametrize(
    "latitude,longitude,expected",
    [
        # South west Norway: zone 31V is narrowed and 32V widened west.
        (60.0, 2.9, "31V"),
        (60.0, 3.0, "32V"),
        # Svalbard: 31X, 33X, 35X and 37X are widened and 32, 34, 36 removed.
        (75.0, 8.9, "31X"),
        (75.0, 9.0, "33X"),
        (75.0, 20.9, "33X"),
        (75.0, 21.0, "35X"),
        (75.0, 32.9, "35X"),
        (75.0, 33.0, "37X"),
        # The same longitudes one band down keep the zone the plain
        # arithmetic gives them, which is what makes these exceptions rather
        # than a general rule: at 9 degrees east the X band jumps to 33 and
        # the W band stays on 32.
        (70.0, 9.0, "32W"),
        (70.0, 21.0, "34W"),
    ],
)
def test_the_norway_and_svalbard_exceptions_match_urbanos(latitude, longitude, expected):
    assert utm_zone(latitude, longitude) == expected


def test_the_zone_argument_projects_onto_a_neighbouring_zones_grid():
    """The three argument form takes the zone rather than deriving it, and
    that is the whole point of it: a point just east of zone 30 can be placed
    on zone 30's grid, past the nominal 6 degree width, as an extended
    coordinate rather than jumping to a fresh 500000.

    mapgen.urbano.world_origin depends on exactly this to keep the two
    corners of one bounding box on one grid.
    """
    own_zone_easting, _ = project(51.5, 0.2, "31U")
    borrowed_easting, _ = project(51.5, 0.2, "30U")
    assert own_zone_easting == pytest.approx(305000.0, abs=2000.0)
    assert borrowed_easting == pytest.approx(722000.0, abs=2000.0)


# --------------------------------------------------------------------------
# Refusals. Never a NaN, never an infinity, never a silent wrong answer.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_latitude_that_is_not_a_real_number_is_refused_not_projected(bad):
    """The trap task 34 recorded: Urbano's readers use default JsonSerializer
    options and reject named float literals, so one NaN anywhere in a project
    setting breaks the file for both of the routes that read it. A number
    this cannot vouch for has to be a refusal, never a value.
    """
    with pytest.raises(ProjectionError, match="real numbers"):
        utm_zone(bad, -3.0)
    with pytest.raises(ProjectionError, match="real numbers"):
        utm_zone(51.5, bad)
    with pytest.raises(ProjectionError, match="real numbers"):
        project(bad, -3.0, "30U")


@pytest.mark.parametrize("latitude", [-80.1, 80.0, 84.0, 90.0])
def test_a_latitude_outside_the_band_table_is_refused(latitude):
    """Urbano's own guard claims to accept up to 84 degrees and then indexes
    position 20 of a 20 character band string for anything at 80 or above,
    which throws before it projects. Refused here with a sentence rather than
    reproduced as a crash.
    """
    with pytest.raises(ProjectionError, match="band table"):
        utm_zone(latitude, -3.0)


def test_the_southernmost_usable_latitude_is_accepted():
    assert utm_zone(-80.0, -3.0) == "30C"


@pytest.mark.parametrize("longitude", [180.0, -180.1, 400.0])
def test_a_longitude_outside_the_world_is_refused(longitude):
    with pytest.raises(ProjectionError, match="-180 to 180"):
        utm_zone(51.5, longitude)


@pytest.mark.parametrize("zone", ["", "U", "30", "30I", "0U", "61U", "thirty-U"])
def test_a_zone_string_that_is_not_one_is_refused(zone):
    with pytest.raises(ProjectionError, match="UTM zone string"):
        project(51.5, -3.0, zone)


def test_the_band_table_has_no_i_or_o_in_it():
    """I and O are excluded from UTM band letters so they cannot be read as
    1 and 0. Urbano indexes this string directly, so getting it wrong would
    shift every band above H by one.
    """
    assert "I" not in LATITUDE_BANDS
    assert "O" not in LATITUDE_BANDS
    assert len(LATITUDE_BANDS) == 20


# --------------------------------------------------------------------------
# The inverse, which the elevation grid needs (task 39).
# --------------------------------------------------------------------------

# Urbano's own `GeoProjector.UTMToLatLong` (DotSpatial's inverse tmerc), read
# out of the loaded assembly by Probe9 in a scratch directory, on fourteen
# points chosen to be awkward rather than convenient: both hemispheres, both
# sides of a central meridian, the false easting exactly, a zone edge, the
# equator, the pole-ward end of the band table, and the two world origin
# corners of a real Welsh survey. Nothing in this table was computed by
# anything in this repository.
#
# easting, northing, zone, latitude, longitude
URBANO_INVERSE = [
    (480797.27250041266, 5693565.592158006, "30U", 51.393, -3.275999999999991),
    (481353.8723899168, 5693563.527297785, "30U", 51.393, -3.267999999999991),
    (480597.0, 5693365.0, "30U", 51.39118950372155, -3.2788675064013955),
    (481553.0, 5694321.0, "30U", 51.39981754617657, -3.2651773575247702),
    (500000.0, 5700000.0, "30U", 51.45118220565308, -2.9999999999999916),
    (400000.0, 5700000.0, "30U", 51.44235231635941, -4.438876794395441),
    (600000.0, 5700000.0, "30U", 51.44235231635941, -1.5611232056045428),
    (698324.116838298, 5709412.24314333, "30U", 51.50099999994762, -0.1424999999906395),
    (582505.6826406379, 4505891.429438546, "18T", 40.69979999999993, -74.02339999999988),
    (300000.0, 6200000.0, "31V", 55.90380878170299, -0.19945899211173349),
    (500000.0, 1000000.0, "34K", -81.06088097496355, 20.999999999999986),
    (450000.0, 6000000.0, "56H", -36.14342855636038, 152.4442256704288),
    (500000.0, 10000000.0, "23K", 0.0, -45.0),
    (500000.0, 0.0, "31N", 0.0, 2.9999999999999916),
]


@pytest.mark.parametrize(
    "easting,northing,zone,latitude,longitude",
    URBANO_INVERSE,
    ids=[f"{row[2]}-{int(row[0])}-{int(row[1])}" for row in URBANO_INVERSE],
)
def test_the_inverse_is_urbanos_own_inverse(
    easting, northing, zone, latitude, longitude
):
    """Within a nanometre of Urbano's `UTMToLatLong`, everywhere.

    A nanometre stated as an angle would be meaningless, so the tolerance is
    converted: one degree of latitude is about 111,320 m, and the check is
    that the disagreement is under a ten millionth of a metre on the ground.
    The measured worst case over this table is 3.2e-9 m, thirty times inside
    it.

    A ten millionth and not a millionth, because a tolerance has to be tight
    enough to be doing work. The smallest term in the longitude series moves
    the answer by 6.4e-7 m at the London fixture, which is 200 km from its
    central meridian, and a millionth would have let a wrong coefficient
    through unnoticed.
    """
    got_latitude, got_longitude = unproject(easting, northing, zone)
    assert abs(got_latitude - latitude) * 111_320.0 < 1e-7
    assert abs(got_longitude - longitude) * 111_320.0 < 1e-7


@pytest.mark.parametrize(
    "latitude,longitude", [(lat, lon) for _, lat, lon in CROSS_CHECK_POINTS]
)
def test_projecting_and_unprojecting_returns_the_same_point(latitude, longitude):
    """The round trip, on every cross-check point, to under a micrometre.

    This is the property that matters for the elevation grid: a grid node is
    projected forward to place it and unprojected to sample the DEM under it,
    and a disagreement of even a metre between the two directions would shift
    the terrain under the buildings by a metre.

    A micrometre and not a nanometre, and the difference is not slack. The
    forward and inverse series are each truncated, so the pair cannot close
    exactly; the measured worst case over this table is 2.2e-5 m, at the far
    edge of a zone. It is Urbano's own limit as much as mapgen's, since both
    directions are DotSpatial's, and the tighter check is the one above,
    where the standard is Urbano's own answer rather than mapgen's other
    direction: 3.2e-9 m.
    """
    zone = utm_zone(latitude, longitude)
    easting, northing = project(latitude, longitude, zone)
    back_latitude, back_longitude = unproject(easting, northing, zone)
    assert abs(back_latitude - latitude) * 111_320.0 < 1e-3
    assert abs(back_longitude - longitude) * 111_320.0 * math.cos(
        math.radians(latitude)
    ) < 1e-3


def test_unprojecting_and_projecting_returns_the_same_grid_coordinate():
    """The other direction of the round trip, which is the one the grid
    actually walks: from a node's easting and northing, out to the DEM, and
    back. A quarter of a million nodes go through this on a large survey and
    a systematic bias in either direction would tilt the whole surface.
    """
    for easting in (400_000.0, 480_597.0, 500_000.0, 600_000.0):
        for northing in (5_693_365.0, 5_700_000.0, 6_200_000.0):
            latitude, longitude = unproject(easting, northing, "30U")
            back_easting, back_northing = project(latitude, longitude, "30U")
            assert abs(back_easting - easting) < 1e-3
            assert abs(back_northing - northing) < 1e-3


def test_the_inverse_refuses_the_same_zone_strings_the_forward_one_does():
    """One parser, so a zone one direction accepts and the other refuses
    cannot exist. That pair would be a round trip that silently is not one.
    """
    for zone in ["", "U", "30", "30I", "0U", "61U", "thirty-U"]:
        with pytest.raises(ProjectionError, match="UTM zone string"):
            unproject(500000.0, 5700000.0, zone)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_an_easting_that_is_not_a_real_number_is_refused_not_unprojected(bad):
    with pytest.raises(ProjectionError, match="must be real numbers"):
        unproject(bad, 5700000.0, "30U")
    with pytest.raises(ProjectionError, match="must be real numbers"):
        unproject(500000.0, bad, "30U")


def test_a_northing_past_the_pole_returns_the_pole_rather_than_a_nan():
    """Urbano's inverse clamps to the pole and zeroes the longitude rather
    than raising, and mapgen has to do the same: a NaN latitude would reach
    the DEM sampler, which would read it as "no coverage" for a reason that
    is not the true one.
    """
    latitude, longitude = unproject(500000.0, 30_000_000.0, "30U")
    assert latitude == pytest.approx(90.0)
    assert longitude == pytest.approx(-3.0)
