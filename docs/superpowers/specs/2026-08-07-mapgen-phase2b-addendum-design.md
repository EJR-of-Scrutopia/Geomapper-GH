# mapgen phase 2b: the owner's addendum

Design spec, distilled from the working sessions of 2026-08-06 and
2026-08-07, after phase 2 items 1 to 3 shipped (Wales LiDAR, INSPIRE
curves, OS Open pack + tier resolver). Every item below was approved by
the owner in conversation; this document is where those approvals become
reviewable design. The phase 2 spec's global constraints carry over
unchanged (no new third-party dependencies, no build step, token-gated
routes, no URL in errors, licences and attribution recorded in
survey.json, live tests behind the `live` marker, nothing fabricated
from nodata, no em dashes, the owner owns the work).

Already delivered ahead of this spec, outside the pipeline:
`docs/grasshopper/lidar_to_mesh.py` (the GH LiDAR-to-mesh script,
urbano-aligned, numpy-accelerated, crop/offset inputs). It stays a
documentation artifact, not pipeline code; anything it taught us is
folded into the items below.

## Item A: categorised boundaries

**Problem.** INSPIRE parcels carry no land-use class at all (properties
today: source, HMLR caveat, year). The owner filters curves in Urbano by
key/value and wants "gardens", "fields", "housing", "water" as separate
filterable sets.

**Design.** Classify each parcel POLYGON (before shared-edge dedup) by
overlay against data the package already holds, in priority order:

1. Building presence (fused .osm footprints incl. Overture/OS): a parcel
   whose interior point set intersects >= 1 dwelling footprint and whose
   dominant land use is residential classifies `housing`.
2. Overture land_use majority overlap (residential -> `garden` when no
   building, `field` for farmland/meadow/farmyard, `recreation`,
   `retail`, `industrial`, `education`, `religious`, `allotments`).
3. Overture water + OSM water tags -> `water`.
4. OS Open Greenspace function (public park etc.) -> `greenspace`.
5. OS OpenMapLocal Woodland -> `woodland`.
6. Nothing matched -> `unclassified` (honest, never guessed).

A parcel spanning classes takes the class of its largest-area overlap;
`mixed` is deliberately NOT a class (the majority is more useful to the
owner than a bucket nobody filters for).

**Output.** `<stem>_boundaries_categorised.geojson`: boundary CURVES
(the existing pinch-point-safe dedup machinery from item 2's
boundary_curves.py) deduplicated WITHIN each category, every feature
carrying `{"category": <one of the classes above>, "source": ...,
"note": <HMLR caveat>, "year": ...}`. The existing fully-deduped
`_boundaries.geojson` and the .osm fusion stay exactly as they are; a
shared edge between a garden and a field appears once per category in
the new file, which is what makes single-category filtering complete.
survey.json records per-category parcel counts.

**Not promised.** Classification is derived from overlay, not from any
register; the UI copy and survey.json say `derived from map overlay,
indicative` wherever categories appear.

## Item B: roof forms and canopy from the LiDAR

**Roofs.** For each building footprint in the fused .osm (OSM, Overture
and OS footprints alike), sample the DSM inside the footprint, subtract
the DTM median (the existing heights.py convention), and fit planes to
the roof surface: RANSAC-style seeded plane fits, pure stdlib + the
numpy-free arithmetic this project already writes. Classify per
footprint: `flat`, `mono`, `gable`, `hip`, `complex` (the honest bucket
when fits disagree), with ridge direction (degrees from north) and eave
and ridge heights in metres. Written as tags on the building way
(`roof:shape`, `roof:direction`, `roof:height:eaves`,
`roof:height:ridge`, `source:roof=...lidar...`) plus an optional
`<stem>_roof_massing.geojson` with one simple prism-with-roof polygon
set per classified building for direct GH import. Buildings whose
sample count or fit quality is below threshold get NO roof tags and are
counted in the record: absence over fabrication. The 1 m raster bounds
what is honest here: a fitted plane is sharper than the pixels it came
from, but a conservatory will not be resolved, and the spec says so.

