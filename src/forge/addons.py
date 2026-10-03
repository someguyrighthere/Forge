"""Components Forge can install for the user, with live status.

Two groups: what Forge needs to get started (Ollama and a model) and optional extras (Pyright, Playwright).
"""
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from forge import config, llm, python_intelligence
from forge.browser import find_browser
from forge.python_debug import _python_executable

NO_WINDOW = 0x08000000 if os.name == "nt" else 0
INSTALL_TIMEOUT = 600
STATUS_CACHE_SECONDS = 5.0
OLLAMA_SETUP_URL = "https://ollama.com/download/OllamaSetup.exe"
MODEL_SIZES = {"qwen3:8b": "about 5 GB", "qwen2.5-coder:7b": "about 5 GB", "llama3.2": "about 2 GB"}

ADDONS = [
    {"id": "ollama", "group": "start", "name": "Ollama",
     "description": "Runs the AI models on your PC. Forge needs it to answer anything (a download of about 1 GB).",
     "needs": "Windows", "needs_link": "https://ollama.com/download"},
    {"id": "model", "group": "start", "name": "AI model",
     "description": "", "needs": "Ollama", "needs_link": "https://ollama.com/download"},
    {"id": "pyright", "group": "extra", "name": "Pyright",
     "description": "Powers Forge's Python code tools: types, references, rename preview, call hierarchy and error checking.",
     "needs": "Node.js", "needs_link": "https://nodejs.org"},
    {"id": "playwright", "group": "extra", "name": "Playwright",
     "description": "Lets Forge click and type on web pages, using the Edge or Chrome you already have (no browser download).",
     "needs": "Python and Edge or Chrome", "needs_link": "https://www.python.org/downloads/"},
]

_lock = threading.Lock()
_progress: dict[str, dict] = {}           # id -> {"state": installing|done|failed, "detail": str}
_cache: dict[str, tuple[float, bool]] = {}  # id -> (checked at, installed)


def _model_name() -> str:
    return str(config.load().get("model") or "qwen3:8b")


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
    if addon_id == "ollama":
        return llm.find_ollama() is not None
    if addon_id == "model":
        try:
            names = {m.get("name", "") for m in llm.list_models()}
        except llm.OllamaError:
            return False
        model = _model_name()
        return model in names or (":" not in model and f"{model}:latest" in names)
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
    """Why this cannot be installed right now, or None."""
    if addon_id == "ollama":
        return None if os.name == "nt" else "One-click setup is for Windows. Get Ollama from ollama.com."
    if addon_id == "model":
        if not llm.find_ollama():
            return "Install Ollama first."
        try:
            llm.list_models()
        except llm.OllamaError:
            return "Ollama is installed but not running yet. Give it a few seconds, then reopen this window."
        return None
    if addon_id == "pyright":
        return None if _npm() else "Needs Node.js, which provides npm."
    if not _python():
        return "Needs Python (pip)."
    if not find_browser():
        return "Needs Microsoft Edge or Google Chrome."
    return None


def get_addons() -> list[dict]:
    """Status of every component, for the window."""
    result = []
    for addon in ADDONS:
        item: dict = dict(addon)
        progress = _progress.get(addon["id"], {})
        installing = progress.get("state") == "installing"
        installed = False if installing else _cached_installed(addon["id"])
        blocker = None if installed or installing else _blocker(addon["id"])
        note = ""
        if addon["id"] == "model":
            model = _model_name()
            size = MODEL_SIZES.get(model, "several GB")
            item["name"] = f"AI model: {model}"
            item["description"] = f"The model Forge answers with (a download of {size}). You can change it later from the model menu."
        if addon["id"] == "playwright" and not installed and not blocker:
            note = f"Installs into: {_python()}"
        item.update(installed=installed, state=progress.get("state", ""), detail=progress.get("detail", ""),
                    blocker=blocker, note=note)
        result.append(item)
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


