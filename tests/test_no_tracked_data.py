import os
import shutil
import subprocess
import unittest

class NoTrackedDataTestSuite(unittest.TestCase):
    def test_no_data_or_backups_tracked_by_git(self):
        """Verifies that no databases, secrets, or backups in data/ or backups/ are tracked in git."""
        if not shutil.which("git"):
            self.skipTest("git command not available in this environment")

        repo_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        # Check if inside a git repository
        if not os.path.exists(os.path.join(repo_dir, ".git")):
            self.skipTest("Not inside a git repository")

        res = subprocess.run(
            ["git", "ls-files", "data", "backups"],
            cwd=repo_dir,
            capture_output=True,
            text=True
        )
        if res.returncode != 0:
            self.skipTest(f"git ls-files failed: {res.stderr}")

        output = res.stdout.strip()
        self.assertEqual(
            output,
            "",
            f"Sensitive files under data/ or backups/ are tracked in git:\n{output}"
        )

if __name__ == "__main__":
    unittest.main()
