"""The last thing standing between a bad fleet run and a published release.

`tools/release.sh` asks pc/fleet_gate.py one question -- may this commit be
tagged -- and every other guard in this repository is upstream of the answer.
So the test that matters is not "does the green case pass" but "is there any
file, of any shape, that makes it say yes when it should say no", and that is
what most of this module is: one test per way the gate could be fooled.

Three of those ways are specific to this fleet and worth naming.

  - A subset run is green and CORRECT. `tools/fleet/run.py --only kfir-ubuntu`
    exits 0 and writes ok: true, because that run did pass; the orchestrator's
    own tests pin that. A gate that read only `ok` would therefore ship a
    three-desk release on one desk's evidence. The state file records `only`
    and `scenarios` precisely so this gate can tell the two apart.

  - The state file is not allowed to describe its own completeness. A run that
    covered two desks and listed two desks in `hosts` is internally
    consistent. The gate reads tools/fleet/fleet.toml itself and insists every
    desk named there reported a pass, so a shrunken run cannot vouch for
    itself.

  - The gate cannot verify without tomllib, and release.sh calls plain
    `python3`. On this Mac /usr/bin/python3 is 3.9.6 and has none, while the
    homebrew python3 on PATH does. An interpreter that cannot read the
    inventory has nothing to say about the release, and that silence must
    refuse rather than pass.

Nothing here touches the real .fleet/last_run.json: tmp_path throughout.
"""
import json
import pathlib
import subprocess
import sys
import time

import pytest

from pc import fleet_gate
from pc.fleet_gate import gate

ROOT = pathlib.Path(__file__).resolve().parents[2]
DESKS = ("local-mac", "kfir-ubuntu", "galit-win10")
SHA = "0f3b1c9e4d5a6b7c8d9e0f1a2b3c4d5e6f708192"


def _hosts(*names, ok=True):
    return {name: {"host": name, "ok": ok, "problems": []} for name in names}


def _state(tmp_path, **kw):
    """A file the gate should accept, with the caller's spoiling applied."""
    doc = {"sha": SHA, "started_at": 900.0, "finished_at": 1000.0,
           "ok": True, "hosts": _hosts(*DESKS), "problems": [],
           "only": None, "scenarios": None}
    doc.update(kw)
    p = tmp_path / "last_run.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def _inventory(tmp_path, *names):
    names = names or DESKS
    body = "".join(f'[hosts.{name}]\nos = "linux"\nssh = ""\n'
                   for name in names)
    p = tmp_path / "fleet.toml"
    p.write_text(body, encoding="utf-8")
    return p


def _refuse(tmp_path, **kw):
    """Every refusal is read by somebody whose release just stopped."""
    reason = gate(_state(tmp_path, **kw), SHA, _inventory(tmp_path),
                  now=lambda: 2000.0)
    assert reason, "the gate passed a file it should have refused"
    assert reason[0].isupper(), f"not a sentence: {reason!r}"
    return reason


# --- the one case that passes ---------------------------------------------

def test_full_green_fresh_matching_run_passes(tmp_path):
    assert gate(_state(tmp_path), SHA, _inventory(tmp_path),
                now=lambda: 2000.0) is None


def test_a_host_the_inventory_does_not_name_is_not_an_objection(tmp_path):
    # The fleet grew a fourth desk after the run, or somebody kept an old
    # name in the result. Neither weakens the evidence for the three desks
    # that matter, and the gate only asks about those.
    state = _state(tmp_path, hosts=_hosts(*DESKS, "spare-pi"))
    assert gate(state, SHA, _inventory(tmp_path), now=lambda: 2000.0) is None


# --- nothing to read ------------------------------------------------------

def test_missing_file_refuses(tmp_path):
    reason = gate(tmp_path / "nope.json", SHA, _inventory(tmp_path))
    assert "no fleet run recorded" in reason


def test_directory_in_place_of_the_file_refuses(tmp_path):
    (tmp_path / "last_run.json").mkdir()
    assert gate(tmp_path / "last_run.json", SHA, _inventory(tmp_path))


def test_truncated_file_refuses(tmp_path):
    p = _state(tmp_path)
    p.write_text(p.read_text(encoding="utf-8")[:40], encoding="utf-8")
    assert gate(p, SHA, _inventory(tmp_path), now=lambda: 2000.0)


