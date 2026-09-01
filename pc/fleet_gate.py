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

  - `ok: 1` is not `ok: True`. Nothing this repository writes produces it, so
    a truthy stand-in means something else wrote the file, and a gate that
    guesses for that is not a gate.

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
    except OSError as e:
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
    except OSError as e:
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
    if isinstance(finished, bool) or not isinstance(finished, (int, float)):
        return (f"The fleet run at {path} never finished"
                f" (finished_at = {finished!r}). A run that died mid-way"
                f" decided nothing, whatever else the file says. Run the"
                f" fleet again.")
    age = float(now()) - float(finished)
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
    """Exit 0 to release, 1 with the reason on stdout to refuse.

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
              f" refuses. Nothing has been released.")
        return 1
    if reason is not None:
        print(reason)
        return 1
    print(f"Fleet gate: {head_sha.strip()[:12]} passed on every desk in"
          f" {inventory}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
