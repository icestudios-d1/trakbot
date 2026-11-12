"""The Cyril -> NetMon hand-over done by the installers.

The Mac installer is run for real (against a scratch root and a stub server): it only needs bash and curl. The
Windows installer cannot run here, so its safety rules are pinned by reading the script.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "cyril_server"
PS1 = (SCRIPTS / "winscripts" / "install.ps1").read_text(encoding="utf-8")
SH = (SCRIPTS / "macscripts" / "install.sh").read_text(encoding="utf-8")
NEW_AGENT = b"#!/bin/sh\nexit 0\n"       # a stand-in "netmon": --selftest and everything else exit 0


# ------------------------------------------------------------------------------------------------------ Windows (static)
def test_windows_installer_has_no_browser_extension():
    assert "extension" not in PS1.lower()


def test_windows_installer_downloads_and_self_tests_before_it_touches_cyril():
    """A failed download or a broken build must never cost a PC its working Cyril agent."""
    first_touch = PS1.index("Stop-ScheduledTask -TaskName $oldAgentTask")
    assert PS1.index("/dist/netmon.exe") < PS1.index('"--selftest"') < first_touch
    assert PS1.index("Checksum verified") < first_touch
    assert PS1.index("[IO.File]::WriteAllText($cfgFile") < first_touch          # config is in place before the hand-over


def test_windows_installer_switches_cyril_off_but_never_deletes_it():
    assert "Disable-ScheduledTask -TaskName $oldAgentTask" in PS1
    # nothing named as an old thing is ever unregistered or deleted in this update (that is the later update)
    for line in PS1.splitlines():
        if re.search(r"Unregister-ScheduledTask|Remove-Item|Remove-ItemProperty", line):
            assert not re.search(r"\$old|Cyril", line), line
    assert "Move-Item" not in PS1[PS1.index("if ($migrating) {"):PS1.index("# 5b. The updater")]      # data is copied, not moved


def test_windows_installer_rolls_back_to_cyril_when_netmon_does_not_come_up():
    assert "function Restore-Cyril" in PS1
    body = PS1[PS1.index("function Restore-Cyril"):PS1.index("if ($migrating) {")]
    assert "Enable-ScheduledTask -TaskName $oldAgentTask" in body and "Start-ScheduledTask -TaskName $oldAgentTask" in body
    # the migration block calls it from a catch, then fails the install loudly
    mig = PS1[PS1.index("if ($migrating) {"):PS1.index("# 5b. The updater")]
    assert "catch {" in mig and "Restore-Cyril $why" in mig and "Fail " in mig
    # the hand-over is only recorded once NetMon is confirmed running
    assert mig.index("netmon.exe exited right after starting") < mig.index("WriteAllText($migratedMarker")


def test_windows_installer_never_stops_the_old_updater_it_may_be_running_inside_of():
    """The old updater task is what fetches this script, so stopping it would kill the install half-way."""
    assert re.search(r"Stop-ScheduledTask[^\n]*\$(oldUpdaterTasks|t)\b", PS1) is None
    settle = PS1[PS1.index("# 5d."):PS1.index("# 6.")]
    assert "Disable-ScheduledTask" in settle and "Stop-ScheduledTask" not in settle and "Unregister" not in settle
    # ... and they are only switched off after the hand-over has been recorded
    assert "if (Test-Path $migratedMarker)" in settle


def test_windows_installer_keeps_identity_and_unsent_data():
    assert "oldCfgFile" in PS1 and "$cfg.user = $old.user" in PS1                 # the employee's name carries over
    assert "Copy-Item -Path $item.FullName" in PS1 and '$_.Name -ne "health.json"' in PS1
    assert "Get-DeviceId" in PS1 and "MachineGuid" in PS1                            # same device id before and after


def test_windows_installer_reports_the_hand_over_to_the_dashboard():
    assert "/api/agent/update-log" in PS1 and 'from_version = "cyril"' in PS1
    assert "Send-MigrationReport $true" in PS1 and "Send-MigrationReport $false" in PS1


def test_windows_uninstaller_removes_both_products():
    un = (SCRIPTS / "winscripts" / "uninstall.ps1").read_text(encoding="utf-8")
    for name in ("NetMon Agent", "NetMon Updater", "NetMon Recovery Check", "Cyril Agent", "Cyril Updater", "Cyril Recovery Check"):
        assert f'"{name}"' in un
    assert '"NetMon"' in un and '"Cyril"' in un and '"netmon", "cyril"' in un


# ----------------------------------------------------------------------------------------------------- macOS (really run)
@pytest.fixture
def stub_server():
    """A tiny server answering what install.sh asks: health, the Mac manifest, the binary and the update log."""
    state = {"posts": [], "binary": NEW_AGENT, "sha": None, "manifest_ok": True}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/api/health":
                return self._send(200, b'{"ok":true}')
            if path == "/api/agent/mac-manifest":
                if not state["manifest_ok"]:
                    return self._send(404)
                sha = state["sha"] or hashlib.sha256(state["binary"]).hexdigest()
                url = "/dist/netmon-mac-" + ("arm64" if "arm64" in self.path else "x86_64")
                return self._send(200, f"version=9.9.9\nbuild=abc1234\nurl={url}\nsha256={sha}\nselftest=1\n".encode())
            if path.startswith("/dist/netmon-mac-"):
                return self._send(200, state["binary"])
            self._send(404)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            state["posts"].append({"path": self.path, "key": self.headers.get("X-Api-Key"), "body": json.loads(self.rfile.read(n))})
            self._send(200, b'{"ok":true}')

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield state
    srv.shutdown()


needs_tools = pytest.mark.skipif(os.geteuid() != 0 or not shutil.which("curl") or not shutil.which("shasum"),
                                 reason="the Mac installer insists on running as root and needs curl and shasum")


def run_installer(root, state, key="agentkey"):
    text = SH.replace("{{SERVER_URL}}", state["url"]).replace("{{AGENT_KEY}}", key).replace("{{PAYLOAD_KEY}}", "")
    env = {"PATH": os.environ["PATH"], "NETMON_TEST_ROOT": str(root), "TMPDIR": str(root)}
    return subprocess.run(["bash", "-s", "--", "--unattended"], input=text, text=True, capture_output=True, env=env, timeout=120)


def make_cyril_mac(root, user="Priya"):
    """What a Mac that has been running Cyril looks like."""
    app = root / "Library/Application Support/Cyril"
    (app / "data/shots-pending").mkdir(parents=True)
    (app / "cyril").write_bytes(b"old agent")
    (app / "config.json").write_text(json.dumps({"server": "https://old.example", "key": "oldkey", "user": user}))
    (app / "data/spool.jsonl").write_text('{"queued":"event 1"}\n{"queued":"event 2"}\n')
    (app / "data/shots-pending/a.png").write_bytes(b"png")
    (app / "data/health.json").write_text('{"failing_since": 12345}')
    (app / "data/updater.log").write_text("old updater log")
    (app / "update.sh").write_text("#!/bin/bash\ncurl -fsS https://old.example/install.sh | bash\n")
    (root / "Library/LaunchAgents").mkdir(parents=True)
    (root / "Library/LaunchDaemons").mkdir(parents=True)
    (root / "Library/LaunchAgents/in.icestudios.cyril.plist").write_text("<plist>old agent</plist>")
    (root / "Library/LaunchDaemons/in.icestudios.cyril.updater.plist").write_text("<plist>old updater</plist>")
    return app


@needs_tools
def test_mac_hand_over_moves_cyril_to_netmon_and_keeps_everything(tmp_path, stub_server):
    old = make_cyril_mac(tmp_path)
    r = run_installer(tmp_path, stub_server)
    assert r.returncode == 0, r.stdout + r.stderr
    new = tmp_path / "Library/Application Support/NetMon"
    # NetMon is installed and configured: same server/key as asked, the employee's name carried over
    assert (new / "netmon").read_bytes() == NEW_AGENT and os.access(new / "netmon", os.X_OK)
    cfg = json.loads((new / "config.json").read_text())
    assert cfg["server"] == stub_server["url"] and cfg["key"] == "agentkey" and cfg["user"] == "Priya"
    # what Cyril had not sent yet came along; its own status file and updater log did not
    assert (new / "data/spool.jsonl").read_text() == (old / "data/spool.jsonl").read_text()
    assert (new / "data/shots-pending/a.png").read_bytes() == b"png"
    assert not (new / "data/health.json").exists() and not (new / "data/updater.log").exists()
    # launchd jobs: NetMon's exist and point at NetMon; Cyril's are switched off (renamed, not deleted)
    agent = (tmp_path / "Library/LaunchAgents/in.icestudios.netmon.plist").read_text()
    assert "/Library/Application Support/NetMon/netmon" in agent and "Cyril" not in agent
    assert "/Library/Application Support/NetMon/update.sh" in (tmp_path / "Library/LaunchDaemons/in.icestudios.netmon.updater.plist").read_text()
    for plist, text in (("LaunchAgents/in.icestudios.cyril", "old agent"), ("LaunchDaemons/in.icestudios.cyril.updater", "old updater")):
        assert not (tmp_path / f"Library/{plist}.plist").exists()
        assert text in (tmp_path / f"Library/{plist}.plist.disabled").read_text()
    # the old updater can no longer fetch/run anything, and nothing of Cyril was deleted
    assert "handed over to NetMon" in (old / "update.sh").read_text() and "curl" not in (old / "update.sh").read_text()
    assert (old / "cyril").read_bytes() == b"old agent" and (old / "data/spool.jsonl").exists() and (old / "config.json").exists()
    assert (new / "migrated-from-cyril").exists()
    # the new updater re-runs this installer with NetMon's own config
    assert "Application Support/NetMon/config.json" in (new / "update.sh").read_text()
    # the dashboard's update log hears about it, under the agent key, with the same device id the agent will use
    posts = [p for p in stub_server["posts"] if p["path"] == "/api/agent/update-log"]
    assert len(posts) == 1 and posts[0]["key"] == "agentkey"
    b = posts[0]["body"]
    assert b["ok"] is True and b["from_version"] == "cyril" and b["to_version"] == "netmon 9.9.9" and b["user"] == "Priya" and b["device"]
    assert "2 data item(s) copied" in b["detail"]      # the spool and the screenshot queue


@needs_tools
def test_mac_hand_over_runs_once_and_a_second_run_changes_nothing(tmp_path, stub_server):
    make_cyril_mac(tmp_path)
    assert run_installer(tmp_path, stub_server).returncode == 0
    new = tmp_path / "Library/Application Support/NetMon"
    (new / "data/spool.jsonl").write_text("NetMon's own newer queue\n")
    r = run_installer(tmp_path, stub_server)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (new / "data/spool.jsonl").read_text() == "NetMon's own newer queue\n"         # never overwritten by the old copy
    assert len([p for p in stub_server["posts"] if p["path"] == "/api/agent/update-log"]) == 1   # reported once, not daily
    assert (tmp_path / "Library/LaunchAgents/in.icestudios.cyril.plist.disabled").exists()


@needs_tools
def test_mac_hand_over_finishes_the_old_updater_if_an_earlier_run_stopped_half_way(tmp_path, stub_server):
    """Marker written but the old updater still enabled (e.g. the run was cut short): the next run completes it."""
    old = make_cyril_mac(tmp_path)
    assert run_installer(tmp_path, stub_server).returncode == 0
    (tmp_path / "Library/LaunchDaemons/in.icestudios.cyril.updater.plist.disabled").rename(
        tmp_path / "Library/LaunchDaemons/in.icestudios.cyril.updater.plist")
    (old / "update.sh").write_text("#!/bin/bash\ncurl https://old.example/install.sh | bash\n")
    assert run_installer(tmp_path, stub_server).returncode == 0
    assert (tmp_path / "Library/LaunchDaemons/in.icestudios.cyril.updater.plist.disabled").exists()
    assert "handed over to NetMon" in (old / "update.sh").read_text()


@needs_tools
@pytest.mark.parametrize("failure", ["wrong_checksum", "no_manifest"])
def test_mac_failed_download_leaves_cyril_completely_untouched(tmp_path, stub_server, failure):
    """Cyril is only stopped after the new program is downloaded, verified and self-tested."""
    old = make_cyril_mac(tmp_path)
    before = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*"))
    if failure == "wrong_checksum":
        stub_server["sha"] = "0" * 64
    else:
        stub_server["manifest_ok"] = False
    r = run_installer(tmp_path, stub_server)
    assert r.returncode != 0
    assert sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if "NetMon" not in str(p)) == \
        [p for p in before if "NetMon" not in p]
    assert (tmp_path / "Library/LaunchAgents/in.icestudios.cyril.plist").exists()
    assert (tmp_path / "Library/LaunchDaemons/in.icestudios.cyril.updater.plist").exists()
    assert "handed over" not in (old / "update.sh").read_text()
    assert not any(p["path"] == "/api/agent/update-log" for p in stub_server["posts"])


@needs_tools
def test_mac_failed_self_test_leaves_cyril_untouched(tmp_path, stub_server):
    make_cyril_mac(tmp_path)
    stub_server["binary"] = b"#!/bin/sh\nexit 3\n"            # a build that cannot start
    r = run_installer(tmp_path, stub_server)
    assert r.returncode != 0 and "self-test" in (r.stdout + r.stderr)
    assert (tmp_path / "Library/LaunchAgents/in.icestudios.cyril.plist").exists()
    assert not (tmp_path / "Library/Application Support/NetMon/netmon").exists()


@needs_tools
def test_mac_fresh_install_has_no_trace_of_cyril_and_makes_no_migration_report(tmp_path, stub_server):
    r = run_installer(tmp_path, stub_server)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "Library/Application Support/NetMon/netmon").exists()
    assert not (tmp_path / "Library/Application Support/Cyril").exists()
    assert not (tmp_path / "Library/Application Support/NetMon/migrated-from-cyril").exists()
    assert not any("cyril" in str(p).lower() for p in tmp_path.rglob("*") if p.is_file() and "tmp" not in p.name)
    assert stub_server["posts"] == []


def test_mac_installer_has_no_browser_extension_and_names_are_netmon():
    assert "extension" not in SH.lower()
    assert 'APPDIR="$R/Library/Application Support/NetMon"' in SH and 'BIN="$APPDIR/netmon"' in SH
