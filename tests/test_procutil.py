"""No console windows for child processes.

The owner ran a survey from the desktop shortcut and had a console window
flash up every few seconds for the length of the download, one per Overture
tile per type. They closed one, which killed the process inside it, and the
run failed with "overturemaps failed for tile r00_c04, type segment:" and
nothing after the colon, because the child died before writing any
diagnostic. These pin the flag that prevents both.
"""

import os
import subprocess
import sys

from mapgen.procutil import child_environment, hidden_process_kwargs, run_hidden


class RecordingRunner:
    """Stands in for subprocess.run and keeps the kwargs it was handed.

    The point of the tests below is that the creationflags argument reaches
    the real subprocess.run, so a double that quietly accepted anything
    would defeat them: this one records exactly what arrived.
    """

    def __init__(self):
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")


def test_run_hidden_passes_create_no_window_on_windows():
    runner = RecordingRunner()
    run_hidden(["some-tool", "--flag"], runner=runner)
    _command, kwargs = runner.calls[0]
    if sys.platform == "win32":
        assert kwargs["creationflags"] == subprocess.CREATE_NO_WINDOW
    else:
        # No console is allocated for a child on other platforms and the
        # flag does not exist there, so passing one would be a TypeError.
        assert "creationflags" not in kwargs


def test_run_hidden_still_captures_output_as_text():
    # Hiding the window must not change what the callers rely on: both
    # read result.stdout/result.stderr to build their own error messages.
    runner = RecordingRunner()
    run_hidden(["some-tool"], runner=runner)
    _command, kwargs = runner.calls[0]
    assert kwargs["capture_output"] is True
    assert kwargs["text"] is True
    assert kwargs["check"] is False


def test_run_hidden_forwards_extra_kwargs():
    # bridge.py passes errors="replace"; losing it would turn a decoding
    # failure in the bridge's output into an exception instead of a
    # readable message.
    runner = RecordingRunner()
    run_hidden(["some-tool"], runner=runner, errors="replace")
    _command, kwargs = runner.calls[0]
    assert kwargs["errors"] == "replace"


def test_hidden_process_kwargs_matches_this_platform():
    # Review finding N10: this was named ..._is_empty_off_windows and,
    # on the only machine that runs this suite, took its win32 arm and
    # checked the opposite of its own name. Renamed to say what it
    # actually does. The non-Windows arm below is unreachable here and is
    # kept as the statement of intent for a platform this project has no
    # machine to run on, not as something being exercised.
    kwargs = hidden_process_kwargs()
    if sys.platform == "win32":
        assert kwargs == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:  # pragma: no cover - no non-Windows machine runs this suite
        assert kwargs == {}


# --- every child runs in UTF-8 mode ------------------------------------
#
# overturemaps 0.20.0 writes its GeoJSON with a bare open(path, "w"), so it
# encodes in the machine's ANSI codepage. Under cp1252, which is this
# machine, it dies part way through on the y-circumflex in a Welsh name and
# leaves a truncated file: `place` over a Barry extent stops at 337,454 of
# 1,056,802 bytes. The owner surveys Welsh sites, so that is their ordinary
# input. PYTHONUTF8=1 in the child's environment is the fix, verified
# against 0.20.0 both ways.


def test_the_child_environment_forces_utf8_mode():
    assert child_environment()["PYTHONUTF8"] == "1"


def test_the_child_environment_keeps_the_rest_of_the_environment():
    # Passing env at all REPLACES the child's whole environment, so a bare
    # {"PYTHONUTF8": "1"} would strip PATH and every child would fail to
    # start with FileNotFoundError. This is the difference between fixing
    # an encoding bug and breaking every download.
    environment = child_environment()
    for name, value in os.environ.items():
        if name == "PYTHONUTF8":
            continue
        assert environment[name] == value
    assert len(environment) >= len(os.environ)


def test_run_hidden_puts_the_child_in_utf8_mode():
    runner = RecordingRunner()
    run_hidden(["some-tool"], runner=runner)
    _command, kwargs = runner.calls[0]
    assert kwargs["env"]["PYTHONUTF8"] == "1"
    assert "PATH" in kwargs["env"] or "Path" in kwargs["env"]


def test_run_hidden_decodes_the_child_as_utf8_and_never_strictly():
    # These have to agree with the env above or fixing one bug introduces
    # another. A child forced into UTF-8 mode writes UTF-8 to stderr too,
    # and text=True decodes with the parent's locale codepage under a
    # STRICT handler by default. cp1252 leaves 0x81, 0x8D, 0x8F, 0x90 and
    # 0x9D undefined, and those are ordinary UTF-8 continuation bytes, so a
    # child that merely mentioned a name like the one below in an error
    # would raise UnicodeDecodeError inside subprocess.run, in the PARENT,
    # as an unhandled exception rather than the clean error its caller is
    # ready to turn into a message.
    assert b"\xc5\x81" == "Ł".encode("utf-8")
    try:
        b"\xc5\x81".decode("cp1252")
        raise AssertionError(
            "cp1252 decoded a byte it has no mapping for, so the hazard this "
            "test describes is not real and the reasoning needs revisiting"
        )
    except UnicodeDecodeError:
        pass

    runner = RecordingRunner()
    run_hidden(["some-tool"], runner=runner)
    _command, kwargs = runner.calls[0]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"


def test_a_caller_can_still_override_any_default():
    # bridge.py has passed errors="replace" for its own child since before
    # this was hoisted into the default. Passing these as literal keyword
    # arguments alongside **kwargs would make that call a duplicate keyword
    # argument TypeError, which is a silly way for the bridge to break.
    runner = RecordingRunner()
    run_hidden(["some-tool"], runner=runner, errors="replace", encoding="latin-1")
    _command, kwargs = runner.calls[0]
    assert kwargs["errors"] == "replace"
    assert kwargs["encoding"] == "latin-1"


def test_the_bridge_call_shape_still_works():
    # The exact call bridge.py makes, as a regression guard on the above.
    from mapgen.bridge import run_bridge  # noqa: F401  (import shape only)

    runner = RecordingRunner()
    run_hidden(["dotnet", "run"], runner=runner, errors="replace")
    assert runner.calls, "the bridge's own call shape raised instead of running"
