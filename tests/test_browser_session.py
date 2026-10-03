import sys

import pytest

from forge import browser_session as bs
from forge import tools, ui

SNAPSHOT = {
    "title": "Shop", "url": "http://shop.test/", "text": "Welcome",
    "elements": [
        {"i": 1, "tag": "a", "type": "", "label": "About us", "href": "/about"},
        {"i": 2, "tag": "input", "type": "text", "label": "Search", "href": ""},
        {"i": 3, "tag": "button", "type": "", "label": "Go", "href": ""},
        {"i": 4, "tag": "input", "type": "checkbox", "label": "Gift", "href": ""},
    ],
}

# A stand-in for the Playwright worker: same JSON-lines protocol, scripted behaviour.
FAKE_WORKER = '''
import json, os, sys
mode = os.environ.get("FAKE_WORKER_MODE", "ok")
snapshot = json.loads(os.environ["FAKE_SNAPSHOT"])
def reply(v):
    sys.stdout.write(json.dumps(v) + "\\n"); sys.stdout.flush()
if mode == "missing":
    reply({"ok": False, "error": "missing-playwright"}); sys.exit(0)
reply({"ok": True, "ready": True})
for line in sys.stdin:
    request = json.loads(line)
    if request["action"] == "close":
        reply({"ok": True}); break
    if mode == "silent":
        continue
    if mode == "die-on-first-action" and not os.path.exists(os.environ["FAKE_MARKER"]):
        open(os.environ["FAKE_MARKER"], "w").close(); sys.exit(1)
    if request["action"] == "click" and request["index"] == 3:
        reply({"ok": False, "error": "TimeoutError: element is covered"}); continue
    snapshot["text"] = "last action: " + json.dumps(request, sort_keys=True)
    reply({"ok": True, "snapshot": snapshot})
'''


@pytest.fixture(autouse=True)
def fake_worker(monkeypatch, tmp_path):
    import json
    monkeypatch.setenv("FORGE_PYTHON", sys.executable)
    monkeypatch.setenv("FAKE_SNAPSHOT", json.dumps(SNAPSHOT))
    monkeypatch.setenv("FAKE_MARKER", str(tmp_path / "marker"))
    monkeypatch.setattr(bs, "WORKER", FAKE_WORKER)
    monkeypatch.setattr(bs, "CALL_TIMEOUT", 5)
    bs.browser_close()
    yield
    bs.browser_close()


def test_tools_are_registered_with_the_right_permissions():
    names = {"browser_open", "browser_click", "browser_type", "browser_close"}
    assert names <= {t["function"]["name"] for t in tools.TOOLS} and names <= tools.IMPLS.keys()
    assert {"browser_open", "browser_close"} <= tools.READ_ONLY
    assert {"browser_click", "browser_type"} <= tools.NEEDS_APPROVAL
    assert not {"browser_click", "browser_type"} & tools.READ_ONLY  # acting on a page is never available in plan mode


def test_open_lists_numbered_elements_with_roles():
    page = bs.browser_open("http://shop.test/")
    assert "Page: Shop  <http://shop.test/>" in page
    assert '[1] link "About us" -> /about' in page and '[2] textbox "Search"' in page
    assert '[3] button "Go"' in page and '[4] checkbox "Gift"' in page and "Welcome" in page.splitlines()[-1] or "Page text:" in page


def test_actions_are_sent_to_the_worker_and_return_the_updated_page():
    bs.browser_open("http://shop.test/")
    typed = bs.browser_type(2, "red shoes", submit=True)
    assert '"action": "type"' in typed and '"text": "red shoes"' in typed and '"submit": true' in typed
    assert '"action": "click"' in bs.browser_click(1)


def test_approval_text_names_the_element_and_page():
    bs.browser_open("http://shop.test/")
    assert ui.describe_call("browser_click", {"index": 1}) == 'click [1] link "About us" on http://shop.test/'
    typed = ui.diff_text("browser_type", {"index": 2, "text": "red shoes", "submit": True})
    assert typed == "type 'red shoes' into textbox \"Search\" on http://shop.test/ and press Enter"


def test_invalid_input_is_rejected_before_reaching_the_browser():
    assert bs.browser_click(1).startswith("Error: no page is open")
    assert bs.browser_open("file:///C:/secret").startswith("Error: url must start with http")
    assert bs.browser_open("javascript:alert(1)").startswith("Error: url must start with http")
    bs.browser_open("http://shop.test/")
    assert "no element [9]" in bs.browser_click(9) and "Valid numbers: 1-4" in bs.browser_click(9)
    assert "no element [0]" in bs.browser_type(0, "x")


def test_worker_reported_errors_are_returned_and_the_session_survives():
    bs.browser_open("http://shop.test/")
    assert bs.browser_click(3) == "Error: TimeoutError: element is covered"
    assert "Page: Shop" in bs.browser_click(1)


def test_missing_playwright_explains_how_to_enable_the_tools(monkeypatch):
    monkeypatch.setenv("FAKE_WORKER_MODE", "missing")
    result = bs.browser_open("http://shop.test/")
    assert result.startswith("Error:") and "pip install playwright" in result


def test_unresponsive_worker_times_out_cleanly(monkeypatch):
    monkeypatch.setenv("FAKE_WORKER_MODE", "silent")
    monkeypatch.setattr(bs, "CALL_TIMEOUT", 1)
    assert "did not respond in time" in bs.browser_open("http://shop.test/")


def test_a_crashed_worker_is_restarted_on_the_next_call(monkeypatch):
    monkeypatch.setenv("FAKE_WORKER_MODE", "die-on-first-action")
    assert "ended unexpectedly" in bs.browser_open("http://shop.test/")
    assert "Page: Shop" in bs.browser_open("http://shop.test/")  # the marker file now exists, so this worker survives


def test_actions_that_change_nothing_say_so(monkeypatch):
    # The fake worker reports the same URL and a text that includes the request, so use a fixed-text variant.
    monkeypatch.setattr(bs, "WORKER", FAKE_WORKER.replace('snapshot["text"] = "last action: " + json.dumps(request, sort_keys=True)', ''))
    bs.browser_open("http://shop.test/")
    assert "Enter did not change the page" in bs.browser_type(2, "red shoes", submit=True)
    assert "did not change after this click" in bs.browser_click(1)
    assert "Note:" not in bs.browser_type(2, "red shoes")  # typing alone is not expected to change the page text


def test_close_resets_the_session():
    bs.browser_open("http://shop.test/")
    assert bs.browser_close() == "Browser session closed."
    assert bs.browser_click(1).startswith("Error: no page is open")
