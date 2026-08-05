# mapgen phase 2: filling in what OSM cannot say

Design spec, distilled from three design conversations with the owner on
2026-08-04 and 2026-08-05, after phase 1 reached Grasshopper end to end
(streets, buildings and terrain rendering from a mapgen package).

## The problem, in the owner's framing

OSM gives geometry without context: buildings with no heights and no plot
boundaries, roads as bare centrelines, no designations, no planning history,
no imagery. The professional answer has always been ESRI or paywalled national
data. Phase 2 assembles the same capability from open UK sources plus one
affordable premium tier, packaged the same way phase 1 packages OSM.

## Decisions already made by the owner

- LiDAR is the keystone source. UK-wide, per-nation backends.
- Property boundaries arrive as INSPIRE-derived **curves tagged into the
  `.osm` file** (`boundary=property`, `source=hm_land_registry`), not as a
  separate polygon layer. Boundaries are reference, not legal precision.
- OS Data Hub Premium is in, as a keyed source like OpenTopography.
- BGS geology enters at the non-commercial tier now, commercial later.
- Digimap is out: tied to one student email, educational licence anyway.
- Google/Bing/Mapbox imagery is out on licensing. Sentinel-2 is always
  packaged; licensed orthos when available.
- planning.data.gov.uk is in despite being England-only.
- Per-extent tier resolution: the backend determines which sources cover the
  download area per category, fuses or replaces per policy, and records who
  won each category in `survey.json`.
- A small-site "precision pull" preset: top tier everything, imagery,
  planning history.
- No fifth tile colour and other phase 1 UI decisions stand; the tier list
  surfaces through the existing availability-watcher slot in the UI.

## Sources

| # | Source | Categories served | Access | Licence posture |
|---|---|---|---|---|
| 1 | NRW LiDAR via DataMapWales (Wales), EA National LiDAR Programme (England), Scottish Remote Sensing Portal | terrain (upgrades `.egrid`), contours, building heights | download APIs, per-nation | open (OGL), verify per portal |
| 2 | HM Land Registry INSPIRE Index Polygons | property boundaries | per-local-authority download, monthly refresh | open + required attribution; indicative not legal |
| 3 | OS Open Roads + OS OpenMap Local + Boundary-Line + Open Greenspace + Open UPRN | roads with names/class, functional sites, admin boundaries, addresses | OS Data Hub OpenData plan | open |
| 4 | OS Data Hub **Premium** (MasterMap Topography, Building Height Attribute) | kerb-level topo, measured heights; per-category **replacement** of OSM | API, key in settings, monthly free credit | premium; credit terms to verify |
| 5 | DataMapWales constraints + Cadw | flood, SSSI/SAC, conservation areas, ancient woodland, listed buildings, monuments | OGC APIs / WFS | open, verify each layer |
| 6 | planning.data.gov.uk | designations as data, England only | API | open |
| 7 | PlanIt aggregator | planning applications by location, decisions, portal links | API | open aggregator; per-council PDF fetch is best-effort |
| 8 | BGS geology | 1:625k open now; 1:50k view; borehole scans | WMS/WFS + downloads | tiered; verify current 1:50k terms |
| 9 | Sentinel-2 | context imagery, land cover | open API | fully open |
| 10 | Welsh Government aerial (DataMapWales) | site orthophotos | WMS | licence unknown, verify before promising |

## Architecture

### Coverage and tiers

Each `LayerSource` gains two declarations:

- `covers(bbox) -> bool | partial` : does this source have data here.
- `tier(category) -> int` : quality rank for each category it serves.

A **resolver** runs at estimate time: for the drawn extent and selected
categories, it produces the tier list (what is available here, what wins,
what fills gaps). The UI shows it in the availability-watcher slot before
Download is pressed. `survey.json` records the resolution: which source won
each category and why (tier, coverage, licence).

### Fusion vs replacement, per category

- **Fusion** (default): a source adds features or attributes to the OSM
  base. INSPIRE boundaries become tagged ways in the `.osm`; LiDAR heights
  become a `height` tag on existing building ways; designations become their
  own tagged geometry.
- **Replacement**: a source substitutes the OSM base for a category.
  MasterMap topography replaces OSM roads/buildings when present and
  licensed, because two geometric truths of one street are worse than either.
  Replacement is recorded per category in `survey.json` and visible in the
  tier list before download.

### Contours are generated, not downloaded

From the LiDAR DTM, marching squares (stdlib, same discipline as
`egrid.py`), at intervals packaged per drawing scale: 5m context, 1m for
1:1250, 0.5m/0.25m for 1:200 and 1:50. Output: polylines with an `elevation`
attribute, so Grasshopper label scripts (text size, spacing, placement along
the line: the owner's ask) stay simple and client-side. The LiDAR DTM also
regenerates the `.egrid` at up to 30x phase 1's resolution, and DSM minus DTM
sampled per footprint writes measured heights onto buildings OSM left flat.

### The precision pull

A preset, not a mode: smallest extent, every top-tier source, orthos if
licensed, contours at fine intervals, planning history via PlanIt (with
document links, PDFs best-effort), everything else identical to a normal run.
The optional AI write-up of planning history is a **separate post-step**
outside the deterministic pipeline, needs its own key, and phase 2 only has
to leave a clean seam for it: the structured application list it would
summarise.

## What phase 2 explicitly does not promise

- Legal-grade boundaries (INSPIRE is indicative; the spec says so in the UI
  copy wherever boundaries appear).
- Welsh planning designations as data (no national feed exists; PlanIt
  covers applications, not designations).
- Sewer records (Dwr Cymru will not sell GIS).
- Google-quality imagery (licensing, not capability).
- Northern Ireland LiDAR parity.

## Task zero: the verification pass

Before any source task is briefed, one investigation task verifies and
records, with dates and quotes:

1. DataMapWales LiDAR endpoints, products, resolutions, coverage over the
   owner's working areas, licence text.
2. EA National LiDAR Programme equivalents for England.
3. OS Data Hub plans: exact free premium credit terms, what MasterMap access
   costs against it per typical site.
4. INSPIRE polygon download mechanics, refresh cadence, attribution wording.
5. BGS 1:50k current licensing tiers, non-commercial terms.
6. PlanIt API coverage of Welsh councils, rate limits, terms.
7. Welsh aerial WMS licence: can an ortho be packaged.
8. Sentinel-2 access route without new dependencies.

Anything the pass cannot confirm gets cut or demoted, not assumed. Phase 1's
standing rule applies: claims from memory are hypotheses until checked, and
this project has paid for forgetting that several times.

## Build order after task zero

1. LiDAR source (Wales + England backends) with contours + heights + egrid
   upgrade. One source, three of the owner's asks.
2. INSPIRE boundaries as tagged curves, shared-edge deduplication included.
3. OS Open pack (roads, OpenMap Local, UPRN) and the resolver + tier list UI.
4. DataMapWales constraints + Cadw + planning.data.gov.uk.
5. OS Data Hub Premium with per-category replacement.
6. Sentinel-2 + orthos where licensed.
7. PlanIt planning history + the precision pull preset.
8. BGS non-commercial.

Standing constraints carry over unchanged: no new third-party dependencies,
no build step, token-gated routes, keys never logged, licences and
attribution recorded per source in `survey.json`, no em dashes, the owner
owns the work.
