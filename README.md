# Forge

A local, Claude-Code-style coding agent with a desktop app (chat window) and a terminal mode. It runs entirely on your own machine through [Ollama](https://ollama.com): no API keys, no cloud, no cost.

## Install (Windows)

Run `installer\Forge-Setup-1.0.0.exe`. It installs Forge for your user (no admin needed, no Python needed), adds Start Menu and optional desktop shortcuts that open the Forge window (`forge-app.exe`, which uses Microsoft Edge in app mode), and can add `forge` to your PATH. It offers to download the default model (`qwen3:8b`, 5.2 GB) if Ollama is installed. Uninstall from Windows Settings > Apps.

The installer is not code-signed (that needs a paid certificate), so Windows SmartScreen may show "Windows protected your PC" the first time. Choose **More info**, then **Run anyway**. Installers downloaded by Forge's own updater are checked against the SHA-256 published on the GitHub release before they run.

Rebuild the installer with `powershell -File build.ps1` (needs PyInstaller, Pillow and Inno Setup 6).

## Releasing an update

1. Bump `__version__` in `src/forge/version.py`, then run `powershell -File build.ps1`.
2. On https://github.com/someguyrighthere/forge/releases create a release tagged `v<version>` (for example `v1.1.0`) and upload `installer\Forge-Setup-<version>.exe` to it.
3. Installed copies check GitHub when they start. Newer versions show an **Update** button in the window. Clicking it opens a "What's new" window with the release notes (the body of the GitHub release, so write them for users) and **Update now** / **Later** buttons. Update now downloads, verifies and installs the update, then restarts Forge, which shows the new version's notes once. Chats and settings are kept.

The repo must stay public so the check works without a login.

Developers: `python -m pip install -e .[dev]`, then `pytest`.

## Use

Open **Forge** from the Start Menu: pick a project folder, then chat. Sessions, model and mode switching, file attachments (@), slash commands (/), approval cards, model downloads and an integrated PowerShell terminal are in the window. Open **Terminal** in the header to start a persistent shell in the selected project folder; it runs as your Windows user and can modify any files that account can access. Or from a terminal:

```
forge                      interactive session in the current folder
forge "fix the failing test"   run one task and exit
forge -c                   continue the latest session
forge --ask                confirm every edit and command
forge --plan               read-only planning
forge -m qwen2.5-coder:7b  pick a model
forge --gui                open the desktop window
```

## What it can do

- **Tools:** read, write and edit files (with diffs), list, glob, grep, run shell commands, web search, fetch pages, a todo list, and a read-only research sub-agent (`task`).
- **Modes:** `auto` (default), `ask` (confirm edits and commands, with "always this session"), `plan` (read-only). Shift+Tab cycles them.
- **Safety:** destructive commands (recursive deletes of drives or home, format, shutdown, `git reset --hard`, force push, ...) are always refused. Add your own regexes under `deny` in the config. `/undo` reverts the last file edits.
- **Memory:** `/remember <note>` is injected into every session. An `AGENT.md` in the project folder is loaded automatically (`/init` writes one). The git status is included in the prompt.
- **Python code intelligence:** Pyright-backed tools provide symbols, inferred types and documentation, go-to-definition, references, rename-impact preview, call hierarchy, and diagnostics in both the terminal and desktop app. `python_module_usage` lists exactly which names one file uses from another module. These read-only tools are also available in plan mode, and the window shows a one-time hint if Pyright isn't installed.
- **Python debugging:** `python_debug` runs a script with breakpoints (optionally conditional, e.g. `app.py:42 if x > 3`) and reports the call stack, local variables and chosen expressions at each hit, or the locals at an uncaught exception. It executes code, so it asks for confirmation in `ask` mode and is unavailable in plan mode. It uses the project's `.venv`/`venv` Python, else `python` on PATH; set `FORGE_PYTHON` to override.
- **GitHub:** `github_prs`, `github_issues` and `github_actions` list and inspect pull requests (with changed files and failing checks), issues (with comments, or search) and Actions runs (jobs, failed steps). They are read-only and use the project's `origin` remote, or pass `owner/name`. Public repositories work without a login; set `GITHUB_TOKEN` (or `GH_TOKEN`) for private repositories, higher rate limits and failed-job logs. The token is only ever sent to `api.github.com`.
- **Notebooks:** `notebook_read` shows a Jupyter notebook's cells and outputs; `notebook_edit` replaces, inserts or deletes cells (it asks for confirmation in `ask` mode, and `/undo` reverts it). Forge edits the file only; it does not run cells.
- **Rendered web pages:** `browser_read` loads a page in the machine's headless Edge or Chrome, runs its JavaScript, and returns the visible text and links, for pages `web_fetch` sees as empty. It reads only; it can't click or type. Set `FORGE_BROWSER` to use a specific browser.
- **UI previews:** `open_preview` opens an existing project HTML file or a localhost development-server URL in your visible browser, so you can see the interface Forge created. It does not open remote URLs.
- **Interactive browsing (optional):** `browser_open`, `browser_click` and `browser_type` let Forge operate a page: it lists the page's numbered links, buttons and text boxes, and acts on them. Opening is read-only; clicking and typing ask for confirmation in `ask` mode (the card names the exact element) and are unavailable in plan mode. This needs Playwright in the Python Forge uses (install it from **Optional downloads**, or `pip install playwright`; it drives your installed Edge or Chrome, so no browser download). Without it the tools say how to enable them.
- **Long sessions:** old messages are summarised automatically before the context fills up (`/compact` does it on demand).
- **Sessions:** saved after every turn; `-c` or `/resume` reopens them.
- **Input:** `@path/to/file` attaches a file, Tab completes commands and paths, Esc+Enter inserts a new line, Ctrl+C stops the current task.

## Optional downloads

Two features need a small extra component. Neither is required; Forge works without them.

| Component | Unlocks | Needs |
|---|---|---|
| Pyright | The Python code tools (types, references, rename preview, call hierarchy, diagnostics) | Node.js |
| Playwright | Clicking and typing on web pages | Python and Edge or Chrome |

Click **🧩 Optional downloads** in the window's sidebar (or type `/addons`) to see what's installed and install either with one click. In the terminal, `/addons` lists them and `/addons install pyright` (or `playwright`) installs one. If a prerequisite is missing, the window says which one and links to it. To install by hand instead: `npm install --global pyright` and `pip install playwright`.

## Python code intelligence

Forge uses Pyright's language server for Python symbols, hover/type information, go-to-definition, references, rename-impact preview, call hierarchy, and diagnostics. Install it from **Optional downloads** (above), or yourself with Node.js:

```powershell
npm install --global pyright
```

Forge finds Pyright on `PATH` or in npm's global folder, so no restart is needed after installing. If it is installed somewhere else, set `PYRIGHT_LANGSERVER` to the full path of the `pyright-langserver` executable (or `pyright-langserver.cmd` on Windows) before starting Forge. Pyright reads the selected project configuration, including `pyrightconfig.json` and supported `pyproject.toml` settings.

## Config

`forge --config` prints the path (`~/.forge/config.toml`): model, context size, default mode, `think` for reasoning models, step limit, and the `allow` / `deny` command regexes. The `AGENT_MODEL` and `OLLAMA_HOST` environment variables are also honored.

## Hardware notes

Tested on an RTX 5050 with 8 GB VRAM and 16 GB RAM: `qwen3:8b` at a 16k context is the best tool-user that fits. `qwen2.5-coder:7b` is faster but less reliable at calling tools. Lower `num_ctx` if you run out of memory.
