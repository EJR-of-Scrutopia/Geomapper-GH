"""Command line entry point.

The survey subcommand is the one that matters. plan, download, merge and
urbano-package are retained so existing muscle memory and any scripts keep
working, and they route through the same orchestration.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from mapgen import __version__
from mapgen.categories import (
    CATEGORY_GROUPS,
    ROAD_SUBTYPES,
    EmptyCategorySelectionError,
    UnknownCategoryError,
)
from mapgen.elevation_models import (
    ALL_DEMTYPE_IDS,
    OFFERED_DEMTYPE_IDS,
    UnknownDemTypeError,
)
from mapgen.geo import BBox, BBoxError, TilingError
from mapgen.naming import NamingError
from mapgen.package import (
    IncompleteSurveyError,
    SurveyRequest,
    UnbridgeablePackageError,
    bridge_package,
    describe_tile_failures,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import (
    EmptySourceSelectionError,
    UnknownSourceError,
    available_sources,
)
from mapgen.sources.elevation import ElevationError
from mapgen.sources.osm import OsmDownloadError
from mapgen.sources.overture import OvertureError

# Task 19 item 4: where a windowless launch's own errors go when there is
# no console to print them to. Alongside ~/.mapgen/config.json, the same
# home-directory convention mapgen.config already established, so both
# live in one place the owner can find.
WINDOWLESS_LOG_PATH = Path.home() / ".mapgen" / "ui.log"


class ConsoleProgress:
    """One whole line per event on stdout, from any number of threads.

    The lock is not decoration, and it is not defensive habit either. Since
    Task 24 OvertureSource.fetch downloads its types concurrently and emits
    tile_done/tile_skipped from up to eight worker threads at once, and
    print() writes the text and the line ending as two separate calls on
    sys.stdout with nothing holding them together. A second thread landing
    between the two is what puts two events on one line and a stray blank
    line after them. During a survey this event stream is the whole of what
    a command line run shows the owner, so garbling it costs them the only
    view they have of a download that takes minutes.

    Checked rather than assumed for the other two sinks this project ships
    before this was written: EventLog (mapgen.jobs), which is what the
    browser path uses, appends under its own lock and was already safe;
    NullProgress holds no state at all. Those three are every ProgressSink
    in production, and a source emitting from threads is now a thing this
    project does, so any fourth has to be checked the same way.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: object) -> None:
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        line = f"[{event}] {detail}".rstrip()
        with self._lock:
            print(line, flush=True)


def _parse_bbox(value: str) -> BBox:
    try:
        return BBox.parse(value)
    except BBoxError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _add_survey_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--bbox", type=_parse_bbox, required=True,
                        help="west,south,east,north. Reversed pairs are normalised.")
    parser.add_argument("--region", required=True, help="Region folder, for example South Wales.")
    parser.add_argument("--site", required=True, help="Site name, for example Barry Waterfront.")
    parser.add_argument("--output-root", type=Path, default=None,
                        help="Where survey folders are created. Defaults to the saved config.")
    parser.add_argument("--tile-size-m", type=float, default=2000.0)
    parser.add_argument("--overlap-m", type=float, default=100.0)
    parser.add_argument("--source", action="append", dest="sources",
                        help="Repeatable. Defaults to osm and overture.")
    parser.add_argument("--overture-type", action="append", dest="overture_types")
    parser.add_argument(
        "--category", action="append", dest="categories",
        help="Repeatable. One of the ids `mapgen categories` lists. Filters OSM "
             "(Overpass path only, see that command's own notes) and Overture "
             "(unless --overture-type is also given, which wins outright). "
             "Defaults to every category, today's behaviour.",
    )
    parser.add_argument(
        "--demtype", dest="elevation_demtype", default=None,
        help="Which OpenTopography DEM the elevation layer downloads. Defaults to "
             "the saved config, which starts at COP30. Nothing here is higher "
             f"resolution than COP30: what changes is the kind of model. "
             f"Offered in the interface: {', '.join(OFFERED_DEMTYPE_IDS)}. "
             f"Also accepted: {', '.join(sorted(set(ALL_DEMTYPE_IDS) - set(OFFERED_DEMTYPE_IDS)))}.",
    )
    parser.add_argument("--date", type=_parse_date, default=None,
                        help="Survey date, ISO format. Defaults to today.")
    parser.add_argument("--keep-work", action="store_true",
                        help="Keep the _work tile folder after a successful run.")
    parser.add_argument("--coordinate-stem", action="store_true",
                        help="Use the legacy coordinate file stem instead of the readable one.")
    parser.add_argument("--force", action="store_true",
                        help="Continue past failed tiles and mark the package incomplete.")
    parser.add_argument("--skip-bridge", action="store_true",
                        help="Skip the Urbano bridge step.")


