# mapgen handoff, updated 2026-08-06

Read this first when resuming. It is tracked in git deliberately, because the
detailed ledger is not.

## Where the work is

- Worktree: `C:\Users\Param\mapgen-phase1`, branch `feat/phase1`
- Main worktree: `.../VS code/Rhino Plugins/mapgen`, branch `main`
- Remote: `https://github.com/EJR-of-Scrutopia/Geomapper-GH.git`
- **The first push happened.** Both remote branches sit at `3748225`.
  `feat/phase1` is now **162 commits ahead** of the remote (as of this
  task's own last commit, 2026-08-06; count with
  `git rev-list --count origin/feat/phase1..HEAD`) and `main` is 1 ahead.
  The permission layer denies `git push` from the assistant's shells, so
  the owner runs it.
- Run tests: `.venv\Scripts\python.exe -m pytest -q` and
  `node tests/js/test_app.js`. Add `-m live` for the network tests.
- Last known green: **1503 Python (12 deselected `live`), 273 Node**, from
  this task's own full offline run; only the one live test this task added
  was rerun this pass, not the other 11. Use the venv python, not the
  system one, or every import fails.

## The ledger, and why it matters

`.superpowers/sdd/2026-08-01-mapgen-phase1/progress.md` holds the memory of
how this was built and why decisions went the way they did.

**It is gitignored.** A `git clean -fdx` destroys it. Read it before doing
anything non-obvious, and consider committing it if the work continues.

Other artefacts beside it: per-task briefs, per-task reports, review diffs.

## State when paused

Phase 1 is complete and the tool works. A real survey runs end to end, writes
`<Site>_<date>.osm` and a `survey.json` that describes itself accurately, and
survives the Urbano bridge failing. The final whole-branch review's two
Criticals and two Importants are closed and were mutation-verified; see the
ledger for the detail, which is no longer repeated here.

Since then, tasks 21 to 39 landed across three review cycles: a whole-branch
review after task 28 (1 Critical, 10 Important, all fixed), and a second after
task 39 (0 Critical, 2 Important, 15 of 17 Minors fixed, the rest deferred
with reasons in `review-fixes-29-39-report.md`). The per-task sections below
stop at task 28; tasks 29 to 39 are summarised right after this paragraph, and
the gitignored ledger holds the full detail of everything.

**Tasks 29 to 39, one line each.** 29: `mapgen bridge <dir>` runs the Urbano
step over an existing package. 30: end-of-run verify, per-tile failure reasons,
bounded retry, no fabricated empty output. 31: red tiles explain themselves;
the destination moved beside Download. 32: retry reaches every layer through
one shared classifier. 33: the console compacts long list fields. 34: Urbano
inspected properly at last (9,221 types; two earlier wrong conclusions about
it corrected, method note in the Urbano section below). 35: **mapgen writes
the Urbano project setting natively**; `utm.py` matches Urbano's own
projection to 1e-9 m. 36: six interface fixes including per-tile green and the
boot-time Download gate. 37: the protobuf crash was `ElevationFilePath`
holding a GeoTIFF; the field now only ever names a real `.egrid`; our OSM XML
was always fine. 38: five interface adjustments plus three fix rounds ending
with two sinks (`setBBox`, `renderTileGrid`) that make "a running job's
display cannot be destroyed from the form" true by construction. 39: **native
`.egrid` terrain writer** (`geotiff.py`, `egrid.py`), proven bit-identical
against Urbano's own grid builder, with orientation checks that fail on
deliberately flipped controls.

**The owner's Grasshopper canvas renders**: streets by OSM tag, buildings
coloured by building tag, from a mapgen package end to end. Terrain awaited
their next test at the time of writing.

Every task was verified by the coordinator directly, by running the code, not
by relaying the implementer's report. Nearly every task found either a real
defect the implementer's own summary described inaccurately, or an error in
the coordinator's brief that the implementer was right to push back on, so
**do not skip the independent verification step in either direction**.

