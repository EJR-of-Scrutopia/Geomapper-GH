# mapgen phase 2 item 4: planning constraints for Wales

Design spec, approved by the owner in conversation on 2026-09-18. The
phase 2 spec's global constraints carry over unchanged: no new
third-party dependencies, no build step, token-gated routes, no URL or
key in any exception message or log line, licences and attribution
recorded in survey.json, live tests behind the `live` marker, nothing
fabricated from missing data, no em dashes, the owner owns the work.

## The owner's decisions

1. **Output: layers and a summary.** GeoJSON layers for Grasshopper and
   Urbano, plus a short plain-English constraints summary.
2. **Reach: the site plus 250 m.** Layers and summary cover the drawn
   rectangle and a 250 m margin around it, and the summary separates
   what is on the site from what is only in the margin, so the setting
   of a listed building, monument or SSSI just over the boundary is
   caught.
3. **Scope: Wales now, England next.** This build is Wales only,
   designed so an England source (planning.data.gov.uk) slots in as a
   second source afterwards.

## Sources

Every layer below is served by the Welsh Government's DataMapWales
GeoServer WFS 2.0 at `https://datamap.gov.wales/geoserver/wfs`, needs no
key, and is published under the Open Government Licence v3 (verified on
each layer page, 2026-09-18). Research notes with verbatim quotes are
kept outside the repo; the facts the build depends on are restated here.

| Designation (as written in the summary) | typeName | Output group | Attribution group |
|---|---|---|---|
| Flood zone, rivers | `inspire-nrw:NRW_FLOODZONE_RIVERS` | flood | A |
| Flood zone, sea | `inspire-nrw:NRW_FLOODZONE_SEAS` | flood | A |
| Flood zone, surface water and small watercourses | `inspire-nrw:NRW_FLOODZONE_SURFACE_WATER_AND_SMALL_WATERCOURSES` | flood | A |
| TAN15 defended zone | `inspire-nrw:NRW_TAN15_DEFENDED_ZONES` | flood | A |
| SSSI | `inspire-nrw:NRW_SSSI` | nature | B |
| Special Area of Conservation | `inspire-nrw:NRW_SAC` | nature | B |
| Special Protection Area | `inspire-nrw:NRW_SPA` | nature | B |
| Ramsar site | `inspire-nrw:NRW_RAMSAR` | nature | B |
| National Nature Reserve | `inspire-nrw:NRW_NNR` | nature | B |
| Local Nature Reserve | `inspire-nrw:NRW_LNR` | nature | B |
| Ancient woodland | `inspire-nrw:NRW_ANCIENT_WOODLAND_INVENTORY_2021` | nature | B |
| National Park | `inspire-nrw:NRW_NATIONAL_PARK` | landscape | B |
| AONB (National Landscape) | `inspire-nrw:NRW_AONB` | landscape | B |
| Heritage Coast | `inspire-nrw:NRW_HERITAGE_COAST` | landscape | B |
| Listed building | `inspire-wg:Cadw_ListedBuildings` | listed_buildings | C |
| Scheduled monument | `inspire-wg:Cadw_SAM` | heritage | C |
| Registered historic park or garden | `geonode:cadw_rhpg_registeredareas` | heritage | C |
| Registered historic landscape | `inspire-wg:Cadw_HistoricLandscapes` | heritage | C |
| World Heritage Site | `inspire-wg:vGeoServer_WorldHeritageSites_Public` | heritage | D |
| Conservation area | `geonode:conservation_areas_wales` | heritage | D |

The flood rows are the Flood Map for Planning zones, which TAN15 uses.
The merged rivers-and-sea layer is deliberately not used: the summary
names the source of the flooding, which the merged layer loses.

**Attribution groups**, reproduced verbatim wherever a layer is used:

- **A (NRW flood).** "Contains Natural Resources Wales information ©
  Natural Resources Wales and database right. All rights reserved. Some
  features of this information are based on digital spatial data
  licensed from the UK Centre for Ecology & Hydrology © UKCEH. Defra,
  Met Office and DARD Rivers Agency © Crown copyright. © Cranfield
  University. © James Hutton Institute. Contains OS data © Crown
  copyright and database right."
- **B (NRW designations).** "Contains Natural Resources Wales
  information © Natural Resources Wales and Database Right. All rights
  Reserved. Contains Ordnance Survey Data. Ordnance Survey Licence
  number AC0000849444. Crown Copyright and Database Right."
