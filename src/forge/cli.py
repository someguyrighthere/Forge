"""Command-line entry point and interactive session."""
import argparse
import re
import sys
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.panel import Panel
from rich.table import Table

from forge import addons, config, llm, tools, ui
from forge.agent import compact, expand_mentions, run_turn, system_prompt

from forge.version import __version__ as VERSION
console = ui.console
VIEW = ui.TerminalView()
MODES = ["auto", "ask", "plan"]

COMMANDS = {
    "/help": "show commands",
    "/model": "list or switch models  (/model <name or number>)",
    "/mode": "auto, ask or plan  (Shift+Tab cycles)",
    "/plan": "toggle read-only plan mode",
    "/yolo": "toggle auto-approve",
    "/undo": "undo the last file edit",
    "/init": "write an AGENT.md describing this project",
    "/clear": "start a fresh conversation",
    "/compact": "summarise older messages now",
    "/remember": "save a note the agent sees in every session",
    "/memory": "show saved notes",
    "/addons": "optional downloads  (/addons install pyright | playwright)",
    "/resume": "list or reopen saved sessions  (/resume <number>)",
    "/exit": "quit",
}


class InputCompleter(Completer):
    def get_completions(self, document, complete_event):
        word = document.get_word_before_cursor(WORD=True)
        if document.text_before_cursor.startswith("/") and " " not in document.text_before_cursor:
            for cmd, desc in COMMANDS.items():
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word), display_meta=desc)
        elif word.startswith("@"):
            partial = word[1:]
            folder = Path(partial).parent if ("/" in partial or "\\" in partial) else Path(".")
            prefix = Path(partial).name
            try:
                for entry in sorted(folder.iterdir())[:200]:
                    if entry.name.lower().startswith(prefix.lower()) and entry.name not in tools.SKIP_DIRS:
                        shown = (str(folder / entry.name) if folder != Path(".") else entry.name)
                        yield Completion(shown + ("/" if entry.is_dir() else ""), start_position=-len(partial))
            except OSError:
                return


def banner(cfg: dict, approver: ui.Approver) -> None:
    mode_note = {"auto": "auto-approve on", "ask": "asks before edits and commands", "plan": "plan mode (read-only)"}[approver.mode]
    body = (f"[bold cyan]Forge[/] [dim]v{VERSION}[/]  ·  local coding agent\n"
            f"[dim]model[/] {cfg['model']}   [dim]context[/] {cfg['num_ctx']}   [dim]{mode_note}[/]\n"
            f"[dim]cwd[/]   {Path.cwd()}\n[dim]/help for commands · @file to attach · Esc+Enter for a new line · Ctrl+C stops a task[/]")
    console.print(Panel(body, border_style="cyan", expand=False))


def pick_model(cfg: dict, arg: str) -> None:
    models = llm.list_models()
    names = [m["name"] for m in models]
    if not arg:
        table = Table(box=None, show_header=False, padding=(0, 2))
        for i, m in enumerate(models, 1):
            mark = "[cyan]●[/]" if m["name"] == cfg["model"] else " "
            table.add_row(mark, str(i), m["name"], f"[dim]{m.get('size', 0) / 1e9:.1f} GB[/]")
        console.print(table)
        console.print("[dim]Switch with /model <name or number>.[/]")
        return
    choice = names[int(arg) - 1] if arg.isdigit() and 0 < int(arg) <= len(names) else arg
    if choice not in names:
        console.print(f"[red]No installed model named {choice}. Run `ollama pull {choice}` first.[/]")
        return
    cfg["model"] = choice
    console.print(f"[green]Model set to {choice}.[/]")


def show_addons(arg: str) -> None:
    """List the optional downloads, or install one (`/addons install pyright`)."""
    words = arg.split()
    if len(words) == 2 and words[0] == "install":
        with console.status(f"Installing {words[1]}… this can take a minute"):
            ok, message = addons.run_install(words[1].lower())
        console.print(f"[green]{message}[/]" if ok else f"[red]{message}[/]")
        return
    for item in addons.get_addons():
        if item["installed"]:
            state = "[green]installed[/]"
        elif item["blocker"]:
            state = f"[yellow]{item['blocker']}[/]"
        else:
            state = f"[dim]not installed: /addons install {item['id']}[/]"
        console.print(f"[cyan]{item['name']:<11}[/] {state}\n            [dim]{item['description']}[/]")


