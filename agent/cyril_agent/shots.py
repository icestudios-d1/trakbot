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
    """True once `app` has held the foreground continuously for >= `after` seconds, then at most once every
    `every` seconds after that. Private/incognito windows hide their URL from us, so the screen is the only
    record of what was viewed: they get a quicker first shot and a tighter cadence."""

    def __init__(self, after: float = 60.0, every: float = 60.0, private_after: float = 10.0, private_every: float = 60.0):
        self.after, self.every = after, every
        self.private_after, self.private_every = private_after, private_every
        self.key = None
        self.app_since = 0.0
        self.last_shot = float("-inf")  # -inf = no shot taken yet for this window

    def tick(self, now: float, app: str, idle: bool, private: bool = False) -> bool:
        if idle or not app:
            self.key = None
            return False
        key = (app, private)
        if key != self.key:
            self.key, self.app_since, self.last_shot = key, now, float("-inf")
        after, every = (self.private_after, self.private_every) if private else (self.after, self.every)
        held = now - self.app_since
        if held >= after and now - self.last_shot >= every:
            self.last_shot = now
            return True
        return False
