"""The app reporting to a real hub (Node.js server started from hub/). Skipped if Node isn't installed."""
import http.cookiejar
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from simconfigmanager import core  # noqa: E402
from test_features import setup, bump, CFG  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_app_reports_to_hub():
    node = shutil.which("node")
    if not node:
        print("node not installed — skipped")
        return
    tmp = Path(tempfile.mkdtemp())
    port = free_port()
    env = dict(os.environ, PORT=str(port), HOST="127.0.0.1", SCM_HUB_DATA=str(tmp / "hubdata"),
               SCM_HUB_ADMIN_PASSWORD="pw", SCM_HUB_CONFIG=str(tmp / "none.json"))
    proc = subprocess.Popen([node, "src/server.js"], cwd=str(ROOT / "hub"), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(base + "/health", timeout=1)
                break
            except OSError:
                time.sleep(0.1)
        jar = http.cookiejar.CookieJar()
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), urllib.request.ProxyHandler({}))

        def call(path, body=None, method="GET"):
            req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"Content-Type": "application/json", "X-Requested-With": "scm"})
            with op.open(req, timeout=10) as r:
                return json.loads(r.read())
        call("/api/login", {"name": "admin", "password": "pw"}, "POST")
        key = call("/api/sims", {"name": "Sim 1"}, "POST")["key"]

        repo, cfg, xml, fc, fx = setup(tmp)
        repo.create_version("Certified", "", "Jan")
        repo.save_rulebook_entry({"file_id": fc.id, "selector": "GRAPHICS/TEXTURE_MAX_LOAD", "required": "4096"})
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("HTTP_PROXY", None)
        os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        try:
            core.hub_push(repo, base, "hub_wrong")
            raise AssertionError("wrong key must fail")
        except ValueError as e:
            assert "401" in str(e)
        res = core.hub_push(repo, base, key)
        assert res["ok"] and res["sim"] == "Sim 1"
        sims = call("/api/sims")["sims"]
        assert sims[0]["state"] == "ok" and sims[0]["ready"] and sims[0]["version"]["number"] == 1, sims

        bump(cfg, CFG.replace("4096", "1024"))
        repo.process_changes(repo.scan())
        core.hub_push(repo, base, key)
        s = call("/api/sims")["sims"][0]
        assert s["state"] == "bad" and s["violations"] == 1 and s["mismatches"] == 1, s
        d = call(f"/api/sims/{s['id']}")
        assert any(f["file"] == "Prepar3D.cfg" and f["status"] == "modified" for f in d["report"]["files_status"])
        assert d["report"]["rulebook"][0]["ok"] is False
        types = {e["type"] for e in call("/api/events")["events"]}
        assert {"drift", "rulebook", "alert"} <= types, types
        print("HUB OK")
    finally:
        proc.terminate()
        proc.wait(timeout=10)


if __name__ == "__main__":
    test_app_reports_to_hub()
