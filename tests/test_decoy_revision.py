from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deployment_engine.core.revision import RevisionError, resolve_ruse_revision


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.email", "test@example.test"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.root), "config", "user.name", "Test"],
            check=True,
        )
        (self.root / "decoys").mkdir()
        (self.root / "decoys" / "runtime.py").write_text("VALUE = 1\n")
        subprocess.run(["git", "-C", str(self.root), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.root), "commit", "-qm", "initial"], check=True
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def test_clean_tree_resolves_full_head(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUSE_GIT_REF", None)
            revision = resolve_ruse_revision(self.root)
        self.assertRegex(revision, r"^[0-9a-f]{40}$")

    def test_dirty_runtime_tree_is_rejected(self):
        (self.root / "decoys" / "runtime.py").write_text("VALUE = 2\n")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RUSE_GIT_REF", None)
            with self.assertRaisesRegex(RevisionError, "uncommitted changes"):
                resolve_ruse_revision(self.root)

    def test_explicit_published_revision_must_be_full_sha(self):
        with patch.dict(os.environ, {"RUSE_GIT_REF": "main"}):
            with self.assertRaisesRegex(RevisionError, "40-character"):
                resolve_ruse_revision(self.root)

        explicit = "a" * 40
        with patch.dict(os.environ, {"RUSE_GIT_REF": explicit}):
            self.assertEqual(resolve_ruse_revision(self.root), explicit)


if __name__ == "__main__":
    unittest.main()
