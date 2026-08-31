"""The two fleet scenarios a customer's own machine runs: install, then update.

Nothing here unpacks a real archive, runs a real installer, or writes to a real
home directory. This repository has a recorded hazard where a test run
uninstalled the live install (tests/conftest.py), and these are the only fleet
scenarios that invoke the installer at all -- so every command they would run
is answered by an injected runner, and every path they would write to is a
temporary directory.

What the fakes are here to hold still is the pair of ways these two scenarios
could go green without proving anything:

  - a version check that reads the version off the bundle it started with
    rather than off the program the installer actually left behind, and
  - an update that reports success when the feed was refused as unsigned, or
    when it held nothing newer -- `blink update` exits 0 for the second of
    those and prints a sentence nobody reads in a passing run.

Both are modelled below by a fake that only changes what ~/.blink/bin/blink
reports when a command genuinely put something there.
"""
import json
import os
import pathlib
import re
import sys
import types

import pytest

from pc.service_ctl import Outcome
from pc.update import archive_name, platform_key
from tests.fleet import agent


def _bin_name():
    return "blink.exe" if sys.platform == "win32" else "blink"


def _programs(bundle_version="1.2.4", install_leaves=..., install_code=0,
              copies=True, update_to=None, update_code=0, update_out="",
              unpacks=True):
    """A stand-in for every program these scenarios run.

    Answers `--version`, `install` and `update` for two distinct programs --
    the unpacked bundle and the copy under the sandbox home -- and it keeps
    the second of those ON DISK rather than in a variable, deliberately. The
    installed copy is a file that exists only because something put it there,
    and a fake that remembered it in a dict could not tell an install that
    worked apart from a directory left over by the run before. That
    difference is the whole of fresh_install.

    `copies=False` is the installer that exits 0 having copied nothing --
    what `blink install` does when handed an unfrozen build (pc/cli.py:1123),
    which is exactly the packaging fault this scenario is there to catch.

    It also stands in for `tar`, writing the program a real unpack would
    leave, so the agent's own "did a program come out of this archive?" check
    has something to look at.
    """
    calls = []

    def _put(kw, version):
        where = agent.installed_bin_under(agent.Path(kw["env"]["HOME"]))
        where.parent.mkdir(parents=True, exist_ok=True)
        where.write_text(version, encoding="utf-8")

    def run(cmd, **kw):
        argv = [str(c) for c in cmd]
        calls.append({"cmd": argv, "kw": kw})
        prog, verb = argv[0], argv[-1]
        if prog in ("tar", "unzip"):
            if unpacks:
                for flag in ("-C", "-d"):
                    if flag in argv:
                        dest = agent.Path(argv[argv.index(flag) + 1]) / "blink"
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / _bin_name()).write_text("x", encoding="utf-8")
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if verb == "--version":
            if ".blink" in prog:
                where = agent.Path(prog)
                if not where.exists():
                    raise FileNotFoundError(prog)
                version = where.read_text(encoding="utf-8")
            else:
                version = bundle_version
            return types.SimpleNamespace(returncode=0,
                                         stdout=f"blink {version}\n", stderr="")
        if verb == "install":
            if install_code == 0 and copies:
                _put(kw, bundle_version if install_leaves is ...
                     else install_leaves)
            return types.SimpleNamespace(returncode=install_code,
                                         stdout="Installed.\n", stderr="")
        if verb == "update":
            if update_code == 0 and update_to is not None:
                _put(kw, update_to)
            return types.SimpleNamespace(returncode=update_code,
                                         stdout=update_out or "updated\n",
                                         stderr="")
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    run.calls = calls
    return run


def _feed(tmp_path, name="feed"):
    """A directory shaped like the release feed BLINK_OTA_DIR serves from."""
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    for leaf in ("manifest.json", "manifest.json.sig",
                 archive_name(platform_key() or "linux-x86_64")):
        (d / leaf).write_bytes(b"x")
    return d


def _archive(tmp_path, name="blink-x.tar.gz"):
    path = tmp_path / name
    path.write_bytes(b"x")
    return path