def test_empty_file_refuses(tmp_path):
    p = _state(tmp_path)
    p.write_text("", encoding="utf-8")
    assert gate(p, SHA, _inventory(tmp_path), now=lambda: 2000.0)


def test_state_file_that_is_not_utf8_refuses_in_prose(tmp_path):
    # A mis-encoded or partly overwritten file is ordinary corruption, not
    # forgery. UnicodeDecodeError is a ValueError, not an OSError, so it used
    # to leave gate() by the exception path instead of the refusal path --
    # safe, because main() catches everything, but the operator deserves a
    # sentence about the fleet rather than a stack.
    p = tmp_path / "last_run.json"
    p.write_bytes(b"\xff\xfe{\x00o\x00k\x00")
    reason = gate(p, SHA, _inventory(tmp_path), now=lambda: 2000.0)
    assert reason and reason[0].isupper() and str(p) in reason


@pytest.mark.parametrize("blob", ["[]", "null", '"ok"', "true"])
def test_json_that_is_not_an_object_refuses(tmp_path, blob):
    p = _state(tmp_path)
    p.write_text(blob, encoding="utf-8")
    assert gate(p, SHA, _inventory(tmp_path), now=lambda: 2000.0)


# --- the verdict itself ---------------------------------------------------

def test_failed_run_refuses(tmp_path):
    assert "failed" in _refuse(tmp_path, ok=False)


@pytest.mark.parametrize("value", [1, "true", "yes", [1], {"ok": True}])
def test_ok_that_is_merely_truthy_refuses(tmp_path, value):
    # `is True`, not truthiness: a 1 here means something wrote this file
    # that was not tools/fleet/run.py, and the gate does not guess for it.
    assert "failed" in _refuse(tmp_path, ok=value)


@pytest.mark.parametrize("value", [["The board was dead"], "git failed", 2])
def test_a_pass_that_also_carries_problems_refuses(tmp_path, value):
    # run.py computes ok as `summary and not problems and sha != unknown`
    # (run.py:794-795), so a genuine file cannot say both. One that does was
    # written by something else, which is the same ground the gate refuses
    # ok: 1 on.
    assert _refuse(tmp_path, problems=value)


def test_missing_ok_refuses(tmp_path):
    doc = json.loads(_state(tmp_path).read_text(encoding="utf-8"))
    doc.pop("ok")
    p = tmp_path / "last_run.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert "failed" in gate(p, SHA, _inventory(tmp_path), now=lambda: 2000.0)


# --- the commit -----------------------------------------------------------

def test_wrong_commit_refuses(tmp_path):
    assert "commit" in _refuse(tmp_path, sha="a" * 40)


def test_an_abbreviation_of_the_right_commit_refuses(tmp_path):
    assert "commit" in _refuse(tmp_path, sha=SHA[:12])


@pytest.mark.parametrize("value", [None, "", "unknown", 12, ["abc"]])
def test_unusable_recorded_commit_refuses(tmp_path, value):
    assert "commit" in _refuse(tmp_path, sha=value)


@pytest.mark.parametrize("head", [None, "", "   "])
def test_unknown_head_refuses(tmp_path, head):
    # `git rev-parse` failing inside release.sh leaves the argument empty
    # under `set -u`; an empty HEAD matches nothing, and must not match a
    # file whose sha is empty either.
    reason = gate(_state(tmp_path, sha=head), head, _inventory(tmp_path),
                  now=lambda: 2000.0)
    assert reason and "commit" in reason


def test_capitals_in_the_head_sha_are_not_a_different_commit(tmp_path):
    assert gate(_state(tmp_path), SHA.upper(), _inventory(tmp_path),
                now=lambda: 2000.0) is None


# --- freshness ------------------------------------------------------------

def test_run_older_than_the_window_refuses(tmp_path):
    reason = gate(_state(tmp_path), SHA, _inventory(tmp_path),
                  now=lambda: 1000.0 + 43201)
    assert "old" in reason


def test_default_window_is_twelve_hours(tmp_path):
    state, inv = _state(tmp_path), _inventory(tmp_path)
    assert gate(state, SHA, inv, now=lambda: 1000.0 + 43199) is None
    assert gate(state, SHA, inv, now=lambda: 1000.0 + 43201)


def test_max_age_is_honoured(tmp_path):
    reason = gate(_state(tmp_path), SHA, _inventory(tmp_path),
                  now=lambda: 1100.0, max_age_s=60)
    assert "old" in reason