def _request_from_args(args: argparse.Namespace) -> SurveyRequest:
    from mapgen.config import load_config

    # Loaded once and read twice: --output-root and --demtype both fall
    # back to the saved config, and reading the file a second time for the
    # second field would be two chances to see two different states of it.
    config = load_config()
    output_root = args.output_root or config.output_root
    return SurveyRequest(
        bbox=args.bbox,
        region=args.region,
        site=args.site,
        output_root=Path(output_root),
        tile_size_m=args.tile_size_m,
        overlap_m=args.overlap_m,
        # `or` is safe here and unsafe in server.py's own version of this
        # line (see C1 there): argparse's append action leaves this None
        # when --source is never given and a non-empty list otherwise, so
        # there is no way to type an empty selection at a terminal. Kept
        # in the same explicit shape anyway, so the two entry points read
        # alike and nobody has to re-derive which of them can produce an
        # empty list.
        source_ids=(
            tuple(args.sources) if args.sources is not None else ("osm", "overture")
        ),
        overture_types=tuple(args.overture_types) if args.overture_types else None,
        categories=tuple(args.categories) if args.categories else None,
        # The flag wins over the saved setting, and neither one is
        # written back: a --demtype passed for one run is that run's
        # choice, not a new default. Same precedence --output-root has
        # always had, for the same reason.
        elevation_demtype=args.elevation_demtype or config.elevation_demtype,
        keep_work=args.keep_work,
        coordinate_stem=args.coordinate_stem,
        force=args.force,
        survey_date=args.date,
        run_bridge_step=not args.skip_bridge,
    )


def format_estimated_duration(seconds: float) -> str:
    """Seconds under a minute, whole minutes above it.

    Task 25 refitted every source's cost model against the concurrent,
    untiled code path that Tasks 23 and 24 left behind, and a run that
    used to be quoted at 28 minutes is now quoted at 3. That made a case
    reachable that had never come up while every estimate was inflated:
    a single-tile extent totals about 29 seconds, and the old
    unconditional "{seconds / 60:.0f} min" printed that as "0 min".

    Zero is the one answer that is not merely imprecise but wrong. It
    reads as "instant" for a job that takes half a minute, and it is the
    kind of number an owner stops trusting the whole panel over. The
    browser panel never had this problem because it has always floored
    at one minute (see app.js), which is a different choice and a worse
    one now that seconds are the honest unit at small extents.
    """
    if seconds < 60:
        return f"{seconds:.0f} s"
    return f"{seconds / 60:.0f} min"


def command_estimate(args: argparse.Namespace) -> int:
    estimate = estimate_survey(_request_from_args(args))
    if args.json:
        print(json.dumps(estimate, indent=2))
        return 0
    extent = estimate["extent_km"]
    print(f"Extent: {extent['width']:.2f} km x {extent['height']:.2f} km")
    print(f"Tiles: {estimate['tiles']} ({estimate['rows']} rows x {estimate['cols']} cols)")
    print(f"Estimated download: {estimate['bytes_estimate'] / 1e6:.0f} MB")
    print(
        f"Estimated duration: "
        f"{format_estimated_duration(estimate['seconds_estimate'])}"
    )
    for source in estimate["sources"]:
        print(f"  {source['display_name']}: {source['licence']}")
    return 0


def _empty_layer_lines(survey: dict) -> list[str]:
    """One line per layer that ran fine and found nothing.

    Task 30's ruling means such a layer leaves no merged file, so without
    this the owner opens the folder and finds a file missing with nothing
    anywhere on screen accounting for it. survey.json explains it; a run
    watched at a terminal should not need the file opened to learn it.

    A layer with failed tiles is deliberately excluded, however empty its
    output is. "Nothing was found here" and "we could not get it" are the
    two things this whole task exists to keep apart, and the failures have
    their own lines a few lines below this one.
    """
    failed = {
        record.get("source") for record in (survey.get("tile_failures") or [])
    }
    lines = []
    for entry in survey.get("sources") or []:
        if entry.get("id") in failed:
            continue
        if entry.get("features_merged") != 0 or entry.get("merged_files"):
            continue
        lines.append(
            f"{entry.get('id')}: nothing was found in this extent, so no file "
            f"was written."
        )
    return lines


