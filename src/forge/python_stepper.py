"""A stepping debugger: start a script, then step, inspect and continue it one command at a time.

The script runs in a worker process under the chosen Python (so it works in the packaged app). The worker
traces only the user's own code, stops at breakpoints and on step commands, and talks JSON lines.
"""
import atexit
import json
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path

from forge.python_debug import _parse_breakpoints, _python_executable

MAX_OUTPUT = 8000
CALL_TIMEOUT = 60
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
ACTIONS = ("start", "step", "next", "out", "continue", "eval", "stop")

WORKER = r'''
import io, json, linecache, os, reprlib, runpy, sys, sysconfig

proto = sys.stdout
cmd_in = sys.stdin          # the debugger's command channel; the program itself gets an empty stdin
payload = json.load(open(sys.argv[1], encoding="utf-8"))
SCRIPT = payload["script"]
BREAKS = {(os.path.normcase(b["file"]), b["line"]): b["condition"] for b in payload["breakpoints"]}
REACHED, STOPPED, CONDITION_ERRORS = {}, set(), {}
LIBRARY = tuple(os.path.normcase(sysconfig.get_paths()[k]) for k in ("stdlib", "purelib", "platlib", "platstdlib"))
THIS_FILE = os.path.normcase(os.path.abspath(__file__))
OUTPUT = []
mode, ref_depth = ("entry" if payload["stop_on_entry"] else "run"), 0

shorten = reprlib.Repr()
shorten.maxstring = shorten.maxother = 200
shorten.maxlist = shorten.maxtuple = shorten.maxset = shorten.maxdict = 10
shorten.maxlevel = 3


class Capture(io.TextIOBase):
    def writable(self):
        return True

    def write(self, text):
        OUTPUT.append(text)
        return len(text)


def send(value):
    proto.write(json.dumps(value) + "\n")
    proto.flush()


def show(value):
    try:
        return shorten.repr(value)
    except BaseException as error:
        return "<repr failed: %s>" % error


def is_user(filename):
    if not filename or filename.startswith("<"):
        return False
    path = os.path.normcase(os.path.abspath(filename))
    return path != THIS_FILE and not path.startswith(LIBRARY)


def depth(frame):
    count = 0
    while frame is not None:
        if is_user(frame.f_code.co_filename):
            count += 1
        frame = frame.f_back
    return count


def take_output():
    text = "".join(OUTPUT)
    OUTPUT.clear()
    return text[-2000:]


def snapshot(frame, reason):
    filename = frame.f_code.co_filename
    lines = linecache.getlines(filename)
    first = max(frame.f_lineno - 3, 1)
    context = [[n, lines[n - 1].rstrip("\n")] for n in range(first, min(frame.f_lineno + 3, len(lines)) + 1)]
    names = [(k, v) for k, v in frame.f_locals.items() if not (k.startswith("__") and k.endswith("__"))]
    stack, walk = [], frame
    while walk is not None and len(stack) < 8:
        if is_user(walk.f_code.co_filename):
            stack.append("%s:%d in %s" % (walk.f_code.co_filename, walk.f_lineno, walk.f_code.co_name))
        walk = walk.f_back
    return {"event": "stopped", "reason": reason, "file": filename, "line": frame.f_lineno,
            "function": frame.f_code.co_name, "context": context, "stack": stack,
            "locals": {k: show(v) for k, v in names[:30]}, "output": take_output(),
            "note": CONDITION_ERRORS.get((os.path.normcase(os.path.abspath(filename)), frame.f_lineno), "")
            if reason == "breakpoint" else ""}


def prompt(frame, reason, post_mortem=False):
    """Report where we stopped, then serve commands until one resumes the program."""
    global mode, ref_depth
    send(snapshot(frame, reason))
    for line in cmd_in:
        request = json.loads(line)
        action = request.get("action")
        if action == "eval":
            try:
                send({"event": "value", "result": show(eval(request["expression"], frame.f_globals, frame.f_locals))})
            except BaseException as error:
                send({"event": "value", "error": "%s: %s" % (type(error).__name__, error)})
        elif action == "stop":
            send({"event": "finished", "stopped": True, "output": take_output()})
            os._exit(0)
        elif action in ("step", "next", "out", "continue"):
            if post_mortem:
                return False
            mode, ref_depth = ("run" if action == "continue" else action), depth(frame)
            return True
    os._exit(0)


def local_trace(frame, event, arg):
    global mode
    if event != "line":
        return local_trace
    reason = None
    key = (os.path.normcase(os.path.abspath(frame.f_code.co_filename)), frame.f_lineno)
    if key in BREAKS:
        condition = BREAKS[key]
        REACHED[key] = REACHED.get(key, 0) + 1
        try:
            reason = "breakpoint" if not condition or eval(condition, frame.f_globals, frame.f_locals) else None
        except BaseException as error:
            CONDITION_ERRORS.setdefault(key, "%s: %s" % (type(error).__name__, error))
            reason = "breakpoint"
    if reason is None:
        if mode == "entry":
            reason = "entry"
        elif mode == "step":
            reason = "step"
        elif mode == "next" and depth(frame) <= ref_depth:
            reason = "next"
        elif mode == "out" and depth(frame) < ref_depth:
            reason = "return"
    if reason:
        if reason == "breakpoint":
            STOPPED.add(key)
        sys.settrace(None)
        try:
            prompt(frame, reason)
        finally:
            sys.settrace(global_trace)
    return local_trace


def global_trace(frame, event, arg):
    return local_trace if event == "call" and is_user(frame.f_code.co_filename) else None


sys.argv = [SCRIPT] + payload["args"]
sys.path.insert(0, os.path.dirname(SCRIPT))
sys.stdin = io.StringIO("")
sys.stdout = sys.stderr = Capture()
code = 0
try:
    sys.settrace(global_trace)
    runpy.run_path(SCRIPT, run_name="__main__")
except SystemExit as error:
    code = error.code if isinstance(error.code, int) else (0 if error.code is None else 1)
except BaseException as error:
    sys.settrace(None)
    import traceback
    tb = error.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    code = 1
    if tb is not None:
        message = "%s: %s" % (type(error).__name__, error)
        prompt(tb.tb_frame, "exception: " + message, post_mortem=True)
    sys.stderr.write(traceback.format_exc())
finally:
    sys.settrace(None)
def unhit_breakpoints():
    notes = []
    for key, condition in BREAKS.items():
        if key in STOPPED:
            continue
        label = "%s:%d" % (os.path.basename(key[0]), key[1])
        source = linecache.getline(key[0], key[1]).strip()
        if key in CONDITION_ERRORS:
            notes.append("%s: the condition %r failed with %s" % (label, condition, CONDITION_ERRORS[key]))
        elif key in REACHED:
            notes.append("%s: reached %d time(s) but the condition %r was never true" % (label, REACHED[key], condition))
        else:
            notes.append("%s: that line never ran (line is: %r)" % (label, source or "<beyond the end of the file>"))
    return notes


send({"event": "finished", "exit_code": code, "output": take_output(), "unhit": unhit_breakpoints()})
'''

