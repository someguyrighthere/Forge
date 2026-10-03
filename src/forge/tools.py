"""Tools the model can call, plus the safety checks around them."""
import html
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from forge.browser import browser_read
from forge.browser_session import browser_click, browser_close, browser_open, browser_type
from forge.github import github_actions, github_issues, github_prs
from forge.notebook import apply_edit as _apply_notebook_edit
from forge.notebook import notebook_read, outline
from forge.python_debug import python_debug
from forge.python_stepper import python_debugger
from forge.python_intelligence import (
    python_call_hierarchy,
    python_definition,
    python_diagnostics,
    python_hover,
    python_module_usage,
    python_references,
    python_rename_impact,
    python_symbols,
)

MAX_OUTPUT = 8000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".expo", ".mypy_cache", ".pytest_cache"}
USER_AGENT = "Mozilla/5.0 (forge-agent)"

# Extra regexes from config.toml; set by the CLI at startup.
USER_DENY: list[str] = []

BLOCKED_COMMANDS = [
    r"(?i)\brm\s+(-\w*\s+)*-\w*[rf]\w*\s+(/|~|\*|[a-z]:\\?)(\s|$)",
    r"(?i)\bremove-item\b.*-recurse.*\s([a-z]:\\?|~|\$env:userprofile\\?)\s*(-|$)",
    r"(?i)\b(diskpart|mkfs|dd\s+if=)\b",
    r"(?i)(^|[;&|]\s*)format(\.com)?\s+[a-z]:",
    r"(?i)\b(shutdown|restart-computer|stop-computer)\b",
    r"(?i)\breg\s+delete\b",
    r"(?i)git\s+push\b.*--force|git\s+reset\s+--hard",
]
DELETE_WORDS = {"rm", "del", "erase", "rd", "rmdir", "remove-item", "ri"}
PROTECTED = re.compile(
    r"^(~|[a-z]:[\\/]?\*?|/\*?|\$env:(userprofile|systemroot|windir|homepath)[\\/]?\*?|%(userprofile|systemroot)%[\\/]?\*?)$",
    re.I)


def clip(text: str, limit: int = MAX_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def resolve(path: str) -> Path:
    return Path(os.path.expanduser(path)).resolve()


def is_blocked(command: str) -> bool:
    patterns = BLOCKED_COMMANDS + USER_DENY
    if any(re.search(p, command) for p in patterns):
        return True
    for part in re.split(r"[;|&\n]+", command):
        tokens = [t.strip("\"'") for t in part.split()]
        if tokens and tokens[0].lower() in DELETE_WORDS and any(PROTECTED.match(t) for t in tokens[1:]):
            return True
    return False


# ------------------------------------------------------------ undo stack
UNDO: list[tuple[Path, str | None]] = []


def _remember(p: Path) -> None:
    UNDO.append((p, p.read_text(encoding="utf-8") if p.is_file() else None))
    del UNDO[:-50]


def undo_last() -> str:
    if not UNDO:
        return "Nothing to undo."
    p, old = UNDO.pop()
    if old is None:
        p.unlink(missing_ok=True)
        return f"Removed {p} (it did not exist before)."
    p.write_text(old, encoding="utf-8", newline="")
    return f"Restored {p}."


# ----------------------------------------------------------------- tools
def read_file(path: str, start_line: int = 1, end_line: int = 0) -> str:
    p = resolve(path)
    if not p.is_file():
        return f"Error: {p} is not a file."
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    start = max(int(start_line or 1), 1)
    end = int(end_line or len(lines))
    chunk = lines[start - 1:end]
    numbered = "\n".join(f"{i}: {line}" for i, line in enumerate(chunk, start))
    return clip(numbered) or "(empty file)"


def write_file(path: str, content: str) -> str:
    p = resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    _remember(p)
    p.write_text(content, encoding="utf-8", newline="")
    return f"Wrote {len(content)} characters to {p}."


def edit_file(path: str, old_string: str, new_string: str, replace_all: bool = False) -> str:
    p = resolve(path)
    if not p.is_file():
        return f"Error: {p} is not a file."
    text = p.read_text(encoding="utf-8")
    count = text.count(old_string)
    if not old_string:
        return "Error: old_string is empty."
    if count == 0:
        return "Error: old_string was not found. Read the file again and copy the text exactly."
    if count > 1 and not replace_all:
        return f"Error: old_string matches {count} places. Add surrounding lines to make it unique, or set replace_all."
    _remember(p)
    p.write_text(text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1),
                 encoding="utf-8", newline="")
    return f"Edited {p} ({count if replace_all else 1} replacement)."


