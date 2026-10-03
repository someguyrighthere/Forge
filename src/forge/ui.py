"""Terminal rendering and the approval prompt."""
import difflib
import re
import sys

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from forge import browser_session, notebook, python_stepper, tools

for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure:
        _reconfigure(encoding="utf-8", errors="replace")

console = Console()


def describe_call(name: str, args: dict) -> str:
    if name in ("read_file", "write_file", "edit_file", "list_dir", "notebook_read", "notebook_edit"):
        return str(args.get("path", "."))
    if name == "run_command":
        return str(args.get("command", ""))
    if name == "python_debug":
        return str(args.get("script", ""))
    if name == "python_debugger":
        return f"{args.get('action', '')} {args.get('script') or args.get('expression') or ''}".strip()
    if name.startswith("python_"):
        return f"{args.get('path', '')} {args.get('symbol') or args.get('module') or ''}".strip()
    if name.startswith("github_"):
        target = args.get("repo") or ""
        item = args.get("number") or args.get("run_id") or args.get("query") or ""
        return f"{target} {'#' if args.get('number') or args.get('run_id') else ''}{item}".strip()
    if name in ("glob_files", "grep"):
        return f"{args.get('pattern', '')} in {args.get('path', '.')}"
    if name in ("web_fetch", "browser_read", "browser_open"):
        return str(args.get("url", ""))
    if name in ("browser_click", "browser_type"):
        return browser_session.describe_action(name, args)
    if name == "web_search":
        return str(args.get("query", ""))
    if name == "task":
        return str(args.get("prompt", ""))[:80]
    return ""


def show_call(name: str, args: dict, indent: str = "") -> None:
    console.print(Text.assemble(indent, ("● ", "cyan"), (name, "bold"), (f"({describe_call(name, args)})", "dim")))


def show_result(name: str, result: str, indent: str = "") -> None:
    if name == "todo":
        for line in result.splitlines():
            console.print(f"{indent}  [green]{line}[/]" if line.startswith("[x]") else f"{indent}  {line}")
        return
    lines = result.splitlines() or [""]
    shown = lines[:6] if name in ("run_command", "grep", "glob_files", "web_search") else lines[:1]
    for i, line in enumerate(shown):
        console.print(Text.assemble(f"{indent}  ", ("⎿ " if i == 0 else "  ", "dim"), (line[:160], "red" if line.startswith("Error") else "dim")))
    if len(lines) > len(shown):
        console.print(Text(f"{indent}    … +{len(lines) - len(shown)} lines", style="dim"))


def clip_lines(text: str, limit: int = 40) -> str:
    lines = text.splitlines()
    return "\n".join(lines[:limit]) + (f"\n… +{len(lines) - limit} more lines" if len(lines) > limit else "")


def diff_text(name: str, args: dict) -> str:
    """The text shown to the user when approving a call: a unified diff, or the command."""
    if name == "edit_file":
        old, new = str(args.get("old_string", "")), str(args.get("new_string", ""))
        return "\n".join(difflib.unified_diff(old.splitlines(), new.splitlines(), "before", "after", lineterm="", n=2)) or "(no change)"
    if name == "write_file":
        p = tools.resolve(str(args.get("path", "")))
        new = str(args.get("content", "")).splitlines()
        old = p.read_text(encoding="utf-8", errors="replace").splitlines() if p.is_file() else []
        return clip_lines("\n".join(difflib.unified_diff(old, new, "current", "new", lineterm="", n=2)))
    if name == "notebook_edit":
        return notebook.describe_edit(args)
    if name == "python_debug":
        parts = [f"run {args.get('script', '')} {' '.join(str(a) for a in args.get('args') or [])}".rstrip()]
        parts += [f"breakpoint: {b}" for b in args.get("breakpoints") or []]
        parts += [f"evaluate: {e}" for e in args.get("expressions") or []]
        return "\n".join(parts)
    if name == "python_debugger":
        return python_stepper.describe_action(args)
    if name in ("browser_click", "browser_type"):
        return browser_session.describe_action(name, args)
    return str(args.get("command", ""))


class Approver:
    """Decides whether a tool call may run, based on the mode and the allow rules."""

    def __init__(self, mode: str, allow: list[str]):
        self.mode = mode
        self.allow_rules = [re.compile(r) for r in allow]
        self.always: set[str] = set()

    @property
    def auto(self) -> bool:
        return self.mode == "auto"

    def allow(self, name: str, args: dict) -> bool:
        if self.mode == "auto" or not tools.needs_approval(name, args) or name in self.always:
            return True
        if name == "run_command" and any(r.search(str(args.get("command", ""))) for r in self.allow_rules):
            return True
        return self.ask(name, args)

    def ask(self, name: str, args: dict) -> bool:
        lang = {"run_command": "powershell", "python_debug": "text", "python_debugger": "text",
                "browser_click": "text", "browser_type": "text"}.get(name, "diff")
        console.print(Panel(Syntax(diff_text(name, args), lang, theme="ansi_dark"),
                            title=f"[yellow]{name}[/] {describe_call(name, args)}", border_style="yellow"))
        while True:
            answer = console.input("[yellow]Allow? [/](y)es / (n)o / (a)lways this session: ").strip().lower()
            if answer in ("y", "yes", ""):
                return True
            if answer in ("n", "no"):
                return False
            if answer in ("a", "always"):
                self.always.add(name)
                return True


class StreamView:
    """Shows a spinner until the first token, then renders markdown live."""

    def __init__(self):
        self.status = console.status("[dim]Thinking…[/]", spinner="dots")
        self.live = None

    def __enter__(self):
        self.status.start()
        return self

    def first_token(self):
        self.status.stop()
        self.live = Live(Markdown(""), console=console, refresh_per_second=12, vertical_overflow="visible")
        self.live.start()

    def update(self, text: str):
        if self.live:
            self.live.update(Markdown(text))

    def finish(self, content: str, started: bool):
        self.status.stop()
        if self.live:
            self.live.stop()
        elif content:
            console.print(Markdown(content))

    def __exit__(self, *exc):
        self.status.stop()
        if self.live:
            self.live.stop()
        return False


class TerminalView:
    """What the agent loop talks to; the desktop window provides its own implementation."""

    def stream(self):
        return StreamView()

    def call(self, name: str, args: dict, depth: int = 0) -> None:
        show_call(name, args, "    " * depth)

    def result(self, name: str, result: str, depth: int = 0) -> None:
        show_result(name, result, "    " * depth)

    def notice(self, text: str, level: str = "info") -> None:
        console.print(f"[{'yellow' if level == 'warn' else 'red' if level == 'error' else 'dim'}]{text}[/]")

    def busy(self, text: str):
        return console.status(f"[dim]{text}[/]", spinner="dots")
