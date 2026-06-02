from __future__ import annotations

import os
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import worker


class ReviewLinkTests(TestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def resolve(
        self,
        remote_url: str,
        branch: str | None = "feature/reviews",
    ) -> worker.ReviewLink | None:
        with patch("worker.shutil.which", return_value=None):
            return worker.resolve_review_link(
                None,
                remote_url=remote_url,
                branch=branch,
                repo_root=Path("."),
            )

    def test_gerrit_change_url_is_used(self) -> None:
        change = worker.GerritChange(number="123", patchset="1", url="https://review/123")

        link = worker.resolve_review_link(
            change,
            remote_url="ssh://review/project",
            branch="main",
            repo_root=Path("."),
        )

        self.assertEqual(link, worker.ReviewLink("Gerrit review", "https://review/123"))

    def test_review_url_override_wins(self) -> None:
        os.environ["PUSH_HOOK_REVIEW_URL"] = "https://reviews.example/change/1"
        os.environ["PUSH_HOOK_PR_URL"] = "https://legacy.example/pull/1"

        link = self.resolve("git@github.com:org/repo.git")

        self.assertEqual(link, worker.ReviewLink("Review", "https://reviews.example/change/1"))

    def test_github_scp_remote_gets_create_pr_url(self) -> None:
        link = self.resolve("git@github.com:org/repo.git")

        self.assertEqual(
            link,
            worker.ReviewLink(
                "Create GitHub PR",
                "https://github.com/org/repo/compare/feature/reviews?expand=1",
            ),
        )

    def test_gitlab_https_remote_gets_create_mr_url(self) -> None:
        link = self.resolve("https://gitlab.com/org/repo.git")

        self.assertEqual(
            link,
            worker.ReviewLink(
                "Create GitLab MR",
                "https://gitlab.com/org/repo/-/merge_requests/new?"
                "merge_request%5Bsource_branch%5D=feature%2Freviews",
            ),
        )

    def test_bitbucket_remote_gets_create_pr_url(self) -> None:
        link = self.resolve("git@bitbucket.org:org/repo.git")

        self.assertEqual(
            link,
            worker.ReviewLink(
                "Create Bitbucket PR",
                "https://bitbucket.org/org/repo/pull-requests/new?source=feature%2Freviews",
            ),
        )

    def test_azure_ssh_remote_gets_create_pr_url(self) -> None:
        link = self.resolve("git@ssh.dev.azure.com:v3/org/project/repo")

        self.assertEqual(
            link,
            worker.ReviewLink(
                "Create Azure DevOps PR",
                "https://dev.azure.com/org/project/_git/repo/pullrequestcreate?"
                "sourceRef=refs%2Fheads%2Ffeature%2Freviews",
            ),
        )

    def test_self_hosted_provider_can_be_configured(self) -> None:
        os.environ["PUSH_HOOK_REVIEW_PROVIDER"] = "gitlab"

        link = self.resolve("git@git.example.com:group/repo.git")

        self.assertEqual(
            link,
            worker.ReviewLink(
                "Create GitLab MR",
                "https://git.example.com/group/repo/-/merge_requests/new?"
                "merge_request%5Bsource_branch%5D=feature%2Freviews",
            ),
        )

    def test_unknown_provider_gets_repository_url(self) -> None:
        link = self.resolve("git@git.example.com:org/repo.git")

        self.assertEqual(
            link,
            worker.ReviewLink("Repository", "https://git.example.com/org/repo"),
        )

    def test_non_branch_push_gets_repository_url(self) -> None:
        link = self.resolve("git@github.com:org/repo.git", branch=None)

        self.assertEqual(
            link,
            worker.ReviewLink("Repository", "https://github.com/org/repo"),
        )


class CommentBodyTests(TestCase):
    def test_review_link_is_added_to_comment(self) -> None:
        body = worker.comment_body(
            message="ABC-1: change",
            commit_sha="a" * 40,
            branch="feature/reviews",
            change=None,
            issue_keys=["ABC-1"],
            review_link=worker.ReviewLink("Create GitLab MR", "https://gitlab.example/new"),
        )

        self.assertEqual(
            body,
            "ABC-1: change\n\nCreate GitLab MR: https://gitlab.example/new",
        )

    def test_jira_comments_split_commit_message_and_url(self) -> None:
        bodies = worker.jira_comment_bodies(
            message="ABC-1: change",
            review_link=worker.ReviewLink("Create GitLab MR", "https://gitlab.example/new"),
        )

        self.assertEqual(bodies, ["ABC-1: change", "https://gitlab.example/new"])


class JiraCommentTests(TestCase):
    def setUp(self) -> None:
        environment = patch.dict(
            os.environ,
            {
                "JIRA_EMAIL": "test@example.com",
                "JIRA_API_TOKEN": "test-token",
                "JIRA_CLOUD_ID": "test-cloud",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def test_existing_comment_is_not_posted_again(self) -> None:
        existing = worker.jira_adf_body("already posted")["body"]

        with patch(
            "worker.jira_request_json",
            return_value={"comments": [{"body": existing}], "total": 1},
        ) as request:
            posted = worker.post_jira_comment(
                issue_key="ABC-1",
                body="already posted",
                dry_run=False,
            )

        self.assertTrue(posted)
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["method"], "GET")

    def test_duplicate_lookup_checks_later_pages(self) -> None:
        existing = worker.jira_adf_body("already posted")["body"]
        first_page = {
            "comments": [{"body": worker.jira_adf_body("different")["body"]}],
            "total": 2,
        }
        second_page = {"comments": [{"body": existing}], "total": 2}

        with patch("worker.jira_request_json", side_effect=[first_page, second_page]) as request:
            posted = worker.post_jira_comment(
                issue_key="ABC-1",
                body="already posted",
                dry_run=False,
            )

        self.assertTrue(posted)
        self.assertEqual(request.call_count, 2)
        self.assertIn("startAt=1", request.call_args.kwargs["url"])

    def test_new_comment_is_checked_then_posted(self) -> None:
        with patch(
            "worker.jira_request_json",
            side_effect=[{"comments": [], "total": 0}, None],
        ) as request:
            posted = worker.post_jira_comment(
                issue_key="ABC-1",
                body="new comment",
                dry_run=False,
            )

        self.assertTrue(posted)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["method"], "GET")
        self.assertEqual(request.call_args_list[1].kwargs["method"], "POST")
