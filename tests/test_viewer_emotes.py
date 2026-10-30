"""Expose the offline JavaScript regression suite to unittest discovery.

Uses an existing Node.js installation; no npm packages are required.
"""
import shutil
import subprocess
import unittest
from pathlib import Path


class ViewerEmoteTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for viewer JavaScript tests")
    def test_template_emote_javascript(self):
        result = subprocess.run(
            [shutil.which("node"), "--test", str(Path(__file__).with_suffix(".js"))],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
