import os
import subprocess
import sys

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..")


def test_pyinstaller_entry_runs_as_a_plain_script():
    """PyInstaller runs the entry file as a top-level script (no parent package). A relative import in it
    killed every cyril.exe at startup ("attempted relative import with no known parent package")."""
    r = subprocess.run([sys.executable, os.path.join(AGENT_DIR, "cyril_entry.py"), "--help"],
                       capture_output=True, text=True, cwd=os.path.dirname(os.path.abspath(__file__)), timeout=60)
    assert r.returncode == 0, r.stderr
    assert "--config" in r.stdout


def test_build_script_uses_that_entry_point():
    build = open(os.path.join(AGENT_DIR, "..", "windows", "build.ps1"), encoding="utf-8").read()
    assert "cyril_entry.py" in build and "__main__.py" not in build
