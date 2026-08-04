"""The folder picker.

The trap this file is written against is the one this project keeps
naming: a stub more permissive than the real thing. A fake that always
hands back a path proves nothing at all about the three outcomes that
actually matter, which are cancelling, timing out, and there being no
picker on the machine, so all three are here.

The other half of that trap is subtler. The child writes a line and the
parent reads it, and a test that fakes the whole child tests the parent
against a fixture it wrote itself, never against what the child really
emits. So the runner used through most of this file does not fabricate
output: it calls the real _child_main, capturing what that function really
writes, and hands it back as the child's stdout. Only the process boundary
and the Tk dialog itself are stood in for.
"""

import io
import json
import subprocess
import sys
import threading
from contextlib import redirect_stdout

import pytest

from mapgen.folderpicker import (
    DEFAULT_TIMEOUT_SECONDS,
    RESULT_MARKER,
    FolderPickerBusy,
    FolderPickerTimeout,
    FolderPickerUnavailable,
    _child_main,
    _parse_child_output,
    choose_directory,
)

# A folder path with Welsh characters in it. Not decoration: the owner
# surveys Welsh sites, procutil forces UTF-8 on every child precisely
# because overturemaps died on this exact character class under cp1252,
# and a picked path is the one place a name the owner chose has to cross
# a process boundary intact.
WELSH_PATH = "C:\\Surveys\\Ynys Môn\\Rhoscolyn ŷ"


class ChildRunningRunner:
    """Stands in for subprocess.run by running the real child in process.

    The command is recorded exactly as handed over, so a test can assert
    what would have been spawned; the arguments after "-m
    mapgen.folderpicker" are then passed to the real _child_main, whose
    real output becomes this call's stdout. What is faked is the process
    boundary and the dialog, not the protocol.
    """

    def __init__(self, answer):
        self._answer = answer
        self.calls = []
        self.initial_dirs = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        child_argv = command[3:]
        buffer = io.StringIO()

        def ask(initial_dir):
            self.initial_dirs.append(initial_dir)
            return self._answer

        with redirect_stdout(buffer):
            code = _child_main(child_argv, ask=ask)
        return subprocess.CompletedProcess(command, code, stdout=buffer.getvalue(), stderr="")


class FailingRunner:
    """A child that started and died, the shape of no tkinter or no desktop."""

    def __init__(self, returncode=1, stderr="ModuleNotFoundError: No module named 'tkinter'"):
        self._returncode = returncode
        self._stderr = stderr

    def __call__(self, command, **kwargs):
        return subprocess.CompletedProcess(command, self._returncode, stdout="", stderr=self._stderr)


class TimingOutRunner:
    def __call__(self, command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 0))


class UnstartableRunner:
    def __call__(self, command, **kwargs):
        raise OSError(2, "The system cannot find the file specified")


class GarbageRunner:
    """A child that exited cleanly having said nothing this parser knows."""

    def __init__(self, stdout=""):
        self._stdout = stdout

    def __call__(self, command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=self._stdout, stderr="")


# --- a path actually chosen ---------------------------------------------


def test_a_chosen_folder_comes_back_as_a_path():
    runner = ChildRunningRunner("C:\\Surveys")
    assert choose_directory(runner=runner) == "C:\\Surveys"


def test_a_welsh_path_survives_the_trip_home_intact():
    # Through the real child's own writer and the real parent's own
    # parser, which is the whole point: this is where a codepage would
    # eat the circumflex if anything here were careless about it.
    runner = ChildRunningRunner(WELSH_PATH)
    assert choose_directory(runner=runner) == WELSH_PATH


def test_the_protocol_line_the_child_writes_is_pure_ascii():
    # So the pipe's encoding cannot be what decides whether a Welsh folder
    # name arrives. procutil's UTF-8 mode is the second layer under this,
    # not the only one.
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _child_main([], ask=lambda _initial: WELSH_PATH)
    buffer.getvalue().encode("ascii")  # raises if anything slipped through


def test_the_child_is_told_where_to_open():
    runner = ChildRunningRunner("C:\\Surveys")
    choose_directory(initial_dir="C:\\Users\\Param\\Surveys", runner=runner)
    assert runner.initial_dirs == ["C:\\Users\\Param\\Surveys"]


def test_no_initial_directory_is_passed_when_there_is_none():
    runner = ChildRunningRunner("C:\\Surveys")
    choose_directory(runner=runner)
    command, _kwargs = runner.calls[0]
    assert command[1:] == ["-m", "mapgen.folderpicker"]


def test_the_child_is_this_same_interpreter_run_as_a_module():
    runner = ChildRunningRunner("C:\\Surveys")
    choose_directory(initial_dir="C:\\Surveys", runner=runner)
    command, _kwargs = runner.calls[0]
    assert command[0] == sys.executable
    assert command[1:3] == ["-m", "mapgen.folderpicker"]
    assert command[3] == "C:\\Surveys"


# --- cancelled ----------------------------------------------------------


def test_cancelling_returns_no_path_rather_than_raising():
    # A cancel is a real answer, not a failure: the caller leaves the
    # field exactly as it was, which is what "must not clear it" means.
    runner = ChildRunningRunner("")
    assert choose_directory(runner=runner) is None


