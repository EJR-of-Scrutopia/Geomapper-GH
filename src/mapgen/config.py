"""User config, remembered between runs.

Mostly preferences. Task 18 added one exception: the elevation layer's
OpenTopography key, because the interface has nowhere else to put a value
the owner types in. Every other API key this tool might ever need still
comes from the environment; this file is not becoming a general credential
store.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from mapgen.fsutil import atomic_write_text

CONFIG_PATH = Path.home() / ".mapgen" / "config.json"
DEFAULT_OUTPUT_ROOT = Path.home() / "Surveys"


@dataclass
class Config:
    output_root: str = str(DEFAULT_OUTPUT_ROOT)
    tile_size_m: float = 2000.0
    overlap_m: float = 100.0
    last_region: str = ""
    # The one exception to this module's own docstring: Task 18 gave the
    # elevation layer's OpenTopography key an interface field, and the
    # owner chose to save it here rather than nowhere. An environment
    # variable still wins over this if one is set; see
    # mapgen.sources.elevation.resolve_api_key. Never logged: the web
    # server's job events never include it, and the interface field is
    # type="password".
    opentopography_api_key: str = ""


def load_config(path: Path | None = None) -> Config:
    target = path or CONFIG_PATH
    if not target.exists():
        return Config()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
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
        if isinstance(default_value, float):
            # An int is a reasonable spelling of a float in hand-edited
            # JSON (2000 rather than 2000.0), but bool is a subclass of
            # int in Python and true/false must not silently become 1.0/0.0.
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                known[field] = float(value)
        elif isinstance(value, type(default_value)):
            known[field] = value
    return Config(**known)


def save_config(config: Config, path: Path | None = None) -> None:
    target = path or CONFIG_PATH
    atomic_write_text(target, json.dumps(asdict(config), indent=2))
