"""Running child processes without putting a console window on screen.

Every external tool mapgen shells out to (the overturemaps CLI, the dotnet
Urbano bridge) is a console executable. On Windows, starting one of those
creates a console window for it, and that happens whether or not its output
is being captured: capturing redirects the stdio handles, it does not stop
the OS allocating a console. When mapgen itself runs under pythonw.exe from
the desktop shortcut, with no console of its own to inherit, each child gets
a brand new window.

The owner hit both halves of this during one download. A survey of a modest
area was, at the time, one CLI call per tile per Overture type, so windows
flashed up every few seconds for the length of the run. Worse, they are real
windows: closing one kills the child process inside it. That is exactly what
happened, and because the process died before writing anything to stderr,
the failure reached the log as "overturemaps failed for tile r00_c04, type
segment:" with nothing after the colon, which explains nothing to anyone.

Task 23 stopped tiling Overture, so that is now one call per type rather
than one per tile per type: eight windows on a full run instead of a
hundred and sixty. Fewer chances to hit this, not a reason to stop hiding
them, and the dotnet bridge child was never tiled to begin with.

CREATE_NO_WINDOW fixes both at once: no window to flash, and none to close
by accident. It is Windows-only, so it is resolved once here rather than
being remembered at each call site. Phase 2 adds more sources, some of which
will shell out too; they get this for free by calling run_hidden.
"""

from __future__ import annotations

import subprocess
import sys

# getattr rather than a bare attribute: CREATE_NO_WINDOW only exists in
# subprocess on Windows, so naming it directly would raise AttributeError at
# import time on any other platform, which the test suite and CI both are.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def hidden_process_kwargs() -> dict[str, object]:
    """The keyword arguments that keep a child process off the screen.

    Empty on anything but Windows, where no console is allocated for a
    child in the first place and the flag does not exist.
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": _CREATE_NO_WINDOW}


def run_hidden(command, runner=subprocess.run, **kwargs):
    """subprocess.run with no console window, output captured as text.

    runner is injectable for the same reason it already was at both call
    sites: the tests drive a fake rather than a real executable. A fake
    receives the creationflags argument exactly as the real subprocess.run
    would, which is what lets a test assert the flag is actually being
    passed rather than trusting that it is.
    """
    return runner(
        command,
        capture_output=True,
        text=True,
        check=False,
        **hidden_process_kwargs(),
        **kwargs,
    )
