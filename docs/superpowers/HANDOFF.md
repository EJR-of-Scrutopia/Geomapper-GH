# mapgen handoff, paused 2026-08-03

Read this first when resuming. It is tracked in git deliberately, because the
detailed ledger is not.

## Where the work is

- Worktree: `C:\Users\Param\mapgen-phase1`, branch `feat/phase1`
- Main worktree: `.../VS code/Rhino Plugins/mapgen`, branch `main` at `034137b`
- Remote: `https://github.com/EJR-of-Scrutopia/Geomapper-GH.git`
- **94 commits unpushed. Nothing has ever been pushed.**
- Run tests: `.venv\Scripts\python.exe -m pytest` and `node tests/js/test_app.js`
- Last known green: 561 Python (1 deselected, the live network test), 66 Node

## The ledger, and why it matters

`.superpowers/sdd/2026-08-01-mapgen-phase1/progress.md` holds roughly 300 lines
recording every defect, every ruling and every reason. It is the memory of how
this was built and why decisions went the way they did.

**It is gitignored.** A `git clean -fdx` destroys it. Read it before doing
anything non-obvious, and consider committing it if the work continues.

Other artefacts beside it: per-task briefs, per-task reports, review diffs.

## State when paused

Phase 1 is complete and the tool works. A real survey runs end to end, writes
`<Site>_<date>.osm` and a `survey.json` that describes itself accurately, and
survives the Urbano bridge failing.

**A fix round was RUNNING in the background when this paused.** Check
`git log` for commits after `3da4bbe` before assuming it did not land. It was
closing these, all found by the final whole-branch review:

1. **Critical.** An unrecognised `--category` id silently produces an empty
   package reporting `complete: true`. `--category building`, where the valid
   id is `buildings`, gave an 85-byte `.osm` with 0 nodes against 1.2 MB
   unfiltered. An architect would conclude the site is empty. Needs validation
   of category ids where the request is built.
2. **Critical.** Deselecting every category makes Overture fetch all eight
   types, because `list(types or DEFAULT)` treats `[]` as unset. Select
   nothing, get everything. One-line fix, nothing pins the current behaviour.
3. **Important.** Resume isolates a changed tiling but not a changed content
   selection: `tiling_fingerprint` covers bbox, tile size and overlap, and
   categories arrived later. A resumed run after changing categories mixes old
   and new data while `survey.json` states the new selection. Fold the
   effective categories and Overture types into the fingerprint.
4. **Important.** Source failures reach the CLI as raw tracebacks; `cli.py`
   catches only four exception types. The browser path is clean.
5. Honesty fixes: the node-cap message advises tile sizes already tried; the
   settings hint promises the retry unconditionally though it is disabled at
   or below 1000 m; stale `filtering_caveat` cross-references; a false README
   claim about one file per Overture type.

## What needs the owner, not an agent

1. **Browser test.** Settings panel, category checklist, draw interaction,
   typeahead. Launch from the desktop `mapgen` shortcut, or
   `.venv\Scripts\mapgen.exe ui` from a terminal.
2. **Rhino test.** Run a survey on a site they know and open it. Nothing in
   this repo has verified the geometry is where an architect expects.
3. **Urbano.** `Urbano.Core.dll` and `ProjectSetup.dll` exist nowhere on this
   machine, so `tools/UrbanoBridge` has never run against a real install and
   `_project_setting.json` is untested as an actual Urbano input. Task 2's
   gate is still open.

## Then

- Push to origin. `main` fast-forwards cleanly.
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
attribution anywhere. No em dashes, including interface copy.

## The lesson worth carrying

The dominant defect across every task was a test that passed for the wrong
reason, almost always a test double more permissive than the real thing. The
method that worked was mutation: break the behaviour, confirm the named test
fails, restore, confirm the diff is clean. Three separate controls in this
tool appeared connected and were not. Assume there is another.
