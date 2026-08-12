# mapgen

A site survey data tool for architectural work. Draw an extent on a map, name
it, and get back a folder of OpenStreetMap, Overture Maps, elevation and (in
Wales) LiDAR data that Grasshopper and Urbano 2 can read directly.

## What it does

You give it a bounding box, a region name and a site name. It splits the
extent into overlapping tiles and downloads OpenStreetMap tile by tile,
deduplicating the overlap. Overture Maps, and the elevation model if you
have an OpenTopography key, are each fetched in one request over the whole
extent instead. Everything is merged into single per-source files and
written into a dated, named package folder alongside a `survey.json` audit
trail and an Urbano 2 project setting file. A failed or interrupted job
resumes rather than restarting.

Only OpenStreetMap is tiled, and only because the OSM map API has a hard
50,000-node cap per request that tiling exists to stay under. Overture is a
bbox-filtered read of cloud-hosted parquet with no equivalent cap, so
tiling it cost one process launch per tile per type and bought nothing: on
one measured extent, 16 tiled calls took 69 to 93 seconds against 4.5
seconds for the single call that returned the identical 9,910 features.

Overture's eight data types are downloaded at the same time rather than one
after another, which on a 10 by 14 km extent takes the same 168 MB from
around 60 seconds to around 18. OpenStreetMap is deliberately left
sequential: its tiles go to a shared public API with its own rate limits,
and pointing many concurrent requests at it is a good way to get throttled
part way through a survey.

It replaces an earlier script, `osm_overture_tiles.py`, that did the same job
by hand-composed command line. Everything that script did is either here
under a real command, or is called out below as intentionally dropped.

## Setup

Requires Python 3.11 or newer.

```powershell
git clone https://github.com/EJR-of-Scrutopia/Geomapper-GH.git mapgen
cd mapgen
.\bootstrap.ps1
.\.venv\Scripts\Activate.ps1
```

`bootstrap.ps1` creates `.venv` and installs mapgen into it in editable mode,
along with its two pinned dependencies, `requests` and `overturemaps`.
`overturemaps` is a normal Python dependency, not a separate install: pip
puts its `overturemaps` command on `.venv\Scripts` alongside `mapgen` itself,
and mapgen's Overture source calls it as a subprocess. There is nothing
further to install for Overture to work.

The Urbano 2 bridge is a separate, optional piece (see "The Urbano bridge"
below). It needs the .NET SDK to build and a working Urbano 2 install to run
against, and its absence does not stop the rest of mapgen from working.

### The OpenTopography key

