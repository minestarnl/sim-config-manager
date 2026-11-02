"""Updater against a stand-in GitHub API: private-repo token, redirect to storage without the token,
checksum verification."""
import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simconfigmanager import updater  # noqa: E402

INSTALLER = b"MZ fake installer bytes" * 100


def start(good_sum=True):
    state = {"auth_on_storage": None}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            host = f"http://{self.headers['Host']}"
            auth = self.headers.get("Authorization")
            if self.path == "/repos/o/r/releases/latest":
                if auth != "Bearer tok":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"tag_name": "v9.9.0", "name": "9.9", "body": "notes", "html_url": host,
                                   "assets": [
                                       {"name": "SimConfigManager-Setup-9.9.0.exe", "url": f"{host}/asset/1",
                                        "browser_download_url": f"{host}/dl/1", "size": len(INSTALLER)},
                                       {"name": "SHA256SUMS.txt", "url": f"{host}/asset/2",
                                        "browser_download_url": f"{host}/dl/2", "size": 1}]}).encode()
            elif self.path.startswith("/asset/"):
                assert auth == "Bearer tok" and self.headers["Accept"] == "application/octet-stream"
                self.send_response(302)
                self.send_header("Location", f"{host}/storage/{self.path[-1]}?sig=abc")
                self.end_headers()
                return
            elif self.path.startswith("/storage/"):
                state["auth_on_storage"] = auth
                if self.path.startswith("/storage/1"):
                    body = INSTALLER
                else:
                    h = hashlib.sha256(INSTALLER if good_sum else b"x").hexdigest()
                    body = f"{h} *SimConfigManager-Setup-9.9.0.exe\nabc *other.exe\n".encode()
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", state


def test_updater():
    assert updater.is_newer("v1.10.0", "1.9.3") and not updater.is_newer("v1.8.0", "1.8.0")
    httpd, api, state = start()
    try:
        try:
            updater.latest_release("o/r", "", api)
            raise AssertionError("private repo without token must fail clearly")
        except updater.UpdateError as e:
            assert "token" in str(e)
        rel = updater.latest_release("o/r", "tok", api)
        assert rel["tag"] == "v9.9.0"
        path = updater.fetch_installer(rel, "tok")
        assert Path(path).read_bytes() == INSTALLER
        assert state["auth_on_storage"] is None, "token must not be sent to the storage redirect"
    finally:
        httpd.shutdown()
    httpd, api, state = start(good_sum=False)
    try:
        rel = updater.latest_release("o/r", "tok", api)
        try:
            updater.fetch_installer(rel, "tok")
            raise AssertionError("checksum mismatch must refuse")
        except updater.UpdateError as e:
            assert "checksum" in str(e)
    finally:
        httpd.shutdown()
    print("UPDATER OK")


if __name__ == "__main__":
    test_updater()
