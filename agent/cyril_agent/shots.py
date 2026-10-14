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
    """True once `app` has held the foreground continuously for >= `after`
    seconds, then at most once every `every` seconds after that."""

    def __init__(self, after: float = 60.0, every: float = 60.0):
        self.after, self.every = after, every
        self.app = None
        self.app_since = 0.0
        self.last_shot = 0.0

    def tick(self, now: float, app: str, idle: bool) -> bool:
        if idle or not app:
            self.app = None
            return False
        if app != self.app:
            self.app, self.app_since, self.last_shot = app, now, 0.0
        held = now - self.app_since
        if held >= self.after and now - self.last_shot >= self.every:
            self.last_shot = now
            return True
        return False
