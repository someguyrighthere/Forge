import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from forge import llm, tools


def test_blocklist():
    for bad in ["rm -rf /", "Remove-Item C:\\ -Recurse -Force", "rm -rf ~", "del C:\\*",
                "git reset --hard", "cd x; rm -rf /", "shutdown /s", "Remove-Item $env:USERPROFILE -Recurse"]:
        assert tools.is_blocked(bad), bad
    for ok in ["git status", "ls -la", "rm -rf build", "Remove-Item .\\dist -Recurse", "echo format", "pytest -q"]:
        assert not tools.is_blocked(ok), ok


def test_user_deny():
    tools.USER_DENY[:] = [r"^npm publish"]
    try:
        assert tools.is_blocked("npm publish")
    finally:
        tools.USER_DENY.clear()


class WorkspaceRestrictionTests(unittest.TestCase):
    def test_resolve_rejects_paths_outside_the_configured_workspace(self):
        root = Path.cwd().resolve()
        with patch.object(tools, "WORKSPACE_ROOT", root):
            self.assertEqual(tools.resolve("inside.txt"), root / "inside.txt")
            with self.assertRaisesRegex(ValueError, "outside the allowed workspace"):
                tools.resolve(str(root.parent / "outside.txt"))

    def test_grep_rejects_a_file_that_resolves_outside_the_workspace(self):
        root = Path.cwd().resolve()
        with tempfile.TemporaryDirectory() as temp_dir:
            outside = Path(temp_dir) / "outside.txt"
            outside.write_text("secret", encoding="utf-8")
            with (
                patch.object(tools, "WORKSPACE_ROOT", root),
                patch.object(tools, "_walk", return_value=iter([outside])),
            ):
                result = tools.grep("secret", str(root))

        self.assertIn("outside the allowed workspace", result)

    def test_agent_exposes_only_workspace_file_and_todo_tools(self):
        from forge import agent, ui

        available = set()

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def update(self, *args):
                pass

            def first_token(self):
                pass

            def finish(self, *args):
                pass

        class View:
            def stream(self):
                return Stream()

        def chat(_model, _messages, schemas, _ctx, _think, **kwargs):
            available.update(schema["function"]["name"] for schema in schemas)
            return "done", [], {"in": 1, "out": 1}, False

        messages = [{"role": "system", "content": ""}, {"role": "user", "content": "build"}]
        cfg = {"model": "test", "num_ctx": 1000, "think": False, "max_steps": 1}
        with (
            patch.object(tools, "WORKSPACE_ROOT", Path.cwd()),
            patch.object(agent.llm, "CANCEL", threading.Event()),
            patch.object(agent.llm, "chat", side_effect=chat),
        ):
            agent.run_turn(cfg, messages, ui.Approver("ask", []), View())

        self.assertEqual(available, tools.WORKSPACE_TOOLS)



def test_edit_and_undo(tmp_path):
    f = tmp_path / "a.txt"
    tools.write_file(str(f), "one\ntwo\ntwo\n")
    assert "matches 2" in tools.edit_file(str(f), "two", "x")
    assert "Edited" in tools.edit_file(str(f), "one", "uno")
    assert "Edited" in tools.edit_file(str(f), "two", "x", replace_all=True)
    assert f.read_text() == "uno\nx\nx\n"
    tools.undo_last()
    assert f.read_text() == "uno\ntwo\ntwo\n"
    tools.undo_last()
    tools.undo_last()
    assert not f.exists()


def test_read_grep_glob(tmp_path):
    (tmp_path / "m.py").write_text("def hi():\n    pass\n")
    assert "1: def hi" in tools.read_file(str(tmp_path / "m.py"))
    assert "m.py:1" in tools.grep(r"def hi", str(tmp_path))
    assert "m.py" in tools.glob_files("*.py", str(tmp_path))


def test_run_command():
    assert "exit code 0" in tools.run_command("echo hello")
    assert "hello" in tools.run_command("echo hello")
    assert "blocked" in tools.run_command("git reset --hard")


def test_text_tool_call_parser():
    known = {"read_file", "run_command"}
    text = json.dumps({"name": "read_file", "arguments": {"path": "x"}})
    calls, rest = llm.parse_text_tool_calls(text, known)
    assert calls[0]["function"]["name"] == "read_file" and rest == ""
    assert llm.parse_text_tool_calls("```json\n" + text + "\n```", known)[0]
    calls, rest = llm.parse_text_tool_calls('Next, I run it.\n\n{"name": "run_command", "arguments": {"command":"ls"}}', known)
    assert calls[0]["function"]["name"] == "run_command" and rest == "Next, I run it."
    assert not llm.parse_text_tool_calls('{"name": "nope", "arguments": {}}', known)[0]
    assert not llm.parse_text_tool_calls("just words {not json}", known)[0]
    assert llm.visible_text('hello {"name": "x"') == "hello "


def test_hub_replays_log_to_new_subscribers():
    from forge.server import Hub
    hub = Hub()
    hub.emit("user", text="hi")
    hub.emit("stream", persist=False, id="x", text="partial")
    q = hub.subscribe()
    assert [q.get_nowait()["type"] for _ in range(2)] == ["reset", "user"]
    assert q.empty()


def test_replay_pairs_calls_and_results():
    from forge.server import replay
    msgs = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
        {"role": "tool", "tool_name": "read_file", "content": "print(1)"},
        {"role": "assistant", "content": "done"},
    ]
    ev = replay(msgs)
    assert [e["type"] for e in ev] == ["user", "call", "result", "assistant"]
    assert ev[1]["id"] == ev[2]["id"]


def test_gui_approver_blocks_until_answer():
    import threading
    from forge.server import GuiApprover, Hub
    hub = Hub()
    ap = GuiApprover(hub, "ask", [])
    result = []
    t = threading.Thread(target=lambda: result.append(ap.ask("run_command", {"command": "echo hi"})))
    t.start()
    for _ in range(100):
        if ap.pending:
            break
        threading.Event().wait(0.02)
    ap.answer(next(iter(ap.pending)), "allow")
    t.join(5)
    assert result == [True]


def test_update_version_compare_and_check(monkeypatch):
    import io, json
    from forge import updater
    assert updater.parse("1.10.0") > updater.parse("1.9.9")
    rel = {"tag_name": "v9.0.0", "body": "n", "assets": [{"name": "Forge-Setup-9.0.0.exe", "browser_download_url": "https://github.com/x/y/a.exe", "digest": "sha256:ab"}]}
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(json.dumps(rel).encode()))
    info = updater.check()
    assert info and info["version"] == "9.0.0" and info["sha256"] == "ab"
    rel["tag_name"] = "v0.0.1"
    assert updater.check() is None


def test_update_refuses_foreign_host():
    import pytest
    from forge import updater
    with pytest.raises(RuntimeError):
        updater.download({"url": "https://evil.example/x.exe", "version": "9", "sha256": ""})
