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
`DownloadUsgs3DepTiffForBounds`, which is United States only. For a UK site
it fails and the component shows an orange warning saying so. That is a
truthful report and it costs the rest of the setting nothing, but it is worth
expecting rather than meeting cold. `climate` is worse and mapgen never
writes it: it is re-fetched with no file check at all.

**Overture is not downloaded at all.** The routine has no Overture branch,
and rewrites `OvertureFilePath` as an empty string. Urbano 2.2.1.2 still
reads an Overture parquet elsewhere (`OVERTURE_KEY_BLDG_HEIGHT` is a real
key) but no longer ships code to fetch one.

One consequence to know about: **the component rewrites the file** when it
runs, with its own recomputed origin and its own path conventions. mapgen's
version is an input to that, not a permanent record. Re-run `mapgen bridge`
over the folder to get mapgen's back.

## Reading the GeoTIFF and the GeoJSON

Not through the project setting. Urbano 2's toolbar has an **Import Geojson
File** component that takes a GeoJSON path directly, which is what makes
mapgen's `<stem>_<type>.geojson` output first-class rather than something
Urbano cannot open, and a separate Import Terrain component for the surface.
The project setting's `ElevationFilePath` and `OvertureFilePath` are there
for those, and for **Deserialize Project Setting**, which unpacks the file
and passes its path strings through verbatim.

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
