"""The setup the customer runs.

This replaces tests/test_install_sh.py. install.sh needed Python on the
customer's machine, built a virtualenv and reached PyPI at install time; the
binary needs none of that, so the shell script and its tests are gone.

Tested against the module rather than the built binary: the logic is the same
either way, and freezing an 11 MB executable per assertion would buy nothing.
The binary itself is exercised end to end by tests/ci/check_install.sh, which
CI runs on a real macOS and Linux runner.
"""
import json
import os
import sys
import time

import pytest

from pc import cli, install_statusline


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """The one thing these tests need that the whole suite does not.

    The redirected HOME and the login-service guard both live in
    tests/conftest.py now -- see the note there for what went wrong when they
    were a per-file concern.

    CODEX_HOME is cleared here for the same class of reason: install now edits
    a file under it, and Codex honours the variable, so a developer who has it
    set in their shell would have had these tests write a hooks.json into
    their real Codex install. HOME alone does not sandbox a path that an
    environment variable can override.
    """
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir()


def _settings(tmp_path, obj=None):
    p = tmp_path / ".claude" / "settings.json"
    if obj is not None:
        p.write_text(json.dumps(obj))
    return p


def _read(tmp_path):
    return json.loads(_settings(tmp_path).read_text())


def test_install_points_the_statusline_at_the_shim(tmp_path, capsys):
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    shim = tmp_path / ".blink" / "blink-statusline.sh"
    assert shim.exists() and os.access(shim, os.X_OK)
    assert (_read(tmp_path)["statusLine"]["command"]
            == cli.install_statusline.statusline_command(str(shim)))


def test_bare_invocation_installs(tmp_path):
    """Someone who downloads a file and runs it means "set this up"."""
    _settings(tmp_path, {})
    assert cli.main([]) == 0
    assert (tmp_path / ".blink" / "blink-statusline.sh").exists()