def show_sessions(arg: str, cfg: dict, messages: list, state: dict) -> None:
    files = config.list_sessions()[:15]
    if not arg:
        for i, f in enumerate(files, 1):
            data = config.load_session(f)
            first = next((m["content"] for m in data["messages"] if m["role"] == "user"), "")
            console.print(f"[cyan]{i:>2}[/] {f.stem}  [dim]{first[:70].strip()!r}[/]")
        console.print("[dim]Reopen with /resume <number>.[/]" if files else "[dim]No saved sessions.[/]")
        return
    if not arg.isdigit() or not 0 < int(arg) <= len(files):
        console.print("[red]Pick a number from /resume.[/]")
        return
    load_into(files[int(arg) - 1], cfg, messages, state)


def load_into(path: Path, cfg: dict, messages: list, state: dict) -> None:
    data = config.load_session(path)
    messages[:] = data["messages"]
    messages[0] = {"role": "system", "content": system_prompt()}
    state["id"], state["used"] = path.stem, 0
    console.print(f"[green]Resumed {path.stem} ({len(messages) - 1} messages).[/]")


def handle_command(line: str, cfg: dict, approver: ui.Approver, messages: list, state: dict):
    """Returns None when handled, or a prompt string to send to the model."""
    cmd, _, arg = line.partition(" ")
    arg = arg.strip()
    if cmd == "/help":
        for name, desc in COMMANDS.items():
            console.print(f"[cyan]{name:<10}[/] {desc}")
    elif cmd == "/model":
        try:
            pick_model(cfg, arg)
        except llm.OllamaError as e:
            console.print(f"[red]{e}[/]")
    elif cmd in ("/mode", "/plan", "/yolo"):
        new = (arg if cmd == "/mode" else
               ("auto" if approver.mode == "plan" else "ask" if approver.mode == "auto" else "auto") if cmd == "/yolo" else
               "auto" if approver.mode == "plan" else "plan")
        if new not in MODES:
            console.print("[red]Modes: auto, ask, plan.[/]")
        else:
            approver.mode = new
            messages[0] = {"role": "system", "content": system_prompt(new)}
            console.print(f"[green]Mode: {new}[/]")
    elif cmd == "/undo":
        console.print(tools.undo_last())
    elif cmd == "/clear":
        messages[:] = [{"role": "system", "content": system_prompt(approver.mode)}]
        tools.TODOS.clear()
        state["used"], state["id"] = 0, config.new_session_id()
        console.print("[dim]Conversation cleared.[/]")
    elif cmd == "/compact":
        compact(cfg, messages, VIEW)
    elif cmd == "/remember":
        if arg:
            config.add_memory(arg)
            messages[0] = {"role": "system", "content": system_prompt(approver.mode)}
            console.print("[green]Saved.[/]")
        else:
            console.print("[red]Usage: /remember <note>[/]")
    elif cmd == "/memory":
        console.print(config.read_memory() or "[dim]No notes saved yet. Use /remember <note>.[/]")
        console.print(f"[dim]{config.MEMORY_FILE}[/]")
    elif cmd in ("/resume", "/sessions"):
        show_sessions(arg, cfg, messages, state)
    elif cmd == "/addons":
        show_addons(arg)
    elif cmd == "/init":
        return ("Explore this project (list files, read the main ones) and write an AGENT.md in the working directory "
                "with: what the project is, how to build/run/test it, the code layout, and conventions to follow. "
                "Keep it under 40 lines.")
    elif cmd == "/exit":
        raise EOFError
    else:
        console.print(f"[red]Unknown command {cmd}. Try /help.[/]")
    return None


