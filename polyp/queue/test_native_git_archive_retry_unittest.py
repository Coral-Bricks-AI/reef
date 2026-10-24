"""Local Git-only regression tests for the real cbq archive publication helper.

No database, model, agent worker, GPU or remote network endpoint is started.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

from polyp.queue.cbq import git_commit_push


class ArchiveRetryTests(unittest.TestCase):
    def git(self, root, *args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()

    def fixture(self, folder):
        bare = folder / "bare.git"
        work = folder / "work"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
        self.git(work, "config", "user.email", "test@example.com")
        self.git(work, "config", "user.name", "Test")
        self.git(work, "remote", "add", "origin", str(bare))
        file = work / "archive.md"
        file.write_text("first\n")
        self.git(work, "add", "archive.md")
        self.git(work, "commit", "-qm", "first")
        self.git(work, "push", "-q", "-u", "origin", "main")
        return bare, work, file

    def remote_main(self, bare):
        return self.git(bare, "rev-parse", "refs/heads/main")

    def test_replay_after_unpushed_commit_must_publish_pending_head(self):
        with tempfile.TemporaryDirectory() as temp:
            bare, work, file = self.fixture(Path(temp))
            file.write_text("second\n")
            self.git(work, "add", "archive.md")
            self.git(work, "commit", "-qm", "archived but push interrupted")
            self.assertNotEqual(self.remote_main(bare), self.git(work, "rev-parse", "HEAD"))
            self.assertTrue(git_commit_push(work, [file], "replay archive"))
            self.assertEqual(self.remote_main(bare), self.git(work, "rev-parse", "HEAD"))

    def test_new_change_still_commits_and_publishes(self):
        with tempfile.TemporaryDirectory() as temp:
            bare, work, file = self.fixture(Path(temp))
            file.write_text("second\n")
            self.assertTrue(git_commit_push(work, [file], "second archive"))
            self.assertEqual(self.remote_main(bare), self.git(work, "rev-parse", "HEAD"))

    def test_already_synced_archive_is_idempotently_successful(self):
        with tempfile.TemporaryDirectory() as temp:
            bare, work, file = self.fixture(Path(temp))
            self.assertTrue(git_commit_push(work, [file], "nothing new"))
            self.assertEqual(self.remote_main(bare), self.git(work, "rev-parse", "HEAD"))


if __name__ == "__main__":
    unittest.main()
