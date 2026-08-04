"""The elevation model vocabulary.

Two kinds of check here, and they are not the same kind. Most of these pin
BEHAVIOUR (what validate_demtype accepts and refuses, what model_for hands
back for something it has never heard of). One pins a FACT read from
OpenTopography's own published OpenAPI description of /globaldem on
2026-08-04: the exact set of demtype values the service accepts. A fact
test fails when the service changes, which is the point of writing it
down; a reader who finds it failing should re-read the API description
rather than edit the list until it passes.
"""

import pytest

from mapgen.elevation_models import (
    ALL_DEMTYPE_IDS,
    DEFAULT_DEMTYPE,
    DEMTYPES,
    OFFERED_DEMTYPE_IDS,
    UNSTATED_LICENCE,
    UnknownDemTypeError,
    model_for,
    offered_choices,
    validate_demtype,
)

# Quoted from the demtype parameter's own enum in
# https://portal.opentopography.org/apidocs/openapi.json, read 2026-08-04.
DOCUMENTED_API_ENUM = {
    "SRTMGL3",
    "SRTMGL1",
    "SRTMGL1_E",
    "AW3D30",
    "AW3D30_E",
    "SRTM15Plus",
    "NASADEM",
    "COP30",
    "COP90",
    "EU_DTM",
    "GEDI_L3",
    "GEBCOIceTopo",
    "GEBCOSubIceTopo",
    "CA_MRDEM_DTM",
    "CA_MRDEM_DSM",
    "ANADEM",
    "GEDTM30",
}


def test_the_vocabulary_is_exactly_the_documented_api_enum():
    # Neither narrower nor wider. Narrower would refuse a value the service
    # itself accepts, which is a restriction this tool has no business
    # inventing; wider would accept a value the service answers with an
    # error page instead of a TIFF, which is the failure validate_demtype
    # exists to catch before a download starts.
    assert set(ALL_DEMTYPE_IDS) == DOCUMENTED_API_ENUM


def test_the_default_is_unchanged_and_is_offered_first():
    # Existing saved configs and existing scripted CLI invocations must
    # behave identically after this task, which starts here.
    assert DEFAULT_DEMTYPE == "COP30"
    assert OFFERED_DEMTYPE_IDS[0] == DEFAULT_DEMTYPE


def test_every_offered_id_is_a_real_demtype():
    for demtype in OFFERED_DEMTYPE_IDS:
        assert demtype in DEMTYPES


def test_nothing_offered_is_coarser_than_90_m():
    # A dropdown for site survey work has no business offering a 500 m
    # bathymetry grid or a 1 km terrain product: at a 2 km tile those are
    # four pixels across and one pixel across respectively.
    for demtype in OFFERED_DEMTYPE_IDS:
        assert DEMTYPES[demtype].resolution_m <= 90.0


def test_nothing_offered_claims_a_licence_that_was_never_established():
    # The offered seven are the ones whose terms were actually read on
    # OpenTopography's own dataset pages. If a model is added to the
    # dropdown without that being done, this fails rather than shipping an
    # unchecked licence string into a package's provenance.
    for demtype in OFFERED_DEMTYPE_IDS:
        assert DEMTYPES[demtype].licence != UNSTATED_LICENCE


def test_every_offered_model_carries_an_attribution():
    for demtype in OFFERED_DEMTYPE_IDS:
        assert DEMTYPES[demtype].attribution.strip()


def test_ellipsoidal_and_regional_models_are_accepted_but_never_offered():
    # Accepted, because they are real values the API takes. Not offered,
    # because ellipsoidal heights over Britain differ from the ordinary
    # ones by tens of metres with nothing on screen to say so, and Canada
    # and South America do not cover a Welsh site at all.
    for demtype in ("SRTMGL1_E", "AW3D30_E", "CA_MRDEM_DTM", "ANADEM"):
        assert demtype in DEMTYPES
        assert demtype not in OFFERED_DEMTYPE_IDS


def test_validate_accepts_every_id_in_the_vocabulary():
    for demtype in ALL_DEMTYPE_IDS:
        validate_demtype(demtype)


def test_validate_rejects_an_unknown_id_and_names_the_valid_ones():
    with pytest.raises(UnknownDemTypeError) as caught:
        validate_demtype("COP-30")
    message = str(caught.value)
    assert "COP-30" in message
    assert "COP30" in message


def test_validate_rejects_a_lookalike_that_differs_only_in_case():
    # The API's enum is case sensitive and answers cop30 with an error
    # page, not a TIFF.
    with pytest.raises(UnknownDemTypeError):
        validate_demtype("cop30")


def test_validate_rejects_an_empty_string():
    # The browser omits the key entirely when its select has never been
    # populated; an empty string reaching here is a genuinely malformed
    # request, not a "use the default" signal, and must not be silently
    # coerced into one.
    with pytest.raises(UnknownDemTypeError):
        validate_demtype("")


def test_validate_rejects_a_non_string_without_a_typeerror():
    # PUT /api/config writes whatever it is given with no type check of
    # its own, so a hand-edited config.json holding a number really can
    # reach here. It must come back as the same plain, one-line request
    # error every other bad value does, not as a TypeError from deep
    # inside a query-string builder.
    for value in (30, None, ["COP30"]):
        with pytest.raises(UnknownDemTypeError):
            validate_demtype(value)


def test_model_for_returns_the_recorded_metadata():
    model = model_for("EU_DTM")
    assert model.resolution_m == 30.0
    assert model.licence == "CC BY 4.0"
    assert "Hengl" in model.attribution


def test_model_for_never_raises_for_an_unknown_id_and_claims_nothing():
    # Not a second validation site: validate_demtype is the only place
    # that refuses anything, and a source constructed directly from Python
    # must still have a licence string and a pixel size to work with.
    model = model_for("SOMETHING_NEW")
    assert model.id == "SOMETHING_NEW"
    assert model.resolution_m == 30.0
    assert model.licence == UNSTATED_LICENCE
    assert "portal.opentopography.org" in model.licence


def test_offered_choices_are_in_order_and_shaped_for_the_select():
    choices = offered_choices()
    assert [choice["id"] for choice in choices] == list(OFFERED_DEMTYPE_IDS)
    for choice in choices:
        assert set(choice) == {"id", "label"}
        assert choice["label"].startswith(choice["id"])


def test_the_labels_say_which_models_are_surface_and_which_are_bare_earth():
    # The one thing this setting genuinely changes for a Welsh site. If the
    # labels stop saying it, the owner is choosing between seven names that
    # all look like the same thing at the same resolution.
    assert "surface" in DEMTYPES["COP30"].label
    assert "bare earth" in DEMTYPES["EU_DTM"].label


def test_no_label_or_licence_uses_an_em_dash():
    # A project-wide rule, checked where new prose is most likely to
    # arrive: this module is a table of copy that reaches both a dropdown
    # and a package's own survey.json. Written as an escape rather than
    # the character itself so a plain grep for one over this repository
    # still comes back empty, including from this file.
    em_dash = chr(8212)
    for model in DEMTYPES.values():
        for text in (model.label, model.licence, model.attribution):
            assert em_dash not in text
