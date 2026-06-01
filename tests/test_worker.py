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
