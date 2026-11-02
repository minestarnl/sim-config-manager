"""
Claude over the network: lets Claude Desktop on another computer (e.g. a mechanic's MacBook) use the
connector of a sim PC on the LAN.

    MacBook: Claude Desktop ──stdio──▶ relay (bash + curl, nothing to install)
                                         │  HTTP POST + access key (LAN only)
    Sim PC:  SimConfigManager ◀──────────┘  /mcp  (MCP Streamable HTTP, JSON responses)

The sim PC side runs inside the SimConfigManager app. Every access key has a name (who it belongs to) and
can be allowed or not to propose changes. Proposals still need approval on the sim PC.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from .core import APP_NAME, APP_VERSION

DEFAULT_PORT = 8765
CLIENT_NAMES = {"claude": "Claude", "chatgpt": "ChatGPT"}
FIREWALL_RULE = f"{APP_NAME} - Claude network access"


# --------------------------------------------------------------------------- access keys

def new_key() -> str:
    return "scm_" + secrets.token_urlsafe(24)


def load_keys(repo) -> list[dict]:
    try:
        return json.loads(repo.get("net_keys") or "[]")
    except ValueError:
        return []


def save_keys(repo, keys: list[dict]) -> None:
    repo.set("net_keys", json.dumps(keys))


def add_key(repo, name: str, may_propose: bool = True) -> dict:
    keys = load_keys(repo)
    k = {"name": name.strip() or "Unnamed", "key": new_key(), "created": time.time(), "may_propose": may_propose}
    keys.append(k)
    save_keys(repo, keys)
    return k


def lan_addresses() -> list[str]:
    """IPv4 addresses of this PC that other computers on the LAN can use."""
    addrs = set()
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


# --------------------------------------------------------------------------- HTTP server (sim PC)

class NetworkServer:
    """MCP over HTTP inside the app. Thread-safe: each request opens its own read-only store connection."""

    def __init__(self, data_dir, port: int = DEFAULT_PORT, host: str = "0.0.0.0",
                 on_activity: Optional[Callable[[str], None]] = None):
        self.data_dir = str(data_dir)
        self.port = port
        self.host = host
        self.keys: list[dict] = []          # replaced by the app whenever keys change
        self.last_seen: dict[str, tuple[float, str]] = {}   # key name -> (time, "ip (client)")
        self.recent: dict[str, float] = {}
        self.on_activity = on_activity
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.thread: Optional[threading.Thread] = None

    # ----- lifecycle
    def start(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = f"{APP_NAME}/{APP_VERSION}"

            def log_message(self, fmt, *args):  # keep quiet
                pass

            def _send(self, code: int, body: bytes = b"", ctype: str = "application/json"):
                self.send_response(code)
                if body:
                    self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def do_GET(self):
                if self.path.rstrip("/") in ("", "/health"):
                    from .core import Repo
                    try:
                        name = Repo(server.data_dir, read_only=True).machine
                    except Exception:  # noqa: BLE001
                        name = socket.gethostname()
                    self._send(200, json.dumps({"app": APP_NAME, "version": APP_VERSION, "sim": name,
                                                "mcp": "/mcp"}).encode(), "application/json")
                elif self.path.startswith("/mcp"):
                    self.send_response(405)
                    self.send_header("Allow", "POST")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send(404)

            def do_DELETE(self):
                self._send(405)

            def do_POST(self):
                if not self.path.startswith("/mcp"):
                    return self._send(404)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    msg = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    msg = None
                ids = [m.get("id") for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict)]

                def rpc_error(code, http_code, text):
                    body = [{"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": text}}
                            for i in ids if i is not None]
                    out = body if isinstance(msg, list) else (body[0] if body else
                                                              {"jsonrpc": "2.0", "id": None,
                                                               "error": {"code": code, "message": text}})
                    self._send(http_code, (json.dumps(out) + "\n").encode())

                # Browsers always send Origin; real MCP clients/relays don't. Blocks DNS-rebinding attacks.
                if self.headers.get("Origin"):
                    return rpc_error(-32001, 403, "Requests from web pages are not allowed.")
                auth = self.headers.get("Authorization", "")
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                key = next((k for k in server.keys if token and hmac.compare_digest(k["key"], token)), None)
                if key is None:
                    return rpc_error(-32001, 401, f"Access key not accepted by {APP_NAME} on this sim PC. Ask for "
                                                  f"a new key in the app's Claude tab → Network access.")
                if msg is None:
                    return rpc_error(-32700, 400, "Parse error")
                ip = self.client_address[0]
                client = CLIENT_NAMES.get((self.headers.get("X-SCM-Client") or "claude").strip().lower(), "Claude")
                tag = f"{key['name']} / {client}"
                first = tag not in server.recent or time.time() - server.recent[tag] > 600
                server.recent[tag] = time.time()
                server.last_seen[key["name"]] = (time.time(), f"{ip} ({client})")
                if first and server.on_activity:
                    server.on_activity(f"{client} connected over the network: {key['name']} from {ip}")
                from .mcp_server import Server
                handler = Server(server.data_dir, author=f"{client} via {key['name']}",
                                 may_propose=bool(key.get("may_propose", True)))
                if isinstance(msg, list):
                    resp = [r for r in (handler.handle(m) for m in msg if isinstance(m, dict)) if r]
                else:
                    resp = handler.handle(msg) if isinstance(msg, dict) else None
                if isinstance(msg, dict) and msg.get("method") == "tools/call" and server.on_activity:
                    tool = (msg.get("params") or {}).get("name")
                    if tool == "propose_change":
                        server.on_activity(f"{key['name']} (via {client}) proposed a change")
                if not resp:
                    return self._send(202)
                self._send(200, (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))

        self.httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="scm-net", daemon=True)
        self.thread.start()

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None

    @property
    def running(self) -> bool:
        return self.httpd is not None


# --------------------------------------------------------------------------- relay (laptop side)

def relay(url: str, key: str, inp=None, out=None, timeout: float = 300) -> None:
    """stdio ⇄ HTTP relay, for Windows laptops (SimConfigManager-MCP.exe --remote URL --key KEY)."""
    inp = inp or sys.stdin.buffer
    out = out or sys.stdout.buffer
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # LAN: never via a proxy
    for raw in inp:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError:
            continue
        req = urllib.request.Request(url, data=raw, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {key}"})
        try:
            with opener.open(req, timeout=timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
        except (urllib.error.URLError, OSError) as e:
            ids = [m.get("id") for m in (msg if isinstance(msg, list) else [msg]) if isinstance(m, dict)]
            errs = [{"jsonrpc": "2.0", "id": i, "error": {"code": -32000, "message": unreachable_text(url, e)}}
                    for i in ids if i is not None]
            body = (json.dumps(errs if isinstance(msg, list) else errs[0]) + "\n").encode() if errs else b""
        body = body.strip()
        if body:
            out.write(body + b"\n")
            out.flush()


def unreachable_text(url: str, err=None) -> str:
    return (f"Cannot reach the sim PC at {url}" + (f" ({err})" if err else "") + ". Check that the sim PC is on, "
            f"{APP_NAME} is running there with Network access switched on, and this computer is on the same "
            f"network (not guest Wi-Fi).")


# Relay for macOS (and Linux): bash + curl, both present on every Mac — nothing to download or install.
MAC_RELAY = r'''URL="$1"; KEY="$2"; CLIENT="${3:-claude}"
while IFS= read -r line || [ -n "$line" ]; do
  [ -z "$line" ] && continue
  resp=$(printf '%s' "$line" | /usr/bin/curl -sS --noproxy '*' --max-time 300 -X POST -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "Authorization: Bearer $KEY" -H "X-SCM-Client: $CLIENT" --data-binary @- "$URL")
  if [ $? -eq 0 ]; then
    [ -n "$resp" ] && printf '%s\n' "$resp"
  else
    id=$(printf '%s' "$line" | sed -n 's/.*"id"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*[,}].*/\1/p')
    [ -n "$id" ] && printf '{"jsonrpc":"2.0","id":%s,"error":{"code":-32000,"message":"Cannot reach the sim PC at %s. Check that it is on, SimConfigManager is running there with Network access switched on, and this Mac is on the same network."}}\n' "$id" "$URL"
  fi
done'''


def server_key_name(sim_name: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "-", sim_name.lower()).strip("-") or "pc"
    return slug if slug.startswith("sim") else "sim-" + slug


def mac_config(sim_name: str, url: str, key: str) -> dict:
    return {"mcpServers": {server_key_name(sim_name): {
        "command": "/bin/bash", "args": ["-c", MAC_RELAY, "sim-config-relay", url, key]}}}


def windows_config(sim_name: str, url: str, key: str,
                   exe: str = r"C:\SimConfigManager\SimConfigManager-MCP.exe") -> dict:
    return {"mcpServers": {server_key_name(sim_name): {"command": exe, "args": ["--remote", url, "--key", key]}}}


# --------------------------------------------------------------------------- Windows firewall

def set_firewall_rule(port: int, enable: bool) -> tuple[bool, str]:
    """Allow the port from the local network only (remoteip=localsubnet). Needs admin."""
    if sys.platform != "win32":
        return True, "not Windows"
    import subprocess
    flags = {"creationflags": 0x08000000}
    subprocess.run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={FIREWALL_RULE}"],
                   capture_output=True, **flags)
    if not enable:
        return True, "removed"
    r = subprocess.run(["netsh", "advfirewall", "firewall", "add", "rule", f"name={FIREWALL_RULE}", "dir=in",
                        "action=allow", "protocol=TCP", f"localport={port}", "remoteip=localsubnet", "profile=any",
                        f"program={sys.executable}" if getattr(sys, "frozen", False) else "enable=yes"],
                       capture_output=True, text=True, **flags)
    return r.returncode == 0, (r.stdout or r.stderr or "").strip()


def check_reachable(url: str, timeout: float = 3) -> bool:
    base = url.rsplit("/mcp", 1)[0]
    try:
        with urllib.request.urlopen(base + "/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def hostname() -> str:
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


# --------------------------------------------------------------------------- ChatGPT via the engineer's Mac
#
# ChatGPT reaches MCP servers from OpenAI's cloud. OpenAI's Secure MCP Tunnel avoids exposing anything: the
# engineer runs OpenAI's `tunnel-client` on their own Mac; it opens an OUTBOUND connection to OpenAI, receives
# ChatGPT's requests and hands them to a local command — our relay — which forwards them over the LAN to the
# sim PC with the engineer's personal key. ChatGPT can only reach the sim while that Mac runs the tunnel.

def _sh(v: str) -> str:
    import shlex
    return shlex.quote(v)


def relay_script(url: str, key: str, client: str = "chatgpt") -> str:
    """Standalone relay file (values baked in) — used as tunnel-client's --mcp-command."""
    return ("#!/bin/bash\n# SimConfigManager relay: stdio <-> sim PC over the LAN. Contains a personal access key.\n"
            f"set -- {_sh(url)} {_sh(key)} {_sh(client)}\n" + MAC_RELAY + "\n")


