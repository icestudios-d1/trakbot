"""Read the foreground Chromium browser's address bar through Windows UI Automation.

Why: the URL otherwise only comes from the companion browser extension, and an extension cannot be installed
silently on PCs that are not domain/MDM managed. UI Automation needs nothing installed in the browser, works for
Edge/Chrome/Brave/Opera/Vivaldi, and reads what is on screen (the same thing a screenshot shows).
"""
_auto = None


def _load():
    global _auto
    if _auto is None:
        import uiautomation as auto   # heavy import (COM): only done on Windows, once
        _auto = auto
    return _auto


def read_address_bar(hwnd: int) -> str | None:
    """Raw text of the browser's address box, or None if there is no such box on this window."""
    auto = _load()
    window = auto.ControlFromHandle(hwnd)
    # "OmniboxViewViews" is the Chromium address box class (Chrome, Edge, Brave, Opera, Vivaldi)
    box = window.EditControl(ClassName="OmniboxViewViews")
    if not box.Exists(1.0, 0.1):
        return None
    return box.GetValuePattern().Value
