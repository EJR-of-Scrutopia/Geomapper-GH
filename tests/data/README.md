# Test data: two real DEMs, and Urbano's own answers about them

Nothing in this folder was constructed for a test. Every file is either a
real survey product or the recorded output of Urbano's own code reading one.

## The GeoTIFFs

| file | survey | pixels |
| --- | --- | --- |
| `Barry-Full_2026-08-04.tif` | Barry Full, the task 35 test package | 29 x 18 |
| `Porthcawl_2026-08-04.tif` | Porthcawl, the owner's own package | 245 x 109 |

Both are OpenTopography COP30 downloads made by `mapgen survey` on
2026-08-04, copied here byte for byte. They are the reason
`src/mapgen/geotiff.py` is as narrow as it is: little endian classic TIFF,
32 bit IEEE float samples, one sample per pixel, LZW compressed with no
predictor, one 256 by 256 tile, `ModelPixelScale` and `ModelTiepoint` at a
thirtieth of an arc minute, WGS84, `RasterPixelIsPoint`, and no
`GDAL_NODATA` tag at all.

**Copernicus DEM, free for any use with attribution.**
(c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH.

## Urbano's own answers

`urbano_samples_barry.txt` and `urbano_samples_porthcawl.txt` are
`Urbano.Core.Helpers.TiffExtensions.ReadTiffFile.CreateDemSourceFromTiff`
run on the two files above, out of process, against a copy of Urbano
2.2.1.2's `Urbano.SiteAnalysis.gha` (task 39, Probe9). 169 points each,
spread across and beyond each survey's own extent so that the "cannot
sample here" answers are in the table as well as the heights.

One row per point:

    latitude longitude has_coverage could_sample height

`height` is `-` where Urbano said it could not sample there. Where it could,
the value is what its `TryGetElevation` returned, printed round trippably,
and `tests/test_geotiff.py` asserts mapgen reproduces it to the last bit of
a single precision float rather than approximately.

`urbano_grid_barry.txt` and `urbano_grid_porthcawl.txt` are the same
assembly's `ElevationExtensions.BuildElevationGridFromTiff` on the same two
files, with the padding and options `ProjectSettingComponent` uses when it
writes a `.egrid`: the header it produced, and every one of its heights.
`tests/test_egrid.py` builds the same grid in Python and compares the two
value by value.
