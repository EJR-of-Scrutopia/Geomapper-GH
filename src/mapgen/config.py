"""User config, remembered between runs.

Mostly preferences. Task 18 added one exception: the elevation layer's
OpenTopography key, because the interface has nowhere else to put a value
the owner types in. Every other API key this tool might ever need still
comes from the environment; this file is not becoming a general credential
store.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from mapgen.elevation_models import DEFAULT_DEMTYPE
from mapgen.fsutil import atomic_write_text

CONFIG_PATH = Path.home() / ".mapgen" / "config.json"
DEFAULT_OUTPUT_ROOT = Path.home() / "Surveys"


@dataclass
class Config:
    output_root: str = str(DEFAULT_OUTPUT_ROOT)
    tile_size_m: float = 2000.0
    overlap_m: float = 100.0
    last_region: str = ""
    # Task 22: "auto" follows the OS/browser preference (prefers-color-
    # scheme), "light" and "dark" override it. Validated the same
    # structural way as every other field here (must be a str; an
    # unrecognised value is not rejected by load_config itself, which has
    # no notion of an enum, only a type, the same as every other field),
    # so app.js's own applyTheme is what actually falls back to "auto"
    # for anything it does not recognise.
    theme: str = "auto"
    # The one exception to this module's own docstring: Task 18 gave the
    # elevation layer's OpenTopography key an interface field, and the
    # owner chose to save it here rather than nowhere. An environment
    # variable still wins over this if one is set; see
    # mapgen.sources.elevation.resolve_api_key. Never logged: the web
    # server's job events never include it, and the interface field is
    # type="password".
    opentopography_api_key: str = ""
    # Task 28: which OpenTopography DEM the elevation layer downloads.
    # Source-qualified, like opentopography_api_key and unlike
    # output_root, because config.json is one flat namespace shared by
    # everything and a bare "demtype" would not say whose. Validated the
    # same structural way as every other field here (must be a str; an
    # unrecognised value is not rejected by load_config itself, which has
    # no notion of a vocabulary, only a type), so the actual refusal
    # happens where it does for categories: SurveyRequest.__post_init__,
    # the one place the CLI and the browser both pass through. A
    # hand-edited config.json holding a model that does not exist
    # therefore reports a plain "Unknown elevation model" on the next
    # estimate rather than failing mid-download.
    elevation_demtype: str = DEFAULT_DEMTYPE
    # Task 38, item 4: how much of the map's column the owner has given
    # the log, in CSS pixels, set by dragging the divider above it.
    #
    # A preference and nothing more, which is what this module is mostly
    # for. It is here rather than in the browser's own storage because
    # the browser has nowhere durable to put it: mapgen serves on an
    # ephemeral port (see web.server.serve, port 0), and every
    # browser-side store is keyed by origin including that port, so the
    # next launch would read an empty one. This file is the only thing
    # about a session that outlives the session.
    #
    # 0.0 means "never chosen", not "no height": the stylesheet's own
    # 120px is what a page with no saved split shows, and app.js only
    # overrides it when this is set. It is a float for the same reason
    # tile_size_m is, so load_config's numeric branch accepts a
    # hand-edited 240 as readily as 240.0 while still refusing true.
    # Nothing clamps it here, deliberately: what fits is a question about
    # the window it is being restored into, which only the browser can
    # answer, and it answers it on every restore (see setLogHeight).
    log_height_px: float = 0.0


def load_config(path: Path | None = None) -> Config:
    target = path or CONFIG_PATH
    if not target.exists():
        return Config()
    # A whole file that cannot be used falls back to every default, and
    # says so, for the same reason a single rejected field below does: a
    # silent reset is indistinguishable from the settings having never
    # been made. The realistic cause is a hand-edited Windows path with
    # one backslash ("C:\Surveys"), which is not valid JSON.
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        warnings.warn(
            f"{target}: not valid JSON ({exc}); using the defaults for every field.",
            stacklevel=2,
        )
        return Config()
    except OSError:
        return Config()

    # Valid JSON is not guaranteed to be an object: a bare number, string,
    # list or null all parse without error. The same guard jobs.py's
    # _merge_saved applies to its own state.json, applied here for the same
    # reason: "field in payload" behaves like substring or membership testing
    # instead of key lookup once payload is not a dict, which happens to
    # survive for some inputs (a string or list with no field name inside
    # it) and raises TypeError for others (an int or None is not iterable
    # at all). Neither outcome is intended; both are closed off here.
    if not isinstance(payload, dict):
        warnings.warn(
            f"{target}: not a JSON object; using the defaults for every field.",
            stacklevel=2,
        )
        return Config()

    defaults = Config()
    known: dict[str, object] = {}
    for field, default_value in defaults.__dict__.items():
        if field not in payload:
            continue
        value = payload[field]
        # A field is only accepted if its value has the same shape as that
        # field's own default. A null or wrong-typed value, for example a
        # number where a string is expected, falls back to the default for
        # that field alone rather than poisoning the whole Config with a
        # value no caller declared it could hold, such as a None that later
        # crashes Path(None) far away from here.
        #
        # A coordinator review pointed out that the else this if/elif used
        # to fall through to had no body at all: a rejected field just
        # never entered known, silently, with nothing anywhere to say
        # which field it was or why. warnings.warn below does not change
        # what value the field takes, only whether the rejection is
        # visible: still the default, now with a reason.
        #
        # This is also, deliberately, the ONLY place that validates: PUT
        # /api/config (see server.py) writes whatever a client sends
        # straight through setattr and save_config with no type check of
        # its own, on purpose, so a bad value and a good one are stored
        # the same way and there is exactly one place, here, that decides
        # what counts as valid. That single choke point is what makes
        # this warning cover both routes to a bad config.json at once: a
        # malformed PUT body, and a person hand-editing the file directly
        # on disk. Either way, the bad value only ever surfaces as this
        # warning the next time the file is loaded, never as a crash.
        if isinstance(default_value, float):
            # An int is a reasonable spelling of a float in hand-edited
            # JSON (2000 rather than 2000.0), but bool is a subclass of
            # int in Python and true/false must not silently become 1.0/0.0.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known[field] = float(value)
            else:
                warnings.warn(
                    f"{target}: {field!r} is {value!r}, not a number; "
                    f"keeping the default ({default_value!r}) for this field.",
                    stacklevel=2,
                )
        elif isinstance(value, type(default_value)):
            known[field] = value
        else:
            warnings.warn(
                f"{target}: {field!r} is {value!r}, not a "
                f"{type(default_value).__name__}; keeping the default "
                f"({default_value!r}) for this field.",
                stacklevel=2,
            )
    return Config(**known)


def _is_readable_config(target: Path) -> bool:
    try:
        return isinstance(json.loads(target.read_text(encoding="utf-8")), dict)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def save_config(config: Config, path: Path | None = None) -> None:
    """Writes `config` to `path`, first moving aside an existing file that
    load_config could not read. PUT /api/config loads (every default, for
    such a file) and then saves, so without this one settings change in
    the picker would silently destroy a hand-edited file and whatever it
    held. The copy sits beside it as config.json.unreadable-<UTC time>.
    """
    target = path or CONFIG_PATH
    if target.exists() and not _is_readable_config(target):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target.replace(target.with_name(f"{target.name}.unreadable-{stamp}"))
    atomic_write_text(target, json.dumps(asdict(config), indent=2))
