"""Serial console driver for an OpenWrt guest under QEMU.

QEMU exposes the guest's ttyS0 on a unix socket. A background thread reads
everything the guest prints into a buffer (and a log file); `run` sends one
shell command wrapped in unique markers and returns its exit status and
output. Only the Python standard library is used.
"""

import re
import socket
import threading
import time


class ConsoleError(RuntimeError):
    """The guest did not print what we waited for in time."""


def wrap_command(cmd, token):
    """Return the line to type so the output can be cut out reliably.

    The markers are built with printf from two halves, so the echoed input
    line (if echo is still on) never contains the joined marker text.
    """
    return (
        "printf '%s%s\\n' __E2E_B {t}__; {cmd}; "
        "printf '%s%s_%s__\\n' __E2E_E {t} $?\n"
    ).format(t=token, cmd=cmd)


def parse_output(buf, token):
    """Find a wrapped command's result in `buf`.

    Returns (exit_status, output) once the end marker is present, or None.
    """
    begin = "__E2E_B{}__".format(token)
    end_re = re.compile(r"__E2E_E{}_(\d+)__".format(re.escape(token)))
    m = end_re.search(buf)
    if not m:
        return None
    start = buf.rfind(begin, 0, m.start())
    if start < 0:
        return None
    body = buf[start + len(begin):m.start()]
    body = body.replace("\r", "")
    if body.startswith("\n"):
        body = body[1:]
    return int(m.group(1)), body.rstrip("\n")


class Console:
    """Line-oriented access to the guest serial console."""

    def __init__(self, path, log_path, connect_timeout=30):
        self._buf = ""
        self._mark = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False
        self._log = open(log_path, "a", encoding="utf-8", errors="replace")
        deadline = time.monotonic() + connect_timeout
        while True:
            try:
                self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self._sock.connect(path)
                break
            except OSError:
                self._sock.close()
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        self._seq = 0
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self):
        while True:
            try:
                data = self._sock.recv(4096)
            except OSError:
                data = b""
            if not data:
                with self._cond:
                    self._closed = True
                    self._cond.notify_all()
                return
            text = data.decode("utf-8", errors="replace")
            self._log.write(text)
            self._log.flush()
            with self._cond:
                self._buf += text
                # Bound memory: keep the last 1 MiB; the log file has it all.
                if len(self._buf) > (1 << 20):
                    drop = len(self._buf) - (1 << 19)
                    self._buf = self._buf[drop:]
                    self._mark = max(0, self._mark - drop)
                self._cond.notify_all()

    def close(self):
        try:
            self._sock.close()
        finally:
            self._log.close()

    def send(self, text):
        self._sock.sendall(text.encode())

    def mark(self):
        """Ignore everything printed so far in later `expect` calls."""
        with self._cond:
            self._mark = len(self._buf)

    def expect(self, pattern, timeout):
        """Wait for a regex to appear after the mark; returns the match."""
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                m = rx.search(self._buf, self._mark)
                if m:
                    self._mark = m.end()
                    return m
                left = deadline - time.monotonic()
                if left <= 0 or self._closed:
                    tail = self._buf[-2000:]
                    raise ConsoleError(
                        "timed out waiting for {!r}; console tail:\n{}".format(pattern, tail))
                self._cond.wait(min(left, 1.0))

    def login(self, timeout):
        """Activate the console after boot and quiet kernel messages."""
        self.expect(r"Please press Enter to activate this console", timeout)
        time.sleep(1)
        self.send("\n")
        self.expect(r"root@[^:]*:[^#]*# ", 30)
        self.run("dmesg -n 1", timeout=30)

    def run(self, cmd, timeout=60, check=True):
        """Run one shell command; returns (status, output)."""
        self._seq += 1
        token = "{:x}{:04d}".format(int(time.time()) & 0xFFFFF, self._seq)
        with self._cond:
            start = len(self._buf)
        self.send(wrap_command(cmd, token))
        deadline = time.monotonic() + timeout
        with self._cond:
            while True:
                res = parse_output(self._buf[start:], token)
                if res is not None:
                    break
                left = deadline - time.monotonic()
                if left <= 0 or self._closed:
                    raise ConsoleError("command timed out: {}\n{}".format(
                        cmd, self._buf[start:][-2000:]))
                self._cond.wait(min(left, 1.0))
        status, out = res
        if check and status != 0:
            raise ConsoleError("command failed ({}): {}\n{}".format(status, cmd, out))
        return status, out