def test_an_existing_statusline_is_kept_and_still_runs(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    chain = (tmp_path / ".blink" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-bar.sh"


def test_unrelated_settings_survive(tmp_path):
    _settings(tmp_path, {"model": "opus", "env": {"FOO": "bar"}})
    cli.main(["install"])
    got = _read(tmp_path)
    assert got["model"] == "opus" and got["env"] == {"FOO": "bar"}


def test_install_discloses_before_it_writes(tmp_path, capsys):
    """It asks nothing, so the disclosure is the only safeguard."""
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    out = capsys.readouterr().out
    # Split on the step marker by pattern, not the literal "[1/3]": the step
    # count changes whenever a step is added, and partition() on a separator
    # that is no longer there returns the WHOLE output as the disclosure --
    # so every assertion below passed vacuously while the real check was gone.
    import re
    m = re.search(r"^\[1/\d\]", out, re.M)
    assert m, "install printed no step markers at all"
    disclosure, rest = out[:m.start()], out[m.start():]
    assert str(_settings(tmp_path)) in disclosure
    assert "statusLine.command" in disclosure
    assert "sh ~/my-bar.sh" in disclosure
    assert "uninstall" in disclosure
    assert rest, "the disclosure must come before the work"


def test_the_disclosure_names_every_file_install_writes(tmp_path, capsys):
    """It asks nothing, so a disclosure that is merely MOSTLY right is worse
    than none -- it is the thing people rely on instead of reading the diff.

    This pins the failure that actually happened: the hooks key started being
    written while the disclosure still said 'statusLine.command, and nothing
    else in the file'.
    """
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    import re
    out = capsys.readouterr().out
    disclosure = out[:re.search(r"^\[1/\d\]", out, re.M).start()]

    assert "hooks" in disclosure, "disclosure omits the hooks key it writes"
    assert str(cli.hook_shim_path()) in disclosure, \
        "disclosure omits the hook shim it creates"
    # The hook shim records session and agent ids. The disclosure is the only
    # safeguard on an install that asks nothing, so it has to say so.
    assert "session and" in disclosure and "agent ids" in disclosure, \
        "disclosure does not mention the ids the hook shim records"
    assert "state" in disclosure, "disclosure omits the state directory"
    assert str(cli.shim_path()) in disclosure
    assert str(_settings(tmp_path)) in disclosure

    # And everything it named is a thing that now exists or was changed.
    settings = json.loads(_settings(tmp_path).read_text())
    assert "hooks" in settings
    assert os.path.exists(cli.hook_shim_path())


def test_install_is_idempotent(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    cli.main(["install"])
    chain = (tmp_path / ".blink" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-bar.sh", "second run chained the shim to itself"


def test_uninstall_restores_their_command(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    cli.main(["uninstall"])
    assert _read(tmp_path)["statusLine"]["command"] == "sh ~/my-bar.sh"
    assert not (tmp_path / ".blink" / "blink-statusline.sh").exists()


def test_uninstall_keeps_the_ota_signing_key(tmp_path):
    """~/.blink is shared with a key that cannot be regenerated."""
    key = tmp_path / ".blink" / "ota_signing_key_p256.pem"
    key.parent.mkdir(exist_ok=True)
    key.write_text("PRIVATE KEY")
    _settings(tmp_path, {})
    cli.main(["install"])
    cli.main(["uninstall"])
    assert key.read_text() == "PRIVATE KEY"


def test_uninstall_leaves_a_foreign_statusline_alone(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["uninstall"])
    assert _read(tmp_path)["statusLine"]["command"] == "sh ~/my-bar.sh"


def test_install_on_a_machine_where_claude_never_wrote_settings(tmp_path):
    """~/.claude/settings.json only exists once a setting has been changed."""
    _settings(tmp_path).unlink(missing_ok=True)
    assert cli.main(["install"]) == 0
    assert "blink-statusline.sh" in _read(tmp_path)["statusLine"]["command"]


def test_status_runs_before_and_after_install(tmp_path, capsys):
    assert cli.main(["status"]) == 0
    assert "none yet" in capsys.readouterr().out
    _settings(tmp_path, {})
    cli.main(["install"])
    (tmp_path / ".blink" / "statusline.json").write_text("{}")
    assert cli.main(["status"]) == 0
    assert "fresh" in capsys.readouterr().out


def test_the_shim_it_writes_is_the_one_in_the_tree(tmp_path):
    """One source of truth, so the shipped shim cannot drift from the repo."""
    _settings(tmp_path, {})
    cli.main(["install"])
    here = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    src = open(os.path.join(here, "tools", "blink-statusline.sh")).read()
    assert (tmp_path / ".blink" / "blink-statusline.sh").read_text() == src


def test_too_old_claude_warns_rather_than_refusing(tmp_path, capsys, monkeypatch):
    """Everything installed stays correct, so it works the moment they update."""
    monkeypatch.setattr(cli, "claude_version", lambda: ("2.1.0 (Claude Code)", (2, 1, 0)))
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "needs 2.1.100 or newer" in out
    assert _read(tmp_path)["statusLine"]["command"]  # installed anyway


def test_run_does_not_hand_the_subcommand_name_to_the_daemon(monkeypatch):
    """`blink run` must not leave "run" in the daemon's own argv.

    It did: claude_usage_bridge.main() parsed sys.argv itself, saw the
    subcommand name, rejected it and exited. The login service restarted it
    every ten seconds and the board never received anything -- 866 KB of
    identical errors before anyone looked at the log.
    """
    seen = {}

    class FakeBridge:
        @staticmethod
        def main(argv=None):
            seen["argv"] = argv

    monkeypatch.setitem(__import__("sys").modules, "claude_usage_bridge", FakeBridge)
    assert cli.main(["run"]) == 0
    assert seen["argv"] is not None, "argv must be passed, not left to sys.argv"
    assert "run" not in seen["argv"]


def test_run_forwards_an_explicit_port(monkeypatch):
    seen = {}

    class FakeBridge:
        @staticmethod
        def main(argv=None):
            seen["argv"] = argv

    monkeypatch.setitem(__import__("sys").modules, "claude_usage_bridge", FakeBridge)
    cli.main(["run", "--port", "/dev/cu.usbserial-1"])
    assert "--port" in seen["argv"] and "/dev/cu.usbserial-1" in seen["argv"]


def test_uninstall_says_so_when_the_binary_will_not_go(tmp_path, monkeypatch,
                                                       capsys):
    """An uninstall that cannot finish must not report that it did.

    On Windows the daemon holds its own .exe open and `schtasks /end` returns
    before the process has exited, so the delete lost the race -- and
    rmtree(ignore_errors=True) swallowed it, leaving a 12 MB binary behind
    under a printed "removed". Six CI scenarios went red the moment the daemon
    stopped crashing on startup and lived long enough to hold the file.
    """
    cli.main(["install"])
    assert os.path.exists(cli.bin_dir())

    monkeypatch.setattr(cli.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)   # no real waiting

    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out
    if sys.platform == "win32":
        # Windows hands the delete to a detached cmd that outlives this
        # process (_schedule_windows_cleanup), so the uninstall IS going to
        # finish and says so; exiting 1 there would be the false report.
        assert rc == 0
        assert "will be gone" in out
        return
    assert rc == 1, "a failed uninstall must not exit 0"
    assert "could not be removed" in out
    assert cli.bin_dir() in out, "it has to say which path to delete by hand"


def test_uninstall_reports_removed_when_it_worked(tmp_path, capsys):
    cli.main(["install"])
    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not be removed" not in out
    assert not os.path.exists(cli.bin_dir())


def test_install_refuses_to_touch_a_settings_file_that_will_not_parse(
        tmp_path, capsys):
    """Treating unparseable JSON as {} would write a fresh settings.json over
    whatever the customer was halfway through editing."""
    claude = tmp_path / ".claude"
    claude.mkdir(exist_ok=True)
    broken = '{"statusLine": {"command": "sh /their/bar.sh",}}'   # trailing comma
    (claude / "settings.json").write_text(broken)

    rc = cli.main(["install"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "not valid JSON" in out
    assert "Nothing was changed" in out
    assert (claude / "settings.json").read_text() == broken
    assert not os.path.exists(cli.bin_dir()), "it installed anyway"


def test_uninstall_finishes_even_when_settings_will_not_parse(tmp_path, capsys):
    """The login service is removed before this step, so stopping here would
    leave the machine half-undone."""
    cli.main(["install"])
    (tmp_path / ".claude" / "settings.json").write_text("{ not json")

    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out

    assert "Left alone" in out
    assert not os.path.exists(cli.bin_dir()), "the binary survived"
    assert rc == 0


def test_status_reports_the_hooks_not_only_the_status_line(tmp_path, capsys):
    """Install writes two things into settings.json, so status must report
    two. Someone whose activity pip never lights needs a way to see whether
    the hooks are there; 'Status line installed' answers a different
    question."""
    _settings(tmp_path, {})
    cli.main(["install"])
    capsys.readouterr()
    cli.main(["status"])
    # Derived, not hardcoded: the event list grows, and a pinned "6/6" turns
    # every addition into a spurious failure in a test about something else.
    from pc import install_hooks
    n = len(install_hooks.HOOK_EVENTS)
    assert f"hooks installed ({n}/{n} events" in capsys.readouterr().out


def test_status_notices_hooks_that_went_missing(tmp_path, capsys):
    """Counted from settings.json, not from the shim existing on disk: the
    file being there proves an install ran once, not that Claude Code is
    still configured to call it."""
    _settings(tmp_path, {})
    cli.main(["install"])
    from pc import install_hooks
    n = len(install_hooks.HOOK_EVENTS)
    data = json.loads(_settings(tmp_path).read_text())
    del data["hooks"]["PreToolUse"]
    del data["hooks"]["Stop"]
    _settings(tmp_path).write_text(json.dumps(data))
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert f"PARTIAL -- {n - 2}/{n}" in out
    assert "install` to restore them" in out


def test_status_does_not_call_a_missing_hook_shim_out_of_date(tmp_path, capsys):
    """Absent and stale are different faults and want different sentences.

    The hooks in settings.json still point at the shim path, so Claude Code
    runs a file that is not there on every event and the pip never lights at
    all. "Out of date" sends the reader hunting for a version mismatch in a
    file they will not find.
    """
    _settings(tmp_path, {})
    cli.main(["install"])
    os.remove(cli.hook_shim_path())
    capsys.readouterr()
    cli.main(["status"])

    out = capsys.readouterr().out
    assert "the activity hook shim is missing" in out
    assert "out of date" not in out
    # What went wrong AND what to do about it: the daemon rewrites a missing
    # shim exactly as it rewrites a stale one, so the promise still holds.
    assert "the hooks run nothing --" in out


def test_status_still_says_out_of_date_for_a_shim_that_is_merely_stale(
        tmp_path, capsys):
    """The other half of the same branch: a file that exists and is wrong."""
    _settings(tmp_path, {})
    cli.main(["install"])
    with open(cli.hook_shim_path(), "w") as f:
        f.write("#!/bin/sh\n# an older install left this here\n")
    capsys.readouterr()
    cli.main(["status"])

    out = capsys.readouterr().out
    assert "the activity hook shim is out of date" in out
    assert "missing" not in out


def test_live_sessions_counts_real_sessions_not_zero(tmp_path):
    """_live_sessions() unpacks ClaudeStateProvider.scan()'s return tuple by
    hand. scan() gained a third value (names, for the session-name hint) and
    the bare `except Exception: return 0` around this call means a stale
    2-value unpack does not crash -- it just silently reports zero live
    sessions forever, on every machine, whether or not hooks are installed.
    Nothing else in this suite calls _live_sessions(), so this is the only
    test that would have caught that regression."""
    state_dir = tmp_path / ".blink" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.state").write_text(
        json.dumps({"event": "PreToolUse", "t": time.time()}))
    (state_dir / "s2.state").write_text(
        json.dumps({"event": "Notification", "t": time.time()}))
    assert cli._live_sessions() == 2


def test_status_says_so_when_no_hooks_are_installed(tmp_path, capsys):
    _settings(tmp_path, {"statusLine": {"type": "command", "command": "x"}})
    cli.main(["status"])
    assert "hooks not installed" in capsys.readouterr().out


# --- Claude Code absent ----------------------------------------------------
#
# Steps 2 and 3 write into ~/.claude/settings.json. With no Claude Code on the
# machine nothing ever reads that file, so both steps reported success and
# produced nothing -- a device that half works, and a customer with no way to
# learn why. These tests are about saying so.


def test_no_claude_code_but_a_desktop_cache_says_what_is_missing(
        tmp_path, capsys, monkeypatch):
    """The real desktop-only desk. It works, with less on it, and the install
    now names which less."""
    monkeypatch.setattr(cli, "claude_version", lambda: (None, None))
    monkeypatch.setattr(cli, "desktop_app_present", lambda: True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Claude Desktop alone" in out
    assert "reset countdowns" in out
    assert "activity light" in out
    # The reason, not just the fact -- otherwise it reads as unimplemented.
    assert "does not record when either window resets" in out


def test_no_claude_code_still_installs_everything(tmp_path, capsys, monkeypatch):
    """A note, not a refusal, and not a failed step. The edits are correct and
    start working by themselves the day Claude Code arrives -- exactly the
    rule the too-old warning already follows."""
    monkeypatch.setattr(cli, "claude_version", lambda: (None, None))
    monkeypatch.setattr(cli, "desktop_app_present", lambda: True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    settings = _read(tmp_path)
    assert settings["statusLine"]["command"]
    assert settings.get("hooks")


def test_neither_source_is_stated_more_firmly(tmp_path, capsys, monkeypatch):
    """No Claude Code and no desktop cache is not a reduced panel, it is an
    empty one, and the copy must not soften that into 'works with less'."""
    monkeypatch.setattr(cli, "claude_version", lambda: (None, None))
    monkeypatch.setattr(cli, "desktop_app_present", lambda: False)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Nothing on this machine reports usage yet" in out
    assert "sit blank" in out
    assert "Claude Desktop alone" not in out


def test_a_working_claude_code_says_none_of_it(tmp_path, capsys, monkeypatch):
    """The common machine pays nothing for this."""
    monkeypatch.setattr(cli, "claude_version",
                        lambda: ("2.1.245 (Claude Code)", (2, 1, 245)))
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Claude Desktop alone" not in out
    assert "Nothing on this machine reports usage" not in out


def test_the_disclosure_counts_the_hooks_it_actually_writes(tmp_path, capsys,
                                                            monkeypatch):
    """It said six while writing ten. The number drifted when three events were
    added and nothing failed -- and this text is the only consent the customer
    is asked for before we edit a file they own."""
    monkeypatch.setattr(cli, "claude_version",
                        lambda: ("2.1.245 (Claude Code)", (2, 1, 245)))
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    n = len(cli.install_hooks.HOOK_EVENTS)
    assert f"for each of {n} Claude Code" in out
    written = _read(tmp_path).get("hooks") or {}
    assert len(written) == n, "disclosure and reality must agree"


def test_a_settings_file_that_is_not_an_object_is_refused_not_crashed(tmp_path):
    """Valid JSON, wrong shape. Five call sites did data.get(...) on it; in the
    daemon that AttributeError killed main() and KeepAlive restarted it every
    ten seconds forever."""
    for doc in ("[]", "null", '"a string"', "42"):
        (tmp_path / ".claude").mkdir(exist_ok=True)
        (tmp_path / ".claude" / "settings.json").write_text(doc)
        with pytest.raises(install_statusline.SettingsUnreadable):
            install_statusline._load(str(tmp_path / ".claude" / "settings.json"))


def test_a_statusline_that_is_not_a_dict_reads_as_absent():
    """`{"statusLine": "my-bar"}` -- the `or {}` idiom looked safe and was not:
    only None and {} take that branch."""
    for bad in ("my-bar", [], 7, None):
        assert install_statusline._current_command({"statusLine": bad}) == ""
    assert install_statusline._current_command({"statusLine": {"command": 9}}) == ""
    assert install_statusline._current_command(
        {"statusLine": {"command": "x"}}) == "x"


# --- Codex hooks -----------------------------------------------------------
#
# Install now edits a second vendor's configuration. These tests never let it
# reach a real one: codex_present and install_codex_hooks.install are both
# replaced, HOME is tmp_path (conftest) and CODEX_HOME is cleared (_sandbox).

# The column the labelled block in _announce_codex_hooks wraps into. A line
# indented this far continues the sentence above it and is not a new one, so
# the sentence-case rule below does not apply to it.
_CODEX_WRAP = " " * 11


def test_install_says_codex_will_ask_before_it_writes(monkeypatch, capsys):
    """Disclosure before the write, not after -- and specifically the trust
    prompt, because an unexplained dialog from a tool the user did not think
    they were configuring is a support incident, not a feature."""
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    written = []
    monkeypatch.setattr(
        cli.install_codex_hooks, "install",
        lambda p, s: written.append(p) or "installed (10 events).")

    cli._announce_codex_hooks()
    out_before = capsys.readouterr().out
    cli._install_codex_hooks()

    assert written, "the test must actually reach the installer"
    assert "trust" in out_before.lower(), \
        "the disclosure does not mention the prompt Codex will show"
    assert "Codex" in out_before
    for line in out_before.splitlines():
        if not line.strip() or line.startswith(_CODEX_WRAP):
            continue
        assert line.strip()[0].isupper(), f"copy is sentence case: {line!r}"


def test_install_skips_the_codex_step_when_codex_is_absent(monkeypatch):
    """Writing a hooks file for a tool that is not installed would leave a
    stranger's configuration on the machine."""
    monkeypatch.setattr(cli, "codex_present", lambda: False)
    called = []
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: called.append(p))
    msg = cli._install_codex_hooks()
    assert called == []
    assert "no Codex" in msg


def test_install_does_not_fail_when_the_codex_config_is_unreadable(monkeypatch):
    """The status line is the product; the activity light is a nicety. A hooks
    file we cannot safely edit costs a pip, not an install."""
    monkeypatch.setattr(cli, "codex_present", lambda: True)

    def boom(_p, _s):
        raise cli.install_statusline.SettingsUnreadable("not valid JSON")

    monkeypatch.setattr(cli.install_codex_hooks, "install", boom)
    msg = cli._install_codex_hooks()
    assert msg.startswith("skipped")
    assert "not valid JSON" in msg


def test_install_creates_the_codex_state_directory_private(monkeypatch):
    """The slots name the projects someone has open. The default umask would
    leave them readable by every account on the machine."""
    import stat
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: "installed (10 events).")
    cli._install_codex_hooks()
    d = os.path.join(cli.blink_home(), "state-codex")
    assert os.path.isdir(d)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_uninstall_removes_the_codex_slots_too(tmp_path):
    """A left-behind slot directory is not litter, it is a lie: the daemon
    would go on counting sessions from a tool it no longer hooks."""
    for sub in ("state", "state-codex"):
        d = os.path.join(cli.blink_home(), sub)
        os.makedirs(os.path.join(d, "sess-1"), exist_ok=True)
        with open(os.path.join(d, "sess-1.state"), "w") as f:
            f.write("{}")
        with open(os.path.join(d, "sess-1", "agent-1"), "w") as f:
            f.write("")

    cli._rm_state_dir()

    for sub in ("state", "state-codex"):
        d = os.path.join(cli.blink_home(), sub)
        assert not os.path.exists(os.path.join(d, "sess-1.state"))
        assert not os.path.exists(os.path.join(d, "sess-1"))


def test_uninstall_keeps_going_when_the_codex_file_is_unreadable(capsys):
    """The login service is already gone by this point in cmd_uninstall, so
    stopping here would leave the machine half-undone.

    Against a real unparseable file, not a stub that raises. This used to
    monkeypatch install_codex_hooks.uninstall into raising SettingsUnreadable
    so that cmd_uninstall's handler had something to catch -- but uninstall()
    catches its own at every point that can raise one and returns a sentence
    instead, so the exception being caught could not occur and the handler has
    gone. What is left to prove is the property that actually matters: the
    step reports and returns, and the file it could not parse is untouched.
    """
    hooks = cli.install_codex_hooks.hooks_file()
    os.makedirs(os.path.dirname(hooks), exist_ok=True)
    with open(hooks, "w") as f:
        f.write("{ not json")

    cli._uninstall_codex_hooks()

    assert "left it alone" in capsys.readouterr().out
    with open(hooks) as f:
        assert f.read() == "{ not json", "it rewrote a file it cannot parse"


def test_uninstall_says_it_left_the_codex_trust_record_alone(tmp_path, capsys,
                                                             monkeypatch):
    """The ruling this carries is only half a decision if the user cannot see
    it. Removing the trust record would make a headless reinstall silently
    dead -- Codex skips a distrusted hook with no output at all -- so it stays,
    and uninstall says so, names the file, and says how to undo it."""
    codex = tmp_path / ".codex"
    codex.mkdir()
    config = codex / "config.toml"
    config.write_text('[hooks.state."k"]\ntrusted_hash = "sha256:0"\n')
    monkeypatch.setattr(cli.install_codex_hooks, "uninstall",
                        lambda p, s=None: "Codex state hooks removed (10).")

    cli._uninstall_codex_hooks()

    out = capsys.readouterr().out
    assert "trust" in out.lower(), "uninstall says nothing about the trust record"
    assert str(config) in out, "it does not name the file it left behind"
    assert "hooks.state" in out, "it does not say how to undo it"
    # And the record really is still there. This is the assertion the ruling
    # turns on: a single added os.remove() in _uninstall_codex_hooks would be
    # invisible to every other check in this file.
    assert "trusted_hash" in config.read_text()


def test_uninstall_says_nothing_about_a_codex_config_that_is_not_there(
        monkeypatch, capsys):
    """The majority of machines have no Codex. A paragraph about a config file
    that does not exist is noise about a record that was never written."""
    monkeypatch.setattr(cli.install_codex_hooks, "uninstall",
                        lambda p, s=None: "No Codex state hooks to remove.")
    cli._uninstall_codex_hooks()
    assert "trust" not in capsys.readouterr().out.lower()


def test_status_reports_codex_hooks_not_installed(monkeypatch):
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker", lambda: set())
    lines = cli._codex_hook_status()
    assert any("not installed" in ln for ln in lines)


def test_status_reports_a_registered_hook_that_has_never_fired(monkeypatch):
    """Which is exactly what declining the trust prompt looks like, and the
    single most likely support call this feature will generate: under
    `codex exec` a distrusted hook is skipped with no prompt and no output at
    all, so there is nothing else anywhere to see."""
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})
    monkeypatch.setattr(cli.codex_state, "scan",
                        lambda now, path=None, sweep=True: ({}, 0))
    lines = cli._codex_hook_status()
    assert any("trust" in ln.lower() for ln in lines)


def test_status_reports_live_codex_sessions(monkeypatch):
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})
    monkeypatch.setattr(cli.codex_state, "scan",
                        lambda now, path=None, sweep=True: (
                            {"a": "running", "b": "waiting"}, 0))
    lines = cli._codex_hook_status()
    assert any("2" in ln for ln in lines)


def test_status_does_not_blame_the_trust_prompt_for_slots_it_cannot_read(
        monkeypatch):
    """Two different faults with two different repairs. Reporting an
    unreadable directory as 'never written anything' sends someone to Codex's
    trust prompt to fix a permissions problem, and it will not be there."""
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})

    def boom(now, path=None, sweep=True):
        raise OSError("permission denied")

    monkeypatch.setattr(cli.codex_state, "scan", boom)
    lines = cli._codex_hook_status()
    assert any("permission denied" in ln for ln in lines)
    assert not any("trust" in ln.lower() for ln in lines)