The elevation layer (Copernicus DEM, via OpenTopography) needs a free API key
from [portal.opentopography.org](https://portal.opentopography.org). Without
one, every other layer still works; elevation is simply left out, and the web
interface tells you why if you try to select it.

Set it as an environment variable:

```powershell
$env:OPENTOPOGRAPHY_API_KEY = "your-key"
```

or paste it into the OpenTopography field under the Settings button in the
web interface, which saves it to `~/.mapgen/config.json`. The environment
variable always wins if both are set. `OPENTOPO_API_KEY` is accepted as an
older alias for the same variable.

The field itself is `type="password"`, so a saved key and an unsaved one
both show as the same row of dots; a small "Key saved" or "No key saved"
label next to it says which is actually true, and updates the moment you
save a new key or clear the field. It never shows the key itself, only
whether one is on disk.

### Which elevation model

The DEM is a choice, in Settings or with `--demtype`, and it defaults to
COP30 exactly as it always was. Be clear about what the choice is for,
because it is not what it sounds like:

**Nothing in the list is higher resolution than COP30.** OpenTopography's
global DEM API serves 30 m at best, anywhere, Wales included, and a paid
OpenTopography plan (OT+) buys high resolution LiDAR for North America
rather than sharper data anywhere else. If you came here looking for a
finer DEM for a Welsh site, it is not in this setting; it is NRW LiDAR
through DataMapWales, which is phase 2 work (see the roadmap).

What the choice actually changes is the kind of model:

| Model | Resolution | What it measures |
| --- | --- | --- |
| `COP30` (default) | 30 m | Surface: roofs and tree canopy are in the heights |
| `EU_DTM` | 30 m | Bare earth terrain, Europe including the UK |
| `AW3D30` | 30 m | Surface (JAXA ALOS) |
| `NASADEM` | 30 m | Surface (reprocessed SRTM) |
| `SRTMGL1` | 30 m | Surface (SRTM) |
| `COP90` | 90 m | Surface, coarser and quicker |
| `SRTMGL3` | 90 m | Surface, coarser |

For a site section or a ground plane, `EU_DTM` is often the more useful of
these, because a 30 m surface model puts trees and roofs into the terrain.
That, and not sharpness, is the reason this setting exists.

Every other `demtype` OpenTopography's API accepts is still valid on
`--demtype` (the ellipsoidal variants `SRTMGL1_E` and `AW3D30_E`, the
bathymetry grids, `GEDTM30`, and the Canada and South America products),
and none of them is offered in the interface: the ellipsoidal ones differ
from their ordinary siblings by tens of metres vertically with nothing on
screen to say so, the bathymetry grids and `GEDI_L3` are 500 m and 1 km,
and the regional ones do not cover Wales. `survey.json` records the model
each package actually holds, along with that model's own licence and
citation rather than Copernicus' regardless of what was downloaded.

An unrecognised model is refused before anything is downloaded, by the
same construction-time check an unrecognised `--category` goes through, so
the browser and the command line both get "Unknown elevation model" and
the list of valid ones rather than a download that fails halfway.

## Running it

### Browser

```powershell
mapgen ui
```

(or `python -m mapgen ui` if the console script is not yet on `PATH`). This
opens a local page, served on `127.0.0.1` with a per-launch access token in
the URL, so nothing else on the machine can drive it. On the page:

- Draw a rectangle on the map by pressing, dragging and releasing, press
  **Select viewport** to take exactly what the map is currently showing,
  paste a `west,south,east,north` bbox string, or search a place name and
  pick a result. Whichever way you set it, the extent always shows as a
  rectangle. A viewport capture is a snapshot, not a binding: panning or
  zooming afterwards leaves the rectangle where it is, and pressing the
  button again captures the new view.
- However it was set, the rectangle can then be adjusted rather than
  redrawn. It carries four corner handles: drag one to resize, keeping
  the opposite corner where it is, or drag anywhere inside the rectangle
  to move the whole thing. Either way the estimate and the tile grid
  follow when you let go, not while you are dragging, and a drag that
  would leave the box with no area at all is refused and leaves the
  extent as it was. Escape puts back a rectangle you are part way
  through moving.
- The extent is fixed for as long as a download is running, since the run
  has already been told what to fetch and the grid on the map is that
  run's own. The corner handles come off, and Draw extent, Select
  viewport, the bbox box and the place search all go dead rather than
  looking live and doing nothing. Everything comes back when the run ends,
  however it ends: finished, stopped, failed, or the page losing contact
  with it.
- Region and Site fill in on their own: picking a place from the search
  results uses that result's own name, and drawing or pasting a rectangle
  reverse-geocodes its centre. Both stay ordinary editable text fields, and
  neither is ever overwritten once you have typed into it; if one cannot be
  worked out, the estimate panel says which is missing rather than guessing
  from coordinates.
- Tick which layers you want (OpenStreetMap, Overture Maps, elevation,
  Welsh LiDAR where the extent falls inside Wales, and HM Land Registry
  property boundaries where the extent falls in England or Wales) and
  which categories (buildings, roads, broken down into motorway, trunk,
  primary, secondary, residential, service, footpath, cycleway and track,
  water, vegetation and landuse, rail, boundaries, points of interest).
  Categories are ticked by default, matching the everything-selected
  behaviour before this control existed. Category selection reaches
  Overture directly. It reaches OpenStreetMap by switching that download
  to Overpass automatically, since the default OSM map API cannot filter
  by tag at all; ticking everything (the default) keeps the map API,
  today's faster, well-tested path for a whole-area pull. The estimate
  panel and `survey.json` both say which of the two a run actually used
  and why, since Overpass is a separate, shared public service with its
  own rate limits (see "Limits worth knowing about"). At least one
  category is required: unticking the last one disables Download with a
  plain reason, the same way a missing site name already does, rather
  than letting you press it and get an error back.
- Where the download lands sits in the main form, directly above the
  Download button: a text field holding the folder everything is saved
  under, and a Browse button beneath it. Above both, once a region and
  site are filled in, is the exact folder this run will create, dated and
  named, composed by the same code that creates it. The field holds the
  root; the line above holds the whole path. There is one such field on
  the page and not a copy of one, so the path you type there is the path
  the download uses.
- Browse opens a real Windows folder dialog and writes the chosen path
  into the field. A browser cannot hand a page a filesystem path, so the
  dialog is opened by the local server, in a short-lived child process;
  the text field is unchanged and still does the whole job on its own.
  Cancelling, a dialog left open for two minutes, and a machine with no
  picker at all each leave the field exactly as it was and say so
  underneath it. If the dialog does not appear, look behind the browser
  window: it is a native window and can open behind a maximised one.
- Tile size and overlap live behind the Settings button: defaults you set
  once, not per-survey choices. An API key field appears there per data
  source that needs one (OpenTopography's, for the elevation layer),
  driven by the same source registry the layer list comes from, so a
  second or third keyed source in a later phase needs no new panel. The
  elevation model select is built from that same registry (see "Which
  elevation model"), and it says on the page that nothing in it is
  sharper than the default. An Appearance setting there too: Match system
  (the default), Light or Dark, applied to the whole page and to the tile
  grid below.
- Tile size is a slider, and it says what each size costs for the extent
  currently drawn: the tile count and the estimated time, re-estimated
  from the server a moment after you stop moving it rather than on every
  step of a drag. It does not show a failure risk, and that is a refusal
  rather than an omission. Risk would depend on how dense the
  OpenStreetMap data is on that particular ground, which nothing here can
  know before downloading it, and a percentage or a traffic light would
  look measured while being invented. What it shows instead is the real
  trade, in a sentence that changes with the slider: larger tiles mean
  fewer requests and are quicker on sparse ground, but more of them need
  splitting on dense ground, and each split costs the requests its pieces
  take. Splitting is not a failure (see the tile grid below), so there is
  nothing else honest to warn about. The slider keeps the 500 m minimum
  the number field before it had, and a saved size outside its range
  widens the slider rather than being quietly rewritten to fit.
- Above the Download button you get the extent in kilometres, tile count,
  and an estimated download size and duration, computed before anything is
  fetched, so a mis-drawn box over the wrong country is obvious immediately.
  The same request also draws the actual tiling as rectangles on the map,
  from the server's own tile geometry rather than a client-side guess, so
  what you see is the grid the pipeline is really about to use.
- The log sits under the map, at the foot of the map's own column, so the
  form pane beside it runs the full height of the window. The line
  directly above it is a divider: drag it up to give the log more height
  and the map less, down for the reverse. Neither can be crushed to
  nothing, and the split you choose is saved with the rest of your
  settings, so it comes back on the next launch. One saved on a large
  monitor is cut down to fit a smaller window rather than opening a page
  with no map on it.
- The download runs with a live per-tile log, and the tile grid shades each
  rectangle as it goes: not started, in progress, done, or failed, the last
  one drawn distinctly since it is the one worth noticing before deciding
  you have enough. Tiles go green one at a time, as each one is finished:
  a tile is done when every layer you selected has finished with that tile,
  which for a layer that downloads the whole extent in one request means
  when that layer lands. A tile too dense for one request is drawn with a
  dashed outline over whatever state it is in, and its tooltip says into
  how many pieces it was split, so a square that has gone quiet explains
  itself rather than looking stuck.
- That grid belongs to the run for as long as the run lasts. Changing the
  tile size, the overlap or the layer selection while a download is going
  still re-estimates, and the panel above Download still answers with what
  the new tiling would cost, but the rectangles on the map are left
  reporting on the download in progress rather than being redrawn for a
  tiling nothing is fetching. The new tiling is drawn the next time you
  ask for an estimate after the run ends. Download itself stays out of
  reach until then, since starting a second run is what would clear the
  log of the first.
- A red tile says why it is red. Hover one, or tap it on a touchscreen, and
  the reason the run actually recorded appears on the tile: the service
  timed out, or answered 429, or answered 503 four times over, and whether
  it was retried and failed again. The same reasons are listed under the
  Download button as well, so the account is there without having to know
  to hover anything, and clicking a red tile marks its entry in that list,
  which is how you tell one rectangle from seventy-one others. The
  sentences are the ones `survey.json` records under `tile_failures` and
  the ones the command line prints, composed in one place, so the page, the
  file and the terminal cannot tell you three different stories about the
  same run. A tile that failed and then landed on the retry is not a
  failure and stops being red; a tile the verify pass finds on disk after
  all is corrected the same way. A tile that downloaded successfully and
  happened to contain nothing is a success, is not red, and is offered no
  explanation, because it does not need one.
- A progress bar on the strip directly under the map, to the right of the
  tile legend, answers the other question, how much longer. Beside it, one
  short line says what is happening now and which package it is going
  into: the tile being worked and the layer working it (`osm r02_c05`),
  or the layer's own name while a layer that downloads the whole extent
  at once is running, then checking the files and writing the package,
  followed by the survey folder's own name. It is derived from the same
  progress events the grid
  is, weighted by what each layer's own estimate says it costs, so a
  finished OpenStreetMap pass reads as the large majority of the run it
  actually is. The countdown starts from the estimate and switches to the
  run's own measured rate once there is enough finished work to measure.
  Nothing is reported to the second, because nothing here is accurate to
  the second. A run that
  passes its estimate says so rather than sitting at zero, a tile that
  needed splitting is called out as having made the run longer, and a
  stopped run settles at the percentage it actually reached rather than
  jumping to complete. A Stop button ends the download: it does not discard
  what has already been fetched. A tile already being downloaded is
  finished and kept, whatever source is running next never starts,
  everything fetched so far is merged into the package exactly as a
  finished run would, and `survey.json` records the package as stopped, not
  complete and not failed, so the folder and its own audit trail always
  agree about how far the download actually got. Re-running the same
  extent afterwards resumes rather than restarts, picking up from the
  tiles the stop left unfetched, the same as it always has for an
  interrupted or failed run. There is no separate Pause: stopping already
  keeps everything, and resuming already continues from where it stopped,
  so a second button promising the same outcome would only be one more
  thing to explain. One honest caveat about how long Stop takes to land:
  Overture's eight types download together, so a stop during that step
  waits for all eight rather than for one, and if they were already all
  running it will finish the whole Overture step before it stops. That is
  the same "an in-progress download is finished and kept" rule as
  everywhere else, and the step it applies to is now around 18 seconds
  long instead of around 60, so in practice Stop lands sooner than it did.
- A Stop server button ends the session immediately from the page itself,
  useful under a terminal launch too, not only the windowless one below.

Only one download runs at a time; starting a second while one is in progress
is refused with a clear message rather than run concurrently, because
simultaneous Overpass requests from one machine are how you get rate limited.

#### Desktop shortcut, windowless

```powershell
.\.venv\Scripts\Activate.ps1
.\assets\make_shortcut.ps1
```

Creates (or refreshes) a `mapgen.lnk` on the Desktop that launches
`pythonw.exe -m mapgen ui --windowless`: no console window, and the server
stops itself once the page is genuinely gone.

Genuinely gone is decided from three signals, not one. Closing the tab
reports itself immediately, so a real close does not wait. Returning to the
page from another window pings at once. Failing both, 90 seconds of complete
silence is taken as the page having gone away. That last number is high on
purpose: browsers throttle background tabs to roughly one timer tick a
minute, so a shorter timeout means "you looked at another window for half a
minute", not "the page is closed". A download in progress holds the server
open regardless of any of this, because killing a job someone is waiting for
is worse than leaving a process running. The Stop server button on the page
ends it immediately either way, so a crashed browser does not leave a
process behind waiting out the full grace period.
Safe to re-run any time: it overwrites the existing shortcut rather than
needing to be deleted first, which is what makes the shortcut itself safe to
regenerate instead of hand-edited.

`mapgen ui` typed into a terminal is completely unaffected by any of this:
console, output, and running until Ctrl+C regardless of the browser tab, all
exactly as before. `--windowless` is what changes the behaviour, and only the
shortcut passes it.

With no console, `mapgen ui --windowless` has nowhere to print to: Python
itself sets `sys.stdout`/`sys.stderr` to `None` under `pythonw.exe`, and an
ordinary `print()` on `None` crashes outright. Output is redirected to
`~/.mapgen/ui.log` instead, and a failure that stops the server before it can
even open the page also raises a native Windows message box pointing at that
log, so a windowless launch that cannot start is not a silent one.

### Command line

The real command used to verify this tool, kept here because it is a working
example, not an invented one:

```powershell
mapgen survey `
  --bbox=-3.29,51.38,-3.28,51.39 `
  --region "South Wales" `
  --site "Barry Waterfront" `
  --tile-size-m 1000 `
  --overlap-m 50 `
  --source osm --source overture `
  --overture-type building --overture-type water
```

`--source` is repeatable and defaults to `osm` and `overture` if omitted.
`--overture-type` is repeatable and defaults to a map-oriented set of eight
types (building, place, segment, connector, infrastructure, land_use,
land_cover, water) if omitted. `--region` and `--site` are always required.

`--category` is repeatable and defaults to every category if omitted; run
`mapgen categories` for the full list of ids, including the nine road
subtypes. An id outside that list, `building` for the real id `buildings`
being the typo actually seen in practice, is rejected outright with the
list of valid ids, not silently treated as an empty or partial selection:
a category selection that reaches neither a real filter nor an error is
worse than either. It reaches Overture's own type selection (mapped from
category to type; `--overture-type`, if also given, wins outright over
`--category` for Overture specifically, since they are two ways of
choosing the same thing). Deselecting every category that would map to
an Overture type at all, `--category rail` alone for example, correctly
fetches nothing from Overture rather than falling back to the full
default set: an empty selection and an unspecified one are treated as
two different things throughout.

Passing no `--category` at all still means every category, exactly as
above. An explicitly empty category selection, which the CLI's own
`--category` flag cannot produce (it always takes a value when given)
but the browser's checklist can, by unticking every box, is a different,
refused case: it matches nothing in either OSM's tag filter or Overture's
type mapping, and there is no real survey this tool serves where that is
a useful, deliberate answer, unlike `--category rail` above, which still
narrows to a real (if Overture-empty) filter. Refused with a plain
message that nothing is selected and at least one category is needed,
through the same `SurveyRequest` construction that an unknown id already
goes through, so a Python caller building a request directly gets the
same rejection the browser does.

For OpenStreetMap, a genuine restriction switches the download to Overpass
automatically, since the default OSM map API has no server-side filtering
at all and cannot honour one. Ticking or passing every category (the
default) keeps the map API, today's behaviour and the faster path for a
whole-area pull. This switch is automatic, not a separate flag: `mapgen
estimate` and `survey.json`'s per-source `routing_note` both say which
endpoint a run actually used and why, since Overpass is a distinct, shared
public service with its own rate limits, separate from the map API's.
`use_overpass=True` is also still available directly to Python callers as
a standing, whole-run choice independent of category selection; see
`OsmSource.configure` if calling this from Python rather than the CLI.

Other commands:

```powershell
# Same arguments as survey, but reports tiles, size and duration without downloading.
mapgen estimate --bbox=-3.29,51.38,-3.28,51.39 --region "South Wales" --site "Barry Waterfront"

# Runs the Urbano bridge, and only the bridge, over a package you already have.
mapgen bridge "C:\Surveys\South-Wales\2026-08-01_Barry-Waterfront"

# Lists every registered data source and its licence.
mapgen sources

# Lists every --category id, including the nine road subtypes.
mapgen categories
```

`mapgen bridge` gives a package that already exists its
`<stem>_project_setting.json`, without a byte being downloaded again. Any
package produced before mapgen started writing that file itself has none, so
this is how those are brought up to date. It attempts the C# bridge too, and
exits on whether the project setting was produced rather than on whether that
step worked, because the project setting is what the command is for.

It reads the package's own `survey.json` for the extent, the file stem and
which layers the package holds, and re-derives none of that from the folder
name. It refuses, in one plain line, if the folder is not there, if there is
no `survey.json` in it (that package needs the survey running again, which
resumes from whatever is already on disk, not this), if the `survey.json`
cannot be read, or if a merged file the bridge needs is missing from the
package root, naming the file. One exception, for the case you will actually
meet: a package whose `<stem>.tif` is missing **and** whose `tiles` record
every elevation tile as `failed` is bridged without elevation, exactly as the
download itself would have. That is the package positively stating the layer
was attempted and did not arrive, which is different from a file having been
moved or deleted, or the wrong folder being passed, all of which are still
refused. Elevation is the only layer with an API key, so it is the only one
that fails this way routinely. Nothing but the `bridge`, `elevation_grid` and
`project_setting` blocks of `survey.json` is written: `complete` and `stopped`
describe the download, which this command was not present for. It exits 0 when the package
has its project setting and 1 when it could not be written, unlike `mapgen
survey`, which exits 0 either way because it still delivered its data.

`mapgen survey` exits 0 on a complete package and 1 if any tile failed
(`--force` continues past a failed tile instead of stopping, and marks the
package incomplete in `survey.json` rather than pretending it finished).
Either way it prints which tiles it could not get, for which layer, and why,
and every run now attempts every tile: a tile that fails no longer ends its
layer, and one whose failure a retry could plausibly fix (a timeout, a rate
limit, a 5xx) is tried once more later in the same run, after the rest of the
extent has been fetched. Without `--force` a run that is still short when
that is done exits 1 with the same account, and `survey.json` is written
before it does, so the folder explains itself.

That retry reaches every layer, not just OpenStreetMap. A DEM that timed out
is asked again; an Overture type whose download died part way through is
asked again, and only that type, since the ones that landed are already on
disk. What is never asked again is a request that will be refused
identically: a rejected or missing OpenTopography key, a DEM model that does
not exist, or an `overturemaps` invocation the command itself would not
accept. Retrying those spends your own quota to be told the same thing and
delays the honest error. A service that answers with a `Retry-After` gets
the period it asked for, up to a minute; past that the retry is not made at
all, because a survey you are watching should not silently stop for an hour,
and Stop interrupts the wait the instant you press it. A run that completed
only because a retry worked says so, on the terminal in one line and in
`survey.json` under `retries`, so you can tell afterwards that a package
which finished was nevertheless fragile.
`plan`, `download`, `merge` and `urbano-package` still exist as command
names, for muscle memory, but they are aliases for `survey`/`estimate` with
the current flag set, not the old script's flags: `--output-dir`, `--tile-id`
and `--max-tiles` are gone, and an old invocation using them fails with a
plain argument error rather than doing something subtly different.

## What a survey folder contains

A survey of "Barry Waterfront" in region "South Wales" produces:

```text
<output root>/South-Wales/2026-08-03_Barry-Waterfront/
  Barry-Waterfront_2026-08-03.osm                    merged OpenStreetMap XML
  Barry-Waterfront_2026-08-03_building.geojson        one file per Overture type
  Barry-Waterfront_2026-08-03_water.geojson           ever fetched into this package
  Barry-Waterfront_2026-08-03.tif                     elevation, only if selected
  Barry-Waterfront_2026-08-03_lidar_dtm.tif           Welsh 1 m terrain, only if
  Barry-Waterfront_2026-08-03_lidar_dsm.tif           `lidar_wales` was selected
  Barry-Waterfront_2026-08-03_contours_5m.geojson     generated from that DTM,
  Barry-Waterfront_2026-08-03_contours_1m.geojson     one file per interval the
  Barry-Waterfront_2026-08-03_contours_0.5m.geojson   extent's own area qualifies
  Barry-Waterfront_2026-08-03_contours_0.25m.geojson  for
  Barry-Waterfront_2026-08-03_roof_massing.geojson    eaves polygons and gable
                                                      ridge lines, only if the
                                                      package's own LiDAR was
                                                      at the 1 m level
  Barry-Waterfront_2026-08-03_canopy.geojson          canopy points, only if
                                                      the LiDAR found above-
                                                      ground clusters
  Barry-Waterfront_2026-08-03_lidar25_dsm.tif         Creigiau/Pentyrch 25 cm,
  Barry-Waterfront_2026-08-03_lidar25_dtm.tif         only if `lidar_cardiff`
                                                      was selected and the
                                                      extent falls inside the
                                                      ten-tile block
  Barry-Waterfront_2026-08-03_boundaries.geojson      HM Land Registry property
                                                      boundary curves, only if
                                                      `inspire` was selected
  Barry-Waterfront_2026-08-03_parcels.geojson         closed parcel rings behind
                                                      those curves, same `inspire`
                                                      gate
  Barry-Waterfront_2026-08-03.egrid                   the same DEM, in the format
                                                      Urbano reads terrain from
  Barry-Waterfront_2026-08-03_project_setting.json    point Urbano 2 here,
                                                      written every run
  survey.json                                         audit trail, read this first
  layers/
    water.geojson                                     friendly-named copies of
    vegetation.geojson                                three specific Overture types,
    landuse.geojson                                   for direct use outside Urbano
```

A real run over a small extent of Barry Waterfront's docks (`osm` only)
produced 3375 nodes across 69 ways, including 19 tagged buildings, landing
correctly around 51.385 N, 3.28 W. Coordinate ranges in the merged file
usually extend a little beyond the bbox you asked for: the OSM map API
returns whole ways, not clipped fragments, so a way that crosses your
boundary comes back complete. That is correct behaviour, not a bug.

Each file:

- **`<stem>.osm`**: every OSM node, way and relation in the extent, tiles
  merged and deduplicated by id, highest version wins at a seam. This is the
  file the Urbano bridge reads for OSM geometry. **It is absent when the
  extent genuinely contained nothing**, over open water for instance, or
  under a category selection nothing on that ground matches: mapgen writes
  no empty file to stand in for data that was never there, and `survey.json`
  says so through an empty `merged_files` and a `features_merged` of zero.
  A tile that came back empty is still an `ok` tile; empty and failed are
  different things throughout.
- **`<stem>_<type>.geojson`**: one GeoJSON FeatureCollection per Overture
  type the completed request asked for, each fetched in a single request
  over the whole extent, so there are no tile seams in it to deduplicate.
  A type that returned no features over this extent gets no file, for the
  same reason `<stem>.osm` can be absent.
  A complete run also sweeps
  away any merged file an earlier, differently-scoped attempt left in the
  root, so the folder matches `survey.json` rather than accumulating types
  the current request never asked for. The sweep only ever touches names
  mapgen itself could have written for this exact survey stem: your own
  files in the folder are never candidates, whatever they are called. An
  incomplete run (`complete: false`) keeps everything it finds until a
  resume finishes the job.
- **`layers/water.geojson`, `layers/vegetation.geojson`, `layers/landuse.geojson`**:
  copies of the Overture `water`, `land_cover` and `land_use` types
  specifically, under names meant to be read straight into Grasshopper
  without knowing which Overture type produced them. Only these three are
  copied here; every other requested type is still available as the
  `<stem>_<type>.geojson` file above.
- **`<stem>.tif`**: the whole-area DEM GeoTIFF, if an elevation source was
  selected and an OpenTopography key was available. Which model it is comes
  from the `--demtype` setting (COP30 by default) and is recorded in
  `survey.json`.
- **`<stem>_lidar_dtm.tif`, `<stem>_lidar_dsm.tif`**: the Welsh Government's
  own 1 m bare-earth terrain (DTM) and surface (DSM) models, packaged
  whenever the `lidar_wales` source is selected and the extent falls inside
  Wales. `<stem>.egrid` reads the DTM ahead of the 30 m DEM (see below);
  `lidar_heights` (further down) reads both, to give flat OSM buildings a
  real `height`.
- **`<stem>_contours_5m.geojson`, `<stem>_contours_1m.geojson`,
  `<stem>_contours_0.5m.geojson`, `<stem>_contours_0.25m.geojson`**:
  generated, not downloaded, from the packaged `_lidar_dtm.tif` at the
  moment it is packaged, one file per interval the extent's own area
  qualifies for (5 m always, 1 m under 6 sq km, 0.5 m and 0.25 m under
  1.5 sq km); an interval the extent does not qualify for gets no file.
  WGS84 GeoJSON, each feature carrying `elevation`, `interval_m`,
  `source`, `source_resolution_m` and `interpolated` (`true` below 1 m,
  since a 1 m raster cannot really resolve a quarter-metre rise, so those
  two files are honest about being smoothed rather than measured).
- **`<stem>_roof_massing.geojson`**: one eaves Polygon per building
  `roof_forms` (below) classified, `[lon, lat, z]` with `z` metres above
  that building's own ground, plus one ridge LineString per classified
  `gable` (the only shape with two significant planes to intersect;
  `mono` has a direction but only one plane, `flat` and `complex` have
  neither). Polygon properties: `building`, `shape`, `direction`,
  `eaves`, `ridge`, `quality`, `ground_m`, `source`, `note`; ridge
  properties: `building`, `feature` (`"ridge"`), `source`, `note`.
  Packaged whenever `lidar_wales` is selected, the package's own rasters
  are at the 1 m LiDAR level, and at least one building classified; any
  of those failing writes no file at all.
- **`<stem>_canopy.geojson`**: one Point per cluster of above-ground
  LiDAR return outside every building footprint, `height` the p90 of the
  cluster in metres, plus `crown_radius`, `resolution`, `source` and
  `note`. Packaged whenever `lidar_wales` is selected and at least one
  cluster qualified; unlike roof massing, it runs at whatever resolution
  the package's rasters carry, not only at 1 m, so a package too coarse
  for roof tags can still hold this file.

  Roof fitting only runs on 1 m LiDAR: a plane fit needs enough samples
  per face to tell one roof plane from noise, and a package whose own
  rasters are coarser gets no roof tags at all, `roof_forms.skipped_reason`
  (below) naming why. Where it does run, a fitted plane reads sharper than
  the pixels it came from, but it will not resolve a conservatory or
  anything else the DSM itself cannot see. The shipped tag vocabulary is
  `gable`, `flat`, `mono` and `complex`: a fifth class, `hip`, was tried
  against a real Welsh town's 1 m LiDAR and dropped, because at 1 m the
  DSM cannot tell a hip roof from a cross-gable and the same roof read as
  hip, gable, complex or flat depending only on which way it faced and
  which noise it drew. A
  building whose evidence is too thin, or whose fitted ridge sits under
  2.0 m of its own ground (the same floor a building's `height` tag is
  already refused under, elsewhere in this file: a slab is not a
  building), gets no roof tags at all rather than a guess.
  `<stem>_canopy.geojson` is exactly what its own `note` property says,
  `vegetation and other above-ground features, derived from LiDAR,
  indicative`: a pylon or a crane clears
  the same 3 m floor a tree does and a raster cannot tell them apart, so
  read it as an above-ground survey, never a species one.
- **`<stem>_lidar25_dsm.tif`, `<stem>_lidar25_dtm.tif`**: 25 cm terrain
  (surface and bare-earth) from Natural Resources Wales' 2011 historic
  archive, packaged whenever the `lidar_cardiff` source is selected and
  the extent falls inside the ten quarter-tiles it covers: Creigiau and
  Pentyrch, north-west Cardiff, about 2.5 km2, the only 25 cm the archive
  holds anywhere near Cardiff. **Not central Cardiff, and not current
  ground**: flown 23 March 2011, fifteen years of change since, so
  buildings and ground both may differ from what stands there today.
  Extents under about 600 x 600 m inside the block come back at 25 cm
  (the raster budget itself is a 1024 m padded window, but a 200 m pad
  on every side eats into it, leaving about 624 m of raw, drawable
  extent; 600 rounds that down, never up); a larger covered extent is
  refused outright, with the pixel count and the reason recorded rather
  than silently downsampled. Terrain only:
  fused building heights, roof forms, canopy points, `<stem>.egrid` and
  every contour file above stay on the 2020-2023 1 m data
  (`lidar_wales`) regardless of whether `lidar_cardiff` is also
  selected, so a package never understates the vintage of half of what
  it holds by dressing a 2011 archive's own terrain up with a newer
  flight's derived products.
- **`<stem>_boundaries.geojson`**: HM Land Registry's INSPIRE Index
  Polygon parcels for whichever local authority (or authorities, at a
  border) the extent falls into, packaged whenever `inspire` is selected
  and the extent is inside England or Wales. Not the parcels themselves:
  shared edges between neighbouring parcels collapse to one line and
  chains break at junctions, so this is a boundary drawing, a
  FeatureCollection of LineStrings, never a cadastral database of closed,
  tagged areas. Every feature's own `note` property repeats HM Land
  Registry's own caveat verbatim: **"The extent of the land contained in
  any registered title cannot be established from the INSPIRE Index
  Polygons."** So does every `boundary=property` way this file is fused
  into `<stem>.osm` as (see below). The injected ways carry negative,
  descending ids, the ordinary OSM-editor convention for synthetic data
  that was never uploaded, and are idempotent: a re-run (including
  `mapgen bridge`) recognises its own prior fusion by the `source=
  hm_land_registry` tag and injects nothing twice. `inspire_boundaries`
  (below) reports what a run actually fused.
- **`<stem>_parcels.geojson`**: the closed parcel rings `<stem>_boundaries.geojson`'s
  own curves are deduplicated from, one Polygon per INSPIRE parcel (WGS84,
  exterior ring only), packaged alongside it under the same `inspire` gate.
  Every parcel's own full ring is here undeduplicated, shared edges and all,
  which is what `boundaries_categories` (below) classifies parcel by parcel
  before handing each category's own group to the same curve-deduplication
  machinery. Same `{"source", "note", "year"}` properties as
  `<stem>_boundaries.geojson`, per feature.
- **`<stem>.egrid`**: a DEM converted into Urbano's own elevation grid,
  which is the only format any Urbano component reads terrain in. When a
  package also holds `<stem>_lidar_dtm.tif` (the `lidar_wales` source's own
  1 m Welsh Government/Natural Resources Wales terrain model), that DTM
  answers first and the 30 m `<stem>.tif` fills in only where the DTM has
  no coverage at all, over the sea and beyond the LiDAR mosaic's own edge;
  `survey.json`'s `elevation_grid.source` says which of the two, or both,
  actually answered. Derived output, like the `layers/` copies: the `.tif` is the
  original and stays. Written on every run and by `mapgen bridge`, so a
  package downloaded before this existed gains terrain without being
  downloaded again. If no DEM can be converted the package is finished
  without it and `survey.json`'s `elevation_grid.error` says which part
  of the file was refused. See `docs/urbano/README.md` for what is in it
  and why it is built the way Urbano builds its own.
- **`<stem>_project_setting.json`**: the single file to point Urbano 2 at.
  Written by mapgen itself on every run, whether or not the Urbano bridge
  ran, succeeded or was skipped. It carries the extent, the UTM world origin
  and an absolute path to every layer the package actually holds, and it
  names only files that are genuinely there: a package with no DEM has an
  empty `ElevationFilePath` and no `elevation` in its `Layers`. The one case
  where it is absent is a package holding nothing Urbano can read at all, and
  then `survey.json`'s `project_setting.error` says so in a sentence.
- **`survey.json`**: see below.
- **`_work/`**: the in-progress scratch folder, keyed by a fingerprint of the
  exact bbox, tile size, overlap, category selection and Overture type
  selection. It is only present while a job is incomplete, or if
  `--keep-work` was passed. A clean, complete run removes it automatically.
  Re-running the same survey with the same tiling AND the same content
  selection resumes from whatever is already in here rather than
  restarting; changing either gets a fresh, separate scratch folder of its
  own, so raw tiles fetched under an old selection are never mistaken for
  the new one's.

### survey.json

The audit trail. If this data ends up informing real project work, being able
to say where every polygon came from, under what licence, is not optional.
Fields, as actually written:

| Field | Meaning |
| --- | --- |
| `schema_version` | Currently `1`. Bump only on a breaking change to this shape. |
| `tool_version` | The mapgen version that produced this package. |
| `site`, `region` | Exactly as typed. |
| `slug.site`, `slug.region` | The versions used to build the folder path. |
| `date` | The survey date, ISO format. Defaults to today, overridable with `--date`. |
| `urbano_stem` | The file stem used for every named output in this package. |
| `bbox` | The requested extent, `west`/`south`/`east`/`north`. |
| `extent_km` | Width and height of that bbox in kilometres. |
| `tiling` | `tile_size_m` and `overlap_m` actually used, and the resulting `rows`/`cols`. Always the tiling that was requested: a tile too dense for one request is split inside its own tile (see "Limits worth knowing about") and the plan itself never changes. |
| `categories` | The resolved category selection: every id in `mapgen categories` if none was specified, otherwise exactly what was asked for. |
| `sources` | One entry per requested source: `id`, `licence`, `attribution`, `merged_files` (the files this layer actually left in the package root, empty if it left none), `features_merged` (how many features or OSM elements went into them, absent for elevation, whose output is a raster rather than a feature collection), `endpoints_used` (which real URLs were actually contacted this run; empty if every tile was already on disk from an earlier run, since a skipped tile has no endpoint to record), for Overture `types` (the actual Overture types fetched), and for elevation `demtype` (the DEM model the package actually holds, with `licence` and `attribution` beside it being that model's own). For OpenStreetMap, `routing_note` names which endpoint (map API or Overpass) this run used and why, present only when there is something to say (absent for the default map API run, since that is not a deviation worth flagging). For `inspire`, `conditions_url` (the HM Land Registry conditions page both attribution statements below point at) is always added, and `attribution`'s two `[year]` placeholders are substituted with the real publication year read off this package's own `<stem>_boundaries.geojson` the moment that file has at least one feature to read a year from; a package whose boundaries file is empty (an authority with zero kept parcels in this extent) keeps the placeholder rather than guessing at a year. For `os_open` and `os_uprn`, `products` names the exact OS Data Hub version (`"2026-04"`-shaped) each product this run actually fetched was sharded under, and `attribution`'s own `[year]` placeholder is substituted with the LATEST of those products' own years (a survey straddling an OS monthly re-publication can legitimately use more than one version at once; `products` is what keeps that honest rather than hidden behind a single averaged-away year); a run where the source never actually completed a product keeps the placeholder, the same "nothing to substitute honestly" rule `inspire`'s own empty-boundaries case follows. |
| `resolution` | One entry per category with at least one covering, serving source for this extent, `{"category", "sources"}`; a category with no covering source at all is omitted, never included empty. Each inner entry is `{"id", "display_name", "tier", "coverage", "role"}`: `tier` is that source's own rank for this category (1 is the best per-feature quality), `coverage` is `"full"`/`"partial"`/`"none"` for how much of THIS extent the source actually reaches, and `role` is `"base"` (the best tier actually present here for this category), `"fill"` (a covering source at a worse tier, kept as backup), or `"reference"` (OS Open's roads and rail specifically, which are never fused into `<stem>.osm` and are read separately, filtered in Urbano's own GeoJSON import, regardless of tier). Each entry also gains an optional `detail`, a short human sentence naming the quality that source delivers at this exact extent (Wales LiDAR's is computed from the raster level the download would actually use; every other source's is a fixed sentence), present only when that source implements it; the estimate panel's own tier list shows only the base entry's (`sources[0]`), appended after its name. Sorted by tier then source id within each category, matching the estimate panel's own tier list shown before Download. |
| `tiles` | One entry per tile, `tile_id` plus an `"ok"`/`"failed"`/`"pending"` status per source. Why a tile is `"failed"` is in `tile_failures` below, not here, so this row keeps the shape every earlier package already has. `"pending"` means the source never got a turn on that tile at all, most often because a Stop request landed first; it is a different, more honest claim than `"failed"`, which means a real attempt came up short. |
| `tile_failures` | One entry per tile that is still missing when the run ends: `source`, `tile_id`, `kind`, `reason` and `retried`. Empty on an ordinary run. `reason` is a plain sentence written for a person, never a traceback and never a URL. `kind` is the fixed vocabulary the retry decision is made on: `timeout`, `unreachable`, `rate_limited` and `service_error` are retried once, automatically, later in the same run; `not_authorised`, `refused`, `node_cap`, `no_output` and `unknown` are not, because asking again gets the same answer. `retried` says whether that second attempt happened. Every layer produces these, not just OpenStreetMap: elevation and Overture make one whole-extent request each, so a failure of theirs is recorded against every planned tile with the same reason, and the terminal says it once naming the layer rather than once per tile. **A layer that found nothing is not in here.** An extent with no buildings in it produces no file and no failure; the empty `merged_files` in its `sources` entry is what explains that. |
| `retries` | One entry per tile this run had to ask for a second time, whatever the answer was: `source`, `tile_id`, `pass_number`, `kind`, `reason`, `whole_layer` and `recovered`. Empty on a run that never stumbled. This is what tells a package that completed first time from one that completed only because a retry worked, and it exists because `tile_failures` cannot say: a tile the retry recovered is deliberately dropped from that list, so without this a fragile run and a clean one read identically afterwards. `recovered: true` is the interesting value rather than the alarming one, and the terminal prints one line for it. `recovered: false` deliberately duplicates a `tile_failures` entry, because the same fact is worth having in both the "what is missing" and the "what was fragile" reading. `whole_layer` is the same fact `tile_failures` carries above about elevation and Overture, on the recovery side: those two ask for the whole extent in one request, so one download stumbling and then working produces an entry per planned tile, and `whole_layer: true` is how the file says those entries are one event rather than seventy-two. The entries stay per tile because only the run's final verdict can say which tiles a single request actually delivered, and the terminal names the layer once instead of counting them. |
| `verified` | What the end-of-run check found: `checked`, `ok`, `failed`, `pending`, and `corrections`. Every planned tile of every fetched layer is reconciled against what is genuinely on disk before the run is declared finished. `corrections` is normally empty, and when it is not it is the important part: it lists tiles this run had recorded as downloaded that had no file behind them, and which have been put right rather than left to be believed. |
| `complete` | `true` only if every requested source downloaded and merged every tile successfully. Says nothing about the Urbano bridge, which is a separate concern, recorded next. |
| `stopped` | `true` only when a Stop request is the reason `complete` is `false`, never for an ordinary tile failure and never alongside `complete: true`. Distinguishes the two ways a package can be short: `complete: false, stopped: true` is exactly as far as you asked it to go and is safe to hand to Grasshopper as is; `complete: false, stopped: false` means something failed. Re-running the same extent resumes either way. A Stop that lands after every tile has already finished leaves `complete: true, stopped: false`: nothing about the data is short, so this field has nothing to report and the run is reported as done. The only trace such a run leaves is `bridge.attempted: false`, below; the project setting is still written, so nothing about handing the folder to Grasshopper is affected, and `mapgen bridge` is how you get the bridge's own extras later without downloading the extent again. |
| `bridge` | `attempted`, `ok` and `error`: whether the Urbano bridge ran, whether it succeeded, and a plain sentence if not. `ok` is `null` if the bridge step was skipped entirely, which any run you stopped does, on purpose: `attempted` is `false` and `ok` is `null` the same as `--skip-bridge`, since starting another external process after a Stop request works against stopping promptly. That holds even when the Stop landed too late to cost you any data, so a `complete: true` package with `bridge.attempted: false` and no `--skip-bridge` is a run you stopped right at the end. Two more fields appear only once `mapgen bridge` has been run over the package afterwards, and are described in the row below. |
| `bridge.ran_at`, `bridge.during_download` | Present only after `mapgen bridge <package-dir>` (see "Command line"), which runs the bridge step alone against a package that already exists. `ran_at` is when that later attempt happened, and `attempted`, `ok` and `error` beside it describe **that** attempt rather than the download: the freshest answer to "does this package have Urbano files" is the useful one, and it is where a reader already looks. `during_download` keeps the download's own `attempted`/`ok`/`error` exactly as it wrote them, so a later success never makes the file claim the bridge succeeded during a run where it did not. Running the command a second time updates the first three again and leaves `during_download` alone: it is the original, not the previous. It is `null`, rather than a fabricated `false`, in the one case where the package's record held no readable `bridge` block for it to keep. **No `ran_at` means the block describes the download**, which is every package written before this existed and every ordinary run since. |
| `elevation_grid` | `written`, `file`, `nodes`, `covered`, `error`, `source`, `min_height` and `max_height`: whether this package has its DEM in the format Urbano reads terrain from, what it is called, how many grid points it has and how many of those a DEM could give a height for, a plain sentence if the conversion was refused, which raster actually answered it, and the lowest and highest real height on the grid. `written: false` with a null `error` means this package simply has no DEM, which is a different statement from one that could not be converted. `covered` is well under `nodes` on any coastal survey and is not a fault: the grid covers the extent plus 200 m on every side, Urbano's own margin, and a DEM does not reach that far. `source` is `"lidar_wales+opentopography"` when a package's 1 m Welsh LiDAR DTM answered first and the 30 m OpenTopography DEM filled in beyond its edge, `"lidar_wales"` when the LiDAR DTM answered alone (no OpenTopography DEM in the package), `"opentopography"` for the DEM alone, exactly as every package before this existed, and `null` on every branch that wrote nothing at all. `min_height`/`max_height` are the min and max over the grid's own covered nodes, rounded to 2 decimals, so Grasshopper labels can anchor to the terrain's real values instead of the flattened elevations Urbano's GeoJSON import leaves contours with; both are `null` on exactly the branches `covered` is `null`, since a grid with zero covered nodes is refused before it is ever written (`_NoCoverageError`, `egrid.py`), never returned as a `written: true` record with nothing real on it. |
| `project_setting` | `written`, `file`, `layers` and `error`: whether this package has the one file Urbano 2 is pointed at, what it is called, which layers it names, and a plain sentence if it could not be written. mapgen writes this file itself, so it is a different question from `bridge` above and is answered separately from it: a run whose bridge failed, a `--skip-bridge` run and a run you stopped all still have one. `layers` is what the file actually names, read off what is genuinely in the folder, so it can be shorter than the `sources` list if a layer found nothing. |
| `buildings_fusion` | `written`, `from_overture`, `from_os`, `kept_existing`, `skipped_overlap` and `error`: whether this package's `<stem>.osm` had missing building footprints injected from Overture's `<stem>_building.geojson` and OS OpenMap Local's `<stem>_os_buildings.geojson`, Overture checked first. `written` is how many new ways this run actually added; `from_overture`/`from_os` split that same total by which candidate file each one came from. `kept_existing` is every building already in the file before this run touched it, whether from the original download or an earlier fusion, left alone. `skipped_overlap` is every candidate footprint this run looked at and did not write, for either of two reasons folded into one count: it duplicated something already accepted, or it could not be trusted as a shape at all (an invalid or too-small ring); nothing here is invented for a footprint this project cannot vouch for. A package with neither candidate file, or with nothing left for either to add, is `written: 0` with a null `error`, the ordinary and expected outcome (the owner's own default today, with `os_open` unselected), not a reader-visible failure; only a non-null `error` means the step genuinely could not finish. Runs before `lidar_heights`, on every run and on `mapgen bridge`, so an injected footprint with no height of its own is already in the file by the time heights fusion runs immediately after it. |
| `boundaries_categories` | `parcels`, `counts`, `samples`, `capped`, `note` and `error`: whether this package's INSPIRE parcels (`<stem>_parcels.geojson`) were classified by overlay against the package's own data and written to `<stem>_boundaries_categorised.geojson`. `parcels` is how many parcels this run classified. `counts` is `{category: count}` for every category with at least one parcel, drawn from the fixed vocabulary (`housing`, `garden`, `field`, `recreation`, `retail`, `industrial`, `education`, `religious`, `allotments`, `water`, `greenspace`, `woodland`, `unclassified`); a category with none in this package is simply absent from the map, never a zero entry. `samples` is the total interior sample points tested across every parcel, and `capped` is how many parcels hit the per-parcel sample ceiling, a signal that one parcel's own classification is a coarser approximation than usual, not a wrong one. `note` is always the fixed sentence `derived from map overlay, indicative`, the same caveat wherever a category appears in this package, because a parcel's category comes from sampling overlay data already in the package, never from an authoritative land-use register. `parcels: 0`, `counts: {}`, `samples: 0`, `capped: 0` and `error: null` together mean this package simply has no parcels to classify (`inspire` not selected, selected and holding none in this extent, or a package downloaded before `<stem>_parcels.geojson` existed and re-bridged: `mapgen bridge` never re-runs a source's merge, so an old package gains its parcels, and with them its categories, only from a fresh survey over the extent), the same "nothing to do" reading `buildings_fusion`'s own all-zero record gets; only a non-null `error` means the step genuinely could not finish, and the rest of the package, including any earlier run's own categorised file, is left untouched. Runs immediately after `buildings_fusion` and before `lidar_heights`, on every run and on `mapgen bridge`, because it reads building evidence out of the FUSED `.osm` that step has already written. |
| `lidar_heights` | `written`, `buildings`, `kept_existing`, `no_data` and `error`: whether this package's `<stem>.osm` had DSM-minus-DTM building heights fused into it from the Welsh LiDAR layer. `written` is None on a package that never selected `lidar_wales`, which is different from `written: 0` (fused, and found nothing to add: every building already had a height, or none had enough LiDAR under it). `buildings` is every way tagged `building=*`; `kept_existing` is how many already carried a `height` tag and were left alone; `no_data` is how many could not be given one, for any reason (too little raster coverage, no OSTN15 shift, a malformed footprint): nothing here is invented for a building the rasters have no evidence for. Runs before the bridge, on every run and on `mapgen bridge`, so a package downloaded before this existed gets its buildings fixed in place with no re-download. |
| `roof_forms` | `buildings`, `classified`, `kept_existing`, `below_quality`, `no_data`, `relations_skipped`, `shapes`, `skipped_reason` and `error`: whether this package's `<stem>.osm` had a roof form fitted onto every untagged building way, from the same Welsh LiDAR DSM and DTM `lidar_heights` reads above it. `buildings` is every way tagged `building=*` (None when this package never selected `lidar_wales`, or is missing its `.osm` or either raster, which is different from `buildings: 0`); `classified` is how many were tagged `roof:shape` and the rest by this run; `kept_existing` is how many already carried `roof:shape`, from an earlier fusion or from OSM itself, and were left untouched; `below_quality` is how many had DSM samples but the fit did not clear the honesty floor (too few significant planes, too little of the footprint explained, or a fitted ridge under 2.0 m of its own ground); `no_data` is how many had no usable samples at all (no raster coverage, no OSTN15 shift, an unresolvable footprint); `relations_skipped` is every multipolygon relation, never inspected for a roof. `buildings == classified + kept_existing + below_quality + no_data` always. `shapes` is `{shape: count}` for classified ways only, over `gable`, `flat`, `mono` and `complex` (a fifth class, `hip`, was tried and dropped; see the `<stem>_roof_massing.geojson` entry above). `skipped_reason` is set, and every count above is None, when this package's own rasters are coarser than the 1 m level roof fitting needs: the same guidance the estimate panel's own detail preview already gives before download (extents under about 4 x 4 km come back at 1 m). `error` is a plain sentence on the rare case the step could not finish at all (an unreadable raster, a network failure fetching OSTN15), distinct from a resolution skip. Runs immediately after `lidar_heights`, on every run and on `mapgen bridge`. |
| `canopy` | `points`, `skipped_small`, `resolution_m` and `error`: whether this package's own LiDAR found clusters of above-ground return (DSM at least 3 m above the DTM) outside every building footprint and wrote them to `<stem>_canopy.geojson`. `points` is how many clusters cleared the minimum size (2 pixels, 8 m2) and became a Point feature; None when this package never selected `lidar_wales` or is missing its `.osm` or either raster, which is different from `points: 0` (ran, found nothing above the floor). `skipped_small` is how many candidate clusters were found and were too small to keep. `resolution_m` is the pixel size this run's own rasters actually carried, whatever that was: unlike `roof_forms`, this step has no 1 m floor of its own and runs at any resolution the package holds. `<stem>_canopy.geojson` itself is written only when `points` is at least 1. `error` is a plain sentence on the rare case the step could not finish (a mismatched DTM/DSM grid, an unreadable raster). Runs immediately after `roof_forms`, on every run and on `mapgen bridge`. |
| `inspire_boundaries` | `written`, `curves`, `kept_existing` and `error`: whether this package's `<stem>.osm` had HM Land Registry property boundary curves fused into it from `<stem>_boundaries.geojson`. `written` is None on a package that never selected `inspire`, which is different from `written: 0`: an authority whose padded extent held zero kept parcels writes a real, empty boundaries file and genuinely fuses nothing, and a re-run of a package already fused writes nothing a second time either, both `written: 0` for different, honest reasons. `curves` is how many LineString curves the boundaries GeoJSON itself holds, read whether or not this run went on to inject any of them. `kept_existing` is how many of that file's own ways were already in `<stem>.osm` from an earlier fusion (the download itself, or an earlier `mapgen bridge`), recognised by their own `source=hm_land_registry` tag, which is what makes a repeat run `written: 0` rather than a duplicate set of ways. Runs immediately after `canopy`, before the bridge, on every run and on `mapgen bridge`. |
| `started_at`, `finished_at` | UTC timestamps. |

## Using the output in Grasshopper, with Urbano 2

This is the point of the tool. In Grasshopper, on the **Urbano2** tab, in the
**1 Download/Import** panel, place the **Project Setting** component, paste
the contents of `<stem>_project_setting.json` into a panel, wire that panel
into the component's text input, and set its boolean input to true. You need
to be signed in to Urbano: the component checks its token first and refuses
before it does anything else.

That one file carries the extent, the UTM world origin and an absolute path
to every layer the package holds. Two things about how Urbano reads it are
worth knowing, because both are visible from the canvas:

- **It rebuilds the OSM path itself**, as the folder plus the file stem plus
  `.osm`, and skips the download if that file is already there. mapgen's
  naming matches, which is what makes a mapgen package a pass-through rather
  than a fresh download. A `.osm` is read as OSM XML, which is what mapgen
  writes, and a `.osm.pbf` as OSM PBF.
- **Terrain is the `.egrid`, never the `.tif`.** Urbano's
  `ElevationFilePath` is not a path to a raster: every component that reads
  it, Import Terrain included, deserialises it as an `ElevationGrid`
  protobuf, so a GeoTIFF there is not ignored, it stops the component with
  "Unexpected end-group in source data". mapgen converts the DEM into that
  grid and names the grid, which is also what stops Urbano attempting its
  own United States only elevation download. The `<stem>.tif` is still in
  the package and still recorded in `survey.json`; nothing in Urbano opens
  it. Expect the terrain to stop short of the survey edge by 40 to 70 m:
  the DEM OpenTopography returns is slightly smaller than the extent asked
  for, and `mapgen survey` prints how many of the grid's points have data.

The GeoJSON files are read separately, and Urbano 2 has a component for
them: **Import Geojson File** takes a GeoJSON path directly. The
`<stem>_project_setting.json` Overture path is there for that, and for the
**Deserialize Project Setting** component, which unpacks the file into its
folder, bound string, granularity, layer names and file paths, verbatim. If
Import Buildings' data source is set to Overture it will say "not a parquet
file": that field wants GeoParquet, so use OSM as the source there and take
the GeoJSON through Import Geojson File.

You can also skip the Project Setting component altogether. Any Grasshopper
panel holding the JSON can be wired straight into the project setting input
of **Import Streets** or **Import Buildings**, and those read the paths in
the file verbatim rather than rebuilding them.

The `layers/*.geojson` files (and the `<stem>_<type>.geojson` files in the
package root) are also independent of Urbano entirely, and read into any
Grasshopper component that understands GeoJSON.

To check the world origin against Urbano's own arithmetic rather than against
mapgen's tests, use the **WSG84 to UTM ProjPt** component on the same tab:
feed it the bottom left and bottom right corners of your extent and compare
its answer with `CoordinateReference` in the file.

## Data sources, licences and attribution

These are legal obligations, not decoration. `survey.json`'s `sources` array
records the exact licence and attribution string for every layer in a given
package; carry that text forward into anything published from this data,
including drawing sheets.

| Source | Licence | Attribution string |
| --- | --- | --- |
| OpenStreetMap | Open Database License (ODbL) 1.0 | (c) OpenStreetMap contributors |
| Overture Maps | Mixed by theme: Open Database License (ODbL) and CDLA-Permissive-2.0 | (c) Overture Maps Foundation |
| Copernicus DEM, via OpenTopography | Free for any use, with attribution | (c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH |
| Welsh LiDAR (`lidar_wales`) | Open Government Licence v3.0 | Contains Welsh Government and Natural Resources Wales information licensed under the Open Government Licence v3.0 |
| Cardiff 25 cm historic LiDAR (`lidar_cardiff`) | Open Government Licence for Public Sector Information (OGL) | Contains Natural Resources Wales information © Natural Resources Wales and Database Right. All rights Reserved. |
| HM Land Registry INSPIRE Index Polygons (`inspire`) | Open Government Licence v3.0 | This information is subject to Crown copyright and database rights [year] and is reproduced with the permission of HM Land Registry. The polygons (including the associated geometry, namely x, y co-ordinates) are subject to Crown copyright and database rights [year] Ordnance Survey AC0000851063. |
| OS Open map data (`os_open`): OpenMap Local, Open Roads, Open Greenspace | Open Government Licence v3.0 | Contains OS data © Crown copyright and database right [year] |
| Addresses (`os_uprn`): OS Open UPRN | Open Government Licence v3.0 | Contains OS data © Crown copyright and database right [year] |
| OSTN15 transformation (`bng.py`) | Ordnance Survey, Open Source Initiative BSD Licence | Copyright and database rights Ordnance Survey Limited 2016, Crown copyright and database rights Land & Property Services 2016 and/or Ordnance Survey Ireland, 2016. All rights reserved. |

Every package with a Welsh LiDAR layer or an `.egrid` derived from it carries
its coordinates through the OSTN15 transformation (`src/mapgen/bng.py`), so
that row's licence and attribution carry forward too, even though OSTN15
never appears as a `LayerSource` of its own.

**OS Open's downloads are cached once, not repeated per survey.** `os_open`
parses OpenMap Local, Open Roads and Open Greenspace once per 100km National
Grid square and keeps the result under `~/.mapgen/osopen`; a first survey
touching south Wales' SS and ST squares downloads roughly 170 MB of OpenMap
Local zips between the two of them, plus Open Greenspace's own few MB, and
Open Roads reads only the squares it needs, at roughly 50 MB per area, out of
the OS Data Hub's single 608 MB national zip through a ranged read rather
than downloading the whole file. `os_uprn`'s address layer is a single 619 MB
national CSV, downloaded once, ever, and cached for every later survey
regardless of where in Great Britain it runs. `lidar_cardiff`'s two archive
zips (about 84 MB combined) are cached the same way, once, under
`~/.mapgen/lidar_cardiff`, regardless of which part of the ten-tile block a
given survey's own extent touches. Every later survey over ground
already cached this way costs nothing further to download, and `mapgen
estimate` says so through each source's own `routing_note`.

**Both** attribution statements above are required for `inspire`, not one or
the other: the first covers the underlying INSPIRE data, the second the
geometry itself, and `survey.json`'s own `sources` entry carries both,
`[year]` substituted for the real publication year, alongside a
`conditions_url` pointing at
[HM Land Registry's own conditions page](https://use-land-property-data.service.gov.uk/datasets/inspire/#conditions),
which is the current, authoritative text this table summarises. These
boundaries are indicative, never legal, in HM Land Registry's own words,
repeated on every curve and on every `boundary=property` way they are fused
into: "The extent of the land contained in any registered title cannot be
established from the INSPIRE Index Polygons."

ODbL requires attribution and, if you redistribute the data itself (as
opposed to a map or drawing derived from it), requires any substantial
extract to stay under an ODbL-compatible licence. See
[osm.org/copyright](https://www.openstreetmap.org/copyright) and the
[Overture Maps licensing page](https://overturemaps.org/documentation/attribution/)
for the current, authoritative text; the table above is a working summary,
not a substitute for reading either.

## Limits worth knowing about

**The OSM node cap.** The OSM map API refuses any single request over 50000
nodes. This is the map API's own limit; Overpass has no equivalent, so it
only applies to a run that is actually using the map API, which is the
default whenever every category is selected. A tile that dense is split
into four quarters and downloaded in pieces, and a quarter that is still
too dense is split again, twice at most: a 2000 m tile becomes at most
sixteen 500 m pieces. The pieces are merged back into that tile's own
file, so the rest of the run carries on exactly as if the tile had
arrived in one request, `survey.json` records it as an ordinary `ok`
tile, and the map shows it finishing green. Each split is announced
through the progress log (`[tile_subdivided] source=osm tile_id=r03_c04
pieces=4 depth=1`).

Only the dense tile pays. The tiling itself never changes, so the work
folder, the resume state and every tile and Overture download that has
already succeeded are all untouched: a split costs at most that one
tile's own pieces, about 40 seconds at the map API's rate limit, not
another run. (Until Task 26 the whole run restarted at 1500 m and then
1000 m instead, which meant refetching everything that had already
worked; on a 72-tile extent that turned a three minute download into
fifteen.) The quarters are kept in a `_split/` folder under `_work/`
until the package completes, so a run interrupted mid-split resumes into
it rather than starting it again.

Ground still over 50000 nodes in a sixteenth of a tile fails, and mapgen
says which piece it was and how large that piece is, so "draw a smaller
extent" is a decision you make with the number in front of you. A run
already on Overpass, whether because a category filter put it there or
because `use_overpass=True` was set directly, never splits at all: there
is no node cap on that path for a smaller piece to fix.

**Overpass is a second, separate shared public service.** Once any category
is deselected, OpenStreetMap downloads switch from the map API to Overpass
to actually honour the filter (see "Category filtering" above). Overpass
has its own rate limits, its own availability, and no relation to the map
API's quota; `mapgen estimate` and `survey.json`'s per-source `routing_note`
both name which one a run depends on, so a busy Overpass instance is a known
possibility, not a mystery, when a filtered run is slower or a 429 turns up.
Endpoint rotation and rate limiting already applied to the map API apply
identically here. `use_overpass=True` remains available directly to Python
callers as a standing, whole-run choice independent of category selection.

**Windows path length.** Windows resolves a path at 260 characters by
default, and this repository's own OneDrive-synced location is already
around 110 of those before a survey folder even starts. mapgen computes the
worst-case path a job would produce, including the tiling work folder, and
refuses the job before any network call if that exceeds 240 characters,
naming the longest path it would have written and suggesting a shorter
output root or region/site name. This is a real limit, already hit once
during development, not a theoretical one: choose output roots and site
names accordingly if you are working from a deeply nested folder.

**The Urbano bridge.** `tools/UrbanoBridge` is a C# program that loads
Urbano's own assemblies to do work mapgen cannot do natively. It is optional
and, as shipped, it does not work: it looks for `Urbano.Core.dll` and
`ProjectSetup.dll`, and Urbano 2 has neither, having merged both into
`Urbano.SiteAnalysis.gha`. What a retarget would involve is written up in
`docs/urbano/README.md`.

Nothing in a package depends on it. mapgen writes
`<stem>_project_setting.json` itself, so a failed bridge costs a survey
nothing: the failure is recorded as a plain sentence in `survey.json`'s
`bridge.error` and printed to the console, and the package is finished and
usable. Pass `--skip-bridge` to skip the step outright rather than have it
attempt and fail. Three of its steps (census blocks, EPW climate and USGS
3DEP elevation) are United States only in any case, so for UK work there is
little it would add. To build it:

```powershell
dotnet build tools/UrbanoBridge/UrbanoBridge.csproj
```

## Benchmarking against OS NGD

`mapgen benchmark <package-dir>` compares a package's own open-stack outputs
(OSM buildings and roads, plus whatever Overture/OS OpenMap Local footprints
`fuse_missing_buildings` injected) against Ordnance Survey's own survey-grade
National Geographic Database over the same extent: matched building
fractions and IoU, road centreline offsets, and an epoch-shift verdict (see
`docs/superpowers/specs/2026-08-06-epoch-shift-note.md`). It needs an OS Data
Hub Premium key, development mode, with the NGD Features API added to the
project; supply it with `--key` or the `OS_NGD_KEY` environment variable
(`--key` wins if both are given), never through `~/.mapgen/config.json`.

```powershell
$env:OS_NGD_KEY = "your-dev-mode-key"
mapgen benchmark "C:\Surveys\South-Wales\2026-08-06_Cowbridge-with-Llanblethian"
```

Reports (`report.md` and `report.json`) are written under `benchmarks/` by
default (`--out` to choose elsewhere), local only: `benchmarks/` is
gitignored and nothing under it is ever meant to be committed or shipped.

The firewall this command runs behind is binding, not a suggestion: premium
NGD geometry, feature ids and attribute values are read into memory, turned
into aggregate counts and statistics, and discarded; nothing from the pull
itself, only class names and numbers, ever reaches the report, and nothing
from either report ever reaches a package directory or a client. `ngd.py`,
the client that talks to NGD, never writes to disk at all, by construction.

A development-mode project throttles to 50 transactions per minute per API;
`mapgen benchmark` paces its own requests to stay under that, and honours a
`Retry-After` header with one bounded retry if the ceiling is hit anyway.

## Tests

Python:

```powershell
.venv\Scripts\python.exe -m pytest
```

One test, `tests/test_live_smoke.py`, hits the real OpenStreetMap API and is
excluded from the default run (`pyproject.toml` sets `addopts = "-m 'not live'"`).
Run it deliberately when you want to confirm live network access still works:

```powershell
.venv\Scripts\python.exe -m pytest -m live -v
```

The map picker's front end (`src/mapgen/web/static/app.js`) is plain
JavaScript with no build step and no bundler, so it is tested separately with
a small, dependency-free Node script rather than a JS framework:

```powershell
node tests/js/test_app.js
```

Requires Node (developed against v22). It runs the real, committed `app.js`
inside a minimal DOM and `fetch` stub and checks its behaviour directly; it
does not open a browser and cannot check map rendering or tile loading, which
still need a human looking at the page.

## Roadmap

Phase 2 adds UK survey-grade layers behind the same `LayerSource` interface
used by `osm`, `overture` and `elevation`.

**Build item 1, Welsh LiDAR, has shipped.** The `lidar_wales` source packages
Natural Resources Wales' 1 m DTM and DSM rasters straight from the Welsh
Government's whole-Wales mosaics (Open Government Licence v3.0), generates
contours at the interval each extent's own area qualifies for, fuses
DSM-minus-DTM building heights into the `.osm`, and feeds the `.egrid` from
that same 1 m DTM with the 30 m OpenTopography DEM as fallback beyond its
edge. The resolution promise here is 1 m, not finer: the 25 cm data in NRW's
own archive catalogue turned out, on checking, to be ten 2011 quarter-tiles,
about 2.5 km2, in the Creigiau and Pentyrch corner of north-west Cardiff,
nowhere near most Welsh sites, which is why `lidar_wales` itself never
claims 25 cm. That corner is served separately, honestly, by its own
source; see item D below. (This paragraph used to say no copy anywhere in
this project claims 25 cm at all. That was true before item D existed; the
25 cm claim now exists in exactly one place, `lidar_cardiff`'s own
surfaces, where it is true.)

**Build item 2, INSPIRE property boundaries, has shipped.** The `inspire`
source downloads HM Land Registry's INSPIRE Index Polygons for whichever
local authority (or authorities, at a border) an extent falls into, from a
committed, offline index of all 318 England and Wales authorities, and
deduplicates the parcels into curves: shared edges between neighbouring
parcels collapse to a single line and chains break at junctions, so the
result is a boundary drawing, `<stem>_boundaries.geojson`, never a
cadastral database of closed, tagged parcels. The same curves are fused
into `<stem>.osm` as `boundary=property` ways, and `survey.json` carries
both of HM Land Registry's required attribution statements with the
publication year substituted in, plus the conditions link. See "Data
sources, licences and attribution" and the `inspire_boundaries` row of the
`survey.json` schema above for the detail.

**Build item 3, an OS Open pack and a buildings fusion step, has shipped.**
`os_open` packages OS OpenMap Local, OS Open Roads and OS Open Greenspace as
six GeoJSON files (`_os_buildings`, `_os_roads`, `_os_rail`, `_os_greenspace`,
`_os_sites`, `_os_land`; see `docs/urbano/README.md` for what each holds and
how to filter it in Urbano), and `os_uprn` packages OS Open UPRN addresses as
`_os_uprn.geojson`. A per-extent tier resolver (`src/mapgen/resolver.py`)
records, for every category a selected source can serve, which source this
extent will actually use and which others are along for backup or reference
only; the estimate panel and `survey.json`'s own `resolution` key both carry
it. The buildings fusion step (`src/mapgen/buildings.py`) is the owner's own
missing-buildings fix: it injects Overture and OS OpenMap Local footprints
the OSM base lacks straight into `<stem>.osm`, fusion only, never replacing
an existing OSM building, and it runs whether or not `os_open` itself is
selected. OS Open Roads is deliberately never fused (fusing it would double
every road OSM already carries); it ships as a reference layer instead,
filtered by key and value in Urbano's own GeoJSON import. **Boundary-Line**
(OS's own administrative boundaries product) is deferred from this pack:
nothing in the owner's workflow consumes it yet, and it joins later through
the same OS Data Hub client in a short task if wanted.

The items the owner named in the 2026-08-07 working session now have their
own spec addendum (`docs/superpowers/specs/2026-08-07-mapgen-phase2b-addendum-design.md`)
and most have shipped: the per-source detail preview (item C), categorised
property boundaries (item A, see the `boundaries_categories` row of the
`survey.json` schema above), roof forms and canopy positions read off the
DSM (item B, see the `<stem>_roof_massing.geojson` and `<stem>_canopy.geojson`
entries and the `roof_forms` and `canopy` schema rows above), and the Grasshopper
GeoTIFF reader script (`docs/grasshopper/lidar_to_mesh.py`, a documentation
artifact rather than pipeline code).

**Build item D, the Cardiff 25 cm LiDAR source, has shipped.**
`lidar_cardiff` packages Natural Resources Wales' 2011 historic 25 cm
archive as `<stem>_lidar25_dsm.tif` and `<stem>_lidar25_dtm.tif` over the
ten quarter-tiles it actually covers, Creigiau and Pentyrch in north-west
Cardiff, about 2.5 km2; see "What a survey folder contains" above for the
full detail (location honesty, the flown-2011 vintage, the budget rule,
and why heights, roofs, canopy, the `.egrid` and every contour file still
read from the 2020-2023 1 m data regardless). Proven live over a real,
sub-budget extent inside the block: both rasters at 0.25 m exactly, real
Creigiau/Pentyrch heights sampled off them, a real cache-warm survey
running in about 22 seconds end to end (the merge itself accounting for
nearly all of that), and the Grasshopper reader
(`docs/grasshopper/lidar_to_mesh.py`) confirmed against the live DSM. The
owner's own 2026-08-08 decision, recorded so it is not re-opened: ship the
spec as written, the ten tiles, not the general Wales-wide archive access
the plan-time probe also surfaced as an option; that broader route was
offered and explicitly declined.

Still to come, in build order: an OS benchmark (item F), a dev-mode
comparison against OS's paid developer tooling to calibrate the tier
resolver's own tier tables against a second opinion; then, per the phase 2
design record, DataMapWales constraints and Cadw designations together
with planning.data.gov.uk for England, Sentinel-2 context imagery via
Earth Search, England LiDAR as its own task (the discovery API is open but
bulk raster download there has no documented route yet), PlanIt planning
history, and BGS boreholes. See
`docs/superpowers/specs/` for the design record covering that last group.
