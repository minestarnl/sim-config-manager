"""A small in-memory stand-in for the Google Drive v3 REST API — just the calls SimConfigManager makes:
files.list (q: name / parents / mimeType / trashed), files.get (metadata + alt=media), files.create (folder),
resumable upload (POST/PATCH → session → PUT). Requires a Bearer token. Also simulates a 503 once."""
import json
import re
import threading
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FOLDER = "application/vnd.google-apps.folder"


class FakeDrive:
    def __init__(self, token="test-token"):
        self.token = token
        self.files = {"root": {"id": "root", "name": "My Drive", "mimeType": FOLDER, "parents": []},
                      "shared123": {"id": "shared123", "name": "Sim configs", "mimeType": FOLDER, "parents": []}}
        self.content = {}
        self.sessions = {}
        self.calls = 0
        self.fail_next = 0
        self.lock = threading.Lock()

    def start(self):
        drive = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                if body:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode())

            def _auth(self):
                drive.calls += 1
                if self.headers.get("Authorization") != f"Bearer {drive.token}":
                    self._json({"error": {"code": 401}}, 401)
                    return False
                if drive.fail_next:
                    drive.fail_next -= 1
                    self._json({"error": {"code": 503}}, 503)
                    return False
                return True

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n) if n else b""

            def do_GET(self):
                if not self._auth():
                    return
                u = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(u.query)
                if u.path == "/drive/v3/files":
                    q = qs.get("q", [""])[0]
                    res = []
                    for f in drive.files.values():
                        if f.get("trashed"):
                            continue
                        m = re.search(r"name='((?:[^'\\]|\\.)*)'", q)
                        if m and f["name"] != m.group(1).replace("\\'", "'").replace("\\\\", "\\"):
                            continue
                        m = re.search(r"'([^']+)' in parents", q)
                        if m and m.group(1) not in f["parents"]:
                            continue
                        if f"mimeType='{FOLDER}'" in q and f["mimeType"] != FOLDER:
                            continue
                        res.append({"id": f["id"], "name": f["name"]})
                    page = int(qs.get("pageToken", ["0"])[0])
                    size = 2  # force paging to be exercised
                    chunk = res[page:page + size]
                    out = {"files": chunk}
                    if page + size < len(res):
                        out["nextPageToken"] = str(page + size)
                    return self._json(out)
                m = re.match(r"^/drive/v3/files/([^/]+)$", u.path)
                if m:
                    f = drive.files.get(m.group(1))
                    if not f:
                        return self._json({"error": {"code": 404}}, 404)
                    if qs.get("alt") == ["media"]:
                        return self._send(200, drive.content.get(f["id"], b""), "application/octet-stream")
                    return self._json({"id": f["id"], "name": f["name"], "mimeType": f["mimeType"]})
                self._json({}, 404)

            def do_POST(self):
                if not self._auth():
                    return
                u = urllib.parse.urlparse(self.path)
                body = self._body()
                if u.path == "/drive/v3/files":
                    meta = json.loads(body)
                    fid = uuid.uuid4().hex[:12]
                    drive.files[fid] = {"id": fid, "name": meta["name"], "mimeType": meta.get("mimeType", ""),
                                        "parents": meta.get("parents", [])}
                    return self._json({"id": fid})
                if u.path == "/upload/drive/v3/files":
                    meta = json.loads(body)
                    sid = uuid.uuid4().hex
                    drive.sessions[sid] = ("new", meta)
                    host = self.headers.get("Host")
                    return self._send(200, b"", headers={"Location": f"http://{host}/upload/session/{sid}"})
                self._json({}, 404)

            def do_PATCH(self):
                if not self._auth():
                    return
                u = urllib.parse.urlparse(self.path)
                self._body()
                m = re.match(r"^/upload/drive/v3/files/([^/]+)$", u.path)
                if m and m.group(1) in drive.files:
                    sid = uuid.uuid4().hex
                    drive.sessions[sid] = ("update", m.group(1))
                    host = self.headers.get("Host")
                    return self._send(200, b"", headers={"Location": f"http://{host}/upload/session/{sid}"})
                self._json({}, 404)

            def do_PUT(self):
                if not self._auth():
                    return
                u = urllib.parse.urlparse(self.path)
                body = self._body()
                m = re.match(r"^/upload/session/([^/]+)$", u.path)
                if not m or m.group(1) not in drive.sessions:
                    return self._json({}, 404)
                kind, arg = drive.sessions.pop(m.group(1))
                if kind == "new":
                    fid = uuid.uuid4().hex[:12]
                    drive.files[fid] = {"id": fid, "name": arg["name"], "mimeType": "", "parents": arg["parents"]}
                else:
                    fid = arg
                drive.content[fid] = body
                return self._json({"id": fid})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()

    def names_in(self, folder_name):
        fid = next(f["id"] for f in self.files.values() if f["name"] == folder_name and f["mimeType"] == FOLDER)
        return sorted(f["name"] for f in self.files.values() if fid in f["parents"])
