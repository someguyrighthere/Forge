"""Interactive browsing (open, click, type) through an optional Playwright worker process.

The worker runs under the system Python (so it also works in the packaged app) and drives the
installed Edge or Chrome. Playwright is not bundled: without it these tools explain how to enable them.
"""
import atexit
import json
import os
import queue
import subprocess
import tempfile
import threading
import urllib.parse
from pathlib import Path

from forge.python_debug import _python_executable

MAX_OUTPUT = 8000
CALL_TIMEOUT = 45
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SETUP_HINT = ("Interactive browsing needs Playwright in the Python that Forge uses: install it from 'Optional downloads' "
              "in the Forge window, or run `pip install playwright` "
              "(it drives your installed Edge or Chrome, so no browser download is needed), then try again. "
              "Set FORGE_PYTHON to choose a different Python.")

# Runs inside the worker. Replies are JSON lines on the real stdout; anything else goes to stderr.
WORKER = r'''
import json, os, sys
proto, sys.stdout = sys.stdout, sys.stderr

def reply(value):
    proto.write(json.dumps(value) + "\n")
    proto.flush()

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    reply({"ok": False, "error": "missing-playwright"})
    sys.exit(0)

SNAPSHOT = """() => {
  document.querySelectorAll('[data-forge-id]').forEach((e) => e.removeAttribute('data-forge-id'));
  const query = 'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], [role=textbox], [role=checkbox]';
  const elements = [];
  for (const el of document.querySelectorAll(query)) {
    const box = el.getBoundingClientRect(), style = getComputedStyle(el);
    if (box.width < 2 || box.height < 2 || style.visibility === 'hidden' || style.display === 'none') continue;
    const index = elements.length + 1;
    el.setAttribute('data-forge-id', index);
    const label = (el.getAttribute('aria-label') || el.innerText || el.value || el.placeholder || el.getAttribute('title') || el.name || '')
      .trim().replace(/\s+/g, ' ').slice(0, 60);
    elements.push({i: index, tag: el.tagName.toLowerCase(), type: el.getAttribute('type') || el.getAttribute('role') || '',
                   label, href: el.getAttribute('href') || ''});
    if (elements.length >= 60) break;
  }
  return {title: document.title, url: location.href, text: document.body ? document.body.innerText.slice(0, 3000) : '', elements};
}"""

try:
    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(channel=os.environ.get("FORGE_BROWSER_CHANNEL", "msedge"), headless=True)
    page = browser.new_page(viewport={"width": 1280, "height": 900})
except Exception as error:
    reply({"ok": False, "error": "Could not start the browser: " + str(error)[:300]})
    sys.exit(0)
reply({"ok": True, "ready": True})


def settle():
    try:
        page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    page.wait_for_timeout(500)


for line in sys.stdin:
    request = json.loads(line)
    action = request.get("action")
    try:
        if action == "close":
            reply({"ok": True})
            break
        if action == "open":
            page.goto(request["url"], timeout=20000, wait_until="domcontentloaded")
            settle()
        else:
            target = page.locator('[data-forge-id="%d"]' % int(request["index"])).first
            if action == "click":
                target.click(timeout=5000)
            elif action == "type":
                target.fill(request["text"], timeout=5000)
                if request.get("submit"):
                    target.press("Enter")
            settle()
        reply({"ok": True, "snapshot": page.evaluate(SNAPSHOT)})
    except Exception as error:
        reply({"ok": False, "error": type(error).__name__ + ": " + str(error)[:300]})

try:
    browser.close()
    playwright.stop()
except Exception:
    pass
'''

_lock = threading.RLock()
_process: subprocess.Popen | None = None
_lines: "queue.Queue[str | None]" = queue.Queue()
_elements: dict[int, str] = {}
_page_url = ""
_signature = ""  # URL + text of the last page, to notice actions that changed nothing


def _clip(text: str) -> str:
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n... [truncated {len(text) - MAX_OUTPUT} characters]"


def _read_output(process: subprocess.Popen, sink: "queue.Queue[str | None]") -> None:
    assert process.stdout is not None
    for line in process.stdout:
        sink.put(line)
    sink.put(None)


def _stop() -> None:
    global _process
    with _lock:
        process, _process = _process, None
        if process is None:
            return
        try:
            if process.poll() is None and process.stdin is not None:
                process.stdin.write(json.dumps({"action": "close"}) + "\n")
                process.stdin.flush()
                process.wait(timeout=3)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass  # a broken pipe from a worker that already died


atexit.register(_stop)


def _receive(timeout: float) -> dict:
    try:
        line = _lines.get(timeout=timeout)
    except queue.Empty:
        raise TimeoutError("the browser did not respond in time") from None
    if line is None:
        raise RuntimeError("the browser process ended unexpectedly")
    return json.loads(line)


