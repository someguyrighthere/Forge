"""Settings, memory and session storage under ~/.forge."""
import json
import os
import time
import tomllib
from pathlib import Path

HOME = Path(os.environ.get("FORGE_HOME", Path.home() / ".forge"))
CONFIG_FILE = HOME / "config.toml"
MEMORY_FILE = HOME / "memory.md"
HISTORY_FILE = HOME / "history"
SESSIONS = HOME / "sessions"

DEFAULTS = {
    "model": "qwen3:8b",
    "num_ctx": 16384,
    "mode": "auto",       # auto = run everything, ask = confirm edits and commands, plan = read-only
    "think": False,       # let reasoning models (qwen3) think before answering
    "max_steps": 25,
    "allow": [],          # regexes of commands that never need confirmation in ask mode
    "deny": [],           # regexes of commands that are always refused
}

DEFAULT_CONFIG_TEXT = """\
# Forge settings. Delete a line to fall back to the default.
model = "qwen3:8b"
num_ctx = 16384
mode = "auto"        # auto | ask | plan
think = false
max_steps = 25

# Regexes matched against shell commands.
allow = ["^git (status|diff|log)", "^(ls|dir|pytest|python -m pytest)"]
deny = []
"""


def ensure_home() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    SESSIONS.mkdir(exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")


def load() -> dict:
    ensure_home()
    cfg = dict(DEFAULTS)
    try:
        cfg.update(tomllib.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")))
    except (tomllib.TOMLDecodeError, OSError):
        pass
    if os.environ.get("AGENT_MODEL"):
        cfg["model"] = os.environ["AGENT_MODEL"]
    return cfg


def read_memory() -> str:
    return MEMORY_FILE.read_text(encoding="utf-8", errors="replace") if MEMORY_FILE.is_file() else ""


def add_memory(note: str) -> None:
    ensure_home()
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- {note}\n")


def save_session(session_id: str, model: str, messages: list) -> None:
    ensure_home()
    (SESSIONS / f"{session_id}.json").write_text(
        json.dumps({"model": model, "cwd": os.getcwd(), "messages": messages}), encoding="utf-8")


def list_sessions() -> list[Path]:
    ensure_home()
    return sorted(SESSIONS.glob("*.json"), reverse=True)


def load_session(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def new_session_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S")
