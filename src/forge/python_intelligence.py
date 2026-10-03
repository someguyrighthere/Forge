"""Read-only Python code intelligence through the Pyright language server."""
import ast
import atexit
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

REQUEST_TIMEOUT = 20
DIAGNOSTICS_TIMEOUT = 30
MAX_OUTPUT = 8000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SYMBOL_KINDS = {
    1: "File", 2: "Module", 3: "Namespace", 4: "Package", 5: "Class",
    6: "Method", 7: "Property", 8: "Field", 9: "Constructor", 10: "Enum",
    11: "Interface", 12: "Function", 13: "Variable", 14: "Constant",
    15: "String", 16: "Number", 17: "Boolean", 18: "Array", 19: "Object",
    20: "Key", 21: "Null", 22: "Enum member", 23: "Struct", 24: "Event",
    25: "Operator", 26: "Type parameter",
}


class PyrightConnectionError(RuntimeError):
    """The language server process or its LSP stream failed."""


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} characters]"


def _server_command() -> list[str]:
    executable = os.environ.get("PYRIGHT_LANGSERVER")
    if not executable:
        executable = shutil.which("pyright-langserver") or shutil.which("pyright-langserver.cmd")
    if not executable:
        raise RuntimeError(
            "Pyright's language server is not installed or is not on PATH. "
            "Install Node.js, run `npm install --global pyright`, then restart Forge. "
            "If needed, set PYRIGHT_LANGSERVER to the full path of pyright-langserver."
        )
    return [executable, "--stdio"]


def pyright_available() -> bool:
    """True when the Pyright language server can be found (used to show a setup hint in the window)."""
    try:
        _server_command()
        return True
    except RuntimeError:
        return False


def _uri_path(uri: str) -> Path:
    value = unquote(urlparse(uri).path)
    if os.name == "nt" and re.match(r"^/[A-Za-z]:/", value):
        value = value[1:]
    return Path(value)


def _location_text(location: dict) -> str:
    uri = location.get("uri", "")
    path = _uri_path(uri) if uri else Path("(unknown file)")
    position = location.get("range", {}).get("start", {})
    return f"{path}:{position.get('line', 0) + 1}:{position.get('character', 0) + 1}"


class _PyrightSession:
    def __init__(self, root: Path):
        command = _server_command()
        if os.name == "nt" and command[0].lower().endswith((".cmd", ".bat")):
            args: str | list[str] = subprocess.list2cmdline(command)
            use_shell = True
        else:
            args, use_shell = command, False
        self.process = subprocess.Popen(
            args, shell=use_shell, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0, creationflags=NO_WINDOW)
        assert self.process.stdin is not None and self.process.stdout is not None
        self._stdin, self._stdout = self.process.stdin, self.process.stdout
        self._messages: queue.Queue = queue.Queue()
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._documents: dict[str, tuple[int, str]] = {}
        self._reader = threading.Thread(target=self._read_messages, daemon=True)
        self._reader.start()
        try:
            root_uri = root.as_uri()
            self.request("initialize", {
                "processId": os.getpid(),
                "rootUri": root_uri,
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "textDocument": {
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                        "references": {"dynamicRegistration": False},
                        "definition": {"linkSupport": False},
                        "rename": {"prepareSupport": True},
                        "typeDefinition": {"linkSupport": False},
                        "callHierarchy": {"dynamicRegistration": False},
                    },
                    "workspace": {"workspaceFolders": True},
                },
                "workspaceFolders": [{"uri": root_uri, "name": root.name}],
            })
            self.notify("initialized", {})
        except Exception:
            self.close()
            raise

    def _read_messages(self) -> None:
        try:
            while True:
                header = self._stdout.readline()
                if not header:
                    break
                headers = {}
                while header.strip():
                    key, separator, value = header.decode("ascii").partition(":")
                    if separator:
                        headers[key.lower()] = value.strip()
                    header = self._stdout.readline()
                    if not header:
                        raise RuntimeError("Pyright closed its output unexpectedly.")
                length = int(headers["content-length"])
                body = bytearray()
                while len(body) < length:
                    chunk = self._stdout.read(length - len(body))
                    if not chunk:
                        break
                    body.extend(chunk)
                if len(body) != length:
                    raise RuntimeError("Pyright returned an incomplete language-server message.")
                self._messages.put(json.loads(body.decode("utf-8")))
        except Exception as error:
            self._messages.put({"_reader_error": str(error)})
        else:
            self._messages.put({"_reader_error": "Pyright closed its output unexpectedly."})

    def _send(self, message: dict) -> None:
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        framed = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body
        with self._write_lock:
            if self.process.poll() is not None:
                raise PyrightConnectionError("Pyright's language server exited unexpectedly.")
            self._stdin.write(framed)
            self._stdin.flush()

    def _receive(self, timeout: float) -> dict:
        try:
            message = self._messages.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError("Pyright's language server did not respond in time.") from error
        if "_reader_error" in message:
            raise PyrightConnectionError(f"Could not read Pyright's response: {message['_reader_error']}")
        return message

    def request(self, method: str, params: dict, timeout: float = REQUEST_TIMEOUT):
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(max(0, deadline - time.monotonic()))
            if "method" in message:
                self._handle_incoming(message)
            elif message.get("id") == request_id:
                if "error" in message:
                    error = message["error"]
                    raise RuntimeError(f"Pyright {method} failed: {error.get('message', error)}")
                return message.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def open_document(self, path: Path, text: str) -> str:
        uri = path.as_uri()
        current = self._documents.get(uri)
        if current is None:
            self.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": "python", "version": 1, "text": text}
            })
            self._documents[uri] = (1, text)
        elif current[1] != text:
            version = current[0] + 1
            self.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": text}],
            })
            self._documents[uri] = (version, text)
        return uri

    def document_diagnostics(self, uri: str) -> list[dict]:
        result = self.request("textDocument/diagnostic", {"textDocument": {"uri": uri}},
                              timeout=DIAGNOSTICS_TIMEOUT)
        return (result or {}).get("items", [])

    def _handle_incoming(self, message: dict) -> None:
        """Ignore server notifications; answer server-to-client requests."""
        if "id" not in message:
            return
        params = message.get("params") or {}
        # Pyright waits for replies to these (e.g. workspace/configuration) before analysing.
        result = [{} for _ in params.get("items", [])] if message["method"] == "workspace/configuration" else None
        self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                self.notify("exit", {})
            except (OSError, RuntimeError):
                pass
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


