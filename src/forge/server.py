"""Desktop window backend: a small local web server plus an app-mode browser window."""
import json
import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from forge import config, llm, python_intelligence, tools, ui, updater
from forge.agent import compact, expand_mentions, run_turn, system_prompt

WEB = Path(__file__).parent / "web"
RECENT_FILE = config.HOME / "recent.json"
MODES = ("auto", "ask", "plan")
INIT_PROMPT = ("Explore this project (list files, read the main ones) and write an AGENT.md in the working directory "
               "with: what the project is, how to build/run/test it, the code layout, and conventions to follow. "
               "Keep it under 40 lines.")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".ico": "image/x-icon", ".png": "image/png", ".svg": "image/svg+xml"}


def clip_args(args: dict, limit: int = 6000) -> dict:
    return {k: (v[:limit] if isinstance(v, str) else v) for k, v in args.items()}


class Hub:
    """Keeps the event log for the current chat and fans events out to connected windows."""

    def __init__(self):
        self.log: list[dict] = []
        self.subs: list[queue.Queue] = []
        self.lock = threading.Lock()
        self.counter = 0
        self.live: dict | None = None

    def next_id(self) -> str:
        with self.lock:
            self.counter += 1
            return f"e{self.counter}"

    def emit(self, type: str, persist: bool = True, **data) -> None:
        ev = {"type": type, **data}
        with self.lock:
            if persist:
                self.log.append(ev)
            for q in self.subs:
                q.put(ev)

    def reset(self, events: list[dict] | None = None) -> None:
        with self.lock:
            self.log = list(events or [])
            self.live = None
            for q in self.subs:
                q.put({"type": "reset"})
                for ev in self.log:
                    q.put(ev)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            q.put({"type": "reset"})
            for ev in self.log:
                q.put(ev)
            if self.live:
                q.put(self.live)
            self.subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.subs:
                self.subs.remove(q)


class GuiStream:
    """One assistant message that grows while the model writes it."""

    def __init__(self, hub: Hub):
        self.hub, self.id, self.started, self.last = hub, hub.next_id(), False, 0.0

    def __enter__(self):
        return self

    def first_token(self):
        self.started = True
        self.hub.emit("stream_start", id=self.id)

    def update(self, text: str):
        now = time.monotonic()
        if now - self.last < 0.06:
            return
        self.last = now
        ev = {"type": "stream", "id": self.id, "text": text}
        self.hub.live = ev
        self.hub.emit("stream", persist=False, id=self.id, text=text)

    def finish(self, content: str, started: bool):
        self.hub.live = None
        if started:
            self.hub.emit("stream_end", id=self.id, text=content)
        elif content:
            self.hub.emit("assistant", id=self.id, text=content)

    def __exit__(self, *exc):
        return False


class GuiView:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.open: dict[int, str] = {}

    def stream(self):
        return GuiStream(self.hub)

    def call(self, name: str, args: dict, depth: int = 0) -> None:
        cid = self.hub.next_id()
        self.open[depth] = cid
        self.hub.emit("call", id=cid, name=name, args=clip_args(args), depth=depth)

    def result(self, name: str, result: str, depth: int = 0) -> None:
        cid = self.open.pop(depth, self.hub.next_id())
        self.hub.emit("result", id=cid, name=name, result=tools.clip(result, 6000), depth=depth)
        if name == "todo":
            self.hub.emit("todos", items=list(tools.TODOS))

    def notice(self, text: str, level: str = "info") -> None:
        self.hub.emit("notice", text=text, level=level)

    def busy(self, text: str):
        hub = self.hub

        class Status:
            def __enter__(self):
                hub.emit("status", persist=False, text=text)

            def __exit__(self, *exc):
                hub.emit("status", persist=False, text="")
                return False

        return Status()


