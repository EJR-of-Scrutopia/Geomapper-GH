# Grasshopper scripts

Standalone scripts for working with mapgen packages inside Grasshopper.
Nothing here is part of the pipeline; each file is pasted into a GH
component and documents itself.

## lidar_to_mesh.py

Turns a package's `_lidar_dtm.tif` or `_lidar_dsm.tif` into a terrain
mesh, standard library only, no Heron or other plugins.

Setup (once):

1. In Rhino 8 Grasshopper, drop a **Python 3** component on the canvas.
2. Paste the whole of `lidar_to_mesh.py` into it.
3. Add inputs named `path` (str), `step` (int), `frame` (str),
   `mapgen_src` (str), `at_origin` (bool), and outputs `mesh`, `info`,
   `origin`. Only `path` is required; the file's docstring explains
   each.

Use:

- `path` = the tif inside the package folder.
- `step` = 2 to start (every 2nd sample); 1 is native resolution and
  can be millions of vertices over a whole town.
- `frame` = `urbano` (default) puts the mesh in the same absolute UTM
  metres Urbano places the package's layers, so everything aligns; it
  needs `mapgen_src` pointing at mapgen's `src` directory. `bng` gives
  raw British National Grid metres instead.
- Holes in the LiDAR stay holes in the mesh; nothing is interpolated.

The DSM minus the DTM, both meshed the same way, is roofs, trees and
hedges as built form; the DTM alone is the bare ground the contours
came from.

Self-test without Rhino (prints stats, builds no geometry):

```
python docs/grasshopper/lidar_to_mesh.py <path-to-tif> 4 urbano <mapgen-src>
```

Verified 2026-08-07 against the real Cowbridge package: 2,579 x 1,647
samples at 2 m, 265,740 data points at step 4, z 15.99 to 135.07 m,
zone 30U, worst frame-transform interpolation error 0.7 mm.