Known accepted debris: commit `aa8ff29` carries a captured mid-mutation
`app.js` and fails the Node suite at that commit (HEAD is correct); a
parallel-work `git add -A` swept files across two commits. Documented, not
rewritten. Standing policy since: implementers sharing a tree commit with
explicit paths only.

### Task 21, phase A repairs (`69166c8`)

Unticking every category is refused at `SurveyRequest.__post_init__`, so the
CLI and the browser get the same rejection through one code path. Saved API
key indicator added, without echoing the key. Resume confirmed against a
replica of the owner's real package shape.

### Task 22, stop with the data intact (`43e2456`, `de76352`, `0344e7d`, `572d26b`)

Pressing Stop keeps every tile already fetched, merges them, and writes a
truthful `survey.json`. Cancellation reaches the per-tile loop. `survey.json`
gained a `stopped` field, because `complete` alone could not tell "short
because the owner said so" from "short because something broke". The map grew
a live tile grid with four states, and the interface gained dark and light
mode following the OS by default.

Verified live: stop took 0.55s, and left a 5.6 MB well-formed `.osm` with
17,783 nodes and 4,105 ways plus a valid `survey.json`.

**A defect was found in review and fixed in `572d26b`:** a stop marked every
tile it never reached as `failed`, so 14 of 16 tiles read as failures when
nothing had failed, and the map turned red at the moment of a clean stop. A
tile a stop never reached is now `pending` and emits no `tile_failed`.

### Task 23, Overture is no longer tiled (`812171f` and five more, plus `c7a707b`, `143a941`, `eef7840`)

Overture was tiled only because OSM had to be. OSM's 50,000-node cap is real;
Overture is a bbox-filtered parquet read with no equivalent limit. Measured
over the same extent, tiling cost 15 to 21x the wall clock for byte-identical
output.

**Measured end to end through the real CLI: 1623s before, 85s after**, with
byte-equivalent packages and identical per-type feature counts across all
eight types. Overture now fetches one whole-extent file per type.

Two defects surfaced while checking this, both specific to `overturemaps`
**0.20.0**, and both invisible until now because `shutil.which` finds the
older **0.19.0** in `C:\Python313\Scripts` before the 0.20.0 copy in
`.venv\Scripts`:

1. **0.20.0 crashes on Welsh characters.** It died on `ŷ` writing place
   names over the Barry bbox, at 337,454 of 1,056,802 bytes, because it
   wrote with the cp1252 locale default. The owner surveys Welsh sites, so
   this is their normal input. Fixed by forcing `PYTHONUTF8=1` for every
   child process in `procutil`, together with `encoding="utf-8",
   errors="replace"` for reading child output, since decoding a UTF-8 child's
   stderr as cp1252 under a strict handler is its own crash.
2. **0.20.0 leaves a `.state` sidecar** that survived into the package root
   as a junk layer. Cleaned up after download and by the up-front debris
   sweep, which covers the resume case where the download never runs.

`overturemaps` is now pinned `>=0.19,<0.21`. Note the pin decides what a
fresh install **gets**; which copy **runs** is still PATH order.

Verified end to end on the failing configuration: the 0.20.0 copy forced first
on PATH, cp1252 locale, `PYTHONUTF8` stripped from the parent so the child
could not inherit the fix by accident. Complete package, zero junk files,
Welsh diacritics intact.

### Task 24, concurrent Overture downloads

The 8 whole-extent calls now run together. Measured first: sequentially 68.54s,
3 at a time 31.73s, 8 at a time 19.07s, byte-identical output at every level.
At 8-way the wall clock equals the single slowest type, so 8-way is the floor.

Worst-case Stop latency roughly doubled as a result, from about 10-12s to about
17.8s, because every download slows under contention. Accepted: the window a
stop can land in is 3.4x shorter and the expected wait drops about 40%.

### Task 25, the cost estimate refit

The estimate had drifted to 34x over at a real site extent, because it still
modelled cost as scaling with ground covered, which stopped being true when
Overture went untiled and concurrent. Now within about 20%, verified
independently at two extents 62x apart in area.

