"""Network access: HTTP endpoint on the sim PC, driven through both relays (exe --remote and the Mac bash/curl
relay), plus wrong key / unreachable PC / browser-origin checks."""
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from simconfigmanager import core, net  # noqa: E402
from test_mcp import make_repo  # noqa: E402

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def run_relay(cmd, messages):
    p = subprocess.run(cmd, input="".join(json.dumps(m) + "\n" for m in messages).encode(), capture_output=True,
                       timeout=60, cwd=str(ROOT))
    return [json.loads(ln) for ln in p.stdout.decode().splitlines() if ln.strip()]


def session(call_args):
    return [INIT, {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "read_file",
                                                                         "arguments": {"file": "Prepar3D.cfg"}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "propose_change", "arguments":
                                                                         call_args}}]


def test_network():
    tmp = Path(tempfile.mkdtemp())
    repo, sim = make_repo(tmp)
    k_rw = net.add_key(repo, "Jan's MacBook", may_propose=True)
    k_ro = net.add_key(repo, "Read-only laptop", may_propose=False)
    activity = []
    port = free_port()
    srv = net.NetworkServer(repo.data_dir, port=port, host="127.0.0.1", on_activity=activity.append)
    srv.keys = net.load_keys(repo)
    srv.start()
    url = f"http://127.0.0.1:{port}/mcp"
    try:
        assert net.check_reachable(url)
        prop_args = {"file": "ProSim config.xml", "reason": "test",
                     "replacements": [{"find": "192.168.10.20", "replace": "192.168.10.40"}]}

        # 1. Windows relay: SimConfigManager-MCP.exe --remote URL --key KEY
        out = run_relay([sys.executable, "-m", "simconfigmanager.mcp_server", "--remote", url, "--key", k_rw["key"]],
                        session(prop_args))
        assert [o["id"] for o in out] == [1, 2, 3, 4], out
        assert out[0]["result"]["serverInfo"]["name"] == "sim-config-manager"
        assert "TEXTURE_MAX_LOAD=4096" in out[2]["result"]["content"][0]["text"]
        assert not out[3]["result"]["isError"], out[3]
        props = core.list_proposals(repo, "pending")
        assert len(props) == 1 and props[0]["author"] == "Claude via Jan's MacBook"
        assert any("Jan's MacBook" in a for a in activity), activity

        # 2. read-only key may read but not propose
        out = run_relay([sys.executable, "-m", "simconfigmanager.mcp_server", "--remote", url, "--key", k_ro["key"]],
                        session(prop_args))
        assert not out[2]["result"]["isError"] and out[3]["result"]["isError"]

        # 3. wrong key → clear error per request id
        out = run_relay([sys.executable, "-m", "simconfigmanager.mcp_server", "--remote", url, "--key", "nope"],
                        [INIT])
        assert out[0]["id"] == 1 and "Access key not accepted" in out[0]["error"]["message"]

        # 4. Mac relay (bash + curl), exactly as written into the Mac's Claude Desktop config
        if shutil.which("bash") and Path("/usr/bin/curl").exists():
            cfg = net.mac_config("Sim 1", url, k_rw["key"])["mcpServers"]["sim-1"]
            out = run_relay([cfg["command"]] + cfg["args"], session(prop_args)[:4])
            assert [o["id"] for o in out] == [1, 2, 3], out
            assert "TEXTURE_MAX_LOAD=4096" in out[2]["result"]["content"][0]["text"]
            # unreachable PC → JSON-RPC error with the right id, not silence
            dead = net.mac_config("Sim 1", f"http://127.0.0.1:{free_port()}/mcp", "x")["mcpServers"]["sim-1"]
            out = run_relay([dead["command"]] + dead["args"], [INIT])
            assert out and out[0]["id"] == 1 and "Cannot reach" in out[0]["error"]["message"], out

        # 5. requests from a web page (Origin header) are refused
        req = urllib.request.Request(url, data=json.dumps(INIT).encode(), method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {k_rw['key']}",
            "Origin": "http://evil.example"})
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected 403")
        except urllib.error.HTTPError as e:
            assert e.code == 403
    finally:
        srv.stop()

    # unreachable via the exe relay
    out = run_relay([sys.executable, "-m", "simconfigmanager.mcp_server", "--remote", url, "--key", "x"], [INIT])
    assert out[0]["id"] == 1 and "Cannot reach" in out[0]["error"]["message"]
    print("NETWORK TESTS PASSED")


if __name__ == "__main__":
    test_network()
