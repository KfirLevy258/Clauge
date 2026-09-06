"""Decides whether `.fleet/last_run.json` licenses tagging a release.

tools/release.sh builds the firmware, signs it with the key that lives only on
this machine, and publishes it to every board in the field. Everything that
could have caught a bad build has already run by then, so this is the last
place a release can be stopped -- and the only evidence it has is the file
tools/fleet/run.py leaves behind after driving a real board on all three
desks.

Reading that file is not the same as trusting it. Three of its shapes are
green and honest and still not a licence to ship:

  - `run.py --only kfir-ubuntu` exits 0 and writes ok: true, because that run
    did pass. So does `--scenarios overage`. Both are correct behaviour for
    the orchestrator and pinned by its own tests; both are a fraction of the
    evidence a release needs. `only` and `scenarios` are recorded for exactly
    this reader, and a value in either one is a refusal here.

  - A run covering two desks and listing two desks in `hosts` is internally
    consistent, so the file cannot be allowed to describe its own
    completeness. The desks are read from tools/fleet/fleet.toml instead, and
    every one of them has to be present with ok exactly True.

  - `ok: 1` is not `ok: True`, and a `finished_at` of `NaN` is not a time.
    Nothing this repository writes produces either, so a stand-in that merely
    passes a type check means something else wrote the file, and a gate that
    guesses on its behalf is not a gate. The NaN is the sharper of the two:
    it is a float, it survives json.loads, and it compares False against
    every bound, so it does not fail the freshness check -- it deletes it.

Failing closed is the whole design. A missing file, an unreadable one, a
damaged one, a missing inventory, or an interpreter with no tomllib are all
refusals -- never a pass and, from main(), never a bare traceback either.
That last case is real rather than theoretical: release.sh runs plain
`python3`, and on this Mac /usr/bin/python3 is 3.9.6 with no tomllib while the
homebrew python3 on PATH has it.

Stdlib only, and nothing from the rest of pc/. This module runs under whatever
interpreter a release shell happens to have, which is not the one the daemon's
dependencies were installed for.
"""
import json
import math
import sys
import time
from pathlib import Path

try:                                  # 3.11+; release.sh runs plain `python3`
    import tomllib
except ModuleNotFoundError:           # pragma: no cover - exercised by a stub
    tomllib = None

# Twelve hours: long enough to prove a commit in the morning and release it
# after lunch, short enough that a run from before yesterday's rebase cannot
# vouch for today's build. The commit check already pins the code; this pins
# the desks, which drift on their own -- an unplugged board, a Windows update,
# a daemon left stopped by the last run.
MAX_AGE_S = 43200

USAGE = ("usage: python3 -m pc.fleet_gate"
         " <.fleet/last_run.json> <head sha> <tools/fleet/fleet.toml>")


