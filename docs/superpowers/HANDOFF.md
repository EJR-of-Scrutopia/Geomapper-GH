# mapgen handoff, updated 2026-08-04

Read this first when resuming. It is tracked in git deliberately, because the
detailed ledger is not.

## Where the work is

- Worktree: `C:\Users\Param\mapgen-phase1`, branch `feat/phase1`
- Main worktree: `.../VS code/Rhino Plugins/mapgen`, branch `main`
- Remote: `https://github.com/EJR-of-Scrutopia/Geomapper-GH.git`
- **The first push happened.** Both remote branches sit at `3748225`.
  `feat/phase1` is now **18 commits ahead** of the remote and `main` is 1
  ahead. The permission layer denies `git push` from the assistant's shells,
  so the owner runs it.
- Run tests: `.venv\Scripts\python.exe -m pytest -q` and
  `node tests/js/test_app.js`. Add `-m live` for the network tests.
- Last known green: **667 Python (3 deselected `live`), 95 Node**. Use the
  venv python, not the system one, or every import fails.

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

Since then, four tasks landed. Each was verified by the coordinator directly,
by running the code, not by relaying the implementer's report. Each of the
last three found a real defect that the implementer's own summary described
inaccurately or that the coordinator's brief got wrong, so **do not skip the
independent verification step**.

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

In flight at the time of writing. Measured first: the 8 whole-extent calls run
sequentially in 68.54s, 3 at a time in 31.73s, and 8 at a time in 19.07s, with
byte-identical output at every level. At 8-way the wall clock equals the single
slowest type, so 8-way is the floor.

## What needs the owner, not an agent

1. **Browser test.** The tile grid's four states at real zoom, whether they
   read clearly in both light and dark, and watching Stop behave live. None of
   this can be checked without a browser. Launch from the desktop `mapgen`
   shortcut, or `.venv\Scripts\mapgen.exe ui`.
2. **Rhino test.** Run a survey on a site they know and open it. Nothing in
   this repo has verified the geometry is where an architect expects.
3. **Urbano.** `Urbano.Core.dll` and `ProjectSetup.dll` exist nowhere on this
   machine, so `tools/UrbanoBridge` has never run against a real install and
   `_project_setting.json` is untested as an actual Urbano input. Task 2's
   gate is still open.
4. **The Barry package.** `C:\Users\Param\Surveys\Vale-of-Glamorgan\2026-08-03_Barry`
   is a real interrupted download holding 20 complete OSM tiles, 34 Overture
   files, zero `.part` files and no `survey.json`, which is exactly the state
   resume exists for. Resume it with the **CLI, not the browser**, repeating
   the original parameters plus `--date 2026-08-03` explicitly, because the
   web UI never sends a date and so only lands on that folder if run on the
   same day. Nothing needs deleting first. Its 34 Overture files are orphaned
   by Task 23's layout change and will be swept and refetched, which is faster
   than resuming them would have been.
5. **The push.** `git push origin feat/phase1 main` from
   `C:\Users\Param\mapgen-phase1`.
6. **Scratch directories to delete by hand**, since the permission layer
   denies deletion from the assistant's shells: `C:\Users\Param\mgbench23`
   (around 315 MB), `mgbench`, `mgpar`, `mgstop`, `mgorphan`, `mggap`,
   `mgstate`, `mgstate2`, `mgwelsh`, `mgtmp1`, and
   `C:\Users\Param\AppData\Local\Temp\mgrev`. Nothing under
   `C:\Users\Param\Surveys` was touched by any of this.

## Still on the owner's list, not yet built

In their stated order: fix what is broken, then speed, then interface.

- **Subdivide on failure.** The owner changed their mind from the whole-run
  tile-size retry to splitting a failed tile into 2 or 4 and continuing, so
  progress is not lost. Not started.
- **Interface work.** A save-location button, a progress bar with a countdown,
  a tile size slider showing time and failure risk, and an availability
  watcher with a quality badge checked on demand.
- **`demtype` is hardcoded** to COP30 and is reachable from neither the CLI
  nor settings. The owner's OpenTopography tier allows higher resolution
  (OT-hosted High Resolution Global 10m, 250M points per job), so this is
  leaving detail on the table.

## Then

- Phase 2 spec, layers in this order: NRW LiDAR through DataMapWales, OS Open
  data, constraints and designations, geology and land cover.
- **Verify the phase 2 research before building on it.** The claims that NRW
  LiDAR is 25cm to 2m under OGL covering about 70 percent of Wales, and that
  Dwr Cymru will not sell sewer GIS at any price, came from research in
  session and no test can check them.
- Property and planning records were judged a separate tool: address-keyed
  rather than geometry-keyed. EPC bulk data, Land Registry INSPIRE polygons,
  council tax bands. No national Welsh planning feed exists.

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
