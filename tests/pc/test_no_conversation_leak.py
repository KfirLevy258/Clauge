"""Nothing from a conversation store may reach a log, a message, or a fixture.

README.md's "Something's wrong?" section tells customers to email support the tail of ~/.blink/bridge.log
and promises that "nothing in either is a secret". Every source this project
reads has to keep that promise true, and one of them now reads a store made
largely of chat text.

Two axes, on purpose. The runtime tests below drive every module that
touches conversation-adjacent bytes -- pc/desktop_idb.py, pc/cowork_audit.py,
pc/desktop_local_storage.py, and the two substrate modules they read
through, pc/v8_clone.py and pc/leveldb.py -- with a marker string, and check
stdout, stderr AND the logging module (a runtime test only catches a leak on
the path it happens to exercise). The static check at the bottom catches the
failure mode a runtime test cannot: a future engineer adding a helpful
`print(f"could not parse {value!r}")` while debugging, on a line no fixture
here happens to walk.
"""
import ast
import json
import logging
import os

import pytest

from pc import cowork_audit
from pc import desktop_idb
from pc import desktop_local_storage
from pc import leveldb
from pc import v8_clone
from tests.support import leveldb_fixture as fx
from tests.support import v8_fixture as vfx

MARKER = "MARKER-DO-NOT-LEAK"
LS_KEY = (b"_https://claude.ai\x00\x01"
          b"claudeai.ochre_heron_tide.3d2fe603-510b-4256-bd4e-2f2b1b689bef")

# Every module in this plan that reads a store containing, or adjacent to,
# the owner's own conversations. Paths are repo-relative so the static check
# below reads the real file on disk rather than a compiled module object.
SENSITIVE_MODULES = [
    "pc/desktop_idb.py",
    "pc/cowork_audit.py",
    "pc/desktop_local_storage.py",
    "pc/v8_clone.py",
    "pc/leveldb.py",
    # Decoded values rather than raw buffers, so the risk is lower -- but
    # these are the two modules a future debugger reaches for first, which is
    # exactly when a print of "the record" gets added.
    "pc/providers/claude_desktop_ls.py",
    "pc/providers/weekly_anchor.py",
]

_PRIVACY_PROMISE = (
    "README.md's troubleshooting section promises customers that the tail of ~/.blink/bridge.log "
    "holds nothing secret. This module reads a store built largely of the "
    "owner's own conversations (or, for pc/leveldb.py and pc/v8_clone.py, "
    "is the substrate every such module reads through), so this is a real "
    "leak, not a test artifact.")


@pytest.fixture(autouse=True)
def _capture_every_log_level(caplog):
    """caplog defaults to WARNING and above. A leak routed through
    logging.debug or logging.info is exactly as real as one on stderr."""
    caplog.set_level(logging.DEBUG)


def _assert_no_leak(capsys, caplog, label):
    captured = capsys.readouterr()
    assert MARKER not in captured.out, (
        f"{label} wrote the marker to stdout. {_PRIVACY_PROMISE}")
    assert MARKER not in captured.err, (
        f"{label} wrote the marker to stderr. {_PRIVACY_PROMISE}")
    assert MARKER not in caplog.text, (
        f"{label} routed the marker through the logging module -- the "
        f"exact channel a future `logging.debug(f'...{{value!r}}')` would "
        f"use, and one `capsys` alone would never catch. {_PRIVACY_PROMISE}")


# --- pc/desktop_idb.py


def test_reading_the_conversation_store_leaks_nothing(tmp_path, capsys,
                                                        caplog):
    chat = {"messages": [{"text": MARKER}], "created_at": MARKER}
    (tmp_path / "000103.log").write_bytes(fx.build_log(
        [[("put", b"\x00cowork:cse_x", vfx.dumps(chat))]]))
    try:
        desktop_idb.seven_day_reset(str(tmp_path))
    except Exception as exc:            # pragma: no cover - must not happen
        assert MARKER not in str(exc)
        raise
    _assert_no_leak(capsys, caplog,
                     "desktop_idb.seven_day_reset (well-formed record)")


def test_a_corrupt_conversation_store_leaks_nothing(tmp_path, capsys,
                                                      caplog):
    """The failure path is where buffer bytes escape, not the happy one."""
    (tmp_path / "000103.log").write_bytes(
        b"\x00cowork" + MARKER.encode() + b"\xff" * 200)
    desktop_idb.seven_day_reset(str(tmp_path))
    _assert_no_leak(capsys, caplog,
                     "desktop_idb.seven_day_reset (corrupt buffer)")


# --- pc/cowork_audit.py -- not covered before this fix round, and just as
# sensitive: these files sit inside the owner's own Claude session directory.


def test_reading_a_cowork_audit_file_leaks_nothing(tmp_path, capsys, caplog):
    session = tmp_path / "session-1"
    session.mkdir()
    line = json.dumps({
        "type": "rate_limit_event",
        "timestamp": "2026-09-05T12:00:00Z",
        "rate_limit_info": {"unifiedWindows": {
            "seven_day": {"resetsAt": 1788933600.0}}},
        "extra": MARKER})
    (session / "audit.jsonl").write_text(line + "\n", encoding="utf-8")
    cowork_audit.seven_day_reset(str(tmp_path))
    _assert_no_leak(capsys, caplog,
                     "cowork_audit.seven_day_reset (well-formed line)")