Its guard test had been passing for the wrong reason: it asserted a band
anchored to measurements taken when downloads were sequential, so the number
stayed inside a band that no longer described anything.

### Task 26, a dense tile splits instead of restarting the run

A tile over the OSM 50,000-node cap used to restart the **whole run** at a
smaller tile size. Because `tiling_fingerprint` hashes `tile_size_m`, that
landed in a fresh `_work/` directory and refetched everything, including every
Overture type that had already succeeded. On the Barry extent: 72 tiles, then
132, then 272, with a full Overture run each time.

Now the tile splits into quarters, recursively, capped at
`MAX_SUBDIVISION_DEPTH` (2). Quarters are recombined into the single
`<tile_id>.osm` the rest of the pipeline expects, so the fingerprint never
changes and resume keeps working. The whole-run ladder is deleted.

**A path-length caveat worth knowing.** `DEFAULT_PATH_LIMIT` is 240 and does
not actually protect against Windows MAX_PATH, because every write goes
through a temp path that appends about 26 characters. An ordinary tile at the
limit already creates 266. This predates subdivision and applies to every write
mapgen has ever made. It works here only because `LongPathsEnabled` is 1 on
this machine. Two tests in `test_naming.py` record this honestly.

### Tasks 27 and 28, the interface

Progress bar with a countdown, tile size slider, folder picker, and the
elevation DEM model as a validated choice with per-model licence and
attribution.

**The tile size slider does not show failure risk**, deliberately. Density is
unknowable before downloading, and since Task 26 a dense tile is not a failure
at all. A fabricated risk metric that looks measured would be worse than the
plain sentence it shows instead.

**The most important thing Task 27 found was not a feature.** The Node
harness's Leaflet stub had no `setStyle`, and no test had ever supplied a real
`tile_grid`, so Task 22's entire grid-painting path had **never once executed**
while the suite reported green. Task 28 then found two more stubs with the same
disease, and the whole-branch review found a third. Assume there are more.

## What needs the owner, not an agent

1. **Browser test.** The tile grid's four states at real zoom, whether they
   read clearly in both light and dark, and watching Stop behave live. None of
   this can be checked without a browser. Launch from the desktop `mapgen`
   shortcut, or `.venv\Scripts\mapgen.exe ui`.
2. **Rhino test.** Run a survey on a site they know and open it. Nothing in
   this repo has verified the geometry is where an architect expects.