_lock = threading.RLock()
_process: subprocess.Popen | None = None
_events: "queue.Queue[str | None]" = queue.Queue()


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} characters]"


def _pump(process: subprocess.Popen, sink: "queue.Queue[str | None]") -> None:
    assert process.stdout is not None
    for line in process.stdout:
        sink.put(line)
    sink.put(None)


def _end() -> None:
    global _process
    with _lock:
        process, _process = _process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass  # a broken pipe from a process that already ended


atexit.register(_end)


def _next_event() -> dict:
    try:
        line = _events.get(timeout=CALL_TIMEOUT)
    except queue.Empty:
        raise TimeoutError(f"the program did not stop within {CALL_TIMEOUT}s (it may be waiting or in a long loop)") from None
    if line is None:
        raise RuntimeError("the debugged program ended unexpectedly")
    return json.loads(line)


def _format(event: dict) -> str:
    kind = event.get("event")
    if kind == "finished":
        end = "stopped by you" if event.get("stopped") else f"finished (exit code {event.get('exit_code')})"
        text = f"The program {end}. The debug session is over."
        if event.get("output", "").strip():
            text += "\n\nProgram output:\n" + event["output"].strip()
        if event.get("unhit"):
            text += ("\n\nThese breakpoints never stopped the program (line numbers are 1-based, so read the file to "
                     "pick the right line):\n" + "\n".join("  " + note for note in event["unhit"]))
        return _clip(text)
    reason = event["reason"]
    where = f"{event['file']}:{event['line']} in {event['function']}()"
    if reason.startswith("exception"):
        heading = "Program raised " + reason.removeprefix("exception: ") + " (inspect with eval; any step ends the session)"
    else:
        heading = {"breakpoint": "Stopped at a breakpoint", "entry": "Stopped at the first line", "step": "Stepped",
                   "next": "Stepped over", "return": "Stepped out"}.get(reason, "Stopped")
    lines = [f"{heading}: {where}", ""]
    if event.get("note"):
        lines = [f"{heading}: {where}", f"(the breakpoint condition failed with {event['note']}, so it stopped anyway)", ""]
    for number, code in event["context"]:
        lines.append(f"{'->' if number == event['line'] else '  '} {number:>4}  {code}")
    lines += ["", "Locals:"] + [f"  {name} = {value}" for name, value in event["locals"].items()]
    if len(event["stack"]) > 1:
        lines += ["", "Stack: " + " <- ".join(event["stack"][:5])]
    if event.get("output", "").strip():
        lines += ["", "Program output since last stop:", event["output"].rstrip()]
    return _clip("\n".join(lines))


