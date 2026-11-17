"""Screenshot capture, throttled per foreground app.

Captures the whole screen (all monitors) regardless of what's on it — this is
the backstop for content a browser's private-browsing mode deliberately hides
from the agent (it hides page data from the browser's own extension APIs and
from browsing history, not from the OS's screen buffer).
"""
import base64
import io

import mss
from PIL import Image

MAX_WIDTH = 1280
JPEG_QUALITY = 60


MENU_BAR_SHARE = 0.06   # top slice of each display left out of the blank test (menu bar, notch area included)


def _flat(img: Image.Image) -> bool:
    lo, hi = img.convert("L").resize((64, 40)).getextrema()
    return hi - lo <= 6


def looks_blank(img: Image.Image, displays: list[tuple[int, int, int, int]] | None = None) -> bool:
    """True if the picture shows nothing of the screen. That is what macOS hands back while Screen Recording is not
    allowed (or not yet in effect for the running agent): every window, the wallpaper included, is left out and a
    flat dark frame (about RGB 40,39,44) is returned. The menu bar is still drawn, though, so the frame is NOT one
    flat colour: a thin strip of wallpaper-tinted menu bar sits at the top of each display. So each display is
    tested without its top slice. `displays` = (left, top, width, height) of each display within `img`; default:
    the whole picture is one display."""
    if _flat(img):
        return True
    for left, top, w, h in displays or [(0, 0, img.width, img.height)]:
        skip = max(1, int(h * MENU_BAR_SHARE))
        if h - skip < 8 or w < 8 or not _flat(img.crop((left, top + skip, left + w, top + h))):
            return False
    return True


def capture() -> tuple[str, bool]:
    """(JPEG as base64, picture is blank)."""
    with mss.mss() as sct:
        whole = sct.monitors[0]  # index 0 = all monitors combined
        raw = sct.grab(whole)
        mons = sct.monitors[1:]
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    # mss gives display positions in points while the picture is in pixels (2x on Retina): scale them across
    sx, sy = img.width / whole["width"], img.height / whole["height"]
    displays = [(round((d["left"] - whole["left"]) * sx), round((d["top"] - whole["top"]) * sy),
                 round(d["width"] * sx), round(d["height"] * sy)) for d in mons] or None
    blank = looks_blank(img, displays)
    if img.width > MAX_WIDTH:
        h = int(img.height * MAX_WIDTH / img.width)
        img = img.resize((MAX_WIDTH, h))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode(), blank


def capture_jpeg_b64() -> str:
    return capture()[0]


class ScreenshotThrottle:
    """When to take the next screenshot.

    - The app in front keeps changing: one shot every `every` seconds (default 60) so quick switching between,
      say, Edge and Photoshop is seen in detail.
    - The same app stays in front: slow down to one shot every `stable_every` seconds (default 180); a long Photoshop
      session needs a progress check, not a picture a minute.
    - Private/incognito windows hide their address from us, so the screen is the only record of what was viewed:
      first shot after `private_after` seconds, then every `private_every`, regardless of stability.
    Nothing is captured while the user is idle or no program is in front. The first shot after starting (or coming
    back from idle) is taken straight away."""

    def __init__(self, every: float = 60.0, stable_every: float = 180.0, private_after: float = 10.0,
                 private_every: float = 60.0):
        self.every, self.stable_every = every, stable_every
        self.private_after, self.private_every = private_after, private_every
        self.key = None
        self.app_since = 0.0
        self.changed = True               # app switched since the last shot
        self.last_shot = float("-inf")

    def tick(self, now: float, app: str, idle: bool, private: bool = False) -> bool:
        if idle or not app:
            self.key = None
            return False
        key = (app, private)
        if key != self.key:
            self.key, self.app_since, self.changed = key, now, True
        gap = now - self.last_shot
        if private:
            due = now - self.app_since >= self.private_after and gap >= self.private_every
        else:
            due = gap >= (self.every if self.changed else self.stable_every)
        if due:
            self.last_shot, self.changed = now, False
            return True
        return False
