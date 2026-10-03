import queue

from forge.server import Hub, watch_window


class Clock:
    """A fake clock whose sleep() advances time and fires scripted window events."""

    def __init__(self, hub, script):
        self.t, self.hub, self.script = 0.0, hub, sorted(script)

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds
        while self.script and self.script[0][0] <= self.t:
            _, action = self.script.pop(0)
            action(self.hub)


def connect(hub):
    hub.subs.append(queue.Queue())


def disconnect(hub):
    hub.subs.clear()


def run(script, **kwargs):
    hub = Hub()
    clock = Clock(hub, script)
    watch_window(hub, now=clock.now, sleep=clock.sleep, **kwargs)
    return clock.t


def test_returns_shortly_after_the_window_disconnects():
    finished = run([(2, connect), (60, disconnect)], grace=10, connect_timeout=45)
    assert 70 <= finished < 71  # disconnect at 60s plus the 10s grace period


def test_keeps_running_while_the_window_stays_connected():
    finished = run([(2, connect), (600, disconnect)], grace=10)
    assert finished >= 610


def test_a_brief_reconnect_such_as_a_page_reload_does_not_end_the_session():
    finished = run([(2, connect), (30, disconnect), (35, connect), (100, disconnect)], grace=10)
    assert 110 <= finished < 111


def test_gives_up_when_no_window_ever_connects():
    finished = run([], grace=10, connect_timeout=45)
    assert 45 <= finished < 46
