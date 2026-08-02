"""User config, remembered between runs.

Only holds preferences, never secrets. API keys come from the environment.
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


def load_config(path: Path | None = None) -> Config:
    target = path or CONFIG_PATH
    if not target.exists():
        return Config()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Config()
    known = {field: payload[field] for field in Config().__dict__ if field in payload}
    return Config(**known)


def save_config(config: Config, path: Path | None = None) -> None:
    target = path or CONFIG_PATH
    atomic_write_text(target, json.dumps(asdict(config), indent=2))