def _signed_by_ollama(path: Path) -> bool:
    """True only when Windows reports a valid digital signature whose subject names Ollama."""
    quoted = str(path).replace("'", "''")  # single-quoted PowerShell literal: only ' needs escaping
    script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
              "\"$($s.Status)|$($s.SignerCertificate.Subject)\"")
    try:
        output = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
                                creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return False
    status, _, subject = output.partition("|")
    return status == "Valid" and "ollama" in subject.lower()


def _install_ollama(report) -> tuple[bool, str]:
    folder = Path(tempfile.mkdtemp(prefix="forge-ollama-"))
    target = folder / "OllamaSetup.exe"
    try:
        request = urllib.request.Request(OLLAMA_SETUP_URL, headers={"User-Agent": "forge-agent"})
        with urllib.request.urlopen(request, timeout=30) as response, open(target, "wb") as out:
            total, done = int(response.headers.get("Content-Length") or 0), 0
            while chunk := response.read(1 << 20):
                out.write(chunk)
                done += len(chunk)
                report(f"Downloading… {done * 100 // total}%" if total else f"Downloading… {done >> 20} MB")
        report("Checking the download's signature…")
        if not _signed_by_ollama(target):
            return False, "The downloaded installer's signature could not be verified as Ollama's, so it was not run."
        report("Installing… (this can take a few minutes)")
        code = subprocess.run([str(target), "/VERYSILENT", "/NORESTART"], timeout=INSTALL_TIMEOUT,
                              stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).returncode
        if code != 0:
            return False, f"The Ollama installer failed (exit code {code})."
        return True, "Installed."
    except OSError as error:
        return False, f"Could not download or run the Ollama installer: {error}"
    except subprocess.TimeoutExpired:
        return False, f"The install took longer than {INSTALL_TIMEOUT // 60} minutes and was stopped."
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def run_install(addon_id: str, report=None) -> tuple[bool, str]:
    """Install one component and wait for it. Returns (success, short message). report(text) gets progress."""
    report = report or (lambda text: None)
    if addon_id not in {a["id"] for a in ADDONS}:
        return False, f"Unknown add-on {addon_id!r}."
    blocker = _blocker(addon_id)
    if blocker:
        return False, blocker
    try:
        if addon_id == "ollama":
            ok, message = _install_ollama(report)
            if not ok:
                return False, message
            code, output = 0, ""
        elif addon_id == "model":
            try:
                llm.pull_model(_model_name(), lambda status, pct: report(
                    f"Downloading… {pct}%" if pct is not None else (status or "Working…")))
            except llm.OllamaError as error:
                return False, str(error)
            code, output = 0, ""
        elif addon_id == "pyright":
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
    if addon_id == "ollama":
        time.sleep(1)  # let the installer's own launch settle before the caller starts or checks the server
    if not _installed(addon_id) and addon_id != "model":
        return False, "The installer finished, but Forge still can't find it. Restart Forge and check again."
    return True, "Installed."


def install(addon_id: str, on_done=None) -> str | None:
    """Start installing in the background. Returns an error message, or None when it has started.

    on_done(success) is called from the worker thread when it finishes (the app uses it to start and recheck Ollama).
    """
    if addon_id not in {a["id"] for a in ADDONS}:
        return f"Unknown add-on {addon_id!r}."
    with _lock:
        if _progress.get(addon_id, {}).get("state") == "installing":
            return "That is already installing."
        blocker = _blocker(addon_id)
        if blocker:
            return blocker
        _progress[addon_id] = {"state": "installing", "detail": ""}

    def report(text: str) -> None:
        _progress[addon_id] = {"state": "installing", "detail": text}

    def work():
        ok, message = run_install(addon_id, report)
        _progress[addon_id] = {"state": "done" if ok else "failed", "detail": "" if ok else message}
        if on_done:
            try:
                on_done(ok)
            except Exception:
                pass  # a failing callback must not hide the install result

    threading.Thread(target=work, daemon=True).start()
    return None
