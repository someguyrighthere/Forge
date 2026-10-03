import os
import sys
import traceback
from pathlib import Path

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")
if sys.stdin is None:
    sys.stdin = open(os.devnull, "r")

try:
    from forge.server import run
    raise SystemExit(run())
except SystemExit:
    raise
except BaseException:
    log = Path.home() / ".forge" / "forge.log"
    log.parent.mkdir(exist_ok=True)
    log.write_text(traceback.format_exc(), encoding="utf-8")
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, f"Forge could not start.\n\nDetails: {log}", "Forge", 0x10)
    except Exception:
        pass
    raise SystemExit(1)
