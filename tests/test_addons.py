import subprocess
import time
from pathlib import Path

import pytest

from forge import addons, python_intelligence
from forge.server import App


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    addons._progress.clear()
    addons._cache.clear()
    monkeypatch.setattr(addons, "_npm", lambda: "C:/node/npm.cmd")
    monkeypatch.setattr(addons, "_python", lambda: "C:/py/python.exe")
    monkeypatch.setattr(addons, "find_browser", lambda: "C:/edge/msedge.exe")
    monkeypatch.setattr(addons, "_installed", lambda addon_id: False)
    monkeypatch.setattr(addons, "_model_name", lambda: "qwen3:8b")
    monkeypatch.setattr(addons.llm, "find_ollama", lambda: "C:/ollama/ollama.exe")
    monkeypatch.setattr(addons.llm, "list_models", lambda: [])
    yield
    addons._progress.clear()
    addons._cache.clear()


def by_id(items):
    return {item["id"]: item for item in items}


def wait_for(condition, seconds=5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def test_lists_every_component_in_two_groups_with_status_and_what_they_do():
    items = by_id(addons.get_addons())
    assert list(items) == ["ollama", "model", "pyright", "playwright"]
    assert [items[i]["group"] for i in items] == ["start", "start", "extra", "extra"]
    assert items["pyright"]["installed"] is False and items["pyright"]["blocker"] is None
    assert "Python code tools" in items["pyright"]["description"]
    assert items["playwright"]["note"] == "Installs into: C:/py/python.exe"
    assert items["model"]["name"] == "AI model: qwen3:8b" and "about 5 GB" in items["model"]["description"]


def test_installed_addons_are_reported_and_cannot_be_reinstalled(monkeypatch):
    monkeypatch.setattr(addons, "_installed", lambda addon_id: addon_id == "pyright")
    items = by_id(addons.get_addons())
    assert items["pyright"]["installed"] is True and items["pyright"]["blocker"] is None
    assert items["playwright"]["installed"] is False


def test_missing_prerequisites_are_explained(monkeypatch):
    monkeypatch.setattr(addons, "_npm", lambda: None)
    monkeypatch.setattr(addons, "find_browser", lambda: None)
    items = by_id(addons.get_addons())
    assert items["pyright"]["blocker"] == "Needs Node.js, which provides npm."
    assert items["playwright"]["blocker"] == "Needs Microsoft Edge or Google Chrome."
    monkeypatch.setattr(addons, "_python", lambda: None)
    assert by_id(addons.get_addons())["playwright"]["blocker"] == "Needs Python (pip)."
    assert addons.install("pyright") == "Needs Node.js, which provides npm."
    assert not addons._progress  # a blocked install never starts


def test_the_model_needs_ollama_installed_and_running(monkeypatch):
    monkeypatch.setattr(addons.llm, "find_ollama", lambda: None)
    assert by_id(addons.get_addons())["model"]["blocker"] == "Install Ollama first."

    monkeypatch.setattr(addons.llm, "find_ollama", lambda: "C:/ollama/ollama.exe")

    def unreachable():
        raise addons.llm.OllamaError("Cannot reach Ollama")
    monkeypatch.setattr(addons.llm, "list_models", unreachable)
    assert "not running yet" in by_id(addons.get_addons())["model"]["blocker"]
    assert addons.install("model") is not None and not addons._progress


def test_run_install_uses_the_fixed_commands(monkeypatch):
    commands = []
    monkeypatch.setattr(addons, "_run", lambda command: commands.append(command) or (0, "added 1 package"))
    monkeypatch.setattr(addons, "_installed", lambda addon_id: True)
    assert addons.run_install("pyright") == (True, "Installed.")
    assert addons.run_install("playwright") == (True, "Installed.")
    assert commands == [["C:/node/npm.cmd", "install", "--global", "pyright"],
                        ["C:/py/python.exe", "-m", "pip", "install", "playwright"]]


def test_failed_install_returns_the_end_of_the_output(monkeypatch):
    output = "\n".join(f"line {n}" for n in range(30)) + "\nERROR: no matching distribution"
    monkeypatch.setattr(addons, "_run", lambda command: (1, output))
    ok, message = addons.run_install("playwright")
    assert ok is False and message.endswith("ERROR: no matching distribution")
    assert len(message.splitlines()) == 8 and "line 0" not in message


def test_a_finished_install_that_is_still_not_found_says_so(monkeypatch):
    monkeypatch.setattr(addons, "_run", lambda command: (0, "ok"))
    ok, message = addons.run_install("pyright")
    assert ok is False and "Restart Forge" in message


def test_permission_errors_retry_with_a_user_install(monkeypatch):
    commands = []

    def fake_run(command):
        commands.append(command)
        return (1, "ERROR: [Errno 13] Permission denied") if "--user" not in command else (0, "ok")
    monkeypatch.setattr(addons, "_run", fake_run)
    monkeypatch.setattr(addons, "_installed", lambda addon_id: True)
    assert addons.run_install("playwright") == (True, "Installed.")
    assert commands[-1] == ["C:/py/python.exe", "-m", "pip", "install", "--user", "playwright"]


def test_timeouts_and_missing_programs_are_reported(monkeypatch):
    def timeout(command):
        raise subprocess.TimeoutExpired(command, 600)
    monkeypatch.setattr(addons, "_run", timeout)
    assert "longer than 10 minutes" in addons.run_install("pyright")[1]

    def missing(command):
        raise FileNotFoundError("npm")
    monkeypatch.setattr(addons, "_run", missing)
    assert addons.run_install("pyright")[1].startswith("Could not run the installer")


def test_unknown_addons_are_rejected():
    assert addons.run_install("rm -rf")[1].startswith("Unknown add-on")
    error = addons.install("something-else")
    assert error is not None and error.startswith("Unknown add-on")


def test_background_install_reports_progress_and_blocks_duplicates(monkeypatch):
    release = []
    monkeypatch.setattr(addons, "run_install", lambda addon_id, report=None: (release and release[0] or time.sleep(0.3)) or (True, "Installed."))
    assert addons.install("pyright") is None
    assert by_id(addons.get_addons())["pyright"]["state"] == "installing"
    assert addons.install("pyright") == "That is already installing."
    assert wait_for(lambda: addons._progress["pyright"]["state"] == "done")


def test_a_failed_background_install_keeps_the_error_for_the_window(monkeypatch):
    monkeypatch.setattr(addons, "run_install", lambda addon_id, report=None: (False, "npm ERR! network"))
    addons.install("playwright")
    assert wait_for(lambda: addons._progress["playwright"]["state"] == "failed")
    entry = by_id(addons.get_addons())["playwright"]
    assert entry["state"] == "failed" and entry["detail"] == "npm ERR! network"
    assert addons.install("playwright") is None  # "Try again" starts a fresh attempt


def test_install_status_checks_are_cached_briefly(monkeypatch):
    calls = []
    monkeypatch.setattr(addons, "_installed", lambda addon_id: calls.append(addon_id) or False)
    addons.get_addons()
    addons.get_addons()
    assert calls.count("playwright") == 1  # not re-checked on every poll of the window


def test_server_command_starts_an_install_and_rechecks_ollama_when_it_finishes(monkeypatch):
    from forge import server
    started, rechecked = [], []
    monkeypatch.setattr(addons, "install", lambda addon_id, on_done=None: started.append((addon_id, on_done)))
    monkeypatch.setattr(server, "ensure_ollama", lambda app: rechecked.append(app))
    app = App.__new__(App)
    assert app.command("addon_install", "ollama") is None
    assert started[0][0] == "ollama"
    started[0][1](True)   # the install finished
    assert rechecked == [app]


def test_installed_model_is_recognised_by_name(monkeypatch):
    monkeypatch.undo()  # use the real _installed with Ollama's model list faked below
    monkeypatch.setattr(addons.llm, "list_models", lambda: [{"name": "qwen3:8b"}, {"name": "llama3.2:latest"}])
    monkeypatch.setattr(addons, "_model_name", lambda: "qwen3:8b")
    assert addons._installed("model") is True
    monkeypatch.setattr(addons, "_model_name", lambda: "llama3.2")   # no tag given: ":latest" counts
    assert addons._installed("model") is True
    monkeypatch.setattr(addons, "_model_name", lambda: "mistral")
    assert addons._installed("model") is False


def test_model_install_reports_download_progress(monkeypatch):
    seen = []
    monkeypatch.setattr(addons.llm, "list_models", lambda: [])
    monkeypatch.setattr(addons.llm, "pull_model", lambda name, on_progress: (
        [on_progress(status, pct) for status, pct in (("pulling manifest", None), ("pulling", 40), ("pulling", 100))],
        seen.append(name)))
    monkeypatch.setattr(addons, "_installed", lambda addon_id: True)
    reports = []
    assert addons.run_install("model", reports.append) == (True, "Installed.")
    assert seen == ["qwen3:8b"] and reports == ["pulling manifest", "Downloading… 40%", "Downloading… 100%"]


def test_model_download_errors_are_shown(monkeypatch):
    def fail(name, on_progress):
        raise addons.llm.OllamaError("pull model manifest: file does not exist")
    monkeypatch.setattr(addons.llm, "pull_model", fail)
    assert addons.run_install("model") == (False, "pull model manifest: file does not exist")


def test_signature_check_accepts_only_a_valid_ollama_signature(monkeypatch, tmp_path):
    def answer(text):
        monkeypatch.setattr(addons.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": text})())
        return addons._signed_by_ollama(tmp_path / "OllamaSetup.exe")
    assert answer("Valid|CN=Ollama Inc., O=Ollama Inc., C=US") is True
    assert answer("Valid|CN=Some Other Publisher") is False       # signed, but not by Ollama
    assert answer("NotSigned|") is False
    assert answer("HashMismatch|CN=Ollama Inc.") is False        # tampered after signing
    assert answer("") is False

    def broken(*args, **kwargs):
        raise OSError("powershell missing")
    monkeypatch.setattr(addons.subprocess, "run", broken)
    assert addons._signed_by_ollama(tmp_path / "x.exe") is False


def test_signature_check_passes_the_file_path_inside_the_command_not_as_an_ignored_argument(monkeypatch):
    # Arguments after -Command are not bound to param(), which once made every real installer fail the check.
    seen = {}
    monkeypatch.setattr(addons.subprocess, "run", lambda command, **k: seen.update(command=command) or type("R", (), {"stdout": ""})())
    addons._signed_by_ollama(Path("C:/Temp/it's here/OllamaSetup.exe"))
    script = seen["command"][-1]
    assert "param(" not in script and "'C:\\Temp\\it''s here\\OllamaSetup.exe'" in script.replace("/", "\\")


class FakeResponse:
    def __init__(self, data):
        self.data, self.headers = data, {"Content-Length": str(len(data))}

    def read(self, size):
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_ollama_install_downloads_verifies_then_runs_silently_and_cleans_up(monkeypatch):
    requested, ran, folders = [], [], []
    monkeypatch.setattr(addons.urllib.request, "urlopen", lambda request, timeout=0: requested.append(request.full_url) or FakeResponse(b"x" * 3_000_000))
    monkeypatch.setattr(addons, "_signed_by_ollama", lambda path: folders.append(path.parent) or True)
    monkeypatch.setattr(addons.subprocess, "run", lambda command, **k: ran.append(command) or type("R", (), {"returncode": 0})())
    monkeypatch.setattr(addons, "_installed", lambda addon_id: True)
    monkeypatch.setattr(addons.time, "sleep", lambda s: None)
    reports = []
    assert addons.run_install("ollama", reports.append) == (True, "Installed.")
    assert requested == ["https://ollama.com/download/OllamaSetup.exe"]       # only ever the fixed address
    assert ran[0][1:] == ["/VERYSILENT", "/NORESTART"] and ran[0][0].endswith("OllamaSetup.exe")
    assert any(r.startswith("Downloading…") for r in reports) and "Checking the download's signature…" in reports
    assert not folders[0].exists()                                            # the 3 MB download was deleted


def test_ollama_installer_is_never_run_when_its_signature_is_not_valid(monkeypatch):
    ran = []
    monkeypatch.setattr(addons.urllib.request, "urlopen", lambda request, timeout=0: FakeResponse(b"MZ not really"))
    monkeypatch.setattr(addons, "_signed_by_ollama", lambda path: False)
    monkeypatch.setattr(addons.subprocess, "run", lambda command, **k: ran.append(command))
    ok, message = addons.run_install("ollama")
    assert ok is False and "was not run" in message and ran == []


def test_ollama_download_failures_are_reported(monkeypatch):
    def offline(request, timeout=0):
        raise OSError("no internet")
    monkeypatch.setattr(addons.urllib.request, "urlopen", offline)
    ok, message = addons.run_install("ollama")
    assert ok is False and "no internet" in message


def test_on_done_is_called_with_the_result_and_cannot_hide_it(monkeypatch):
    monkeypatch.setattr(addons, "run_install", lambda addon_id, report=None: (True, "Installed."))
    results = []

    def broken_callback(ok):
        results.append(ok)
        raise RuntimeError("callback bug")
    assert addons.install("pyright", on_done=broken_callback) is None
    assert wait_for(lambda: addons._progress.get("pyright", {}).get("state") == "done")
    assert results == [True]


def test_pyright_is_found_in_npms_global_folder_without_a_restart(monkeypatch, tmp_path):
    folder = tmp_path / "npm"
    folder.mkdir()
    launcher = folder / ("pyright-langserver.cmd" if __import__("os").name == "nt" else "pyright-langserver")
    launcher.write_text("", encoding="utf-8")
    monkeypatch.delenv("PYRIGHT_LANGSERVER", raising=False)
    monkeypatch.setattr(python_intelligence.shutil, "which", lambda name: None)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert python_intelligence._server_command() == [str(launcher), "--stdio"]
    assert python_intelligence.pyright_available() is True


def test_terminal_command_lists_and_installs(monkeypatch, capsys):
    cli = pytest.importorskip("forge.cli")  # needs prompt_toolkit, a runtime dependency of the terminal UI
    cli.show_addons("")
    listing = capsys.readouterr().out
    assert "Pyright" in listing and "/addons install pyright" in listing and "Playwright" in listing
    monkeypatch.setattr(addons, "run_install", lambda addon_id: (True, "Installed."))
    cli.show_addons("install pyright")
    assert "Installed." in capsys.readouterr().out
