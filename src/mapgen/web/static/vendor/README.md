# Vendored third-party files

Leaflet and MapLibre are committed here rather than loaded from a CDN, so the
map picker keeps working offline and cannot break because a CDN changed or
went down.

| File | Version | Source | SHA256 |
| --- | --- | --- | --- |
| `leaflet.js` | 1.9.4 | `https://unpkg.com/leaflet@1.9.4/dist/leaflet.js` | `db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a` |
| `leaflet.css` | 1.9.4 | `https://unpkg.com/leaflet@1.9.4/dist/leaflet.css` | `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6` |
| `maplibre-gl.js` | 5.24.0 | `https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.js` | `45a9b07a9189ce56054c620a947ccf41e291e58c95e9b61533b740aaa65ee5cb` |
| `maplibre-gl.css` | 5.24.0 | `https://unpkg.com/maplibre-gl@5.24.0/dist/maplibre-gl.css` | `ab1e70d59ec40465bae7e7030da2f3ccf28133fd502e62bd598eefbadfd7a732` |
| `maplibre-gl.LICENSE.txt` | 5.24.0 | `https://unpkg.com/maplibre-gl@5.24.0/LICENSE.txt` | `ee5fc05a0677eaf69601d2c7db0d9ecd6cc27c3abc1d0733bc9ed34707cf8ef2` |
| `leaflet-maplibre-gl.js` | 0.1.4 | `https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.1.4/leaflet-maplibre-gl.js` | `1e6cf8cb3eb5fd909879aa1bf36a383fb506c9a5b2dbbfababce65a294dd1fcb` |
| `leaflet-maplibre-gl.LICENSE` | 0.1.4 | `https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.1.4/LICENSE` | `eaa721ba158cbeff47ad53b1035dfc26ff744df66662c93ff715c9885197ebf3` |

Leaflet's marker icon PNGs are not vendored, and the interface is written so
that it never needs them. It draws rectangles, and the four handles on the
drawn extent are markers with a DIV icon (`L.divIcon`), which is a styled
element rather than an image. Anything added here that used the default
`L.Icon` would request `marker-icon.png` from this folder and get a 404, so a
new marker needs either its own div icon or those files vendored alongside
these two.

## The vector basemap

MapLibre GL JS draws the OpenFreeMap "Bright" vector map, and
`@maplibre/maplibre-gl-leaflet` hosts it as an ordinary Leaflet layer, so
every rectangle, handle and tile-grid shape the page draws stays Leaflet code.
MapLibre is BSD-3-Clause and the bridge is ISC; both licence texts sit beside
their files, as both licences require for redistribution.

MapLibre is held at 5.24.0 deliberately. From 6.0 it ships only as ES
modules with a separate worker module, and the bridge reads a classic
`maplibregl` global; 5.24.0 is the newest single-file build. Each file above
was checked against the SHA-256 integrity value the npm registry publishes for
it before it was written here.

## Verifying

```powershell
Get-FileHash src\mapgen\web\static\vendor\* -Algorithm SHA256
```

These hashes are of the exact bytes committed, and `.gitattributes` marks this
directory `-text` so a checkout reproduces them. That rule is load bearing:
upstream ships `leaflet.css` with CRLF line endings and `leaflet.js` with LF,
and this repository is developed on Windows with `core.autocrlf=true`, which
rewrites line endings on checkout unless told not to. Without the rule the CSS
comes back out of a fresh clone with different bytes and a different hash.
