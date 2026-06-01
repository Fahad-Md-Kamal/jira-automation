# Jira Push Hook

This package provides a Jira automation CLI and an optional Git `pre-push` hook.

You can use it directly for Jira issue comments, worklogs, issue lookups,
ticket search, and custom field updates. If you enable hook mode, it also runs
a background worker during `git push` to post commit-message comments to Jira.

The hook worker is provider-agnostic for normal pushes and can optionally post
a Gerrit review comment when pushing to `refs/for/*`. Jira comments can include
review links for Gerrit, GitHub, GitLab, Bitbucket Cloud, and Azure DevOps.

When a commit message contains multiple Jira keys, the worker deduplicates and
comments on each key (for example, `ABC-1, ABC-2`).

## Required Environment Variables

Set these in your OS environment before using Jira commands or hook mode:

```bash
export JIRA_EMAIL="your-email"
export JIRA_API_TOKEN="your-token"
export JIRA_SITE="your-site.atlassian.net"
```

Example commit subject:

```text
P1732-842,P1732-850 : feat(map): add single-file render facets API with string/numeric strategies
```

Multiple keys are comma-separated with no space. Single or double colon separators both work.

## Install From This Repo

From the repository root:

```bash
python3 -m pip install --user .
jira-push-hook scopes
```

or install in an isolated environment:

```bash
pipx install .
jira-push-hook scopes
```

## CLI Commands

Available Jira automation commands:

```text
jira-push-hook cloud-id
jira-push-hook check-auth
jira-push-hook scopes
jira-push-hook issue <ISSUE-KEY>
jira-push-hook list-issues <PROJECT> [--max N] [--jql ...]
jira-push-hook list-assignee --me|--account-id <ID>|--unassigned [--project PROJECT] [--max N]
jira-push-hook find-field [FIELD NAME]
jira-push-hook comment <ISSUE-KEY> [TEXT]
jira-push-hook worklog <ISSUE-KEY> <DURATION> [TEXT] [--started ...]
jira-push-hook ai-contribution <ISSUE-KEY> <VALUE>
jira-push-hook percent-done <ISSUE-KEY> <VALUE>
jira-push-hook transition <ISSUE-KEY> --to "In Progress"
jira-push-hook transition <ISSUE-KEY> --list
```

If you run `jira-push-hook` with no arguments, it starts the interactive Jira
automation menu.

## Git Hook Mode

Install the optional `pre-push` hook in a repository:

```bash
jira-push-hook install
```

That hook starts the packaged worker during pushes and writes logs under:

```text
.git/push-comment-hook/logs/
```

### Review Links

For Gerrit pushes to `refs/for/*`, the worker waits for the created Gerrit
change and uses its review URL.

For ordinary branch pushes, the worker adds a provider-specific create-review
link to the Jira comment. When `gh` is installed and the pushed GitHub branch
already has an open pull request, its URL is used instead.

GitHub, GitLab, Bitbucket Cloud, and Azure DevOps are detected from standard
remote URLs. For self-hosted or unusual remotes, configure the provider and
repository web URL explicitly:

```bash
export PUSH_HOOK_REVIEW_PROVIDER="gitlab"
export PUSH_HOOK_REPOSITORY_URL="https://git.example.com/group/repo"
```

Every push with a parseable remote includes a URL in the Jira comment. When no
provider-specific review link can be built, the repository web URL is used.
For local or unusual remotes that cannot be converted to a web URL, set
`PUSH_HOOK_REPOSITORY_URL`.

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
jira-push-hook scopes
```

## Jira Configuration

This package reads Jira settings from OS environment variables only.
Export them in your current shell, terminal profile, or CI environment.

Set required variables in your current shell session:

```bash
export JIRA_EMAIL="your-email"
export JIRA_API_TOKEN="your-token"
export JIRA_SITE="your-site.atlassian.net"
```

To persist them for your user, add the same exports to your shell profile
(`~/.zshrc` or `~/.bashrc`) and open a new shell.

Optional values:

```bash
export JIRA_CLOUD_ID="your-cloud-id"
export JIRA_BASE_URL="https://your-site.atlassian.net"
export JIRA_API_VERSION="3"
```

### Create A Jira API Token

Create a scoped Atlassian API token and use it as `JIRA_API_TOKEN`.

1. Open Atlassian account security and create an API token with scopes.
2. Select your Jira site.
3. Copy the token value and export it as `JIRA_API_TOKEN` in your OS environment.
4. Keep `JIRA_EMAIL` set to the Atlassian account email that owns the token.

If you want the token scopes to match the Jira helper this package was derived
from, grant these Jira scopes:

```text
Read
read:audit-log:jira
read:avatar:jira
read:comment:jira
read:comment.property:jira
read:field:jira
read:field-configuration:jira
read:group:jira
read:issue:jira
read:issue.changelog:jira
read:issue-details:jira
read:issue-meta:jira
read:issue-security-level:jira
read:issue-worklog:jira
read:issue-worklog.property:jira
read:issue.vote:jira
read:jira-user
read:project:jira
read:project-role:jira
read:status:jira
read:user:jira

Write
write:comment:jira
write:field:jira
write:issue:jira
write:issue-worklog:jira
write:issue-worklog.property:jira
```

For the current hook package, comment posting mainly depends on Jira comment
access and issue access.

`JIRA_CLOUD_ID` can be set directly to skip cloud ID discovery.

## Hook Options

```bash
export PUSH_HOOK_POST_JIRA_COMMENT=1            # Enable Jira comments (default: 1).
export PUSH_HOOK_POST_GERRIT_COMMENT=0          # Enable Gerrit review comments (default: 0).
export PUSH_HOOK_REQUIRE_TICKET_KEY=1           # Block push when commit message has no Jira key.
export PUSH_HOOK_JIRA_PROJECTS="P1732,MYAPP"   # Comma-separated project prefixes to match.
                                                #   When set, only keys like P1732-* or MYAPP-*
                                                #   are recognised.  Avoids false positives such
                                                #   as UTF-8 or HTTP-404.
                                                #   When absent, any WORD-NUMBER token is matched.
export PUSH_HOOK_REVIEW_URL="https://review.example/change/123" # Optional review URL override.
export PUSH_HOOK_REVIEW_PROVIDER="gitlab"        # Optional: github, gitlab, bitbucket, azure-devops.
export PUSH_HOOK_REPOSITORY_URL="https://git.example.com/group/repo" # Optional repository web URL.
export PUSH_HOOK_PR_URL="https://github.com/org/repo/pull/123"  # Legacy alias for PUSH_HOOK_REVIEW_URL.
export POST_PUSH_COMMENT_INCLUDE_DETAILS=1       # Include commit/branch/change details.
export PUSH_HOOK_GERRIT_WAIT_SECONDS=120         # Max wait for Gerrit change visibility.
export PUSH_HOOK_GERRIT_POLL_SECONDS=5           # Poll interval for Gerrit lookup.
```

These settings affect hook mode and the background worker only.
