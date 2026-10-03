"""Read-only GitHub tools (pull requests, issues, Actions runs) over the REST API."""
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
MAX_OUTPUT = 8000
NO_WINDOW = 0x08000000 if os.name == "nt" else 0
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REMOTE_PATTERN = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


class GitHubError(RuntimeError):
    """A GitHub request failed in a way worth showing to the model."""


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """Never forward the token to a different host (log downloads redirect to storage)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlparse(newurl).netloc != urllib.parse.urlparse(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


_OPENER = urllib.request.build_opener(_SameHostRedirects)


def _clip(text: str, limit: int = MAX_OUTPUT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""


def _request(path: str, params: dict | None = None, raw: bool = False):
    url = API + path + ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v}) if params else "")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "forge-agent",
               "X-GitHub-Api-Version": "2022-11-28"}
    if _token():
        headers["Authorization"] = f"Bearer {_token()}"
    try:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=20) as response:
            body = response.read(2_000_000)
    except urllib.error.HTTPError as error:
        hint = "" if _token() else " Set GITHUB_TOKEN to access private repositories and raise rate limits."
        if error.code == 404:
            raise GitHubError(f"Not found (or private).{hint}") from None
        if error.code in (401, 403):
            reset = error.headers.get("X-RateLimit-Remaining") == "0"
            raise GitHubError(("GitHub rate limit reached." if reset else f"GitHub denied access (HTTP {error.code}).") + hint) from None
        raise GitHubError(f"GitHub returned HTTP {error.code}.") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise GitHubError(f"Could not reach GitHub: {error}") from None
    text = body.decode("utf-8", errors="replace")
    return text if raw else json.loads(text)


def _repo(repo: str | None) -> str:
    if repo:
        if not REPO_PATTERN.match(repo.strip()):
            raise GitHubError(f"Invalid repository {repo!r}. Use the form 'owner/name'.")
        return repo.strip()
    try:
        remote = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True, timeout=5,
                                stdin=subprocess.DEVNULL, creationflags=NO_WINDOW).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        remote = ""
    match = REMOTE_PATTERN.search(remote)
    if not match:
        raise GitHubError("Could not tell which repository to use. Pass repo='owner/name' or run Forge inside a "
                          "git clone whose 'origin' is on GitHub.")
    return f"{match[1]}/{match[2]}"


def _limit(value) -> int:
    return min(max(int(value or 10), 1), 30)


def _day(timestamp: str | None) -> str:
    return (timestamp or "")[:10]


def _guard(function):
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except GitHubError as error:
            return f"Error: {error}"
    wrapper.__name__, wrapper.__doc__ = function.__name__, function.__doc__
    return wrapper


@_guard
def github_prs(repo: str | None = None, number: int | None = None, state: str = "open", limit: int = 10) -> str:
    """List pull requests, or show one (with changed files and CI check results)."""
    name = _repo(repo)
    if number:
        pr = _request(f"/repos/{name}/pulls/{int(number)}")
        files = _request(f"/repos/{name}/pulls/{int(number)}/files", {"per_page": 30})
        checks = _request(f"/repos/{name}/commits/{pr['head']['sha']}/check-runs", {"per_page": 50}).get("check_runs", [])
        status = "merged" if pr.get("merged") else pr["state"]
        lines = [f"#{pr['number']} {pr['title']} [{status}{', draft' if pr.get('draft') else ''}]",
                 f"{pr['user']['login']}: {pr['head']['label']} -> {pr['base']['label']}  "
                 f"(+{pr.get('additions', 0)} -{pr.get('deletions', 0)}, {pr.get('changed_files', 0)} files, "
                 f"mergeable: {pr.get('mergeable_state', 'unknown')})", "", (pr.get("body") or "(no description)")[:1500]]
        if checks:
            failing = [c for c in checks if c.get("conclusion") in ("failure", "timed_out", "cancelled", "action_required")]
            lines += ["", f"Checks: {len(checks) - len(failing)} ok/other, {len(failing)} failing"]
            lines += [f"  FAILED {c['name']} (run it with github_actions)" for c in failing]
        lines += ["", "Files:"] + [f"  {f['status']:<9} {f['filename']} (+{f['additions']} -{f['deletions']})" for f in files]
        return _clip("\n".join(lines))
    prs = _request(f"/repos/{name}/pulls", {"state": state, "per_page": _limit(limit)})
    return _clip("\n".join(
        f"#{p['number']} {p['title']} [{'draft' if p.get('draft') else p['state']}] {p['user']['login']} "
        f"{p['head']['ref']} -> {p['base']['ref']} updated {_day(p['updated_at'])}" for p in prs) or "No pull requests.")


@_guard
def github_issues(repo: str | None = None, number: int | None = None, state: str = "open", limit: int = 10,
                  query: str | None = None) -> str:
    """List or search issues, or show one with its recent comments."""
    name = _repo(repo)
    if number:
        issue = _request(f"/repos/{name}/issues/{int(number)}")
        comments = _request(f"/repos/{name}/issues/{int(number)}/comments", {"per_page": 30})
        labels = ", ".join(label["name"] for label in issue.get("labels", [])) or "none"
        lines = [f"#{issue['number']} {issue['title']} [{issue['state']}] by {issue['user']['login']}",
                 f"labels: {labels}; comments: {issue.get('comments', 0)}", "", (issue.get("body") or "(no description)")[:2000]]
        for comment in comments[-10:]:
            lines += ["", f"--- {comment['user']['login']} ({_day(comment['created_at'])}):", comment["body"][:800]]
        return _clip("\n".join(lines))
    if query:
        found = _request("/search/issues", {"q": f"repo:{name} is:issue {query}", "per_page": _limit(limit)})["items"]
    else:
        found = [i for i in _request(f"/repos/{name}/issues", {"state": state, "per_page": min(_limit(limit) * 4, 100)})
                 if "pull_request" not in i][:_limit(limit)]
    return _clip("\n".join(
        f"#{i['number']} {i['title']} [{i['state']}] {i['user']['login']} updated {_day(i['updated_at'])}"
        for i in found) or "No issues.")


@_guard
def github_actions(repo: str | None = None, run_id: int | None = None, branch: str | None = None,
                   status: str | None = None, limit: int = 10) -> str:
    """List workflow runs, or show one run's jobs, failed steps and (with a token) failed-job log tails."""
    name = _repo(repo)
    if run_id:
        run = _request(f"/repos/{name}/actions/runs/{int(run_id)}")
        jobs = _request(f"/repos/{name}/actions/runs/{int(run_id)}/jobs", {"per_page": 50}).get("jobs", [])
        lines = [f"Run {run['id']}: {run['name']} #{run['run_number']} on {run['head_branch']} "
                 f"[{run['status']}/{run.get('conclusion') or 'pending'}] {run['event']} {_day(run['created_at'])}",
                 run.get("display_title", "")]
        for job in jobs:
            lines.append(f"\nJob {job['name']} [{job.get('conclusion') or job['status']}]")
            lines += [f"  step failed: {s['name']}" for s in job.get("steps", []) if s.get("conclusion") == "failure"]
            if job.get("conclusion") == "failure":
                if _token():
                    try:
                        log = _request(f"/repos/{name}/actions/jobs/{job['id']}/logs", raw=True)
                        lines.append("  log tail:\n" + "\n".join("    " + l for l in log.splitlines()[-40:]))
                    except GitHubError as error:
                        lines.append(f"  (log unavailable: {error})")
                else:
                    lines.append("  (set GITHUB_TOKEN to include this job's log; GitHub requires auth for logs)")
        return _clip("\n".join(lines))
    runs = _request(f"/repos/{name}/actions/runs", {"per_page": _limit(limit), "branch": branch, "status": status})
    return _clip("\n".join(
        f"{r['id']} {r['name']} #{r['run_number']} {r['head_branch']} [{r.get('conclusion') or r['status']}] "
        f"{r['event']} {_day(r['created_at'])}" for r in runs.get("workflow_runs", [])) or "No workflow runs.")
