"""Driving the C# UrbanoBridge.

The bridge loads Urbano's own assemblies to produce the .egrid, the geoparquet
and the project setting JSON that Urbano 2 reads. We shell out to it rather than
reimplementing any of that.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from mapgen.procutil import run_hidden
from typing import Callable

from mapgen.geo import BBox

DEFAULT_PROJECT_PATH = (
    Path(__file__).resolve().parents[2] / "tools" / "UrbanoBridge" / "UrbanoBridge.csproj"
)

# UrbanoBridge's OWN diagnostic (Program.cs's ResolvePackageDirectory), quoted
# here verbatim, for the one failure this bridge can already name precisely:
# no Urbano install at all. Greedy up to the LAST period on the line, not the
# first: the real directory always contains one itself (...packages\8.0\...),
# so a non-greedy match would truncate the path at "8" and drop "0\Urbano2".
_MISSING_URBANO_RE = re.compile(
    r"No installed Urbano package with Urbano\.Core\.dll and ProjectSetup\.dll "
    r"was found under (.+)\.\s*$",
    re.MULTILINE,
)


def _missing_urbano_directory(output: str) -> str | None:
    """The directory UrbanoBridge looked in, if its output says plainly that
    no Urbano install was found there; None for any other failure shape.
    """
    match = _MISSING_URBANO_RE.search(output)
    return match.group(1).strip() if match else None


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

    # Captured rather than left to inherit this process's stdout/stderr, so
    # it can be inspected for the one failure UrbanoBridge already names
    # precisely (see _missing_urbano_directory) before deciding whether the
    # owner needs to see it at all.
    # run_hidden for the same reason overture.py uses it: dotnet is a console
    # executable, and under the windowless desktop shortcut every invocation
    # would otherwise open a console window the owner can close. See
    # mapgen.procutil.
    result = run_hidden(
        build_command(request, project),
        runner=runner,
        errors="replace",
    )
    if result.returncode != 0:
        stdout = getattr(result, "stdout", "") or ""
        stderr = getattr(result, "stderr", "") or ""
        missing_dir = _missing_urbano_directory(stdout + stderr)
        if missing_dir:
            # Expected and harmless on a machine with no Urbano installed,
            # which is every run this owner makes today: reported as a
            # plain sentence, not the DirectoryNotFoundException and C#
            # stack trace that would otherwise print before it on every
            # single survey. The raw output is deliberately NOT printed
            # here, unlike the genuinely-unexpected branch below.
            raise BridgeError(
                f"Urbano is not installed: no Urbano.Core.dll or ProjectSetup.dll "
                f"was found under {missing_dir}. Urbano is optional; the rest of "
                f"the package does not need it."
            )
        # A genuinely unexpected failure: nothing here recognises it, so the
        # raw subprocess output is the only place the real detail lives.
        # Printed rather than swallowed, exactly where "see the output
        # above" below still points.
        if stdout:
            print(stdout, end="" if stdout.endswith("\n") else "\n")
        if stderr:
            print(stderr, end="" if stderr.endswith("\n") else "\n", file=sys.stderr)
        raise BridgeError(
            f"The Urbano bridge failed with exit code {result.returncode}. "
            f"See the output above for details."
        )
