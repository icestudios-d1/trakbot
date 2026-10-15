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


def capture_jpeg_b64() -> str:
    with mss.mss() as sct:
        raw = sct.grab(sct.monitors[0])  # index 0 = all monitors combined
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    if img.width > MAX_WIDTH:
        h = int(img.height * MAX_WIDTH / img.width)
        img = img.resize((MAX_WIDTH, h))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=JPEG_QUALITY)
    return base64.b64encode(buf.getvalue()).decode()


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