# --- choosing the unpacker --------------------------------------------

def test_unpack_dispatches_by_extension(tmp_path):
    assert agent.unpack_cmd("b.tar.gz", tmp_path)[0] == "tar"
    cmd = agent.unpack_cmd("b.zip", tmp_path)
    assert cmd[0] in ("tar", "unzip", "powershell")


def test_unpack_of_a_zip_uses_a_tool_that_can_read_one():
    """GNU tar cannot read a zip; bsdtar can, and only Linux has the first."""
    cmd = agent.unpack_cmd("b.zip", "/dest")
    assert cmd[0] == ("unzip" if sys.platform.startswith("linux") else "tar")


def test_unpack_refuses_an_archive_it_cannot_read():
    with pytest.raises(ValueError) as e:
        agent.unpack_cmd("blink-macos-arm64.dmg", "/dest")
    assert "dmg" in str(e.value)


def test_unpack_bundle_finds_the_program_the_archive_carried(tmp_path):
    runner = _programs()
    where, problems = agent.unpack_bundle(_archive(tmp_path),
                                          tmp_path / "unpacked", runner)
    assert problems == []
    assert agent.bundle_bin(where).exists()


def test_unpack_bundle_reports_an_archive_with_no_program_in_it(tmp_path):
    runner = _programs(unpacks=False)
    where, problems = agent.unpack_bundle(_archive(tmp_path),
                                          tmp_path / "unpacked", runner)
    assert where is None
    assert any("no" in p and _bin_name() in p for p in problems)


def test_unpack_bundle_reports_a_missing_archive(tmp_path):
    where, problems = agent.unpack_bundle(tmp_path / "gone.tar.gz",
                                          tmp_path / "u", _programs())
    assert where is None and any("gone.tar.gz" in p for p in problems)


# --- the fresh install ------------------------------------------------

def test_install_scenario_asserts_version(tmp_path):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)

        class R:
            returncode = 0
            stdout = "blink 1.3.0\n"
            stderr = ""

        return R()

    res = agent.run_install_check(tmp_path, expect_version="1.3.0",
                                  runner=fake_run)
    assert res["ok"] is True
    res = agent.run_install_check(tmp_path, expect_version="9.9.9",
                                  runner=fake_run)
    assert res["ok"] is False


def test_install_check_runs_the_installer_and_the_copy_it_left(tmp_path):
    runner = _programs(bundle_version="1.3.0")
    res = agent.run_install_check(tmp_path / "bundle", expect_version="1.3.0",
                                  runner=runner, sandbox=tmp_path / "home")
    assert res["ok"] is True, res["problems"]
    verbs = [c["cmd"][-1] for c in runner.calls]
    assert verbs == ["--version", "install", "--version"]
    assert ".blink" in runner.calls[-1]["cmd"][0], \
        "the last check must read the installed copy, not the bundle"


def test_install_check_fails_when_the_installer_left_an_older_program(tmp_path):
    """The version that matters is the one under ~/.blink/bin afterwards."""
    runner = _programs(bundle_version="1.3.0", install_leaves="1.2.4")
    res = agent.run_install_check(tmp_path / "bundle", expect_version="1.3.0",
                                  runner=runner, sandbox=tmp_path / "home")
    assert res["ok"] is False
    assert any("1.2.4" in p and "1.3.0" in p for p in res["problems"])


def test_install_check_reports_an_installer_that_failed(tmp_path):
    runner = _programs(bundle_version="1.3.0", install_code=3)
    res = agent.run_install_check(tmp_path / "bundle", expect_version="1.3.0",
                                  runner=runner, sandbox=tmp_path / "home")
    assert res["ok"] is False
    assert any("install" in p and "3" in p for p in res["problems"])


def test_a_prefix_of_the_version_is_not_a_match(tmp_path):
    """`1.2` must not pass against a program reporting 1.2.4."""
    runner = _programs(bundle_version="1.2.4")
    res = agent.run_install_check(tmp_path / "bundle", expect_version="1.2",
                                  runner=runner, sandbox=tmp_path / "home")
    assert res["ok"] is False


