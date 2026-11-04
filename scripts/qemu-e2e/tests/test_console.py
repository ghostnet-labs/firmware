import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import console  # noqa: E402


class ParseOutputTest(unittest.TestCase):
    def test_none_until_end_marker(self):
        self.assertIsNone(console.parse_output("__E2E_Bab1__\r\nhello\r\n", "ab1"))

    def test_cuts_output_between_markers(self):
        buf = "noise\r\n__E2E_Bab1__\r\nline 1\r\nline 2\r\n__E2E_Eab1_0__\r\nroot@x:~# "
        self.assertEqual(console.parse_output(buf, "ab1"), (0, "line 1\nline 2"))

    def test_exit_status(self):
        buf = "__E2E_Bt__\n__E2E_Et_127__\n"
        self.assertEqual(console.parse_output(buf, "t"), (127, ""))

    def test_echoed_command_line_is_not_a_marker(self):
        token = "f00"
        echoed = console.wrap_command("false", token).replace("\n", "\r\n")
        self.assertIsNone(console.parse_output(echoed, token))
        self.assertEqual(console.parse_output(echoed + "__E2E_Bf00__\r\n__E2E_Ef00_1__\r\n", token),
                         (1, ""))

    def test_other_tokens_ignored(self):
        buf = "__E2E_Ba__\nx\n__E2E_Ea_0__\n"
        self.assertIsNone(console.parse_output(buf, "b"))


class FakeGuest:
    """A unix-socket 'serial console' that runs each line through sh."""

    def __init__(self, path):
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.srv.accept()
        conn.sendall(b"[    1.0] boot noise\r\nPlease press Enter to activate this console.\r\n")
        buf = b""
        while True:
            data = conn.recv(4096)
            if not data:
                return
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    out = subprocess.run(["sh", "-c", line.decode()], capture_output=True).stdout
                    conn.sendall(out.replace(b"\n", b"\r\n"))
                conn.sendall(b"root@OpenWrt:~# ")

    def close(self):
        self.srv.close()


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        path = os.path.join(self.dir, "con.sock")
        self.guest = FakeGuest(path)
        self.con = console.Console(path, os.path.join(self.dir, "console.log"))

    def tearDown(self):
        self.con.close()
        self.guest.close()

    def test_login_and_run(self):
        # stty/dmesg are absent or harmless on the test host; run them via sh.
        self.con.expect(r"Please press Enter", 10)
        self.con.send("\n")
        self.con.expect(r"root@[^:]*:[^#]*# ", 10)
        self.assertEqual(self.con.run("echo hi; echo there"), (0, "hi\nthere"))
        status, _ = self.con.run("(exit 3)", check=False)
        self.assertEqual(status, 3)
        with self.assertRaises(console.ConsoleError):
            self.con.run("false")

    def test_expect_times_out(self):
        self.con.expect(r"Please press Enter", 10)
        with self.assertRaises(console.ConsoleError):
            self.con.expect(r"never printed", 1)

    def test_log_file_has_everything(self):
        self.con.expect(r"Please press Enter", 10)
        self.con.send("\n")
        self.con.run("echo logged-line")
        with open(os.path.join(self.dir, "console.log")) as f:
            self.assertIn("logged-line", f.read())


if __name__ == "__main__":
    unittest.main()
