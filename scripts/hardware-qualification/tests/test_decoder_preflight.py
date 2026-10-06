import os
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("decoder_preflight", Path(__file__).parents[1] / "decoder_preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


def make_elf(path, machine=183, interpreter=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (interpreter + "\0").encode() if interpreter else b""
    header = struct.pack("<16sHHIQQQIHHHHHH", b"\x7fELF\x02\x01" + b"\0" * 10,
                         3, machine, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 3 if interpreter else 1, 0, 120, 0, 0, len(raw), len(raw), 1)
    path.write_bytes(header + segment + raw)
    path.chmod(0o755)


class DecoderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loader = self.root / "ld-linux-aarch64.so.1"
        make_elf(self.loader)
        make_elf(self.root / "bin/java", interpreter=str(self.loader))
        make_elf(self.root / "lib/server/libjvm.so")
        self.api = self.root / "libsdrplay_api.so"
        make_elf(self.api)
        (self.root / "release").write_text('JAVA_VERSION="23.0.1"\n')
        (self.root / "bin/sdr-trunk").write_text("unused launcher")
        (self.root / "bin/sdr-trunk").chmod(0o755)

    def run_audit(self, machine="aarch64"):
        return preflight.inspect(self.root, self.api, machine)

    def test_present_prerequisites_never_qualify_decoding(self):
        result = self.run_audit()
        self.assertEqual(result["prerequisite_status"], "prerequisites_present")
        self.assertEqual(result["deployment_qualification"], "not_evaluated")
        self.assertIn("transitive_native_dependencies", result["not_evaluated"])

    def test_missing_glibc_loader_blocks_musl_root_case(self):
        self.loader.unlink()
        result = self.run_audit()
        self.assertEqual(result["prerequisite_status"], "blocked")
        self.assertEqual(next(x["status"] for x in result["checks"] if x["name"] == "dynamic_loader"), "blocked")

    def test_cross_architecture_is_blocked(self):
        self.assertEqual(self.run_audit("x86_64")["prerequisite_status"], "blocked")

    def test_absent_api_is_blocked(self):
        self.api.unlink()
        self.assertEqual(self.run_audit()["prerequisite_status"], "blocked")

    def test_wrong_api_architecture_is_blocked(self):
        make_elf(self.api, machine=62)
        self.assertEqual(self.run_audit()["prerequisite_status"], "blocked")

    def test_wrong_java_and_nonexecutable_launcher_are_blocked(self):
        (self.root / "release").write_text('JAVA_VERSION="22"\n')
        (self.root / "bin/sdr-trunk").chmod(0o644)
        self.assertEqual(self.run_audit()["prerequisite_status"], "blocked")

    def test_malformed_and_truncated_elf_are_blocked(self):
        for data in [b"not ELF", (self.root / "bin/java").read_bytes()[:80]]:
            (self.root / "bin/java").write_bytes(data)
            self.assertEqual(self.run_audit()["prerequisite_status"], "blocked")

    def test_interpreter_traversal_is_rejected(self):
        make_elf(self.root / "bin/java", interpreter="/lib/../bad")
        self.assertEqual(self.run_audit()["prerequisite_status"], "blocked")

    def test_excessive_header_count_is_rejected(self):
        path = self.root / "bin/java"
        data = bytearray(path.read_bytes())
        struct.pack_into("<H", data, 56, 65535)
        path.write_bytes(data)
        with self.assertRaises(ValueError):
            preflight.elf(path)

    def test_unknown_host_architecture_blocks(self):
        self.assertEqual(self.run_audit("unknown")["prerequisite_status"], "blocked")

    def test_special_files_do_not_block_reads(self):
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        with self.assertRaises(ValueError):
            preflight.elf(fifo)
        with self.assertRaises(ValueError):
            preflight.regular_text(fifo)

    def test_oversized_or_duplicate_metadata_is_blocked(self):
        for text in ["x" * 65537, 'JAVA_VERSION="23.0.1"\n' * 2]:
            (self.root / "release").write_text(text)
            self.assertEqual(self.run_audit()["prerequisite_status"], "blocked")

    def test_cli_preserves_existing_evidence(self):
        output = self.root / "evidence.json"
        output.write_text("keep")
        with patch("sys.argv", ["preflight", "--installation", str(self.root), "--output", str(output)]):
            with self.assertRaises(FileExistsError):
                preflight.main()
        self.assertEqual(output.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
