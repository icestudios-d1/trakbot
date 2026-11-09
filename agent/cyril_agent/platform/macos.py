"""macOS specifics: the foreground app and window title, the address of the page in a browser, and the status of
the permissions the agent needs.

Permissions (macOS asks the person, an installer cannot grant them without an MDM profile):
- Screen Recording: needed for screenshots AND to read other apps' window titles (Quartz).
- Automation (per browser): needed to read the page address. macOS asks the first time ("cyril wants to control
  Google Chrome"); the answer is remembered.
Nothing here needs Accessibility.
"""
import ctypes
import re
import subprocess

# Scriptable browsers. Chromium-based ones expose the window mode ("normal"/"incognito"); Safari does not script
# private windows at all (the call fails, which we treat as "address unreadable").
CHROMIUM_APPS = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Vivaldi", "Opera", "Chromium", "Arc")


def is_scriptable_browser(app: str) -> bool:
    return (app or "") in CHROMIUM_APPS or (app or "") == "Safari"


def browser_script(app: str) -> str:
    if app == "Safari":
        return ('tell application "Safari" to if (count of windows) > 0 then return "normal||" & (URL of current tab of front window)')
    return (f'tell application "{app}" to if (count of windows) > 0 then return (mode of front window) & "||" & '
            f'(URL of active tab of front window)')


def parse_browser_reply(text: str) -> tuple[str, bool]:
    """'normal||https://a.com/x' -> ('https://a.com/x', False); 'incognito||...' -> ('', True) (never keep a private URL)."""
    mode, _, url = (text or "").strip().partition("||")
    if mode.strip().lower() == "incognito":
        return "", True
    return url.strip(), False


def browser_address(app: str) -> tuple[str, bool]:
    """(url, is_private) for the front window of `app`. ('', False) if it cannot be read (no window, permission
    not granted yet, browser not scriptable)."""
    if not is_scriptable_browser(app):
        return "", False
    try:
        r = subprocess.run(["osascript", "-e", browser_script(app)], capture_output=True, text=True, timeout=4)
    except (OSError, subprocess.TimeoutExpired):
        return "", False
    return parse_browser_reply(r.stdout) if r.returncode == 0 else ("", False)


def parse_lsappinfo_pid(text: str) -> int | None:
    """'"pid"=1234' (lsappinfo info -only pid ...) -> 1234; None if there is no pid in it."""
    m = re.search(r'"?pid"?\s*=\s*(\d+)', text or "")
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def front_pid_lsappinfo() -> int | None:
    """Process id of the app in front, asked fresh from the system every time (`lsappinfo`). Unlike NSWorkspace,
    this cannot go stale in a background process."""
    try:
        asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        if not asn or "ASN" not in asn:
            return None
        out = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_lsappinfo_pid(out)


def _pump_run_loop() -> None:
    """NSWorkspace only learns that another app came to the front through notifications delivered by the run loop.
    The agent has no run loop of its own, so without this frontmostApplication() keeps returning whatever was in
    front when the agent started (after a fresh install that is System Settings, which the installer opens)."""
    try:
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.01))
    except Exception:
        pass


def frontmost_window() -> dict | None:
    """{'app','title'} of the foreground window via Cocoa/Quartz (needs Screen Recording for the title)."""
    from AppKit import NSRunningApplication, NSWorkspace
    from Quartz import CGWindowListCopyWindowInfo, kCGNullWindowID, kCGWindowListOptionOnScreenOnly
    front, pid = None, front_pid_lsappinfo()
    if pid is not None:
        front = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if front is None:
        _pump_run_loop()
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
    if front is None:
        return None
    pid, title = front.processIdentifier(), ""
    for w in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID) or []:   # front to back
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer", 0) == 0 and w.get("kCGWindowName"):
            title = str(w["kCGWindowName"])
            break
    return {"app": str(front.localizedName() or "unknown"), "title": title}


def _cg():
    return ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")


def screen_recording_granted() -> bool | None:
    try:
        f = _cg().CGPreflightScreenCaptureAccess
        f.restype = ctypes.c_bool
        return bool(f())
    except Exception:
        return None   # macOS older than 10.15: no such permission


def request_screen_recording() -> None:
    """Shows macOS's one-time 'would like to record your screen' prompt (does nothing if already decided)."""
    try:
        f = _cg().CGRequestScreenCaptureAccess
        f.restype = ctypes.c_bool
        f()
    except Exception:
        pass
