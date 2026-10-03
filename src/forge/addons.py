"""Optional components Forge can install for the user (Pyright and Playwright), with live status."""
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from forge import python_intelligence
from forge.browser import find_browser
from forge.python_debug import _python_executable

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INSTALL_TIMEOUT = 600
STATUS_CACHE_SECONDS = 5.0

ADDONS = [
    {"id": "pyright", "name": "Pyright",
     "description": "Powers Forge's Python code tools: types, references, rename preview, call hierarchy and error checking.",
     "needs": "Node.js", "needs_link": "https://nodejs.org"},
    {"id": "playwright", "name": "Playwright",
     "description": "Lets Forge click and type on web pages, using the Edge or Chrome you already have (no browser download).",
     "needs": "Python and Edge or Chrome", "needs_link": "https://www.python.org/downloads/"},
]

_lock = threading.Lock()
_progress: dict[str, dict] = {}           # id -> {"state": installing|done|failed, "detail": str}
_cache: dict[str, tuple[float, bool]] = {}  # id -> (checked at, installed)


def _npm() -> str | None:
    found = shutil.which("npm") or shutil.which("npm.cmd")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base and (Path(base) / "nodejs" / "npm.cmd").is_file():
            return str(Path(base) / "nodejs" / "npm.cmd")
    return None


def _python() -> str | None:
    try:
        return _python_executable(Path.cwd().resolve())
    except RuntimeError:
        return None


def _playwright_installed(python: str) -> bool:
    try:
        return subprocess.run([python, "-c", "import playwright"], capture_output=True, timeout=30,
                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _installed(addon_id: str) -> bool:
    if addon_id == "pyright":
        return python_intelligence.pyright_available()
    python = _python()
    return bool(python) and _playwright_installed(python)


def _cached_installed(addon_id: str) -> bool:
    checked, value = _cache.get(addon_id, (0.0, False))
    if time.monotonic() - checked > STATUS_CACHE_SECONDS:
        value = _installed(addon_id)
        _cache[addon_id] = (time.monotonic(), value)
    return value


def _blocker(addon_id: str) -> str | None:
    """Why this add-on cannot be installed right now, or None."""
    if addon_id == "pyright":
        return None if _npm() else "Needs Node.js, which provides npm."
    if not _python():
        return "Needs Python (pip)."
    if not find_browser():
        return "Needs Microsoft Edge or Google Chrome."
    return None


def get_addons() -> list[dict]:
    """Status of every add-on, for the window."""
    result = []
    for addon in ADDONS:
        progress = _progress.get(addon["id"], {})
        installing = progress.get("state") == "installing"
        installed = False if installing else _cached_installed(addon["id"])
        blocker = None if installed or installing else _blocker(addon["id"])
        note = ""
        if addon["id"] == "playwright" and not installed and not blocker:
            note = f"Installs into: {_python()}"
        result.append({**addon, "installed": installed, "state": progress.get("state", ""),
                       "detail": progress.get("detail", ""), "blocker": blocker, "note": note})
    return result


def _run(command: list[str]) -> tuple[int, str]:
    if os.name == "nt" and command[0].lower().endswith((".cmd", ".bat")):
        process = subprocess.run(subprocess.list2cmdline(command), shell=True, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=INSTALL_TIMEOUT,
                                 stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    else:
        process = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=INSTALL_TIMEOUT, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    return process.returncode, (process.stdout + "\n" + process.stderr).strip()


def run_install(addon_id: str) -> tuple[bool, str]:
    """Install one add-on and wait for it. Returns (success, short message)."""
    known = {a["id"] for a in ADDONS}
    if addon_id not in known:
        return False, f"Unknown add-on {addon_id!r}."
    blocker = _blocker(addon_id)
    if blocker:
        return False, blocker
    try:
        if addon_id == "pyright":
            npm = _npm()
            assert npm
            code, output = _run([npm, "install", "--global", "pyright"])
        else:
            python = _python()
            assert python
            code, output = _run([python, "-m", "pip", "install", "playwright"])
            if code != 0 and ("Permission" in output or "Errno 13" in output):
                code, output = _run([python, "-m", "pip", "install", "--user", "playwright"])
    except subprocess.TimeoutExpired:
        return False, f"The install took longer than {INSTALL_TIMEOUT // 60} minutes and was stopped."
    except OSError as error:
        return False, f"Could not run the installer: {error}"
    _cache.pop(addon_id, None)
    if code != 0:
        return False, "\n".join(output.splitlines()[-8:]) or f"The installer failed (exit code {code})."
    if not _installed(addon_id):
        return False, "The installer finished, but Forge still can't find it. Restart Forge and check again."
    return True, "Installed."


def install(addon_id: str) -> str | None:
    """Start installing in the background. Returns an error message, or None when it has started."""
    if addon_id not in {a["id"] for a in ADDONS}:
        return f"Unknown add-on {addon_id!r}."
    with _lock:
        if _progress.get(addon_id, {}).get("state") == "installing":
            return "That is already installing."
        blocker = _blocker(addon_id)
        if blocker:
            return blocker
        _progress[addon_id] = {"state": "installing", "detail": ""}

    def work():
        ok, message = run_install(addon_id)
        _progress[addon_id] = {"state": "done" if ok else "failed", "detail": "" if ok else message}

    threading.Thread(target=work, daemon=True).start()
    return None
