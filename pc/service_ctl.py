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
    are both answers, returned as a line the caller can log.

The per-platform commands live in cli._Backend, not here. That hierarchy
exists because five separate sys.platform ladders drifted apart from each
other (see its docstring); a sixth one in this file would be the same defect
with a new name.
"""
import subprocess

from pc import cli


def stop_service(runner=None) -> str:
    """Stop this machine's bridge service. Returns what happened.

    runner stands in for subprocess.run and is resolved at call time, so a
    test that stubs subprocess.run stays in charge of what can be executed.
    """
    return _safely("stop", runner)


def start_service(runner=None) -> str:
    """Start it again, after stop_service(). Returns what happened."""
    return _safely("start", runner)


def _safely(verb: str, runner) -> str:
    # BLINK_SKIP_SERVICE is honoured for the same reason restart_service()
    # honours it: every test in this repository sets it, and the login
    # service is the one piece of state that is NOT scoped to $HOME -- so
    # without this a test under a temporary HOME could still stop the agent
    # of whoever is logged in. Set in a real fleet run's environment, it
    # turns that run into a no-op, which the returned line says plainly.
    if cli._skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
    try:
        return getattr(cli.backend(), verb)(runner or subprocess.run)
    except Exception as e:                       # never out of a finally block
        return f"could not {verb} the service: {e}"
