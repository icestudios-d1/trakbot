"""Foreground window + idle time, per OS. Window helpers derive from
ActivityWatch's aw-watcher-window (MPL-2.0)."""
import ctypes
import os
import subprocess
import sys


def _linux_window():
    from . import xprop
    wid = xprop.get_active_window_id()
    if wid in ("0x0", None):
        return None
    w = xprop.get_window(wid, True)
    cls = w["class"]
    return {"app": (cls[-1] if cls else "unknown"), "title": w["name"]}


def _win_window():
    from . import windows_win32 as w
    hwnd = w.get_active_window_handle()
    if not hwnd:
        return None
    try:
        app = w.get_app_name(hwnd)
    except Exception:
        try:
            app = w.get_app_name_wmi(hwnd)
        except Exception:
            app = None
    return {"app": app or "unknown", "title": w.get_window_title(hwnd) or ""}


_MAC = '''tell application "System Events"
 set p to first application process whose frontmost is true
 set n to name of p
 try
  set t to name of front window of p
 on error
  set t to ""
 end try
end tell
return n & "||" & t'''


def _mac_window():
    out = subprocess.run(["osascript", "-e", _MAC], capture_output=True, text=True, timeout=5).stdout.strip()
    app, _, title = out.partition("||")
    return {"app": app or "unknown", "title": title} if out else None


def get_active_window():
    try:
        if sys.platform.startswith("linux"):
            return _linux_window()
        if sys.platform == "win32":
            return _win_window()
        if sys.platform == "darwin":
            return _mac_window()
    except Exception:
        return None
    return None


def idle_seconds() -> float:
    """Seconds since last keyboard/mouse input (0 if unknown)."""
    try:
        if sys.platform == "win32":
            class LII(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            lii = LII(ctypes.sizeof(LII), 0)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
            return (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                if "HIDIdleTime" in line:
                    return int(line.split("=")[-1]) / 1e9
        elif os.environ.get("DISPLAY"):
            return int(subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5).stdout) / 1000.0
    except Exception:
        pass
    return 0.0