def test_a_corrupt_cowork_audit_line_leaks_nothing(tmp_path, capsys, caplog):
    """Same reasoning as the IndexedDB case: the failure path is where a
    line fragment or a decode error is most likely to escape."""
    session = tmp_path / "session-1"
    session.mkdir()
    (session / "audit.jsonl").write_text(
        "unifiedWindows " + MARKER + " { not actually json\n",
        encoding="utf-8")
    cowork_audit.seven_day_reset(str(tmp_path))
    _assert_no_leak(capsys, caplog,
                     "cowork_audit.seven_day_reset (unparseable line)")


# --- pc/desktop_local_storage.py


def test_reading_local_storage_leaks_nothing(tmp_path, capsys, caplog):
    (tmp_path / "000001.log").write_bytes(fx.build_log(
        [[("put", LS_KEY, b"\x01" + MARKER.encode("utf-8"))]]))
    desktop_local_storage.newest_record(str(tmp_path))
    _assert_no_leak(capsys, caplog, "desktop_local_storage.newest_record")


# --- pc/v8_clone.py -- the substrate desktop_idb reads through. Driven
# directly, not only indirectly through desktop_idb, so a leak introduced
# here is caught even if desktop_idb's own callers still happen to hide it.


def test_v8_clone_parse_leaks_nothing_on_a_well_formed_value(capsys, caplog):
    v8_clone.parse(vfx.dumps({"note": MARKER}))
    _assert_no_leak(capsys, caplog, "v8_clone.parse (well-formed value)")


def test_v8_clone_parse_leaks_nothing_on_a_truncated_buffer(capsys, caplog):
    v8_clone.parse(b"\xff\x0f" + MARKER.encode() + b"\xff" * 3)
    _assert_no_leak(capsys, caplog, "v8_clone.parse (truncated buffer)")


# --- pc/leveldb.py -- the substrate both desktop_idb and
# desktop_local_storage read through.


def test_leveldb_scan_leaks_nothing_on_a_corrupt_log(tmp_path, capsys,
                                                       caplog):
    (tmp_path / "000001.log").write_bytes(
        b"\x00\x00\x00\x00" + MARKER.encode() + b"\xff" * 100)
    leveldb.scan(str(tmp_path), lambda k: True)
    leveldb.scan_all(str(tmp_path), lambda k: True)
    _assert_no_leak(capsys, caplog, "leveldb.scan / scan_all (corrupt log)")


# --- Fixtures themselves


def test_no_fixture_was_captured_from_a_real_machine():
    """A standing check, not a one-off. A captured fixture would put somebody's
    chat text into a public repository, which is the outcome the whole rule
    exists to prevent."""
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures")
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            body = fh.read()
        assert b"ochre_heron_tide" not in body, name
        assert b"unifiedWindows" not in body, name


# --- Static: the axis that actually catches the failure mode this guard
# exists for. A runtime test only catches a debug print if it happens to
# exercise the exact line it was added to; this catches it always.

_LOGGER_METHODS = {"debug", "info", "warning", "warn", "error",
                    "critical", "exception", "log"}


def _repo_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))


def _static_violations(path):
    """[(lineno, description), ...] for anything in `path` that could carry
    buffer content off the module through print, logging, or an
    interpolated exception message."""
    with open(path, "r", encoding="utf-8") as fh:
        source = fh.read()
    tree = ast.parse(source, filename=path)
    violations = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            violations.append((node.lineno, "a print(...) call"))
        elif (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOGGER_METHODS):
            violations.append((
                node.lineno,
                f"a call to .{node.func.attr}(...), which reads as a "
                "logging call"))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            mod = getattr(node, "module", None)
            names = [alias.name for alias in node.names]
            if mod == "logging" or "logging" in names:
                violations.append(
                    (node.lineno, "an import of the logging module"))
        elif isinstance(node, ast.Raise) and node.exc is not None:
            for sub in ast.walk(node.exc):
                if isinstance(sub, ast.JoinedStr):
                    violations.append(
                        (node.lineno, "an f-string inside a raise"))
                    break
                if isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.Mod):
                    violations.append((
                        node.lineno, "%-style interpolation inside a raise"))
                    break
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "format"):
                    violations.append(
                        (node.lineno, ".format(...) inside a raise"))
                    break
    return violations


@pytest.mark.parametrize("relpath", SENSITIVE_MODULES)
def test_no_print_or_logging_call_in_sensitive_modules(relpath):
    """A future `print(f"could not parse {value!r}")`, added while
    debugging one of these modules, must fail here -- not ship, and not
    wait for a runtime test that happens to walk that exact line."""
    path = os.path.join(_repo_root(), relpath)
    violations = _static_violations(path)
    assert not violations, (
        f"{relpath} line {violations[0][0]} contains "
        f"{violations[0][1]}. {_PRIVACY_PROMISE} A print, a logging call, "
        "or an interpolated exception message in this file is exactly how "
        "that promise breaks. Return the value as data instead -- None on "
        "failure, never a side channel.")