def notebook_edit(path: str, action: str, index: int = 0, source: str = "", cell_type: str | None = None) -> str:
    p = resolve(path)
    if p.suffix.lower() != ".ipynb" or not p.is_file():
        return f"Error: {p} is not an existing .ipynb notebook."
    try:
        text, message = _apply_notebook_edit(p.read_text(encoding="utf-8"), action, index, source, cell_type)
    except ValueError as e:
        return f"Error: {e}"
    _remember(p)
    p.write_text(text, encoding="utf-8", newline="")
    return f"{message}\n{outline(text)}"


def list_dir(path: str = ".") -> str:
    p = resolve(path)
    if not p.is_dir():
        return f"Error: {p} is not a directory."
    entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    return "\n".join(f"{e.name}{'/' if e.is_dir() else ''}" for e in entries[:300]) or "(empty)"


def _walk(root: Path, pattern: str):
    for p in root.rglob(pattern):
        if not any(part in SKIP_DIRS for part in p.relative_to(root).parts):
            yield p


def glob_files(pattern: str, path: str = ".") -> str:
    root = resolve(path)
    found = []
    for p in _walk(root, pattern):
        found.append(str(p.relative_to(root)))
        if len(found) >= 200:
            break
    return "\n".join(found) or "No matches."


def grep(pattern: str, path: str = ".", file_glob: str = "*") -> str:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return f"Error: invalid regex: {e}"
    root = resolve(path)
    files = [root] if root.is_file() else (p for p in _walk(root, file_glob) if p.is_file())
    hits = []
    for f in files:
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{f}:{n}: {line.strip()[:200]}")
                    if len(hits) >= 100:
                        return "\n".join(hits) + "\n... (stopped at 100 matches)"
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(hits) or "No matches."


NO_WINDOW = 0x08000000 if os.name == "nt" else 0
RUNNING: dict[str, subprocess.Popen | None] = {"proc": None}


def kill_running() -> None:
    proc = RUNNING.get("proc")
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()


