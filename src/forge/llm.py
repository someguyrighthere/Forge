"""Talking to the local Ollama server."""
import json
import os
import http.client
import re
import shutil
import threading
import urllib.error
import urllib.request
from pathlib import Path

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
if not HOST.startswith("http"):
    HOST = "http://" + HOST


class OllamaError(Exception):
    pass


class Cancelled(Exception):
    pass


CANCEL = threading.Event()
ACTIVE = {"resp": None}


def cancel() -> None:
    CANCEL.set()
    resp = ACTIVE.get("resp")
    if resp is not None:
        try:
            resp.close()
        except Exception:
            pass


def _post(path: str, payload: dict, timeout: int = 600):
    req = urllib.request.Request(HOST + path, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        return urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise OllamaError(e.read().decode(errors="replace")) from None
    except urllib.error.URLError as e:
        raise OllamaError(f"Cannot reach Ollama at {HOST}: {e.reason}. Is it running?") from None


def list_models() -> list[dict]:
    try:
        with urllib.request.urlopen(HOST + "/api/tags", timeout=5) as r:
            return json.load(r).get("models", [])
    except (urllib.error.URLError, TimeoutError) as e:
        raise OllamaError(f"Cannot reach Ollama at {HOST}: {e}") from None


def find_ollama() -> str | None:
    """Path of the Ollama executable, or None when it is not installed."""
    found = shutil.which("ollama")
    if found:
        return found
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.is_file() else None


def pull_model(name: str, on_progress=None, timeout: int = 3600) -> None:
    """Download a model through Ollama, calling on_progress(status, percent or None) as it goes."""
    request = urllib.request.Request(HOST + "/api/pull", data=json.dumps({"model": name, "stream": True}).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            for line in response:
                chunk = json.loads(line)
                if "error" in chunk:
                    raise OllamaError(str(chunk["error"]))
                total, done = chunk.get("total") or 0, chunk.get("completed") or 0
                if on_progress:
                    on_progress(chunk.get("status", ""), int(done * 100 / total) if total else None)
    except urllib.error.URLError as e:
        raise OllamaError(f"Cannot reach Ollama at {HOST}: {e.reason}. Is it running?") from None
    except (OSError, ValueError) as e:
        raise OllamaError(str(e)) from None


def _options(model: str, num_ctx: int, think: bool) -> dict:
    extra = {"think": think} if model.startswith("qwen3") else {}
    return {"options": {"num_ctx": num_ctx}, "keep_alive": "30m", **extra}


MARKER = re.compile(r'</?tool_(?:call|request)>|\{\s*"name"\s*:|\[\s*\{\s*"name"')


def visible_text(content: str) -> str:
    """The part of a reply to show: everything before an inline tool call."""
    m = MARKER.search(content)
    return content[:m.start()] if m else content


def parse_text_tool_calls(content: str, known: set[str]):
    """Some local models write tool calls as JSON text (often after a sentence) instead of structured calls.
    Returns (calls, remaining_text)."""
    decoder = json.JSONDecoder()
    calls, spans, i = [], [], 0
    while i < len(content):
        if content[i] not in "{[":
            i += 1
            continue
        try:
            data, end = decoder.raw_decode(content, i)
        except json.JSONDecodeError:
            i += 1
            continue
        found = []
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("name") in known:
                args = item.get("arguments", item.get("parameters", {}))
                if isinstance(args, (dict, str)):
                    found.append({"function": {"name": item["name"], "arguments": args}})
        if found:
            calls += found
            spans.append((i, end))
            i = end
        else:
            i += 1
    rest = content
    for start, end in reversed(spans):
        rest = rest[:start] + rest[end:]
    rest = re.sub(r"(?is)</?tool_(?:call|request)>|^```(?:json)?\s*$|^```\s*$", "", rest, flags=re.M).strip()
    return calls, rest


def chat(model: str, messages: list, tools: list, num_ctx: int, think: bool = False,
         on_text=None, on_first_token=None):
    """Stream one reply. Returns (content, tool_calls, usage). on_text gets the text so far."""
    resp = _post("/api/chat", {"model": model, "messages": messages, "tools": tools, "stream": True,
                               **_options(model, num_ctx, think)})
    content, calls, usage, started = "", [], {}, False
    ACTIVE["resp"] = resp
    try:
        content, calls, usage, started = _read(resp, on_text, on_first_token)
    except (OSError, ValueError, http.client.HTTPException):
        if CANCEL.is_set():
            raise Cancelled() from None
        raise
    finally:
        ACTIVE["resp"] = None
    if CANCEL.is_set():
        raise Cancelled()
    if not calls and content:
        calls, rest = parse_text_tool_calls(content, {t["function"]["name"] for t in tools})
        if calls:
            content = rest
    return content, calls, usage, started


def _read(resp, on_text, on_first_token):
    content, calls, usage, started = "", [], {}, False
    with resp:
        for line in resp:
            if CANCEL.is_set():
                raise Cancelled()
            if not line.strip():
                continue
            chunk = json.loads(line)
            if "error" in chunk:
                raise OllamaError(chunk["error"])
            msg = chunk.get("message", {})
            if msg.get("content"):
                content += msg["content"]
                shown = visible_text(content).strip()
                if on_text and shown:
                    if not started and on_first_token:
                        on_first_token()
                    started = True
                    on_text(shown)
            calls.extend(msg.get("tool_calls") or [])
            if chunk.get("done"):
                usage = {"in": chunk.get("prompt_eval_count", 0), "out": chunk.get("eval_count", 0)}
    return content, calls, usage, started


def complete(model: str, messages: list, num_ctx: int) -> str:
    with _post("/api/chat", {"model": model, "messages": messages, "stream": False,
                             **_options(model, num_ctx, False)}) as r:
        return json.load(r).get("message", {}).get("content", "").strip()
