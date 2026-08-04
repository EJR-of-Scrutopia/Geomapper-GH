"""Writing the Urbano 2 project setting file, without Urbano.

`<stem>_project_setting.json` is the one file Urbano 2 is pointed at. Until
this module existed it was written in exactly one place, the C# UrbanoBridge,
which has never once succeeded on the owner's machine, so the file had never
once been produced. A real survey was taken into Grasshopper and failed
because the only JSON in the folder was mapgen's own `survey.json`, which
means nothing to Urbano.

Nothing here loads an Urbano assembly, starts a process or needs Urbano
installed. The format is Urbano's, established in task 34 from its own
serialiser, and the coordinate reference is computed by mapgen.utm.

## What Urbano does with this file, which is what every decision here is for

Two routes read it, both decompiled and read rather than guessed.

**The Project Setting component.** Its text input takes the file's contents.
`SolveInstance` parses them, then works out which layers to make sure of from
two things unioned: the `Layers` list, and every file path field that is not
an empty string. It then rebuilds each data path itself, as
`Folder + "\\" + FileNameStr + <its own fixed extension>`, IGNORING the path
strings in the file, and downloads anything not already there.

Two consequences run through this module:

  * `FileNameStr` must be mapgen's own stem. It is what Urbano rebuilds
    every path from, so a stem of `Barry-Waterfront_2026-08-03` is what makes
    Urbano find `Barry-Waterfront_2026-08-03.osm` already on disk and skip
    the download. Urbano's own convention for this field is a coordinate
    string, and nothing validates the two against each other: the only
    identity check `UrbanoProjectSetting.TryLoad` makes is on the bound
    string rebuilt from `Top`, `Bottom`, `Right` and `Left`, never on
    `FileNameStr`.
  * Naming a layer commits Urbano to having it. An elevation layer with no
    `<stem>.egrid` beside it sends Urbano to the USGS 3DEP service, which is
    United States only, and it reports the failure on the component. That is
    a visible, honest warning rather than a silent absence, which is the
    right side of this project's own rule, but it is worth knowing before it
    is met.

**Casting the text straight into a ProjectSettingParam**, which is what the
Deserialize Project Setting component does. That route passes the path
strings through verbatim, and is where pointing them at the files mapgen
actually produced pays: `ElevationFilePath` and `OvertureFilePath` come out
of it as real paths to the real GeoTIFF and GeoJSON in the package.

## Never a NaN and never an infinity

Both readers other than the text input use default `JsonSerializer` options,
which reject named float literals outright, so one NaN anywhere breaks the
file. `json.dumps` is called with `allow_nan=False` for that reason and every
number is checked before it gets there. A number that cannot be computed is a
failure to report, not a value to write.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from mapgen.geo import BBox
from mapgen.utm import ProjectionError, project, utm_zone

PROJECT_SETTING_SUFFIX = "_project_setting.json"

# Urbano's three, read off Key.BLOCK_GRANULARITY_* in task 34. Only the first
# is ever used here; the other two are recorded so a caller passing one of
# them is not silently refused by a check that has never heard of them.
GRANULARITIES = ("Block", "Block Group", "Tract")
DEFAULT_GRANULARITY = "Block"

# Urbano's own layer names, read off Key.PROJECTSETTING_DATA_LAYER_NAME_*,
# in the order it lists them. `climate` is deliberately never written: it is
# the one layer Urbano re-fetches unconditionally, with no file check at all,
# and it is a United States only EPW lookup.
LAYER_OSM = "osm"
LAYER_CENSUS = "census"
LAYER_ELEVATION = "elevation"
LAYER_OVERTURE = "overture"
LAYER_ORDER = (LAYER_OSM, LAYER_CENSUS, LAYER_ELEVATION, LAYER_OVERTURE)


class ProjectSettingError(RuntimeError):
    """Raised when a usable project setting cannot be produced.

    A RuntimeError rather than a ValueError, matching IncompleteSurveyError
    in mapgen.package: this is never the owner asking for something
    impossible, it is the work having been done and something about the
    result not being fit to write down.

    Callers in mapgen.package catch it and record it, exactly as they do a
    bridge failure. A survey's real data is never discarded because its
    project setting could not be written.
    """


@dataclass(frozen=True)
class DataFile:
    """One of Urbano's four file path fields, and the layer it implies."""

    layer: str
    field: str
    # In preference order. The first suffix that exists on disk wins.
    suffixes: tuple[str, ...]


