"""Command line entry point.

The survey subcommand is the one that matters. plan, download, merge and
urbano-package are retained so existing muscle memory and any scripts keep
working, and they route through the same orchestration.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Sequence

from mapgen import __version__
from mapgen.categories import CATEGORY_GROUPS, ROAD_SUBTYPES
from mapgen.geo import BBox, BBoxError, TilingError
from mapgen.naming import NamingError
from mapgen.package import (
    SurveyRequest,
    estimate_survey,
    register_default_sources,
    run_survey,
)
from mapgen.sources.base import UnknownSourceError, available_sources


class ConsoleProgress:
    def emit(self, event: str, **fields: object) -> None:
        detail = " ".join(f"{key}={value}" for key, value in fields.items())
        print(f"[{event}] {detail}".rstrip(), flush=True)


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

    output_root = args.output_root or load_config().output_root
    return SurveyRequest(
        bbox=args.bbox,
        region=args.region,
        site=args.site,
        output_root=Path(output_root),
        tile_size_m=args.tile_size_m,
        overlap_m=args.overlap_m,
        source_ids=tuple(args.sources or ("osm", "overture")),
        overture_types=tuple(args.overture_types) if args.overture_types else None,
        categories=tuple(args.categories) if args.categories else None,
        keep_work=args.keep_work,
        coordinate_stem=args.coordinate_stem,
        force=args.force,
        survey_date=args.date,
        run_bridge_step=not args.skip_bridge,
    )


def command_estimate(args: argparse.Namespace) -> int:
    estimate = estimate_survey(_request_from_args(args))
    if args.json:
        print(json.dumps(estimate, indent=2))
        return 0
    extent = estimate["extent_km"]
    print(f"Extent: {extent['width']:.2f} km x {extent['height']:.2f} km")
    print(f"Tiles: {estimate['tiles']} ({estimate['rows']} rows x {estimate['cols']} cols)")
    print(f"Estimated download: {estimate['bytes_estimate'] / 1e6:.0f} MB")
    print(f"Estimated duration: {estimate['seconds_estimate'] / 60:.0f} min")
    for source in estimate["sources"]:
        print(f"  {source['display_name']}: {source['licence']}")
    return 0


def command_survey(args: argparse.Namespace) -> int:
    result = run_survey(_request_from_args(args), progress=ConsoleProgress())
    print(f"\nPackage: {result.paths.root}")
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
    if not result.complete:
        print("Package is INCOMPLETE. See survey.json for which tiles failed.", file=sys.stderr)
        return 1
    return 0


def command_ui(args: argparse.Namespace) -> int:
    from mapgen.web.server import serve

    serve(open_browser=not args.no_browser, port=args.port)
    return 0


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
    ui.set_defaults(func=command_ui)

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
    except (NamingError, UnknownSourceError, BBoxError, TilingError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
