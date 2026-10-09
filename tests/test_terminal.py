import shutil
import time

import pytest

from forge import server


def test_terminal_commands_are_bounded_and_reject_null_characters():
    terminal = server.TerminalSession()

    assert terminal.write("   ") == "Enter a PowerShell command first."
    assert terminal.write("x" * (server.MAX_TERMINAL_COMMAND + 1)).startswith("Terminal commands must be at most")
    assert terminal.write("echo\x00bad") == "Terminal commands cannot contain a null character."


def test_terminal_output_is_bounded_and_reports_dropped_text():
    terminal = server.TerminalSession()
    chunk = "x" * (server.MAX_TERMINAL_OUTPUT // 2)
    terminal._append(chunk)
    terminal._append(chunk)
    terminal._append(chunk)

    state = terminal.snapshot()

    assert len(state["output"]) == server.MAX_TERMINAL_OUTPUT
    assert state["truncated"]
    assert state["seq"] == 3


def test_terminal_reports_a_clipped_single_output_chunk():
    terminal = server.TerminalSession()
    terminal._append("x" * (server.MAX_TERMINAL_OUTPUT + 1))

    state = terminal.snapshot()

    assert state["truncated"]
    assert len(state["output"]) == server.MAX_TERMINAL_OUTPUT


@pytest.mark.skipif(not (shutil.which("pwsh") or shutil.which("powershell")),
                    reason="PowerShell is not installed")
def test_terminal_runs_commands_and_tracks_the_shell_working_directory():
    terminal = server.TerminalSession()
    error = terminal.start()
    assert error is None
    try:
        assert terminal.write("Write-Output 'FORGE_TERMINAL_TEST'") is None
        deadline = time.monotonic() + 10
        state = terminal.snapshot()
        while "FORGE_TERMINAL_TEST" not in state["output"] and time.monotonic() < deadline:
            time.sleep(0.1)
            state = terminal.snapshot()
        assert "FORGE_TERMINAL_TEST" in state["output"]
        assert state["cwd"]

        assert terminal.write("Set-Location $env:TEMP") is None
        deadline = time.monotonic() + 10
        state = terminal.snapshot()
        while state["cwd"] == str(server.Path.cwd()) and time.monotonic() < deadline:
            time.sleep(0.1)
            state = terminal.snapshot()
        assert state["cwd"] != str(server.Path.cwd())
    finally:
        assert terminal.stop() is None
