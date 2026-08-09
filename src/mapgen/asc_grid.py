"""Reading an ESRI ASCII grid, the format inside the NRW archive's zips.

Natural Resources Wales' historic LiDAR archive (`docs`'s Item D; see
`.superpowers/sdd/2026-08-09-mapgen-phase2b-d-cardiff-lidar/probe-report.md`)
ships one zip per 10 km block per resolution per year per product, and every
zip member probed live on 2026-08-08 was this format rather than a TIFF:
`dsm_D0147883_20120330_20120330_mm_units.asc`, a plain text header followed
by the grid itself, one row per line. The real members are 2000x2000 cells,
CRLF terminated, and hold heights in MILLIMETRES (the filename says so), so
Task 4 calls `parse_asc` with `value_scale=0.001`. `parse_asc_header` exists
so a caller can check a member's bounds and cell size cheaply, without
converting four million tokens to float first.

A real header, whitespace separated and in the order NRW happens to write
it, but read here in any order and any case:

    ncols        2000
    nrows        2000
    xllcorner    310500
    yllcorner    176500
    cellsize     0.25
    NODATA_value -9999

The grid's own convention is that the first row is the NORTH edge, which is
also `BngWindow`'s convention, so no flip happens here. `xllcorner` and
`yllcorner` are the SOUTH-WEST corner, so the window's `n_top` (its north
edge, per `cog.py`'s own docstring) is `yllcorner + nrows * cellsize`
rather than `yllcorner` itself.
"""

from __future__ import annotations

import math
from array import array

from mapgen.cog import BngWindow

_REQUIRED_INT_KEYS = ("ncols", "nrows")
_REQUIRED_FLOAT_KEYS = ("xllcorner", "yllcorner", "cellsize")
_DEFAULT_NODATA = -9999.0


class AscGridError(RuntimeError):
    """Raised when an ESRI ASCII grid's header or values cannot be read.

    Every message names the header key or row number at fault, and never
    the zip or URL the text came from: a source address is not this
    module's business to repeat back in a failure sentence.
    """


def parse_asc_header(text: str) -> dict:
    """The six header keys, read without looking at a single value row.

    Stops at the first line whose first token parses as a number, which is
    the ESRI convention for where the header ends: it never scans further
    into `text`, so a caller can bounds-check a member many megabytes long
    for the cost of reading half a dozen short lines.
    """
    header, _ = _read_header(text)
    return header


def parse_asc(text: str, value_scale: float = 1.0) -> BngWindow:
    """The whole grid as a `BngWindow`, north-first, nodata turned to NaN.

    `value_scale` multiplies every value that is not nodata; the NRW
    archive's own members are in millimetres, so Task 4 passes 0.001.
    """
    header, data_start = _read_header(text)
    ncols = header["ncols"]
    nrows = header["nrows"]
    nodata = header["nodata_value"]

    values = array("f")
    rows = _iter_lines(text, data_start)
    for row_number in range(1, nrows + 1):
        try:
            line = next(rows)
        except StopIteration:
            raise AscGridError(
                f"ASCII grid ends after {row_number - 1} of {nrows} rows."
            ) from None
        tokens = line.split()
        if len(tokens) < ncols:
            raise AscGridError(
                f"ASCII grid row {row_number} holds {len(tokens)} values, "
                f"short of the {ncols} the header declares."
            )
        for token in tokens[:ncols]:
            try:
                value = float(token)
            except ValueError:
                raise AscGridError(
                    f"ASCII grid row {row_number} holds {token!r}, which is "
                    f"not a number."
                ) from None
            values.append(math.nan if value == nodata else value * value_scale)

    return BngWindow(
        e_origin=header["xllcorner"],
        n_top=header["yllcorner"] + nrows * header["cellsize"],
        pixel_size=header["cellsize"],
        width=ncols,
        height=nrows,
        values=values,
    )


def _read_header(text: str) -> tuple[dict, int]:
    """The header as a coerced dict, plus where its first data line starts.

    `pos` walks `text` one line at a time; the loop breaks the moment a
    line's first token parses as a number, leaving `pos` at the start of
    that line so `parse_asc` can resume reading rows from exactly there
    without re-scanning anything this function already looked at.
    """
    raw: dict[str, str] = {}
    pos = 0
    length = len(text)
    while True:
        newline = text.find("\n", pos)
        end = length if newline == -1 else newline
        line = text[pos:end]
        if line.endswith("\r"):
            line = line[:-1]
        tokens = line.split()
        if not tokens:
            if newline == -1:
                break
            pos = newline + 1
            continue
        if _is_number(tokens[0]):
            break
        if len(tokens) < 2:
            raise AscGridError(
                f"ASCII grid header line {tokens[0]!r} has no value."
            )
        raw[tokens[0].lower()] = tokens[1]
        if newline == -1:
            pos = length
            break
        pos = newline + 1
    return _coerce_header(raw), pos


def _coerce_header(raw: dict[str, str]) -> dict:
    header: dict[str, float] = {}
    for key in _REQUIRED_INT_KEYS:
        text_value = raw.get(key)
        if text_value is None:
            raise AscGridError(f"ASCII grid header is missing '{key}'.")
        try:
            value = int(text_value)
        except ValueError:
            raise AscGridError(
                f"ASCII grid header's '{key}' ({text_value!r}) is not a "
                f"whole number."
            ) from None
        if value <= 0:
            raise AscGridError(
                f"ASCII grid header's '{key}' ({value}) must be positive."
            )
        header[key] = value
    for key in _REQUIRED_FLOAT_KEYS:
        text_value = raw.get(key)
        if text_value is None:
            raise AscGridError(f"ASCII grid header is missing '{key}'.")
        try:
            header[key] = float(text_value)
        except ValueError:
            raise AscGridError(
                f"ASCII grid header's '{key}' ({text_value!r}) is not a "
                f"number."
            ) from None
    nodata_text = raw.get("nodata_value")
    if nodata_text is None:
        header["nodata_value"] = _DEFAULT_NODATA
    else:
        try:
            header["nodata_value"] = float(nodata_text)
        except ValueError:
            raise AscGridError(
                f"ASCII grid header's 'nodata_value' ({nodata_text!r}) is "
                f"not a number."
            ) from None
    return header


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _iter_lines(text: str, start: int):
    """Lines from `start` to the end of `text`, CRLF and LF alike."""
    pos = start
    length = len(text)
    while pos <= length:
        newline = text.find("\n", pos)
        end = length if newline == -1 else newline
        line = text[pos:end]
        if line.endswith("\r"):
            line = line[:-1]
        yield line
        if newline == -1:
            return
        pos = newline + 1
