"""Driving the C# UrbanoBridge.

The bridge loads Urbano's own assemblies to produce the .egrid, the geoparquet
and the project setting JSON that Urbano 2 reads. We shell out to it rather than
reimplementing any of that.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from mapgen.geo import BBox

DEFAULT_PROJECT_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "UrbanoBridge" / "UrbanoBridge.csproj"
)


class BridgeError(RuntimeError):
    """Raised when the bridge is missing or exits non-zero."""


@dataclass(frozen=True)
class BridgeRequest:
    bbox: BBox
    output_dir: Path
    file_name_stem: str | None
    granularity: str = "Block"
    osm_file_path: Path | None = None
    elevation_tiff_path: Path | None = None
    skip_blocks: bool = True
    skip_climate: bool = True
    skip_elevation: bool = False
    skip_overture: bool = False
    package_dir: str | None = None


def build_command(request: BridgeRequest, project_path: Path) -> list[str]:
    command = [
        "dotnet",
        "run",
        "--project",
        str(project_path),
        "--",
        "--bbox",
        request.bbox.to_query_string(),
        "--output-folder",
        str(request.output_dir),
        "--granularity",
        request.granularity,
    ]

    if request.file_name_stem:
        command.extend(["--file-name-stem", request.file_name_stem])
    if request.package_dir:
        command.extend(["--package-dir", request.package_dir])
    if request.osm_file_path is not None:
        command.extend(["--osm-file-path", str(request.osm_file_path)])
    if request.elevation_tiff_path is not None:
        command.extend(["--elevation-tiff-path", str(request.elevation_tiff_path)])

    for flag, enabled in (
        ("--skip-climate", request.skip_climate),
        ("--skip-blocks", request.skip_blocks),
        ("--skip-elevation", request.skip_elevation),
        ("--skip-overture", request.skip_overture),
    ):
        if enabled:
            command.append(flag)

    return command


def run_bridge(
    request: BridgeRequest,
    project_path: Path | None = None,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    project = project_path if project_path is not None else DEFAULT_PROJECT_PATH
    if not project.exists():
        raise BridgeError(
            f"Urbano bridge project not found at {project}. Build it with:\n"
            f"  dotnet build tools/UrbanoBridge/UrbanoBridge.csproj"
        )

    result = runner(build_command(request, project), check=False)
    if result.returncode != 0:
        raise BridgeError(
            f"The Urbano bridge failed with exit code {result.returncode}. "
            f"See the output above for details."
        )