_SESSION: _PyrightSession | None = None
_SESSION_ROOT: Path | None = None
_SESSION_LOCK = threading.RLock()


def _close_session() -> None:
    global _SESSION, _SESSION_ROOT
    with _SESSION_LOCK:
        if _SESSION is not None:
            _SESSION.close()
            _SESSION = None
            _SESSION_ROOT = None


atexit.register(_close_session)


def _open(path: str) -> tuple[Path, str]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise ValueError(f"Python source file does not exist: {file_path}")
    if file_path.suffix.lower() != ".py":
        raise ValueError(f"Expected a .py source file, got: {file_path}")
    return file_path, file_path.read_text(encoding="utf-8", errors="replace")


def _with_document(path: str, action):
    global _SESSION, _SESSION_ROOT
    file_path, source = _open(path)
    root = Path.cwd().resolve()
    with _SESSION_LOCK:
        if _SESSION is None or _SESSION_ROOT != root or _SESSION.process.poll() is not None:
            _close_session()
            _SESSION = _PyrightSession(root)
            _SESSION_ROOT = root
        try:
            uri = _SESSION.open_document(file_path, source)
            return action(_SESSION, uri)
        except (TimeoutError, PyrightConnectionError):
            _close_session()
            raise


def _position(line: int | None, column: int | None) -> dict:
    if not line or not column or line < 1 or column < 1:
        raise ValueError("Give the symbol's name (symbol=...) or a 1-based line and column.")
    return {"line": line - 1, "character": column - 1}


def _locator(line: int | None, column: int | None, symbol: str | None):
    """Return a function that finds the target position in an open document.

    Local models are poor at counting columns, so a symbol name is accepted instead of line/column.
    With a name, an explicit line restricts the search to that line; otherwise a def/class of that
    name wins over other occurrences.
    """
    if not symbol:
        position = _position(line, column)
        return lambda session, uri: position
    name = symbol.strip().split(".")[-1]
    if not name.isidentifier():
        raise ValueError(f"{symbol!r} is not a valid Python symbol name.")
    word = re.compile(rf"\b{re.escape(name)}\b")
    definition = re.compile(rf"^\s*(?:async\s+def|def|class)\s+({re.escape(name)})\b")

    def locate(session, uri):
        lines = session._documents[uri][1].splitlines()
        if line and 1 <= line <= len(lines):
            match = word.search(lines[line - 1])
            if match:
                return {"line": line - 1, "character": match.start()}
        else:
            for index, text in enumerate(lines):
                match = definition.match(text)
                if match:
                    return {"line": index, "character": match.start(1)}
            for index, text in enumerate(lines):
                match = None if text.lstrip().startswith("#") else word.search(text)
                if match:
                    return {"line": index, "character": match.start()}
        raise ValueError(f"Could not find the symbol {name!r} in that file" + (f" on line {line}." if line else "."))
    return locate


