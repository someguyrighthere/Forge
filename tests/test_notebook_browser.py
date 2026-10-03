import json
import subprocess

import pytest

from forge import browser, notebook, tools, ui

NOTEBOOK = {
    "cells": [
        {"cell_type": "markdown", "id": "m1", "metadata": {"tags": ["intro"]}, "source": ["# Title\n", "Intro"]},
        {"cell_type": "code", "id": "c1", "metadata": {}, "execution_count": 3, "source": ["print('hi')\n", "1 + 1"],
         "outputs": [{"output_type": "stream", "name": "stdout", "text": ["hi\n"]},
                     {"output_type": "execute_result", "execution_count": 3, "metadata": {},
                      "data": {"text/plain": ["2"]}}]},
        {"cell_type": "code", "id": "c2", "metadata": {}, "execution_count": 4, "source": ["1 / 0"],
         "outputs": [{"output_type": "error", "ename": "ZeroDivisionError", "evalue": "division by zero",
                      "traceback": []}]},
    ],
    "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}},
    "nbformat": 4, "nbformat_minor": 5,
}


@pytest.fixture
def nb(tmp_path):
    path = tmp_path / "analysis.ipynb"
    path.write_text(json.dumps(NOTEBOOK, indent=1) + "\n", encoding="utf-8")
    return path


def test_tools_are_registered_with_the_right_permissions():
    names = {t["function"]["name"] for t in tools.TOOLS}
    assert {"notebook_read", "notebook_edit", "browser_read"} <= names
    assert {"notebook_read", "browser_read"} <= tools.READ_ONLY
    assert "notebook_edit" in tools.NEEDS_APPROVAL and "notebook_edit" not in tools.READ_ONLY
    assert ui.describe_call("notebook_edit", {"path": "a.ipynb"}) == "a.ipynb"
    assert ui.describe_call("browser_read", {"url": "https://x.dev"}) == "https://x.dev"
    shown = ui.diff_text("notebook_edit", {"action": "replace", "index": 1, "source": "a = 1\nb = 2"})
    assert shown == "replace cell 1\n+ a = 1\n+ b = 2"


def test_notebook_read_shows_cells_and_outputs(nb):
    text = tools.notebook_read(str(nb))
    assert "3 cell(s), kernel: python3" in text
    assert "[0] markdown" in text and "# Title" in text
    assert "[1] code (run #3)" in text and "-- output --\nhi\n2" in text
    assert "ZeroDivisionError: division by zero" in text
    assert "[1] code" not in tools.notebook_read(str(nb), start=2)
    assert tools.notebook_read(str(nb.with_suffix(".txt"))).startswith("Error:")


def test_notebook_edit_replace_keeps_type_id_and_clears_outputs(nb):
    assert "Replaced cell 1" in tools.notebook_edit(str(nb), "replace", 1, "total = 5\ntotal")
    saved = json.loads(nb.read_text(encoding="utf-8"))
    cell = saved["cells"][1]
    assert cell["id"] == "c1" and cell["cell_type"] == "code"
    assert cell["source"] == ["total = 5\n", "total"] and cell["outputs"] == [] and cell["execution_count"] is None
    assert saved["cells"][0] == NOTEBOOK["cells"][0] and saved["metadata"] == NOTEBOOK["metadata"]
    tools.notebook_edit(str(nb), "replace", 0, "# New title")
    assert json.loads(nb.read_text(encoding="utf-8"))["cells"][0]["cell_type"] == "markdown"  # type is kept


def test_notebook_edit_result_lists_the_new_cell_indices(nb):
    result = tools.notebook_edit(str(nb), "insert", 0, "## Report", "markdown")
    assert result.startswith("Inserted a markdown cell at index 0.\nCells now:")
    # After the insert, the cell that was at index 2 is at index 3; the outline makes that visible.
    assert "[0] markdown: ## Report" in result and "[1] markdown: # Title" in result
    assert "[2] code: print('hi')" in result and "[3] code: 1 / 0" in result


def test_notebook_edit_insert_and_delete_with_undo(nb):
    original = nb.read_text(encoding="utf-8")
    assert "Inserted a markdown cell at index 0" in tools.notebook_edit(str(nb), "insert", 0, "Hello", "markdown")
    tools.notebook_edit(str(nb), "insert", 4, "x = 1")  # index == cell count appends
    cells = json.loads(nb.read_text(encoding="utf-8"))["cells"]
    assert [c["cell_type"] for c in cells] == ["markdown", "markdown", "code", "code", "code"]
    assert "Deleted cell 0" in tools.notebook_edit(str(nb), "delete", 0)
    assert len(json.loads(nb.read_text(encoding="utf-8"))["cells"]) == 4
    for _ in range(3):
        tools.undo_last()
    assert nb.read_text(encoding="utf-8") == original


def test_notebook_edit_rejects_bad_input_without_touching_the_file(nb):
    before = nb.read_text(encoding="utf-8")
    assert "between 0 and 2" in tools.notebook_edit(str(nb), "replace", 9, "x")
    assert "between 0 and 3" in tools.notebook_edit(str(nb), "insert", 9, "x")
    assert "action must be" in tools.notebook_edit(str(nb), "explode", 0)
    assert "cell_type must be" in tools.notebook_edit(str(nb), "insert", 0, "x", "python")
    nb.write_text("not json", encoding="utf-8")
    assert "invalid JSON" in tools.notebook_edit(str(nb), "delete", 0)
    assert nb.read_text(encoding="utf-8") == "not json" and before != "not json"


def test_html_to_text_skips_scripts_and_collects_links():
    html = ("<html><head><title> My  Page </title><style>p{}</style></head><body><h1>Hi</h1>"
            "<script>var secret = 1;</script><p>Para <b>bold</b></p>"
            "<a href='/docs'>Docs</a><a href='mailto:a@b.c'>Mail</a><a href='https://x.dev/a'><span>Deep</span> link</a>"
            "</body></html>")
    text = browser.html_to_text(html, "https://site.test/start")
    assert text.startswith("Title: My Page") and "Hi" in text and "Para bold" in text
    assert "secret" not in text and "p{}" not in text
    assert "- Docs: https://site.test/docs" in text and "- Deep link: https://x.dev/a" in text
    assert "mailto" not in text


def test_browser_read_runs_headless_browser_and_reports_problems(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        return subprocess.CompletedProcess(command, 0, "<html><body><p>Rendered 42</p></body></html>", "")
    monkeypatch.setattr(browser, "find_browser", lambda: "edge.exe")
    monkeypatch.setattr(browser.subprocess, "run", fake_run)
    assert "Rendered 42" in tools.browser_read("https://example.com/app", wait_seconds=3)
    assert "--dump-dom" in seen["command"] and "--virtual-time-budget=3000" in seen["command"]
    assert seen["command"][-1] == "https://example.com/app"
    assert tools.browser_read("file:///C:/secret.txt").startswith("Error: url must start with http")
    assert tools.browser_read("javascript:alert(1)").startswith("Error: url must start with http")

    monkeypatch.setattr(browser.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 1, "  ", ""))
    assert "returned nothing" in browser.browser_read("https://example.com")

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(browser.subprocess, "run", timeout)
    assert "took too long" in browser.browser_read("https://example.com")

    def missing():
        raise RuntimeError("No Edge or Chrome found.")
    monkeypatch.setattr(browser, "find_browser", missing)
    assert browser.browser_read("https://example.com") == "Error: No Edge or Chrome found."