def _start() -> None:
    global _process, _lines
    python = _python_executable(Path.cwd().resolve())
    folder = Path(tempfile.mkdtemp(prefix="forge-browser-worker-"))
    script = folder / "worker.py"
    script.write_text(WORKER, encoding="utf-8")
    _lines = queue.Queue()
    _process = subprocess.Popen([python, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                creationflags=NO_WINDOW)
    threading.Thread(target=_read_output, args=(_process, _lines), daemon=True).start()
    try:
        hello = _receive(CALL_TIMEOUT)
    except (TimeoutError, RuntimeError):
        _stop()
        raise
    if not hello.get("ok"):
        _stop()
        error = hello.get("error", "unknown error")
        raise RuntimeError(SETUP_HINT if error == "missing-playwright" else error)


def _call(request: dict, unchanged_note: str | None = None) -> str:
    """Send one action to the worker (starting it if needed) and return a formatted page snapshot.

    If unchanged_note is given and the page (URL and text) is the same afterwards, the note is appended
    so the model learns that the action had no effect instead of assuming it worked.
    """
    with _lock:
        before = _signature
        try:
            if _process is None or _process.poll() is not None:
                _stop()
                _start()
            assert _process is not None and _process.stdin is not None
            _process.stdin.write(json.dumps(request) + "\n")
            _process.stdin.flush()
            result = _receive(CALL_TIMEOUT)
        except (TimeoutError, RuntimeError, OSError, json.JSONDecodeError) as error:
            _stop()
            return f"Error: {error}"
        if not result.get("ok"):
            return f"Error: {result.get('error', 'the browser action failed')}"
        page = _format(result["snapshot"])
        if unchanged_note and _signature == before:
            page += f"\n\nNote: {unchanged_note}"
        return page


def _format(snapshot: dict) -> str:
    global _page_url, _signature
    _elements.clear()
    _page_url = snapshot.get("url", "")
    _signature = _page_url + "\n" + snapshot.get("text", "")
    lines = [f"Page: {snapshot.get('title') or '(untitled)'}  <{_page_url}>", "", "Elements (use these numbers):"]
    for item in snapshot.get("elements", []):
        kind = item["tag"]
        if kind == "a":
            kind = "link"
        elif kind in ("input", "textarea"):
            kind = item["type"] if item["type"] in ("checkbox", "radio", "submit", "button") else "textbox"
        elif kind == "select":
            kind = "select"
        label = item["label"] or "(no label)"
        _elements[item["i"]] = f'{kind} "{label}"'
        suffix = f" -> {item['href'][:70]}" if item.get("href") and item["tag"] == "a" else ""
        lines.append(f'  [{item["i"]}] {kind} "{label}"{suffix}')
    if not snapshot.get("elements"):
        lines.append("  (none visible)")
    lines += ["", "Page text:", snapshot.get("text", "").strip()]
    return _clip("\n".join(lines))


def browser_open(url: str) -> str:
    """Open a page in the interactive browser session and list its clickable elements."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Error: url must start with http:// or https://"
    return _call({"action": "open", "url": url})


def browser_click(index: int) -> str:
    """Click element [index] from the latest page listing."""
    if not _elements:
        return "Error: no page is open. Call browser_open first."
    if int(index) not in _elements:
        return f"Error: there is no element [{index}] on the current page. Valid numbers: 1-{max(_elements)}."
    return _call({"action": "click", "index": int(index)}, unchanged_note="the page did not change after this click.")


def browser_type(index: int, text: str, submit: bool = False) -> str:
    """Type text into element [index], optionally pressing Enter."""
    if not _elements:
        return "Error: no page is open. Call browser_open first."
    if int(index) not in _elements:
        return f"Error: there is no element [{index}] on the current page. Valid numbers: 1-{max(_elements)}."
    note = ("pressing Enter did not change the page. This page may not use a form: click its Search or Submit "
            "button (see the numbered list) instead.") if submit else None
    return _call({"action": "type", "index": int(index), "text": str(text), "submit": bool(submit)}, unchanged_note=note)


def browser_close() -> str:
    """Close the interactive browser session."""
    global _signature
    _stop()
    _elements.clear()
    _signature = ""
    return "Browser session closed."


def describe_action(name: str, args: dict) -> str:
    """Approval text naming the element the model wants to act on."""
    try:
        index = int(args.get("index", 0))
    except (TypeError, ValueError):
        index = 0
    target = _elements.get(index, f"element [{index}]")
    where = f" on {_page_url}" if _page_url else ""
    if name == "browser_type":
        enter = " and press Enter" if args.get("submit") else ""
        return f"type {args.get('text', '')!r} into {target}{where}{enter}"
    return f"click [{index}] {target}{where}"
