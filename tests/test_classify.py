from __future__ import annotations

from mapgen.classify import OverlaySets, classify_parcel, classify_parcels

# --------------------------------------------------------------------------
# Shared synthetic geometry. All coordinates WGS84 [lon, lat]; degrees at
# this latitude (~51.4N) are big enough (~111km/deg lat, ~69km/deg lon) that
# 0.001 degrees is roughly 70-110 metres, which is what these fixtures use
# to land parcels comfortably inside the sampled-majority grid path (the
# rules' own clamp(sqrt(area_m2)/8, 2.0, 20.0) spacing) unless a test is
# deliberately building the "too small for the grid" sliver case.
# --------------------------------------------------------------------------

BASE_LON = -3.280
BASE_LAT = 51.400


def _rect(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """An axis-aligned rectangle ring, exterior-only, not explicitly closed
    (matching buildings.py's own candidate-ring convention: `point_in_ring`
    and `representative_point` both accept an open ring)."""
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def _empty_overlays() -> OverlaySets:
    return OverlaySets(buildings=[], landuse=[], water=[], greenspace=[], woodland=[])


# A 0.001 x 0.001 degree parcel (roughly 70m x 111m at this latitude),
# comfortably large enough for the grid-sampling path (not the sliver
# fallback) in every test below that is not itself testing the fallback.
_PARCEL = _rect(BASE_LON, BASE_LAT, BASE_LON + 0.001, BASE_LAT + 0.001)


def test_residential_landuse_with_a_building_hit_is_housing() -> None:
    overlays = OverlaySets(
        buildings=[_rect(BASE_LON + 0.0003, BASE_LAT + 0.0003, BASE_LON + 0.0007, BASE_LAT + 0.0007)],
        landuse=[("residential", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "housing"
    assert sample_count >= 4


def test_residential_landuse_without_a_building_is_garden() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[("residential", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "garden"
    assert sample_count >= 4


def test_farmland_majority_with_a_barn_footprint_is_field_not_housing() -> None:
    """The building rule needs a RESIDENTIAL land-use majority, not merely
    a building hit: a barn sitting in farmland must not become housing.
    Pinned per the plan header's own reasoning for this exact case."""
    overlays = OverlaySets(
        buildings=[_rect(BASE_LON + 0.0003, BASE_LAT + 0.0003, BASE_LON + 0.0007, BASE_LAT + 0.0007)],
        landuse=[("farmland", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "field"


def test_parcel_straddling_farmland_and_residential_is_field() -> None:
    """60% farmland / 40% residential by area: farmland has the plurality
    of samples, so it wins outright even though residential (garden) sits
    earlier in the priority order (garden only wins actual TIES)."""
    x0, y0, x1, y1 = BASE_LON, BASE_LAT, BASE_LON + 0.001, BASE_LAT + 0.001
    split = x0 + 0.4 * (x1 - x0)
    overlays = OverlaySets(
        buildings=[],
        landuse=[
            ("residential", _rect(x0, y0, split, y1)),
            ("farmland", _rect(split, y0, x1, y1)),
        ],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "field"


def test_water_majority_is_water() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[],
        water=[_PARCEL],
        greenspace=[],
        woodland=[],
    )
    category, sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "water"
    assert sample_count >= 4


def test_no_overlay_evidence_is_unclassified() -> None:
    category, sample_count = classify_parcel(_PARCEL, _empty_overlays())
    assert category == "unclassified"
    assert sample_count >= 4


def test_sliver_parcel_too_small_for_the_grid_uses_one_sample() -> None:
    """A needle-thin sliver: the square grid at the clamped minimum
    spacing (2.0m) cannot catch 4 interior points, so the fallback (the
    representative point, guaranteed interior, plus whichever of the 4
    clipped bbox-edge midpoints also land inside) is used instead. This
    sliver is thin enough that none of the 4 midpoints survive the
    ray-cast, leaving exactly the representative point: 1 sample."""
    sliver = [
        (BASE_LON, BASE_LAT),
        (BASE_LON + 0.00004, BASE_LAT + 0.0000006),
        (BASE_LON + 0.00004, BASE_LAT - 0.0000002),
    ]
    category, sample_count = classify_parcel(sliver, _empty_overlays())
    assert sample_count == 1
    assert category == "unclassified"


def test_sliver_parcel_samples_from_its_representative_point() -> None:
    """Same sliver, but entirely inside a water ring: proves the single
    fallback sample is actually used for classification (not merely
    counted), since there is no other way this sliver could come back
    water without its one sample being tested against the overlay."""
    sliver = [
        (BASE_LON, BASE_LAT),
        (BASE_LON + 0.00004, BASE_LAT + 0.0000006),
        (BASE_LON + 0.00004, BASE_LAT - 0.0000002),
    ]
    enclosing_water = _rect(BASE_LON - 0.001, BASE_LAT - 0.001, BASE_LON + 0.001, BASE_LAT + 0.001)
    overlays = OverlaySets(buildings=[], landuse=[], water=[enclosing_water], greenspace=[], woodland=[])
    category, sample_count = classify_parcel(sliver, overlays)
    assert sample_count == 1
    assert category == "water"


def test_tie_between_two_landuse_classes_is_broken_by_priority_order() -> None:
    """Two land-use rings split the parcel exactly in half: retail and
    industrial tie on sample count. retail sits earlier in the mapping
    table's own listed order (the pinned priority order), so it wins."""
    x0, y0, x1, y1 = BASE_LON, BASE_LAT, BASE_LON + 0.001, BASE_LAT + 0.001
    mid = x0 + 0.5 * (x1 - x0)
    overlays = OverlaySets(
        buildings=[],
        landuse=[
            ("retail", _rect(x0, y0, mid, y1)),
            ("industrial", _rect(mid, y0, x1, y1)),
        ],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "retail"


def test_grass_landuse_is_greenspace() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[("grass", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "greenspace"


def test_cemetery_landuse_is_religious() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[("cemetery", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "religious"


def test_unmapped_landuse_class_contributes_no_evidence() -> None:
    """A land-use class absent from the pinned mapping table (the rules
    file's "any other land_use class -> treated as no land-use evidence")
    must not win a majority merely by being the only ring present."""
    overlays = OverlaySets(
        buildings=[],
        landuse=[("military", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "unclassified"


def test_os_greenspace_polygon_majority_is_greenspace() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[],
        water=[],
        greenspace=[_PARCEL],
        woodland=[],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "greenspace"


def test_woodland_polygon_majority_is_woodland() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[],
        water=[],
        greenspace=[],
        woodland=[_PARCEL],
    )
    category, _sample_count = classify_parcel(_PARCEL, overlays)
    assert category == "woodland"


# --------------------------------------------------------------------------
# classify_parcels (plural): one shared spatial hash over the overlay
# rings, must agree with classify_parcel (singular) exactly.
# --------------------------------------------------------------------------


def test_classify_parcels_agrees_with_classify_parcel_over_a_mixed_fixture() -> None:
    """The plan's own pinned invariant: `classify_parcels` builds ONE
    shared spatial hash over all overlay rings and classifies each parcel
    against it; `classify_parcel` is the readable, unhashed reference. A
    mixed fixture (housing, garden, field, water, greenspace, woodland,
    unclassified and a sliver all in one run) proves the hashed batch
    path never disagrees with the per-parcel reference."""
    housing_parcel = _rect(BASE_LON, BASE_LAT, BASE_LON + 0.001, BASE_LAT + 0.001)
    garden_parcel = _rect(BASE_LON + 0.01, BASE_LAT, BASE_LON + 0.011, BASE_LAT + 0.001)
    field_parcel = _rect(BASE_LON + 0.02, BASE_LAT, BASE_LON + 0.021, BASE_LAT + 0.001)
    water_parcel = _rect(BASE_LON + 0.03, BASE_LAT, BASE_LON + 0.031, BASE_LAT + 0.001)
    greenspace_parcel = _rect(BASE_LON + 0.04, BASE_LAT, BASE_LON + 0.041, BASE_LAT + 0.001)
    woodland_parcel = _rect(BASE_LON + 0.05, BASE_LAT, BASE_LON + 0.051, BASE_LAT + 0.001)
    unclassified_parcel = _rect(BASE_LON + 0.06, BASE_LAT, BASE_LON + 0.061, BASE_LAT + 0.001)
    sliver_parcel = [
        (BASE_LON + 0.07, BASE_LAT),
        (BASE_LON + 0.07004, BASE_LAT + 0.0000006),
        (BASE_LON + 0.07004, BASE_LAT - 0.0000002),
    ]

    overlays = OverlaySets(
        buildings=[
            _rect(
                BASE_LON + 0.0003,
                BASE_LAT + 0.0003,
                BASE_LON + 0.0007,
                BASE_LAT + 0.0007,
            )
        ],
        landuse=[
            ("residential", housing_parcel),
            ("residential", garden_parcel),
            ("farmland", field_parcel),
        ],
        water=[water_parcel],
        greenspace=[greenspace_parcel],
        woodland=[woodland_parcel],
    )

    rings = [
        housing_parcel,
        garden_parcel,
        field_parcel,
        water_parcel,
        greenspace_parcel,
        woodland_parcel,
        unclassified_parcel,
        sliver_parcel,
    ]

    singular_results = [classify_parcel(ring, overlays) for ring in rings]
    plural_results = classify_parcels(rings, overlays)

    assert plural_results == singular_results
    assert [category for category, _count in singular_results] == [
        "housing",
        "garden",
        "field",
        "water",
        "greenspace",
        "woodland",
        "unclassified",
        "unclassified",
    ]


def test_classify_parcels_returns_one_result_per_ring_in_order() -> None:
    rings = [_PARCEL, _rect(BASE_LON + 0.01, BASE_LAT, BASE_LON + 0.011, BASE_LAT + 0.001)]
    overlays = OverlaySets(
        buildings=[],
        landuse=[("grass", rings[0])],
        water=[],
        greenspace=[],
        woodland=[],
    )
    results = classify_parcels(rings, overlays)
    assert len(results) == 2
    assert results[0][0] == "greenspace"
    assert results[1][0] == "unclassified"