- **C (Cadw).** "Designated Historic Asset GIS Data, The Welsh Historic
  Environment Service (Cadw), DATE, licensed under the Open Government
  Licence", with DATE replaced by the survey's own fetch date, as Cadw's
  own wording instructs ("the date that you received the data from
  Cadw").
- **D (no statement published).** The layer page states OGL v3 and no
  attribution text. The package records "Contains public sector
  information licensed under the Open Government Licence v3.0.",
  which is OGL v3's own default attribution, plus the dataset title and
  publisher.

## Deliberately left out, and said so

- **Coal mining risk.** The Mining Remediation Authority (the Coal
  Authority until November 2024) publishes Development High Risk Areas
  only as map tiles with no queryable service, and its older published
  terms restrict re-use for anything within its public task. The
  summary lists it under "Not checked", pointing to the authority's own
  free interactive map.
- **Tree Preservation Orders.** No national Welsh dataset exists. Not
  checked; the summary says to ask the council.
- **England.** The next build. Not checked outside Wales (see
  "Coverage").

## Licence and usage position

1. **The OS licence number in attribution B.** The licence is OGL v3 on
   every layer page; the licence number is part of the attribution
   statement to reproduce, the same arrangement as the HM Land Registry
   INSPIRE data this tool already packages. Carried verbatim; not a
   blocker.
2. **robots.txt.** DataMapWales's robots.txt disallows `/geoserver/`
   for crawlers. mapgen is not a crawler: it sends one request per
   layer (about twenty) per survey the owner starts, over one small
   area, identified by its User-Agent, and `lidar_wales` already queries
   the same server. Owner action before any public release: a short
   courtesy note to DataMapWales describing that pattern. Not a blocker
   for this build.
3. **Conservation areas** are boundaries only. The dataset's own caveat
   is carried into the summary: "This dataset provides information on
   the map boundaries only. For the full information, including
   information on designations, individuals should contact the
   appropriate Local Authority."

## Architecture

**One new source, `constraints_wales`**, display name "Planning
constraints (Wales)", in `src/mapgen/sources/constraints_wales.py`,
driven by a declarative table of the twenty layers above (typeName,
designation label, output group, attribution group, and which property
carries the name and the grade or zone). Rejected alternatives: one
source per designation (twenty entries cluttering the picker and the
Advanced list), and fusing constraints into the `.osm` (Urbano already
reads GeoJSON; the `.osm` fusion adds nothing for this data).

**Pure logic in `src/mapgen/constraints.py`**, no network: clipping
polygons to a rectangle, area share of the site in British National
Grid metres, point counting by grade, and composing the summary text
and the survey.json record from fetched features. Everything testable
without a server.

**Fetch.** One WFS 2.0 GetFeature per layer, one at a time:
`typeNames=<typeName>`, `outputFormat=application/json`,
`srsName=EPSG:4326`, and `bbox=<minLon>,<minLat>,<maxLon>,<maxLat>,EPSG:4326`
over the extent padded by 250 m. Two verified traps: without
`srsName`, GeoServer answers in EPSG:27700; and the bbox must be in
lon,lat order (lat,lon returns nothing, silently). Paged with `count`
and `startIndex` so a dense layer (485 surface-water polygons over
Cowbridge alone) is never truncated; a layer whose total would pass a
fixed ceiling of 20,000 features is recorded as failed rather than cut
short. The shared descriptive User-Agent; the request timeout used by
the other WFS callers. `cancel` is checked between layers.

**The server's bbox filter is a prefilter, never the answer.** A
GeoServer store can be set to a "loose" bounding box, matching on a
feature's envelope rather than its shape: the research query over a
1.5 km Cowbridge box returned a Heritage Coast feature although the
Glamorgan Heritage Coast lies about 7 km south. Every returned feature
is therefore re-tested locally for true intersection with the site and
with the margin, in British National Grid metres, and one that touches
neither is dropped before it reaches any file, count or summary line.

**Failures are never absences.** A layer that fails (transport, status,
unparseable body, over the ceiling) goes into `tile_failures` with its
reason and appears in the summary under "Not checked", never under
"Checked, none found". The run carries on with the other layers.

**Coverage.** `covers(bbox)` answers from a committed file, never the
network: `src/mapgen/data/wales_local_authorities.geojson`, the 22
Welsh unitary authorities to the high-water mark from DataMapWales
`inspire-wg:localauthorities` (OGL v3, 2026-09-18), simplified to about
20 m for size (the full layer is 7.2 MB and 270,107 vertices), with the
script that produced it committed beside the source as
`tools/build_wales_authorities.py`. "full" when the padded extent lies
wholly within the union, "partial" when it crosses the edge (the coast
or the English border), "none" otherwise. The same file names the
council or councils the site touches, for the summary's "confirm with"
line. The LiDAR mosaic's bounding rectangle is not used: it includes
parts of England, where every Welsh layer would return nothing and a
"none found" would be false.

**Selection.** Auto-selected by the existing coverage mechanism
wherever `covers()` answers "full" or "partial". A new resolver
category, "planning constraints", with this source at tier 1 in Wales,
so the Advanced tier list names it. The picker's layer panel needs no
new line.

**Estimate.** No network. A small fixed byte figure and about one
second per layer.

## Outputs in the package

Five GeoJSON files, each written only when it has features, all listed
in `possible_outputs(stem)` so the stale-output sweep can remove a
previous run's copy:

- `<stem>_constraints_flood.geojson`
- `<stem>_constraints_nature.geojson`
- `<stem>_constraints_landscape.geojson`
- `<stem>_constraints_heritage.geojson` (areas and scheduled monuments)
- `<stem>_constraints_listed_buildings.geojson` (points)

Every feature carries: `designation` (the table's label), `name`, `grade_or_zone`
(a listed building's grade, a flood zone's own risk value, or null),
`reference` (the source's record number where it has one), `where`
(`"site"` or `"margin"`), and `source` (the typeName). Area geometry is
clipped to the padded extent; points are kept as they are. `where` is
what Urbano's key/value import filters on, the same pattern as the
categorised boundaries' `category`.

**`<stem>_constraints.txt`**, the summary, short lines under fixed
headings. Worked example, the shape the tests pin:

```text
Planning constraints: Cowbridge with Llanblethian, 2026-09-18
Indicative, from open data. Confirm with Vale of Glamorgan Council.

On the site
- Conservation area: Cowbridge (64% of the site)
- Flood zone, rivers, Zone 3 (12% of the site)
- Listed buildings: 38 (Grade II* 2, Grade II 36)
- Scheduled monument: Cowbridge Town Wall

Within 250 m
- Conservation area: Llanblethian
- Listed buildings: 11 (Grade II 11)

Checked, none found
- Flood zone, sea; SAC; Special Protection Area; Ramsar site; National Nature Reserve; Local Nature Reserve; National Park; AONB (National Landscape); Heritage Coast; Registered historic park or garden; Registered historic landscape; World Heritage Site

Not checked
- Coal mining risk: no open data service. See the Mining Remediation Authority's interactive map.
- Tree Preservation Orders: no national dataset for Wales. Ask the council.

Conservation areas are boundaries only; contact the council for full details.

Sources
- <each attribution group used, verbatim>
```

Rules the tests pin: an area's share is its clipped area over the
drawn site's area, both in British National Grid metres, rounded to a
whole percent, written "under 1%" below 0.5%, and "the whole site" at
99.5% and above; "Within 250 m" lists only features that touch the
margin and not the site; a designation checked and absent everywhere
goes under "Checked, none found" by its table label; any layer that
failed, and the two permanent exclusions, go under "Not checked"; when
`covers()` answered "partial", "Not checked" gains "Part of the extent
is outside Wales and was not checked." Multiple councils are listed
together in the "Confirm with" line.

**survey.json** gains a `constraints` record carrying the same facts as
the summary in structure (on_site, margin, none_found, not_checked, each
a list of objects), the councils, the margin in metres, and the
per-layer feature counts. Provenance records the layers used, the
licence, and each attribution statement as used, with Cadw's DATE
already substituted.

**The picker's log** prints the "On the site" lines when the run
finishes, so the headline is on screen without opening the folder.

## Testing

- Recorded real WFS responses (small, trimmed, committed as fixtures)
  stand in for the network in every ordinary test.
- `constraints.py`: clipping, area share against hand-computed
  rectangles, the rounding rules, grade counting, site versus margin,
  "Checked, none found" versus "Not checked", the partial-coverage line,
  the Cadw DATE substitution.
- The source: request shape (srsName, lon,lat bbox order, paging), a
  failed layer landing in `tile_failures` and never in none found, the
  feature ceiling, `covers()` full, partial and none against the
  committed councils file, cancel between layers.
- The local intersection re-test: a feature whose envelope overlaps the
  padded extent but whose shape touches neither the site nor the margin
  is dropped.
- One live test over Cowbridge expecting the Cowbridge and Llanblethian
  conservation areas and at least 30 listed buildings, and expecting no
  Heritage Coast (the loose-bbox trap, caught live). It asserts nothing
  about the SSSI or ancient woodland counts the research reported,
  since those came from the same unfiltered bbox query.

## Not promised

- A formal constraints search. The summary says "Indicative, from open
  data", and names the council to confirm with.
- Anything the open layers do not hold: TPOs, Article 4 directions,
  coal mining risk, local plan allocations, the full text of any
  designation.
- England, until its own build.

## Next, not in this build

England through planning.data.gov.uk: area queries use the `geometry`
parameter with WKT (a `bbox` parameter is silently ignored), GeoJSON
from `entity.geojson`, OGL v3, and every dataset page warns that the
data "may be incomplete and not yet cover all of England", so absence
there must read as "not published for this council" rather than "none
found".