def test_run_that_never_finished_refuses(tmp_path):
    # finished_at stays None until the run ends. A None is a run that died,
    # not an age of zero.
    assert _refuse(tmp_path, finished_at=None)


@pytest.mark.parametrize("value", ["1000.0", True, [1000.0], {}])
def test_finished_at_that_is_not_a_time_refuses(tmp_path, value):
    assert _refuse(tmp_path, finished_at=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_finished_at_that_is_not_a_finite_time_refuses(tmp_path, value):
    # json.dumps writes these as the bare tokens NaN, Infinity, -Infinity and
    # json.loads reads them straight back, so they arrive here as ordinary
    # floats and clear an isinstance check. NaN then compares False against
    # everything: `age < 0` and `age > max_age_s` are BOTH false, and the one
    # check that stops a months-old green run from vouching for today's desks
    # is nullified by three characters.
    #
    # The same shape of bug reached this project once already, in the
    # daemon's poll interval, where BLINK_POLL_INTERVAL_S=nan walked past a
    # `<= 0` guard and stopped polling for a whole run.
    assert _refuse(tmp_path, finished_at=value)


def test_a_clock_that_gives_no_usable_time_refuses(tmp_path):
    # The other half of the NaN case: the timestamp is fine and the clock is
    # not. The subtraction is still NaN and still compares False against both
    # bounds, so it is guarded on the result as well as on the input.
    reason = gate(_state(tmp_path), SHA, _inventory(tmp_path),
                  now=lambda: float("nan"))
    assert reason and reason[0].isupper()


def test_finish_in_the_future_refuses(tmp_path):
    # Not an age of "negative, therefore fresh". A timestamp ahead of this
    # machine's clock means one of the two is wrong, and neither answer
    # licenses a release.
    assert _refuse(tmp_path, finished_at=9000.0)


# --- a subset is not a fleet run ------------------------------------------

def test_single_desk_run_refuses(tmp_path):
    # `--only kfir-ubuntu` exits 0 with ok: true, and it is right to.
    reason = gate(_state(tmp_path, only=["kfir-ubuntu"],
                         hosts=_hosts("kfir-ubuntu")),
                  SHA, _inventory(tmp_path), now=lambda: 2000.0)
    assert reason


def test_only_naming_every_desk_still_refuses(tmp_path):
    # The hosts all reported, so check 6 alone would let this through. It is
    # still a run somebody narrowed by hand, and the gate says so.
    assert _refuse(tmp_path, only=list(DESKS))


def test_scenario_subset_refuses(tmp_path):
    assert _refuse(tmp_path, scenarios="overage")


def test_empty_scenario_selection_still_refuses(tmp_path):
    assert _refuse(tmp_path, scenarios=[])


# --- every desk, and each one green ---------------------------------------

def test_a_desk_that_did_not_report_refuses(tmp_path):
    reason = _refuse(tmp_path, hosts=_hosts("local-mac", "kfir-ubuntu"))
    assert "galit-win10" in reason


def test_a_desk_that_failed_refuses(tmp_path):
    hosts = _hosts(*DESKS)
    hosts["galit-win10"]["ok"] = False
    reason = _refuse(tmp_path, hosts=hosts)
    assert "galit-win10" in reason


@pytest.mark.parametrize("value", [1, "true", None, "ok"])
def test_a_desks_ok_that_is_not_exactly_true_refuses(tmp_path, value):
    hosts = _hosts(*DESKS)
    hosts["local-mac"]["ok"] = value
    assert "local-mac" in _refuse(tmp_path, hosts=hosts)


def test_a_desk_entry_that_is_not_a_table_refuses(tmp_path):
    hosts = _hosts(*DESKS)
    hosts["local-mac"] = "passed"
    assert "local-mac" in _refuse(tmp_path, hosts=hosts)


@pytest.mark.parametrize("value", [None, [], "all good", 3])
def test_hosts_that_is_not_a_table_refuses(tmp_path, value):
    assert _refuse(tmp_path, hosts=value)


def test_no_hosts_at_all_refuses(tmp_path):
    assert _refuse(tmp_path, hosts={})


# --- the inventory the gate checks against --------------------------------

def test_missing_inventory_refuses(tmp_path):
    assert gate(_state(tmp_path), SHA, tmp_path / "gone.toml",
                now=lambda: 2000.0)


def test_malformed_inventory_refuses(tmp_path):
    inv = _inventory(tmp_path)
    inv.write_text("[hosts.local-mac\nos = ", encoding="utf-8")
    assert gate(_state(tmp_path), SHA, inv, now=lambda: 2000.0)


def test_inventory_that_is_not_utf8_refuses_in_prose(tmp_path):
    # It refused before this test existed, but with the codec's own words --
    # "'utf-8' codec can't decode byte 0xff in position 0" -- which names no
    # file, mentions no fleet, and does not start with a capital.
    inv = tmp_path / "fleet.toml"
    inv.write_bytes(b"\xff\xfe[\x00h\x00")
    reason = gate(_state(tmp_path), SHA, inv, now=lambda: 2000.0)
    assert reason and reason[0].isupper() and str(inv) in reason


def test_inventory_naming_no_desks_refuses(tmp_path):
    # An emptied inventory would otherwise make check 6 vacuously true, and
    # a fleet of nobody would ship every release.
    inv = _inventory(tmp_path)
    inv.write_text("# every desk commented out\n", encoding="utf-8")
    assert gate(_state(tmp_path), SHA, inv, now=lambda: 2000.0)


def test_inventory_whose_hosts_key_is_not_a_table_refuses(tmp_path):
    inv = _inventory(tmp_path)
    inv.write_text('hosts = "local-mac"\n', encoding="utf-8")
    assert gate(_state(tmp_path), SHA, inv, now=lambda: 2000.0)


def test_a_fourth_desk_in_the_inventory_refuses_an_old_three_desk_run(tmp_path):
    inv = _inventory(tmp_path, *DESKS, "new-desk")
    reason = gate(_state(tmp_path), SHA, inv, now=lambda: 2000.0)
    assert reason and "new-desk" in reason


def test_the_real_inventory_names_the_three_desks(tmp_path):
    # Pins the gate against the file release.sh actually hands it, so a
    # rename in fleet.toml cannot quietly drop a desk from the check.
    state = _state(tmp_path)
    real = ROOT / "tools" / "fleet" / "fleet.toml"
    assert gate(state, SHA, real, now=lambda: 2000.0) is None


# --- a gate that cannot verify --------------------------------------------

def test_no_tomllib_refuses(tmp_path, monkeypatch):
    # release.sh calls plain `python3`. On this Mac /usr/bin/python3 is 3.9.6
    # and has no tomllib, so this is the difference between a refusal and a
    # traceback in front of somebody publishing a release.
    monkeypatch.setattr(fleet_gate, "tomllib", None)
    reason = gate(_state(tmp_path), SHA, _inventory(tmp_path),
                  now=lambda: 2000.0)
    assert reason and "tomllib" in reason


def test_the_gate_imports_nothing_else_from_pc():
    # It runs under whatever python3 a release shell has, which is not the
    # one the daemon's dependencies were installed for.
    src = (ROOT / "pc" / "fleet_gate.py").read_text(encoding="utf-8")
    assert "from pc" not in src and "import pc" not in src


# --- the command release.sh runs ------------------------------------------

def test_main_prints_the_reason_on_stderr_and_exits_one(tmp_path, capsys):
    # stderr, not stdout: release.sh writes its own "refused $TAG for the
    # reason above" to stderr, and under a redirection that separates the two
    # streams that FATAL line would otherwise point at a reason which is not
    # above it.
    code = fleet_gate.main([str(_state(tmp_path, ok=False)), SHA,
                            str(_inventory(tmp_path))])
    assert code == 1
    out = capsys.readouterr()
    assert "failed" in out.err
    assert out.out == ""


def test_main_exits_zero_on_a_good_run(tmp_path, capsys):
    p = _state(tmp_path, finished_at=None)
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["finished_at"] = time.time()
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert fleet_gate.main([str(p), SHA, str(_inventory(tmp_path))]) == 0


def test_main_refuses_the_wrong_number_of_arguments(tmp_path):
    assert fleet_gate.main([str(_state(tmp_path)), SHA]) != 0


def test_run_as_a_module_refuses_with_status_one(tmp_path):
    # The whole path release.sh takes: `python3 -m pc.fleet_gate`, non-zero
    # status, reason on stderr beside the FATAL block that follows it.
    done = subprocess.run(
        [sys.executable, "-m", "pc.fleet_gate",
         str(_state(tmp_path, sha="b" * 40)), SHA, str(_inventory(tmp_path))],
        cwd=str(ROOT), capture_output=True, text=True)
    assert done.returncode == 1
    assert "commit" in done.stderr