def test_install_check_sandboxes_both_home_variables(tmp_path):
    runner = _programs(bundle_version="1.3.0")
    home = tmp_path / "home"
    agent.run_install_check(tmp_path / "bundle", expect_version="1.3.0",
                            runner=runner, sandbox=home)
    assert runner.calls
    for call in runner.calls:
        env = call["kw"]["env"]
        assert env["HOME"] == str(home) and env["USERPROFILE"] == str(home)
        assert env["BLINK_SKIP_SERVICE"] == "1"
        assert "BLINK_SCENARIO" not in env
        assert env["HOME"] != os.path.expanduser("~")


# --- what the child must not inherit ----------------------------------

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIPPED = [ROOT / "claude_usage_bridge.py", ROOT / "blink_main.py",
           *sorted((ROOT / "pc").rglob("*.py"))]
BLINK_VAR = re.compile(r"BLINK_[A-Z0-9_]+")

# Names matching the pattern that are not environment variables the program
# reads. Empty, and worth keeping empty: every entry here is a name this
# contract stops checking, so anything added needs a reason written beside it.
NOT_AN_ENV_VAR = frozenset()


def _shipped_blink_vars():
    """Every BLINK_* variable the shipped program reads, from its own source.

    Read out of the sources rather than listed here on purpose. A hand-kept
    list is a snapshot: it was already one variable out of date when the
    review found BLINK_RELEASE_PUBKEY_FILE in it, and it would have gone out
    of date again the next time somebody added one to pc/. This cannot.
    """
    found = set()
    for path in SHIPPED:
        found |= set(BLINK_VAR.findall(path.read_text(encoding="utf-8")))
    return found - NOT_AN_ENV_VAR


LEAKY = tuple(sorted(_shipped_blink_vars()))


def test_the_variable_sweep_still_finds_the_shipped_ones():
    """If this grep ever comes back empty the contract below proves nothing."""
    found = _shipped_blink_vars()
    assert {"BLINK_OTA_DIR", "BLINK_RELEASE_PUBKEY_FILE", "BLINK_SKIP_SERVICE",
            "BLINK_TAP", "BLINK_SCENARIO", "BLINK_POLL_INTERVAL_S",
            "BLINK_NO_AUTO_UPDATE", "BLINK_NO_WATCHDOG"} <= found


def test_no_blink_variable_reaches_a_child_by_inheritance(tmp_path,
                                                          monkeypatch):
    """Neither builder may pass on a BLINK_* variable it did not set itself.

    The daemon passes matter here as much as the bundle ones, and arguably
    more: an inherited BLINK_OTA_DIR redirects the FIRMWARE feed
    (pc/ota.py:52), and a scenario is a running daemon offering firmware to a
    real board. A stray variable in somebody's shell could put an unrelated
    local build in front of three of them.
    """
    for name in LEAKY:
        monkeypatch.setenv(name, "inherited")
    daemon = agent.env_for_run(tmp_path, scenario=tmp_path / "s.json",
                               tap=tmp_path / "t.jsonl", sandbox=True)
    bundle = agent.bundle_env(tmp_path, ota_dir=tmp_path / "feed")
    for name in LEAKY:
        assert daemon.get(name) != "inherited", f"{name} reached the daemon"
        assert bundle.get(name) != "inherited", f"{name} reached the bundle"


