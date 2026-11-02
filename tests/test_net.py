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



FAKE_TUNNEL_CLIENT = r'''#!/bin/bash
# stand-in for OpenAI's tunnel-client: remembers init settings, and on "run" drives the MCP command like the
# real client does (JSON-RPC lines over its stdin/stdout)
STATE="$HOME/.fake-tunnel"
case "$1" in
  --version) echo "tunnel-client fake 0.0";;
  init) shift; while [ $# -gt 0 ]; do case "$1" in --profile) P="$2";; --tunnel-id) T="$2";; --mcp-command) C="$2";; esac; shift; done
        mkdir -p "$STATE"; printf '%s\n' "$T" > "$STATE/$P.tid"; printf '%s\n' "$C" > "$STATE/$P.cmd";;
  doctor) echo "doctor ok";;
  run) P="$3"; printf '%s\n' "$CONTROL_PLANE_API_KEY" > "$STATE/$P.runkey"
       printf '%s\n%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"openai-tunnel","version":"1"}}}' \
         '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"propose_change","arguments":{"file":"Prepar3D.cfg","reason":"via ChatGPT","replacements":[{"find":"FULLSCREEN=True","replace":"FULLSCREEN=False"}]}}}' \
         | eval "$(cat "$STATE/$P.cmd")" > "$STATE/$P.out";;
esac
'''
FAKE_SECURITY = r'''#!/bin/bash
# stand-in for macOS "security" (Keychain)
F="$HOME/.fake-keychain"
if [ "$1" = add-generic-password ]; then while [ $# -gt 0 ]; do [ "$1" = -w ] && printf '%s' "$2" > "$F"; shift; done
elif [ "$1" = find-generic-password ]; then cat "$F"; fi
'''


def test_chatgpt_mac_setup():
    if not shutil.which("bash") or not Path("/usr/bin/curl").exists():
        return  # the Mac scripts need bash + curl (CI on Windows skips this)
    tmp = Path(tempfile.mkdtemp())
    repo, sim = make_repo(tmp)
    k = net.add_key(repo, "Jan – MacBook", may_propose=True)
    port = free_port()
    srv = net.NetworkServer(repo.data_dir, port=port, host="127.0.0.1")
    srv.keys = net.load_keys(repo)
    srv.start()
    try:
        url = f"http://127.0.0.1:{port}/mcp"
        script = net.chatgpt_setup_script("Sim 1", "Jan", url, k["key"])
        home, bindir = tmp / "home", tmp / "bin"
        home.mkdir()
        bindir.mkdir()
        for name, body in (("tunnel-client", FAKE_TUNNEL_CLIENT), ("security", FAKE_SECURITY),
                           ("open", "#!/bin/bash\nexit 0\n"), ("brew", "#!/bin/bash\nexit 0\n")):
            (bindir / name).write_text(body)
            (bindir / name).chmod(0o755)
        (tmp / "setup.command").write_text(script)
        env = {"HOME": str(home), "USER": "jan", "PATH": f"{bindir}:/usr/bin:/bin"}
        assert subprocess.run(["bash", "-n", str(tmp / "setup.command")]).returncode == 0
        r = subprocess.run(["bash", str(tmp / "setup.command")], input=b"tunnel_abc123\nsk-runtime-secret\n",
                           capture_output=True, env=env, timeout=60)
        assert r.returncode == 0, r.stdout.decode() + r.stderr.decode()
        out = r.stdout.decode()
        assert "OK - Sim 1 answers" in out, out
        d = home / "SimConfigManager"
        relay = d / "sim-1-relay.sh"
        assert relay.exists() and oct(relay.stat().st_mode)[-3:] == "700"
        assert (home / ".fake-tunnel" / "scm-sim-1.tid").read_text().strip() == "tunnel_abc123"
        assert (home / ".fake-keychain").read_text() == "sk-runtime-secret"
        assert "sk-runtime-secret" not in "".join(p.read_text() for p in d.iterdir())  # API key only in Keychain

        # the on/off switch: starts the tunnel with the key from the Keychain, ChatGPT's calls reach the sim
        start = d / "Start ChatGPT access - Sim 1.command"
        assert start.exists()
        r = subprocess.run(["bash", str(start)], capture_output=True, env=env, timeout=60)
        assert r.returncode == 0, r.stderr.decode()
        assert (home / ".fake-tunnel" / "scm-sim-1.runkey").read_text().strip() == "sk-runtime-secret"
        lines = [json.loads(x) for x in (home / ".fake-tunnel" / "scm-sim-1.out").read_text().splitlines()]
        assert lines[0]["result"]["serverInfo"]["name"] == "sim-config-manager", lines
        assert not lines[1]["result"]["isError"], lines[1]
        props = core.list_proposals(repo, "pending")
        assert props and props[-1]["author"] == "ChatGPT via Jan – MacBook", props
        assert "ChatGPT" in srv.last_seen["Jan – MacBook"][1]
        print("CHATGPT MAC SETUP TESTS PASSED")
    finally:
        srv.stop()


if __name__ == "__main__":
    test_network()
    test_chatgpt_mac_setup()