class GuiApprover(ui.Approver):
    """Asks through an approval card in the window and waits for the click."""

    def __init__(self, hub: Hub, mode: str, allow: list[str]):
        super().__init__(mode, allow)
        self.hub = hub
        self.pending: dict[str, dict] = {}

    def ask(self, name: str, args: dict) -> bool:
        aid = self.hub.next_id()
        box = {"event": threading.Event(), "answer": "deny"}
        self.pending[aid] = box
        self.hub.emit("approval", id=aid, name=name, target=ui.describe_call(name, args), diff=ui.diff_text(name, args))
        while not box["event"].wait(0.25):
            if llm.CANCEL.is_set():
                box["answer"] = "deny"
                break
        self.pending.pop(aid, None)
        answer = box["answer"]
        if answer == "always":
            self.always.add(name)
        self.hub.emit("approval_done", id=aid, answer=answer)
        if llm.CANCEL.is_set():
            raise llm.Cancelled()
        return answer in ("allow", "always")

    def answer(self, aid: str, answer: str) -> None:
        box = self.pending.get(aid)
        if box:
            box["answer"] = answer
            box["event"].set()


def replay(messages: list) -> list[dict]:
    """Rebuild the visible chat from saved messages."""
    events, pending, n = [], [], 0
    for m in messages[1:]:
        role = m.get("role")
        if role == "user":
            text = m.get("content", "")
            if text.startswith("Summary of the earlier conversation"):
                events.append({"type": "notice", "text": "Earlier messages were summarised.", "level": "info"})
            else:
                events.append({"type": "user", "text": text.split("\n\n[contents of ")[0]})
        elif role == "assistant":
            if m.get("content"):
                events.append({"type": "assistant", "id": f"r{n}", "text": m["content"]})
            for call in m.get("tool_calls") or []:
                n += 1
                fn = call.get("function", {})
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                pending.append(f"r{n}")
                events.append({"type": "call", "id": f"r{n}", "name": fn.get("name", ""), "args": clip_args(args), "depth": 0})
        elif role == "tool" and pending:
            events.append({"type": "result", "id": pending.pop(0), "name": m.get("tool_name", ""),
                           "result": tools.clip(m.get("content", ""), 6000), "depth": 0})
    return events


def find_ollama() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.is_file() else None


