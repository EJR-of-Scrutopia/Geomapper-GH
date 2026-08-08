# The Urbano project setting file

**mapgen writes this file itself now** (task 35, `src/mapgen/urbano.py`), on
every run, whether or not the bridge ran. Everything below is the evidence
that shaped it, and the reference for testing it by hand.

`sample_project_setting.json` beside this file was produced by **Urbano's own
serialiser**, not written by hand and not guessed. A throwaway .NET probe
loaded `Urbano.SiteAnalysis.gha` out of process, constructed a real
`WorldOrigin` and `UrbanoProjectSetting` for a London bounding box, and called
its `ToJson()`. It is property for property what
`tools/UrbanoBridge/Program.cs` already writes, which is how we know mapgen's
JSON shape was correct all along.

## What reading the file actually does, decompiled

Task 35 read `Urbano.Grasshopper.ProjectSettingComponent`,
`Urbano.Core.Data.UrbanoProjectSetting` and
`Urbano.Core.Helpers.GeoProjector` with ilspycmd rather than inferring their
behaviour. Four things follow, and mapgen's writer is built on them.

**`FileNameStr` is not checked against anything.** `TryLoad` globs
`*_project_setting.json`, deserialises each match, and accepts it only if
`GetBoundString(Top, Bottom, Right, Left)` equals the bound string it was
asked for and the granularity matches. `FileNameStr` takes no part in that.
So it is free to be mapgen's own stem, which it has to be, because:

**Every data path is rebuilt, never read.** The component's download routine
computes `Path.Combine(Folder, FileNameStr)` and appends its own fixed
extension: `.osm` then `.osm.pbf` for OSM, `.blocks` for census, `.egrid` for
elevation. The path strings in the file are ignored on that route. A stem of
`Barry-Waterfront_2026-08-03` makes Urbano find mapgen's own
`Barry-Waterfront_2026-08-03.osm` and skip the download; Urbano's coordinate
string convention would send it looking for a file nobody has.

**Naming a layer commits Urbano to fetching it.** The requested layer set is
the `Layers` list unioned with every file path field that is not empty. For
`elevation` with no `<stem>.egrid` beside it, that means
`DownloadUsgs3DepTiffForBounds`, which is United States only. Since task 37
mapgen never names an elevation layer it cannot back with a `.egrid`, and
since task 39 it writes that `.egrid` itself, so a UK package carries the
layer AND skips the download. `climate` is worse and mapgen never writes it
either: it is re-fetched with no file check at all.

**Overture is not downloaded at all.** The routine has no Overture branch,
and rewrites `OvertureFilePath` as an empty string. Urbano 2.2.1.2 still
reads an Overture parquet elsewhere (`OVERTURE_KEY_BLDG_HEIGHT` is a real
key) but no longer ships code to fetch one.

One consequence to know about: **the component rewrites the file** when it
runs, with its own recomputed origin and its own path conventions. mapgen's
version is an input to that, not a permanent record. Re-run `mapgen bridge`
over the folder to get mapgen's back.

## What each path field actually is, and why the DEM is not in one

Every one of these is an instruction to a specific parser, not a hint about
where a file lives. Established in task 37 by running Urbano's own code out
of process against a real mapgen package, not by reading it.

**`OsmFilePath` is dispatched on its extension, case sensitively.** `.osm` is
read as OSM XML and `.osm.pbf` as OSM PBF (`Key.OSM_EXTENSION` and
`Key.OSM_PBF_EXTENSION`, read off `Key` at runtime). Anything else leaves
`OsmExtension.ImportOsmGeometries` with a null source and throws before it
reads a byte. mapgen writes OSM XML named `.osm`, which is exactly what
Urbano wants: its own reader took a real mapgen `.osm` and returned 98 street
polylines and 759 building polygons. **No PBF writer is needed and none was
written.**

**`ElevationFilePath` is a protobuf `ElevationGrid`, never a raster.** All
seven components that read it hand it straight to
`ProtoBuf.Serializer.Deserialize<ElevationGrid>`: Import Buildings 3D, Import
Public Transits, Import Amenity and Import Green Space do it on any non-empty
string with no toggle and no `File.Exists`; Import Streets and Import Block
do it behind a toggle; and Import Terrain, the one whose name suggests it
would take a GeoTIFF, does it too. mapgen named its `.tif` here until task 37
and that is what produced

