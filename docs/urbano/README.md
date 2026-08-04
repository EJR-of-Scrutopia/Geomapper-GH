# Testing the Urbano project setting by hand

`sample_project_setting.json` beside this file was produced by **Urbano's own
serialiser**, not written by hand and not guessed. A throwaway .NET probe
loaded `Urbano.SiteAnalysis.gha` out of process, constructed a real
`WorldOrigin` and `UrbanoProjectSetting` for a London bounding box, and called
its `ToJson()`. It is property for property what
`tools/UrbanoBridge/Program.cs` already writes, which is how we know mapgen's
JSON shape was correct all along.

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

## Why the bridge executable may not be needed at all

`ProjectSettingComponent` rebuilds every data path as
`Folder + "\" + FileNameStr + <extension>` and **skips any file already on
disk**. mapgen's naming already matches that exactly. So a mapgen package plus
a correct project setting JSON turns the component into a no-download
pass-through, with no `UrbanoBridge` executable involved.

Three of the bridge's engine steps are US only, and the traveller model it
wants (`URBANO_TravelerClassifier.onnx`) is not on this machine. For UK work
the smaller shape is plausibly the better one. That decision has not been
made.

## If you want the bridge instead

The retarget is four edits in `tools/UrbanoBridge/Program.cs`: find the `.gha`
rather than two DLLs, load one assembly rather than two, rename five types
from `Urbano.Core.Process.*` to `Urbano.Core.Helpers.*`, and delete
`ProjectSetup.Overture`, which no longer exists in 2.2.1.2. Plus
`_MISSING_URBANO_RE` in `src/mapgen/bridge.py`.

Full evidence, including how the assembly was read and why two earlier
attempts reached wrong answers, is in
`.superpowers/sdd/2026-08-01-mapgen-phase1/task-34-report.md`.