def test_each_builder_sets_back_exactly_what_it_means_to(tmp_path,
                                                         monkeypatch):
    """The whole BLINK_* surface of each child, named.

    Written as an equality rather than a list of absences so that it fails
    both ways: a variable that leaks in, and a variable the sweep took away
    that the child actually needed.
    """
    for name in LEAKY:
        monkeypatch.setenv(name, "inherited")
    scenario, tap = tmp_path / "s.json", tmp_path / "t.jsonl"

    daemon = agent.env_for_run(tmp_path, scenario=scenario, tap=tap,
                               sandbox=True, poll_interval=7.0)
    assert {k: v for k, v in daemon.items() if k.startswith("BLINK_")} == {
        "BLINK_SKIP_SERVICE": "1", "BLINK_TAP": str(tap),
        "BLINK_POLL_INTERVAL_S": "7.0", "BLINK_SCENARIO": str(scenario)}

    quiet = agent.env_for_run(tmp_path, scenario=None, tap=tap, sandbox=False)
    assert "BLINK_SCENARIO" not in quiet

    feed = tmp_path / "feed"
    assert {k: v for k, v in agent.bundle_env(tmp_path, ota_dir=feed).items()
            if k.startswith("BLINK_")} == {"BLINK_SKIP_SERVICE": "1",
                                           "BLINK_OTA_DIR": str(feed)}
    assert {k: v for k, v in agent.bundle_env(tmp_path).items()
            if k.startswith("BLINK_")} == {"BLINK_SKIP_SERVICE": "1"}


def test_bundle_env_strips_a_signing_key_override(tmp_path, monkeypatch):
    """The one variable that could turn a fabricated feed into a green run.

    BLINK_RELEASE_PUBKEY_FILE makes the update verify against a key of the
    operator's choosing (pc/update.py:73-85), and tests/ci/check_update.sh
    sets it by design -- so it is a variable somebody working in this
    repository plausibly has exported. Inherited, update_path would verify a
    throwaway locally signed release and report that it had proved the real
    signed update path.
    """
    monkeypatch.setenv("BLINK_RELEASE_PUBKEY_FILE", "/tmp/throwaway.pem")
    assert "BLINK_RELEASE_PUBKEY_FILE" not in agent.bundle_env(tmp_path)


def test_nothing_leaky_reaches_any_child_of_either_scenario(
        tmp_path, monkeypatch):
    for name in LEAKY:
        monkeypatch.setenv(name, "leaked")
    runner = _programs(bundle_version="1.2.4", update_to="1.3.0")
    agent.run_install_check(tmp_path / "b", expect_version="1.2.4",
                            runner=runner, sandbox=tmp_path / "h1")
    agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                           expect_version="1.3.0", runner=runner,
                           sandbox=tmp_path / "h2")
    assert runner.calls
    for call in runner.calls:
        env = call["kw"]["env"]
        for name in LEAKY:
            if name == "BLINK_OTA_DIR" and call["cmd"][-1] == "update":
                assert env[name] == str(tmp_path / "feed")
                continue
            assert env.get(name) != "leaked", \
                f"{name} reached `{call['cmd'][-1]}`"


# --- the update path --------------------------------------------------

def test_update_check_takes_the_candidate_off_the_feed(tmp_path):
    runner = _programs(bundle_version="1.2.4", update_to="1.3.0")
    res = agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                                 expect_version="1.3.0", runner=runner,
                                 sandbox=tmp_path / "home")
    assert res["ok"] is True, res["problems"]
    update = [c for c in runner.calls if c["cmd"][-1] == "update"]
    assert len(update) == 1
    assert update[0]["kw"]["env"]["BLINK_OTA_DIR"] == str(tmp_path / "feed")


def test_update_check_fails_when_the_feed_was_refused(tmp_path):
    """An unsigned or unreadable feed exits 1 and must never read as a pass."""
    runner = _programs(bundle_version="1.2.4", update_code=1,
                       update_out="Could not read the release feed, or it is"
                                  " not properly signed.\n")
    res = agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                                 expect_version="1.3.0", runner=runner,
                                 sandbox=tmp_path / "home")
    assert res["ok"] is False
    assert any("properly signed" in p for p in res["problems"])


def test_update_check_fails_when_nothing_newer_was_taken(tmp_path):
    """`blink update` exits 0 for "Already up to date." as well."""
    runner = _programs(bundle_version="1.2.4", update_to=None,
                       update_out="Already up to date.\n")
    res = agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                                 expect_version="1.3.0", runner=runner,
                                 sandbox=tmp_path / "home")
    assert res["ok"] is False
    assert any("1.2.4" in p and "1.3.0" in p for p in res["problems"])


