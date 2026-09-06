"""Windows: the USB-serial chip on the board needs a driver Windows omits.

Every CYD ships a WCH CH340. macOS and Linux carry a driver for it in the
kernel; Windows does not, and never has. So a customer on Windows plugs the
board in and gets a device with a yellow triangle in Device Manager, no COM
port, and -- until this module existed -- `blink status` saying "Board not
plugged in", which is the one thing that was definitely not true. That wording
sent a customer looking for a bad cable (2026-09-06).

The reason status could not tell the difference is that everything else here
enumerates SERIAL PORTS, through pyserial. A chip with no driver never becomes
a serial port, so from pyserial's side an undriven board and an empty desk are
the same empty list. Windows still knows about the device -- that is what the
triangle is -- just not through any interface that lists COM ports. This
module asks Windows directly, through the same configuration manager Device
Manager itself uses, and then installs the driver.

Two halves, and they are deliberately separable:

  - Detection is free. It needs no administrator, no bundled payload and no
    network, so it runs on every `blink status` on Windows and costs a
    customer nothing. A build carrying no driver still tells them exactly
    what is wrong and what to install.

  - Installation needs both an administrator -- Windows permits no other kind
    of driver install -- and the driver package itself, which is staged at
    BUILD time by tools/fetch_ch340_driver.sh rather than committed here.
    See that script for why the bytes are not in this repository.

Everything below is import-safe on macOS and Linux: the ctypes handles are
opened inside the functions that need them, never at import, so pc/cli.py can
import this unconditionally.
"""
import os
import re
import subprocess
import sys
from collections import namedtuple

from pc import ota


# The USB-serial bridges whose driver Windows does not carry.
#
# NOT the whole of claude_usage_bridge.KNOWN_USB_SERIAL. Windows has shipped
# an in-box driver for the FTDI FT232R and the Silicon Labs CP210x for years,
# and an ESP32-S3's native USB is a standard CDC device -- offering to install
# a WCH driver for any of those would be wrong, and would fail. Only the WCH
# parts belong here.
NEEDS_WCH_DRIVER = frozenset({
    (0x1A86, 0x7523),   # CH340/CH341 -- what every CYD on this desk carries
    (0x1A86, 0x7522),
    (0x1A86, 0x5523),
})

# The .inf inside the WCH package. Also the string this module looks for in
# `pnputil /enum-drivers` to decide whether the work is already done: pnputil
# translates its LABELS into the machine's language, but the value beside
# "Original Name" is a file name, and a file name is not translated.
INF_NAME = "ch341ser.inf"

# Where the driver lives: inside the bundle when frozen, under the repository
# root when running from a checkout. tools/build_binary.sh puts it in the
# first, tools/fetch_ch340_driver.sh in the second.
BUNDLE_SUBDIR = ("drivers", "ch341ser")
VENDOR_SUBDIR = ("vendor", "ch341ser")

# Where a person goes when this build carries no driver.
DRIVER_PAGE = "https://www.wch-ic.com/downloads/CH341SER_EXE.html"

# CM_PROB_* from cfgmgr32.h, in the words a customer needs rather than the
# words Microsoft uses. Only the codes an undriven or misbehaving USB-serial
# device actually produces; anything else is reported by number, which is
# still enough to carry a support conversation.
PROBLEM_NOTES = {
    1:  "Windows has no configuration for it",
    10: "Windows could not start it",
    14: "it needs the machine restarted",
    18: "its driver needs reinstalling",
    19: "its registry entry is damaged",
    22: "it has been disabled in Device Manager",
    28: "Windows has no driver for it",
    31: "Windows could not load its driver",
    37: "its driver failed to start",
    43: "Windows stopped it after a fault",
}
# The plain missing-driver case.
CM_PROB_FAILED_INSTALL = 28

# No problem code at all -- which, measured on the Windows 10 release desk
# (19045, 2026-09-06), is what a CH340 with no driver actually looks like.
#
# This overturned the first version of this module. The assumption was that
# an undriven board carries CM_PROB_FAILED_INSTALL, because Device Manager
# draws a yellow triangle on it. It does not. With the driver package deleted
# and the machine rebooted so the board enumerated fresh, Windows reported
# problem 0 on the device, and `pnputil /enum-devices /problem` reported "No
# devices were found on the system" -- while there was no COM port and no way
# to reach the board. Windows does not consider a device with no function
# driver to be in a problem state. It is simply a device that does nothing.
#
# So the problem code is not the signal. The signal is that no driver is
# BOUND: the devnode has no Service. The problem codes below still matter --
# a device really can be disabled, or fail its install -- but they are the
# rarer half, and keying on them alone meant the detector reported a healthy
# desk while the board sat there unreachable.
NO_DRIVER_BOUND = 0

