import threading
from contextlib import contextmanager

from forge import agent


def test_user_turn_continues_past_25_tool_rounds(monkeypatch):
    calls = 0
    notices = []

    def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls <= 26:
            tool_calls = [{"function": {"name": "read_file", "arguments": "{}"}}]
            return "", tool_calls, {"in": 1, "out": 1}, True
        return "Finished.", [], {"in": 1, "out": 1}, True

    class Stream:
        def update(self, text):
            pass

        def first_token(self):
            pass

        def finish(self, content, started):
            pass

    class View:
        @contextmanager
        def stream(self):
            yield Stream()

        def call(self, *args):
            pass

        def result(self, *args):
            pass

        def notice(self, text, level="info"):
            notices.append((text, level))

    class Approver:
        mode = "auto"

        def allow(self, name, args):
            return True

    monkeypatch.setattr(agent.llm, "CANCEL", threading.Event())
    monkeypatch.setattr(agent.llm, "chat", chat)
    monkeypatch.setitem(agent.tools.IMPLS, "read_file", lambda: "file contents")

    messages = [{"role": "system", "content": "test"}, {"role": "user", "content": "inspect"}]
    agent.run_turn({"num_ctx": 16384, "think": False, "model": "test-model", "max_steps": 25},
                   messages, Approver(), View())

    assert calls == 27
    assert messages[-1] == {"role": "assistant", "content": "Finished."}
    assert not notices