def chatgpt_setup_script(sim_name: str, person: str, url: str, key: str) -> str:
    """One-time setup for the engineer's Mac. Run with:  bash setup-chatgpt-<sim>.command"""
    import re
    slug = server_key_name(sim_name)
    sim_disp = re.sub(r"[^A-Za-z0-9 ._-]", "", sim_name) or slug
    relay = relay_script(url, key, "chatgpt")
    return f"""#!/bin/bash
# SimConfigManager — set up ChatGPT access to {sim_disp} on THIS Mac ({person}).
# Run it in Terminal:   bash ~/Downloads/setup-chatgpt-{slug}.command
# Needs: Homebrew (https://brew.sh), an OpenAI tunnel ID and a runtime API key (see the steps in the app).
set -e
SIM={_sh(sim_disp)}
SLUG={_sh(slug)}
URL={_sh(url)}
DIR="$HOME/SimConfigManager"
PROFILE="scm-$SLUG"
mkdir -p "$DIR" && chmod 700 "$DIR"

echo "== 1/5  Checking the connection to $SIM ($URL)"
if /usr/bin/curl -s --noproxy '*' --max-time 5 "${{URL%/mcp}}/health" | grep -q SimConfigManager; then
  echo "       OK - $SIM answers."
else
  echo "       Can't reach $SIM right now (is this Mac on the sim network and is network access on?)."
  echo "       Setup continues; it will work once the sim PC is reachable."
fi

echo "== 2/5  OpenAI tunnel-client"
if ! command -v tunnel-client >/dev/null 2>&1; then
  if ! command -v brew >/dev/null 2>&1; then
    echo "       Homebrew is needed first: open https://brew.sh, install it, then run this script again."; exit 1
  fi
  brew install openai/tools/tunnel-client
fi
tunnel-client --version || true

echo "== 3/5  Writing the relay to $DIR/$SLUG-relay.sh"
cat > "$DIR/$SLUG-relay.sh" <<'SCM_RELAY'
{relay}SCM_RELAY
chmod 700 "$DIR/$SLUG-relay.sh"

echo "== 4/5  Linking your OpenAI tunnel"
read -r -p "       Tunnel ID (starts with tunnel_): " TID
read -r -s -p "       Runtime API key (input hidden): " RKEY; echo
security add-generic-password -U -s "SimConfigManager tunnel $SLUG" -a "$USER" -w "$RKEY"
echo "       Key saved in your macOS Keychain."
export CONTROL_PLANE_API_KEY="$RKEY"
tunnel-client init --sample sample_mcp_stdio_local --profile "$PROFILE" --tunnel-id "$TID" \\
  --mcp-command "/bin/bash $DIR/$SLUG-relay.sh"
tunnel-client doctor --profile "$PROFILE" --explain || echo "       (doctor reported problems - see above)"

echo "== 5/5  Creating the on/off switch"
START="$DIR/Start ChatGPT access - $SIM.command"
cat > "$START" <<SCM_START
#!/bin/bash
# Double-click to let ChatGPT reach $SIM through this Mac. Close the window (or Ctrl+C) to stop.
export CONTROL_PLANE_API_KEY="\\$(security find-generic-password -s 'SimConfigManager tunnel $SLUG' -a "\\$USER" -w)"
echo "ChatGPT can now reach $SIM through this Mac. Close this window to stop."
exec tunnel-client run --profile "$PROFILE"
SCM_START
chmod 700 "$START"
echo
echo "Done. To switch ChatGPT access on: double-click"
echo "   $START"
echo "Then in ChatGPT add the connector once (Add custom MCP server -> Connection: Tunnel)."
open "$DIR" 2>/dev/null || true
"""