3. **Urbano: solved on paper, not yet confirmed in Rhino.** This section was
   wrong twice before it was right. Read the method note at the end before
   trusting any future edit to it.

   **The findings, all independently re-verified by the coordinator with a
   built-and-run `System.Reflection.Metadata` tool, not inferred:**

   - Urbano 2.2.1.2 sits at
     `AppData\Roaming\McNeel\Rhinoceros\packages\8.0\Urbano2\2.2.1.2\` and
     ships exactly one assembly, `Urbano.SiteAnalysis.gha` (17.3 MB).
   - That assembly defines **9,221 types across 419 namespaces**, including
     `Urbano.Core.Data`, `Urbano.Core.Engine` and `Urbano.Core.Helpers`.
     Everything the bridge needs is in there, in one file.
   - `Urbano.Core.dll` and `ProjectSetup.dll`, the two filenames
     `tools/UrbanoBridge/Program.cs` loads, do not exist. That is the bug.
   - The namespace moved: `Urbano.Core.Helpers.WorldOrigin` exists,
     `Urbano.Core.Process.WorldOrigin` does not. Confirmed both ways.
   - `Ed.Core.dll` is **Rhino's Monaco script editor**, nothing to do with
     Urbano. Searching it was searching the wrong file.
   - Every string literal in the `.gha` is encrypted (the whole `#US` heap is
     38 strings), so **no string search of it can ever find anything**. Type
     and namespace names live in `#Strings` and are readable; string literals
     are not.

   **The retarget** is four edits in `Program.cs`: find the `.gha` rather than
   two DLLs, load one assembly rather than two, rename five types from
   `Urbano.Core.Process.*` to `Urbano.Core.Helpers.*`, and delete
   `ProjectSetup.Overture`, which genuinely no longer exists in 2.2.1.2. Plus
   `_MISSING_URBANO_RE` in `bridge.py`.

   **But the bridge may be the wrong shape anyway.** `ProjectSettingComponent`
   takes a text input and a boolean; given a project setting JSON it rebuilds
   every data path as `Folder + "\" + FileNameStr + <extension>` and **skips
   any file already on disk**. mapgen's naming already matches that exactly.
   So a mapgen package plus the right JSON makes the component a no-download
   pass-through that just emits a `ProjectSettingParam` for the rest of the
   canvas, with no bridge executable involved at all. Three of the bridge's
   engine steps are US only and its traveller ONNX model is absent from this
   machine, so for a UK survey the smaller shape may be the better one. That
   is a design call the owner has not made yet.

   `<stem>_project_setting.json` is confirmed by black-box test to be exactly
   the right filename. The bridge already gets that right and has never
   reached it.

   Two traps: `NaN` or `Infinity` in the JSON breaks both of Urbano's readers,
   which use default `JsonSerializer` options while Urbano's own `ToJson`
   writes named float literals. And `ProjectSettingComponent.SolveInstance`
   refuses to run on a missing or expired `UrbanoAuth` token, which is
   unrelated to any of this but will look like a failure.

   **Method note, worth more than the findings.** The first attempt read a raw
   binary string search as proof that types existed. The second attempt
   "corrected" it using `ReflectionOnlyLoadFrom`, whose `GetTypes()` threw and
   returned a **partial** list of 314 types, which was taken as authoritative.
   314 was 3% of the truth, and the correction was wrong in the opposite
   direction. Neither a string search nor a failed reflection load is
   evidence. Use a metadata reader that resolves no dependencies, and check
   whether the result you got is partial before believing it.
4. **The Barry package is abandoned deliberately.** The owner has said to drop
   it and download a fresh one from the interface instead, so the resume
   instructions that used to be here no longer apply. The folder can be
   deleted whenever they like.
5. **The push.** `git push origin feat/phase1 main` from
   `C:\Users\Param\mapgen-phase1`.
6. **Scratch directories to delete by hand**, since the permission layer
   denies deletion from the assistant's shells: `C:\Users\Param\mgbench23`
   (around 315 MB), `mgbench`, `mgpar`, `mgstop`, `mgorphan`, `mggap`,
   `mgstate`, `mgstate2`, `mgwelsh`, `mgtmp1`, and
   `C:\Users\Param\AppData\Local\Temp\mgrev`. Nothing under
   `C:\Users\Param\Surveys` was touched by any of this.

## Still on the owner's list, not yet built

- **Availability watcher.** Check a source on demand and show a quality badge.
  The only item from the owner's interface list not yet built.

**A correction worth not re-inheriting.** An earlier version of this file said
exposing `demtype` would unlock the higher resolution the owner's
OpenTopography tier allows. That was wrong, and `sources/elevation_models.py`
was written to refute it. OpenTopography's global DEM API serves nothing finer
than 30 m anywhere, and OT+ buys North American LiDAR, not sharper global data.
COP30 was already at the ceiling for Wales. The real benefit of the choice is
surface model against bare earth at the same 30 m (EU_DTM covers the UK). NRW
LiDAR through DataMapWales, in phase 2, is the only route to genuine detail on
Welsh sites.

## Then

Phase 2 is specced, approved by the owner, and verified: read
`docs/superpowers/specs/2026-08-05-mapgen-phase2-design.md` (revised after
the task-zero verification pass; evidence in `phase2-task-zero-report.md`
beside the ledger).