class App:
    def __init__(self):
        self.cfg = config.load()
        tools.USER_DENY[:] = self.cfg["deny"]
        self.hub = Hub()
        self.view = GuiView(self.hub)
        mode = self.cfg["mode"] if self.cfg["mode"] in MODES else "auto"
        self.approver = GuiApprover(self.hub, mode, self.cfg["allow"])
        self.state = {"used": 0, "id": config.new_session_id()}
        self.messages = [{"role": "system", "content": system_prompt(mode)}]
        self.busy = False
        self.pulling: str | None = None
        self.update_info: dict | None = None
        self._update: dict | None = None
        self.whats_new: dict | None = None
        self.window: subprocess.Popen | None = None
        self.models: list[dict] = []
        self.ollama_ok = False

    # ------------------------------------------------------------ state
    def refresh_models(self) -> None:
        try:
            self.models = llm.list_models()
            self.ollama_ok = True
        except llm.OllamaError:
            self.models, self.ollama_ok = [], False

    def snapshot(self) -> dict:
        return {
            "model": self.cfg["model"], "mode": self.approver.mode, "cwd": str(Path.cwd()),
            "used": self.state["used"], "ctx": self.cfg["num_ctx"], "busy": self.busy,
            "ollama": self.ollama_ok, "pulling": self.pulling, "sessionId": self.state["id"],
            "models": [{"name": m["name"], "size": m.get("size", 0)} for m in self.models],
            "recent": self.recent(), "version": updater.__version__, "update": self.update_info,
            "whatsNew": self.whats_new, "pyright": python_intelligence.pyright_available(),
        }

    def push_state(self) -> None:
        self.hub.emit("state", persist=False, state=self.snapshot())

    def recent(self) -> list[str]:
        try:
            return [p for p in json.loads(RECENT_FILE.read_text(encoding="utf-8")) if Path(p).is_dir()][:8]
        except (OSError, ValueError):
            return []

    def remember_folder(self, path: str) -> None:
        items = [path] + [p for p in self.recent() if p != path]
        config.ensure_home()
        RECENT_FILE.write_text(json.dumps(items[:8]), encoding="utf-8")

    def rebuild_prompt(self) -> None:
        self.messages[0] = {"role": "system", "content": system_prompt(self.approver.mode)}

    # ------------------------------------------------------------ running
    def start(self, label: str, fn) -> bool:
        if self.busy:
            return False
        self.busy = True
        llm.CANCEL.clear()
        self.hub.emit("busy", persist=False, value=True)
        threading.Thread(target=self._run, args=(label, fn), daemon=True).start()
        return True

    def _run(self, label: str, fn) -> None:
        start = len(self.messages)
        try:
            fn()
        except llm.Cancelled:
            del self.messages[start + 1:]
            self.view.notice("Stopped.", "warn")
        except llm.OllamaError as e:
            del self.messages[start + 1:]
            self.view.notice(str(e), "error")
        except Exception as e:  # never let the worker die silently
            self.view.notice(f"{type(e).__name__}: {e}", "error")
        finally:
            self.busy = False
            self.hub.live = None
            if len(self.messages) > 1:
                config.save_session(self.state["id"], self.cfg["model"], self.messages)
            self.hub.emit("busy", persist=False, value=False)
            self.push_state()

    def send(self, text: str) -> bool:
        def work():
            self.rebuild_prompt()
            self.messages.append({"role": "user", "content": expand_mentions(text)})
            usage = run_turn(self.cfg, self.messages, self.approver, self.view, self.state["used"])
            self.state["used"] = sum(usage.values())
            self.push_state()

        if self.busy:
            return False
        self.hub.emit("user", text=text)
        return self.start("send", work)

    def stop(self) -> None:
        llm.cancel()
        tools.kill_running()
        for box in list(self.approver.pending.values()):
            box["event"].set()

    # ------------------------------------------------------------ commands
    def new_chat(self) -> None:
        self.messages[:] = [{"role": "system", "content": system_prompt(self.approver.mode)}]
        tools.TODOS.clear()
        self.state.update(used=0, id=config.new_session_id())
        self.hub.reset([])
        self.push_state()

    def set_model(self, name: str) -> str | None:
        if name not in [m["name"] for m in self.models]:
            return f"No installed model named {name}."
        self.cfg["model"] = name
        self.push_state()

    def set_mode(self, mode: str) -> str | None:
        if mode not in MODES:
            return "Modes: auto, ask, plan."
        self.approver.mode = mode
        self.rebuild_prompt()
        self.push_state()

    def set_folder(self, path: str) -> str | None:
        p = Path(path)
        if not p.is_dir():
            return f"{path} is not a folder."
        os.chdir(p)
        self.remember_folder(str(p))
        self.new_chat()

    def resume(self, sid: str) -> str | None:
        path = config.SESSIONS / f"{Path(sid).name}.json"
        if not path.is_file():
            return "Session not found."
        data = config.load_session(path)
        if data.get("cwd") and Path(data["cwd"]).is_dir():
            os.chdir(data["cwd"])
        self.messages[:] = data["messages"]
        self.rebuild_prompt()
        self.state.update(used=0, id=path.stem)
        self.hub.reset(replay(self.messages))
        self.push_state()

    def sessions(self) -> list[dict]:
        out = []
        for f in config.list_sessions()[:30]:
            try:
                data = config.load_session(f)
            except (OSError, ValueError):
                continue
            first = next((m["content"] for m in data["messages"] if m.get("role") == "user"), "")
            first = first.split("\n\n[contents of ")[0].strip()
            if first.startswith("Summary of the earlier"):
                first = "Earlier conversation"
            out.append({"id": f.stem, "title": first[:70] or "New chat", "cwd": data.get("cwd", "")})
        return out

    def delete_session(self, sid: str) -> None:
        (config.SESSIONS / f"{Path(sid).name}.json").unlink(missing_ok=True)

    def check_update(self) -> None:
        info = updater.check()
        if info:
            self._update = info
            self.update_info = {"version": info["version"], "notes": info["notes"]}
            self.push_state()

    def check_whats_new(self) -> None:
        info = updater.pending_whats_new()
        if info:
            self.whats_new = info
            self.push_state()

    def apply_update(self) -> str | None:
        info = self._update
        if not info:
            return "No update available."
        if self.busy:
            return "Wait for the current task or press Stop."

        def progress(p):
            self.hub.emit("pull", persist=False, name="update", status="downloading", pct=p)

        def work():
            try:
                progress(0)
                path = updater.download(info, progress)
                self.hub.emit("pull", persist=False, name="update", status="done", pct=None)
                self.view.notice("Installing Forge " + info["version"] + ". The app will restart.")
                updater.run_installer(path)
                time.sleep(1.5)
                if self.window is not None:
                    self.window.terminate()
                os._exit(0)
            except Exception as e:
                self.hub.emit("pull", persist=False, name="update", status="done", pct=None)
                self.view.notice("Update failed: " + str(e), "error")

        threading.Thread(target=work, daemon=True).start()
        return None

    def pull(self, name: str) -> str | None:
        if self.pulling:
            return "A download is already running."
        self.pulling = name

        def work():
            try:
                body = json.dumps({"model": name, "stream": True}).encode()
                req = urllib.request.Request(llm.HOST + "/api/pull", data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=3600) as r:
                    for line in r:
                        chunk = json.loads(line)
                        if "error" in chunk:
                            raise RuntimeError(chunk["error"])
                        total, done = chunk.get("total") or 0, chunk.get("completed") or 0
                        pct = int(done * 100 / total) if total else None
                        self.hub.emit("pull", persist=False, name=name, status=chunk.get("status", ""), pct=pct)
                self.view.notice(f"Downloaded {name}.")
            except Exception as e:
                self.view.notice(f"Download failed: {e}", "error")
            finally:
                self.pulling = None
                self.hub.emit("pull", persist=False, name=name, status="done", pct=None)
                self.refresh_models()
                self.push_state()

        threading.Thread(target=work, daemon=True).start()
        self.push_state()

    def files(self, query: str) -> list[str]:
        query, root, found = query.lower(), Path.cwd(), []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in tools.SKIP_DIRS and not d.startswith(".")]
            depth = len(Path(dirpath).relative_to(root).parts)
            if depth > 5:
                dirnames[:] = []
            for name in filenames:
                rel = str((Path(dirpath) / name).relative_to(root))
                if query in rel.lower():
                    found.append(rel)
                    if len(found) >= 40:
                        return found
        return found

    def command(self, cmd: str, arg: str) -> str | None:
        if cmd == "new":
            if self.busy:
                return "Wait for the current task or press Stop."
            return self.new_chat()
        if cmd == "model":
            return self.set_model(arg)
        if cmd == "mode":
            return self.set_mode(arg)
        if cmd == "undo":
            self.view.notice(tools.undo_last())
        elif cmd == "compact":
            if not self.start("compact", lambda: compact(self.cfg, self.messages, self.view)):
                return "Wait for the current task or press Stop."
        elif cmd == "remember":
            if not arg:
                return "Usage: /remember <note>"
            config.add_memory(arg)
            self.rebuild_prompt()
            self.view.notice("Saved to memory.")
        elif cmd == "init":
            if not self.send(INIT_PROMPT):
                return "Wait for the current task or press Stop."
        elif cmd == "folder":
            if self.busy:
                return "Wait for the current task or press Stop."
            return self.set_folder(arg)
        elif cmd == "resume":
            if self.busy:
                return "Wait for the current task or press Stop."
            return self.resume(arg)
        elif cmd == "pull":
            return self.pull(arg)
        elif cmd == "update":
            return self.apply_update()
        elif cmd == "whatsnew_seen":
            updater.mark_seen()
            self.whats_new = None
            self.push_state()
        elif cmd == "delete":
            self.delete_session(arg)
        else:
            return f"Unknown command {cmd}."
        return None