# The states that installing a driver can actually fix, and therefore the
# ones `blink driver` is offered for. A disabled device (22), a damaged
# registry entry (19) or a pending restart (14) are none of this program's
# business, and sending somebody round that loop wastes a support pass.
DRIVER_FIXES = frozenset({
    NO_DRIVER_BOUND,
    CM_PROB_FAILED_INSTALL,
    18,     # its driver needs reinstalling
    31,     # Windows could not load its driver
    37,     # its driver failed to start
})

_DN_HAS_PROBLEM = 0x00000400
# CM_DRP_* from cfgmgr32.h. SERVICE is the name of the kernel service driving
# the device -- "CH341SER_A64" once WCH's driver is bound, and absent
# entirely when nothing is.
_CM_DRP_SERVICE = 0x00000005
_CM_DRP_DEVICEDESC = 0x00000001
_CR_BUFFER_SMALL = 0x0000001A
_CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
_CM_GETIDLIST_FILTER_PRESENT = 0x00000100
_CR_SUCCESS = 0

# pnputil's "the driver is in, but the machine has to be restarted before
# Windows will use it". Not a failure, and it must not be reported as one.
_ERROR_SUCCESS_REBOOT_REQUIRED = 3010
# What ShellExecuteEx reports when somebody clicks No on the Windows
# permission prompt. Also not a failure of ours.
_ERROR_CANCELLED = 1223

INSTALL_TIMEOUT_S = 180


Undriven = namedtuple("Undriven", "instance_id vid pid problem note")


# ---------------------------------------------------------------- pure parts
#
# Everything in this section is ordinary Python over strings, separated out
# because it is the part that can be tested away from Windows. The ctypes and
# pnputil halves below can only be exercised on a Windows machine, so a bug in
# how a device id is read, or in how pnputil's output is understood, would
# otherwise be discoverable only on a customer's desk.

# Case-insensitive throughout, prefix included. Windows writes these ids in
# upper case and has for as long as anyone has looked, but the cost of not
# depending on that is one flag, and the cost of depending on it wrongly is a
# board that is never recognised on somebody's machine.
_VID_PID = re.compile(r"VID_([0-9A-F]{4})&PID_([0-9A-F]{4})", re.IGNORECASE)


def ids_from(instance_id):
    """(vid, pid) out of a device instance id, or None.

    A USB instance id looks like USB\\VID_1A86&PID_7523\\5&1D2B3C4&0&2. The
    same VID_/PID_ pair also appears in the ids of a composite device's
    children, which is harmless: they carry the same numbers, and the caller
    de-duplicates on the whole id.
    """
    m = _VID_PID.search(instance_id or "")
    if not m:
        return None
    return int(m.group(1), 16), int(m.group(2), 16)


def wants_wch_driver(instance_id):
    """Is this device one of the chips Windows has no driver for?"""
    return ids_from(instance_id) in NEEDS_WCH_DRIVER


def problem_note(code):
    """A customer-facing reason for a CM_PROB_* code.

    Code 0 is not "fine" here. It is only ever passed in for a device that
    has no driver bound -- see NO_DRIVER_BOUND -- and to the person looking
    at the board that is indistinguishable from Windows having no driver,
    because it is.
    """
    if code == NO_DRIVER_BOUND:
        return PROBLEM_NOTES[CM_PROB_FAILED_INSTALL]
    return PROBLEM_NOTES.get(code, f"Windows reported problem code {code}")


def store_dir_has_driver(names):
    """Is the package among these DriverStore\\FileRepository entries?

    Windows names each staged package's directory <inf>_<arch>_<hash>, e.g.
    ch341ser.inf_amd64_9a1f0b3c2d4e5f60. Only the .inf's name is stable
    across versions and architectures, so only it is matched, and it is
    matched as a prefix so that some other package merely mentioning the
    string cannot be mistaken for this one.
    """
    prefix = INF_NAME + "_"
    return any((n or "").lower().startswith(prefix) for n in names)


def store_has_driver(enum_drivers_output):
    """Same question, read out of `pnputil /enum-drivers` output.

    The fallback for a machine whose driver store directory cannot be
    listed. Matches on the .inf's original name rather than on a label: the
    labels are localised, and the Windows desk this product is tested on runs
    a non-English profile -- a check keyed to the English "Original Name:"
    would have quietly reported "not installed" there forever.
    """
    return INF_NAME in (enum_drivers_output or "").lower()


