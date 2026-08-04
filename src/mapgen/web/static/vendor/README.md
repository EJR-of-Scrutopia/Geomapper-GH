# Vendored third-party files

Leaflet is committed here rather than loaded from a CDN, so the map picker
keeps working offline and cannot break because a CDN changed or went down.

| File | Version | Source | SHA256 |
| --- | --- | --- | --- |
| `leaflet.js` | 1.9.4 | `https://unpkg.com/leaflet@1.9.4/dist/leaflet.js` | `db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a` |
| `leaflet.css` | 1.9.4 | `https://unpkg.com/leaflet@1.9.4/dist/leaflet.css` | `a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6` |

Leaflet's marker icon PNGs are not vendored, and the interface is written so
that it never needs them. It draws rectangles, and the four handles on the
drawn extent are markers with a DIV icon (`L.divIcon`), which is a styled
element rather than an image. Anything added here that used the default
`L.Icon` would request `marker-icon.png` from this folder and get a 404, so a
new marker needs either its own div icon or those files vendored alongside
these two.

## Verifying

```powershell
Get-FileHash src\mapgen\web\static\vendor\leaflet.js -Algorithm SHA256
Get-FileHash src\mapgen\web\static\vendor\leaflet.css -Algorithm SHA256
```

These hashes are of the exact bytes committed, and `.gitattributes` marks this
directory `-text` so a checkout reproduces them. That rule is load bearing:
upstream ships `leaflet.css` with CRLF line endings and `leaflet.js` with LF,
and this repository is developed on Windows with `core.autocrlf=true`, which
rewrites line endings on checkout unless told not to. Without the rule the CSS
comes back out of a fresh clone with different bytes and a different hash.
