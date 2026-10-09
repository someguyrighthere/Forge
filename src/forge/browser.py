"""Read JavaScript-rendered web pages with the machine's own headless Edge or Chrome."""
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse
import webbrowser
from html.parser import HTMLParser
from pathlib import Path

MAX_OUTPUT = 8000
MAX_LINKS = 25
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
SKIPPED = {"script", "style", "noscript", "template", "svg", "head"}
BLOCKS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "pre",
          "header", "footer", "nav", "main", "table", "ul", "ol", "blockquote", "form"}


class _Extractor(HTMLParser):
    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.title = base, ""
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if tag in SKIPPED:
            self._skip += 1
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag == "a" and not self._skip:
            self._href = dict(attrs).get("href")
            self._link_text = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag in SKIPPED and self._skip:
            self._skip -= 1
        if tag in BLOCKS:
            self.parts.append("\n")
        if tag == "a" and self._href:
            label = " ".join("".join(self._link_text).split())
            target = urllib.parse.urljoin(self.base, self._href)
            if label and target.startswith(("http://", "https://")) and (label, target) not in self.links:
                self.links.append((label, target))
            self._href = None

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        if not self._skip:
            self.parts.append(data)
            if self._href is not None:
                self._link_text.append(data)


def html_to_text(html: str, base_url: str = "") -> str:
    """Visible text of a rendered page plus its first links."""
    extractor = _Extractor(base_url)
    extractor.feed(html)
    text = re.sub(r"[ \t]+", " ", "".join(extractor.parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    out = [f"Title: {' '.join(extractor.title.split())}" if extractor.title.strip() else "", text]
    if extractor.links:
        out.append("\nLinks:")
        out += [f"- {label[:80]}: {target}" for label, target in extractor.links[:MAX_LINKS]]
    return "\n".join(part for part in out if part)


def find_browser() -> str:
    configured = os.environ.get("FORGE_BROWSER")
    if configured:
        return configured
    candidates = []
    for variable in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                           Path(base) / "Google/Chrome/Application/chrome.exe"]
    candidates += [Path(found) for name in ("msedge", "google-chrome", "chromium", "chrome")
                   if (found := shutil.which(name))]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("No Edge or Chrome found. Install one, or set FORGE_BROWSER to its full path.")


def browser_read(url: str, wait_seconds: int = 5) -> str:
    """Load a page in headless Edge/Chrome, run its JavaScript, and return the visible text and links."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return "Error: url must start with http:// or https://"
    wait = min(max(int(wait_seconds or 5), 1), 20)
    try:
        browser = find_browser()
    except RuntimeError as error:
        return f"Error: {error}"
    with tempfile.TemporaryDirectory(prefix="forge-browser-") as profile:
        command = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   f"--user-data-dir={profile}", f"--virtual-time-budget={wait * 1000}", "--dump-dom", url]
        try:
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=wait + 40, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            return f"Error: the browser took too long to load {url}."
        except OSError as error:
            return f"Error: could not start the browser: {error}"
    if not run.stdout.strip():
        return f"Error: the browser returned nothing for {url} (exit code {run.returncode})."
    text = html_to_text(run.stdout, url)
    return text[:MAX_OUTPUT] + (f"\n... [truncated {len(text) - MAX_OUTPUT} characters]" if len(text) > MAX_OUTPUT else "")


def open_preview(target: str) -> str:
    """Open a project HTML file or localhost web app in the user's visible browser."""
    target = target.strip()
    if not target:
        return "Error: provide a project HTML file or localhost URL to preview."

    if "://" in target:
        parsed = urllib.parse.urlsplit(target)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            return "Error: previews may only open HTTP(S) URLs served from localhost."
        if parsed.username or parsed.password:
            return "Error: preview URLs cannot contain credentials."
        try:
            parsed.port
        except ValueError:
            return "Error: preview URL has an invalid port."
        url = target
    else:
        root = Path.cwd().resolve()
        path = Path(target)
        if not path.is_absolute():
            path = root / path
        try:
            path = path.resolve(strict=True)
            path.relative_to(root)
        except (OSError, ValueError):
            return "Error: preview files must exist inside the current project folder."
        if path.is_dir():
            path = next((candidate for candidate in (path / "index.html", path / "index.htm")
                         if candidate.is_file()), path)
        if not path.is_file() or path.suffix.lower() not in {".html", ".htm"}:
            return "Error: choose an existing HTML file (or a folder containing index.html)."
        url = path.as_uri()

    try:
        opened = webbrowser.open(url, new=2)
    except OSError as error:
        return f"Error: could not open the preview in your browser: {error}"
    if not opened:
        return "Error: the system could not open the preview in a browser."
    return f"Opened the preview in your browser: {url}"