def command_survey(args: argparse.Namespace) -> int:
    result = run_survey(_request_from_args(args), progress=ConsoleProgress())
    print(f"\nPackage: {result.paths.root}")
    for line in _empty_layer_lines(result.survey):
        print(line)
    bridge = result.survey.get("bridge") or {}
    if bridge.get("ok"):
        print(f"Urbano project setting: {result.paths.project_setting.name}")
    elif bridge.get("attempted"):
        # Urbano itself writes this file as part of a successful bridge run;
        # a failed bridge never produced it, whatever name survey.json's
        # stem predicts it would have had. Naming a file that does not
        # exist here would send the owner looking for it, or worse, into
        # Grasshopper pointed at nothing.
        print("Urbano project setting: not produced, the Urbano bridge step failed.")
        # A plain sentence, already produced by bridge.py or package.py, never
        # a stack trace: the survey data itself is unaffected by this failure.
        print(f"Urbano bridge step failed: {bridge.get('error')}", file=sys.stderr)
    else:
        print("Urbano project setting: not produced, the bridge step was skipped.")
    # Task 30, section 5: a scripted run must not be silent about what it
    # could not get. This is the same account survey.json carries and the
    # same one IncompleteSurveyError raises out of an unforced run,
    # composed by the same function, so the three cannot drift apart.
    # Printed even on a complete run, which cannot happen today (a failed
    # tile is never complete) but would be the more dangerous silence if
    # it ever did.
    failures = result.survey.get("tile_failures") or []
    # planned_tiles from the package's own record of its plan, not from a
    # recount here, so this prints exactly what IncompleteSurveyError
    # would have said for the same run (Task 32: a layer whose every
    # planned tile failed identically is one line, not seventy-two).
    planned = len(result.survey.get("tiles") or []) or None
    for line in describe_tile_failures(failures, planned_tiles=planned):
        print(line, file=sys.stderr)
    # Task 32, section 4: a run that completed only because a retry
    # worked is not the same run as one that never stumbled, and nothing
    # else here would say so. tile_failures is empty for a recovered
    # tile, deliberately, so without this line a fragile run and a clean
    # one print identically.
    recovered = [
        record
        for record in (result.survey.get("retries") or [])
        if record.get("recovered")
    ]
    if recovered:
        layers = ", ".join(sorted({str(record.get("source")) for record in recovered}))
        noun = "tile" if len(recovered) == 1 else "tiles"
        print(
            f"{len(recovered)} {noun} arrived only on a retry ({layers}). "
            f"See survey.json for which.",
            file=sys.stderr,
        )
    if not result.complete:
        print("Package is INCOMPLETE. See survey.json for which tiles failed.", file=sys.stderr)
        return 1
    return 0


def command_bridge(args: argparse.Namespace) -> int:
    """`mapgen bridge <package-dir>`: the Urbano files for a package that
    already exists, without downloading it again.

    Exits 1 on a bridge that ran and failed, where `mapgen survey` exits 0
    for the same failure. That is not an inconsistency. A survey whose
    bridge fails still delivered the OSM, Overture and elevation data it
    was asked for, which is most of what it was for; this command was asked
    for exactly one thing, so a failure here is the whole of it. The
    package is unharmed either way, and survey.json says what happened.
    """
    payload = bridge_package(args.package_dir, progress=ConsoleProgress())
    bridge = payload.get("bridge") or {}
    print(f"\nPackage: {args.package_dir}")
    if bridge.get("ok"):
        # Named from the package's own recorded stem, which is what was
        # handed to the bridge as --file-name-stem, so this is the file
        # that was actually just written rather than a prediction.
        print(f"Urbano project setting: {payload.get('urbano_stem')}_project_setting.json")
        return 0
    # A plain sentence, already produced by bridge.py, never a stack trace:
    # the survey data in the folder is untouched by this failure.
    print("Urbano project setting: not produced, the Urbano bridge step failed.")
    print(f"Urbano bridge step failed: {bridge.get('error')}", file=sys.stderr)
    return 1


