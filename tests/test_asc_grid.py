"""asc_grid.py's suite, against ESRI ASCII grid text built by hand.

The NRW archive members this parser exists for are 2000x2000 and in
millimetres; the grids built here are small stand-ins for the same shape
(header, then north-first rows), so what is under test is the parsing
rule rather than a fixture the size of a real download.
"""

from __future__ import annotations

import math
import time

import pytest

from mapgen.asc_grid import AscGridError, parse_asc, parse_asc_header

_HEADER = (
    "ncols 3\n"
    "nrows 2\n"
    "xllcorner 310500\n"
    "yllcorner 176500\n"
    "cellsize 0.25\n"
    "NODATA_value -9999\n"
)


def test_parses_grid_shape_bounds_and_nodata_placement() -> None:
    text = _HEADER + (
        "1.0 2.0 -9999\n"
        "4.0 5.0 6.0\n"
    )
    window = parse_asc(text)
    assert window.width == 3
    assert window.height == 2
    assert window.pixel_size == pytest.approx(0.25)
    assert window.e_origin == pytest.approx(310500.0)
    # yllcorner + nrows * cellsize = 176500 + 2 * 0.25
    assert window.n_top == pytest.approx(176500.5)
    # North row (first) at index 0..2, south row (second) at index 3..5.
    assert window.values[0] == pytest.approx(1.0)
    assert window.values[1] == pytest.approx(2.0)
    assert math.isnan(window.values[2])
    assert window.values[3] == pytest.approx(4.0)
    assert window.values[4] == pytest.approx(5.0)
    assert window.values[5] == pytest.approx(6.0)


def test_value_scale_converts_millimetres_to_metres() -> None:
    text = _HEADER + (
        "85321 0 0\n"
        "0 0 0\n"
    )
    window = parse_asc(text, value_scale=0.001)
    assert window.values[0] == pytest.approx(85.321, abs=1e-3)


def test_crlf_and_lf_parse_identically() -> None:
    lf_text = _HEADER + (
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    crlf_text = lf_text.replace("\n", "\r\n")
    lf_window = parse_asc(lf_text)
    crlf_window = parse_asc(crlf_text)
    assert list(crlf_window.values) == list(lf_window.values)
    assert crlf_window.width == lf_window.width
    assert crlf_window.height == lf_window.height
    assert crlf_window.e_origin == lf_window.e_origin
    assert crlf_window.n_top == lf_window.n_top


def test_header_keys_are_case_and_order_insensitive() -> None:
    text = (
        "NCOLS 3\n"
        "CellSize 0.25\n"
        "NROWS 2\n"
        "YllCorner 176500\n"
        "XLLCORNER 310500\n"
        "nodata_value -9999\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    window = parse_asc(text)
    assert window.width == 3
    assert window.height == 2
    assert window.e_origin == pytest.approx(310500.0)
    assert window.n_top == pytest.approx(176500.5)


def test_missing_ncols_raises_naming_it() -> None:
    text = (
        "nrows 2\n"
        "xllcorner 310500\n"
        "yllcorner 176500\n"
        "cellsize 0.25\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="ncols"):
        parse_asc(text)


def test_short_row_raises_with_row_number() -> None:
    text = _HEADER + (
        "1.0 2.0 3.0\n"
        "4.0 5.0\n"
    )
    with pytest.raises(AscGridError, match="2"):
        parse_asc(text)


def test_scientific_notation_value() -> None:
    text = _HEADER + (
        "1.5e2 0 0\n"
        "0 0 0\n"
    )
    window = parse_asc(text)
    assert window.values[0] == pytest.approx(150.0)


def test_parse_asc_header_returns_six_keys_without_touching_values() -> None:
    # The value rows are garbage: parse_asc_header must never reach them.
    text = _HEADER + "not a number at all, this would blow up a float parse\n"
    header = parse_asc_header(text)
    assert header == {
        "ncols": 3,
        "nrows": 2,
        "xllcorner": 310500.0,
        "yllcorner": 176500.0,
        "cellsize": 0.25,
        "nodata_value": -9999.0,
    }


def test_parse_asc_header_defaults_nodata_value() -> None:
    text = (
        "ncols 3\n"
        "nrows 2\n"
        "xllcorner 310500\n"
        "yllcorner 176500\n"
        "cellsize 0.25\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    header = parse_asc_header(text)
    assert header["nodata_value"] == pytest.approx(-9999.0)


def test_asc_grid_error_never_carries_a_url() -> None:
    text = "not a header at all\n"
    try:
        parse_asc(text)
    except AscGridError as exc:
        assert "http" not in str(exc).lower()
    else:
        pytest.fail("expected AscGridError")


def test_cellsize_zero_raises_naming_it() -> None:
    text = (
        "ncols 3\n"
        "nrows 2\n"
        "xllcorner 310500\n"
        "yllcorner 176500\n"
        "cellsize 0\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="cellsize"):
        parse_asc(text)


def test_cellsize_negative_raises_naming_it() -> None:
    text = (
        "ncols 3\n"
        "nrows 2\n"
        "xllcorner 310500\n"
        "yllcorner 176500\n"
        "cellsize -0.25\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="cellsize"):
        parse_asc(text)


def test_non_finite_corner_raises_naming_it() -> None:
    text = (
        "ncols 3\n"
        "nrows 2\n"
        "xllcorner nan\n"
        "yllcorner 176500\n"
        "cellsize 0.25\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="xllcorner"):
        parse_asc(text)


def test_nan_value_token_raises_with_row_number() -> None:
    text = _HEADER + (
        "nan 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="row 1"):
        parse_asc(text)


def test_inf_value_token_raises_with_row_number() -> None:
    text = _HEADER + (
        "1.0 2.0 3.0\n"
        "inf 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="row 2"):
        parse_asc(text)


def test_over_long_row_raises_like_a_short_row() -> None:
    text = _HEADER + (
        "1.0 2.0 3.0 4.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="row 1"):
        parse_asc(text)


def test_duplicate_header_key_raises() -> None:
    text = (
        "ncols 3\n"
        "nrows 2\n"
        "xllcorner 310500\n"
        "yllcorner 176500\n"
        "cellsize 0.25\n"
        "ncols 999\n"
        "1.0 2.0 3.0\n"
        "4.0 5.0 6.0\n"
    )
    with pytest.raises(AscGridError, match="ncols"):
        parse_asc(text)


def test_garbage_value_block_raises_quickly_instead_of_scanning() -> None:
    """A value block that never starts with a numeric token must not turn
    the header-only read into a full-file scan.

    Each garbage line carries a distinct, non-numeric first token so the
    duplicate-key check does not short-circuit this before the line-count
    bound does: this is specifically exercising that bound. Enough lines
    that an unbounded scan would show up as a fraction of a second, not
    merely as an exception.
    """
    garbage = "".join(f"junk{i} value\n" for i in range(200_000))
    text = _HEADER + garbage
    started = time.perf_counter()
    with pytest.raises(AscGridError, match="no value row"):
        parse_asc_header(text)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.05
