# Jira Push Hook

This package installs a local Git `pre-push` hook that runs a background worker
to post commit-message comments to Jira.

The worker is provider-agnostic for normal pushes and can optionally post a
Gerrit review comment when pushing to `refs/for/*`.

Example commit subject:

```text
P1732-82 :: feat(map_app): add unified property-based detected/raw columns across render APIs
```

## Install From This Repo

From the repository root:

```bash
python3 -m pip install --user .
jira-push-hook install
```

or install in an isolated environment:

```bash
pipx install .
jira-push-hook install
```

## Build A Wheel To Share

```bash
python3 -m pip install --upgrade build
python3 -m build
```

The wheel is written to:

```text
dist/
```

Teammates can install that wheel with:

```bash
python3 -m pip install --user jira_push_hook-*.whl
jira-push-hook install
```

## Jira Configuration

By default, the worker loads environment variables from these files if present:

```text
.env
.jira.env
backend/jira-script/.env
backend/.env
```

Supported values:

```bash
export JIRA_EMAIL="your-email"
export JIRA_API_TOKEN="your-token"
export JIRA_SITE="your-site.atlassian.net"
```

You can also set `JIRA_CLOUD_ID` directly. To use another env file:

```bash
export JIRA_ENV_FILE="/path/to/.env"
```

## Options

```bash
export PUSH_HOOK_POST_JIRA_COMMENT=1           # Enable Jira comments (default: 1).
export PUSH_HOOK_POST_GERRIT_COMMENT=0         # Enable Gerrit review comments (default: 0).
export POST_PUSH_COMMENT_INCLUDE_DETAILS=1      # Include commit/branch/change details.
export PUSH_HOOK_GERRIT_WAIT_SECONDS=120        # Max wait for Gerrit change visibility.
export PUSH_HOOK_GERRIT_POLL_SECONDS=5          # Poll interval for Gerrit lookup.
```

Backward-compatible env vars still work:

```bash
JIRA_POST_COMMIT_COMMENT
GERRIT_POST_COMMIT_COMMENT
GERRIT_COMMENT_WAIT_SECONDS
GERRIT_COMMENT_POLL_SECONDS
```

Worker logs are written under:

```text
.git/push-comment-hook/logs/
```