def test_cancelling_on_a_platform_that_returns_an_empty_tuple_is_also_no_path():
    # askdirectory returns "" on some platforms and () on others.
    runner = ChildRunningRunner(())
    assert choose_directory(runner=runner) is None


# --- timed out ----------------------------------------------------------


def test_a_dialog_left_open_times_out_with_a_message_that_says_nothing_changed():
    with pytest.raises(FolderPickerTimeout) as caught:
        choose_directory(runner=TimingOutRunner())
    assert "Nothing has changed" in str(caught.value)


def test_the_timeout_is_actually_handed_to_the_child():
    # Not merely stored: a timeout the runner never receives is a dialog
    # that can hold a handler thread open forever.
    runner = ChildRunningRunner("C:\\Surveys")
    choose_directory(runner=runner)
    _command, kwargs = runner.calls[0]
    assert kwargs["timeout"] == DEFAULT_TIMEOUT_SECONDS


def test_a_caller_can_shorten_the_timeout():
    runner = ChildRunningRunner("C:\\Surveys")
    choose_directory(timeout_seconds=5, runner=runner)
    _command, kwargs = runner.calls[0]
    assert kwargs["timeout"] == 5


def test_the_default_timeout_is_generous_enough_to_find_a_folder_in():
    # A judgement, pinned so that shortening it is a deliberate act. Under
    # about half a minute and an ordinary pick starts failing; over about
    # five and a dialog nobody saw holds a thread for the rest of the
    # afternoon.
    assert 30 <= DEFAULT_TIMEOUT_SECONDS <= 300


# --- no picker on this machine -------------------------------------------


def test_a_child_that_cannot_run_reports_the_picker_as_unavailable():
    with pytest.raises(FolderPickerUnavailable) as caught:
        choose_directory(runner=FailingRunner())
    message = str(caught.value)
    # The child's own last line of stderr, so "no tkinter" and "no
    # desktop" do not read identically. Nothing secret goes anywhere near
    # this child, so there is nothing here to redact.
    assert "tkinter" in message
    assert "Type the folder path instead" in message


def test_a_child_that_will_not_start_at_all_reports_the_picker_as_unavailable():
    with pytest.raises(FolderPickerUnavailable):
        choose_directory(runner=UnstartableRunner())


def test_a_child_that_says_nothing_useful_is_not_read_as_a_cancel():
    # The dangerous misreading: silence is not "the person said no", it is
    # "this did not work", and the two need different messages.
    with pytest.raises(FolderPickerUnavailable):
        choose_directory(runner=GarbageRunner("Traceback (most recent call last):\n"))


def test_a_marked_line_that_is_not_json_is_not_read_as_a_cancel():
    with pytest.raises(FolderPickerUnavailable):
        choose_directory(runner=GarbageRunner(RESULT_MARKER + "{not json\n"))


def test_output_before_the_marked_line_is_ignored():
    # A shell profile, a deprecation warning, anything a Python start-up
    # might print. The marker is what makes the answer findable rather
    # than assumed to be the only thing on stdout.
    noisy = "some unrelated warning\n" + RESULT_MARKER + json.dumps({"path": "C:\\Surveys"}) + "\n"
    assert choose_directory(runner=GarbageRunner(noisy)) == "C:\\Surveys"


# --- one at a time -------------------------------------------------------


def test_a_second_dialog_while_one_is_open_is_refused_rather_than_stacked():
    opened = threading.Event()
    release = threading.Event()
    outcome = {}

    class BlockingRunner:
        def __call__(self, command, **kwargs):
            opened.set()
            release.wait(timeout=5)
            return subprocess.CompletedProcess(
                command, 0, stdout=RESULT_MARKER + json.dumps({"path": "C:\\Surveys"}) + "\n", stderr=""
            )

    def first():
        outcome["first"] = choose_directory(runner=BlockingRunner())

    thread = threading.Thread(target=first)
    thread.start()
    try:
        assert opened.wait(timeout=5), "the first dialog never opened"
        with pytest.raises(FolderPickerBusy):
            choose_directory(runner=ChildRunningRunner("C:\\Elsewhere"))
    finally:
        release.set()
        thread.join(timeout=5)

    assert outcome["first"] == "C:\\Surveys"


@pytest.mark.parametrize(
    "runner",
    [TimingOutRunner(), UnstartableRunner(), FailingRunner(), GarbageRunner()],
)
def test_a_failed_pick_still_lets_the_next_one_open(runner):
    # A lock held by a dialog that already failed would turn one bad
    # attempt into a picker that never works again until the server is
    # restarted.
    with pytest.raises(Exception):
        choose_directory(runner=runner)
    assert choose_directory(runner=ChildRunningRunner("C:\\Surveys")) == "C:\\Surveys"


# --- the two halves of the protocol agree --------------------------------


@pytest.mark.parametrize("answer", ["C:\\Surveys", WELSH_PATH, "", ()])
def test_what_the_child_writes_is_what_the_parent_reads(answer):
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _child_main([], ask=lambda _initial: answer)
    expected = answer if isinstance(answer, str) and answer else None
    assert _parse_child_output(buffer.getvalue()) == expected
