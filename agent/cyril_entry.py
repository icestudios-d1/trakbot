"""PyInstaller entry point for cyril.exe (windows/build.ps1 builds this file).

Why a separate file: PyInstaller runs its entry script as a top-level module with no parent package, so the
relative import in cyril_agent/__main__.py ("from .main import main") fails there with "attempted relative
import with no known parent package" and the .exe dies before doing anything. This script imports the package
by absolute name instead. `python -m cyril_agent` keeps using __main__.py.
"""
import os
import sys
import traceback

from cyril_agent.main import main

if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # The Windows build has no console; leave the traceback where the installer's diagnostics look for it.
        try:
            d = os.path.join(os.environ.get("ProgramData", os.path.expanduser("~")), "Cyril", "data")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "crash.log"), "a", encoding="utf-8") as f:
                f.write(traceback.format_exc() + "\n")
        except OSError:
            pass
        sys.exit(1)