def _install_windowless_safety() -> bool:
    """Redirects sys.stdout/sys.stderr to WINDOWLESS_LOG_PATH when there
    is no console attached, returning True if it actually did so.

    pythonw.exe, and the assets/make_shortcut.ps1 shortcut this backs, run
    as a GUI-subsystem process: Python itself sets sys.stdout and
    sys.stderr to None in that case, since there is no console for them to
    write to. Left alone, the very first ordinary print() anywhere in
    serve() (there are several, today) crashes immediately with
    AttributeError: 'NoneType' object has no attribute 'write', which is a
    WORSE failure than the one being reported, since the crash happens
    inside the code that would have written the original error down. A
    tool that fails invisibly is worse than one that fails loudly, and an
    AttributeError two frames from the real problem is its own kind of
    invisible: redirecting first means every existing print() keeps
    working completely unchanged, writing to a file instead of a console
    that was never going to exist either way.

    A real console (sys.stdout is not None) needs none of this: `mapgen
    ui` from a terminal must keep working exactly as it does today, so
    this is a no-op whenever a console is actually attached, windowless
    flag or not.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return False
    WINDOWLESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handle = open(WINDOWLESS_LOG_PATH, "a", encoding="utf-8", buffering=1)
    handle.write(
        f"\n--- mapgen ui --windowless started "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} ---\n"
    )
    sys.stdout = handle
    sys.stderr = handle
    return True


def _default_message_box(message: str) -> None:
    import ctypes

    MB_ICONERROR = 0x10
    ctypes.windll.user32.MessageBoxW(None, message, "mapgen", MB_ICONERROR)


def _report_windowless_failure(
    exc: BaseException, message_box: Callable[[str], None] = _default_message_box
) -> None:
    """The one thing a windowless launch can do that a silent crash under
    pythonw cannot: put an unmissable, native signal in front of the owner
    that something went wrong, since there is no console for a traceback
    to ever appear in. traceback.print_exc() below writes the full detail
    to sys.stderr, which _install_windowless_safety has already pointed at
    WINDOWLESS_LOG_PATH by the time this can ever be reached, so the
    message box itself only needs to be loud, not detailed.

    message_box is injectable so a test can prove this function is called
    on failure without a real MessageBoxW popping up and waiting on
    someone to click it. A failing message_box (no user32, running under
    something unexpected) must never mask the original exception or raise
    a second, different one out of a failure-reporting path, so it is
    swallowed, after the log write above has already happened regardless.
    """
    traceback.print_exc()
    message = (
        f"mapgen ui failed to start:\n{exc}\n\n"
        f"Full details were written to:\n{WINDOWLESS_LOG_PATH}"
    )
    try:
        message_box(message)
    except Exception:
        pass


def command_ui(args: argparse.Namespace) -> int:
    from mapgen.web.server import DEFAULT_HEARTBEAT_TIMEOUT_SECONDS, serve

    windowless = args.windowless
    if windowless:
        _install_windowless_safety()
    try:
        serve(
            open_browser=not args.no_browser,
            port=args.port,
            heartbeat_timeout_seconds=DEFAULT_HEARTBEAT_TIMEOUT_SECONDS if windowless else None,
        )
        return 0
    except Exception as exc:
        if not windowless:
            raise
        # Under a real console this re-raises and behaves exactly as
        # before (a traceback on stderr, a non-zero exit): only a
        # windowless launch, which has no console for that traceback to
        # ever reach, gets the extra, louder reporting.
        _report_windowless_failure(exc)
        return 1


def command_sources(args: argparse.Namespace) -> int:
    for source in available_sources():
        key = " (needs an API key)" if source.requires_api_key else ""
        print(f"{source.id}: {source.display_name}{key}")
        print(f"  {source.licence}")
    return 0


def command_categories(args: argparse.Namespace) -> int:
    """Lists every --category id, so the flag is discoverable without
    reading source. Road subtypes are indented under "roads": that
    grouping is a label only, "roads" itself is never a valid --category
    value, only its nine children are (see mapgen.categories).
    """
    road_labels = dict(ROAD_SUBTYPES)
    for group in CATEGORY_GROUPS:
        if not group.children:
            print(f"{group.id}: {group.label}")
            continue
        print(f"{group.label} (grouping label, not itself a --category value):")
        for subtype_id in group.children:
            print(f"  {subtype_id}: {road_labels[subtype_id]}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mapgen",
        description="Site survey data packaging for architectural work.",
    )
    parser.add_argument("--version", action="version", version=f"mapgen {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    survey = subparsers.add_parser("survey", help="Download and package a survey area.")
    _add_survey_arguments(survey)
    survey.set_defaults(func=command_survey)

    estimate = subparsers.add_parser("estimate", help="Report tiles, size and duration only.")
    _add_survey_arguments(estimate)
    estimate.add_argument("--json", action="store_true")
    estimate.set_defaults(func=command_estimate)

    ui = subparsers.add_parser("ui", help="Open the map picker in a browser.")
    ui.add_argument("--port", type=int, default=0, help="0 picks a free port.")
    ui.add_argument("--no-browser", action="store_true")
    ui.add_argument(
        "--windowless", action="store_true",
        help="For the desktop shortcut, not everyday terminal use: stops the server "
             "when the page is closed, and redirects output to "
             f"{WINDOWLESS_LOG_PATH} since a windowless launch has no console to print "
             "to. Plain `mapgen ui` is unaffected either way.",
    )
    ui.set_defaults(func=command_ui)

    # The inverse of --skip-bridge, which has existed since the bridge did.
    # Deliberately a command line only thing: the browser is for choosing an
    # extent and downloading it, and this operates on a folder that is
    # already finished.
    bridge = subparsers.add_parser(
        "bridge",
        help="Run the Urbano bridge over a survey package that already exists.",
    )
    bridge.add_argument(
        "package_dir", type=Path,
        help="The survey package folder, the one holding survey.json. Its own "
             "record says which layers it holds and over what extent; nothing "
             "is re-derived from the folder name.",
    )
    bridge.set_defaults(func=command_bridge)

    sources = subparsers.add_parser("sources", help="List available data sources.")
    sources.set_defaults(func=command_sources)

    categories = subparsers.add_parser("categories", help="List available --category ids.")
    categories.set_defaults(func=command_categories)

    # Legacy names, same machinery.
    for legacy in ("plan", "download", "merge", "urbano-package"):
        alias = subparsers.add_parser(legacy, help=f"Alias for survey ({legacy}).")
        _add_survey_arguments(alias)
        alias.set_defaults(
            func=command_estimate if legacy == "plan" else command_survey,
            json=False,
        )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    register_default_sources()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse has already printed its own specific message to stderr,
        # for example which required argument is missing or why --bbox
        # failed to parse, before ever raising this. That message names the
        # actual problem; a second, fixed message guessing it was the bbox
        # format would be wrong for every other kind of argument error, so
        # nothing is added here, only the traceback is stopped from escaping.
        return exc.code if isinstance(exc.code, int) else 2

    try:
        return args.func(args)
    except (
        NamingError,
        UnknownSourceError,
        BBoxError,
        TilingError,
        UnknownCategoryError,
        EmptyCategorySelectionError,
        EmptySourceSelectionError,
        UnknownDemTypeError,
        UnbridgeablePackageError,
        IncompleteSurveyError,
        OsmDownloadError,
        OvertureError,
        ElevationError,
    ) as exc:
        # A coordinator review's Important 2: this tuple covered a request
        # that could never be BUILT (a bad name, bbox, source id or
        # tiling), but not one that failed once it started actually
        # downloading. OsmDownloadError (NodeCapExceededError included, it
        # subclasses this), OvertureError and ElevationError, plus the new
        # UnknownCategoryError, all used to escape here as a raw, 20-line
        # Python traceback on the terminal instead of the same plain,
        # one-line message the browser path already gave the same
        # failures (JobManager's own except Exception: record.error =
        # str(exc) never had this gap; only this CLI path did). Every one
        # of these is already a deliberately plain, one-line message
        # written for exactly this purpose (see each class's own raise
        # sites); str(exc) here is not a fallback, it is what they were
        # always for. EmptyCategorySelectionError (Task 21) joins the
        # tuple the same way UnknownCategoryError did: it is a ValueError
        # already caught generically by server.py's _REQUEST_VALUE_ERRORS,
        # but this CLI path names its exceptions explicitly rather than
        # catching ValueError itself, so it needs the same one-line
        # addition here that every new request-validation error has.
        # UnknownDemTypeError (Task 28) is the next one along, added the
        # same way for the same reason, and EmptySourceSelectionError
        # (review finding C1) the one after that.
        # UnbridgeablePackageError (Task 29) is every refusal `mapgen
        # bridge` can make: a folder that is not there, a folder with no
        # survey.json, a survey.json that cannot be read, and a package
        # short of a file the bridge needs. Each is already one plain
        # sentence naming the folder and what to do next, which is the
        # whole reason they are exceptions rather than return codes.
        # IncompleteSurveyError (Task 30) is the same idea for the one
        # failure this list did not previously have to carry, because it
        # did not previously exist: an unforced run that ends with tiles
        # still missing. Its message is several lines rather than one,
        # naming every tile and why, and that is deliberate. It is the
        # command line half of "if nothing then it should say".
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