def make_session(cfg: dict, approver: ui.Approver, state: dict) -> PromptSession:
    kb = KeyBindings()

    @kb.add("enter")
    def _(event):
        event.current_buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event):
        event.current_buffer.insert_text("\n")

    @kb.add("s-tab")
    def _(event):
        approver.mode = MODES[(MODES.index(approver.mode) + 1) % len(MODES)]
        event.app.invalidate()

    def toolbar():
        used = state["used"]
        return (f" {cfg['model']}  |  mode: {approver.mode}  |  context: {used:,}/{cfg['num_ctx']:,}  |  "
                f"{Path.cwd().name}  |  Shift+Tab mode")

    config.ensure_home()
    return PromptSession(history=FileHistory(str(config.HISTORY_FILE)), completer=InputCompleter(),
                         complete_while_typing=True, multiline=True, key_bindings=kb, bottom_toolbar=toolbar,
                         style=Style.from_dict({"bottom-toolbar": "noreverse #888888 bg:default"}))


def send(text: str, cfg: dict, approver: ui.Approver, messages: list, state: dict) -> None:
    messages[0] = {"role": "system", "content": system_prompt(approver.mode)}
    start = len(messages)
    messages.append({"role": "user", "content": expand_mentions(text)})
    try:
        state["used"] = sum(run_turn(cfg, messages, approver, VIEW, state["used"]).values())
    except KeyboardInterrupt:
        del messages[start + 1:]
        console.print("\n[yellow]Stopped.[/]")
    except llm.OllamaError as e:
        del messages[start + 1:]
        console.print(f"[red]Error: {e}[/]")
    config.save_session(state["id"], cfg["model"], messages)


def check_ollama(cfg: dict) -> bool:
    try:
        names = [m["name"] for m in llm.list_models()]
    except llm.OllamaError as e:
        console.print(f"[red]{e}[/]\n[dim]Install Ollama from https://ollama.com and start it, then retry.[/]")
        return False
    if cfg["model"] not in names:
        console.print(f"[red]Model {cfg['model']} is not installed.[/] Run: [bold]ollama pull {cfg['model']}[/]"
                      + (f"\n[dim]Installed: {', '.join(names)}[/]" if names else ""))
        return False
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="forge", description="Local coding agent powered by Ollama.")
    ap.add_argument("prompt", nargs="*", help="run one task and exit")
    ap.add_argument("-m", "--model")
    ap.add_argument("--ctx", type=int, help="context window in tokens")
    ap.add_argument("--ask", action="store_true", help="confirm edits and commands")
    ap.add_argument("--plan", action="store_true", help="read-only plan mode")
    ap.add_argument("-c", "--continue", dest="cont", action="store_true", help="resume the latest session")
    ap.add_argument("--gui", action="store_true", help="open the desktop window")
    ap.add_argument("--config", action="store_true", help="print the config file path")
    ap.add_argument("--version", action="version", version=f"forge {VERSION}")
    args = ap.parse_args(argv)

    if args.gui:
        from forge import server
        return server.run()
    cfg = config.load()
    if args.config:
        print(config.CONFIG_FILE)
        return 0
    cfg["model"] = args.model or cfg["model"]
    cfg["num_ctx"] = args.ctx or cfg["num_ctx"]
    tools.USER_DENY[:] = cfg["deny"]
    mode = "plan" if args.plan else "ask" if args.ask else cfg["mode"]
    approver = ui.Approver(mode if mode in MODES else "auto", cfg["allow"])
    if not check_ollama(cfg):
        return 1

    state = {"used": 0, "id": config.new_session_id()}
    messages = [{"role": "system", "content": system_prompt(approver.mode)}]
    if args.cont and config.list_sessions():
        load_into(config.list_sessions()[0], cfg, messages, state)

    if args.prompt:
        send(" ".join(args.prompt), cfg, approver, messages, state)
        return 0

    banner(cfg, approver)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    session = make_session(cfg, approver, state) if interactive else None
    while True:
        try:
            line = (session.prompt([("class:prompt", "› ")]) if session else input("› ")).strip()
        except KeyboardInterrupt:
            continue
        except EOFError:
            break
        if not line:
            continue
        if line.startswith("/"):
            try:
                line = handle_command(line, cfg, approver, messages, state)
            except EOFError:
                break
            if not line:
                continue
        send(line, cfg, approver, messages, state)
    console.print("[dim]Bye.[/]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
