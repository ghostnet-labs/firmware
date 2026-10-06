from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from inventory import command, read


class InventoryTests(unittest.TestCase):
    def test_missing_command_is_explicit(self):
        with patch("inventory.shutil.which", return_value=None):
            self.assertEqual(command(["not-installed"])["status"], "missing")

    def test_failure_keeps_error_output(self):
        result = command([sys.executable, "-c", "import sys; print('denied'); sys.exit(3)"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["returncode"], 3)
        self.assertIn("denied", result["output"])

    def test_timeout_is_not_success(self):
        result = command([sys.executable, "-c", "import time; time.sleep(2)"], timeout=0.05)
        self.assertEqual(result["status"], "timeout")

    def test_capture_is_bounded_and_reports_truncation(self):
        result = command([sys.executable, "-c", "print('x'*1000)"], limit=20)
        self.assertEqual(len(result["output"]), 20)
        self.assertTrue(result["truncated"])

    def test_device_tree_nul_and_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model"
            path.write_bytes(b"test board\x00")
            self.assertEqual(read(path)["value"], "test board")
            self.assertEqual(read(path.with_name("missing"))["status"], "unavailable")
