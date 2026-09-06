"""Which port may be reset, and what `blink status` says about the desk."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from claude_usage_bridge import may_reset_port
from pc.cli import board_lines
from pc.version import RELEASE_VERSION


class TestMayReset:
    def test_an_explicit_port_is_the_operators_call(self):
        assert may_reset_port(True, "COM3", None, False)

    def test_a_stranger_is_never_reset(self):
        assert not may_reset_port(False, "COM4", "COM3", False)
        assert not may_reset_port(False, "COM4", "COM3", True)

    def test_the_remembered_port_waits_for_the_patient_probe(self):
        # A board still booting in its usual socket answers within 11 s;
        # resetting it on the 1.5 s pass is the double boot on plug-in.
        assert not may_reset_port(False, "COM3", "COM3", False)
        assert may_reset_port(False, "COM3", "COM3", True)


class TestBoardLines:
    def test_the_board_is_named_with_what_is_known_about_it(self):
        lines = board_lines({"port": "COM3", "board_id": "abc", "fw": "1.2.2"},
                            [("COM3", "CH340")])
        assert lines == ["Board       COM3 (CH340) -- id abc, firmware 1.2.2"]

    def test_other_boards_are_listed_as_left_alone(self):
        lines = board_lines({"port": "COM3", "board_id": "abc", "fw": None},
                            [("COM3", "CH340"), ("COM7", "CH340"), ("COM9", "FTDI")])
        assert lines[0] == "Board       COM3 (CH340) -- id abc"
        assert "COM7 (CH340), COM9 (FTDI)" in lines[1]
        assert "left alone" in lines[1]

    def test_nothing_identified_yet(self):
        lines = board_lines({}, [("COM7", "CH340")])
        assert lines == ["Board       none identified yet -- looking at COM7 (CH340)"]

    def test_the_board_moved_socket(self):
        lines = board_lines({"port": "/dev/cu.usbserial-14220", "board_id": "abc"},
                            [("/dev/cu.usbserial-14330", "CH340")])
        assert lines[0].startswith("Board       none identified yet")
        assert lines[1] == "            last seen on /dev/cu.usbserial-14220"

    def test_unplugged(self):
        assert board_lines({"port": "COM3"}, []) == \
            ["Board       not plugged in (last seen on COM3)"]
        assert board_lines({}, []) == ["Board       not plugged in"]


class TestBoardAheadOfTheApp:
    """The board running a LATER release than the app on this computer.

    They ship from one tag and install together, so this means something split
    them: a hand-flash, or an app update that failed after the firmware went
    on. The half left behind is the one the panel cannot reach -- the board's
    update row says the app is old and no tap on it can help -- so the only
    place that can name the fix is here.
    """

    def _ahead(self):
        major, minor, patch = (int(n) for n in RELEASE_VERSION.split("."))
        return f"{major + 1}.{minor}.{patch}"

    def test_it_says_so_and_names_the_command(self):
        lines = board_lines({"port": "COM3", "board_id": "abc",
                             "fw": self._ahead()},
                            [("COM3", "CH340")], blink_cmd="blink")
        assert any(RELEASE_VERSION in l and "ship together" in l for l in lines), lines
        assert any(l.strip() == "run: blink update" for l in lines), lines

    def test_it_says_nothing_when_the_halves_match(self):
        lines = board_lines({"port": "COM3", "fw": RELEASE_VERSION},
                            [("COM3", "CH340")])
        assert not any("ship together" in l for l in lines), lines

    def test_it_says_nothing_when_the_board_is_behind(self):
        """The ordinary case, and the one the update row already handles: the
        board can fetch its own firmware with a tap."""
        lines = board_lines({"port": "COM3", "fw": "0.9.0"},
                            [("COM3", "CH340")])
        assert not any("ship together" in l for l in lines), lines

    def test_an_unplugged_board_is_still_reported(self):
        """board.json remembers the firmware, so the mismatch is knowable with
        nothing on the desk -- and that is when someone reads `blink status`."""
        lines = board_lines({"port": "COM3", "fw": self._ahead()}, [])
        assert any("ship together" in l for l in lines), lines

    def test_a_board_that_never_said_its_firmware_says_nothing(self):
        for known in ({"port": "COM3"}, {"port": "COM3", "fw": None},
                      {"port": "COM3", "fw": "not a version"}):
            lines = board_lines(known, [("COM3", "CH340")])
            assert not any("ship together" in l for l in lines), (known, lines)