**Build item 1, Wales LiDAR, is built and proven end to end.**
`docs/superpowers/plans/2026-08-05-mapgen-phase2-01-wales-lidar.md`'s nine
tasks are all complete: `lidar_wales` is a registered source delivering
packaged 1 m DTM/DSM rasters, per-interval contours, DSM-DTM building
heights fused into the `.osm`, and an `.egrid` fed from LiDAR with the
30 m DEM as fallback. Task 9's live `run_survey` over a real Barry extent
(osm, overture, elevation, lidar_wales together) confirms the whole chain
on disk at once: a fused `height`/`source:height` pair in the `.osm`, both
LiDAR rasters, all four contour files, an `.egrid` whose
`elevation_grid.source` reads `"lidar_wales+opentopography"`, and a
`survey.json` provenance entry carrying the OGL licence and attribution
sentences. This is pending the whole-branch review that has closed every
prior build item before it shipped; nothing here should be treated as
final until that review runs.

**Build item 2, INSPIRE property boundaries, is built and proven end to
end.** `docs/superpowers/plans/2026-08-06-mapgen-phase2-02-inspire-curves.md`'s
seven tasks are all complete: `inspire` is a registered source that resolves
a survey extent to whichever of the 318 England and Wales HMLR authorities
cover it from a committed offline index, downloads and streams their
INSPIRE Index Polygons through the service's own cookie handshake, and
deduplicates the parcels into curves rather than parcels: shared edges
collapse to one line and chains break at junctions, delivered as
`<stem>_boundaries.geojson` and fused into the `.osm` as `boundary=property`
ways with negative, collision-safe synthetic ids. Task 7's live
`run_survey` over a real Llantwit Major extent (osm, lidar_wales, inspire,
plus elevation when the owner's own key resolves) confirms the whole chain
on disk at once, alongside item 1's own LiDAR chain still intact: 1,747
curves, a fused `boundary=property`/`source=hm_land_registry` way whose
negative ids resolve to real nodes, `survey.json`'s `inspire_boundaries`
block reporting a real written count, and a `sources` entry carrying both
required attribution statements with the placeholder year genuinely
substituted plus the HMLR conditions link. This is pending the
whole-branch review that has closed every prior build item before it
shipped; nothing here should be treated as final until that review runs.

**Watch-items for build item 2 onward, carried from Wales LiDAR's own
review.** The int16 GDAL_METADATA scale/offset refusal deferred at Task 3
(`cog.py`) needs to land before any 16-bit mosaic path is pointed at: right
now a decimetre-scaled 16-bit source would read 10x tall, unrefused.
Retry-After plumbing through `CogError` deferred at Task 6 pays off once a
rate-limited OGC source arrives, which INSPIRE and the DataMapWales
constraints layers both are; and `estimate()` still has no contour-seconds
term, invisible so far only because Wales LiDAR is the only source that
generates contours at all.

Next action: plan build item 3, an OS Open pack (roads, OpenMap Local,
UPRN) behind a per-extent tier resolver, from the phase 2 spec.

## Standing constraints

No new third-party dependencies. No build step. The page contacts only this
machine and the tile servers. Every `/api/` route is token gated. No AI
attribution anywhere. No em dashes, including interface copy. Never log, echo
or embed an API key. Do not touch anything under `C:\Users\Param\Surveys`.

## The lesson worth carrying

The dominant defect across every task was a test that passed for the wrong
reason, almost always a test double more permissive than the real thing. The
method that worked was mutation: break the behaviour, confirm the named test
fails, restore, confirm the diff is clean.

Two recent examples worth remembering, because both nearly proved the
opposite of what they claimed. An `overturemaps` 0.20.0 stub built with
`json.dumps` at its default `ensure_ascii=True` emitted six ASCII characters
where the real library emits one raw character, so it encoded cleanly under
cp1252 and would have "proved" a crash could not happen. And a repro of that
same crash showed the broken version succeeding, because `PYTHONUTF8=1` had
been set on the parent shell so it could print the character, and the child
inherited it, silently applying the very fix the test was trying to show the
absence of.

Assume there is another.
