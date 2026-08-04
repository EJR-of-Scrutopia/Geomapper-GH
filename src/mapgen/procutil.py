"""Running child processes without a console window, and in UTF-8.

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

import os
import subprocess
import sys

# getattr rather than a bare attribute: CREATE_NO_WINDOW only exists in
# subprocess on Windows, so naming it directly would raise AttributeError at
# import time on any other platform, which the test suite and CI both are.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def child_environment() -> dict[str, str]:
    """This process's environment, plus PYTHONUTF8=1 for the child.

    A Python child that opens its output with a bare open(path, "w")
    encodes it in the machine's ANSI codepage, cp1252 here. Anything
    outside cp1252 then kills it mid-write. That is not hypothetical:
    overturemaps 0.20.0 does exactly this and dies on the y-circumflex in
    Welsh names. Downloading `place` over a Barry extent stops after
    337,454 of 1,056,802 bytes with

        UnicodeEncodeError: 'charmap' codec can't encode character
        '\\u0177' in position 443

    The owner's entire subject matter is Welsh sites, so this is their
    normal input, not an edge case. PYTHONUTF8=1 puts the child in UTF-8
    mode and the same download completes in full. Verified against 0.20.0
    on this machine both ways.

    Set here for every child rather than at the one call site that needs it
    today, because the rule is a property of this project and not of one
    dependency: nothing mapgen spawns may let the machine's codepage decide
    how our data gets written, and the failure it prevents is a silently
    truncated data file. A phase 2 source that shells out to another Python
    CLI gets it without anyone having to remember. Inert for a child that
    is not a Python program, which the dotnet bridge is not: it ignores the
    variable entirely, and that is the honest reason this is safe to set
    unconditionally rather than a claim that every child needs it.

    A full copy of os.environ, never a bare {"PYTHONUTF8": "1"}: passing
    env at all REPLACES the child's whole environment, so a partial dict
    would strip PATH and every child would fail to start.
    """
    return {**os.environ, "PYTHONUTF8": "1"}


def hidden_process_kwargs() -> dict[str, object]:
    """The keyword arguments that keep a child process off the screen.

    Empty on anything but Windows, where no console is allocated for a
    child in the first place and the flag does not exist.
    """
    if sys.platform != "win32":
        return {}
    return {"creationflags": _CREATE_NO_WINDOW}


def run_hidden(command, runner=subprocess.run, **kwargs):
    """subprocess.run with no console window, UTF-8 in and UTF-8 out.

    runner is injectable for the same reason it already was at both call
    sites: the tests drive a fake rather than a real executable. A fake
    receives every argument exactly as the real subprocess.run would, which
    is what lets a test assert an argument is genuinely being passed rather
    than trusting that it is.

    encoding and errors are set here, not only env, because the two have to
    agree or fixing one bug introduces another. Forcing a Python child into
    UTF-8 mode (see child_environment) means it writes UTF-8 to stderr too,
    and text=True decodes with the parent's locale codepage under a STRICT
    error handler by default. cp1252 leaves 0x81, 0x8D, 0x8F, 0x90 and 0x9D
    undefined, and those are ordinary UTF-8 continuation bytes, so a child
    that merely mentioned a name like "Łódź" in an error would raise
    UnicodeDecodeError inside subprocess.run, in the parent, as an
    unhandled exception rather than the clean OvertureError the caller is
    ready for. Checked, not assumed: b"\\xc5\\x81".decode("cp1252") raises.
    Decoding as UTF-8 matches what we just told the child to emit, and
    errors="replace" means no child's output can ever crash its own caller.

    bridge.py already passed errors="replace" for exactly this hazard on
    its own child before this was hoisted; its explicit argument still
    works and still wins, since caller kwargs are applied last. That is
    also why these are merged into a dict rather than passed as literal
    keywords: a caller repeating one of them would otherwise be a duplicate
    keyword argument TypeError, which is a silly way for bridge.py to break.

    The one cost is honest: the dotnet bridge is not a Python program, so
    if it ever wrote non-ASCII in the console codepage those bytes now read
    as replacement characters instead of the right ones. Its output in
    practice is DLL paths and .NET exception names, and it could already
    lose them to errors="replace" before this change.
    """
    options = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "check": False,
        "env": child_environment(),
        **hidden_process_kwargs(),
    }
    options.update(kwargs)
    return runner(command, **options)
