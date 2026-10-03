import os
import subprocess
import sys

import pytest

from forge import python_intelligence, tools


def test_python_intelligence_tools_are_registered_read_only():
    names = {"python_symbols", "python_hover", "python_definition", "python_references", "python_rename_impact",
             "python_call_hierarchy", "python_diagnostics"}
    schema_names = {tool["function"]["name"] for tool in tools.TOOLS}
    assert names <= schema_names
    assert names <= tools.READ_ONLY
    assert names <= tools.IMPLS.keys()


def test_server_setup_error_explains_install(monkeypatch):
    monkeypatch.delenv("PYRIGHT_LANGSERVER", raising=False)
    monkeypatch.setattr(python_intelligence.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError, match="npm install --global pyright"):
        python_intelligence._server_command()


def test_locator_finds_symbols_by_name():
    source = ("# average is documented here\n"
              "import statistics\n"
              "def helper():\n    return average([1])\n"
              "async def average(values):\n    return sum(values)\n")
    session = type("Session", (), {"_documents": {"u": (1, source)}})()
    find = lambda **kw: python_intelligence._locator(kw.get("line"), None, kw["symbol"])(session, "u")
    assert find(symbol="average") == {"line": 4, "character": 10}            # the definition wins
    assert find(symbol="average", line=4) == {"line": 3, "character": 11}      # an explicit line is honoured
    assert find(symbol="Anything.helper") == {"line": 2, "character": 4}       # dotted names use the last part
    with pytest.raises(ValueError, match="Could not find"):
        find(symbol="missing")
    with pytest.raises(ValueError, match="not a valid"):
        python_intelligence._locator(None, None, "not valid!")
    with pytest.raises(ValueError, match="symbol=..."):
        python_intelligence._locator(None, None, None)


def test_python_position_uses_one_based_coordinates():
    assert python_intelligence._position(1, 1) == {"line": 0, "character": 0}
    with pytest.raises(ValueError, match="1-based"):
        python_intelligence._position(0, 1)
    with pytest.raises(ValueError, match="symbol=..."):
        python_intelligence._position(None, None)


def test_python_diagnostic_formatting():
    result = python_intelligence._format_diagnostics([{
        "severity": 1,
        "code": "reportUndefinedVariable",
        "range": {"start": {"line": 2, "character": 4}},
        "message": '"missing" is not defined',
    }])
    assert result == 'error [reportUndefinedVariable] 3:5: "missing" is not defined'
    assert python_intelligence._format_diagnostics([]) == "Pyright reported no diagnostics."


def test_python_tool_rejects_non_python_file(tmp_path):
    source = tmp_path / "notes.txt"
    source.write_text("not Python", encoding="utf-8")
    with pytest.raises(ValueError, match="Expected a .py source file"):
        python_intelligence._open(str(source))


