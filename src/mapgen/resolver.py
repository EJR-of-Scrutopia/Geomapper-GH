"""The per-extent category tier resolver.

The phase 2 spec's own architectural centrepiece: mapgen now has several
sources that can serve the same category over the same ground (OSM and OS
Open both carry buildings; Overture and OS Open both carry water), and an
owner picking a survey's sources needs to see, before clicking Download,
which one this extent will actually use for each category and which others
are along for backup only. That is what `resolve()` answers.

## The two optional LayerSource extensions this module reads

`covers(bbox: BBox) -> str` and `tier(category: str) -> int | None` are
optional LayerSource extensions, read the same defensive, getattr way as
every other one `sources/base.py`'s own docstring documents (readiness_
problem, routing_note, possible_outputs): a source that defines neither
simply never appears in any category's resolution, and `resolve()` never
raises over a source missing one or the other. Every real source this
project ships defines both, or neither; a test double may define neither,
and a future source may add both without touching this module at all,
the same "adding a source is one new module plus one register call"
promise `sources/base.py`'s own module docstring states for the registry
itself.

`covers(bbox)` answers a bbox-scoped coverage question, not a category one:
whether THIS source has any real data at all for the extent, ignoring which
categories it serves. `"full"` (every corner of the extent is inside this
source's own coverage), `"partial"` (some of the extent is, some is not:
lidar_wales.py's own Welsh mosaic genuinely ends mid-country, and this
project would rather say so than round it to "full" or "none"), or
`"none"` (the source has nothing here at all).

`tier(category)` answers a category-scoped quality question, not a
bbox-scoped one: this source's own rank among every source that can serve
`category` at all, 1 being the best per-feature quality, or `None` when
this source does not serve that category. A source's tier for a category
never depends on the bbox; only `covers()` does.

**Divergence from the spec, documented rather than silently taken**: the
phase 2 spec's own text describes coverage as `bool | "partial"`, a
two-and-a-half state answer. This module implements three plain strings
instead, `"full" | "partial" | "none"`, because a bare `bool` cannot
distinguish "yes, all of it" from "no, none of it" without the string
`"partial"` sitting awkwardly beside it as a second, differently-typed
value. Three strings say the same three facts the spec asks for, honestly,
without asking a caller to check `isinstance` before comparing.

## ROLES and how a category's entries are labelled

`ROLES` is a closed set of overrides, `(category, source_id) -> role`,
for the two entries the spec calls out by name: OS Open's roads and rail
layers are reference data an owner filters in Urbano's own GeoJSON import
(see `os_open.py`'s own module docstring, and Task 6's buildings-fusion
plan for why roads specifically are never fused into the `.osm`: fusing
would double every road that OSM already carries). Every other
(category, source_id) pair derives its role from tier alone: the source
(or sources, tied) at the best tier PRESENT for this bbox is `"base"`,
every other covering source is `"fill"`. A source ROLES names outranks
that derivation unconditionally, even when it happens to hold the best
tier: `("roads", "os_open")` is `"reference"` whether or not `os_open`
is tier 1 for roads in some future retiering, because what makes a layer
"reference" is a fact about how Urbano treats it, not about its rank.

## resolve()'s output shape, and why it nests by category

`resolve(bbox, sources) -> list[dict]`: one dict per category that has at
least one covering, serving source, in `CATEGORIES` order, each shaped
`{"category": str, "sources": list[dict]}`; a category with no covering
source is omitted entirely, never included empty. Each inner dict is
`{"id", "display_name", "tier", "coverage", "role"}`, sorted by tier
ascending, ties broken by source id for determinism (two sources tied at
the same tier have no other honest ordering to fall back on, and a
resolution list that reordered itself between two runs over the identical
bbox and selection would be a worse record than one that picks a
deterministic, if arbitrary, tiebreak).

Nesting by category, rather than returning one flat list of entries each
carrying its own `"category"` key, is what makes "entry order within a
category" (the tier-then-id sort above) a property of one list rather
than something a reader has to first group by category to see; Task 8's
own `renderResolution` walks exactly this shape, one category group at a
time, to build its one-line-per-category summary.

## Never touches the network, by construction

`resolve()` itself does no I/O of any kind: it calls `covers()`/`tier()`
on whatever sources it is given and assembles their answers. Every
source's own `covers()` is documented, at its own definition, to answer
with no network call either (a committed fixture, a cache-only OSTN15
read with a gridless fallback, or a fixed `"full"` for a source with no
coverage edge at all): resolve() inherits that guarantee from its
callees rather than asserting it itself, the same way `estimate()`'s own
"never touches the network" rule (package.py's own module docstring) has
always been enforced source by source, not by resolve()/estimate_survey
policing it centrally.

## `sources` is the CONFIGURED, SELECTED list, never the registry

`resolve()` takes `sources` as a plain sequence, never reaching into
`sources.base`'s own registry itself: package.py's `_configured_sources
(request)` has already applied the owner's selection (which source ids
were ticked) and any request-scoped reconfiguration (Overture's types,
OSM's categories, elevation's model) by the time it calls this, so a
source the owner did not select is simply not in the list handed in, and
never appears in any category's resolution. Calling `resolve()` with the
full registry instead would report every source as though it had been
selected, which is exactly the same "control that appears to work and
does not" shape `sources/base.py`'s own `EmptySourceSelectionError`
docstring names for a different bug.
"""