**Canopy.** DSM minus DTM OUTSIDE all footprints, thresholded (>= 3 m),
clustered by connected components: each cluster above a minimum area
emits a tree/canopy point (position, canopy height, crown radius from
the cluster area) into `<stem>_canopy.geojson`. This is the owner's
vegetation ask served from data already in every Welsh package. Same
honesty rule: pylons, cranes and other tall non-vegetation will appear;
the copy says `vegetation and other above-ground features` rather than
pretending species knowledge.

**Ordering.** Both run after buildings fusion and heights fusion,
package-time, Wales-covered extents only (they consume the lidar
rasters; the resolver already knows coverage).

**Resolution gate and the spike problem (owner requirement,
2026-08-07).** Roof fitting runs ONLY when the package's LiDAR raster
is at the 1 m level (level 0): fitting planes to a 2 m overview is
fabrication and the step must skip with a clear record and UI-visible
reason, pointing at the detail preview's own guidance about extent
size. And because real DSMs carry spikes and outliers (the owner has
seen both in LiDAR meshes), the plan MUST include a validation task
before any roof tag ships: outlier-robust fitting (trimmed residuals,
never a plain least squares), a per-building fit-quality score
recorded in the output, and a live check over buildings the owner can
verify against reality, with the failure mode being NO tags rather
than bad tags. If validation shows the 1 m DSM cannot support a roof
class reliably, that class is dropped from the vocabulary rather than
guessed.

## Item C: per-source detail preview in the UI (owner ask 2026-08-07)

**Problem.** The owner drew a 6.7 x 3.1 km extent over Port Talbot and
got a 2 m LiDAR raster without knowing why: the pipeline's packaged
raster budget (cog.py MAX_WINDOW_PIXELS = 16,777,216) honestly fell
back to the overview level, but nothing SAID so before download.

