import sys

import pytest

from forge import python_debug as debug
from forge import tools, ui


@pytest.fixture(autouse=True)
def use_current_python(monkeypatch):
    monkeypatch.setenv("FORGE_PYTHON", sys.executable)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "app.py").write_text(
        "import sys\n"
        "\n"
        "def total(values):\n"
        "    result = 0\n"
        "    for value in values:\n"
        "        result += value\n"
        "    return result\n"
        "\n"
        "print('sum', total([1, 2, 3]))\n"
        "print('args', sys.argv[1:])\n",
        encoding="utf-8")
    return tmp_path


def test_describe_call_covers_each_tool_family():
    assert ui.describe_call("grep", {"pattern": "foo", "path": "src"}) == "foo in src"
    assert ui.describe_call("glob_files", {"pattern": "*.py"}) == "*.py in ."
    assert ui.describe_call("python_debug", {"script": "app.py"}) == "app.py"
    assert ui.describe_call("python_references", {"path": "a.py", "symbol": "average"}) == "a.py average"
    assert ui.describe_call("run_command", {"command": "pytest"}) == "pytest"


def test_debug_tool_is_registered_and_needs_approval():
    assert "python_debug" in {t["function"]["name"] for t in tools.TOOLS}
    assert "python_debug" in tools.IMPLS
    assert "python_debug" in tools.NEEDS_APPROVAL
    assert "python_debug" not in tools.READ_ONLY  # it executes code, so plan mode must not offer it
    summary = ui.diff_text("python_debug", {"script": "app.py", "breakpoints": ["app.py:6"], "expressions": ["result"]})
    assert "breakpoint: app.py:6" in summary and "evaluate: result" in summary


def test_breakpoint_captures_locals_stack_and_expressions(project):
    report = debug.python_debug("app.py", ["app.py:6"], ["value * 10"], args=["x"], max_hits=2)
    assert "Breakpoint hits: 2" in report and "stopped after the hit limit" in report
    assert "Hit 1:" in report and "in total" in report
    assert "value = 1" in report and "result = 0" in report and "[expr] value * 10 = 10" in report
    assert "value = 2" in report and "result = 1" in report
    assert "<module>" in report  # the call stack includes the caller


def test_conditional_breakpoint_and_program_output(project):
    report = debug.python_debug("app.py", ["app.py:6 if value == 3"])
    assert "Breakpoint hits: 1" in report and "value = 3" in report and "result = 3" in report
    assert "exit code 0" in report and "sum 6" in report


def test_uncaught_exception_reports_locals_at_crash(project):
    (project / "crash.py").write_text(
        "def divide(a, b):\n    ratio = a / b\n    return ratio\n\ndivide(4, 0)\n", encoding="utf-8")
    report = debug.python_debug("crash.py")
    assert "Uncaught ZeroDivisionError" in report and "crash.py:2" in report
    assert "a = 4" in report and "b = 0" in report and "exit code 1" in report


def test_invalid_inputs_return_errors(project):
    assert "not a Python script" in debug.python_debug("missing.py")
    assert "Invalid breakpoint" in debug.python_debug("app.py", ["nonsense"])
    assert "does not exist" in debug.python_debug("app.py", ["nope.py:3"])


def test_timeout_keeps_hits_collected_so_far(project):
    (project / "loop.py").write_text(
        "import time\nfor step in range(1000):\n    marker = step\n    time.sleep(0.2)\n", encoding="utf-8")
    report = debug.python_debug("loop.py", ["loop.py:3"], max_hits=20, timeout=1)
    assert "timed out after 1s" in report and "marker = 0" in report
