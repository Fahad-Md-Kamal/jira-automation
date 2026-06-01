from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

import worker
from version import __version__


HOOK_MARKER = "jira-push-hook"


HOOK_TEMPLATE = """#!/usr/bin/env bash
set -euo pipefail

# Installed by jira-push-hook.
repo_root="$(git rev-parse --show-toplevel)"
log_dir="$repo_root/.git/push-comment-hook/logs"
mkdir -p "$log_dir"

remote_name="${1:-}"
remote_url="${2:-}"

if command -v jira-push-hook >/dev/null 2>&1; then
  runner=(jira-push-hook worker)
elif command -v python3 >/dev/null 2>&1; then
  runner=(python3 -m worker)
else
  echo "jira-push-hook is not installed and no Python fallback was found." >&2
  exit 0
fi

while read -r local_ref local_sha remote_ref remote_sha; do
  if [[ "$local_sha" == "0000000000000000000000000000000000000000" ]]; then
    continue
  fi

  log_file="$log_dir/$(date +%Y%m%d%H%M%S)-${local_sha:0:12}.log"
  nohup "${runner[@]}" \\
    --remote-name "$remote_name" \\
    --remote-url "$remote_url" \\
    --local-ref "$local_ref" \\
    --local-sha "$local_sha" \\
    --remote-ref "$remote_ref" \\
    > "$log_file" 2>&1 &
  echo "Scheduled push-comment worker; log: $log_file" >&2
done

exit 0
"""


def run_git(args: list[str], *, cwd: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def install_hook(repo: Path) -> int:
    repo_root = Path(run_git(["rev-parse", "--show-toplevel"], cwd=repo)).resolve()
    hook_path = repo_root / ".git" / "hooks" / "pre-push"

    if hook_path.exists() and HOOK_MARKER not in hook_path.read_text(errors="ignore"):
        backup_path = hook_path.with_name(
            f"{hook_path.name}.backup.{time.strftime('%Y%m%d%H%M%S')}"
        )
        shutil.copy2(hook_path, backup_path)
        print(f"Existing pre-push hook backed up to {backup_path}")

    hook_path.write_text(HOOK_TEMPLATE, encoding="utf-8")
    hook_path.chmod(0o755)
    print(f"Installed push-comment hook at {hook_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
      prog="jira-push-hook",
      description="Install and run a Git pre-push hook that comments commit messages on Jira.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    subparsers = parser.add_subparsers(dest="command")

    install = subparsers.add_parser("install", help="install .git/hooks/pre-push in this repo")
    install.add_argument(
        "--repo",
        default=".",
        help="repo path where the hook should be installed; default: current directory",
    )

    worker_parser = subparsers.add_parser("worker", help="run the background post-push worker")
    worker_parser.add_argument("worker_args", nargs=argparse.REMAINDER)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "worker":
        return worker.main(argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "install":
        return install_hook(Path(args.repo).expanduser())

    parser.print_help()
    return 0
