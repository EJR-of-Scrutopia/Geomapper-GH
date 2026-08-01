# OSM + Overture tiling workflow

Use [osm_overture_tiles.py](./osm_overture_tiles.py) to split a study area into overlapping tiles, download each tile, and merge the results back into deduplicated outputs.

By default it now writes standard `.osm` XML for OSM and standard `.geojson` FeatureCollections for Overture, which is a better fit for importers that do not understand Overpass JSON or GeoJSON sequence.

The script expects bbox order `west,south,east,north`. Your study area, normalised to that order, is:

```text
-3.6626,51.3709,-3.1483,51.5476
```

That extent is roughly `35.6 km x 19.7 km`, so a practical starting point is `5000 m` tiles with `250 m` overlap. That gives `32` tiles.

## 1. Plan the grid

```powershell
python osm_overture_tiles.py plan `
  --bbox=-3.6626,51.3709,-3.1483,51.5476 `
  --tile-size-m 5000 `
  --overlap-m 250 `
  --output-dir data\south-wales
```

## 2. Smoke-test one tile

```powershell
python osm_overture_tiles.py download `
  --bbox=-3.6626,51.3709,-3.1483,51.5476 `
  --tile-size-m 5000 `
  --overlap-m 250 `
  --output-dir data\south-wales-smoke `
  --source osm `
  --source overture `
  --overture-type building `
  --overture-type place `
  --tile-id r02_c05
```

## 3. Run the full download

```powershell
python osm_overture_tiles.py download `
  --bbox=-3.6626,51.3709,-3.1483,51.5476 `
  --tile-size-m 5000 `
  --overlap-m 250 `
  --output-dir data\south-wales `
  --source osm `
  --source overture `
  --overture-type building `
  --overture-type place `
  --overture-type segment `
  --overture-type connector `
  --overture-type infrastructure `
  --overture-type land_use `
  --overture-type land_cover `
  --overture-type water
```

## 4. Merge the tiles

```powershell
python osm_overture_tiles.py merge --output-dir data\south-wales
```

Outputs:

- `data\south-wales\manifest.json`
- `data\south-wales\raw\osm\*.osm`
- `data\south-wales\raw\overture\<type>\*.geojson`
- `data\south-wales\merged\osm\all.osm`
- `data\south-wales\merged\overture\<type>.geojson`

## 5. Build a Urbano project folder

Use `urbano-package` when you want a template-style project folder with:

- `OsmFilePath`
- `BlockFilePath`
- `ElevationFilePath`
- `OvertureFilePath`
- `<stem>_project_setting.json`

If you omit `--output-dir`, the command creates a timestamped folder directly under the downloader folder.

```powershell
python osm_overture_tiles.py urbano-package `
  --bbox=-74.0234,40.6998,-74,40.7167
```

You can also target a specific folder:

```powershell
python osm_overture_tiles.py urbano-package `
  --bbox=-74.0234,40.6998,-74,40.7167 `
  --output-dir data\manhattan-urbano
```

That command writes files like:

- `20260314_214500\40.7167_40.6998_-74_-74.0234.osm.pbf`
- `20260314_214500\40.7167_40.6998_-74_-74.0234.blocks`
- `20260314_214500\40.7167_40.6998_-74_-74.0234.egrid`
- `20260314_214500\40.7167_40.6998_-74_-74.0234_overture.parquet`
- `20260314_214500\40.7167_40.6998_-74_-74.0234_project_setting.json`

For non-U.S. fallback runs with `--skip-blocks --skip-elevation --skip-climate`, the final folder now keeps only the top-level package files by default. Use `--keep-tilework` if you also want the temporary tile download folder preserved.

Notes:

- Standard `.osm` XML downloads use the OSM map API, which has a `50000` node limit per request. If you hit that in dense areas, lower `--tile-size-m` to around `1500` to `2000`.
- JSON-mode OSM downloads use the public Overpass API, so they are best-effort. The script slows requests down and retries transient failures.
- The merge step deduplicates OSM by `type/id` and Overture by feature `id`, which is enough for overlapping tile seams.
- Use `--tile-id` to rerun specific failed tiles without touching the rest of the job.
- `--osm-format json` and `--overture-format geojsonseq` are still available if you need the old intermediate formats.
- If Overpass becomes the bottleneck, the next step is to switch the OSM source to a regional `.pbf` extract and clip locally with `osmium` or DuckDB.
- The native Urbano `.blocks`, climate, and `.egrid` steps are U.S.-specific because they use U.S. Census, USA EPW mapping, and USGS 3DEP. For non-U.S. areas, keep using the tile `plan/download/merge` workflow or call `urbano-package` with `--skip-blocks --skip-elevation --skip-climate`.
- `OvertureFilePath` is written as geoparquet because Urbano's own `ProjectSetup` path expects parquet. The bridge falls back to your installed `overturemaps` CLI when the packaged Overture release path is stale.
