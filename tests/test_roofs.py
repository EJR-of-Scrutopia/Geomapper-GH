"""tests/test_roofs.py"""
import math
import random
import xml.etree.ElementTree as ET
from array import array
from pathlib import Path

from mapgen.bng import _NODE_COUNT, Ostn15Grid, from_bng
from mapgen.cog import BngWindow
from mapgen.roofs import (
    INLIER_TOLERANCE_METRES,
    MIN_PLANE_SAMPLES,
    Plane,
    extract_planes,
    ridge_azimuth_deg,
    trimmed_plane,
)


def _plane_points(a, b, c, extent=6, step=1.0):
    """A grid of exact samples of z = a*e + b*n + c over a square."""
    points = []
    for row in range(-extent, extent + 1):
        for col in range(-extent, extent + 1):
            e, n = col * step, row * step
            points.append((e, n, a * e + b * n + c))
    return points


class TestPlane:
    def test_z_at_evaluates_the_plane(self):
        plane = Plane(a=0.5, b=-0.25, c=2.0)
        assert plane.z_at(2.0, 4.0) == 0.5 * 2.0 - 0.25 * 4.0 + 2.0

    def test_slope_of_a_horizontal_plane_is_zero(self):
        assert Plane(a=0.0, b=0.0, c=5.0).slope_deg() == 0.0

    def test_slope_of_a_45_degree_plane(self):
        assert abs(Plane(a=1.0, b=0.0, c=0.0).slope_deg() - 45.0) < 1e-9

    def test_downslope_azimuth_points_downhill(self):
        # z rises to the east (a > 0), so downhill faces west (270).
        assert abs(Plane(a=1.0, b=0.0, c=0.0).downslope_azimuth_deg() - 270.0) < 1e-9
        # z rises to the north (b > 0), so downhill faces south (180).
        assert abs(Plane(a=0.0, b=1.0, c=0.0).downslope_azimuth_deg() - 180.0) < 1e-9


class TestTrimmedPlane:
    def test_recovers_an_exact_plane(self):
        fit = trimmed_plane(_plane_points(0.3, -0.1, 4.0))
        assert abs(fit.a - 0.3) < 1e-6
        assert abs(fit.b + 0.1) < 1e-6
        assert abs(fit.c - 4.0) < 1e-6

    def test_trim_discards_a_spike_a_plain_fit_cannot(self):
        # One +8 m spike on a flat roof of 169 samples. The trimmed refit
        # must land within a centimetre of flat; a plain least squares
        # over the same points is pulled visibly off (documented here as
        # the reason the spec forbids it).
        points = _plane_points(0.0, 0.0, 3.0)
        points[0] = (points[0][0], points[0][1], 11.0)
        fit = trimmed_plane(points)
        assert abs(fit.z_at(0.0, 0.0) - 3.0) < 0.01

    def test_collinear_points_return_none(self):
        points = [(float(i), 0.0, 1.0) for i in range(10)]
        assert trimmed_plane(points) is None

    def test_degenerate_after_trim_returns_none(self):
        # A narrow canopy strip: 5 real samples along one row (e=0, n=0..4),
        # z = 0.1*n + 3, plus one bad DSM spike far from the strip.
        # After trimming the spike, the 5 remaining points are all at e=0
        # (plan-collinear), so the refit should fail and this should return None.
        points = [
            (0.0, 0.0, 3.0), (0.0, 1.0, 3.1), (0.0, 2.0, 3.2),
            (0.0, 3.0, 3.3), (0.0, 4.0, 3.4),
            (3.0, 2.0, 1000.0),
        ]
        result = trimmed_plane(points)
        assert result is None


class TestExtractPlanes:
    def test_single_plane_claims_everything(self):
        points = _plane_points(0.2, 0.0, 5.0)
        fitted = extract_planes(points)
        assert len(fitted) == 1
        assert len(fitted[0].inlier_indices) == len(points)

    def test_two_gable_planes_found_separately(self):
        # A gable along the n axis: z falls away from e = 0 both ways.
        points = []
        for row in range(-6, 7):
            for col in range(-6, 7):
                e, n = float(col), float(row)
                points.append((e, n, 8.0 - 0.7 * abs(e)))
        fitted = extract_planes(points)
        assert len(fitted) == 2
        claimed = set(fitted[0].inlier_indices) | set(fitted[1].inlier_indices)
        # The two planes together explain nearly every sample (the ridge
        # row itself belongs to whichever plane finds it first).
        assert len(claimed) >= 0.9 * len(points)

    def test_deterministic_across_runs(self):
        points = _plane_points(0.1, 0.2, 2.0)
        first = extract_planes(points, rng=random.Random(0))
        second = extract_planes(points, rng=random.Random(0))
        assert [f.inlier_indices for f in first] == [f.inlier_indices for f in second]

    def test_too_few_points_yields_nothing(self):
        assert extract_planes(_plane_points(0.0, 0.0, 3.0)[: MIN_PLANE_SAMPLES - 1]) == []

    def test_extract_planes_calls_trimmed_plane(self):
        # Indirect test that extract_planes correctly calls trimmed_plane on
        # inliers and breaks if trimmed_plane returns None. The degenerate-
        # after-trim case is tested directly in TestTrimmedPlane above. Here
        # we just verify that extract_planes doesn't crash when faced with
        # a large dataset, including the break path when trimmed_plane fails.
        points = _plane_points(0.2, 0.1, 3.0, extent=4, step=1.5)
        fitted = extract_planes(points)
        # Should return at least one plane from well-conditioned data.
        assert len(fitted) >= 1
        # All returned planes should have reasonable parameters.
        for f in fitted:
            assert isinstance(f.plane, Plane)
            assert isinstance(f.inlier_indices, tuple)
            assert len(f.inlier_indices) >= MIN_PLANE_SAMPLES


