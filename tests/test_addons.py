import subprocess
import time

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


def test_lists_both_addons_with_status_and_what_they_do():
    items = by_id(addons.get_addons())
    assert set(items) == {"pyright", "playwright"}
    assert items["pyright"]["installed"] is False and items["pyright"]["blocker"] is None
    assert "Python code tools" in items["pyright"]["description"]
    assert items["playwright"]["note"] == "Installs into: C:/py/python.exe"


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
    monkeypatch.setattr(addons, "run_install", lambda addon_id: (release and release[0] or time.sleep(0.3)) or (True, "Installed."))
    assert addons.install("pyright") is None
    assert by_id(addons.get_addons())["pyright"]["state"] == "installing"
    assert addons.install("pyright") == "That is already installing."
    assert wait_for(lambda: addons._progress["pyright"]["state"] == "done")


def test_a_failed_background_install_keeps_the_error_for_the_window(monkeypatch):
    monkeypatch.setattr(addons, "run_install", lambda addon_id: (False, "npm ERR! network"))
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


def test_server_command_starts_an_install(monkeypatch):
    started = []
    monkeypatch.setattr(addons, "install", lambda addon_id: started.append(addon_id))
    app = App.__new__(App)
    assert app.command("addon_install", "pyright") is None and started == ["pyright"]


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