> Solution exception: Unexpected end-group in source data; this usually means
> the source data is corrupt

on Import Streets and Import Buildings. It is protobuf-net reading a TIFF.
The field names a real `.egrid` or nothing, and since task 39 mapgen converts
the DEM into one, so on an ordinary package it names a real file. The `.tif`
is still written, still named `<stem>.tif`, and still recorded in survey.json;
it is simply not what Urbano opens. See "The elevation grid" below.

**`OvertureFilePath` is a GeoParquet file, and mapgen keeps its GeoJSON there
anyway.** Import Buildings 3D reads it only when its data source dropdown is
set to Overture, and then through `GeoParquetReader`, which checks the magic
bytes and refuses anything else by name (`not a parquet file, head: 7b227479`
for our GeoJSON). Emptying the field would not make that branch work, and the
field is inert on every other route, while a real path is exactly what
**Deserialize Project Setting** hands to **Import Geojson File**, the
component that does read it. So it stays. If Import Buildings says "not a
parquet file", set its data source to OSM.

## Import Geojson File: the Z ordinate is discarded, not merely unread

Task 6 asked whether a GeoJSON position written as `[lon, lat, z]` (a
mapgen contour vertex, for instance) survives **Import Geojson File**
with its height intact. Decompiled `Urbano.Grasshopper.
ImportGeojsonComponent.SolveInstance` and its two downstream helpers to
settle it rather than guess, because this README's other GeoJSON mention
(the `OvertureFilePath` note above) never reached this component's own
body. It does not survive, and the reason is architectural, not a
parsing bug:

