import io
import json

import pytest

from forge import config, updater


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(config, "HISTORY_FILE", tmp_path / "history")
    monkeypatch.setattr(updater, "__version__", "1.1.1")
    monkeypatch.setattr(updater, "release_notes", lambda version, timeout=6.0: f"notes for {version}")
    return tmp_path


def test_fresh_install_is_recorded_silently(home):
    assert updater.pending_whats_new() is None
    assert (home / "last_version").read_text(encoding="utf-8") == "1.1.1"


def test_existing_user_without_a_marker_is_treated_as_upgraded(home):
    (home / "sessions").mkdir()
    (home / "sessions" / "20260101-000000.json").write_text("{}", encoding="utf-8")
    assert updater.pending_whats_new() == {"version": "1.1.1", "notes": "notes for 1.1.1"}
    assert not (home / "last_version").exists()  # only written once the user acknowledges it


def test_upgrade_is_announced_until_acknowledged(home):
    (home / "last_version").write_text("1.1.0", encoding="utf-8")
    pending = updater.pending_whats_new()
    assert pending and pending["version"] == "1.1.1"
    pending = updater.pending_whats_new()
    assert pending and pending["version"] == "1.1.1"  # still pending if the window was never confirmed
    updater.mark_seen()
    assert updater.pending_whats_new() is None


def test_same_or_older_build_announces_nothing(home):
    (home / "last_version").write_text("1.1.1", encoding="utf-8")
    assert updater.pending_whats_new() is None
    (home / "last_version").write_text("1.2.0", encoding="utf-8")  # downgraded
    assert updater.pending_whats_new() is None
    assert (home / "last_version").read_text(encoding="utf-8") == "1.1.1"


def test_check_returns_the_full_release_notes(monkeypatch):
    notes = "x" * 3500
    release = {"tag_name": "v9.0.0", "body": notes, "assets": [
        {"name": "Forge-Setup-9.0.0.exe", "browser_download_url": "https://github.com/x/y/a.exe", "digest": "sha256:ab"}]}
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(release).encode()))
    info = updater.check()
    assert info and info["notes"] == notes  # was cut off at 600 characters before


def test_release_notes_for_a_version_tolerate_failures(monkeypatch):
    monkeypatch.undo()  # use the real release_notes, with the network call replaced below
    seen = {}

    def fake(request, timeout=0):
        seen["url"] = request.full_url
        return io.BytesIO(json.dumps({"body": "Hello"}).encode())
    monkeypatch.setattr(updater.urllib.request, "urlopen", fake)
    assert updater.release_notes("1.2.3") == "Hello"
    assert seen["url"].endswith("/releases/tags/v1.2.3")

    def broken(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(updater.urllib.request, "urlopen", broken)
    assert updater.release_notes("1.2.3") == ""


def test_server_exposes_and_clears_whats_new(home, monkeypatch):
    from forge.server import App, Hub
    app = App.__new__(App)
    app.hub, app.whats_new = Hub(), {"version": "1.1.1", "notes": "n"}
    monkeypatch.setattr(App, "snapshot", lambda self: {"whatsNew": self.whats_new})
    app.command("whatsnew_seen", "")
    assert app.whats_new is None
    assert (home / "last_version").read_text(encoding="utf-8") == "1.1.1"
