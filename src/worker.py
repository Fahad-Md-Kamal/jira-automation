#!/usr/bin/env python3
"""Worker for posting commit-message comments after Git pushes.

This script is intended to be launched by .git/hooks/pre-push. Git does not
have a client-side post-push hook, so the pre-push hook starts this worker in
the background. For Gerrit refs/for pushes, the worker can optionally wait for
the change to appear and post a Gerrit comment. For hosted Git providers, the
worker can add an existing or create-review URL to the Jira comment.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ZERO_SHA = "0" * 40

# Broad fallback: any WORD-NUMBER token that looks like a Jira key.
_ISSUE_KEY_BROAD_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")

# Minimum character length for a Jira project prefix to avoid false positives
# such as single-letter tokens in some regex engines.
_MIN_PREFIX_LEN = 2


def _build_issue_key_re() -> re.Pattern[str]:
    """Return a regex for Jira issue keys.

    When PUSH_HOOK_JIRA_PROJECTS is set (comma-separated project prefixes,
    for example ``P1732,MYAPP,OPEN``), the regex is anchored to those exact
    prefixes so that generic tokens like ``UTF-8`` or ``HTTP-404`` are never
    matched.

    When the variable is absent or empty, the broad fallback is used, which
    matches any ``UPPERCASE_WORD-NUMBER`` token.
    """
    raw = os.environ.get("PUSH_HOOK_JIRA_PROJECTS", "").strip()
    if not raw:
        return _ISSUE_KEY_BROAD_RE

    prefixes = [
        p.strip().upper()
        for p in raw.split(",")
        if len(p.strip()) >= _MIN_PREFIX_LEN
    ]
    if not prefixes:
        return _ISSUE_KEY_BROAD_RE

    alternation = "|".join(re.escape(p) for p in prefixes)
    return re.compile(rf"\b((?:{alternation})-\d+)\b")


@dataclass(frozen=True)
class GerritRemote:
    ssh_target: str
    project: str | None
    port: int | None = None


@dataclass(frozen=True)
class GerritChange:
    number: str
    patchset: str
    url: str | None = None


@dataclass(frozen=True)
class ReviewLink:
    label: str
    url: str


def run(
    args: list[str],
    *,
    cwd: Path,
    check: bool = True,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def git(args: list[str], *, cwd: Path) -> str:
    return run(["git", *args], cwd=cwd).stdout.strip()


def parse_remote_url(remote_url: str) -> GerritRemote:
    parsed = urllib.parse.urlparse(remote_url)

    if parsed.scheme == "ssh":
        host = parsed.hostname
        if not host:
            raise ValueError(f"Could not parse Gerrit host from {remote_url!r}")
        user_prefix = f"{parsed.username}@" if parsed.username else ""
        project = parsed.path.lstrip("/") or None
        return GerritRemote(
            ssh_target=f"{user_prefix}{host}",
            project=project,
            port=parsed.port,
        )

    scp_like = re.match(r"(?:(?P<user>[^@/:]+)@)?(?P<host>[^:/]+):(?P<path>.+)", remote_url)
    if scp_like:
        user = scp_like.group("user")
        host = scp_like.group("host")
        project = scp_like.group("path").lstrip("/") or None
        return GerritRemote(
            ssh_target=f"{user + '@' if user else ''}{host}",
            project=project,
        )

    if "/" not in remote_url and ":" not in remote_url:
        return GerritRemote(ssh_target=remote_url, project=None)

    raise ValueError(f"Unsupported Gerrit SSH remote URL: {remote_url!r}")


def parse_target_branch(remote_ref: str) -> str | None:
    if remote_ref.startswith("refs/for/"):
        branch = remote_ref[len("refs/for/") :]
        return branch.split("%", 1)[0] or None

    if remote_ref.startswith("refs/heads/"):
        branch = remote_ref[len("refs/heads/") :]
        return branch or None

    return None


def ssh_base(remote: GerritRemote) -> list[str]:
    args = ["ssh"]
    if remote.port:
        args.extend(["-p", str(remote.port)])
    args.append(remote.ssh_target)
    return args


def query_gerrit_change(
    *,
    remote: GerritRemote,
    commit_sha: str,
    branch: str | None,
    repo_root: Path,
) -> GerritChange | None:
    query_terms = [f"commit:{commit_sha}"]
    if remote.project:
        query_terms.append(f"project:{remote.project}")
    if branch:
        query_terms.append(f"branch:{branch}")

    cmd = [
        *ssh_base(remote),
        "gerrit",
        "query",
        "--format=JSON",
        "--current-patch-set",
        *query_terms,
    ]

    result = run(cmd, cwd=repo_root, check=False, timeout=20)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        return None

    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "stats" or not payload.get("number"):
            continue

        current_patch_set = payload.get("currentPatchSet") or {}
        patchset = current_patch_set.get("number")
        if patchset is None:
            continue
        return GerritChange(
            number=str(payload["number"]),
            patchset=str(patchset),
            url=payload.get("url"),
        )

    return None


def wait_for_gerrit_change(
    *,
    remote: GerritRemote,
    commit_sha: str,
    branch: str | None,
    repo_root: Path,
    wait_seconds: int,
    poll_seconds: int,
) -> GerritChange | None:
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() <= deadline:
        change = query_gerrit_change(
            remote=remote,
            commit_sha=commit_sha,
            branch=branch,
            repo_root=repo_root,
        )
        if change:
            return change
        time.sleep(poll_seconds)
    return None


def marker_path(repo_root: Path, marker_name: str) -> Path:
    git_path = git(["rev-parse", "--git-path", "push-comment-hook"], cwd=repo_root)
    marker_dir = (repo_root / git_path).resolve()
    marker_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", marker_name)
    return marker_dir / safe_name


def with_marker(repo_root: Path, marker_name: str, action) -> bool:
    marker = marker_path(repo_root, marker_name)
    lock = marker.with_suffix(marker.suffix + ".lock")
    if marker.exists():
        print(f"Skipping already-posted comment marker: {marker.name}")
        return False

    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError:
        print(f"Skipping comment already in progress: {marker.name}")
        return False

    try:
        if action() is False:
            return False
        marker.write_text(str(int(time.time())) + "\n", encoding="utf-8")
        return True
    finally:
        lock.unlink(missing_ok=True)


def commit_message(commit_sha: str, repo_root: Path) -> str:
    return git(["log", "-1", "--format=%B", commit_sha], cwd=repo_root).strip()


def parse_issue_keys(message: str) -> list[str]:
    pattern = _build_issue_key_re()
    keys: list[str] = []
    for key in pattern.findall(message):
        if key not in keys:
            keys.append(key)
    return keys


def resolve_review_link(
    change: GerritChange | None,
    *,
    remote_url: str,
    branch: str | None,
    repo_root: Path,
) -> ReviewLink | None:
    # 1. Explicit override always wins.
    explicit = os.environ.get("PUSH_HOOK_REVIEW_URL") or os.environ.get("PUSH_HOOK_PR_URL")
    if explicit:
        return ReviewLink(label="Review", url=explicit)

    # 2. Gerrit change URL.
    if change and change.url:
        return ReviewLink(label="Gerrit review", url=change.url)

    repository_url = _repository_web_url(remote_url)
    if not repository_url:
        return None

    if not branch:
        return ReviewLink(label="Repository", url=repository_url)

    provider = _review_provider(repository_url)

    # 3. GitHub CLI: look up an open PR for the pushed branch.
    if provider == "github" and shutil.which("gh"):
        try:
            result = subprocess.run(
                ["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"],
                cwd=repo_root,
                text=True,
                capture_output=True,
                timeout=10,
            )
            url = result.stdout.strip()
            if url.startswith("http"):
                return ReviewLink(label="GitHub PR", url=url)
        except Exception:
            pass

    # 4. Fallback: construct the provider's create-review URL. For unknown
    #    providers, still include the pushed repository in the Jira comment.
    return _review_creation_link(provider, repository_url, branch) or ReviewLink(
        label="Repository",
        url=repository_url,
    )


def _repository_web_url(remote_url: str) -> str | None:
    """Convert common Git remote formats to a repository web URL."""
    explicit = os.environ.get("PUSH_HOOK_REPOSITORY_URL")
    if explicit:
        return _strip_git_suffix(explicit.rstrip("/"))

    parsed = urllib.parse.urlparse(remote_url)
    if parsed.scheme in {"http", "https", "ssh"}:
        host = parsed.hostname
        if not host:
            return None
        path = parsed.path.lstrip("/")
        scheme = parsed.scheme if parsed.scheme in {"http", "https"} else "https"
        return _repository_web_url_from_parts(host, path, parsed.port, scheme)

    scp_like = re.match(r"(?:(?:[^@/:]+)@)?(?P<host>[^:/]+):(?P<path>.+)", remote_url)
    if scp_like:
        return _repository_web_url_from_parts(
            scp_like.group("host"),
            scp_like.group("path"),
        )

    return None


def _repository_web_url_from_parts(
    host: str,
    path: str,
    port: int | None = None,
    scheme: str = "https",
) -> str:
    path = path.lstrip("/")
    if host == "ssh.dev.azure.com" and path.startswith("v3/"):
        parts = path.removeprefix("v3/").split("/", 2)
        if len(parts) == 3:
            organization, project, repository = parts
            return (
                f"https://dev.azure.com/{organization}/{project}/_git/"
                f"{_strip_git_suffix(repository)}"
            )

    host_port = f"{host}:{port}" if port and port != 22 else host
    return _strip_git_suffix(f"{scheme}://{host_port}/{path}".rstrip("/"))


def _strip_git_suffix(value: str) -> str:
    return value[:-4] if value.endswith(".git") else value


def _review_provider(repository_url: str) -> str | None:
    explicit = os.environ.get("PUSH_HOOK_REVIEW_PROVIDER", "").strip().lower()
    aliases = {
        "azure": "azure-devops",
        "azure_devops": "azure-devops",
        "github-enterprise": "github",
        "gitlab-self-managed": "gitlab",
    }
    if explicit:
        return aliases.get(explicit, explicit)

    host = (urllib.parse.urlparse(repository_url).hostname or "").lower()
    if host == "github.com" or host.startswith("github."):
        return "github"
    if host == "gitlab.com" or host.startswith("gitlab."):
        return "gitlab"
    if host == "bitbucket.org":
        return "bitbucket"
    if host == "dev.azure.com" or host.endswith(".visualstudio.com"):
        return "azure-devops"
    return None


def _review_creation_link(
    provider: str | None,
    repository_url: str,
    branch: str,
) -> ReviewLink | None:
    repository_url = repository_url.rstrip("/")
    quoted_branch = urllib.parse.quote(branch, safe="/")

    if provider == "github":
        return ReviewLink(
            label="Create GitHub PR",
            url=f"{repository_url}/compare/{quoted_branch}?expand=1",
        )
    if provider == "gitlab":
        query = urllib.parse.urlencode({"merge_request[source_branch]": branch})
        return ReviewLink(
            label="Create GitLab MR",
            url=f"{repository_url}/-/merge_requests/new?{query}",
        )
    if provider == "bitbucket":
        query = urllib.parse.urlencode({"source": branch})
        return ReviewLink(
            label="Create Bitbucket PR",
            url=f"{repository_url}/pull-requests/new?{query}",
        )
    if provider == "azure-devops":
        query = urllib.parse.urlencode({"sourceRef": f"refs/heads/{branch}"})
        return ReviewLink(
            label="Create Azure DevOps PR",
            url=f"{repository_url}/pullrequestcreate?{query}",
        )
    return None


def comment_body(
    *,
    message: str,
    commit_sha: str,
    branch: str | None,
    change: GerritChange | None,
    issue_keys: list[str],
    review_link: ReviewLink | None,
) -> str:
    include_details = os.environ.get("POST_PUSH_COMMENT_INCLUDE_DETAILS", "0") == "1"

    lines = [message.strip()]
    if review_link:
        lines.extend(["", f"{review_link.label}: {review_link.url}"])

    if include_details:
        lines.extend(["", f"Commit: {commit_sha}"])
        if branch:
            lines.append(f"Branch: {branch}")
        if issue_keys:
            lines.append(f"Jira: {', '.join(issue_keys)}")
        if change and change.url:
            lines.append(f"Gerrit: {change.url}")

    return "\n".join(lines).strip()


def jira_comment_bodies(*, message: str, review_link: ReviewLink | None) -> list[str]:
    """Return separate Jira comments for the commit message and pushed URL."""
    bodies = [message.strip()]
    if review_link and review_link.url not in bodies:
        bodies.append(review_link.url)
    return bodies


def jira_comment_marker_name(issue_key: str, body: str) -> str:
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:20]
    return f"jira-{issue_key}-{digest}"


def post_gerrit_comment(
    *,
    remote: GerritRemote,
    change: GerritChange,
    body: str,
    repo_root: Path,
    dry_run: bool,
) -> bool:
    target = f"{change.number},{change.patchset}"
    remote_command = f"gerrit review --message {shlex.quote(body)} {shlex.quote(target)}"
    cmd = [*ssh_base(remote), remote_command]
    if dry_run:
        print("DRY RUN Gerrit command:")
        print(" ".join(shlex.quote(part) for part in cmd))
        return False
    run(cmd, cwd=repo_root, timeout=30)
    print(f"Posted Gerrit comment on change {target}")
    return True


def jira_auth_headers() -> dict[str, str] | None:
    username = os.environ.get("JIRA_EMAIL") or os.environ.get("JIRA_USERNAME")
    token = os.environ.get("JIRA_API_TOKEN")
    if username and token:
        raw = f"{username}:{token}".encode("utf-8")
        encoded = base64.b64encode(raw).decode("ascii")
        return {"Authorization": f"Basic {encoded}"}

    return None


def normalize_jira_site(site: str) -> str:
    return site.removeprefix("https://").removeprefix("http://").strip("/")


def jira_adf_body(text: str) -> dict:
    return {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
    }


def jira_request_json(
    *,
    method: str,
    url: str,
    body: dict | None = None,
    headers: dict[str, str] | None = None,
) -> dict | None:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Accept": "application/json",
            **({"Content-Type": "application/json"} if body is not None else {}),
            **(headers or {}),
        },
        method=method,
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        content = response.read().decode("utf-8")
        return json.loads(content) if content else None


def jira_cloud_id() -> str | None:
    cloud_id = os.environ.get("JIRA_CLOUD_ID")
    if cloud_id:
        return cloud_id

    site = os.environ.get("JIRA_SITE")
    if not site:
        return None

    tenant_url = f"https://{normalize_jira_site(site)}/_edge/tenant_info"
    data = jira_request_json(method="GET", url=tenant_url)
    if not data:
        return None
    return data.get("cloudId")


def jira_comment_url(issue_key: str) -> str | None:
    issue = urllib.parse.quote(issue_key, safe="")
    cloud_id = jira_cloud_id()
    if cloud_id:
        return f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{issue}/comment"

    base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    if base_url:
        api_version = os.environ.get("JIRA_API_VERSION", "3")
        return f"{base_url}/rest/api/{api_version}/issue/{issue}/comment"

    return None


def should_post_jira_comment() -> bool:
    return os.environ.get("PUSH_HOOK_POST_JIRA_COMMENT", "1") != "0"


def should_post_gerrit_comment() -> bool:
    return os.environ.get("PUSH_HOOK_POST_GERRIT_COMMENT", "0") == "1"


def jira_comment_text(body: object) -> str:
    """Extract plain text from a Jira comment body returned by REST API v2 or v3."""
    if isinstance(body, str):
        return body
    if isinstance(body, list):
        return "".join(jira_comment_text(item) for item in body)
    if not isinstance(body, dict):
        return ""
    if body.get("type") == "hardBreak":
        return "\n"
    if isinstance(body.get("text"), str):
        return body["text"]

    content = body.get("content")
    if not isinstance(content, list):
        return ""
    separator = "\n" if body.get("type") == "doc" else ""
    return separator.join(jira_comment_text(item) for item in content)


def jira_comment_exists(*, url: str, body: str, headers: dict[str, str]) -> bool:
    start_at = 0
    page_size = 100

    while True:
        query = urllib.parse.urlencode({"startAt": start_at, "maxResults": page_size})
        payload = jira_request_json(method="GET", url=f"{url}?{query}", headers=headers)
        if not isinstance(payload, dict):
            return False

        comments = payload.get("comments")
        if not isinstance(comments, list):
            return False

        for comment in comments:
            if isinstance(comment, dict) and jira_comment_text(comment.get("body")) == body:
                return True

        next_start = start_at + len(comments)
        total = payload.get("total")
        if not comments or not isinstance(total, int) or next_start >= total:
            return False
        start_at = next_start


def post_jira_comment(*, issue_key: str, body: str, dry_run: bool) -> bool:
    headers = jira_auth_headers()
    url = jira_comment_url(issue_key)
    if not url or not headers:
        print(
            "Skipping Jira comment; export Jira environment variables in the OS "
            "environment (JIRA_EMAIL/JIRA_API_TOKEN/JIRA_SITE or JIRA_CLOUD_ID)."
        )
        return False

    payload = jira_adf_body(body)

    if dry_run:
        print(f"DRY RUN Jira POST: {url}")
        print(json.dumps(payload, indent=2))
        return False

    try:
        if jira_comment_exists(url=url, body=body, headers=headers):
            print(f"Skipping duplicate Jira comment on {issue_key}")
            return True
        jira_request_json(method="POST", url=url, body=payload, headers=headers)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Jira comment failed: HTTP {exc.code}: {details}") from exc

    print(f"Posted Jira comment on {issue_key}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--remote-name", required=True)
    parser.add_argument("--remote-url", required=True)
    parser.add_argument("--local-ref", required=True)
    parser.add_argument("--local-sha", required=True)
    parser.add_argument("--remote-ref", required=True)
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=int(os.environ.get("PUSH_HOOK_GERRIT_WAIT_SECONDS", "120")),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(os.environ.get("PUSH_HOOK_GERRIT_POLL_SECONDS", "5")),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(git(["rev-parse", "--show-toplevel"], cwd=Path.cwd()))

    branch = parse_target_branch(args.remote_ref)
    if args.local_sha == ZERO_SHA:
        print("Skipping delete push.")
        return 0

    message = commit_message(args.local_sha, repo_root)
    if not message:
        print(f"Skipping empty commit message for {args.local_sha}")
        return 0

    issue_keys = parse_issue_keys(message)

    is_gerrit_push = args.remote_ref.startswith("refs/for/")
    remote: GerritRemote | None = None
    change: GerritChange | None = None

    if is_gerrit_push:
        remote = parse_remote_url(args.remote_url)
        change = wait_for_gerrit_change(
            remote=remote,
            commit_sha=args.local_sha,
            branch=branch,
            repo_root=repo_root,
            wait_seconds=args.wait_seconds,
            poll_seconds=args.poll_seconds,
        )
        if not change:
            print(
                f"No Gerrit change found for {args.local_sha} after {args.wait_seconds}s; "
                "skipping Gerrit comment."
            )

    review_link = resolve_review_link(
        change,
        remote_url=args.remote_url,
        branch=branch,
        repo_root=repo_root,
    )
    body = comment_body(
        message=message,
        commit_sha=args.local_sha,
        branch=branch,
        change=change,
        issue_keys=issue_keys,
        review_link=review_link,
    )
    jira_bodies = jira_comment_bodies(message=message, review_link=review_link)

    if is_gerrit_push and remote and change and should_post_gerrit_comment():
        with_marker(
            repo_root,
            f"gerrit-{change.number}-{change.patchset}-{args.local_sha}",
            lambda: post_gerrit_comment(
                remote=remote,
                change=change,
                body=body,
                repo_root=repo_root,
                dry_run=args.dry_run,
            ),
        )

    if issue_keys and should_post_jira_comment():
        for issue_key in issue_keys:
            for jira_body in jira_bodies:
                with_marker(
                    repo_root,
                    jira_comment_marker_name(issue_key, jira_body),
                    lambda issue_key=issue_key, jira_body=jira_body: post_jira_comment(
                        issue_key=issue_key,
                        body=jira_body,
                        dry_run=args.dry_run,
                    ),
                )

    if not issue_keys:
        print("No Jira issue key found in commit message.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