def install_message(status, note=""):
    """The one line `blink install`, `blink driver` and support advice share.

    A pure function so the wording is pinned by a test, rather than by
    whichever of the three call sites someone happened to read.
    """
    if status == "ok":
        return "already installed"
    if status == "installed":
        return "installed" + (f" -- {note}" if note else "")
    if status == "not-windows":
        return "not needed on this system"
    if status == "absent":
        return ("this build carries no driver -- install it by hand from "
                + DRIVER_PAGE)
    if status == "declined":
        return ("skipped -- the Windows permission prompt was declined."
                " Run `blink driver` and choose Yes to finish setting up"
                " the board")
    return f"failed ({note or 'no reason given'})"


def summary(undriven):
    """What is wrong, in one clause, or "" when nothing is.

    No label and no indent: the callers -- `blink status`, the bridge log --
    each have their own column layout, and formatting belongs to them.
    """
    if not undriven:
        return ""
    what = ("a board is" if len(undriven) == 1
            else f"{len(undriven)} boards are")
    return f"{what} plugged in, but {undriven[0].note}"


def as_sentence(text):
    """`text` with its first letter raised, for a caller that starts a line.

    NOT str.capitalize(), which lower-cases everything after the first
    character and turned "Windows has no driver for it" into "windows has no
    driver for it" on the release desk -- a mangled brand name in the first
    sentence a customer with a broken board ever reads.
    """
    if not text:
        return ""
    return text[0].upper() + text[1:]


def advice(undriven, blink_cmd="blink"):
    """What to do about it: zero or one line, unindented.

    Only the plain missing-driver case gets pointed at `blink driver`.
    Nothing here re-enables a device somebody disabled on purpose or repairs
    a damaged registry entry, and telling a customer to run a command that
    cannot fix their problem wastes a support round trip.
    """
    if not undriven:
        return []
    if undriven[0].problem in DRIVER_FIXES:
        return [f"run `{blink_cmd} driver` to install it"]
    return ["open Device Manager to see what it says about that device"]


# --------------------------------------------------------------- the windows
#
# Below here nothing runs anywhere but Windows. Every entry point answers
# empty, or "not Windows", elsewhere rather than raising, so callers need no
# platform test of their own.

def _cfgmgr():
    import ctypes
    return ctypes.WinDLL("cfgmgr32", use_last_error=True)


def _present_usb_ids(cm):
    """Every USB device instance id Windows has attached right now.

    CM_Get_Device_ID_List with the PRESENT filter -- present meaning the
    device is physically here, which is exactly the question. A board plugged
    in last week still has registry entries and must not count.
    """
    import ctypes
    from ctypes import wintypes

    flags = _CM_GETIDLIST_FILTER_ENUMERATOR | _CM_GETIDLIST_FILTER_PRESENT
    enumerator = ctypes.c_wchar_p("USB")
    size = wintypes.ULONG(0)
    if cm.CM_Get_Device_ID_List_SizeW(
            ctypes.byref(size), enumerator, flags) != _CR_SUCCESS:
        return []
    if size.value <= 1:
        return []
    buf = ctypes.create_unicode_buffer(size.value)
    if cm.CM_Get_Device_ID_ListW(
            enumerator, buf, size.value, flags) != _CR_SUCCESS:
        return []
    # A REG_MULTI_SZ: one NUL between entries, two at the end. Read as a
    # slice rather than with .value, which stops at the first NUL and would
    # hand back only the first device on the machine.
    return [s for s in buf[:size.value].split("\0") if s]


def _locate(cm, instance_id):
    """The devnode handle for an instance id, or None.

    None is a race -- the device was unplugged between the listing and this
    call -- and not worth saying anything about.
    """
    import ctypes
    from ctypes import wintypes

    devinst = wintypes.DWORD(0)
    if cm.CM_Locate_DevNodeW(ctypes.byref(devinst),
                             ctypes.c_wchar_p(instance_id), 0) != _CR_SUCCESS:
        return None
    return devinst


def _problem_code(cm, devinst):
    """0 if Windows flags no problem on this device, its CM_PROB_* code if it
    does. Remember that 0 does NOT mean the device works -- see
    NO_DRIVER_BOUND."""
    import ctypes
    from ctypes import wintypes

    status = wintypes.ULONG(0)
    problem = wintypes.ULONG(0)
    if cm.CM_Get_DevNode_Status(ctypes.byref(status), ctypes.byref(problem),
                                devinst, 0) != _CR_SUCCESS:
        return None
    if not (status.value & _DN_HAS_PROBLEM):
        return 0
    return problem.value