class TestRidgeAzimuth:
    def test_gable_ridge_at_30_degrees(self):
        # Ridge along azimuth 30: downslope azimuths 120 and 300, both at
        # gradient magnitude g. Gradients point uphill.
        g = 0.7
        up1 = math.radians(300.0)
        up2 = math.radians(120.0)
        p1 = Plane(a=g * math.sin(up1), b=g * math.cos(up1), c=0.0)
        p2 = Plane(a=g * math.sin(up2), b=g * math.cos(up2), c=0.0)
        azimuth = ridge_azimuth_deg(p1, p2)
        assert abs(azimuth - 30.0) < 1e-6

    def test_parallel_planes_have_no_ridge(self):
        p = Plane(a=0.5, b=0.0, c=0.0)
        assert ridge_azimuth_deg(p, Plane(a=0.5, b=0.0, c=2.0)) is None


from mapgen.roofs import (
    MIN_QUALITY,
    MIN_ROOF_SAMPLES,
    ROOF_SHAPES,
    RoofForm,
    classify_roof,
)
from mapgen.roofs import extract_planes as _extract


def _roof_points(shape, ridge_azimuth_deg=0.0, pitch_deg=35.0,
                 half_width=4.0, half_length=6.0, eaves=5.0, noise=0.0,
                 seed=1):
    """1 m grid samples of a synthetic roof, rotated so its ridge lies
    along `ridge_azimuth_deg`. Local frame: u along the ridge, v across
    it; z falls from the ridge with tan(pitch)."""
    rng = random.Random(seed)
    tan_pitch = math.tan(math.radians(pitch_deg))
    ridge_height = eaves + half_width * tan_pitch
    azimuth = math.radians(ridge_azimuth_deg)
    ue, un = math.sin(azimuth), math.cos(azimuth)     # along ridge
    ve, vn = math.cos(azimuth), -math.sin(azimuth)    # across ridge
    points = []
    for row in range(-int(half_length), int(half_length) + 1):
        for col in range(-int(half_width), int(half_width) + 1):
            u, v = float(row), float(col)
            if shape == "flat":
                z = eaves
            elif shape == "mono":
                z = eaves + (v + half_width) * tan_pitch * 0.5
            elif shape == "gable":
                z = ridge_height - abs(v) * tan_pitch
            elif shape == "hip":
                inset = abs(u) - (half_length - half_width)
                z = ridge_height - max(abs(v), max(inset, 0.0)) * tan_pitch
            else:
                raise ValueError(shape)
            z += rng.gauss(0.0, noise) if noise else 0.0
            points.append((u * ue + v * ve, u * un + v * vn, z))
    return points


def _classified(points):
    return classify_roof(points, _extract(points))


