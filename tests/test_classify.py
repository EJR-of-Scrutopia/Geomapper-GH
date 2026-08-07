from __future__ import annotations

import time

from mapgen.buildings import point_in_ring
from mapgen.classify import (
    GIANT_RING_CELLS,
    SAMPLE_CAP,
    OverlaySets,
    _build_overlay_index,
    _clip_ring_to_bbox,
    _flat_candidates,
    _OverlayIndex,
    classify_parcel,
    classify_parcels,
)

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


# --------------------------------------------------------------------------
# Robustness (task-2-review.md: one Important, one Minor, both
# input-hardening against a ring this classifier should never trust
# blindly, real Task 1 output or not).
# --------------------------------------------------------------------------


def test_pathological_huge_ring_classifies_in_bounded_time_with_capped_samples() -> None:
    """The review's own reproduction case: a 1deg x 1deg ring (the 20m
    spacing ceiling stops scaling with area past ~2.56 ha, so an
    unbounded, malformed, or oversized ring could otherwise drive sample
    count arbitrarily high; the review measured 19,119,210 samples in
    ~10s for this exact shape before the fix). Bounded time AND a sample
    count that never exceeds SAMPLE_CAP are both asserted: the spacing
    widening alone approximates the cap for a roughly square bbox, the
    hard stop in `_grid_samples` is what actually guarantees it."""
    huge = _rect(-1.0, 50.0, 0.0, 51.0)
    start = time.monotonic()
    category, sample_count = classify_parcel(huge, _empty_overlays())
    elapsed = time.monotonic() - start
    assert sample_count <= SAMPLE_CAP
    assert elapsed < 5.0
    assert category == "unclassified"


def test_giant_ring_bbox_skips_per_cell_bucketing_and_classifies_correctly() -> None:
    """The package review's own real-Cowbridge finding: a real Overture
    `water` feature (`class: "sea"`) is not clipped to the query bbox and
    arrives as its own real-world polygon, bbox 12.63 deg x 10.05 deg.
    Bucketed the ordinary way that enumerates 507,817,242 `CELL_SIZE_
    DEGREES` cells for that ONE ring (measured directly against the real
    package), multiple GB just for the list, and the step never
    completed (killed after ~6 minutes). A 10 deg x 10 deg synthetic ring
    here reproduces the same order of magnitude (about 400,000,000
    cells) with a plain 4-vertex rectangle, so this stays a fast unit
    test: `_OverlayIndex` must recognise it as GIANT and route it to the
    overflow list instead of ever building that cell list, which is what
    keeps `_by_cell` itself small regardless of how large a single
    overlay ring's bbox is, and a real classification against it must
    still complete, correctly, in bounded time.

    Round 2 (below) adds a clip that shrinks a real overlay ring BEFORE
    it ever reaches `_OverlayIndex`, so on the real classification path
    (`classify_parcels`) the overflow list this test exercises should
    now stay empty; this test builds `_OverlayIndex` directly, bypassing
    that clip on purpose, to keep proving `GIANT_RING_CELLS`'s own
    overflow path still protects a ring the clip never got the chance to
    shrink (a direct `_OverlayIndex` construction, or a future case the
    clip cannot reduce enough).
    """
    giant_water = _rect(-5.0, 45.0, 5.0, 55.0)
    overlays = OverlaySets(
        buildings=[], landuse=[], water=[giant_water], greenspace=[], woodland=[],
    )

    start = time.monotonic()
    index = _OverlayIndex(_flat_candidates(overlays))
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert len(index._by_cell) < GIANT_RING_CELLS, (
        "a giant ring's own bbox must never be bucketed cell by cell"
    )
    assert len(index._overflow) == 1

    # A small, ordinary parcel sitting inside the giant ring's own area
    # still classifies correctly: the overflow list is tested exactly
    # like the bucketed candidates, never skipped.
    results = classify_parcels([_PARCEL], overlays)
    assert results == [("water", results[0][1])]


# --------------------------------------------------------------------------
# Round 2 of the giant-ring fix (task-3-review.md): clipping every overlay
# candidate to the parcels' own padded extent before it ever reaches
# `_OverlayIndex`, so a real, un-clipped continent-scale ring shrinks to a
# small sliver rather than depending on the overflow list above at all.
# --------------------------------------------------------------------------

# A concave "comb" subject: two horizontal teeth (y in [-1, -0.2] and
# y in [0.2, 1]) extending from a base far to the west (x <= -500) out
# past x = 1000 to the east, with a real gap (y in [-0.2, 0.2]) between
# them. `_CLIP_WINDOW` below sits entirely within the teeth's own x-range
# but straddles the gap in y, so clipping this ring to that window keeps
# two separate visible pieces (one per tooth) joined only by material
# outside the window (the base, far to the west): exactly the shape that
# forces Sutherland-Hodgman's own degenerate seam along the window's own
# boundary between the two pieces.
_COMB_RING: list[tuple[float, float]] = [
    (-1000.0, -1.0),
    (1000.0, -1.0),
    (1000.0, -0.2),
    (-500.0, -0.2),
    (-500.0, 0.2),
    (1000.0, 0.2),
    (1000.0, 1.0),
    (-1000.0, 1.0),
]

