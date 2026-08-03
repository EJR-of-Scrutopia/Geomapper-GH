# mapgen handoff, updated 2026-08-03

Read this first when resuming. It is tracked in git deliberately, because the
detailed ledger is not.

## Where the work is

- Worktree: `C:\Users\Param\mapgen-phase1`, branch `feat/phase1`
- Main worktree: `.../VS code/Rhino Plugins/mapgen`, branch `main` at `034137b`
- Remote: `https://github.com/EJR-of-Scrutopia/Geomapper-GH.git`
- **94 commits unpushed (`git rev-list --count 034137b..HEAD`). Nothing has
  ever been pushed.**
- Run tests: `.venv\Scripts\python.exe -m pytest` and `node tests/js/test_app.js`
- Last known green: 592 Python (1 deselected, the live network test), 66 Node

## The ledger, and why it matters

`.superpowers/sdd/2026-08-01-mapgen-phase1/progress.md` holds the memory of
how this was built and why decisions went the way they did.

**It is gitignored.** A `git clean -fdx` destroys it. Read it before doing
anything non-obvious, and consider committing it if the work continues.

Other artefacts beside it: per-task briefs, per-task reports, review diffs.

## State when paused

Phase 1 is complete and the tool works. A real survey runs end to end, writes
`<Site>_<date>.osm` and a `survey.json` that describes itself accurately, and
survives the Urbano bridge failing.

**The fix round this note used to describe as running has landed.** Commits
`3da4bbe`, `3a68ee4`, `ee6480c`, `b09eb38` (after `3da4bbe`, the two free
fixes, see `git log`) close everything the final whole-branch review raised:

1. **Critical, fixed.** An unrecognised `--category` id is now rejected at
   `SurveyRequest` construction, listing the valid ids, before any source is
   touched. Proven live: the same `--category building` typo that used to
   produce an 85-byte package now fails immediately with no package written.
2. **Critical, fixed.** `OvertureSource` now only defaults to all eight types
   when `types` is `None`; an empty selection (`--category rail` alone, or
   unticking everything) genuinely fetches nothing. Proven live: `--source
   overture --category rail` completes in 0.2 seconds with zero
   `overturemaps` calls.
3. **Important, fixed.** `tiling_fingerprint` now hashes the resolved
   category and Overture type selection alongside bbox/tile size/overlap, so
   a resumed job with a changed selection gets its own `_work/` directory
   instead of mixing old and new tiles. Proven live with two real Overpass
   runs against the same package root: different fingerprints
   (`fc734627` then `7e467c3b`), and the final `.osm` file contained only
   the second run's own category, not a mix.
   **Residual, now CLOSED** (the owner said continue, so the coordinator
   made the design call and fixed it inline): each source declares
   `possible_outputs(stem)`, the closed list of package-root files it
   could ever merge for that stem, and a COMPLETE run sweeps any of those
   names it did not itself produce, emitting `stale_output_removed` per
   file. User files can never match the closed list, survey.json and the
   Urbano bridge artifacts are not source outputs and are left alone, and
   an incomplete run keeps everything until a resume finishes the job.
   Pinned by three tests in test_package.py plus one per source;
   mutation-verified (sweep disabled fails exactly
   test_a_complete_run_sweeps_stale_merged_outputs_and_nothing_else).
4. **Important, fixed.** `cli.py` now catches `OsmDownloadError`,
   `OvertureError`, `ElevationError` and `UnknownCategoryError` alongside
   the original four, so a source failure prints one clean line instead of
   a raw traceback. The browser path was already clean.
5. **Honesty fixes, done.** The node-cap message no longer names tile sizes
   that may already have failed; the settings hint states the retry
   ladder's actual 1000 m floor; `filtering_caveat` cross-references are
   `routing_note`; the README no longer claims exactly one file per
   requested Overture type.

Everything above was verified by mutation (break the fix, confirm the named
test fails for the stated reason, restore, confirm the diff is clean) and,
for the three real-run requirements, by an actual `mapgen.exe` invocation,
not pytest alone.

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
4. **The push.** The permission layer on this machine denies `git push`
   from the assistant's shells, so the owner runs it themselves:
   `cd C:\Users\Param\mapgen-phase1` then
   `git push -u origin main feat/phase1`. Local `main` is already
   fast-forwarded to the same commit; the remote's initial commit is the
   local history's root, so the push is a plain fast-forward.

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
