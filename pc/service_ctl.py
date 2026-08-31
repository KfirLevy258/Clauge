"""Take the serial port off the installed service, and give it back.

The fleet suite runs the real daemon against a real board on the user's own
machines. Only one process can hold a serial port, so a run has to stop the
login service first -- and put it back afterwards, every time, including the
times the run fails halfway through.

Two rules follow from where this is called:

  - It only stops and starts a service that is already installed. Nothing
    here writes, moves or deletes a plist, a unit file or a Scheduled Task.
    A test run brackets somebody's working day; it does not get to rewrite
    their installation on the way past.

  - Nothing here raises. start_service() is called from a finally block, and
    an exception there would replace the failure the run was there to find
    with one about the cleanup. A missing service and a command that failed
    are both answers, returned as an Outcome the caller can log.

The per-platform commands live in cli._Backend, not here. That hierarchy
exists because five separate sys.platform ladders drifted apart from each
other (see its docstring); a sixth one in this file would be the same defect
with a new name.
"""
from typing import NamedTuple

from pc import cli


class Outcome(NamedTuple):
    """What happened, in a form the caller cannot mistake for something else.

    This was a bare string, and the string for a skipped stop reads much like
    the string for a successful one -- which matters because a skip is
    reachable in a real run: tests/ci/check_install.sh documents exporting
    BLINK_SKIP_SERVICE=1, and a fleet agent started from such a shell would
    stop nothing, be refused the port, and blame the board. start_service()
    would no-op in turn, so the desk is left perfectly healthy and there is no
    broken state to diagnose from afterwards.

    So the distinction travels with the answer instead of waiting for a caller
    to think of asking: check .skipped before a run, .ok after one. str() is
    the line to log, so anywhere the old string was printed still reads the
    same.
    """
    ok: bool
    skipped: bool
    detail: str

    def __str__(self) -> str:
        return self.detail


# The backends answer in prose, the way all their sibling methods do -- these
# are the phrases `blink status` and `blink install` print. Rather than teach
# four classes a new return type for two methods, the openings that mean "the
# port is free" (or "the service is back") are named here, and pinned by a
# test so a reworded backend cannot quietly start reporting failure.
_WORKED = ("stopped", "started", "not installed")


def stop_service(runner=None) -> Outcome:
    """Stop this machine's bridge service.

    runner stands in for subprocess.run and is resolved at call time from
    cli's namespace, so a test that stubs cli.subprocess.run stays in charge
    of what this can execute even when the caller passes nothing.
    """
    return _safely("stop", runner)


def start_service(runner=None) -> Outcome:
    """Start it again, after stop_service()."""
    return _safely("start", runner)


def _safely(verb: str, runner) -> Outcome:
    # BLINK_SKIP_SERVICE is honoured for the same reason restart_service()
    # honours it: every test in this repository sets it, and the login
    # service is the one piece of state that is NOT scoped to $HOME -- so
    # without this a test under a temporary HOME could still stop the agent
    # of whoever is logged in.
    if cli._skip_service():
        return Outcome(False, True, "skipped (BLINK_SKIP_SERVICE=1)")
    try:
        detail = getattr(cli.backend(), verb)(runner or cli.subprocess.run)
    except Exception as e:                       # never out of a finally block
        return Outcome(False, False, f"could not {verb} the service: {e}")
    return Outcome(detail.startswith(_WORKED), False, detail)