_CLIP_WINDOW = (-1.0, -1.0, 1.0, 1.0)

# (ring, window, points to check): a handful of hand-built cases, the
# last one the concave comb above.
_CLIP_PRESERVES_CLASSIFICATION_CASES = [
    (
        # A huge rectangle fully containing the window: clips down to
        # exactly the window's own four corners.
        _rect(-1000.0, -1000.0, 1000.0, 1000.0),
        _CLIP_WINDOW,
        [(0.0, 0.0), (0.9, 0.9), (-0.9, -0.9), (0.5, -0.5)],
    ),
    (
        # A huge rectangle only partially overlapping the window (its
        # own right edge at x=0.5 cuts through it).
        _rect(-1000.0, -1000.0, 0.5, 1000.0),
        _CLIP_WINDOW,
        [(-0.9, 0.0), (0.0, 0.0), (0.9, 0.0), (0.5, 0.9)],
    ),
    (
        # The concave comb: points in tooth1, in tooth2, in the gap
        # between them, and near two corners.
        _COMB_RING,
        _CLIP_WINDOW,
        [(0.0, -0.6), (0.0, 0.6), (0.0, 0.0), (0.9, -0.9), (-0.9, 0.9)],
    ),
]


def test_clipping_an_overlay_ring_preserves_classification_for_points_inside_the_window() -> None:
    """The clip's own correctness argument (`_clip_overlay_candidates`'s
    own docstring), checked directly rather than assumed: for a handful
    of hand-built (ring, window, points) cases, including the concave
    comb above (which forces a Sutherland-Hodgman seam), every point
    inside the window classifies IDENTICALLY against the ORIGINAL ring
    and the CLIPPED one, and every vertex of the clipped ring lies
    within the window.
    """
    for ring, window, points in _CLIP_PRESERVES_CLASSIFICATION_CASES:
        clipped = _clip_ring_to_bbox(ring, window)
        assert clipped is not None
        min_x, min_y, max_x, max_y = window
        for x, y in clipped:
            assert min_x - 1e-9 <= x <= max_x + 1e-9
            assert min_y - 1e-9 <= y <= max_y + 1e-9
        for point in points:
            original_hit = point_in_ring(point[0], point[1], ring)
            clipped_hit = point_in_ring(point[0], point[1], clipped)
            assert original_hit == clipped_hit, (
                f"classification diverged at {point}: "
                f"original={original_hit}, clipped={clipped_hit}"
            )


def test_classify_parcels_clips_overlay_rings_so_the_overflow_list_stays_empty() -> None:
    """Round 2's own point: the SAME synthetic 10 deg x 10 deg ring that
    populates `_OverlayIndex._overflow` when built directly (the test
    above) clips down to a small sliver around the actual parcel once
    `_build_overlay_index` (what `classify_parcels` itself calls) runs
    the clip first, so the overflow list stays EMPTY: `GIANT_RING_CELLS`
    is a second line of defence a real overlay ring should never actually
    need to reach any more, checked here via the index's own `_overflow`
    length rather than assumed from the clip alone.
    """
    giant_water = _rect(-5.0, 45.0, 5.0, 55.0)
    overlays = OverlaySets(
        buildings=[], landuse=[], water=[giant_water], greenspace=[], woodland=[],
    )

    index = _build_overlay_index([_PARCEL], overlays)

    assert len(index._overflow) == 0, (
        "clipping should shrink the giant ring before it ever reaches "
        "the index, so the overflow path should not engage at all"
    )
    results = classify_parcels([_PARCEL], overlays)
    assert results == [("water", results[0][1])]


def test_empty_ring_returns_unclassified_with_zero_samples() -> None:
    """A genuinely empty ring (`[]`) used to crash `_bbox`'s own
    `min()`/`max()` on an empty sequence; every other degenerate ring
    (a single repeated point, collinear points, 2 vertices) already
    degrades gracefully to `("unclassified", 1)` via the representative-
    point fallback. Zero samples is the honest count for no geometry at
    all, and is only ever reachable this one way."""
    assert classify_parcel([], _empty_overlays()) == ("unclassified", 0)


def test_classify_parcels_tolerates_an_empty_ring_alongside_real_ones() -> None:
    overlays = OverlaySets(
        buildings=[],
        landuse=[("grass", _PARCEL)],
        water=[],
        greenspace=[],
        woodland=[],
    )
    results = classify_parcels([_PARCEL, []], overlays)
    assert results == [("greenspace", results[0][1]), ("unclassified", 0)]