def test_update_check_refuses_a_previous_bundle_at_the_candidate_version(
        tmp_path):
    """Updating from 1.3.0 to 1.3.0 would pass while proving nothing."""
    runner = _programs(bundle_version="1.3.0", update_to="1.3.0")
    res = agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                                 expect_version="1.3.0", runner=runner,
                                 sandbox=tmp_path / "home")
    assert res["ok"] is False
    assert any("1.3.0" in p and "--prev-bundle" in p for p in res["problems"])
    assert not [c for c in runner.calls if c["cmd"][-1] == "update"]


def test_update_check_starts_from_an_install_of_the_previous_release(tmp_path):
    """The rotation over an existing install is the risky half of an update."""
    runner = _programs(bundle_version="1.2.4", update_to="1.3.0")
    agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                           expect_version="1.3.0", runner=runner,
                           sandbox=tmp_path / "home")
    verbs = [c["cmd"][-1] for c in runner.calls]
    assert verbs.index("install") < verbs.index("update")


def test_the_update_is_run_from_the_installed_copy(tmp_path):
    """A customer updates the program that is running from ~/.blink/bin.

    That is the whole risk in an update: update.apply rotates the directory
    the running executable is inside of (pc/update.py:322-360). Run from the
    unpacked bundle instead, the rename is of a directory nothing is running
    from -- which is the one arrangement that cannot fail the way Windows
    fails.
    """
    runner = _programs(bundle_version="1.2.4", update_to="1.3.0")
    agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                           expect_version="1.3.0", runner=runner,
                           sandbox=tmp_path / "home")
    update = [c for c in runner.calls if c["cmd"][-1] == "update"]
    assert len(update) == 1
    assert ".blink" in update[0]["cmd"][0], \
        "the update must be run by the installed program, not by the bundle"


def test_update_check_sandboxes_both_home_variables(tmp_path):
    runner = _programs(bundle_version="1.2.4", update_to="1.3.0")
    home = tmp_path / "home"
    agent.run_update_check(tmp_path / "prev", tmp_path / "feed",
                           expect_version="1.3.0", runner=runner, sandbox=home)
    for call in runner.calls:
        env = call["kw"]["env"]
        assert env["HOME"] == str(home) and env["USERPROFILE"] == str(home)
        assert env["BLINK_SKIP_SERVICE"] == "1"
        if call["cmd"][-1] != "update":
            assert "BLINK_OTA_DIR" not in env


# --- reading what a program printed -----------------------------------

def test_the_unpacker_is_decoded_here_rather_than_by_subprocess(tmp_path):
    """tar.exe is not one of our children and ignores PYTHONIOENCODING.

    On the Windows desk it writes in the machine's own code page, and the
    message it writes contains the path -- which on that desk contains the
    non-ASCII profile name. Forced through UTF-8 it comes back as replacement
    characters, degrading the one diagnostic a failed unpack has.
    """
    seen = {}

    def runner(cmd, **kw):
        seen.update(kw)
        return types.SimpleNamespace(returncode=1, stdout=b"",
                                     stderr=b"tar: no such file")

    where, problems = agent.unpack_bundle(_archive(tmp_path), tmp_path / "u",
                                          runner)
    assert where is None and "no such file" in problems[0]
    assert "encoding" not in seen and "errors" not in seen


def test_a_message_in_the_machines_own_encoding_still_reads(monkeypatch):
    monkeypatch.setattr(agent.locale, "getpreferredencoding",
                        lambda *a: "cp1255")
    assert agent._decode_console("גלית".encode("cp1255")) == "גלית"


def test_decoding_never_raises_on_bytes_nothing_can_read():
    assert isinstance(agent._decode_console(b"\xff\xfe\xff"), str)
    assert agent._decode_console(b"plain") == "plain"
    assert agent._decode_console("already text") == "already text"
    assert agent._decode_console(None) == ""


def test_our_own_children_are_still_decoded_as_utf8(tmp_path):
    """They are told PYTHONIOENCODING=utf-8, so they are read back by name."""
    runner = _programs(bundle_version="1.3.0")
    agent.run_install_check(tmp_path / "b", expect_version="1.3.0",
                            runner=runner, sandbox=tmp_path / "home")
    ours = [c for c in runner.calls if c["cmd"][0] not in ("tar", "unzip")]
    assert ours and all(c["kw"]["encoding"] == "utf-8" for c in ours)