# Why each list is in the order it is:
#
# osm: `.osm` before `.osm.pbf` because that is the order Urbano's own
#   component checks them in, so the file mapgen names is the file Urbano
#   would have chosen anyway. mapgen writes `.osm`.
# census: mapgen never produces one. Listed so a `.blocks` left by a
#   successful bridge run is picked up rather than ignored.
# elevation: `.egrid` first, because if the bridge ever does succeed its
#   output is the format Urbano reads natively, and `.tif` after it, because
#   that is what mapgen actually produces and it is a real path to a real
#   DEM for anything that reads the field rather than rebuilding it.
# overture: the bridge's `.parquet` first for the same reason, then the
#   buildings GeoJSON, because building heights are the one thing Urbano is
#   known to read Overture for (OVERTURE_KEY_BLDG_HEIGHT). A package with no
#   buildings falls back to whichever other type it holds, sorted, so the
#   field is a real file rather than a guess. Urbano's own download routine
#   handles no Overture layer at all, so this field only ever reaches the
#   components that read it verbatim.
DATA_FILES = (
    DataFile(LAYER_OSM, "OsmFilePath", (".osm", ".osm.pbf")),
    DataFile(LAYER_CENSUS, "BlockFilePath", (".blocks",)),
    DataFile(LAYER_ELEVATION, "ElevationFilePath", (".egrid", ".tif")),
    DataFile(LAYER_OVERTURE, "OvertureFilePath", ("_overture.parquet", "_building.geojson")),
)


def _absolute(path: Path) -> Path:
    """The same rule mapgen.naming.check_path_length uses: resolve only what
    is not already absolute, so an absolute path is never perturbed by a
    round trip through the filesystem.
    """
    return path if path.is_absolute() else path.resolve()


def world_origin(bbox: BBox) -> dict[str, object]:
    """Urbano's `CoordinateReference`: a zone string and the two bottom
    corners of the extent, projected.

    One deliberate difference from Urbano, and it is the only one in this
    module. `WorldOrigin(left, bottom, right)` projects each corner into
    ITS OWN zone and sets `Utm` twice, so the right corner's zone is the one
    that survives. For any extent inside a single zone, which is every real
    survey and both reference fixtures, that is identical to what this
    function does. For an extent that crosses a zone boundary, and the
    30/31 boundary runs through London at the prime meridian, Urbano's
    answer is incoherent: the two eastings are measured from different
    central meridians while `Utm` names only one of them, and `Utm` is what
    every downstream component projects everything else with. The two
    corners would be several hundred kilometres apart on the grid Urbano
    then uses.

    So both corners are projected into ONE zone here, and that zone is the
    bottom right corner's, which is the one Urbano's own last assignment
    keeps. mapgen.utm.project takes the zone precisely so this can be done:
    a point past a zone's nominal 6 degree width is an ordinary extended
    coordinate on that zone's grid, not an error.

    The result agrees with Urbano wherever Urbano is self-consistent, and is
    self-consistent where Urbano is not. That is the only defensible place
    to differ, and the report for task 35 records it as a difference rather
    than leaving it to be discovered.
    """
    zone = utm_zone(bbox.south, bbox.east)
    left_easting, left_northing = project(bbox.south, bbox.west, zone)
    right_easting, right_northing = project(bbox.south, bbox.east, zone)
    return {
        "Utm": zone,
        "BottomLeftUtm": {"Item1": left_easting, "Item2": left_northing},
        "BottomRightUtm": {"Item1": right_easting, "Item2": right_northing},
    }


def resolve_data_files(root: Path, stem: str) -> dict[str, Path]:
    """Which of Urbano's four data files this package actually holds, keyed
    by layer name.

    Read off the disk rather than from survey.json's source list, because
    the question this answers is "what is there", and a layer that was
    requested and found nothing leaves no file (see OsmSource.merge and the
    ruling it cites). A path Urbano cannot open is worse than a field left
    empty: it commits Urbano to a layer and then fails.

    The Overture fallback, and only that one, looks at more than a fixed
    name: a package holds one `<stem>_<type>.geojson` per Overture type and
    the field takes one string, so buildings are preferred and anything else
    the package holds is used in sorted order if there are no buildings.
    """
    found: dict[str, Path] = {}
    for data_file in DATA_FILES:
        for suffix in data_file.suffixes:
            candidate = root / f"{stem}{suffix}"
            if candidate.is_file():
                found[data_file.layer] = candidate
                break
        else:
            if data_file.layer == LAYER_OVERTURE:
                others = sorted(
                    path
                    for path in root.glob(f"{stem}_*.geojson")
                    if path.is_file()
                )
                if others:
                    found[data_file.layer] = others[0]
    return found


