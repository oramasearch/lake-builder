# SPDX-License-Identifier: MIT

"""The version in the code has release notes, and --version reports it."""

import os
import subprocess
import sys
import unittest
from pathlib import Path

from lake_builder import __version__

ROOT = Path(__file__).resolve().parents[1]


class ReleaseVersionTests(unittest.TestCase):
    def test_release_version_is_documented(self):
        notes = ROOT / "docs" / "releases" / f"v{__version__}.md"
        self.assertTrue(notes.is_file(), f"missing {notes.relative_to(ROOT)}")
        index = (ROOT / "docs" / "releases" / "README.md").read_text()
        self.assertIn(f"(v{__version__}.md)", index)

    def test_version_flag_reports_the_code_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "lake_builder", "--version"],
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"lake-builder {__version__}\n")
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