# --- the feed the update reads ----------------------------------------

def test_feed_dir_names_every_file_it_is_missing(tmp_path):
    empty = tmp_path / "feed"
    empty.mkdir()
    problems = agent.check_feed_dir(empty)
    assert any("manifest.json.sig" in p for p in problems)
    assert any(archive_name(platform_key() or "linux-x86_64") in p
               for p in problems)


def test_a_complete_feed_dir_has_nothing_to_say(tmp_path):
    assert agent.check_feed_dir(_feed(tmp_path)) == []


def test_a_feed_dir_that_is_not_there_is_reported(tmp_path):
    problems = agent.check_feed_dir(tmp_path / "nowhere")
    assert any("nowhere" in p for p in problems)


# --- wiring into the run ----------------------------------------------

def _scenarios(tmp_path):
    doc = {"name": "x", "duration_s": 1,
           "steps": [{"at": 0, "provider": "claude", "session_pct": 50.0}],
           "expect": {"min_tx": 1, "min_board_usage": 1,
                      "min_stale_lines": 0, "min_sleep_wakes": 0}}
    d = tmp_path / "scenarios"
    d.mkdir(exist_ok=True)
    (d / "x.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def _deps(runner, **over):
    base = dict(spawn=lambda cmd, env, cwd: None,
                sleep=lambda s: None, now=lambda: 0.0, wall=lambda: 0.0,
                runner=runner,
                stop=lambda: Outcome(True, False, "stopped"),
                start=lambda: Outcome(True, False, "started"))
    base.update(over)
    return agent.Deps(**base)


def test_customer_scenarios_land_in_the_results(tmp_path):
    runner = _programs(bundle_version="1.3.0", update_to="1.3.0")
    args = agent.parse_args([
        "--scenarios", str(_scenarios(tmp_path)),
        "--out", str(tmp_path / "r.json"),
        "--bundle", str(_archive(tmp_path)),
        "--expect-version", "1.3.0"])
    out = agent.customer_path(args, tmp_path / "work", _deps(runner))
    assert out["fresh_install"]["ok"] is True, out["fresh_install"]["problems"]
    assert "update_path" not in out


def test_the_update_scenario_needs_a_feed_and_a_previous_bundle(tmp_path):
    runner = _programs(bundle_version="1.2.4", update_to="1.3.0")
    args = agent.parse_args([
        "--scenarios", str(_scenarios(tmp_path)),
        "--out", str(tmp_path / "r.json"),
        "--prev-bundle", str(_archive(tmp_path, "blink-prev.tar.gz")),
        "--ota-dir", str(_feed(tmp_path)),
        "--expect-version", "1.3.0"])
    out = agent.customer_path(args, tmp_path / "work", _deps(runner))
    assert out["update_path"]["ok"] is True, out["update_path"]["problems"]


def _customer_args(tmp_path, *extra):
    return agent.parse_args([
        "--scenarios", str(_scenarios(tmp_path)),
        "--out", str(tmp_path / "r.json"),
        "--expect-version", "1.3.0", *extra])


def test_fresh_install_does_not_pass_on_the_last_runs_residue(tmp_path):
    """The work root is a fixed path and nothing else clears it.

    So `<out>/fleet-work/fresh_install/home/.blink/bin/blink` outlives the run
    that made it, and the next run's version check would read a program the
    installer never wrote. An installer that exits 0 having copied nothing --
    what an unfrozen archive produces -- would then be proved correct by the
    leftovers of the run before, which is precisely the packaging fault this
    scenario exists to catch.
    """
    work = tmp_path / "work"
    stale = agent.installed_bin_under(work / agent.FRESH_INSTALL / "home")
    stale.parent.mkdir(parents=True)
    stale.write_text("1.3.0", encoding="utf-8")

    runner = _programs(bundle_version="1.3.0", copies=False)
    args = _customer_args(tmp_path, "--bundle", str(_archive(tmp_path)))
    out = agent.customer_path(args, work, _deps(runner))
    assert out[agent.FRESH_INSTALL]["ok"] is False
    assert not stale.exists(), "the work directory was not cleared"


def test_the_update_scenario_also_starts_from_a_cleared_directory(tmp_path):
    work = tmp_path / "work"
    stale = agent.installed_bin_under(work / agent.UPDATE_PATH / "home")
    stale.parent.mkdir(parents=True)
    stale.write_text("1.3.0", encoding="utf-8")

    runner = _programs(bundle_version="1.2.4", copies=False)
    args = _customer_args(tmp_path,
                          "--prev-bundle", str(_archive(tmp_path)),
                          "--ota-dir", str(_feed(tmp_path)))
    out = agent.customer_path(args, work, _deps(runner))
    assert out[agent.UPDATE_PATH]["ok"] is False
    assert not stale.exists(), "the work directory was not cleared"


def test_a_second_run_over_the_first_still_passes(tmp_path):
    """Clearing must not make the scenario fail on its own leftovers."""
    work = tmp_path / "work"
    args = _customer_args(tmp_path, "--bundle", str(_archive(tmp_path)))
    for _ in range(2):
        out = agent.customer_path(args, work,
                                  _deps(_programs(bundle_version="1.3.0")))
        assert out[agent.FRESH_INSTALL]["ok"] is True, \
            out[agent.FRESH_INSTALL]["problems"]


def test_even_the_unpacker_runs_with_a_redirected_home(tmp_path):
    """The invariant is both variables, always -- with no exception for tar."""
    runner = _programs(bundle_version="1.3.0")
    args = _customer_args(tmp_path, "--bundle", str(_archive(tmp_path)))
    agent.customer_path(args, tmp_path / "work", _deps(runner))
    unpackers = [c for c in runner.calls if c["cmd"][0] in ("tar", "unzip")]
    assert unpackers, "no unpack was attempted"
    for call in runner.calls:
        env = call["kw"]["env"]
        assert env["HOME"] != os.path.expanduser("~")
        assert env["USERPROFILE"] != os.path.expanduser("~")
        assert env["HOME"] == env["USERPROFILE"]


def test_a_bundle_without_an_expected_version_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        agent.parse_args(["--out", str(tmp_path / "r.json"),
                          "--bundle", "blink.tar.gz"])


def test_a_previous_bundle_without_a_feed_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        agent.parse_args(["--out", str(tmp_path / "r.json"),
                          "--prev-bundle", "blink.tar.gz",
                          "--expect-version", "1.3.0"])


def test_the_customer_path_does_not_need_a_board(tmp_path):
    """Neither scenario opens the serial port, so an unplugged desk still
    gets a verdict on the half of the product a customer meets first."""
    runner = _programs(bundle_version="1.3.0")
    args = agent.parse_args([
        "--scenarios", str(_scenarios(tmp_path)),
        "--out", str(tmp_path / "r.json"),
        "--bundle", str(_archive(tmp_path)),
        "--expect-version", "1.3.0"])
    result = agent.run(args, _deps(runner, stop=lambda: Outcome(
        True, True, "skipped (BLINK_SKIP_SERVICE=1)")))
    assert result["scenarios"]["fresh_install"]["ok"] is True
    assert result["ok"] is False, "the skipped service stop is still a failure"


def test_the_sandbox_home_is_not_the_one_the_daemon_scenarios_share(tmp_path):
    """An install writes a program and hooks into the home it is given."""
    runner = _programs(bundle_version="1.3.0")
    args = agent.parse_args([
        "--scenarios", str(_scenarios(tmp_path)),
        "--out", str(tmp_path / "r.json"),
        "--bundle", str(_archive(tmp_path)),
        "--expect-version", "1.3.0"])
    work = tmp_path / "work"
    agent.customer_path(args, work, _deps(runner))
    homes = {call["kw"]["env"]["HOME"] for call in runner.calls
             if "env" in call["kw"]}
    assert str(agent.sandbox_home(work)) not in homes
