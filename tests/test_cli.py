from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import cli


class HookTemplateTests(TestCase):
    def test_render_hook_embeds_absolute_worker_runner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jira hook test ") as directory:
            root = Path(directory)
            executable = root / "python runner"
            worker_path = root / "worker module.py"
            executable.touch()
            worker_path.touch()

            with (
                patch("cli.sys.executable", str(executable)),
                patch("cli.worker.__file__", str(worker_path)),
            ):
                hook = cli.render_hook()

        self.assertIn(f"runner=('{executable}' '{worker_path}')", hook)
        self.assertNotIn("__JIRA_PUSH_HOOK_RUNNER__", hook)
        self.assertNotIn("python3 -m worker", hook)

    def test_rendered_hook_executes_embedded_runner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="jira-hook-") as directory:
            repo = Path(directory)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", "TEST-1"],
                check=True,
            )
            sha = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.strip()

            runner = repo / "runner script"
            output = repo / "runner-args"
            runner.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > \"$RUNNER_OUTPUT\"\n",
                encoding="utf-8",
            )
            runner.chmod(0o755)

            with (
                patch("cli.sys.executable", str(runner)),
                patch("cli.worker.__file__", str(repo / "worker module.py")),
            ):
                hook = cli.render_hook()

            stdin = (
                f"refs/heads/main {sha} refs/heads/main "
                "0000000000000000000000000000000000000000\n"
            )
            subprocess.run(
                ["bash", "-c", hook],
                cwd=repo,
                env={"PATH": "/usr/bin:/bin", "RUNNER_OUTPUT": str(output)},
                input=stdin,
                text=True,
                check=True,
            )

            for _ in range(100):
                if output.exists():
                    break
                time.sleep(0.01)
            args = output.read_text(encoding="utf-8").splitlines()

        self.assertEqual(args[0], str(repo / "worker module.py"))
        self.assertEqual(args[1:3], ["--remote-name", ""])