def test_status_says_nothing_about_codex_hooks_without_codex(tmp_path, capsys):
    """No Codex log and no registration of ours: telling someone to install a
    hook for a tool they do not have is worse than saying nothing."""
    assert cli.main(["status"]) == 0
    assert "Codex hook" not in capsys.readouterr().out


def test_status_does_not_sweep_the_codex_slots(tmp_path):
    """A diagnostic that deletes what it is diagnosing destroys the evidence
    somebody ran it to see -- and here that is not an abstraction: the Codex
    hook status is read off exactly the slots the sweep collects, so a status
    run could manufacture the 'never written anything' finding that the next
    status run then reports."""
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "09" / "04"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-09-04T00-00-00-abc.jsonl").write_text("")
    slots = tmp_path / ".blink" / "state-codex"
    slots.mkdir(parents=True)
    # Older than claude_state.ABANDONED_AFTER_S, so a sweep would collect it.
    slot = slots / "sess-old.state"
    slot.write_text(json.dumps({"event": "Stop", "t": time.time() - 7200}))

    assert cli.main(["status"]) == 0

    assert slot.exists(), "`blink status` swept the slots it was reporting on"


# --- the board running ahead of this app -------------------------------------
#
# The line fires on a comparison against THIS APP, so it also fires when the app
# is already the newest thing published -- every developer's desk from the first
# local build onwards. `blink status` does not touch the network on purpose (it
# has to work on a plane, and it is the first thing anyone runs when nothing
# works), so it cannot tell those apart and must not pretend to.

def _board_lines_for(fw):
    from pc.cli import board_lines
    return "\n".join(board_lines({"port": "/dev/x", "board_id": "abc",
                                  "fw": fw},
                                 [("/dev/x", "CH340")], (), "blink"))


def test_a_board_ahead_names_both_possibilities():
    from pc.version import RELEASE_VERSION
    a, b, c = RELEASE_VERSION.split(".")
    out = _board_lines_for(f"{a}.{b}.{int(c) + 1}")
    assert "they ship together" in out
    assert "blink update" in out
    # The half that was missing: the command can find nothing, and then the
    # board is simply ahead of the feed rather than the app being behind it.
    assert "ahead of the published release" in out


def test_a_matched_pair_says_none_of_it():
    from pc.version import RELEASE_VERSION
    out = _board_lines_for(RELEASE_VERSION)
    assert "they ship together" not in out
    assert "ahead of the published release" not in out
