"""No console windows for child processes.

The owner ran a survey from the desktop shortcut and had a console window
flash up every few seconds for the length of the download, one per Overture
tile per type. They closed one, which killed the process inside it, and the
run failed with "overturemaps failed for tile r00_c04, type segment:" and
nothing after the colon, because the child died before writing any
diagnostic. These pin the flag that prevents both.
"""

import subprocess
import sys

from mapgen.procutil import hidden_process_kwargs, run_hidden


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


def test_hidden_process_kwargs_is_empty_off_windows():
    kwargs = hidden_process_kwargs()
    if sys.platform == "win32":
        assert kwargs == {"creationflags": subprocess.CREATE_NO_WINDOW}
    else:
        assert kwargs == {}