def _devnode_string(cm, devinst, prop):
    """One string registry property of a devnode, or "" if it has none.

    "" is the answer that matters: a device with no function driver has no
    CM_DRP_SERVICE at all, and that -- not a problem code -- is how an
    undriven board is actually recognised.
    """
    import ctypes
    from ctypes import wintypes

    size = wintypes.ULONG(0)
    rc = cm.CM_Get_DevNode_Registry_PropertyW(
        devinst, prop, None, None, ctypes.byref(size), 0)
    if rc != _CR_BUFFER_SMALL or size.value == 0:
        return ""
    buf = ctypes.create_unicode_buffer(size.value // 2 + 1)
    if cm.CM_Get_DevNode_Registry_PropertyW(
            devinst, prop, None, buf, ctypes.byref(size), 0) != _CR_SUCCESS:
        return ""
    return buf.value or ""


def undriven_boards():
    """Present WCH bridges Windows cannot use, listed.

    A board counts as undriven when either is true:

      - Windows flags a problem on it (disabled, failed install, and the rest
        of PROBLEM_NOTES), or
      - no driver is BOUND to it -- it has no Service -- which is the state a
        board is in on a machine that has never had the driver, and the state
        that carries no problem code at all. See NO_DRIVER_BOUND for what was
        measured on the release desk, and for why the first version of this
        function reported that machine as healthy.

    Empty everywhere but Windows, and empty on a Windows machine whose driver
    is installed: a bound CH340 has a Service, no problem flag, and a COM
    port that shows up through pyserial like any other.

    Never raises. This runs on every `blink status`, and a status command that
    dies because a system library moved is worse than one that omits a line.
    """
    if sys.platform != "win32":
        return []
    try:
        cm = _cfgmgr()
        found = []
        for instance_id in _present_usb_ids(cm):
            if not wants_wch_driver(instance_id):
                continue
            devinst = _locate(cm, instance_id)
            if devinst is None:
                continue
            code = _problem_code(cm, devinst)
            if code is None:
                continue
            if not code and _devnode_string(cm, devinst, _CM_DRP_SERVICE):
                continue        # bound to a driver and Windows is happy: fine
            vid, pid = ids_from(instance_id)
            found.append(Undriven(instance_id, vid, pid, code,
                                  problem_note(code)))
        return found
    except Exception:
        return []


def is_elevated():
    """Is this process already running as an administrator?"""
    if sys.platform != "win32":
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _pnputil(args, timeout=60):
    """Run pnputil, or None if it could not be run at all.

    errors="replace" because pnputil writes in the console code page, which
    on the non-ASCII Windows profile this is tested against is not UTF-8 --
    and a UnicodeDecodeError here would look exactly like a missing driver.
    """
    try:
        return subprocess.run(["pnputil"] + list(args), capture_output=True,
                              text=True, errors="replace", timeout=timeout,
                              **ota.NO_WINDOW)
    except Exception:
        return None


def driver_store_dir():
    root = os.environ.get("SystemRoot") or "C:\\Windows"
    return os.path.join(root, "System32", "DriverStore", "FileRepository")


def driver_in_store():
    """Has the driver already been staged? None when nothing could say.

    Answered from the driver store's own directory, which any user can list,
    and NOT from `pnputil /enum-drivers`, which wants an administrator on
    current Windows builds. That distinction is the whole behaviour of this
    feature: a check that needed elevation to find out whether elevation was
    needed would raise the Windows permission prompt on every single install,
    on every machine, including the ones with nothing to do -- which is
    exactly the "click Yes without reading" habit this is trying not to
    teach. pnputil stays as a fallback for a machine whose store directory
    cannot be listed at all.
    """
    try:
        return store_dir_has_driver(os.listdir(driver_store_dir()))
    except OSError:
        pass
    proc = _pnputil(["/enum-drivers"])
    if proc is None or proc.returncode != 0:
        return None
    return store_has_driver(proc.stdout)


def driver_package():
    """The bundled .inf to install, or None when this build carries none.

    A build with no driver is a supported state, not a broken one: detection
    and the manual instructions work without it, and it is what every build
    produces until tools/fetch_ch340_driver.sh has been run.
    """
    for base in _bundle_roots():
        inf = _inf_in(os.path.join(base, *BUNDLE_SUBDIR))
        if inf:
            return inf
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _inf_in(os.path.join(repo, *VENDOR_SUBDIR))


def _inf_in(directory):
    """The package's .inf inside `directory`, whatever case it is written in.

    WCH ships the file as CH341SER.INF, in capitals, and it stays in capitals
    through the driver store. Windows would not care -- its paths are
    case-insensitive -- but matching by listing rather than by os.path.exists
    means the same code finds it if a copy ever arrives through something
    that is case-sensitive, which is every archive tool and every checkout on
    the two platforms this is cross-built from.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    for name in names:
        if name.lower() == INF_NAME:
            return os.path.join(directory, name)
    return None


def _bundle_roots():
    if getattr(sys, "frozen", False):
        yield getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))


def _elevated_run(exe, params, timeout_s):
    """Run one command as administrator and wait for it. (rc, error_code).

    ShellExecuteEx with the `runas` verb, which is the documented way to ask
    for the Windows permission prompt. Note what gets elevated: pnputil, with
    the arguments below, and nothing else. Re-launching THIS program elevated
    would hand administrator rights to the whole install -- the service
    registration, the settings file, the copy into ~/.blink -- and every one
    of those is meant to belong to the person who ran it.
    """
    import ctypes
    from ctypes import wintypes

    SEE_MASK_NOCLOSEPROCESS = 0x00000040
    SEE_MASK_NOASYNC = 0x00000100       # we do not pump a message loop
    SW_HIDE = 0

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("fMask", ctypes.c_ulong),
                    ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR),
                    ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int),
                    ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR),
                    ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD),
                    ("hIcon", wintypes.HANDLE),
                    ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = exe
    info.lpParameters = params
    info.nShow = SW_HIDE

    # use_last_error=True, and NOT ctypes.windll.shell32. The cached handles
    # under ctypes.windll are loaded without it, so ctypes.get_last_error()
    # against them returns whatever was there before -- and the one error this
    # has to read correctly is 1223, somebody clicking No on the permission
    # prompt, which must never be reported to them as a failure.
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        return None, ctypes.get_last_error()
    handle = info.hProcess
    if not handle:
        # Asked for with SEE_MASK_NOCLOSEPROCESS, so this should not happen --
        # but waiting on a null handle would block for the full timeout, and
        # "no handle" is not evidence that pnputil ran.
        return None, 0
    try:
        kernel32.WaitForSingleObject(handle, int(timeout_s * 1000))
        rc = wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(rc)):
            return None, 0
        return rc.value, 0
    finally:
        kernel32.CloseHandle(handle)


def install_driver(inf_path=None):
    """Put the CH340 driver in Windows' driver store. (status, note).

    `pnputil /add-driver <inf> /install` rather than running WCH's own setup
    .exe: the .exe opens a window with a button on it, which is not something
    an installer can drive, and it is the only part of this that would have
    needed a person sitting there.

    /install also binds the driver to anything already plugged in, so a
    customer who runs this with the board attached does not have to unplug it
    -- while staging it in the store means a board plugged in LATER is picked
    up with no prompt at all, which is the plug-and-play this is for.
    """
    if sys.platform != "win32":
        return "not-windows", ""
    inf = inf_path or driver_package()
    if not inf:
        return "absent", ""

    args = ["/add-driver", inf, "/install"]
    if is_elevated():
        proc = _pnputil(args, timeout=INSTALL_TIMEOUT_S)
        if proc is None:
            return "failed", "pnputil could not be run"
        rc, err = proc.returncode, 0
    else:
        rc, err = _elevated_run("pnputil.exe", subprocess.list2cmdline(args),
                                INSTALL_TIMEOUT_S)

    if err == _ERROR_CANCELLED:
        return "declined", ""
    if rc is None:
        return "failed", f"the permission prompt failed (error {err})"
    if rc == 0:
        return "installed", ""
    if rc == _ERROR_SUCCESS_REBOOT_REQUIRED:
        return "installed", "restart the machine to finish"
    return "failed", f"pnputil exited {rc}"


def ensure_driver(force=False):
    """The whole job, for `blink install` and `blink driver`. (status, line).

    Idempotent, and quiet on a machine that is already set up: if the driver
    is in the store there is nothing to do and -- this is the part that
    matters -- no Windows permission prompt is raised. An installer that asked
    for administrator every time it ran would teach people to click Yes
    without reading, which is a worse habit than one missing driver.
    """
    if sys.platform != "win32":
        return "not-windows", install_message("not-windows")
    if not force and driver_in_store():
        return "ok", install_message("ok")
    status, note = install_driver()
    return status, install_message(status, note)
