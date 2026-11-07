"""A stand-in for the GitHub releases API, for the OTA end-to-end test.

openmanetd's online update check calls
GET https://api.github.com/repos/<owner>/<name>/releases and then downloads
each asset's browser_download_url. The test points api.github.com at this
server inside the guest and trusts a throwaway CA, so the real openmanetd
code path runs unchanged. The same server also serves plain HTTP for
fetching the CA into the guest.
"""

import hashlib
import http.server
import json
import os
import ssl
import subprocess
import threading
import urllib.parse


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256sums_text(entries):
    """entries: iterable of (filename, hexdigest) -> sha256sum(1) format."""
    return "".join("{}  {}\n".format(d, n) for n, d in entries)


def manifest_json(board_name, asset_name):
    """The release manifest openmanetd uses to map board_name to an image."""
    return json.dumps({"boards": {board_name: asset_name}}, indent=2) + "\n"


def release_json(tag, asset_files, download_base, published_at="2026-01-01T00:00:00Z"):
    """Build the /releases response for one release.

    asset_files: {asset name: local path}.
    """
    assets = []
    for i, (name, path) in enumerate(sorted(asset_files.items()), start=1):
        assets.append({
            "id": i,
            "name": name,
            "size": os.path.getsize(path),
            "content_type": "application/octet-stream",
            "browser_download_url": "{}/download/{}/{}".format(
                download_base.rstrip("/"), tag, urllib.parse.quote(name)),
        })
    return [{
        "id": 1,
        "tag_name": tag,
        "name": "OpenMANET {} (QEMU end-to-end test)".format(tag.lstrip("v")),
        "body": "Fake release served by scripts/qemu-e2e.",
        "draft": False,
        "prerelease": False,
        "published_at": published_at,
        "assets": assets,
    }]


def make_tls_material(workdir, hostname="api.github.com"):
    """Create a throwaway CA and a server certificate for `hostname`.

    Returns (ca_pem, cert_pem, key_pem) paths. Uses the openssl CLI.
    """
    ca_key = os.path.join(workdir, "ca.key")
    ca_pem = os.path.join(workdir, "ca.pem")
    key = os.path.join(workdir, "server.key")
    csr = os.path.join(workdir, "server.csr")
    cert = os.path.join(workdir, "server.pem")
    ext = os.path.join(workdir, "server.ext")
    run = lambda *a: subprocess.run(a, check=True, capture_output=True)  # noqa: E731
    run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
        "-subj", "/CN=OpenMANET QEMU e2e CA", "-keyout", ca_key, "-out", ca_pem,
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=" + hostname,
        "-keyout", key, "-out", csr)
    with open(ext, "w") as f:
        f.write("subjectAltName=DNS:{}\nextendedKeyUsage=serverAuth\n".format(hostname))
    run("openssl", "x509", "-req", "-in", csr, "-CA", ca_pem, "-CAkey", ca_key,
        "-CAcreateserial", "-days", "2", "-extfile", ext, "-out", cert)
    return ca_pem, cert, key


class FakeGitHub:
    """Serves /repos/<repo>/releases, asset downloads and /files/*."""

    def __init__(self, repo, releases, files, files_dir):
        self.repo = repo
        self.releases = releases
        self.files = files            # {(tag, name): local path}
        self.files_dir = files_dir    # plain files served under /files/
        self.requests = []
        self._lock = threading.Lock()
        self._servers = []

    def log(self, method, path, status):
        with self._lock:
            self.requests.append((method, path, status))

    def requested(self, prefix):
        with self._lock:
            return [r for r in self.requests if r[1].startswith(prefix)]

    def _handler(self):
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # quiet; we keep our own log
                pass

            def _send(self, status, body=b"", ctype="application/json", path=None):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                size = os.path.getsize(path) if path else len(body)
                self.send_header("Content-Length", str(size))
                self.end_headers()
                if path:
                    with open(path, "rb") as f:
                        while True:
                            chunk = f.read(1 << 20)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                    fake.log(self.command, self.path, status)
                else:
                    # Log before the body goes out: the client can act on
                    # the reply (and a test can count requests) as soon as
                    # it is written.
                    fake.log(self.command, self.path, status)
                    self.wfile.write(body)

            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                parts = [urllib.parse.unquote(p) for p in url.path.split("/") if p]
                if url.path == "/repos/{}/releases".format(fake.repo):
                    page = urllib.parse.parse_qs(url.query).get("page", ["1"])[0]
                    data = fake.releases if page == "1" else []
                    return self._send(200, json.dumps(data).encode())
                if len(parts) == 3 and parts[0] == "download":
                    path = fake.files.get((parts[1], parts[2]))
                    if path:
                        return self._send(200, ctype="application/octet-stream", path=path)
                if len(parts) == 2 and parts[0] == "files":
                    path = os.path.join(fake.files_dir, os.path.basename(parts[1]))
                    if os.path.isfile(path):
                        return self._send(200, ctype="application/octet-stream", path=path)
                return self._send(404, b'{"message":"Not Found"}')

        return Handler

    def serve(self, port, tls=None):
        """Start a server on 127.0.0.1:port; tls=(cert, key) for HTTPS."""
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(*tls)
            srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self._servers.append(srv)
        return srv.server_address[1]

    def close(self):
        for srv in self._servers:
            srv.shutdown()
            srv.server_close()
