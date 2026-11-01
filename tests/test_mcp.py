"""End-to-end test of the Claude connector: spawn it and speak MCP over stdio like Claude Desktop does."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simconfigmanager import core  # noqa: E402
from simconfigmanager.core import FolderSync, Repo  # noqa: E402

CFG = "[GRAPHICS]\r\nTEXTURE_MAX_LOAD=4096\r\nFULLSCREEN=True\r\n[TERRAIN]\r\nLOD_RADIUS=6.500000\r\n"
XML = '<?xml version="1.0" encoding="Windows-1252"?>\r\n<Config>\r\n  <Server>192.168.10.20</Server>\r\n</Config>\r\n'


class Client:
    def __init__(self, data_dir):
        self.p = subprocess.Popen([sys.executable, "-m", "simconfigmanager.mcp_server", "--data", str(data_dir)],
                                  cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
        self.n = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notify:
            self.n += 1
            msg["id"] = self.n
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()
        if notify:
            return None
        resp = json.loads(self.p.stdout.readline())
        assert resp["id"] == self.n, resp
        return resp

    def call(self, name, **args):
        r = self.send("tools/call", {"name": name, "arguments": args})["result"]
        return r["content"][0]["text"], r.get("isError", False)

    def close(self):
        self.p.stdin.close()
        self.p.wait(timeout=10)


def make_repo(tmp):
    sim = tmp / "sim"
    sim.mkdir()
    (sim / "Prepar3D.cfg").write_bytes(CFG.encode())
    (sim / "config.xml").write_bytes(XML.encode("cp1252"))
    repo = Repo(tmp / "data")
    repo.set("machine_name", "Sim 1")
    repo.add_file(str(sim / "Prepar3D.cfg"), group="P3D v5")
    repo.add_file(str(sim / "config.xml"), "ProSim config.xml", group="ProSim")
    repo.create_version("Baseline", "first", "tester")
    return repo, sim


def test_mcp_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    repo, sim = make_repo(tmp)
    # a second sim in the sync folder
    cloud = tmp / "cloud"
    cloud.mkdir()
    repo.set("sync_folder", str(cloud))
    other_dir = tmp / "sim2"
    other_dir.mkdir()
    (other_dir / "Prepar3D.cfg").write_bytes(CFG.replace("4096", "8192").encode())
    r2 = Repo(tmp / "data2")
    r2.set("machine_name", "Sim 2")
    r2.add_file(str(other_dir / "Prepar3D.cfg"), group="P3D v5")
    r2.ensure_baseline()
    FolderSync(r2, str(cloud)).push()
    FolderSync(repo, str(cloud)).push()
    # drift after the version
    (sim / "Prepar3D.cfg").write_bytes(CFG.replace("6.500000", "4.500000").encode())

    c = Client(tmp / "data")
    try:
        init = c.send("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}})["result"]
        assert init["protocolVersion"] == "2025-06-18" and init["capabilities"]["tools"] is not None
        c.send("notifications/initialized", notify=True)
        names = {t["name"] for t in c.send("tools/list")["result"]["tools"]}
        assert {"overview", "read_file", "search", "diff", "propose_change", "compare_with_other_sim"} <= names

        text, err = c.call("overview")
        assert not err and "Sim 1" in text and "differ" in text and "Sim 2" in text, text

        text, err = c.call("read_file", file="Prepar3D.cfg")
        assert not err and "2| TEXTURE_MAX_LOAD=4096" in text and "CRLF" in text, text
        text, err = c.call("read_file", file="Prepar3D.cfg", version="v1")
        assert "LOD_RADIUS=6.500000" in text
        text, err = c.call("read_file", file="Prepar3D.cfg", machine="Sim 2")
        assert "8192" in text, text

        text, err = c.call("search", pattern="texture")
        assert "Prepar3D.cfg:2 [GRAPHICS]" in text, text

        text, err = c.call("diff", file="Prepar3D.cfg")
        assert "-LOD_RADIUS=6.500000" in text and "+LOD_RADIUS=4.500000" in text, text

        text, err = c.call("compare_with_other_sim", file="Prepar3D.cfg", machine="Sim 2")
        assert "8192" in text and "4096" in text

        text, err = c.call("read_file", file="nope.cfg")
        assert err and "No tracked file" in text

        # proposals: bad find → error, good one → pending proposal, file untouched
        text, err = c.call("propose_change", file="ProSim config.xml", reason="x",
                           replacements=[{"find": "<Server>10.0.0.1", "replace": "y"}])
        assert err
        text, err = c.call("propose_change", file="ProSim config.xml", reason="Point MCP at the new server",
                           replacements=[{"find": "192.168.10.20", "replace": "192.168.10.30"}])
        assert not err and "NOT been applied" in text and "+  <Server>192.168.10.30" in text, text
        text, err = c.call("propose_change", file="ProSim config.xml", reason="break it",
                           replacements=[{"find": "</Server>", "replace": "</Srv>"}])
        assert err and "valid XML" in text
        assert b"192.168.10.20" in (sim / "config.xml").read_bytes()  # untouched
    finally:
        c.close()

    pend = core.list_proposals(repo, "pending")
    assert len(pend) == 1
    core.apply_proposal(repo, pend[0], approved_by="tester")
    data = (sim / "config.xml").read_bytes()
    assert b"192.168.10.30" in data and data.count(b"\r\n") == 4  # CRLF + encoding kept
    assert core.list_proposals(repo, "applied")
    assert any("Claude" in v.message for v in repo.file_history(repo.files()[0].id) + repo.file_history(
        repo.files()[1].id))
    print("MCP TESTS PASSED")


if __name__ == "__main__":
    test_mcp_end_to_end()
