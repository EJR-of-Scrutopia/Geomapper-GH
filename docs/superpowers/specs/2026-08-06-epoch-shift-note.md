# The WGS84/ETRS89 epoch shift, and the roads-vs-terrain offset

Investigation only. Nothing in `src/` changes because of this note.

## The number, derived

ETRS89 is defined by EUREF as fixed to the Eurasian tectonic plate at
epoch 1989.0: a point's ETRS89 coordinate does not change as the plate
moves, by construction. WGS84 and ITRF are geocentric frames that do not
co-rotate with any one plate, so a physical point that is fixed in
ETRS89 drifts, in WGS84/ITRF terms, at the plate's own velocity. The
published Eurasian plate motion (ITRF plate motion models, repeated in
Ordnance Survey's own guidance on ETRS89 vs WGS84) is about 2.5 cm/year,
in a broadly north-easterly direction over Great Britain's latitudes.

Years elapsed to 2026: 2026.0 minus 1989.0 is 37 years.
Accumulated drift: 37 years times 2.5 cm/year = 92.5 cm, about 0.9 m.

Be honest about the rate: published plate motion solutions differ by a
few tenths of a mm/year depending on the ITRF realisation and the GNSS
stations used to fit it, so treat 0.9 m as good to about a decimetre,
not the millimetre. The bearing is commonly cited in the range N50E to
N65E; decomposed on a representative N55E, 0.9 m splits into roughly
0.5 m north and 0.75 m east. Treat that split as indicative of scale and
direction, not a surveyed fact.

## Which mapgen outputs it affects

`to_bng`/`from_bng` in `src/mapgen/bng.py` are, by their own docstrings,
an ETRS89-to-BNG transform: `tm_forward`/`tm_inverse` plus the OSTN15
shift, which is defined for genuine ETRS89 input. Everything that
enters or leaves the package through that pair sits in the true ETRS89
frame: LiDAR height sampling, contour generation, and the INSPIRE
boundary curves all read or write BNG metres through `to_bng`/`from_bng`.

OSM and Overture geometry never passes through that pair at all. It
arrives, and stays, in whatever frame its own source data is in, which
for OSM is effectively current-epoch WGS84: most OSM geometry traces
back to modern GPS fixes, which read in ITRF/WGS84-now, not ETRS89-1989.

So inside one mapgen package there are two coordinate families that
agree to within survey tolerance in 1989 and have been drifting apart
ever since: LiDAR-derived terrain, contours and INSPIRE boundaries in
ETRS89(1989.0), and OSM/Overture roads, paths and buildings in
WGS84(now). The gap between them is the number above, about 0.9 m,
roughly north-east.

## What the owner reported, and what to check

The owner reported roads and paths sitting slightly offset from the
LiDAR-derived terrain, direction unstated. Two things distinguish the
epoch effect from ordinary digitising noise:

- **Consistency.** The epoch shift is one vector for the whole package,
  so every road should be offset from the terrain by the same distance
  and the same bearing, everywhere in the extent. A shift that varies
  in size or direction between roads is not this effect.
- **Scale and bearing.** About a metre, and roughly north-east versus
  south-west (terrain/contours/boundaries sit south-west of where OSM
  places the same physical edge, since ETRS89 lags WGS84-now by the
  drift). A shift the wrong way round, or several metres, is not this
  effect either.

The honest confounder: OSM's own digitising and GPS-trace error is also
metre-scale in places, but it is not systematic. It varies way to way,
node to node, contributor to contributor. Only the part of the offset
that is uniform across the package, in size and direction, is a
candidate for the epoch shift; the rest is ordinary OSM noise and no
coordinate fix will remove it.

## The proposed fix, if confirmed

One constant NE drift vector, applied inside `to_bng`/`from_bng`: add it
to the coordinate on the way into the OSTN15 pipeline, subtract it on
the way out, so LiDAR/contour/boundary geometry is reported in OSM's
current-epoch frame instead of ETRS89(1989.0). Behind a config flag
(alongside the existing preferences in `src/mapgen/config.py`),
defaulting OFF.

Cost: a few lines in `bng.py` plus a re-run of an existing package to
see the effect. Risk: this moves mapgen's BNG output away from what a
professional surveyor's OSGB36-fix workflow would report for the same
site, since that workflow stays in the true ETRS89/OSGB36 frame on
purpose. It also needs the sign and bearing checked against a known
coordinate pair before it ships live, since getting either backwards
widens the gap instead of closing it. That is why it is owner-gated
rather than defaulted on.

## The decision

Switch it on, keep it off, or park it until the owner can A/B a real
package (flag on vs off, same extent, same terrain) and confirm the
offset is the size, direction and consistency described above before
either the config default or this note changes.