def run_command(command: str, timeout: int = 60) -> str:
    if is_blocked(command):
        return "Error: this command is blocked for safety. Ask the user to run it themselves if it is really needed."
    if os.name == "nt":
        prefix = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "
        shell = ["powershell", "-NoProfile", "-NonInteractive", "-Command", prefix + command]
    else:
        shell = ["bash", "-lc", command]
    proc = subprocess.Popen(shell, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    RUNNING["proc"] = proc
    try:
        stdout, stderr = proc.communicate(timeout=min(int(timeout or 60), 600))
    except subprocess.TimeoutExpired:
        kill_running()
        return f"Error: command timed out after {timeout}s."
    finally:
        RUNNING["proc"] = None
    out = (stdout + ("\n" + stderr if stderr else "")).strip()
    return clip(f"exit code {proc.returncode}\n{out}")


def _fetch(url: str, limit: int = 2_000_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limit).decode("utf-8", errors="replace")


def web_fetch(url: str) -> str:
    if not re.match(r"^https?://", url):
        return "Error: url must start with http:// or https://"
    try:
        raw = _fetch(url)
    except (urllib.error.URLError, TimeoutError) as e:
        return f"Error fetching {url}: {e}"
    raw = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", raw)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw))
    return clip(re.sub(r"\s+", " ", text).strip())


def web_search(query: str) -> str:
    try:
        raw = _fetch("https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query), 600_000)
    except (urllib.error.URLError, TimeoutError) as e:
        return f"Error searching: {e}"
    results = []
    for m in re.finditer(r'(?s)class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>', raw):
        link, title, snippet = m.groups()
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(link).query)
        link = qs.get("uddg", [link])[0]
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        results.append(f"{clean(title)}\n{link}\n{clean(snippet)}")
        if len(results) >= 6:
            break
    return "\n\n".join(results) or "No results."


TODOS: list[dict] = []


def todo(items: list) -> str:
    TODOS.clear()
    for item in items:
        if isinstance(item, dict):
            TODOS.append({"task": str(item.get("task", "")), "done": bool(item.get("done", False))})
        else:
            TODOS.append({"task": str(item), "done": False})
    return "\n".join(f"[{'x' if t['done'] else ' '}] {t['task']}" for t in TODOS) or "(empty list)"


# --------------------------------------------------------------- schemas
def schema(name, description, properties, required):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


S = {"type": "string"}
# Position-based Python tools take a symbol name (preferred: models are bad at counting columns)
# or an explicit 1-based line and column.
LOCATION = {"path": S, "symbol": S, "line": {"type": "integer"}, "column": {"type": "integer"}}
TARGET_HELP = ("Set symbol to the name (e.g. 'average' or 'Agent.run'); optionally add line to pick a specific "
               "occurrence. Only if you have exact coordinates, give 1-based line and column instead.")
TOOLS = [
    schema("read_file", "Read a text file with line numbers.",
           {"path": S, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
    schema("write_file", "Create or overwrite a file with the full content.",
           {"path": S, "content": S}, ["path", "content"]),
    schema("edit_file", "Replace one exact, unique piece of text in a file (or all of it with replace_all).",
           {"path": S, "old_string": S, "new_string": S, "replace_all": {"type": "boolean"}},
           ["path", "old_string", "new_string"]),
    schema("list_dir", "List the files in a directory.", {"path": S}, []),
    schema("glob_files", "Find files by glob pattern such as **/*.py.", {"pattern": S, "path": S}, ["pattern"]),
    schema("grep", "Search file contents with a regular expression.",
           {"pattern": S, "path": S, "file_glob": S}, ["pattern"]),
    schema("python_symbols", "List Python functions, classes and other symbols with their source lines using Pyright.",
           {"path": S}, ["path"]),
    schema("python_hover", "Show Pyright's inferred type and documentation for a Python symbol. " + TARGET_HELP,
           dict(LOCATION), ["path"]),
    schema("python_definition", "Go to where a Python symbol is defined (kind=definition) or where its type is "
                                "defined (kind=type), using Pyright. " + TARGET_HELP,
           {**LOCATION, "kind": {"type": "string", "enum": ["definition", "type"]}}, ["path"]),
    schema("python_references", "Find every Pyright-resolved reference to a Python symbol across the project. "
                                + TARGET_HELP,
           {**LOCATION, "include_declaration": {"type": "boolean"}}, ["path"]),
    schema("python_rename_impact", "Preview every file and location that renaming a Python symbol would change, "
                                   "using Pyright. Makes no edits. " + TARGET_HELP,
           dict(LOCATION), ["path"]),
    schema("python_call_hierarchy", "List Pyright-resolved callers and/or callees of a Python function or method. "
                                    + TARGET_HELP,
           {**LOCATION, "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"]}}, ["path"]),
    schema("python_module_usage", "List exactly which names a Python file uses from another module, with line numbers "
                                  "(e.g. path='tool_workflow.py', module='project_workflow'). Use this to answer 'how does A use B'.",
           {"path": S, "module": S}, ["path", "module"]),
    schema("python_diagnostics", "Get Pyright type and code diagnostics for a Python file.",
           {"path": S}, ["path"]),
    schema("python_debug", "Run a Python script under a debugger and report what it saw: at each breakpoint the call "
                           "stack, local variables and chosen expressions; after an uncaught exception, the locals at the "
                           "crash. Breakpoints look like 'path/file.py:42' or 'path/file.py:42 if x > 3'. Executes code.",
           {"script": S, "breakpoints": {"type": "array", "items": S}, "expressions": {"type": "array", "items": S},
            "args": {"type": "array", "items": S}, "max_hits": {"type": "integer"}, "timeout": {"type": "integer"}},
           ["script"]),
    schema("notebook_read", "Show a Jupyter notebook (.ipynb): each cell's 0-based index, type and source, plus code-cell "
                            "outputs. Use this instead of read_file for notebooks. start/end select a cell range.",
           {"path": S, "start": {"type": "integer"}, "end": {"type": "integer"}}, ["path"]),
    schema("notebook_edit", "Edit a Jupyter notebook cell: action is replace, insert (before index; index equal to the "
                            "cell count appends) or delete. cell_type is code, markdown or raw (replace keeps the "
                            "existing type; insert defaults to code). Cell indices shift after an insert or delete: the "
                            "result lists the current cells, so edit later cells first or re-check indices. "
                            "Does not run cells. Use instead of edit_file.",
           {"path": S, "action": {"type": "string", "enum": ["replace", "insert", "delete"]},
            "index": {"type": "integer"}, "source": S,
            "cell_type": {"type": "string", "enum": ["code", "markdown", "raw"]}}, ["path", "action", "index"]),
    schema("browser_read", "Open a web page in a headless browser, run its JavaScript, and return the rendered text and "
                           "links. Use when web_fetch returns an empty or 'loading' page (single-page apps). Read-only.",
           {"url": S, "wait_seconds": {"type": "integer"}}, ["url"]),
    schema("browser_open", "Open a web page in an interactive browser session and list its numbered clickable elements "
                           "(links, buttons, text boxes). Needs Playwright (the tool explains setup if it is missing). "
                           "Use browser_click and browser_type with those numbers; numbers change after every page change.",
           {"url": S}, ["url"]),
    schema("browser_click", "Click element [index] from the latest browser_open/browser_click/browser_type listing. "
                            "Returns the updated page. Asks the user for approval.",
           {"index": {"type": "integer"}}, ["index"]),
    schema("browser_type", "Type text into text box [index] from the latest page listing. submit=true presses Enter, which "
                           "only works on real forms: if the page did not change, click its Search/Submit button instead. "
                           "Returns the updated page. Asks the user for approval.",
           {"index": {"type": "integer"}, "text": S, "submit": {"type": "boolean"}}, ["index", "text"]),
    schema("browser_close", "Close the interactive browser session when you are done with it.", {}, []),
    schema("github_prs", "List a GitHub repository's pull requests, or with number show one pull request including "
                         "changed files and failing CI checks. repo is 'owner/name' (default: this project's git origin).",
           {"repo": S, "number": {"type": "integer"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "limit": {"type": "integer"}}, []),
    schema("github_issues", "List or search (query) a GitHub repository's issues, or with number show one issue with its "
                            "recent comments. repo is 'owner/name' (default: this project's git origin).",
           {"repo": S, "number": {"type": "integer"}, "state": {"type": "string", "enum": ["open", "closed", "all"]},
            "limit": {"type": "integer"}, "query": S}, []),
    schema("github_actions", "List GitHub Actions workflow runs (filter by branch or status such as 'failure'), or with "
                             "run_id show that run's jobs, failed steps and (when GITHUB_TOKEN is set) failed-job log tails. "
                             "repo is 'owner/name' (default: this project's git origin).",
           {"repo": S, "run_id": {"type": "integer"}, "branch": S, "status": S, "limit": {"type": "integer"}}, []),
    schema("python_debugger", "Step through a Python script interactively. action=start (script, optional breakpoints like "
                              "'file.py:42 if x > 3' - read the file first to get the real line numbers; args; stops at the "
                              "first line unless stop_on_entry=false), then step "
                              "(into a call), next (over it), out (until this function returns), continue (to the next "
                              "breakpoint or the end), eval (expression, evaluated in the paused frame), stop. Every action "
                              "returns where it stopped, nearby source and local variables. Runs code: asks for approval to "
                              "start and to eval. For a quick one-shot look, python_debug is simpler.",
           {"action": {"type": "string", "enum": ["start", "step", "next", "out", "continue", "eval", "stop"]},
            "script": S, "breakpoints": {"type": "array", "items": S}, "args": {"type": "array", "items": S},
            "expression": S, "stop_on_entry": {"type": "boolean"}}, ["action"]),
    schema("run_command", "Run a shell command (PowerShell on Windows) and return its output.",
           {"command": S, "timeout": {"type": "integer"}}, ["command"]),
    schema("web_search", "Search the web and return result titles, links and snippets.", {"query": S}, ["query"]),
    schema("web_fetch", "Download a web page as plain text.", {"url": S}, ["url"]),
    schema("todo", "Replace the visible task list. Use for multi-step work.",
           {"items": {"type": "array", "items": {"type": "object", "properties": {
               "task": S, "done": {"type": "boolean"}}}}}, ["items"]),
    schema("task", "Delegate a research question to a read-only sub-agent and get back its answer. "
                   "Use it to explore a large codebase without filling your own context.",
           {"prompt": S}, ["prompt"]),
]
IMPLS = {f.__name__: f for f in (read_file, write_file, edit_file, notebook_read, notebook_edit, list_dir, glob_files, grep,
                                  python_symbols, python_hover, python_definition, python_references,
                                  python_call_hierarchy, python_rename_impact,
                                  python_module_usage, python_diagnostics, python_debug, python_debugger, github_prs,
                                  github_issues, github_actions,
                                  run_command, web_search, web_fetch, browser_read, browser_open, browser_click,
                                  browser_type, browser_close, todo)}
NEEDS_APPROVAL = {"write_file", "edit_file", "notebook_edit", "run_command", "python_debug", "python_debugger",
                  "browser_click", "browser_type"}
# Some tools only need approval for certain actions (stepping an already-approved session does not).
APPROVAL_ACTIONS = {"python_debugger": {"start", "eval"}}


def needs_approval(name: str, args: dict) -> bool:
    if name not in NEEDS_APPROVAL:
        return False
    actions = APPROVAL_ACTIONS.get(name)
    return actions is None or args.get("action") in actions


READ_ONLY = {"read_file", "list_dir", "glob_files", "grep", "python_symbols", "python_hover",
             "python_definition", "python_references", "python_rename_impact", "python_call_hierarchy",
             "python_module_usage", "python_diagnostics", "github_prs", "github_issues", "github_actions", "notebook_read",
             "browser_read", "browser_open", "browser_close",
             "web_search", "web_fetch", "todo", "task"}
