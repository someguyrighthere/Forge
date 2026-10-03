import io
import urllib.error
import urllib.request

import pytest

from forge import github, tools, ui


def test_github_tools_are_registered_read_only():
    names = {"github_prs", "github_issues", "github_actions"}
    assert names <= {t["function"]["name"] for t in tools.TOOLS}
    assert names <= tools.IMPLS.keys() and names <= tools.READ_ONLY
    assert not names & tools.NEEDS_APPROVAL
    assert ui.describe_call("github_prs", {"repo": "a/b", "number": 7}) == "a/b #7"
    assert ui.describe_call("github_issues", {"repo": "a/b", "query": "crash"}) == "a/b crash"


@pytest.mark.parametrize("remote, expected", [
    ("https://github.com/someguyrighthere/forge.git\n", "someguyrighthere/forge"),
    ("git@github.com:owner/repo.name.git", "owner/repo.name"),
    ("https://github.com/owner/repo/", "owner/repo"),
])
def test_repo_is_inferred_from_git_origin(monkeypatch, remote, expected):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": remote})())
    assert github._repo(None) == expected


def test_repo_errors(monkeypatch):
    monkeypatch.setattr(github.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": "https://gitlab.com/a/b"})())
    with pytest.raises(github.GitHubError, match="Could not tell which repository"):
        github._repo(None)
    assert github.github_prs("not valid") == "Error: Invalid repository 'not valid'. Use the form 'owner/name'."
    assert "owner/name" in github.github_issues("../../etc")


def test_pull_request_list_and_detail(monkeypatch):
    def fake(path, params=None, raw=False):
        if path.endswith("/pulls"):
            return [{"number": 3, "title": "Fix bug", "state": "open", "draft": False, "user": {"login": "ann"},
                     "head": {"ref": "fix"}, "base": {"ref": "main"}, "updated_at": "2026-01-02T03:04:05Z"}]
        if path.endswith("/pulls/3"):
            return {"number": 3, "title": "Fix bug", "state": "open", "merged": False, "user": {"login": "ann"},
                    "head": {"label": "ann:fix", "sha": "abc"}, "base": {"label": "o:main"}, "additions": 4,
                    "deletions": 1, "changed_files": 1, "mergeable_state": "blocked", "body": "Details"}
        if path.endswith("/files"):
            return [{"status": "modified", "filename": "a.py", "additions": 4, "deletions": 1}]
        if path.endswith("/check-runs"):
            return {"check_runs": [{"name": "tests", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}]}
        raise AssertionError(path)
    monkeypatch.setattr(github, "_request", fake)
    assert github.github_prs("o/r") == "#3 Fix bug [open] ann fix -> main updated 2026-01-02"
    detail = github.github_prs("o/r", number=3)
    assert "mergeable: blocked" in detail and "1 ok/other, 1 failing" in detail and "FAILED tests" in detail
    assert "modified  a.py (+4 -1)" in detail


def test_issue_list_excludes_pull_requests_and_detail_includes_comments(monkeypatch):
    issues = [{"number": 1, "title": "PR", "state": "open", "user": {"login": "a"}, "updated_at": "2026-01-01",
               "pull_request": {}},
              {"number": 2, "title": "Bug", "state": "open", "user": {"login": "b"}, "updated_at": "2026-01-02"}]
    seen = {}

    def fake(path, params=None, raw=False):
        seen[path] = params
        if path.endswith("/issues"):
            return issues
        if path.endswith("/issues/2"):
            return {"number": 2, "title": "Bug", "state": "open", "user": {"login": "b"}, "labels": [{"name": "bug"}],
                    "comments": 1, "body": "It breaks"}
        return [{"user": {"login": "c"}, "created_at": "2026-01-03T00:00:00Z", "body": "Same here"}]
    monkeypatch.setattr(github, "_request", fake)
    assert github.github_issues("o/r", limit=2) == "#2 Bug [open] b updated 2026-01-02"
    assert seen["/repos/o/r/issues"]["per_page"] == 8  # over-fetches because PRs are mixed into this endpoint
    detail = github.github_issues("o/r", number=2)
    assert "labels: bug" in detail and "--- c (2026-01-03)" in detail and "Same here" in detail


def test_actions_run_detail_only_fetches_logs_with_a_token(monkeypatch):
    calls = []

    def fake(path, params=None, raw=False):
        calls.append(path)
        if path.endswith("/runs/9"):
            return {"id": 9, "name": "CI", "run_number": 4, "head_branch": "main", "status": "completed",
                    "conclusion": "failure", "event": "push", "created_at": "2026-01-01T00:00:00Z", "display_title": "t"}
        if path.endswith("/jobs"):
            return {"jobs": [{"id": 5, "name": "test", "conclusion": "failure", "status": "completed",
                              "steps": [{"name": "pytest", "conclusion": "failure"}]}]}
        return "line1\nboom\n"
    monkeypatch.setattr(github, "_request", fake)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    without = github.github_actions("o/r", run_id=9)
    assert "step failed: pytest" in without and "set GITHUB_TOKEN" in without and not any("logs" in c for c in calls)
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    assert "boom" in github.github_actions("o/r", run_id=9)


def test_http_errors_become_helpful_messages(monkeypatch):
    def failing(code):
        def opener(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, code, "x", {"X-RateLimit-Remaining": "0"}, io.BytesIO())
        return opener
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(github._OPENER, "open", failing(404))
    assert "Not found (or private)" in github.github_prs("o/r") and "GITHUB_TOKEN" in github.github_prs("o/r")
    monkeypatch.setattr(github._OPENER, "open", failing(403))
    assert "rate limit" in github.github_prs("o/r")


def test_token_is_not_forwarded_to_other_hosts_on_redirect():
    handler = github._SameHostRedirects()
    request = urllib.request.Request("https://api.github.com/x", headers={"Authorization": "Bearer secret"})
    other = handler.redirect_request(request, None, 302, "Found", {}, "https://storage.example.com/log")
    same = handler.redirect_request(request, None, 302, "Found", {}, "https://api.github.com/y")
    assert not other.has_header("Authorization")
    assert same.get_header("Authorization") == "Bearer secret"
