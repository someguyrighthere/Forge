import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from forge import config, server


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HOME", tmp_path)
    return tmp_path


@pytest.fixture
def fake_forge():
    """A tiny server that answers /api/state only when given the right token, like Forge does."""
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.path.startswith("/api/state") and "t=secret" in self.path
            self.send_response(200 if ok else 403)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield httpd
    httpd.shutdown()


def write_instance(home, url):
    (home / "instance.json").write_text(json.dumps({"url": url, "pid": 1}), encoding="utf-8")


def test_no_running_instance_when_there_is_no_registration(home):
    assert server.running_instance_url() is None


def test_a_live_registered_instance_is_found(home, fake_forge):
    url = f"http://127.0.0.1:{fake_forge.server_port}/?t=secret"
    write_instance(home, url)
    assert server.running_instance_url() == url


@pytest.mark.parametrize("content", ["not json", '{"url": ""}', '{"url": "http://example.com/?t=x"}',
                                     '{"url": "http://127.0.0.1/?t=x"}', '{"url": "http://127.0.0.1:9/"}', '[]'])
def test_unusable_registrations_are_ignored(home, content):
    (home / "instance.json").write_text(content, encoding="utf-8")
    assert server.running_instance_url() is None


def test_a_dead_or_foreign_server_is_not_treated_as_forge(home, fake_forge):
    write_instance(home, f"http://127.0.0.1:{fake_forge.server_port}/?t=wrong-token")  # answers 403
    assert server.running_instance_url() is None
    fake_forge.shutdown()
    fake_forge.server_close()
    write_instance(home, f"http://127.0.0.1:{fake_forge.server_port}/?t=secret")  # nothing listening any more
    assert server.running_instance_url() is None


def test_register_and_clear_never_remove_a_newer_instance(home):
    server.register_instance("http://127.0.0.1:1111/?t=a")
    assert json.loads((home / "instance.json").read_text(encoding="utf-8"))["url"] == "http://127.0.0.1:1111/?t=a"
    server.register_instance("http://127.0.0.1:2222/?t=b")  # a newer instance took over
    server.clear_instance("http://127.0.0.1:1111/?t=a")      # the old one exits afterwards
    assert (home / "instance.json").exists()
    server.clear_instance("http://127.0.0.1:2222/?t=b")
    assert not (home / "instance.json").exists()


def test_a_second_launch_opens_a_window_on_the_running_instance_instead_of_starting_another(monkeypatch):
    opened = []
    monkeypatch.setattr(server, "running_instance_url", lambda: "http://127.0.0.1:5000/?t=abc")
    monkeypatch.setattr(server, "launch_window", lambda url: opened.append(url) or object())

    def must_not_start(*args, **kwargs):
        raise AssertionError("a second server must not be started")
    monkeypatch.setattr(server, "App", must_not_start)
    assert server.run() == 0
    assert opened == ["http://127.0.0.1:5000/?t=abc"]


def test_second_launch_falls_back_to_the_default_browser_when_there_is_no_edge(monkeypatch):
    opened = []
    monkeypatch.setattr(server, "running_instance_url", lambda: "http://127.0.0.1:5000/?t=abc")
    monkeypatch.setattr(server, "launch_window", lambda url: None)
    monkeypatch.setattr(server.webbrowser, "open", lambda url: opened.append(url))
    assert server.run() == 0 and opened == ["http://127.0.0.1:5000/?t=abc"]


def test_only_one_process_wins_the_startup_lock(home, monkeypatch):
    monkeypatch.setattr(server, "running_instance_url", lambda: None)
    assert server.acquire_startup_lock() is True
    assert (home / "startup.lock").read_text(encoding="utf-8") == str(__import__("os").getpid())
    # A second launch at the same instant waits, and gives up its claim once the winner is serving.
    answers = iter([None, None, "http://127.0.0.1:5000/?t=abc"])
    monkeypatch.setattr(server, "running_instance_url", lambda: next(answers))
    assert server.acquire_startup_lock(timeout=5, poll=0.01) is False


def test_a_stale_lock_from_a_crashed_start_is_taken_over(home, monkeypatch):
    import os
    monkeypatch.setattr(server, "running_instance_url", lambda: None)
    lock = home / "startup.lock"
    lock.write_text("99999", encoding="utf-8")
    old = lock.stat().st_mtime - server.STARTUP_LOCK_STALE_SECONDS - 5
    os.utime(lock, (old, old))
    assert server.acquire_startup_lock(timeout=5, poll=0.01) is True
    assert lock.read_text(encoding="utf-8") == str(os.getpid())


def test_a_waiting_launch_takes_over_when_the_winner_never_comes_up(home, monkeypatch):
    monkeypatch.setattr(server, "running_instance_url", lambda: None)
    (home / "startup.lock").write_text("99999", encoding="utf-8")  # fresh lock, but its owner hangs
    assert server.acquire_startup_lock(timeout=0.3, poll=0.05) is True


def test_release_only_removes_our_own_lock(home):
    import os
    lock = home / "startup.lock"
    lock.write_text("not-our-pid", encoding="utf-8")
    server.release_startup_lock()
    assert lock.exists()
    lock.write_text(str(os.getpid()), encoding="utf-8")
    server.release_startup_lock()
    assert not lock.exists()


def test_the_lock_is_released_even_when_startup_fails(home, monkeypatch):
    monkeypatch.setattr(server, "running_instance_url", lambda: None)

    def broken(*args, **kwargs):
        raise RuntimeError("could not load config")
    monkeypatch.setattr(server, "App", broken)
    with pytest.raises(RuntimeError):
        server.run()
    assert not (home / "startup.lock").exists()


def test_a_launch_that_finds_the_lock_free_but_the_winner_running_hands_off(home, monkeypatch):
    # The winner registers itself and then releases the lock; a waiting launch must not claim the free lock
    # and start a second server.
    monkeypatch.setattr(server, "running_instance_url", lambda: "http://127.0.0.1:5000/?t=abc")
    assert not (home / "startup.lock").exists()            # the winner already released it...
    assert server.acquire_startup_lock(timeout=5, poll=0.01) is False   # ...but it is serving, so we must not start
    assert not (home / "startup.lock").exists()  # the claim we briefly held is released