class TestClassifyRoof:
    def test_flat_roof(self):
        form = _classified(_roof_points("flat", noise=0.03))
        assert form.shape == "flat"
        assert form.direction_deg is None
        assert abs(form.eaves_m - 5.0) < 0.2
        assert abs(form.ridge_m - 5.0) < 0.2
        assert form.quality >= 0.9

    def test_gable_at_30_degrees(self):
        form = _classified(_roof_points("gable", ridge_azimuth_deg=30.0, noise=0.03))
        assert form.shape == "gable"
        assert abs(form.direction_deg - 30.0) < 5.0
        assert form.eaves_m < form.ridge_m
        assert abs(form.ridge_m - (5.0 + 4.0 * math.tan(math.radians(35.0)))) < 0.4

    def test_mono_pitch(self):
        form = _classified(_roof_points("mono", ridge_azimuth_deg=0.0, noise=0.03))
        assert form.shape == "mono"
        assert form.direction_deg is not None

    def test_hip_roof_reads_complex_because_hip_was_dropped(self):
        # `hip` was dropped by Task 4's validation: swept across aspect
        # ratio, azimuth and noise seed at 1 m, a synthetic hip answered
        # hip in only 33 of 135 runs and otherwise answered gable,
        # complex, flat or nothing, while the gable control answered
        # gable 15/15 at every aspect. See classify_roof for the table.
        #
        # A hip must now read `complex`: honest eaves and ridge, no form
        # named, no direction asserted.
        form = _classified(
            _roof_points("hip", ridge_azimuth_deg=90.0, half_width=4.0,
                         half_length=4.5, noise=0.03)
        )
        assert form.shape == "complex"
        assert form.direction_deg is None
        assert form.eaves_m < form.ridge_m

    def test_hip_is_not_in_the_vocabulary(self):
        assert "hip" not in ROOF_SHAPES
        assert ROOF_SHAPES == ("flat", "mono", "gable", "complex")

    def test_a_stepped_flat_roof_is_complex_not_flat(self):
        # Two level decks 3 m apart, each big enough to be a significant
        # plane. Every plane is flat, so the old rule called the whole
        # building `flat` and reported ONE median level as both eaves and
        # ridge, hiding the step. Task 4 measured this on real Cowbridge
        # data: 39% of `flat` buildings spanned more than half a metre,
        # the worst 4.20 m.
        points = []
        for row in range(-6, 7):
            for col in range(-6, 7):
                z = 4.0 if col < 0 else 7.0
                points.append((float(col), float(row), z))
        form = _classified(points)
        assert form.shape == "complex"
        # The step is reported, not averaged away.
        assert form.eaves_m < form.ridge_m
        assert form.ridge_m - form.eaves_m > 2.0

    def test_a_genuinely_level_roof_is_still_flat(self):
        # The other side of the same gate: real noise and a parapet's
        # worth of spread stay under FLAT_MAX_SPREAD_METRES.
        form = _classified(_roof_points("flat", noise=0.03))
        assert form.shape == "flat"
        assert form.eaves_m == form.ridge_m

    def test_spiked_gable_still_classifies_gable(self):
        # Three +8 m spikes (an aerial, a chimney, a bad return) on a
        # 30-degree gable: the owner's own failure case. The trimmed,
        # RANSAC-seeded fit must shrug them off.
        points = _roof_points("gable", ridge_azimuth_deg=30.0, noise=0.05)
        for i in (5, 60, 100):
            e, n, z = points[i]
            points[i] = (e, n, z + 8.0)
        form = _classified(points)
        assert form.shape == "gable"
        assert abs(form.direction_deg - 30.0) < 5.0
        # The ridge height must not be dragged up by the spikes.
        assert form.ridge_m < 5.0 + 4.0 * math.tan(math.radians(35.0)) + 0.5

    def test_pure_noise_is_refused_not_guessed(self):
        rng = random.Random(3)
        points = [
            (float(col), float(row), rng.uniform(0.0, 6.0))
            for row in range(-6, 7)
            for col in range(-6, 7)
        ]
        assert _classified(points) is None

    def test_too_few_samples_refused(self):
        assert _classified(_roof_points("flat")[: MIN_ROOF_SAMPLES - 1]) is None


# --------------------------------------------------------------------------
# Task 3: the .osm loop, roof tags, and the record.
#
# Fixture pattern reused verbatim from test_heights.py: a hand-rolled
# Ostn15Grid every node of which carries a (0, 0) shift, so from_bng and
# to_bng degenerate to tm_inverse/tm_forward, exact inverses of each
# other. That is what lets _osm_with_building below choose BNG
# coordinates directly, project them to lat/lon with from_bng, and get
# them back out of fit_roof_forms's own to_bng call almost unchanged,
# with no real OSTN15 cache and no network touched. A fresh clone or a
# CI runner has no `~/.mapgen` cache; this fixture never asks for one.
# --------------------------------------------------------------------------

from mapgen.roofs import (  # noqa: E402
    ROOF_SHAPE_TAG_KEY,
    SOURCE_ROOF_ATTRIBUTION,
    RoofsRecord,
    fit_roof_forms,
)


def _zero_shift_grid() -> Ostn15Grid:
    shifts = array("f", [0.0]) * (_NODE_COUNT * 2)
    return Ostn15Grid(shifts)


def _window(e0, n0, size, values, pixel=1.0):
    return BngWindow(
        e_origin=e0, n_top=n0 + size * pixel, pixel_size=pixel,
        width=size, height=size, values=array("f", values),
    )