**The file parses through the real, unmodified NetTopologySuite, which
does read Z.** `Urbano.Core.Helpers.GeoJson`'s constructor is `new
NetTopologySuite.IO.GeoJsonReader().Read<FeatureCollection>(jsonString)`,
the genuine NTS reader, which populates a `Coordinate`'s `Z` field
from a three-element position array. Nothing drops the third ordinate
here.

**But `SolveInstance` never reads it.** For every vertex it calls
`Urbano.Grasshopper.UrbanoGhHelpers.LatLonToRhinoPoint(coordinate2.Y,
coordinate2.X, item, item2, elevationGrid)`, passing only `Y` (latitude)
and `X` (longitude) off each parsed `Coordinate`, never `.Z`. The method has
no parameter for it to go in:

    public static Point3d LatLonToRhinoPoint(double lat, double lon,
        string utm, Transform toOrigin, ElevationGrid elevationGrid)
    {
        var (e, n) = GeoProjector.LatLongToUTM(lat, lon, utm);
        var result = new Point3d(e, n, 0.0);
        result.Transform(toOrigin);
        if (elevationGrid != null)
        {
            double z = ElevationExtensions.SampleGridBilinear(elevationGrid, e, n);
            if (double.IsFinite(z)) result.Z = z;
        }
        return result;
    }

Height starts at a literal `0.0` and is only ever overwritten by
sampling an `ElevationGrid`, which this same component builds from a
freshly downloaded USGS 3DEP tiff
(`TiffExtensions.DownloadTiffFile.DownloadUsgs3DepTiffForBounds`) behind
its own "sample elevation" toggle. That DEM source is United States
only (see "Naming a layer commits Urbano to fetching it" above), so for
a UK mapgen package the toggle path never fires, and every imported
vertex lands at Z = 0 regardless of what the source file's third
ordinate said.

**Conclusion: a contour vertex written as `[lon, lat, elevation]` would
import identically to today's `[lon, lat]`, flattened to Z = 0.** This is
the mechanism behind the owner's own observation that contours arrive in
Urbano flattened. `src/mapgen/contours.py`'s vertex serialisation is
unchanged by task 6; the `elevation` property remains the only place a
contour's height reaches Urbano at all, and reaching Grasshopper with a
real Z would need a change on Urbano's side
(`LatLonToRhinoPoint` gaining a Z parameter, or the component reading
`coordinate2.Z` itself), not mapgen's.

Evidence chain, decompiled with `ilspycmd -t <type> "...\Urbano2\2.2.1.2\
Urbano.SiteAnalysis.gha"` on 2026-08-06 (same assembly and tool as task
5's negative-id check above): `ImportGeojsonComponent.SolveInstance`
calls `new GeoJson(File.ReadAllText(path)).FeatureCollection` (confirms
NTS parses Z into `Coordinate.Z`) and then, per vertex,
`UrbanoGhHelpers.LatLonToRhinoPoint(coordinate2.Y, coordinate2.X, item,
item2, elevationGrid)` (confirms `.Z` is never passed);
`LatLonToRhinoPoint` itself hardcodes `Z = 0.0` unless the US-only
`ElevationGrid` path overwrites it.

## The OS Open GeoJSON files: filtering by key and value in Import Geojson File

Phase 2 item 3's `os_open` source (`src/mapgen/sources/os_open.py`) writes up
to six GeoJSON files into the package root, each read the same way as any
other GeoJSON here: straight into **Import Geojson File**, whose component
this README's own "Import Geojson File" section above already covers for Z
handling. Every feature in every one of them carries `"source"` plus
whichever of the properties below OS's own data actually supplied (a
property with no value on a given feature is simply absent from it, never
written as `null` or an empty string), which is what makes filtering by key
and value in the component's own property mapping work at all: a feature
missing the key you filter on just does not match, rather than matching
emptily.

| File | Feature kind | `source` | Other properties to filter on |
| --- | --- | --- | --- |
| `<stem>_os_buildings.geojson` | Building polygons | `os_openmap_local` | `code`, `theme`, `class` |
| `<stem>_os_roads.geojson` | RoadLink lines | `os_open_roads` | `class`, `function`, `form`, `name`, `number`, `trunk`, `primary` |
| `<stem>_os_rail.geojson` | Railway lines | `os_openmap_local` | `class` |
| `<stem>_os_greenspace.geojson` | Greenspace polygons and access points | `os_open_greenspace` | `function`, `access`, `name` |
| `<stem>_os_sites.geojson` | Functional sites, stations, named places | `os_openmap_local` | `theme`, `class`, `name` |
| `<stem>_os_land.geojson` | Woodland and water | `os_openmap_local` | `kind` (`woodland`, `water_area`, `water_line`, `tidal_water`, `foreshore`) |

The filtering workflow the owner already uses elsewhere in Urbano applies
here directly: `os_roads` by `class` (OS's own road classification, e.g.
Motorway/A Road/Local Road) or `function` (e.g. Restricted Local Access) to
pull out a specific road grade rather than every RoadLink at once; `os_sites`
by `theme` to separate, say, education sites from transport ones; `os_land`
by `kind` to pull woodland out from tidal water. A file with no features for
this extent (Cowbridge's own closed railway branch is the standing example:
`os_rail` genuinely writes no file there, see `os_open.py`'s own module
docstring) is simply absent from the package; Import Geojson File is never
pointed at a name that is not there.

**`<stem>_os_roads.geojson` is a reference layer, deliberately never fused
into `<stem>.osm`.** Every other OS Open candidate (buildings) can be fused
because the base OSM layer is often genuinely missing footprints it never
had; OSM's own road network, by contrast, is close to complete over most of
Great Britain already, so fusing OS Open's roads on top of it would double
almost every road the `.osm` already carries rather than filling a real gap.
`os_roads.geojson` exists for comparison and for the owner's own selective
import instead: read it straight into Grasshopper through Import Geojson
File, filtered by `class` or `function` as above, alongside the `.osm`'s own
streets, never as a replacement for them. The same reasoning applies to
`os_rail`, kept as its own file for the same reason rather than merged into
the road one.

**Fused buildings do not arrive as a separate file at all: they arrive
inside `<stem>.osm` itself.** `src/mapgen/buildings.py`'s fusion step
(`_fuse_buildings_step`, `package.py`) reads `<stem>_building.geojson`
(Overture) and `<stem>_os_buildings.geojson` (this source) as CANDIDATES,
injects whichever footprints the base OSM layer is genuinely missing
straight into `<stem>.osm` as ordinary `building=*` ways tagged
`source=overture` or `source=os_openmap_local`, and runs before the LiDAR
heights fusion step, so an injected footprint with no height of its own
picks up a real DSM-minus-DTM `height` the same way an original OSM building
does whenever `lidar_wales` was selected and the extent falls inside Wales.
`<stem>_os_buildings.geojson` itself is left on disk afterwards exactly as
`os_open.py`'s `merge()` wrote it: every OS OpenMap Local building over the
extent, including the ones fusion recognised as already present and skipped,
useful for the owner's own direct GeoJSON import of OS's own building set
independent of the `.osm`, but not itself the record of what fusion actually
added. `survey.json`'s own `buildings_fusion` block (`written`,
`from_overture`, `from_os`, `kept_existing`, `skipped_overlap`) is that
record.

## The categorised boundaries file: filtering by category in Import Geojson File

Phase 2b item A's `<stem>_boundaries_categorised.geojson` (`src/mapgen/package.py`,
`_categorise_boundaries_step`) is packaged whenever `inspire` is selected
on a FRESH survey, beside the plain `<stem>_boundaries.geojson` item 2
already writes. A package downloaded before item A has no
`<stem>_parcels.geojson`, and `mapgen bridge` cannot conjure one (bridge
never re-runs a source's merge): re-survey the extent to get categories
for an older package. Read it
the same way as every other GeoJSON in this README, straight into **Import
Geojson File** (see "Import Geojson File: the Z ordinate is discarded, not
merely unread" above), and the owner's own workflow from there is the same
filtering pattern the OS Open section above already covers: set the
component's property mapping to filter on key `category`, value `garden`
(or `field`, `housing`, `retail`, `industrial`, `education`, `religious`,
`allotments`, `water`, `greenspace`, `woodland`, `recreation`, or
`unclassified`, whichever the extent actually produced; a category with
nothing in this package simply never appears as a value to filter on) to
pull that one category's own outlines into the canvas on its own.

**Why filtering to one category still gets a complete, closed set of
outlines.** Every INSPIRE parcel is classified on its own first, then each
category's own group of parcels is run through the same curve-deduplication
machinery `<stem>_boundaries.geojson` itself uses, separately, per category.
A shared wall between two parcels of the SAME category collapses to one
line exactly as it does in the plain file. A shared wall between two
parcels of DIFFERENT categories (a garden backing onto a field, say) is
kept once under EACH category rather than deduplicated away between them,
deliberately: filtering the import down to `category = garden` alone still
draws that shared wall, because the garden side of it is one of the curves
`garden`'s own group produced, and the same wall reappears, separately,
under `category = field` for the same reason. Filtering to a single
category is never left with a gap where a different-category neighbour
used to close the loop.

**This is an overlay-derived approximation, not a land-use record.** Every
feature's own `category` property comes from sampling the package's own
overlay data (fused building footprints, Overture land use, water,
OS Open Greenspace, OS OpenMapLocal woodland) at points inside each
parcel, majority rule, never from any authoritative land-use register.
Each feature's `note` property carries HM Land Registry's own boundary
caveat verbatim, identical to `_boundaries.geojson`'s (a parcel's own
extent is HM Land Registry's indicative geometry, category or not); the
separate, additional honesty statement about the category itself, the
fixed sentence `derived from map overlay, indicative`, is what
`survey.json`'s own `boundaries_categories` block carries instead, because
the category is mapgen's own sampled guess at what sits inside HM Land
Registry's geometry, not a second fact HM Land Registry itself supplied.

## Roof massing in Grasshopper

`<stem>_roof_massing.geojson` (phase 2b item B, `src/mapgen/roofs.py`) is
packaged whenever `lidar_wales` is selected, the extent's own rasters land at
the 1 m LiDAR level (see the `roof_forms` row of the README's `survey.json`
table), and at least one building classified. Read it the same way as every
other GeoJSON in this README, straight into **Import Geojson File**.

**The `[lon, lat, z]` positions arrive flattened, exactly as the "Import
Geojson File" section above already establishes for any GeoJSON Z ordinate.**
`LatLonToRhinoPoint` never reads it, so both the eaves polygon and the ridge
line land at Z = 0 on import regardless of what mapgen wrote into their third
coordinate. The heights are not lost, they are on the feature as ordinary
properties instead: `eaves` and `ridge`, both metres above the building's own
`ground_m`. Extrude the flattened footprint upward by its own `eaves` value to
get the wall, and loft from that raised polygon to the ridge line, itself
raised by its own `ridge` value, to get the roof; match a polygon to its ridge
line by the shared `building` property, since a footprint can have more than
one feature in the file. `direction` (gable ridge azimuth, 0 to 180; mono
downslope azimuth, 0 to 360; absent on `flat` and `complex`) orients a pitched
roof asset if you are placing one rather than lofting the fitted plane
directly.

`shape` and `quality` are filterable the same way `category` is on the
categorised boundaries file above: set Import Geojson File's property mapping
to filter on key `shape`, value `gable`, `flat`, `mono` or `complex`, to pull
one roof form out on its own, or on `quality` to keep only the fits you trust.
A ridge LineString carries no `shape` or `quality` of its own; find its
building's polygon by the shared `building` id instead.

**The vocabulary shipped is `gable`, `flat`, `mono` and `complex`.** A fifth
class, `hip`, was tried against a real Welsh town's true 1 m LiDAR and
dropped: at 1 m the DSM cannot tell a hip roof from a cross-gable, and a
synthetic sweep across footprint aspect, azimuth and noise answered hip correctly on
only 33 of 135 combinations, the same roof reading as hip, gable, complex or
flat depending only on which way it faced. Its cases fall through to
`complex`: honest eaves and ridge heights, no form claimed.

**Read `note` before treating this as a survey.** Every feature carries
`derived from LiDAR plane fits, indicative`. A fitted plane reads sharper than
the pixels it came from, but it will not resolve a conservatory, a dormer, or
anything else the 1 m DSM itself cannot see, and on a concave footprint a
ridge span can bridge a notch in the outline rather than stopping at it. A
building below the fitter's own honesty floor (too little of the footprint
explained, or a fitted ridge under 2.0 m of its own ground, the same floor a
building's `height` tag is already refused under) carries no roof tags and no
massing feature at all, rather than a guess.

**A re-run over an already-tagged package classifies nothing new.**
`fit_roof_forms` never re-touches a way that already carries `roof:shape`,
from an earlier run or from OSM itself, so a second `mapgen survey` or
`mapgen bridge` over the same package writes no new massing file: the first
run's file is the one that persists on disk.

## Canopy points

`<stem>_canopy.geojson` (phase 2b item B, `src/mapgen/canopy.py`) is packaged
whenever `lidar_wales` is selected and at least one cluster of above-ground
return qualified. Unlike roof massing, it carries no 1 m floor of its own: a
canopy cluster spans many pixels at any resolution this project produces, so
it is packaged at whatever pixel size the extent's own rasters carry, and the
`resolution` property on every feature says which.

Each feature is a Point at a cluster's own centre, flattened to Z = 0 on
import for the same reason the roof massing positions are (see above): place
a circle of `crown_radius` metres at each point instead, and drive its
extrusion, or a tree asset's height, from the feature's own `height` property
(the p90 of the cluster, metres above the ground beneath it, never the
maximum, so one stray high return does not set a whole canopy's height).

**`note` is `vegetation and other above-ground features, derived from LiDAR,
indicative`, deliberately not narrower.** A pylon or a crane clears the same
3 m floor above the DTM a tree does, and a DSM-minus-DTM raster cannot tell
one from the other; read this file as an above-ground survey, never a species
one.

## Bridge parity: canopy always, roof tags only at 1 m

Every Welsh package already on disk, however old, already carries the three
files both steps need: `<stem>.osm`, `<stem>_lidar_dtm.tif`,
`<stem>_lidar_dsm.tif`. `mapgen bridge` re-runs `_fit_roofs_step` and
`_canopy_step` over any of them exactly as a fresh `mapgen survey` would.
This is the opposite of item A's categorised boundaries, which need a fresh
merge and cannot be added to an old package this way: the asymmetry is real,
not an inconsistency in the tool, because every input roofs and canopy need
is already sitting in the folder.

**Canopy lands on every one of them.** It has no resolution floor of its own
and records `resolution_m` as whatever the package's rasters actually carry.

**Roof tags land only when a package's own rasters happen to be at the 1 m
LiDAR level**, and in practice that means an extent under about 4 x 4 km.
Every real Welsh package on this machine today (Cowbridge, Llantwit Major,
Port Talbot) was found, on validation, to be packaged at 2 m: a survey-sized
extent misses the fetch's own pixel budget by about 1.3% and the packager
falls back to the coarser overview silently. So a plain `mapgen bridge` over
one of today's real packages will not, in practice, add roof tags; it will
record exactly why, in `roof_forms.skipped_reason`, the same guidance the
estimate panel's own detail preview already gives before download. A future
extent under that guidance, or a future change to how the packager fetches,
is what would change that outcome; this documents what ships today, not a
promise about either.

## The elevation grid, which is how terrain reaches Grasshopper

`<stem>.egrid` (task 39, `src/mapgen/egrid.py`) is the DEM in the only format
any Urbano component reads elevation in. mapgen writes it on every run and on
every `mapgen bridge`, so a package downloaded before that task gains terrain
without being downloaded again.

**The contract**, from `Urbano.Core.Data.ElevationGrid` decompiled:

    [ProtoContract(SkipConstructor = true)] sealed class ElevationGrid
      [ProtoMember(1)] int      NX      nodes across, easting
      [ProtoMember(2)] int      NY      nodes up, northing
      [ProtoMember(3)] double   X0      easting of node 0, UTM metres
      [ProtoMember(4)] double   Y0      northing of node 0, UTM metres
      [ProtoMember(5)] double   DX      easting step, metres
      [ProtoMember(6)] double   DY      northing step, metres
      [ProtoMember(7)] double[] Z1D     NX*NY heights, metres
      [ProtoIgnore]    double[,] Z      rebuilt from Z1D after reading

Three things about it are not in the field list and are what make a grid right
or wrong. The frame is **absolute UTM metres** in the zone
`CoordinateReference.Utm` names, not the local world origin frame: every
consumer projects to UTM, samples the grid, and only then transforms to the
origin. **Row zero is the SOUTH edge**, where a GeoTIFF's row zero is its
north edge, so terrain written straight out of a raster comes out mirrored,
and it renders rather than failing. And **a hole is `double.NaN`**, which
`ToRhinoMesh` turns into an omitted face rather than a spike.

**mapgen does not devise the grid, it reproduces Urbano's own.**
`ProjectSettingComponent`, on the route that writes a `.egrid`, calls
`ElevationExtensions.BuildElevationGridFromTiff(bounds, 200.0, utm, tiff)` and
serialises the result. `mapgen.egrid.build_grid` is that method transcribed:
the same 200 m pad, the same three cell size tiers (5 to 15 m under 2 km, 15
to 50 m under 10 km, 50 to 200 m above), the same square cell preference, the
same node-aligned corner, the same bilinear sampling with nodata corners given
zero weight. The file mapgen writes is the file Urbano itself would have
written for the same site in the United States, and that was checked: on both
real Welsh DEMs the two grids have identical dimensions, origin and cell size,
and identical heights to the last bit of a float.

The 200 m pad is load bearing rather than cautious. Import Terrain does not
read the grid whole: it snaps the project's bounds to the grid's own lines
(`SnapBboxToGrid`) and crops (`CropAligned`), and the crop indexes the source
array with no bounds check at all. A grid sized exactly to the survey bounds
puts `Math.Ceiling` one line past the end of it.

**Expect holes, and expect them at the edges.** The grid covers the survey
bounds plus 200 m on every side, and the DEM OpenTopography returns is
slightly SMALLER than the bounds asked for: it returns whole source pixels
whose centres fall inside the request, and the bilinear sampler then needs one
more pixel beyond each edge again. So the outer 40 to 70 m of a survey has no
terrain under it, on top of the padded ring. On a 500 m Barry extent that is
3,763 of 12,432 grid points with data; on a 5 km Porthcawl extent it is 20,857
of 25,893. `mapgen survey` and `mapgen bridge` both print the two numbers, and
survey.json's `elevation_grid` block carries them.

Geometry placed on a node with no data keeps `z = 0`: `SampleGridBilinear`
returns NaN and every caller checks `IsFinite` before using it. On the
Porthcawl package that is 256 of 10,652 street vertices and none of 16,253
building vertices.

## Wiring a real mapgen package into Import Streets and Import Buildings

The shortest route, and the one the error above came from, does not involve
the Project Setting component at all. `GH_ProjectSetting.CastFrom` is
`JsonSerializer.Deserialize<UrbanoProjectSetting>(source.ToString())`, so any
Grasshopper text wired into a `ProjectSettingParam` becomes a project
setting. Read the package's `<stem>_project_setting.json` into a panel, wire
the panel into Import Streets' or Import Buildings' first input, and the
component reads mapgen's files by the paths in the file, verbatim. That is
why those paths have to be ones Urbano can open.

The Project Setting component route differs in one way worth knowing: when it
runs, it rebuilds `ElevationFilePath` as `<folder>\<stem>.egrid` whenever the
elevation layer was requested, **whether or not the download succeeded and
whether or not the file exists**. A downstream Import component then opens a
path that is not there. mapgen packages no longer request that layer, so this
cannot arise from one, but a hand-edited setting can walk into it.

## Checking the coordinate reference without any of this

The toolbar also has a **WSG84 to UTM ProjPt** component, which projects a
point with Urbano's own transform. Feed it the bottom left and bottom right
corners of a real extent and compare its answer with `CoordinateReference` in
the file mapgen wrote. That is a check against Urbano itself on your own
ground, and it is worth more than any number of test fixtures.

## What this test does and does not prove

**It proves the wiring**: that the Project Setting component accepts a JSON
string on its text input, parses it, and emits a `ProjectSettingParam` for the
rest of the canvas. That is the question that has been open since the start.

**It does not prove the geometry.** The `CoordinateReference` block in this
sample belongs to the London bounding box in it. Those UTM numbers are not
derived from anything the component recomputes, so if you edit `Top`,
`Bottom`, `Left` and `Right` to your own site and leave the UTM values alone,
the world origin will be wrong even though everything appears to work. Do not
read a successful parse as a correct survey.

## Steps

1. **Check your Urbano sign-in first.** `ProjectSettingComponent.SolveInstance`
   calls `UrbanoAuth.TryLoadToken()` and refuses to run on a missing or expired
   token. That failure looks exactly like the bug this is testing, so rule it
   out before you start.
2. Paste the JSON into a Grasshopper panel.
3. Wire the panel into the Project Setting component's **text** input. The
   component is on the Urbano2 tab, in the "1 Download/Import" panel.
4. Set its boolean input to true.
5. Watch what comes out of it.

## What to look for

- **A `ProjectSettingParam` on the output**: the integration works, and the
  only remaining question is whether mapgen can compute the coordinate
  reference itself.
- **A refusal about authentication**: sign in and try again, this is step 1.
- **A parse error**: the format is wrong after all, and the error text is the
  most valuable thing you can bring back.
- **It tries to download London data**: expected if the files named in the
  JSON do not exist. Urbano skips any layer already on disk and fetches
  anything that is not. Harmless, but it means you are testing against absent
  files rather than a real package.

## The trap that will waste your afternoon

`NaN` or `Infinity` anywhere in the JSON breaks **both** of Urbano's readers.
Its own `ToJson` writes named float literals, but its readers use default
`JsonSerializer` options, which reject them. mapgen writes with
`WriteIndented = true` only and never emits named literals, so mapgen's output
is safe. Hand-edited JSON may not be.

## Why the bridge executable is not needed for this

`ProjectSettingComponent` rebuilds every data path as
`Folder + "\" + FileNameStr + <extension>` and **skips any file already on
disk**. mapgen's naming already matches that exactly. So a mapgen package plus
a correct project setting JSON turns the component into a no-download
pass-through, with no `UrbanoBridge` executable involved.

That decision has now been made, in task 35's direction: mapgen writes the
project setting natively and the bridge is no longer load-bearing for any
part of a package. Three of the bridge's engine steps are US only, and the
traveller model it wants (`URBANO_TravelerClassifier.onnx`) is not on this
machine, so for UK work the smaller shape is the better one.

## If you want the bridge instead

The retarget is four edits in `tools/UrbanoBridge/Program.cs`: find the `.gha`
rather than two DLLs, load one assembly rather than two, rename five types
from `Urbano.Core.Process.*` to `Urbano.Core.Helpers.*`, and delete
`ProjectSetup.Overture`, which no longer exists in 2.2.1.2. Plus
`_MISSING_URBANO_RE` in `src/mapgen/bridge.py`.

Full evidence, including how the assembly was read and why two earlier
attempts reached wrong answers, is in
`.superpowers/sdd/2026-08-01-mapgen-phase1/task-34-report.md`.