from __future__ import annotations

from typing import Sequence

from mapgen.geo import BBox

# The fourteen categories the phase 2 spec names, in the order it states
# them. This is also the order resolve()'s own output list follows: a
# reader (the owner, or Task 8's renderResolution) sees terrain and
# contours before addresses and places, matching how the spec itself
# introduces them rather than an alphabetical or source-registration
# order that would carry no meaning of its own.
CATEGORIES = (
    "terrain",
    "contours",
    "heights",
    "buildings",
    "roads",
    "rail",
    "boundaries",
    "greenspace",
    "sites",
    "land",
    "addresses",
    "land_use",
    "water",
    "places",
)

# (category, source_id) -> role, for the entries whose role is a fact
# about how Urbano treats the layer rather than about its tier rank. See
# the module docstring's "ROLES" section: OS Open's own roads and rail
# layers are reference data an owner filters by key/value in Urbano's
# GeoJSON import, deliberately never fused into the .osm (fusing roads
# would double every one OSM already carries).
ROLES: dict[tuple[str, str], str] = {
    ("roads", "os_open"): "reference",
    ("rail", "os_open"): "reference",
}


def _role_for(category: str, source_id: str, tier: int, best_tier: int) -> str:
    override = ROLES.get((category, source_id))
    if override is not None:
        return override
    return "base" if tier == best_tier else "fill"


def resolve(bbox: BBox, sources: Sequence[object]) -> list[dict]:
    """The per-category source resolution for `bbox`, over exactly
    `sources` (see the module docstring's own "CONFIGURED, SELECTED"
    section): plain dicts and lists throughout, JSON-ready as-is, because
    this lands verbatim in both the estimate HTTP response and
    survey.json.

    A source lacking `covers` or `tier` (a test stub, a future source
    that has not added them yet) is skipped silently for every category,
    the same optional-extension convention every other `sources/base.py`
    extension already follows; it is never an error to hand resolve() a
    source with no opinion on tiering.
    """
    result: list[dict] = []
    for category in CATEGORIES:
        candidates: list[tuple[int, str, object, str]] = []
        for source in sources:
            covers = getattr(source, "covers", None)
            tier_fn = getattr(source, "tier", None)
            if not callable(covers) or not callable(tier_fn):
                continue
            tier = tier_fn(category)
            if tier is None:
                continue
            coverage = covers(bbox)
            if coverage == "none":
                continue
            candidates.append((tier, source.id, source, coverage))

        if not candidates:
            continue

        candidates.sort(key=lambda entry: (entry[0], entry[1]))
        best_tier = candidates[0][0]
        entries = [
            {
                "id": source_id,
                "display_name": source.display_name,
                "tier": tier,
                "coverage": coverage,
                "role": _role_for(category, source_id, tier, best_tier),
            }
            for tier, source_id, source, coverage in candidates
        ]
        result.append({"category": category, "sources": entries})

    return result
