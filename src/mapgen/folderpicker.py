"""A real folder dialog, opened in a child process.

Why a child process, and not a dialog in the handler thread
-----------------------------------------------------------

A browser cannot hand a web page a real filesystem path. That is a
deliberate security boundary, and no amount of webkitdirectory gets round
it: the page can learn a file's NAME and its bytes, never where it lives.
But this page is served by a local server running as the owner, for the
owner, so the dialog belongs on the server side, which is what this module
is.

The server is a ThreadingHTTPServer, so the obvious implementation is to
call tkinter.filedialog.askdirectory straight from the handler thread.
Three separate reasons not to, and only the first is the well-known one:

  * Tk is not reliably usable off the main thread. Tcl's own threading
    model expects every call on an interpreter to come from the thread
    that created it, and a violation is not an exception a caller can
    catch: Tcl panics, and a Tcl panic calls abort(), which takes the
    whole process down. A mis-stepped dialog would therefore not fail the
    one request, it would kill the server mid-download.
  * A dialog waits on a person, and a person can walk away. A blocking
    call inside a handler thread has no way to be given up on; a child
    process can simply be killed, which is exactly what the timeout below
    does.
  * The server process is the one that owns a running job. Anything that
    can crash it can lose a survey that is half downloaded, and a
    convenience button for typing a path is nowhere near worth that risk.

The child is spawned through mapgen.procutil, not a bare subprocess.run,
for the two things procutil already knows about this machine: no console
window flashes up (CREATE_NO_WINDOW), and the child runs in UTF-8 mode so
a Welsh path survives the trip home rather than dying in cp1252. The owner
surveys Welsh sites, so a path with a circumflex in it is ordinary input
here, not an edge case.

CREATE_NO_WINDOW suppresses a CONSOLE window, not a GUI window, and that
was confirmed rather than assumed before this was written: a Tk root
created in a child spawned exactly the way run_hidden spawns one reported
winfo_ismapped() and winfo_viewable() both true, at its real size, on
screen. The dialog appears. What the flag prevents is the black console
box that would otherwise appear beside it.

This is a convenience over a control that already works, never a
replacement for it. Every failure below leaves the typed path field
exactly as it was: see the module's own error classes, each of which the
route turns into a plain message rather than a change to the field.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

from mapgen.procutil import run_hidden

# The single line the child writes on stdout, marked so the parent parses
# a line it recognises rather than trusting that nothing else in a Python
# startup, a Tk initialisation or a shell profile ever prints anything.
RESULT_MARKER = "MAPGEN-FOLDER "

# Long enough to find a folder, short enough that a dialog nobody is
# looking at does not hold a handler thread open indefinitely.
#
# The number is a judgement, not a measurement, so here is the judgement:
# the cost of it being too short is that the dialog closes and the owner
# clicks the button again, losing nothing at all, because a cancelled or
# timed-out pick never touches the field. The cost of it being too long,
# or absent, is a server thread and an orphan window belonging to a dialog
# that opened behind the browser and was never seen. Two minutes is well
# past navigating to a folder on this machine or a mapped drive, including
# creating a new one in the dialog, and well short of "this was forgotten
# about".
DEFAULT_TIMEOUT_SECONDS = 120.0

# One dialog at a time, process wide. The button in the page disables
# itself while a pick is in flight, which covers the ordinary case; this
# covers the ones it cannot, chiefly reloading the page while a dialog is
# open and clicking again, which would otherwise stack a second native
# window on top of the first with no way to tell which answer belongs to
# which request.
_dialog_in_progress = threading.Lock()


def dialog_is_open() -> bool:
    """Whether a folder dialog is on somebody's screen right now.

    Read by the web server's heartbeat watchdog (review finding I7). The
    watchdog's only exemption was a running survey job, which knows
    nothing about this module, so closing the browser while the native
    dialog was open shut the server down within a second of the pagehide
    beacon. ThreadingHTTPServer sets daemon_threads = True and
    ThreadingMixIn does not track daemon threads, so server_close() never
    joins the handler thread blocked inside subprocess.run: the process
    exited, that thread died mid-call, run_hidden never reached its own
    timeout kill, and choose_directory's finally never ran. The dialog
    was left on the desktop belonging to nothing, which is exactly the
    "process behind that only Task Manager can end" the watchdog was
    built to prevent.

    The two timeouts made it certain rather than unlikely: the dialog
    waits 120 seconds and the heartbeat gives up after 90.

    Lock.locked() rather than a counter of our own: this is the same lock
    choose_directory already acquires for the whole life of the child and
    releases in a finally, so there is no second piece of state here to
    fall out of step with the first. It can go stale by at most the
    length of one dialog, since the timeout kills the child regardless.
    """
    return _dialog_in_progress.locked()


class FolderPickerError(RuntimeError):
    """Base class: the pick did not produce a path, for some reason the
    caller should report plainly and then leave the field alone."""


class FolderPickerUnavailable(FolderPickerError):
    """The dialog could not be opened at all.

    No tkinter in this interpreter, no desktop session to draw on, an
    interpreter that cannot re-launch itself, or a child that died before
    saying anything. The interface must fall back to the text field it
    already has, which never stopped working.
    """


class FolderPickerTimeout(FolderPickerError):
    """The dialog was open too long with nothing chosen, so it was closed."""


class FolderPickerBusy(FolderPickerError):
    """A dialog is already open. Only one at a time; see _dialog_in_progress."""


def _child_command(initial_dir: str | None) -> list[str]:
    """This same interpreter, re-launched on this module.

    sys.executable rather than a hardcoded "python": under the desktop
    shortcut it is pythonw.exe, which is the right thing to spawn (a GUI
    subsystem process for a GUI dialog, and no console to suppress in the
    first place). Confirmed that a pythonw child still writes to a
    captured pipe: sys.stdout is only None when there is no valid handle,
    and run_hidden always gives it one.
    """
    if not sys.executable:
        raise FolderPickerUnavailable(
            "The folder picker needs to start a helper process and this "
            "Python has no executable path to start. Type the folder path "
            "instead."
        )
    command = [sys.executable, "-m", "mapgen.folderpicker"]
    if initial_dir:
        command.append(str(initial_dir))
    return command


def _parse_child_output(stdout: str) -> str | None:
    for line in stdout.splitlines():
        if not line.startswith(RESULT_MARKER):
            continue
        try:
            payload = json.loads(line[len(RESULT_MARKER):])
        except json.JSONDecodeError:
            break
        chosen = payload.get("path")
        # A cancel is a real, successful answer, not a failure: the dialog
        # asked and the person said no. It comes back as None so the
        # caller can tell it apart from a path, and leave the field alone.
        return chosen if isinstance(chosen, str) and chosen else None
    raise FolderPickerUnavailable(
        "The folder picker did not report a folder. Type the folder path instead."
    )


def choose_directory(
    initial_dir: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    runner=None,
) -> str | None:
    """Opens a folder dialog and returns the chosen path, or None if the
    person cancelled.

    Raises FolderPickerBusy, FolderPickerTimeout or FolderPickerUnavailable
    for the three ways this does not produce an answer. All three are
    reported and then ignored by the caller: none of them is ever a reason
    to change what is in the output root field.

    runner is injectable for the same reason procutil's own is, and for
    the same reason every other external call in this project takes one:
    the tests drive a fake rather than a real dialog, and a fake receives
    every argument exactly as subprocess.run would, so a test can assert
    the timeout is genuinely being passed rather than trust that it is.
    """
    if not _dialog_in_progress.acquire(blocking=False):
        raise FolderPickerBusy(
            "A folder picker is already open. Finish with that one first; it "
            "may be behind this window."
        )
    try:
        command = _child_command(initial_dir)
        extra = {} if runner is None else {"runner": runner}
        try:
            result = run_hidden(command, timeout=timeout_seconds, **extra)
        except subprocess.TimeoutExpired:
            # subprocess.run kills the child before re-raising, so the
            # dialog is gone rather than left on screen with nothing
            # listening to it.
            raise FolderPickerTimeout(
                f"The folder picker was open for {timeout_seconds:.0f} seconds "
                f"with nothing chosen, so it was closed. Nothing has changed. "
                f"Try again, or type the folder path."
            ) from None
        except OSError as exc:
            # The helper process could not be started at all.
            raise FolderPickerUnavailable(
                f"The folder picker could not start ({exc}). Type the folder "
                f"path instead."
            ) from None

        if result.returncode != 0:
            # Most likely no tkinter in this interpreter, or no desktop
            # session. The child's own last line of stderr says which, and
            # is worth passing on: nothing secret ever goes near this
            # child, so there is nothing here to redact.
            detail = (result.stderr or "").strip().splitlines()
            reason = detail[-1] if detail else "no reason given"
            raise FolderPickerUnavailable(
                f"The folder picker is not available on this machine "
                f"({reason}). Type the folder path instead."
            )
        return _parse_child_output(result.stdout or "")
    finally:
        _dialog_in_progress.release()


def _ask_directory(initial_dir: str) -> object:
    """The Tk half of the child, and the only part of this module that a
    test cannot reach: it opens a window and waits for a person.

    Verified by hand instead, once, which is the honest thing to say about
    six lines that cannot be automated on a build machine: a Tk top level
    created in a child spawned exactly as run_hidden spawns one came back
    with winfo_ismapped() and winfo_viewable() both true at its requested
    size, which is what confirms CREATE_NO_WINDOW suppresses the console
    window and not this. Everything either side of this function, the
    command that starts the child and the protocol that carries its answer
    home, is driven end to end by the tests.
    """
    import tkinter
    import tkinter.filedialog

    root = tkinter.Tk()
    try:
        # Withdrawn so the empty Tk main window never appears; the dialog
        # itself is a separate native window and is unaffected.
        root.withdraw()
        # A native dialog parented to a withdrawn root can open behind
        # whatever has focus, which for this tool is always a browser
        # window filling the screen. Making the (invisible) parent topmost
        # is what pulls the dialog in front of it. The page says to look
        # behind the browser anyway, because nothing here can promise a
        # window manager will honour this.
        root.attributes("-topmost", True)
        root.update()
        return tkinter.filedialog.askdirectory(
            title="mapgen: where survey folders are created",
            initialdir=initial_dir or None,
            mustexist=True,
        )
    finally:
        root.destroy()


def _child_main(argv: list[str], ask=_ask_directory) -> int:
    """What runs in the child. Prints one marked JSON line and exits.

    Kept deliberately small: everything it can do wrong, it does in a
    process whose death costs nothing.

    ask is injectable so the tests can run this exact function, and the
    exact protocol line it writes, without a native dialog appearing on
    somebody's screen. That matters more here than injectability usually
    does: the parent's parser and this writer are two halves of one
    agreement, and a test that fakes the whole child cannot notice the two
    halves disagreeing.
    """
    chosen = ask(argv[0] if argv else "")

    # askdirectory returns "" on some platforms and an empty tuple on
    # others when the dialog is cancelled. Both are "no", and neither is
    # a path.
    path = chosen if isinstance(chosen, str) and chosen else None
    # ensure_ascii, the default, so the line crossing the pipe is pure
    # ASCII whatever is in the path: a Welsh folder name comes back
    # through the JSON escape rather than depending on the pipe's
    # encoding. procutil already puts the child in UTF-8 mode and decodes
    # as UTF-8, which is the belt to this brace; both are here because
    # this is the one path in the tool where a name the owner typed in
    # Welsh has to survive a process boundary.
    sys.stdout.write(RESULT_MARKER + json.dumps({"path": path}) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a child process
    raise SystemExit(_child_main(sys.argv[1:]))
