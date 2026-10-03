import sys

import pytest

from forge import python_stepper as ps
from forge import tools, ui

DEMO = """def square(n):
    result = n * n
    return result


def total(values):
    running = 0
    for value in values:
        running += square(value)
    return running


numbers = [1, 2, 3]
answer = total(numbers)
print("answer is", answer)
"""


@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("FORGE_PYTHON", sys.executable)
    (tmp_path / "demo.py").write_text(DEMO, encoding="utf-8")
    ps.python_debugger("stop")
    yield tmp_path
    ps.python_debugger("stop")


def test_tool_is_registered_and_only_start_and_eval_need_approval():
    assert "python_debugger" in {t["function"]["name"] for t in tools.TOOLS} and "python_debugger" in tools.IMPLS
    assert "python_debugger" not in tools.READ_ONLY  # it runs code, so plan mode must not offer it
    assert tools.needs_approval("python_debugger", {"action": "start"}) is True
    assert tools.needs_approval("python_debugger", {"action": "eval", "expression": "x"}) is True
    for action in ("step", "next", "out", "continue", "stop"):
        assert tools.needs_approval("python_debugger", {"action": action}) is False
    assert tools.needs_approval("read_file", {}) is False and tools.needs_approval("run_command", {}) is True
    assert ui.diff_text("python_debugger", {"action": "eval", "expression": "n * 2"}) == "evaluate in the paused program: n * 2"
    shown = ui.diff_text("python_debugger", {"action": "start", "script": "app.py", "breakpoints": ["app.py:3"]})
    assert shown == "debug app.py\nbreakpoint: app.py:3"
    assert ui.describe_call("python_debugger", {"action": "next"}) == "next"
    assert ui.describe_call("python_debugger", {"action": "start", "script": "app.py"}) == "start app.py"


def test_start_stops_on_the_first_line_with_source_context():
    text = ps.python_debugger("start", script="demo.py")
    assert "Stopped at the first line:" in text and "demo.py:1 in <module>()" in text
    assert "->    1  def square(n):" in text and "      2      result = n * n" in text


def test_next_steps_over_a_call_and_step_goes_into_it(project):
    ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:14"], stop_on_entry=False)
    over = ps.python_debugger("next")
    assert "Stepped over:" in over and "demo.py:15 in <module>()" in over and "answer = 14" in over
    ps.python_debugger("stop")
    ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:14"], stop_on_entry=False)
    into = ps.python_debugger("step")
    assert "Stepped:" in into and "demo.py:7 in total()" in into and "values = [1, 2, 3]" in into


def test_conditional_breakpoint_locals_stack_and_eval():
    text = ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:3 if n == 3"], stop_on_entry=False)
    assert "Stopped at a breakpoint:" in text and "demo.py:3 in square()" in text
    assert "n = 3" in text and "result = 9" in text
    assert "square <- " in text.replace("in square", "square") or "in square" in text and "in total" in text and "in <module>" in text
    assert ps.python_debugger("eval", expression="n * 10") == "n * 10 = 30"
    assert ps.python_debugger("eval", expression="[n, result]") == "[n, result] = [3, 9]"
    assert ps.python_debugger("eval", expression="nope + 1").startswith("Error evaluating: NameError")
    assert ps.python_debugger("eval", expression="") == "Error: eval needs an expression."


def test_out_returns_to_the_caller_and_continue_runs_to_the_end_with_output():
    ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:3 if n == 3"], stop_on_entry=False)
    out = ps.python_debugger("out")
    assert "Stepped out:" in out and "in total()" in out and "running = 14" in out
    finished = ps.python_debugger("continue")
    assert "finished (exit code 0)" in finished and "answer is 14" in finished
    assert ps.python_debugger("step").startswith("Error: no debug session is running")


