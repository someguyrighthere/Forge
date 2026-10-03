"""Update check against GitHub Releases, and a silent in-place upgrade."""
import hashlib
import json
import re
import subprocess
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from forge import config
from forge.version import __version__

REPO = "someguyrighthere/forge"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
ASSET = re.compile(r"^Forge-Setup-[\d.]+\.exe$")
MAX_NOTES = 4000


def parse(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v.split("-")[0])[:4])


def check(timeout: float = 6.0) -> dict | None:
    """Returns {version, url, sha256, notes} when a newer release exists, else None."""
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "forge-updater"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            rel = json.load(r)
        latest = str(rel.get("tag_name", "")).lstrip("vV")
        if not latest or parse(latest) <= parse(__version__):
            return None
        for a in rel.get("assets", []):
            if ASSET.match(a.get("name", "")):
                digest = a.get("digest") or ""
                return {"version": latest, "url": a["browser_download_url"],
                        "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
                        "notes": (rel.get("body") or "")[:MAX_NOTES]}
    except (OSError, ValueError, KeyError):
        pass
    return None


def release_notes(version: str, timeout: float = 6.0) -> str:
    """The published notes for one release, or '' when they cannot be fetched."""
    url = f"https://api.github.com/repos/{REPO}/releases/tags/v{version}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "forge-updater"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return (json.load(r).get("body") or "")[:MAX_NOTES]
    except (OSError, ValueError):
        return ""


def _marker() -> Path:
    return config.HOME / "last_version"


def pending_whats_new() -> dict | None:
    """{version, notes} when this version has not been acknowledged yet after an upgrade, else None.

    The last acknowledged version is stored in ~/.forge/last_version. Copies that predate this file
    but already have chats or history are treated as upgrades; a brand-new install is recorded silently.
    """
    try:
        previous = _marker().read_text(encoding="utf-8").strip() or None
    except OSError:
        previous = None
    if previous is None:
        used_before = any(config.SESSIONS.glob("*.json")) or config.HISTORY_FILE.exists()
        if not used_before:
            mark_seen()
            return None
    elif parse(previous) >= parse(__version__):
        if previous != __version__:
            mark_seen()  # downgraded or reinstalled an older build: nothing new to announce
        return None
    return {"version": __version__, "notes": release_notes(__version__)}


def mark_seen() -> None:
    try:
        config.ensure_home()
        _marker().write_text(__version__, encoding="utf-8")
    except OSError:
        pass


def download(info: dict, progress=None) -> Path:
    host = urllib.parse.urlparse(info["url"]).hostname or ""
    if not (host == "github.com" or host.endswith(".githubusercontent.com")):
        raise RuntimeError("Refusing to download an update from " + host)
    dest = Path(tempfile.gettempdir()) / f"Forge-Setup-{info['version']}.exe"
    sha = hashlib.sha256()
    req = urllib.request.Request(info["url"], headers={"User-Agent": "forge-updater"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while chunk := r.read(1 << 16):
            f.write(chunk)
            sha.update(chunk)
            done += len(chunk)
            if progress and total:
                progress(round(done * 100 / total))
    if info["sha256"] and sha.hexdigest().lower() != info["sha256"].lower():
        dest.unlink(missing_ok=True)
        raise RuntimeError("The downloaded update failed its integrity check.")
    return dest


def run_installer(path: Path) -> None:
    """Starts the installer detached; the caller must exit so the files can be replaced."""
    cmd = [str(path), "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/LOG={Path(tempfile.gettempdir()) / 'forge-update.log'}"]
    quiet = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    detached = 0x00000008 | 0x00000200
    try:
        subprocess.Popen(cmd, creationflags=detached | 0x01000000, **quiet)  # leave any job object that would kill it
    except OSError:
        subprocess.Popen(cmd, creationflags=detached, **quiet)
