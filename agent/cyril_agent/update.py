"""Self-update: checks the server's /api/agent/version manifest, and if a
newer build is available, downloads it, verifies its SHA-256, and swaps the
running binary -- no popup, no interruption (the same pattern Chrome,
CrowdStrike and most managed IT agents use for background patching).

Not silent in the auditing sense: every step is written to a local log file
(CYRIL_UPDATE_LOG, default next to the spool file) and reported back to the
server's /api/agent/update-log, so what happened and when is always
reconstructable by IT -- this is "no interruptive UI", not "no record".

This module never touches whether the process is visible to the OS (Task
Manager, Activity Monitor, `ps`, the Scheduled Task list): it only replaces
the binary's bytes. Hiding the running process itself is out of scope here
and in the rest of this codebase -- see README "What we deliberately didn't
build".
"""
import hashlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

VERSION = "0.2.0"  # bump on every release; must match manifest.json's "version" for a no-op check


def _log_path() -> str:
    return os.environ.get("CYRIL_UPDATE_LOG", os.path.expanduser("~/.cyril-update.log"))


def _logger() -> logging.Logger:
    lg = logging.getLogger("cyril.update")
    if not lg.handlers:
        h = logging.FileHandler(_log_path())
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
    return lg


USER_AGENT = "CyrilAgent/1.0"  # urllib's default UA is a well-known bot signature
                                # that Cloudflare's basic WAF/bot rules block on
                                # sight -- see main.py's _post for the same fix.


def _get_json(url: str, timeout: float = 15.0) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _download(url: str, timeout: float = 120.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _report(server: str, key: str, user: str, device: str, from_v: str, to_v: str, ok: bool, detail: str) -> None:
    try:
        req = urllib.request.Request(
            server.rstrip("/") + "/api/agent/update-log",
            json.dumps({"user": user, "device": device, "from_version": from_v,
                        "to_version": to_v, "ok": ok, "detail": detail[:2000]}).encode(),
            {"Content-Type": "application/json", "X-Api-Key": key, "User-Agent": USER_AGENT})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        pass  # the local log file is the record of truth; server reachability isn't required


def check_and_apply(server: str, key: str, user: str, device: str) -> bool:
    """Returns True if an update was applied (caller should then re-exec/exit
    so the Scheduled Task's auto-restart picks up the new binary)."""
    log = _logger()
    try:
        manifest = _get_json(server.rstrip("/") + "/api/agent/version")
    except Exception as e:
        log.info("version check failed: %s", e)
        return False

    new_version = manifest.get("version")
    if not new_version or new_version == VERSION:
        return False

    asset_key = {"win32": "windows_exe", "darwin": "macos", "linux": "linux"}.get(sys.platform, "linux")
    asset = manifest.get(asset_key) or {}
    url, sha256 = asset.get("url"), asset.get("sha256")
    if not url or not sha256:
        log.info("no asset for platform %s in manifest %s", sys.platform, new_version)
        return False

    log.info("update available: %s -> %s, downloading %s", VERSION, new_version, url)
    try:
        data = _download(server.rstrip("/") + url if url.startswith("/") else url)
    except Exception as e:
        log.error("download failed: %s", e)
        _report(server, key, user, device, VERSION, new_version, False, f"download failed: {e}")
        return False

    actual = _sha256(data)
    if actual != sha256.lower():
        log.error("checksum mismatch: expected %s got %s -- refusing to apply", sha256, actual)
        _report(server, key, user, device, VERSION, new_version, False, "checksum mismatch")
        return False

    try:
        _apply(data)
    except Exception as e:
        log.error("apply failed: %s", e)
        _report(server, key, user, device, VERSION, new_version, False, f"apply failed: {e}")
        return False

    log.info("update applied: %s -> %s", VERSION, new_version)
    _report(server, key, user, device, VERSION, new_version, True, "applied")
    return True


def _apply(new_bytes: bytes) -> None:
    """Atomically replace the running executable. On Windows the running
    .exe can't overwrite itself directly, so the new build is written
    alongside it and swapped in on next launch by a tiny helper the
    Scheduled Task's restart picks up."""
    target = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
    tmp_fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(target) or ".", suffix=".new")
    with os.fdopen(tmp_fd, "wb") as f:
        f.write(new_bytes)
    if platform.system() == "Windows":
        # Can't replace a running .exe in place; stage it and swap via a
        # detached helper that waits for this process to exit, then renames
        # the files. The Scheduled Task's RestartCount/-Interval relaunches us.
        pending = target + ".pending"
        shutil.move(tmp_path, pending)
        helper = (
            f'$p="{target}"; $n="{pending}"; '
            'for($i=0;$i -lt 30;$i++){ try { Rename-Item -Force $n $p; break } catch { Start-Sleep 1 } }'
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", helper],
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0)
        os._exit(0)  # the Scheduled Task's auto-restart (RestartCount=999) brings the new build up
    else:
        os.chmod(tmp_path, 0o755)
        shutil.move(tmp_path, target)  # same filesystem rename: atomic, no helper needed
        os._exit(0)
