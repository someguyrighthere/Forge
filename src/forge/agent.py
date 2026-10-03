"""The agent loop: prompt building, tool execution, context compaction, sub-agents."""
import json
import platform
import re
import subprocess
from pathlib import Path

from forge import config, llm, tools, ui

COMPACT_AT = 0.75
SUBAGENT_STEPS = 12


def git_summary() -> str:
    try:
        out = subprocess.run(["git", "status", "--short", "--branch"], capture_output=True, text=True,
                             timeout=5, encoding="utf-8", errors="replace",
                             creationflags=tools.NO_WINDOW, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"\n\nGit status:\n{tools.clip(out, 1500)}" if out else ""


def system_prompt(mode: str = "auto") -> str:
    extra = ""
    memory = config.read_memory()
    if memory:
        extra += "\n\nThings the user asked you to remember:\n" + tools.clip(memory, 3000)
    notes = Path.cwd() / "AGENT.md"
    if notes.is_file():
        extra += "\n\nProject instructions from AGENT.md:\n" + tools.clip(notes.read_text(encoding="utf-8", errors="replace"), 4000)
    if mode == "plan":
        extra += ("\n\nPLAN MODE: you can only read and search. Investigate, then reply with a clear numbered plan. "
                  "Do not try to edit files or run commands.")
    return (
        "You are Forge, a local coding assistant running on the user's computer through Ollama. "
        "Help with software tasks by using your tools: read files before editing them, make small precise edits, "
        "and run commands (tests, builds) to check your work. Use the todo tool to plan multi-step tasks. "
        "Never invent file contents or command results; if you are unsure, look. "
        "To learn what a file contains, call read_file with its path. To find things, use list_dir, glob_files or grep. "
        "For Python symbols, inferred types, definitions, references, rename impact, callers/callees, or diagnostics, prefer the python_* "
        "tools; pass the symbol's name (symbol=...) rather than guessing line and column numbers. To see runtime values or why a script crashes, use "
        "python_debug with breakpoints instead of adding print statements. "
        "Callers and references only show WHERE something is used: before you say what a function does or returns, "
        "read it with read_file or python_hover. To see what one file uses from another module, call "
        "python_module_usage instead of listing the other module's symbols. State only what a tool result showed. "
        "For GitHub pull requests, issues, or CI/Actions runs, use the github_* tools. "
        "Use notebook_read and notebook_edit for .ipynb files, never read_file or edit_file. "
        "If web_fetch returns an empty or 'loading' page, retry with browser_read, which runs the page's JavaScript. "
        "To click or type on a web page, use browser_open and then browser_click/browser_type with the element numbers it lists; "
        "re-read the numbers after every action, and call browser_close when done. If an action changes nothing, "
        "try another element (for example click the page's Search button) before giving up. "
        "Use the task tool for broad research across many files. "
        "Only use web_search or web_fetch when the user gives a URL or asks you to look something up online. "
        "Do not call a tool unless it helps with the request. "
        "Be brief. When you finish, summarise what you did in a sentence or two.\n"
        f"Operating system: {platform.system()}. Working directory: {Path.cwd()}." + git_summary() + extra
    )


def expand_mentions(text: str) -> str:
    """Attach the contents of @file mentions to the message."""
    blocks = []
    for ref in dict.fromkeys(re.findall(r"(?<!\S)@(\S+)", text)):
        p = tools.resolve(ref)
        if p.is_file():
            blocks.append(f"[contents of {p}]\n{tools.read_file(str(p))}")
    return text + ("\n\n" + "\n\n".join(blocks) if blocks else "")


def compact(cfg: dict, messages: list, view) -> None:
    """Summarise the older part of the conversation so long sessions keep fitting in the context window."""
    if len(messages) < 6:
        return
    keep = 4
    while keep < len(messages) - 1 and messages[-keep].get("role") == "tool":
        keep += 1
    old, recent = messages[1:-keep], messages[-keep:]
    transcript = "\n".join(f"{m['role']}: {tools.clip(str(m.get('content', '')), 1500)}" for m in old)
    prompt = [
        {"role": "system", "content": "Summarise this coding session for your own later use. Keep file paths, decisions, "
                                       "commands that worked, errors and unfinished tasks. Be concise."},
        {"role": "user", "content": tools.clip(transcript, 12000)}]
    try:
        with view.busy("Compacting conversation…"):
            summary = llm.complete(cfg["model"], prompt, cfg["num_ctx"])
    except llm.OllamaError:
        summary = ""
    summary = summary or "(earlier messages were dropped to free up context)"
    messages[1:] = [{"role": "user", "content": "Summary of the earlier conversation:\n" + summary}] + recent
    view.notice("Older messages were summarised to free up context.")


def _parse_args(args) -> dict:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return {}
    return args if isinstance(args, dict) else {}


def run_subagent(cfg: dict, prompt: str, view) -> str:
    messages = [{"role": "system", "content": system_prompt("plan") + "\nYou are a sub-agent. Answer the question "
                 "concisely with the key facts and file paths you found."},
                {"role": "user", "content": prompt}]
    run_turn(cfg, messages, ui.Approver("plan", []), view, depth=1, max_steps=SUBAGENT_STEPS)
    return messages[-1].get("content") or "(the sub-agent produced no answer)"


def run_turn(cfg: dict, messages: list, approver: ui.Approver, view, used: int = 0, depth: int = 0,
             max_steps: int = 0) -> dict:
    """Run the model until it stops calling tools. Returns the last token usage."""
    plan = approver.mode == "plan"
    allowed = tools.READ_ONLY if plan or depth else set(tools.IMPLS) | {"task"}
    if depth:
        allowed = allowed - {"task"}
    schemas = [t for t in tools.TOOLS if t["function"]["name"] in allowed]
    usage = {"in": used, "out": 0}
    for _ in range(max_steps or cfg["max_steps"]):
        if llm.CANCEL.is_set():
            raise llm.Cancelled()
        if not depth and usage["in"] + usage["out"] > cfg["num_ctx"] * COMPACT_AT:
            compact(cfg, messages, view)
            usage = {"in": 0, "out": 0}
        if depth:
            content, calls, usage, started = llm.chat(cfg["model"], messages, schemas, cfg["num_ctx"], cfg["think"])
        else:
            with view.stream() as s:
                content, calls, usage, started = llm.chat(cfg["model"], messages, schemas, cfg["num_ctx"], cfg["think"],
                                                          on_text=s.update, on_first_token=s.first_token)
                s.finish(content, started)
        assistant = {"role": "assistant", "content": content}
        if calls:
            assistant["tool_calls"] = calls
        messages.append(assistant)
        if not calls:
            return usage
        for call in calls:
            if llm.CANCEL.is_set():
                raise llm.Cancelled()
            fn = call.get("function", {})
            name, args = fn.get("name", ""), _parse_args(fn.get("arguments"))
            view.call(name, args, depth)
            if name not in allowed:
                result = f"Error: tool {name} is not available{' in plan mode' if plan else ''}."
            elif not approver.allow(name, args):
                result = "The user declined this action."
            else:
                try:
                    if name == "task":
                        result = run_subagent(cfg, str(args.get("prompt", "")), view)
                    else:
                        result = tools.IMPLS[name](**args)
                except llm.Cancelled:
                    raise
                except TypeError as e:
                    result = f"Error: bad arguments for {name}: {e}"
                except Exception as e:  # tool failures go back to the model
                    result = f"Error: {e}"
            if llm.CANCEL.is_set():
                raise llm.Cancelled()
            view.result(name, result, depth)
            messages.append({"role": "tool", "tool_name": name, "content": result})
    if not depth:
        view.notice(f"Stopped after {cfg['max_steps']} steps. Send a message to continue.", "warn")
    else:
        messages.append({"role": "assistant", "content": "(sub-agent ran out of steps)"})
    return usage