**Design.** A third optional LayerSource extension beside covers/tier:
`detail(bbox) -> str | None`, a short human sentence naming the quality
this exact extent gets. Sources without one contribute nothing (the
codebase's optional-extension convention). Implementations:

- lidar_wales: computes the level the real read_window would choose for
  the padded extent (the same quartering walk estimate() already
  mirrors) and returns `1 m at this extent` or `2 m at this extent
  (extents under about 4 x 4 km come back at 1 m)`, or 4 m etc for the
  genuinely huge. The number is computed, never guessed.
- elevation: `30 m (Copernicus GLO-30)` for the default model; a
  configured instance names ITS OWN model's resolution and label
  (`90 m (Copernicus GLO-90)` for COP90, and so on), because the
  Settings DEM dropdown is a first-class path and a fixed string would
  lie there. Ruled during item C's build (review of 2026-08-07): this
  spec's purpose, a truthful quality preview, governs over any literal
  example string in it. Item D and later implementers: do not re-copy
  the fixed-literal pattern for sources with per-request configuration.
- os_open: `1:10,000 scale, generalized footprints (OS OpenMap Local)`.
- osm/overture: `traced footprints and centrelines, typically 1 to 5 m
  positional accuracy`.
- inspire: `indicative extents, not legal boundaries`.
- os_uprn: `one point per addressable location`.
- lidar_cardiff (item D): `25 cm at this extent, flown 2011` where
  covered.

**Surface.** The resolution entries in the estimate payload gain a
`detail` field per source; the UI tier list appends it to each line
(`terrain: LiDAR terrain (Wales, 1 m), 2 m at this extent (extents
under about 4 x 4 km come back at 1 m)` reads long but says exactly
what the owner asked to know); survey.json records the same strings so
a package documents what quality it was built at. No new estimate call,
no network: detail() is disk-and-arithmetic only, like covers().

## Item D: Cardiff 25 cm LiDAR (owner ask 2026-08-07)

**What exists.** Task zero (2026-08-05) verified: ten 25 cm tiles from a
2011 flight covering part of central Cardiff on DataMapWales, and
nothing else at that resolution anywhere in Wales. The owner has
projects in Cardiff and wants that detail available.

**Design.** A new opt-in source `lidar_cardiff` ("LiDAR terrain
(central Cardiff, 25 cm, flown 2011)"): fetches the 2011 tiles
intersecting the extent, packages `<stem>_lidar25_dsm.tif` /
`<stem>_lidar25_dtm.tif` (same writer, same reader, same GH script
compatibility), covers() from the ten tiles' real footprint (probed at
plan time), tier above lidar_wales for terrain WHERE COVERED. The
detail() string and every UI/provenance surface carries `flown 2011`:
fourteen years stale, buildings since then are absent or different, and
the owner decides per project whether vintage detail beats current
coarseness.

**Deliberate exclusions.** Heights fusion and roof forms stay on the
2020-2023 1 m data: heights belong to the CURRENT building stock, and a
2011 DSM writing heights onto 2026 footprints would fabricate. Terrain,
contours at fine intervals, and detail meshes are what 25 cm serves.
(The DTM portion changes little in 14 years; if the owner later wants
25 cm ground blended into the .egrid, that is a separate decision taken
with eyes open, not a default.)

**Plan-time verification required.** Exact DataMapWales endpoints,
formats, tile index, licence text, and whether the 2011 data is DSM,
DTM or both: a live probe before any task is briefed, per this
project's standing rule.

## Item E: full-resolution LiDAR raster option (OWNER-GATED)

Raising MAX_WINDOW_PIXELS (or a per-request override) so large extents
can package at 1 m: Port Talbot at 1 m means roughly 80 MB per raster
and four-times-heavier meshes. The owner is calibrating with a
site-scale 1 m pull first; this item proceeds only if they ask after
that. Recorded here so the decision has a home; no build order slot
until gated open.

## Item F: the OS benchmark

**Purpose.** Measure OSM, Overture and the LiDAR-derived outputs
against OS's best data per category, so the tier tables become measured
rather than believed, improvement is targeted, and the epoch-shift
question is settled by road-offset statistics against a BNG-native
survey-grade reference.

**Mechanism.** OS Data Hub Premium DEV MODE (free, evaluation-only; a
dev-mode project key, never shipped, never in a package) pulling NGD
buildings/transport over the owner's test extents, plus optionally one
owner-purchased MasterMap extract over Cowbridge as a static licensed
ground truth. A `mapgen benchmark` CLI command produces a report:
per-category counts, footprint IoU distributions, centreline offset
statistics (mean vector + spread, which answers the epoch question),
feature classes present in OS and absent here. Output is a markdown +
JSON report in a benchmarks/ directory, never a package file.

**The firewall (binding).** Nothing from any OS premium product enters
a package, a fusion, a shard cache, or any output a client could
receive. The benchmark reads premium data, computes statistics, writes
the report, and discards the data. No geometry, no attribute, no
correction derived from premium data flows into mapgen outputs. This is
the derived-data rule that keeps every mapgen package licence-clean,
and it is a review criterion for every benchmark task.

**Shared machinery.** The MasterMap GML parser built for the extract
comparison is written as a reusable reader, because the deferred
premium IMPORT path (a client-licensed extract dropped into a package
as top tier) will consume it later. Import stays deferred; only the
parser is shared.

## Build order

1. Item C (detail preview): smallest, immediately useful, extends the
   resolver that just shipped.
2. Item A (categorised boundaries): all inputs already in packages.
3. Item B (roofs + canopy): the flagship; benefits from A's parcel work
   only socially, not technically.
4. Item D (Cardiff 25 cm): needs its live probe; independent of A-C.
5. Item F (benchmark): after A-D so the comparison measures the
   finished stack; before phase 2 item 4 (constraints) so its findings
   steer the tier tables early.
6. Item E: only if the owner opens the gate.

Phase 2 item 4 (constraints/Cadw/planning.data.gov.uk) follows this
addendum unless the owner reorders.