def _send(request: dict) -> None:
    assert _process is not None and _process.stdin is not None
    _process.stdin.write(json.dumps(request) + "\n")
    _process.stdin.flush()


def _alive() -> bool:
    return _process is not None and _process.poll() is None


def python_debugger(action: str, script: str | None = None, breakpoints: list | None = None,
                    args: list | None = None, expression: str | None = None, stop_on_entry: bool = True) -> str:
    """Control a step-by-step debug session of a Python script."""
    global _process, _events
    if action not in ACTIONS:
        return f"Error: action must be one of: {', '.join(ACTIONS)}."
    with _lock:
        if action == "start":
            return _start(script, breakpoints or [], args or [], stop_on_entry)
        if action == "stop":
            was_running = _alive()
            _end()
            return "Debug session closed." if was_running else "No debug session was running."
        if not _alive():
            _end()
            return "Error: no debug session is running. Start one with action='start'."
        try:
            if action == "eval":
                if not expression or not str(expression).strip():
                    return "Error: eval needs an expression."
                _send({"action": "eval", "expression": str(expression)})
                result = _next_event()
                return f"{expression} = {result['result']}" if "result" in result else f"Error evaluating: {result.get('error')}"
            _send({"action": action})
            event = _next_event()
        except (TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as error:
            _end()
            return f"Error: {error}. The debug session was closed."
        if event.get("event") == "finished":
            _end()
        return _format(event)


def _start(script: str | None, breakpoints: list, args: list, stop_on_entry: bool) -> str:
    global _process, _events
    if not script:
        return "Error: start needs a script path."
    root = Path.cwd().resolve()
    path = Path(os.path.expanduser(script))
    path = (path if path.is_absolute() else root / path).resolve()
    if path.suffix.lower() != ".py" or not path.is_file():
        return f"Error: {path} is not a Python script."
    try:
        parsed = _parse_breakpoints(breakpoints, root)
        python = _python_executable(root)
    except (ValueError, RuntimeError) as error:
        return f"Error: {error}"
    _end()
    folder = Path(tempfile.mkdtemp(prefix="forge-stepper-"))
    (folder / "worker.py").write_text(WORKER, encoding="utf-8")
    (folder / "request.json").write_text(json.dumps({
        "script": str(path), "args": [str(a) for a in args], "breakpoints": parsed, "stop_on_entry": bool(stop_on_entry)}),
        encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    _events = queue.Queue()
    _process = subprocess.Popen([python, str(folder / "worker.py"), str(folder / "request.json")], cwd=root, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    threading.Thread(target=_pump, args=(_process, _events), daemon=True).start()
    try:
        event = _next_event()
    except (TimeoutError, RuntimeError) as error:
        _end()
        return f"Error: {error}. The debug session was closed."
    if event.get("event") == "finished":
        _end()
    return _format(event)


def describe_action(args: dict) -> str:
    """Approval text for the actions that run code (start and eval)."""
    if args.get("action") == "eval":
        return f"evaluate in the paused program: {args.get('expression', '')}"
    parts = [f"debug {args.get('script', '')} {' '.join(str(a) for a in args.get('args') or [])}".rstrip()]
    parts += [f"breakpoint: {b}" for b in args.get("breakpoints") or []]
    return "\n".join(parts)