def build_project_setting(
    bbox: BBox,
    root: Path,
    stem: str,
    granularity: str = DEFAULT_GRANULARITY,
) -> dict[str, object]:
    """The project setting for a package, as a dict ready to serialise.

    Key order is Urbano's own property order, which is what its serialiser
    produces, so a mapgen file and an Urbano file can be read side by side.

    Refuses rather than writes a file describing nothing: a package with
    none of Urbano's four data files in it has nothing for the setting to
    point at, and a project setting naming no data is exactly the shape of
    the failure this whole module exists to remove.
    """
    if granularity not in GRANULARITIES:
        raise ProjectSettingError(
            f"{granularity!r} is not an Urbano granularity. Use one of: "
            f"{', '.join(GRANULARITIES)}."
        )
    root = _absolute(Path(root))
    files = resolve_data_files(root, stem)
    if not files:
        raise ProjectSettingError(
            f"No file Urbano can read was found in {root} for {stem}, so a "
            f"project setting would name nothing. The survey has to produce at "
            f"least one layer before Urbano has anything to open."
        )

    try:
        reference = world_origin(bbox)
    except ProjectionError as exc:
        raise ProjectSettingError(
            f"The Urbano coordinate reference could not be computed for this "
            f"extent: {exc}"
        ) from None

    for value, name in (
        (bbox.north, "north"), (bbox.south, "south"),
        (bbox.west, "west"), (bbox.east, "east"),
    ):
        # BBox.validated already refuses a NaN bound, since every one of its
        # comparisons is false for one. Checked again here because this is
        # the last point before the number reaches a file that breaks both
        # of Urbano's readers if it is not real, and because a BBox can be
        # constructed directly without going through validated().
        if not math.isfinite(value):
            raise ProjectSettingError(
                f"The {name} edge of this extent is {value}, which Urbano "
                f"cannot read. A project setting must carry real bounds."
            )

    return {
        "Folder": str(root),
        "FileNameStr": stem,
        "Granularity": granularity,
        "CoordinateReference": reference,
        "Top": bbox.north,
        "Bottom": bbox.south,
        "Left": bbox.west,
        "Right": bbox.east,
        **{
            data_file.field: str(files[data_file.layer]) if data_file.layer in files else ""
            for data_file in DATA_FILES
        },
        "Layers": [layer for layer in LAYER_ORDER if layer in files],
    }


def project_setting_path(root: Path, stem: str) -> Path:
    """`<root>/<stem>_project_setting.json`.

    The suffix is established by black-box experiment against Urbano's own
    `TryLoad` (task 34): the search pattern is `*_project_setting.json`, case
    insensitively, and the separating underscore is required.
    """
    return Path(root) / f"{stem}{PROJECT_SETTING_SUFFIX}"


def write_project_setting(
    bbox: BBox,
    root: Path,
    stem: str,
    granularity: str = DEFAULT_GRANULARITY,
) -> Path:
    """Write the project setting for a package, returning the path written.

    Uses mapgen.fsutil.atomic_write_text for the same reason survey.json
    does: a half-written file in this position is a file Grasshopper is
    pointed at and cannot parse, and this one is written at the end of a run
    that may be interrupted.

    `allow_nan=False` is not decoration. It is the last guard against the
    one failure that breaks the file for both of Urbano's readers, and it
    turns a slip anywhere upstream into an exception here rather than a
    package that looks finished.
    """
    # Imported here rather than at module scope to keep this module's import
    # graph to mapgen.geo and mapgen.utm, both of which are pure arithmetic.
    # Nothing else in it touches the filesystem beyond asking whether a file
    # exists, which makes the whole of the format reasoning testable without
    # a temporary directory.
    from mapgen.fsutil import atomic_write_text

    setting = build_project_setting(bbox, root, stem, granularity)
    try:
        text = json.dumps(setting, indent=2, allow_nan=False)
    except ValueError as exc:
        raise ProjectSettingError(
            f"The project setting for {stem} could not be written because it "
            f"held a value Urbano cannot read: {exc}"
        ) from None
    target = project_setting_path(_absolute(Path(root)), stem)
    atomic_write_text(target, text)
    return target


def describe_layers(layers: Sequence[str]) -> str:
    """The layer list as one plain phrase, for a terminal line.

    Here rather than in cli.py because the same sentence is wanted by the
    survey summary and by `mapgen bridge`, and two copies of it would drift.
    """
    if not layers:
        return "no layers"
    if len(layers) == 1:
        return layers[0]
    return f"{', '.join(layers[:-1])} and {layers[-1]}"