def _format_symbols(symbols: list, indent: int = 0) -> list[str]:
    lines = []
    for symbol in symbols or []:
        location = symbol.get("range") or symbol.get("location", {}).get("range", {})
        start = location.get("start", {}).get("line", 0) + 1
        kind = SYMBOL_KINDS.get(symbol.get("kind"), "Symbol")
        lines.append(f"{'  ' * indent}{kind} {symbol.get('name', '(unnamed)')} - line {start}")
        lines.extend(_format_symbols(symbol.get("children", []), indent + 1))
    return lines


def python_symbols(path: str) -> str:
    """List Python symbols and their source locations using Pyright."""
    def inspect(session, uri):
        result = session.request("textDocument/documentSymbol", {"textDocument": {"uri": uri}})
        return _clip("\n".join(_format_symbols(result)) or "No symbols found.")
    return _with_document(path, inspect)


def python_hover(path: str, line: int | None = None, column: int | None = None, symbol: str | None = None) -> str:
    """Show Pyright's inferred type and documentation at a symbol or 1-based source position."""
    locate = _locator(line, column, symbol)

    def inspect(session, uri):
        position = locate(session, uri)
        result = session.request("textDocument/hover", {
            "textDocument": {"uri": uri}, "position": position
        })
        if not result:
            return "Pyright found no hover information at that position."
        contents = result.get("contents", "")
        if isinstance(contents, dict):
            text = contents.get("value", "")
        elif isinstance(contents, list):
            text = "\n".join(item.get("value", "") if isinstance(item, dict) else str(item)
                             for item in contents)
        else:
            text = str(contents)
        return _clip(text.strip() or "Pyright returned empty hover information.")
    return _with_document(path, inspect)


def _source_line(location: dict) -> str:
    path = _uri_path(location.get("uri", ""))
    line = location.get("range", {}).get("start", {}).get("line", 0)
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for number, text in enumerate(handle):
                if number == line:
                    return text.strip()[:200]
    except OSError:
        pass
    return ""


def python_definition(path: str, line: int | None = None, column: int | None = None, kind: str = "definition",
                      symbol: str | None = None) -> str:
    """Go to the definition (or the type's definition) of a symbol or 1-based source position."""
    if kind not in {"definition", "type"}:
        raise ValueError("kind must be definition or type.")
    locate = _locator(line, column, symbol)
    method = "textDocument/typeDefinition" if kind == "type" else "textDocument/definition"

    def inspect(session, uri):
        position = locate(session, uri)
        result = session.request(method, {"textDocument": {"uri": uri}, "position": position})
        if isinstance(result, dict):
            result = [result]
        lines = []
        for item in (result or [])[:50]:
            # Servers may answer with LocationLink objects instead of Location objects.
            location = ({"uri": item["targetUri"], "range": item.get("targetSelectionRange", item["targetRange"])}
                        if "targetUri" in item else item)
            snippet = _source_line(location)
            lines.append(_location_text(location) + (f"  {snippet}" if snippet else ""))
        return _clip("\n".join(lines) or f"Pyright found no {kind} for that position.")
    return _with_document(path, inspect)