def test_language_server_tools_and_document_updates(tmp_path, monkeypatch, request):
    server = tmp_path / "fake_pyright.py"
    server.write_text(
        r'''
import json
import sys

uri = ""
version = 0
configured = False

def receive():
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    headers = {}
    while line.strip():
        key, _, value = line.decode("ascii").partition(":")
        headers[key.lower()] = value.strip()
        line = sys.stdin.buffer.readline()
    length = int(headers["content-length"])
    body = bytearray()
    while len(body) < length:
        body.extend(sys.stdin.buffer.read(length - len(body)))
    return json.loads(body)

def send(message):
    body = json.dumps(message).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()

while True:
    message = receive()
    if message is None or message.get("method") == "exit":
        break
    method = message.get("method")
    if method in {"textDocument/didOpen", "textDocument/didChange"}:
        document = message["params"]["textDocument"]
        uri, version = document["uri"], document.get("version")
        # Like the real server, ask the client for configuration; it only analyses once answered.
        send({"jsonrpc": "2.0", "id": 9001, "method": "workspace/configuration",
              "params": {"items": [{"section": "python"}]}})
    elif method is None and message.get("id") == 9001:
        configured = message.get("result") == [{}]
    elif "id" in message:
        if method == "initialize":
            result = {}
        elif method == "textDocument/diagnostic":
            text = f"version {version}" if configured else "client never answered workspace/configuration"
            result = {"kind": "full", "items": [{
                "severity": 1, "code": "reportTest", "message": text,
                "range": {"start": {"line": 0, "character": 0}}}]}
        elif method == "textDocument/documentSymbol":
            result = [{"name": "example", "kind": 12, "range": {"start": {"line": 0}}}]
        elif method == "textDocument/hover":
            result = {"contents": {"kind": "markdown", "value": "```python\n(value: str) -> int\n```"}}
        elif method == "textDocument/definition":
            result = {"uri": uri, "range": {"start": {"line": 0, "character": 4}}}
        elif method == "textDocument/typeDefinition":
            result = [{"targetUri": uri, "targetRange": {"start": {"line": 1}},
                       "targetSelectionRange": {"start": {"line": 1, "character": 11}}}]
        elif method == "textDocument/prepareRename":
            result = {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 11}}
        elif method == "textDocument/rename":
            assert message["params"]["newName"] == "__forge_rename_preview__"
            result = {"documentChanges": [{"textDocument": {"uri": uri, "version": 1}, "edits": [
                {"range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 11}}, "newText": "x"},
                {"range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 11}}, "newText": "x"}]}]}
        elif method == "textDocument/references":
            result = [{"uri": uri, "range": {"start": {"line": 2, "character": 4}}}]
        elif method == "textDocument/prepareCallHierarchy":
            result = [{"name": "example", "uri": uri, "range": {}, "selectionRange": {}}]
        elif method == "callHierarchy/incomingCalls":
            result = [{"from": {"name": "caller", "uri": uri}, "fromRanges": [{"start": {"line": 4}}]}]
        elif method == "callHierarchy/outgoingCalls":
            result = [{"to": {"name": "callee", "uri": uri, "selectionRange": {"start": {"line": 7}}}, "fromRanges": [{"start": {"line": 5}}]}]
        else:
            raise RuntimeError(f"Unexpected request: {method}")
        send({"jsonrpc": "2.0", "id": message["id"], "result": result})
''',
        encoding="utf-8",
    )
    source = tmp_path / "sample.py"
    source.write_text("def example(value):\n    return value\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    command = [sys.executable, str(server), "--stdio"]
    if os.name == "nt":
        launcher = tmp_path / "pyright-langserver.cmd"
        launcher.write_text(
            "@echo off\r\n" + subprocess.list2cmdline([sys.executable, str(server)]) + " %*\r\n",
            encoding="utf-8",
        )
        command = [str(launcher), "--stdio"]
    monkeypatch.setattr(
        python_intelligence, "_server_command", lambda: command,
    )
    request.addfinalizer(python_intelligence._close_session)

    assert "Function example - line 1" in python_intelligence.python_symbols(str(source))
    assert "(value: str) -> int" in python_intelligence.python_hover(str(source), 1, 5)
    impact = python_intelligence.python_rename_impact(str(source), 1, 5)
    assert "Renaming `example` would change 2 occurrence(s) in 1 file(s)" in impact
    assert impact.index("1:5") < impact.index("3:5")  # edits are listed in source order
    assert f"{source}:3:5" in python_intelligence.python_references(str(source), 1, 5)
    definition = python_intelligence.python_definition(str(source), 2, 12)
    assert f"{source}:1:5  def example(value):" in definition
    type_definition = python_intelligence.python_definition(str(source), 2, 12, "type")
    assert f"{source}:2:12  return value" in type_definition
    with pytest.raises(ValueError, match="kind must be"):
        python_intelligence.python_definition(str(source), 2, 12, "implementation")
    incoming = python_intelligence.python_call_hierarchy(str(source), 1, 5, "incoming")
    assert "Callers:" in incoming and "caller" in incoming and "Callees:" not in incoming
    outgoing = python_intelligence.python_call_hierarchy(str(source), 1, 5, "outgoing")
    assert "callee" in outgoing and f"{source}:8:1" in outgoing and "Callers:" not in outgoing
    assert "error [reportTest] 1:1: version 1" == python_intelligence.python_diagnostics(str(source))

    source.write_text("def example(value: int):\n    return value\n", encoding="utf-8")
    assert "version 2" in python_intelligence.python_diagnostics(str(source))
