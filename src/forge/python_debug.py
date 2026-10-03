"""Run a Python script with breakpoints and report the runtime state seen there."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_OUTPUT = 8000
MAX_HITS = 20
MAX_TIMEOUT = 120
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
BREAKPOINT = re.compile(r"^(?P<file>.+?):(?P<line>\d+)(?:\s+if\s+(?P<condition>.+))?$")

# Runs inside the debugged process. It records state at each breakpoint hit (or uncaught
# exception) into a result file after every event so a timeout still leaves usable data.
HELPER = r'''
import json, os, reprlib, runpy, sys, threading

payload = json.load(open(sys.argv[1], encoding="utf-8"))
RESULT, LIMIT, EXPRESSIONS = payload["result"], payload["max_hits"], payload["expressions"]
BREAKPOINTS = {}
for item in payload["breakpoints"]:
    BREAKPOINTS.setdefault(os.path.normcase(item["file"]), {})[item["line"]] = item["condition"]

shorten = reprlib.Repr()
shorten.maxstring = shorten.maxother = 200
shorten.maxlist = shorten.maxtuple = shorten.maxset = shorten.maxdict = 10
shorten.maxlevel = 3
state = {"hits": [], "exception": None, "stopped": False}
lock = threading.Lock()


def show(value):
    try:
        return shorten.repr(value)
    except BaseException as error:
        return "<repr failed: %s>" % error


def local_variables(frame):
    items = [(k, v) for k, v in frame.f_locals.items() if not (k.startswith("__") and k.endswith("__"))]
    return {k: show(v) for k, v in items[:40]}


def call_stack(frame):
    lines = []
    while frame is not None and len(lines) < 8:
        lines.append("%s:%d in %s" % (frame.f_code.co_filename, frame.f_lineno, frame.f_code.co_name))
        frame = frame.f_back
    return lines


def evaluate(frame):
    values = {}
    for text in EXPRESSIONS:
        try:
            values[text] = show(eval(text, frame.f_globals, frame.f_locals))
        except BaseException as error:
            values[text] = "error: %s: %s" % (type(error).__name__, error)
    return values


def save():
    with open(RESULT, "w", encoding="utf-8") as handle:
        json.dump(state, handle)


def tracer(frame, event, arg):
    lines = BREAKPOINTS.get(os.path.normcase(os.path.abspath(frame.f_code.co_filename)))
    if not lines:
        return None

    def on_line(frame, event, arg):
        if event == "line" and frame.f_lineno in lines:
            condition, note, take = lines[frame.f_lineno], None, True
            if condition:
                try:
                    take = bool(eval(condition, frame.f_globals, frame.f_locals))
                except BaseException as error:
                    note = "condition error: %s: %s" % (type(error).__name__, error)
            if take:
                with lock:
                    state["hits"].append({
                        "file": frame.f_code.co_filename, "line": frame.f_lineno, "function": frame.f_code.co_name,
                        "locals": local_variables(frame), "expressions": evaluate(frame),
                        "stack": call_stack(frame), "note": note})
                    if len(state["hits"]) >= LIMIT:
                        state["stopped"] = True
                    save()
                    if state["stopped"]:
                        sys.stdout.flush()
                        sys.stderr.flush()
                        os._exit(0)
        return on_line

    return on_line


script = payload["script"]
sys.argv = [script] + payload["args"]
sys.path.insert(0, os.path.dirname(script))
sys.settrace(tracer)
threading.settrace(tracer)
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as error:
    sys.settrace(None)
    save()
    raise
except BaseException as error:
    sys.settrace(None)
    import traceback
    tb = error.__traceback__
    while tb.tb_next is not None:
        tb = tb.tb_next
    state["exception"] = {
        "type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()[-3000:],
        "file": tb.tb_frame.f_code.co_filename, "line": tb.tb_lineno,
        "locals": local_variables(tb.tb_frame), "stack": call_stack(tb.tb_frame)}
    save()
    sys.exit(1)
sys.settrace(None)
save()
'''


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def _python_executable(root: Path) -> str:
    configured = os.environ.get("FORGE_PYTHON")
    if configured:
        return configured
    for candidate in (root / ".venv" / "Scripts" / "python.exe", root / "venv" / "Scripts" / "python.exe",
                      root / ".venv" / "bin" / "python", root / "venv" / "bin" / "python"):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("python") or shutil.which("python3")
    if found:
        return found
    if not getattr(sys, "frozen", False):
        return sys.executable
    raise RuntimeError("No Python interpreter found. Install Python or set FORGE_PYTHON to its full path.")


def _parse_breakpoints(breakpoints: list, root: Path) -> list[dict]:
    parsed = []
    for text in breakpoints or []:
        match = BREAKPOINT.match(str(text).strip())
        if not match:
            raise ValueError(f"Invalid breakpoint {text!r}. Use 'path/to/file.py:LINE' or 'file.py:LINE if condition'.")
        file = Path(os.path.expanduser(match["file"]))
        file = (file if file.is_absolute() else root / file).resolve()
        if file.suffix.lower() != ".py" or not file.is_file():
            raise ValueError(f"Breakpoint file does not exist or is not a .py file: {file}. The format is "
                             f"'path/to/file.py:LINE' with a line NUMBER (not a function name), optionally followed by "
                             f"' if condition', e.g. 'shop.py:3 if price == 50'.")
        parsed.append({"file": str(file), "line": int(match["line"]), "condition": match["condition"]})
    return parsed


def _stop(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True, creationflags=NO_WINDOW)
    else:
        process.kill()


def _format(result: dict, returncode: int | None, output: str, timed_out: bool, timeout: int) -> str:
    hits = result.get("hits", [])
    status = f"timed out after {timeout}s" if timed_out else (
        "stopped after the hit limit" if result.get("stopped") else f"exit code {returncode}")
    lines = [f"Debug run finished ({status}). Breakpoint hits: {len(hits)}."]
    for number, hit in enumerate(hits, 1):
        lines.append(f"\nHit {number}: {hit['file']}:{hit['line']} in {hit['function']}")
        if hit.get("note"):
            lines.append(f"  note: {hit['note']}")
        lines.append("  stack: " + " <- ".join(hit["stack"][:5]))
        lines.extend(f"  {name} = {value}" for name, value in hit["locals"].items())
        lines.extend(f"  [expr] {expr} = {value}" for expr, value in hit["expressions"].items())
    error = result.get("exception")
    if error:
        lines.append(f"\nUncaught {error['type']}: {error['message']}")
        lines.append(f"  raised at {error['file']}:{error['line']}")
        lines.append("  stack: " + " <- ".join(error["stack"][:5]))
        lines.extend(f"  {name} = {value}" for name, value in error["locals"].items())
        lines.append("\n" + error["traceback"].rstrip())
    if output.strip():
        lines.append("\nProgram output:\n" + _clip(output.strip(), 3000))
    return _clip("\n".join(lines))


def python_debug(script: str, breakpoints: list | None = None, expressions: list | None = None,
                 args: list | None = None, max_hits: int = 5, timeout: int = 30) -> str:
    """Run a Python script, capturing the stack, locals and expressions at breakpoints or at a crash."""
    root = Path.cwd().resolve()
    script_path = Path(os.path.expanduser(script))
    script_path = (script_path if script_path.is_absolute() else root / script_path).resolve()
    if script_path.suffix.lower() != ".py" or not script_path.is_file():
        return f"Error: {script_path} is not a Python script."
    try:
        parsed = _parse_breakpoints(breakpoints or [], root)
        python = _python_executable(root)
    except (ValueError, RuntimeError) as error:
        return f"Error: {error}"
    limit = min(max(int(max_hits or 5), 1), MAX_HITS)
    timeout = min(max(int(timeout or 30), 1), MAX_TIMEOUT)
    with tempfile.TemporaryDirectory(prefix="forge-debug-") as folder:
        work = Path(folder)
        helper, request, result_path = work / "helper.py", work / "request.json", work / "result.json"
        helper.write_text(HELPER, encoding="utf-8")
        request.write_text(json.dumps({
            "script": str(script_path), "args": [str(a) for a in args or []], "breakpoints": parsed,
            "expressions": [str(e) for e in (expressions or [])[:10]], "max_hits": limit,
            "result": str(result_path)}), encoding="utf-8")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
        process = subprocess.Popen([python, str(helper), str(request)], cwd=root, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                   errors="replace", creationflags=NO_WINDOW)
        timed_out = False
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _stop(process)
            output, _ = process.communicate()
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            result = {}
    return _format(result, process.returncode, output or "", timed_out, timeout)
