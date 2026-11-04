import json
import os
import ssl
import subprocess
import sys
import tempfile
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import fakegithub  # noqa: E402
import e2e  # noqa: E402


class HelpersTest(unittest.TestCase):
    def test_sha256sums_format(self):
        self.assertEqual(fakegithub.sha256sums_text([("a.img.gz", "ab" * 32)]),
                         "{}  a.img.gz\n".format("ab" * 32))

    def test_manifest_maps_board_to_asset(self):
        data = json.loads(fakegithub.manifest_json("qemu-standard-pc-q35-ich9-2009", "x.img.gz"))
        self.assertEqual(data, {"boards": {"qemu-standard-pc-q35-ich9-2009": "x.img.gz"}})

    def test_bump_version(self):
        self.assertEqual(e2e.bump_version("1.8.0"), "v2.0.0")
        self.assertEqual(e2e.bump_version("v1.8.0-rc1"), "v2.0.0")
        self.assertIsNone(e2e.bump_version(""))
        self.assertIsNone(e2e.bump_version("unknown"))

    def test_looks_like_luci(self):
        self.assertTrue(e2e.looks_like_luci('<link href="/luci-static/bootstrap/cascade.css">'))
        self.assertFalse(e2e.looks_like_luci('<div id="root"></div> /cgi-bin/luci'))
        self.assertFalse(e2e.looks_like_luci("<html></html>"))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.img = os.path.join(self.dir, "fw.img.gz")
        with open(self.img, "wb") as f:
            f.write(b"\x1f\x8b" + b"x" * 5000)
        with open(os.path.join(self.dir, "ca.pem"), "w") as f:
            f.write("PEM")
        assets = {"fw.img.gz": self.img}
        self.fake = fakegithub.FakeGitHub(
            "ghostnet-labs/firmware", [], {("v2.0.0", "fw.img.gz"): self.img}, self.dir)
        self.port = self.fake.serve(0)
        self.fake.releases = fakegithub.release_json(
            "v2.0.0", assets, "http://127.0.0.1:{}".format(self.port))

    def tearDown(self):
        self.fake.close()

    def get(self, path, ctx=None, scheme="http", port=None):
        url = "{}://127.0.0.1:{}{}".format(scheme, port or self.port, path)
        try:
            with urllib.request.urlopen(url, timeout=10, context=ctx) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_release_listing_and_pagination(self):
        code, body = self.get("/repos/ghostnet-labs/firmware/releases?per_page=30&page=1")
        self.assertEqual(code, 200)
        rel = json.loads(body)[0]
        self.assertEqual(rel["tag_name"], "v2.0.0")
        self.assertFalse(rel["draft"])
        self.assertEqual(rel["assets"][0]["size"], 5002)
        code, body = self.get("/repos/ghostnet-labs/firmware/releases?per_page=30&page=2")
        self.assertEqual(json.loads(body), [])
        self.assertEqual(len(self.fake.requested("/repos/ghostnet-labs/firmware/releases")), 2)

    def test_asset_download_follows_browser_url(self):
        url = self.fake.releases[0]["assets"][0]["browser_download_url"]
        with urllib.request.urlopen(url, timeout=10) as r:
            self.assertEqual(r.read(), open(self.img, "rb").read())

    def test_unknown_paths_404(self):
        self.assertEqual(self.get("/repos/other/repo/releases")[0], 404)
        self.assertEqual(self.get("/download/v9.9.9/fw.img.gz")[0], 404)
        self.assertEqual(self.get("/files/../e2e.py")[0], 404)

    def test_files_dir(self):
        self.assertEqual(self.get("/files/ca.pem"), (200, b"PEM"))

    def test_tls_with_generated_ca(self):
        ca, cert, key = fakegithub.make_tls_material(self.dir)
        port = self.fake.serve(0, tls=(cert, key))
        ctx = ssl.create_default_context(cafile=ca)
        ctx.check_hostname = False  # we connect by IP; the SAN is api.github.com
        code, _ = self.get("/repos/ghostnet-labs/firmware/releases", ctx=ctx, scheme="https", port=port)
        self.assertEqual(code, 200)
        out = subprocess.run(["openssl", "x509", "-in", cert, "-noout", "-ext", "subjectAltName"],
                             capture_output=True, text=True, check=True).stdout
        self.assertIn("DNS:api.github.com", out)


if __name__ == "__main__":
    unittest.main()