def _names(value):
    """A comma-separated rendering of whatever run.py recorded."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value) or "(none)"
    return str(value)


def _failing_desks(doc):
    """The desks in a result document that did not report a clean pass."""
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict):
        return []
    return sorted(name for name, result in hosts.items()
                  if not (isinstance(result, dict)
                          and result.get("ok") is True))


def _inventory_names(inventory_path):
    """The desks a full fleet run must cover, from fleet.toml.

    Deliberately a second, much simpler reader than run.py's load_inventory:
    importing that would drag the orchestrator (and argparse, subprocess, and
    its notion of the repo root) into an interpreter chosen by a release
    shell. All this needs is the host names, and anything that stops it from
    getting them is raised as the refusal an operator reads.
    """
    path = Path(inventory_path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        # Including the decode error, which is a ValueError and would
        # otherwise be returned to the operator as the codec's own words --
        # naming no file, mentioning no fleet, starting in lower case.
        raise ValueError(
            f"The fleet inventory at {path} could not be read: {e}. The gate"
            f" checks the run against the desks that file names, so without"
            f" it there is nothing to check against.")
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(
            f"The fleet inventory at {path} is not valid TOML: {e}. Until it"
            f" parses, the gate cannot tell which desks a full run covers.")
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        raise ValueError(
            f"The fleet inventory at {path} names no desks, so 'every desk"
            f" passed' would be true of a run that touched nothing. Restore"
            f" the [hosts.<name>] tables before releasing.")
    return sorted(hosts)


def gate(path, head_sha, inventory_path, now=time.time, max_age_s=MAX_AGE_S):
    """None if this commit may be released; otherwise the reason it may not.

    The reason is returned rather than raised because it is prose for an
    operator whose release just stopped: it says which of the six conditions
    failed and what to do about it. Every path out of here is either None or
    such a sentence -- there is no third answer meaning "probably fine".
    """
    if tomllib is None:
        return (f"The fleet gate cannot check anything: this Python"
                f" ({sys.version.split()[0]}) has no tomllib, so the desks in"
                f" {inventory_path} cannot be read. Run the release under"
                f" Python 3.11 or newer. A gate that cannot verify must not"
                f" pass a release.")

    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (f"There is no fleet run recorded at {path}. Prove this commit"
                f" on the fleet first: python3 tools/fleet/run.py")
    except (OSError, UnicodeDecodeError) as e:
        # UnicodeDecodeError is a ValueError, not an OSError, so it needs
        # naming: a mis-encoded or partly overwritten file is ordinary
        # corruption, and the contract of this function is that every way out
        # of it is a sentence somebody can act on.
        return (f"The fleet run at {path} could not be read: {e}. The gate"
                f" refuses on anything it cannot read, so fix the file or"
                f" run the fleet again.")

    try:
        doc = json.loads(raw)
    except ValueError as e:
        return (f"The fleet run at {path} is not the JSON"
                f" tools/fleet/run.py writes ({e}). That file is replaced"
                f" atomically, so a damaged one is not a half-finished"
                f" write -- something else produced it. Run the fleet again.")
    if not isinstance(doc, dict):
        return (f"The fleet run at {path} is a {type(doc).__name__}, not the"
                f" record tools/fleet/run.py writes. Run the fleet again.")

    # 1. The run's own verdict.
    ok = doc.get("ok")
    if ok is not True:
        if ok is False:
            lead = "The last fleet run failed."
        else:
            lead = (f"The last fleet run cannot be read as a pass: its \"ok\""
                    f" is {ok!r} rather than true, so it counts as failed.")
        bad = _failing_desks(doc)
        who = f" Desks that did not pass: {', '.join(bad)}." if bad else ""
        return (f"{lead}{who} Fix what it reported and run"
                f" tools/fleet/run.py again.")
    # A pass that also lists problems is not a pass this file could have been
    # written by: run.py computes ok as `summary and not problems and sha is
    # known` (run.py:794-795), so the two cannot both be there. Same ground as
    # refusing ok: 1 -- only run.py writes this file, and a document that
    # contradicts run.py's own arithmetic is not evidence of anything.
    problems = doc.get("problems")
    if problems:
        return (f"The fleet run at {path} claims to have passed while also"
                f" reporting {_names(problems)}. tools/fleet/run.py cannot"
                f" produce both, so this file did not come from a run that"
                f" finished cleanly. Run the fleet again.")

    # 2. The run has to be about the code being released.
    head = head_sha.strip() if isinstance(head_sha, str) else ""
    if not head:
        return ("The commit being released could not be determined, so no"
                " fleet run can be matched to it. Check that git rev-parse"
                " HEAD works in this checkout.")
    recorded = doc.get("sha")
    if not isinstance(recorded, str) or not recorded.strip():
        return (f"The fleet run at {path} names no commit"
                f" (sha = {recorded!r}), so nothing ties it to the code being"
                f" released. Run the fleet again from this checkout.")
    recorded = recorded.strip()
    if recorded.lower() != head.lower():
        return (f"The fleet run is for commit {recorded}, HEAD is {head}."
                f" The desks proved other code than the code being tagged."
                f" Run the fleet on this commit.")

    # 3. And recent enough that the desks have not drifted underneath it.
    finished = doc.get("finished_at")
    # math.isfinite, not just isinstance. json.loads accepts the non-standard
    # NaN, Infinity and -Infinity tokens by default, and a NaN is a perfectly
    # ordinary float that clears every type check -- and then compares False
    # against everything, so `age < 0` and `age > max_age_s` are BOTH false
    # and the freshness check silently evaporates. Three characters in the
    # file would otherwise let a months-old green run vouch for today's
    # desks. (The same shape of bug already cost this project a run of
    # polling, via BLINK_POLL_INTERVAL_S=nan walking past a `<= 0` guard.)
    if (isinstance(finished, bool)
            or not isinstance(finished, (int, float))
            or not math.isfinite(finished)):
        return (f"The fleet run at {path} never finished"
                f" (finished_at = {finished!r}). A run that died mid-way, or"
                f" a timestamp that is not a real moment, decided nothing --"
                f" whatever else the file says. Run the fleet again.")
    age = float(now()) - float(finished)
    if not math.isfinite(age):
        return (f"The age of the fleet run at {path} does not come out as a"
                f" number, so this machine's clock cannot be trusted to judge"
                f" it. Nothing is released on a time nobody can read.")
    if age < 0:
        return (f"The fleet run at {path} claims to have finished"
                f" {abs(age) / 3600:.1f} h in the future. Either its"
                f" timestamp or this machine's clock is wrong, and the gate"
                f" cannot judge the run's age until that is settled.")
    if age > max_age_s:
        return (f"The fleet run is too old ({age / 3600:.1f} h; the limit is"
                f" {max_age_s / 3600:.1f} h). Boards get unplugged and desks"
                f" get rebooted. Run the fleet again before tagging.")

    # 4/5. A narrowed run is green about the part of the fleet it ran.
    only = doc.get("only")
    if only is not None:
        return (f"That run covered only {_names(only)}: it was started with"
                f" --only, so it says nothing about the rest of the fleet."
                f" Run tools/fleet/run.py with no --only.")
    scenarios = doc.get("scenarios")
    if scenarios is not None:
        return (f"That run covered only the {_names(scenarios)} scenario(s):"
                f" it was started with --scenarios, so the rest were never"
                f" tried. Run tools/fleet/run.py with no --scenarios.")

    # 6. Every desk in the inventory, each one green. Asked of fleet.toml
    #    rather than of the result file, which cannot vouch for its own
    #    completeness.
    hosts = doc.get("hosts")
    if not isinstance(hosts, dict) or not hosts:
        return (f"The fleet run at {path} lists no desks"
                f" (hosts = {type(hosts).__name__}), so its pass is about"
                f" nothing. Run the fleet again.")
    try:
        wanted = _inventory_names(inventory_path)
    except ValueError as e:
        return str(e)
    missing = [name for name in wanted if name not in hosts]
    if missing:
        return (f"The fleet run says nothing about {', '.join(missing)},"
                f" which {inventory_path} names as part of the fleet. A"
                f" release is proved on every desk or on none. Run the fleet"
                f" again.")
    bad = [name for name in wanted
           if not (isinstance(hosts[name], dict)
                   and hosts[name].get("ok") is True)]
    if bad:
        return (f"These desks did not report a clean pass:"
                f" {', '.join(bad)}. The run's own summary says otherwise,"
                f" which makes the file untrustworthy as well as red. Run"
                f" the fleet again.")
    return None


def main(argv=None):
    """Exit 0 to release, 1 with the reason on stderr to refuse.

    Refusals go to stderr because release.sh prints its own FATAL block --
    "the fleet gate refused $TAG for the reason above" -- to stderr straight
    after. On one stream they read as one message; split across two, the
    FATAL line points at a reason that is not above it.

    The catch-all is the point of the wrapper: release.sh reads an exit
    status, and a traceback would already fail closed there, but the operator
    would be left reading a stack instead of a sentence. Anything unexpected
    is reported as what it is -- a check that could not be completed, and
    therefore a refusal.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 3:
        print(USAGE, file=sys.stderr)
        return 2
    state, head_sha, inventory = argv
    try:
        reason = gate(state, head_sha, inventory)
    except Exception as e:  # noqa: BLE001 - a refusal, not a crash
        print(f"The fleet gate could not complete its check ({e!r}), so it"
              f" refuses. Nothing has been released.", file=sys.stderr)
        return 1
    if reason is not None:
        print(reason, file=sys.stderr)
        return 1
    print(f"Fleet gate: {head_sha.strip()[:12]} passed on every desk in"
          f" {inventory}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