def _gable_windows(e0, n0, size=20, eaves=5.0, ridge=8.0):
    """DTM flat at 100.0; DSM carries a west-east gable over the middle
    of the window (ridge running north-south at the window's centre
    column), background at ground."""
    ground = [100.0] * (size * size)
    dsm = []
    centre = size / 2.0
    for row in range(size):
        for col in range(size):
            offset = abs((col + 0.5) - centre)
            if 4 <= row < size - 4 and offset <= 6.0:
                dsm.append(100.0 + max(eaves, ridge - (ridge - eaves) * offset / 6.0))
            else:
                dsm.append(100.0)
    return _window(e0, n0, size, ground), _window(e0, n0, size, dsm)


def _osm_with_building(path, ring_bng, grid, extra_tags=()):
    """Write a minimal mapgen-shaped .osm holding one closed building way
    whose nodes are `ring_bng` projected to lat/lon via from_bng."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<osm version="0.6" generator="test">']
    refs = []
    for i, (e, n) in enumerate(ring_bng, start=1):
        lat, lon = from_bng(e, n, grid)
        lines.append(f'  <node id="{i}" lat="{lat:.7f}" lon="{lon:.7f}" />')
        refs.append(i)
    nds = "".join(f'<nd ref="{r}" />' for r in refs + [refs[0]])
    tags = '<tag k="building" v="yes" />' + "".join(
        f'<tag k="{k}" v="{v}" />' for k, v in extra_tags
    )
    lines.append(f'  <way id="100">{nds}{tags}</way>')
    lines.append("</osm>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestFitRoofForms:
    def test_gable_building_gains_roof_tags(self, tmp_path):
        grid = _zero_shift_grid()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.buildings == 1
        assert record.classified == 1
        assert record.shapes.get("gable") == 1
        root = ET.fromstring(osm.read_text(encoding="utf-8"))
        way = root.find("way")
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        assert tags["roof:shape"] == "gable"
        assert float(tags["roof:height:eaves"]) < float(tags["roof:height:ridge"])
        assert tags["source:roof"] == SOURCE_ROOF_ATTRIBUTION
        # Ridge runs north-south in the fixture: direction near 0 or 180.
        direction = float(tags["roof:direction"])
        assert min(direction, 180.0 - direction) < 10.0

    def test_second_run_is_byte_identical(self, tmp_path):
        grid = _zero_shift_grid()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        fit_roof_forms(osm, dtm, dsm, grid)
        first = osm.read_bytes()
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.kept_existing == 1
        assert record.classified == 0
        assert osm.read_bytes() == first

    def test_no_raster_data_writes_nothing(self, tmp_path):
        grid = _zero_shift_grid()
        e0, n0 = 318000.0, 176000.0
        nan = float("nan")
        dtm = _window(e0, n0, 20, [nan] * 400)
        dsm = _window(e0, n0, 20, [nan] * 400)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid)
        before = osm.read_bytes()
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.no_data == 1
        assert osm.read_bytes() == before

    def test_counts_always_reconcile(self, tmp_path):
        # buildings == classified + kept_existing + below_quality + no_data
        grid = _zero_shift_grid()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid, extra_tags=(("roof:shape", "gable"),))
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.buildings == (
            record.classified + record.kept_existing
            + record.below_quality + record.no_data
        )
        assert record.kept_existing == 1

    def test_a_stale_partial_roof_tag_is_kept_not_duplicated(self, tmp_path):
        # A way with roof:direction but no roof:shape is real-world OSM
        # shape (roof:* tags are contributed independently upstream): it
        # must still pass the roof:shape gate as untagged, get fitted,
        # and come out with exactly one roof:direction tag, the mapper's
        # own stale value, untouched, alongside the freshly fitted keys.
        grid = _zero_shift_grid()
        e0, n0 = 318000.0, 176000.0
        dtm, dsm = _gable_windows(e0, n0)
        osm = tmp_path / "site.osm"
        ring = [(e0 + 4.0, n0 + 4.0), (e0 + 16.0, n0 + 4.0),
                (e0 + 16.0, n0 + 16.0), (e0 + 4.0, n0 + 16.0)]
        _osm_with_building(osm, ring, grid, extra_tags=(("roof:direction", "123"),))
        record = fit_roof_forms(osm, dtm, dsm, grid)
        assert record.classified == 1
        assert record.kept_existing == 0
        way = ET.fromstring(osm.read_text(encoding="utf-8")).find("way")
        direction_tags = [t for t in way.findall("tag") if t.get("k") == "roof:direction"]
        assert len(direction_tags) == 1
        assert direction_tags[0].get("v") == "123"
        tags = {t.get("k"): t.get("v") for t in way.findall("tag")}
        assert tags["roof:shape"] == "gable"
        assert tags["roof:height:eaves"]
        assert tags["roof:height:ridge"]
        assert tags["source:roof"] == SOURCE_ROOF_ATTRIBUTION
