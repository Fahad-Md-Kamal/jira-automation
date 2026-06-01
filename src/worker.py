#!/usr/bin/env python3
"""Worker for posting commit-message comments after Git pushes.

This script is intended to be launched by .git/hooks/pre-push. Git does not
have a client-side post-push hook, so the pre-push hook starts this worker in
the background. For Gerrit refs/for pushes, the worker can optionally wait for
the change to appear and post a Gerrit comment.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


ZERO_SHA = "0" * 40
ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
DEFAULT_JIRA_ENV_FILES = (
    Path(".env"),
    Path(".jira.env"),
    Path("backend/jira-script/.env"),
    Path("backend/.env"),
)


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
        action()
        marker.write_text(str(int(time.time())) + "\n", encoding="utf-8")
        return True
    finally:
        lock.unlink(missing_ok=True)


def commit_message(commit_sha: str, repo_root: Path) -> str:
    return git(["log", "-1", "--format=%B", commit_sha], cwd=repo_root).strip()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", key):
            continue
        if key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]

        os.environ[key] = value


def load_jira_env(repo_root: Path) -> None:
    env_file = os.environ.get("JIRA_ENV_FILE")
    if env_file:
        load_env_file(Path(env_file).expanduser())
        return

    for relative_path in DEFAULT_JIRA_ENV_FILES:
        load_env_file(repo_root / relative_path)


def comment_body(
    *,
    message: str,
    commit_sha: str,
    branch: str | None,
    change: GerritChange | None,
    issue_key: str | None,
) -> str:
    if os.environ.get("POST_PUSH_COMMENT_INCLUDE_DETAILS", "0") != "1":
        return message

    lines = [
        message,
        "",
        f"Commit: {commit_sha}",
    ]
    if branch:
        lines.append(f"Branch: {branch}")
    if issue_key:
        lines.append(f"Jira: {issue_key}")
    if change and change.url:
        lines.append(f"Gerrit: {change.url}")
    return "\n".join(lines).strip()


def post_gerrit_comment(
    *,
    remote: GerritRemote,
    change: GerritChange,
    body: str,
    repo_root: Path,
    dry_run: bool,
) -> None:
    target = f"{change.number},{change.patchset}"
    remote_command = f"gerrit review --message {shlex.quote(body)} {shlex.quote(target)}"
    cmd = [*ssh_base(remote), remote_command]
    if dry_run:
        print("DRY RUN Gerrit command:")
        print(" ".join(shlex.quote(part) for part in cmd))
        return
    run(cmd, cwd=repo_root, timeout=30)
    print(f"Posted Gerrit comment on change {target}")


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
    value = os.environ.get("PUSH_HOOK_POST_JIRA_COMMENT")
    if value is None:
        value = os.environ.get("JIRA_POST_COMMIT_COMMENT", "1")
    return value != "0"


def should_post_gerrit_comment() -> bool:
    value = os.environ.get("PUSH_HOOK_POST_GERRIT_COMMENT")
    if value is None:
        value = os.environ.get("GERRIT_POST_COMMIT_COMMENT", "0")
    return value == "1"


def post_jira_comment(*, issue_key: str, body: str, dry_run: bool) -> None:
    headers = jira_auth_headers()
    url = jira_comment_url(issue_key)
    if not url or not headers:
        print(
            "Skipping Jira comment; set backend/jira-script/.env, JIRA_ENV_FILE, "
            "or Jira env vars."
        )
        return

    payload = jira_adf_body(body)

    if dry_run:
        print(f"DRY RUN Jira POST: {url}")
        print(json.dumps(payload, indent=2))
        return

    try:
        jira_request_json(method="POST", url=url, body=payload, headers=headers)
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Jira comment failed: HTTP {exc.code}: {details}") from exc

    print(f"Posted Jira comment on {issue_key}")


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
        default=int(
            os.environ.get(
                "PUSH_HOOK_GERRIT_WAIT_SECONDS",
                os.environ.get("GERRIT_COMMENT_WAIT_SECONDS", "120"),
            )
        ),
    )
    parser.add_argument(
        "--poll-seconds",
        type=int,
        default=int(
            os.environ.get(
                "PUSH_HOOK_GERRIT_POLL_SECONDS",
                os.environ.get("GERRIT_COMMENT_POLL_SECONDS", "5"),
            )
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    repo_root = Path(git(["rev-parse", "--show-toplevel"], cwd=Path.cwd()))
    load_jira_env(repo_root)

    branch = parse_target_branch(args.remote_ref)
    if args.local_sha == ZERO_SHA:
        print("Skipping delete push.")
        return 0

    message = commit_message(args.local_sha, repo_root)
    if not message:
        print(f"Skipping empty commit message for {args.local_sha}")
        return 0

    issue_match = ISSUE_KEY_RE.search(message)
    issue_key = issue_match.group(1) if issue_match else None

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

    body = comment_body(
        message=message,
        commit_sha=args.local_sha,
        branch=branch,
        change=change,
        issue_key=issue_key,
    )

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

    if issue_key and should_post_jira_comment():
        with_marker(
            repo_root,
            f"jira-{issue_key}-{args.local_sha}",
            lambda: post_jira_comment(issue_key=issue_key, body=body, dry_run=args.dry_run),
        )

    if not issue_key:
        print("No Jira issue key found in commit message.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