def test_an_uncaught_exception_stops_for_inspection_and_any_step_ends_the_session(project):
    (project / "bad.py").write_text("def divide(x):\n    return 10 / x\n\ndivide(0)\n", encoding="utf-8")
    text = ps.python_debugger("start", script="bad.py", stop_on_entry=False)
    assert "Program raised ZeroDivisionError: division by zero" in text and "bad.py:2 in divide()" in text
    assert "x = 0" in text
    assert ps.python_debugger("eval", expression="x + 5") == "x + 5 = 5"
    ended = ps.python_debugger("next")
    assert "finished (exit code 1)" in ended and "ZeroDivisionError" in ended


def test_program_output_is_reported_and_input_does_not_hang(project):
    (project / "chat.py").write_text(
        "print('hello')\ntry:\n    name = input('name? ')\nexcept EOFError:\n    name = 'nobody'\nprint('hi', name)\n",
        encoding="utf-8")
    started = ps.python_debugger("start", script="chat.py", breakpoints=["chat.py:6"], stop_on_entry=False)
    assert "Program output since last stop:\nhello" in started
    text = ps.python_debugger("eval", expression="name")
    assert text == "name = 'nobody'"
    finished = ps.python_debugger("continue")
    assert "hi nobody" in finished


def test_library_code_is_not_stepped_into(project):
    (project / "lib.py").write_text("import json\nvalue = json.dumps({'a': 1})\nprint(value)\n", encoding="utf-8")
    ps.python_debugger("start", script="lib.py", breakpoints=["lib.py:2"], stop_on_entry=False)
    after = ps.python_debugger("step")
    assert after.splitlines()[0].endswith("lib.py:3 in <module>()")  # json.dumps ran, but was not entered


def test_stop_closes_the_session_and_actions_validate_their_input():
    ps.python_debugger("start", script="demo.py")
    assert ps.python_debugger("stop") == "Debug session closed."
    assert ps.python_debugger("stop") == "No debug session was running."
    assert ps.python_debugger("explode").startswith("Error: action must be one of")
    assert ps.python_debugger("start").startswith("Error: start needs a script path")
    assert "is not a Python script" in ps.python_debugger("start", script="missing.py")
    assert "Invalid breakpoint" in ps.python_debugger("start", script="demo.py", breakpoints=["nonsense"])
    assert ps.python_debugger("eval", expression="1").startswith("Error: no debug session is running")


def test_a_program_that_never_stops_times_out_and_closes_the_session(project, monkeypatch):
    (project / "loop.py").write_text("import time\nwhile True:\n    time.sleep(0.1)\n", encoding="utf-8")
    ps.python_debugger("start", script="loop.py")
    monkeypatch.setattr(ps, "CALL_TIMEOUT", 1)
    result = ps.python_debugger("continue")
    assert "did not stop within 1s" in result and "session was closed" in result
    assert ps.python_debugger("next").startswith("Error: no debug session is running")


def test_breakpoints_that_never_fire_are_explained(project):
    never_ran = ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:99"], stop_on_entry=False)
    assert "never stopped the program" in never_ran and "demo.py:99: that line never ran" in never_ran
    ps.python_debugger("stop")
    wrong_condition = ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:3 if n == 99"], stop_on_entry=False)
    assert "demo.py:3: reached 3 time(s) but the condition 'n == 99' was never true" in wrong_condition
    ps.python_debugger("stop")
    bad_name = ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:15 if price == 50"], stop_on_entry=False)
    assert "Stopped at a breakpoint" in bad_name and "condition failed with NameError" in bad_name
    ps.python_debugger("stop")
    never_ok = ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:3 if nope"], stop_on_entry=False)
    assert "Stopped at a breakpoint" in never_ok and "condition failed with NameError" in never_ok


def test_starting_again_replaces_the_previous_session():
    ps.python_debugger("start", script="demo.py")
    second = ps.python_debugger("start", script="demo.py", breakpoints=["demo.py:3"], stop_on_entry=False)
    assert "Stopped at a breakpoint:" in second and "demo.py:3" in second
