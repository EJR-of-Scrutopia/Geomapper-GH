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
