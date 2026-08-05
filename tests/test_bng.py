import math

import pytest

from mapgen.bng import BngError, tm_forward, tm_inverse


def test_tm_forward_true_origin_lands_on_false_origin_scaled():
    # The true origin projects to the false origin exactly, by construction.
    easting, northing = tm_forward(49.0, -2.0)
    assert easting == pytest.approx(400_000.0, abs=1e-6)
    assert northing == pytest.approx(-100_000.0, abs=1e-6)


def test_tm_roundtrip_over_wales():
    # Forward then inverse must return to the input to well under a
    # millimetre in degrees (1e-9 deg is about 0.1 mm on the ground).
    for lat, lon in [(51.40, -3.27), (51.48, -3.18), (53.32, -4.63), (52.42, -4.08)]:
        easting, northing = tm_forward(lat, lon)
        back_lat, back_lon = tm_inverse(easting, northing)
        assert back_lat == pytest.approx(lat, abs=1e-9)
        assert back_lon == pytest.approx(lon, abs=1e-9)


def test_tm_forward_monotonic_in_the_right_directions():
    # North increases northing, east increases easting: the cheapest way to
    # catch a swapped sign or a swapped argument order.
    e1, n1 = tm_forward(51.40, -3.27)
    e2, n2 = tm_forward(51.41, -3.27)
    e3, n3 = tm_forward(51.40, -3.26)
    assert n2 > n1 and abs(e2 - e1) < 200.0
    assert e3 > e1 and abs(n3 - n1) < 200.0


# --------------------------------------------------------------------------
# Refusals. Never a NaN, never an infinity, never a silent wrong answer.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_tm_forward_refuses_a_non_finite_input(bad):
    with pytest.raises(BngError, match="real numbers"):
        tm_forward(bad, -3.0)
    with pytest.raises(BngError, match="real numbers"):
        tm_forward(51.5, bad)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_tm_inverse_refuses_a_non_finite_input(bad):
    with pytest.raises(BngError, match="real numbers"):
        tm_inverse(bad, 100_000.0)
    with pytest.raises(BngError, match="real numbers"):
        tm_inverse(400_000.0, bad)
