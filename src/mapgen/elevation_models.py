"""The OpenTopography elevation vocabulary: which DEM models exist, which
of them are worth offering here, and what each one honestly gives you.

One vocabulary, read from one place, by everything that has to agree on it:
the CLI's --demtype flag, GET /api/sources (which the settings panel's
select is built from), SurveyRequest's validation, and ElevationSource's own
per-model licence, attribution and pixel size. The same "derive, do not
duplicate" shape mapgen.categories already uses for the category ids, for
the same reason: four copies of a list of ids is four chances to drift.

What this setting actually buys, which is less than it sounds
--------------------------------------------------------------

Task 28 was originally justified as unlocking the higher resolution the
owner's OpenTopography subscription allows. That justification is wrong,
and it is wrong in a way worth writing down so nobody re-derives it:

  * OpenTopography's global DEM API serves nothing finer than 30 m,
    anywhere. Every 30 m product in the list below is at the same ground
    sample distance as COP30, which is what this tool already downloaded
    before the setting existed.
  * The OT+ subscription buys high resolution LiDAR for North America
    (USGS 3DEP at 1 m over most of CONUS, NOAA coastal, Natural Resources
    Canada). Its own comparison table lists global datasets as "All Global
    (30m)" for subscribers and non-subscribers alike. A subscription
    changes nothing about what this tool can fetch over Wales.
  * The genuinely higher resolution route for a Welsh site is NRW LiDAR at
    1 m through DataMapWales, which is a different service, a different
    module, and phase 2 work. See sources/elevation.py's own module
    docstring, which has said so since before this task.

So the honest benefit here is CHOICE and TRANSPARENCY, not resolution, and
the choice that actually matters is the kind of model rather than its
sharpness. COP30, COP90, SRTMGL1, SRTMGL3, NASADEM and AW3D30 are all
SURFACE models: the height they report over a town is the roofs, and over
a wood it is the canopy. EU_DTM is BARE EARTH at the same 30 m, which for
a site section or a ground plane in Rhino is a different and often better
thing to be holding. That is the sentence the settings panel tells the
owner, and it is the whole of what this setting is for.

Where the metadata came from
----------------------------

The id list and each dataset's own resolution are from OpenTopography's
published OpenAPI description of the /globaldem endpoint (the demtype
parameter's enum and its accompanying description), read on 2026-08-04.
No live API call was made with the owner's key to establish any of this.

Licence and attribution for the seven OFFERED models are quoted from each
dataset's own OpenTopography metadata page, again read rather than
guessed. Three of those pages state "Data License: Not Provided" and give
only a citation, so that is exactly what is recorded below: no licence
claim, and the citation OpenTopography itself asks for. The remaining ten
ids are accepted (they are real values the API takes, and refusing them
here would invent a restriction the service does not have) but are not
offered in the interface and carry no licence claim at all, because
nothing here has established one for them.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_DEMTYPE = "COP30"

# What goes in survey.json for a model whose terms nobody here has read.
# Deliberately not a guess at a permissive licence: this string ends up in
# a package's own provenance record, which the owner may one day have to
# stand behind, and "we did not check" is the only truthful thing to say
# about a dataset nobody checked.
UNSTATED_LICENCE = (
    "Not established by mapgen. See this dataset's page at "
    "portal.opentopography.org for its terms before publishing anything "
    "derived from it."
)
UNSTATED_ATTRIBUTION = "Distributed by OpenTopography."

# Three of OpenTopography's own dataset pages (SRTM, NASADEM, AW3D30) say
# "Data License: Not Provided" and offer a citation instead. That is a
# different statement from the one above: the terms were looked up, and
# what was found is that OpenTopography states none, so the citation it
# does ask for is what a package should carry.
NO_LICENCE_STATED = (
    "No licence stated by OpenTopography. Cite the dataset as recorded in "
    "this entry's attribution."
)


@dataclass(frozen=True)
class ElevationModel:
    """One demtype the OpenTopography global DEM API accepts.

    resolution_m is the dataset's own ground sample distance, used by
    ElevationSource.estimate to size the download. label is what the
    settings panel's select shows, and is written to read the same way in
    a dropdown as in `mapgen estimate`'s own source line: the id first,
    because that is what the CLI flag and the API itself take, then what
    kind of model it is, then its resolution.
    """

    id: str
    label: str
    resolution_m: float
    licence: str
    attribution: str


def _offered(id_: str, label: str, resolution_m: float, licence: str, attribution: str):
    return ElevationModel(id_, label, resolution_m, licence, attribution)


def _accepted(id_: str, label: str, resolution_m: float) -> ElevationModel:
    return ElevationModel(id_, label, resolution_m, UNSTATED_LICENCE, UNSTATED_ATTRIBUTION)


_COPERNICUS_LICENCE = "Copernicus DEM, free for any use with attribution"
# Left exactly as ElevationSource has always carried it. It predates this
# module, it matches the copyright line on OpenTopography's own Copernicus
# page, and a package written before today records this same string, so
# rewording it would make two identical downloads look like two different
# provenances.
_COPERNICUS_ATTRIBUTION = "(c) DLR e.V. 2010-2014, (c) Airbus Defence and Space GmbH"

# The seven offered in the interface: every one of them covers Wales, is
# 90 m or finer, and has had its terms read. Ordered as the select shows
# them, best first rather than alphabetically, since a dropdown's first
# entry is read as a recommendation whether or not it was meant as one.
_OFFERED: tuple[ElevationModel, ...] = (
    _offered(
        "COP30",
        "COP30, Copernicus surface model, 30 m (default)",
        30.0,
        _COPERNICUS_LICENCE,
        _COPERNICUS_ATTRIBUTION,
    ),
    _offered(
        "EU_DTM",
        "EU_DTM, bare earth terrain, 30 m, Europe",
        30.0,
        "CC BY 4.0",
        "Hengl, T., Leal Parente, L., Krizan, J., and Bonannella, C. (2022). "
        "Continental Europe Digital Terrain Model. Distributed by OpenTopography. "
        "https://doi.org/10.5069/G99021ZF",
    ),
    _offered(
        "AW3D30",
        "AW3D30, ALOS surface model, 30 m",
        30.0,
        NO_LICENCE_STATED,
        "Japan Aerospace Exploration Agency (2021). ALOS World 3D 30 meter DEM. "
        "V3.2, Jan 2021. Distributed by OpenTopography. "
        "https://doi.org/10.5069/G94M92HB",
    ),
    _offered(
        "NASADEM",
        "NASADEM, reprocessed SRTM surface model, 30 m",
        30.0,
        NO_LICENCE_STATED,
        "NASA JPL (2021). NASADEM Merged DEM Global 1 arc second V001. "
        "Distributed by OpenTopography. https://doi.org/10.5069/G93T9FD9",
    ),
    _offered(
        "SRTMGL1",
        "SRTMGL1, SRTM surface model, 30 m",
        30.0,
        NO_LICENCE_STATED,
        "NASA Shuttle Radar Topography Mission (2013). SRTM Global. "
        "Distributed by OpenTopography. https://doi.org/10.5069/G9445JDF",
    ),
    _offered(
        "COP90",
        "COP90, Copernicus surface model, 90 m",
        90.0,
        _COPERNICUS_LICENCE,
        _COPERNICUS_ATTRIBUTION,
    ),
    _offered(
        "SRTMGL3",
        "SRTMGL3, SRTM surface model, 90 m",
        90.0,
        NO_LICENCE_STATED,
        "NASA Shuttle Radar Topography Mission (2013). SRTM Global. "
        "Distributed by OpenTopography. https://doi.org/10.5069/G9445JDF",
    ),
)

# Accepted but not offered. Each is a real demtype the API takes, so a
# person who knows they want one can still pass --demtype and get it; none
# of them belongs in a dropdown for Welsh site survey work, for the reason
# named in each label. The ellipsoidal variants are the ones most worth
# keeping out of a dropdown: they are the same data as their ordinary
# siblings with heights measured from the ellipsoid instead of the geoid,
# which over Britain is a vertical offset of tens of metres, silently.
_ACCEPTED_ONLY: tuple[ElevationModel, ...] = (
    _accepted("SRTMGL1_E", "SRTMGL1_E, SRTM 30 m, ellipsoidal heights", 30.0),
    _accepted("AW3D30_E", "AW3D30_E, ALOS 30 m, ellipsoidal heights", 30.0),
    _accepted("GEDTM30", "GEDTM30, global ensemble bare earth terrain, 30 m", 30.0),
    _accepted("CA_MRDEM_DSM", "CA_MRDEM_DSM, surface model, 30 m, Canada only", 30.0),
    _accepted("CA_MRDEM_DTM", "CA_MRDEM_DTM, bare earth terrain, 30 m, Canada only", 30.0),
    _accepted("ANADEM", "ANADEM, bare earth terrain, 30 m, South America only", 30.0),
    _accepted("SRTM15Plus", "SRTM15Plus, global bathymetry and topography, 500 m", 500.0),
    _accepted("GEBCOIceTopo", "GEBCOIceTopo, global bathymetry, 500 m", 500.0),
    _accepted("GEBCOSubIceTopo", "GEBCOSubIceTopo, global bathymetry, 500 m", 500.0),
    _accepted("GEDI_L3", "GEDI_L3, bare earth terrain, 1000 m", 1000.0),
)

DEMTYPES: dict[str, ElevationModel] = {
    model.id: model for model in (*_OFFERED, *_ACCEPTED_ONLY)
}

# What the settings panel offers, in order. Read through GET /api/sources
# rather than written into index.html, so the markup cannot drift from
# this list the way a second hand-maintained copy eventually would.
OFFERED_DEMTYPE_IDS: tuple[str, ...] = tuple(model.id for model in _OFFERED)

ALL_DEMTYPE_IDS: tuple[str, ...] = tuple(DEMTYPES)

# The resolution assumed for an id this module has never heard of. Only
# ever reached through model_for() below, and only ever used to size a
# download estimate, never to decide anything about correctness: an
# unrecognised id cannot reach a real request at all, because
# validate_demtype rejects it at SurveyRequest construction first.
_FALLBACK_RESOLUTION_M = 30.0


class UnknownDemTypeError(ValueError):
    """Raised by validate_demtype for a demtype outside ALL_DEMTYPE_IDS.

    A ValueError subclass, exactly as UnknownCategoryError is and for the
    same reason: server.py's _REQUEST_VALUE_ERRORS already catches
    ValueError generically, so the browser gets a 400 with this message
    unchanged, and cli.py's main() needs only the same one-line addition
    to its own explicit exception tuple that every request-validation
    error before this one has needed.

    The failure this prevents is the one UnknownCategoryError's docstring
    describes, one layer along: OpenTopography answers a bad demtype with
    an error page rather than a TIFF, which ElevationSource already turns
    into "OpenTopography did not return a TIFF", a message that names
    neither the misspelled value nor the valid ones. Rejecting it before
    the request is made is the difference between "Unknown elevation
    model: COP-30" and a download that fails halfway through a survey for
    reasons the log does not explain.
    """


def validate_demtype(demtype: object) -> None:
    """Raises UnknownDemTypeError unless demtype is an id this vocabulary
    recognises.

    Called once, from SurveyRequest.__post_init__ (see package.py), so the
    CLI's --demtype and the browser's select share this one check, matching
    what Task 21 established for categories: this is validation of a
    person's own words becoming an id, and it belongs at the single point
    where both routes into the tool already meet.

    Deliberately accepts every id the API's own published enum lists, not
    only the seven the settings panel offers. Refusing a value the service
    itself accepts would be inventing a restriction, and the CLI is where
    somebody who genuinely wants ellipsoidal heights or a bathymetry grid
    is expected to ask for it.

    A non-string is rejected here rather than left to blow up later on:
    PUT /api/config applies whatever it is given with no type check of its
    own (see mapgen.config), so a hand-edited config.json holding a number
    would otherwise reach the API's query string as one.
    """
    if isinstance(demtype, str) and demtype in DEMTYPES:
        return
    raise UnknownDemTypeError(
        f"Unknown elevation model: {demtype!r}. "
        f"Valid models are: {', '.join(ALL_DEMTYPE_IDS)}."
    )


def model_for(demtype: str) -> ElevationModel:
    """The metadata for a demtype, never raising for an unrecognised one.

    NOT a second validation site, deliberately: validate_demtype above is
    the only place that decides whether an id is acceptable, and it runs at
    SurveyRequest construction before any of this is reached. This function
    exists so ElevationSource can be constructed directly from Python with
    whatever a caller likes (a test, an experiment, a phase 2 module) and
    still have a licence string, an attribution and a pixel size to work
    with, rather than raising from a constructor that has no business
    refusing anything.

    The record handed back for an unknown id claims nothing: the unstated
    licence and attribution above, and a 30 m pixel, which is the finest
    thing this API serves and therefore the estimate that overstates rather
    than understates the download.
    """
    known = DEMTYPES.get(demtype)
    if known is not None:
        return known
    return ElevationModel(
        id=demtype,
        label=str(demtype),
        resolution_m=_FALLBACK_RESOLUTION_M,
        licence=UNSTATED_LICENCE,
        attribution=UNSTATED_ATTRIBUTION,
    )


def offered_choices() -> list[dict[str, str]]:
    """The select's options, in order: what GET /api/sources hands the
    browser so index.html does not carry a second copy of this list.
    """
    return [{"id": model.id, "label": model.label} for model in _OFFERED]