def python_module_usage(path: str, module: str) -> str:
    """List the names a Python file uses from another module, with line numbers."""
    file_path, source = _open(path)
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return f"Error: {file_path.name} has a syntax error: {error}"
    target = module.strip().removesuffix(".py").replace("\\", ".").replace("/", ".")
    short = target.split(".")[-1]
    aliases: set[str] = set()          # local names bound to the module object itself
    direct: dict[str, str] = {}        # local name -> name imported with "from module import name"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.name == target:
                    aliases.add(item.asname or item.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            origin = node.module or ""
            for item in node.names:
                if origin == target or origin.endswith("." + target):
                    direct[item.asname or item.name] = item.name
                elif item.name == short:
                    aliases.add(item.asname or item.name)  # "from package import module"
    if not aliases and not direct:
        return f"{file_path.name} does not import {target!r}."
    uses: dict[str, set[int]] = {name: set() for name in direct.values()}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases:
            uses.setdefault(node.attr, set()).add(node.lineno)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in direct:
            uses[direct[node.id]].add(node.lineno)
    lines = [f"{file_path.name} uses {len(uses)} name(s) from {target}:"]
    for name in sorted(uses):
        where = sorted(uses[name])
        lines.append(f"  {name}: " + (", ".join(f"line {n}" for n in where[:8]) + (" ..." if len(where) > 8 else "")
                                      if where else "imported but never used"))
    return _clip("\n".join(lines))


def python_references(path: str, line: int | None = None, column: int | None = None,
                      include_declaration: bool = True, symbol: str | None = None) -> str:
    """Find Python references to a symbol or the symbol at a 1-based source position."""
    locate = _locator(line, column, symbol)

    def inspect(session, uri):
        position = locate(session, uri)
        result = session.request("textDocument/references", {
            "textDocument": {"uri": uri}, "position": position,
            "context": {"includeDeclaration": include_declaration},
        })
        locations = result or []
        return _clip("\n".join(_location_text(item) for item in locations[:200])
                     or "Pyright found no references.")
    return _with_document(path, inspect)


def python_rename_impact(path: str, line: int | None = None, column: int | None = None,
                         symbol: str | None = None) -> str:
    """Preview every location a rename of a symbol or position would change. Edits nothing."""
    locate = _locator(line, column, symbol)

    def inspect(session, uri):
        params = {"textDocument": {"uri": uri}, "position": locate(session, uri)}
        try:
            prepared = session.request("textDocument/prepareRename", params)
        except RuntimeError:
            prepared = None
        if not prepared:
            return "Pyright cannot rename the symbol at that position (it may be a keyword, literal, or library symbol)."
        name = prepared.get("placeholder") if isinstance(prepared, dict) else None
        if not name:
            span = prepared.get("range", prepared)
            text = session._documents[uri][1].splitlines()
            if span["start"]["line"] == span["end"]["line"] and span["start"]["line"] < len(text):
                name = text[span["start"]["line"]][span["start"]["character"]:span["end"]["character"]]
        edit = session.request("textDocument/rename", {**params, "newName": "__forge_rename_preview__"}) or {}
        changes: dict[str, list[dict]] = dict(edit.get("changes") or {})
        for change in edit.get("documentChanges") or []:
            if "textDocument" in change:
                changes.setdefault(change["textDocument"]["uri"], []).extend(change.get("edits", []))
        total = sum(len(edits) for edits in changes.values())
        if not total:
            return "Pyright found nothing to rename at that position."
        label = f"`{name}`" if name else "the symbol"
        lines = [f"Renaming {label} would change {total} occurrence(s) in {len(changes)} file(s):"]
        for file_uri, edits in sorted(changes.items(), key=lambda item: str(_uri_path(item[0]))):
            lines.append(f"{_uri_path(file_uri)} ({len(edits)})")
            for edit_item in sorted(edits, key=lambda e: (e["range"]["start"]["line"], e["range"]["start"]["character"]))[:50]:
                start = edit_item["range"]["start"]
                lines.append(f"  {start['line'] + 1}:{start['character'] + 1}")
        return _clip("\n".join(lines))
    return _with_document(path, inspect)


def python_call_hierarchy(path: str, line: int | None = None, column: int | None = None,
                          direction: str = "both", symbol: str | None = None) -> str:
    """List callers and/or callees for a symbol or 1-based source position."""
    if direction not in {"incoming", "outgoing", "both"}:
        raise ValueError("direction must be incoming, outgoing, or both.")
    locate = _locator(line, column, symbol)

    def inspect(session, uri):
        params = {"textDocument": {"uri": uri}, "position": locate(session, uri)}
        items = session.request("textDocument/prepareCallHierarchy", params) or []
        if not items:
            return "Pyright found no call-hierarchy item at that position."
        item = items[0]
        output = []
        for label, method, call_direction, key in (
            ("Callers", "callHierarchy/incomingCalls", "incoming", "from"),
            ("Callees", "callHierarchy/outgoingCalls", "outgoing", "to"),
        ):
            if direction != "both" and direction != call_direction:
                continue
            calls = session.request(method, {"item": item}) or []
            output.append(f"{label}:")
            for call in calls[:100]:
                other = call.get(key, {})
                ranges = call.get("fromRanges") or []
                if key == "from" and ranges:
                    location = {"uri": other.get("uri", ""), "range": ranges[0]}
                else:
                    location = {"uri": other.get("uri", ""), "range": other.get("selectionRange", {})}
                output.append(f"  {other.get('name', '(unnamed)')} - {_location_text(location)}")
            if not calls:
                output.append("  (none)")
        return _clip("\n".join(output))
    return _with_document(path, inspect)


def python_diagnostics(path: str) -> str:
    """Get Pyright diagnostics for a Python file."""
    return _with_document(
        path, lambda session, uri: _format_diagnostics(session.document_diagnostics(uri))
    )


def _format_diagnostics(diagnostics: list[dict]) -> str:
    if not diagnostics:
        return "Pyright reported no diagnostics."
    output = []
    for item in diagnostics[:100]:
        start = item.get("range", {}).get("start", {})
        severity = {1: "error", 2: "warning", 3: "information", 4: "hint"}.get(
            item.get("severity") or 0, "diagnostic")
        code = item.get("code")
        identifier = f" [{code}]" if code else ""
        output.append(
            f"{severity}{identifier} {start.get('line', 0) + 1}:"
            f"{start.get('character', 0) + 1}: {item.get('message', '').replace(chr(0xa0), ' ')}"
        )
    return _clip("\n".join(output))