def pick_folder() -> str | None:
    if os.name != "nt":
        return None
    script = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$f = New-Object System.Windows.Forms.Form -Property @{TopMost=$true}; "
              "$d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description='Choose a project folder'; "
              "if ($d.ShowDialog($f) -eq 'OK') { [Console]::OutputEncoding=[Text.Encoding]::UTF8; Write-Output $d.SelectedPath }")
    r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", creationflags=tools.NO_WINDOW)
    return r.stdout.strip() or None


class Handler(BaseHTTPRequestHandler):
    app: App
    token: str
    port: int

    def log_message(self, format: str, *args) -> None:
        pass

    def reply(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def json(self, data, code: int = 200) -> None:
        self.reply(code, json.dumps(data).encode(), "application/json")

    def allowed(self, query: dict) -> bool:
        host_ok = self.headers.get("Host", "") in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
        return host_ok and secrets.compare_digest(query.get("t", [""])[0], self.token)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        if self.headers.get("Host", "") not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}"):
            return self.json({"error": "forbidden"}, 403)
        if not url.path.startswith("/api/"):
            name = "index.html" if url.path == "/" else url.path.lstrip("/")
            f = (WEB / name).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                return self.json({"error": "not found"}, 404)
            return self.reply(200, f.read_bytes(), MIME.get(f.suffix, "application/octet-stream"))
        if not self.allowed(query):
            return self.json({"error": "forbidden"}, 403)
        app = self.app
        if url.path == "/api/state":
            return self.json(app.snapshot())
        if url.path == "/api/sessions":
            return self.json(app.sessions())
        if url.path == "/api/files":
            return self.json(app.files(query.get("q", [""])[0]))
        if url.path == "/api/memory":
            return self.json({"text": config.read_memory(), "path": str(config.MEMORY_FILE)})
        if url.path == "/api/events":
            return self.stream_events()
        self.json({"error": "not found"}, 404)

    def stream_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        q = self.app.hub.subscribe()
        try:
            while True:
                try:
                    ev = q.get(timeout=15)
                    self.wfile.write(b"data: " + json.dumps(ev).encode() + b"\n\n")
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (OSError, ValueError):
            pass
        finally:
            self.app.hub.unsubscribe(q)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self.allowed(urllib.parse.parse_qs(url.query)):
            return self.json({"error": "forbidden"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.json({"error": "bad json"}, 400)
        app = self.app
        if url.path == "/api/send":
            text = str(body.get("text", "")).strip()
            if not text:
                return self.json({"error": "empty"}, 400)
            return self.json({"ok": app.send(text)}) if not app.busy else self.json({"error": "busy"}, 409)
        if url.path == "/api/stop":
            app.stop()
            return self.json({"ok": True})
        if url.path == "/api/approve":
            app.approver.answer(str(body.get("id", "")), str(body.get("answer", "deny")))
            return self.json({"ok": True})
        if url.path == "/api/command":
            err = app.command(str(body.get("cmd", "")), str(body.get("arg", "")))
            return self.json({"error": err} if err else {"ok": True})
        if url.path == "/api/folder":
            if app.busy:
                return self.json({"error": "Wait for the current task or press Stop."})
            path = str(body.get("path") or "") or pick_folder()
            if not path:
                return self.json({"ok": False})
            err = app.set_folder(path)
            return self.json({"error": err} if err else {"ok": True})
        if url.path == "/api/memory":
            config.ensure_home()
            config.MEMORY_FILE.write_text(str(body.get("text", "")), encoding="utf-8")
            app.rebuild_prompt()
            return self.json({"ok": True})
        self.json({"error": "not found"}, 404)


def find_browser() -> str | None:
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
            if base and (Path(base) / rel).is_file():
                return str(Path(base) / rel)
    return shutil.which("msedge") or shutil.which("chrome")


def launch_window(url: str):
    exe = find_browser()
    if not exe:
        return None
    profile = config.HOME / "webprofile"
    return subprocess.Popen([exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1180,820",
                             "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])


def ensure_ollama(app: App) -> None:
    app.refresh_models()
    if not app.ollama_ok:
        exe = find_ollama()
        if exe:
            subprocess.Popen([exe, "serve"], creationflags=tools.NO_WINDOW | 0x00000008,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
            for _ in range(30):
                time.sleep(0.5)
                app.refresh_models()
                if app.ollama_ok:
                    break
    app.push_state()


HANDOFF_SECONDS = 5.0


def _instance_file() -> Path:
    return config.HOME / "instance.json"


def running_instance_url() -> str | None:
    """The URL of an already-running Forge, or None. Stale files (crashed app) are ignored."""
    try:
        url = json.loads(_instance_file().read_text(encoding="utf-8")).get("url", "")
        parsed = urllib.parse.urlparse(url)
        token = urllib.parse.parse_qs(parsed.query).get("t", [""])[0]
        if parsed.hostname != "127.0.0.1" or not parsed.port or not token:
            return None
        with urllib.request.urlopen(f"http://127.0.0.1:{parsed.port}/api/state?t={token}", timeout=2) as response:
            return url if response.status == 200 else None
    except (OSError, ValueError, AttributeError):
        return None


def register_instance(url: str) -> None:
    try:
        config.ensure_home()
        _instance_file().write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def clear_instance(url: str) -> None:
    """Remove our registration at exit, but never a newer instance's."""
    try:
        if json.loads(_instance_file().read_text(encoding="utf-8")).get("url") == url:
            _instance_file().unlink()
    except (OSError, ValueError):
        pass


def watch_window(hub: Hub, grace: float = 10.0, connect_timeout: float = 45.0, poll: float = 0.5,
                 now=time.monotonic, sleep=time.sleep) -> None:
    """Block until the window is gone, judged by its connection to the event stream.

    Used when the browser process we launched exited at once: Edge handed the window to another process
    (for example one still shutting down on the same profile), so waiting on our process would quit too early.
    Returns when a window that had connected stays disconnected for `grace` seconds, or when none ever
    connects within `connect_timeout`.
    """
    started, empty_since, seen = now(), None, False
    while True:
        connected = bool(hub.subs)
        seen = seen or connected
        if connected:
            empty_since = None
        else:
            if empty_since is None:
                empty_since = now()
            if seen and now() - empty_since >= grace:
                return
            if not seen and now() - started >= connect_timeout:
                return
        sleep(poll)


def run(port: int = 0) -> int:
    existing = running_instance_url()
    if existing:
        # Two instances would share one browser profile: the second window would open inside the first
        # instance's browser and then point at a server that has already exited. Open a window on the
        # running one instead.
        if launch_window(existing) is None:
            webbrowser.open(existing)
        return 0
    app = App()
    handler = type("BoundHandler", (Handler,), {"app": app, "token": secrets.token_urlsafe(24)})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    handler.port = server.server_port
    url = f"http://127.0.0.1:{server.server_port}/?t={handler.token}"
    threading.Thread(target=server.serve_forever, daemon=True).start()
    register_instance(url)
    threading.Thread(target=ensure_ollama, args=(app,), daemon=True).start()
    threading.Thread(target=app.check_update, daemon=True).start()
    threading.Thread(target=app.check_whats_new, daemon=True).start()
    proc = launch_window(url)
    app.window = proc
    if proc is not None:
        launched = time.monotonic()
        proc.wait()
        if time.monotonic() - launched < HANDOFF_SECONDS:
            watch_window(app.hub)
    else:
        webbrowser.open(url)
        print(f"Forge is running at {url}\nPress Ctrl+C to quit.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    clear_instance(url)
    app.stop()
    os._exit(0)
