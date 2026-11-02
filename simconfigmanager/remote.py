"""
Remote mode: run the SimConfigManager app on another PC or a Mac and work on a sim PC over the LAN as if
sitting at it.

    Laptop app (RemoteRepo) ──HTTP POST /rpc + "full control" key──▶ sim PC app (NetworkServer → RpcExecutor)

Every operation the UI does goes through the store object; RemoteRepo forwards those calls to the sim, where
they run on one worker thread with the sim's own store. Only an explicit allow-list of methods can be called.
Everything is logged on the sim with the person's name ("— by Jan – MacBook").
"""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import core

# --------------------------------------------------------------------------- encoding

DATACLASSES = {c.__name__: c for c in (core.TrackedFile, core.Snapshot, core.RestorePoint, core.Version, core.Rule,
                                       core.ConfigVersion, core.VersionStatus, core.Alert, core.Change)}


def enc(o: Any) -> Any:
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(o)).decode("ascii")}
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return {"__dc__": type(o).__name__, "f": {f.name: enc(getattr(o, f.name)) for f in dataclasses.fields(o)}}
    if isinstance(o, tuple):
        return {"__t__": [enc(x) for x in o]}
    if isinstance(o, (list, set)):
        return [enc(x) for x in o]
    if isinstance(o, dict):
        if all(isinstance(k, str) for k in o):
            return {"__d__": {k: enc(v) for k, v in o.items()}}
        return {"__dk__": [[enc(k), enc(v)] for k, v in o.items()]}
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"can't send {type(o).__name__}")


def dec(o: Any) -> Any:
    if isinstance(o, list):
        return [dec(x) for x in o]
    if isinstance(o, dict):
        if "__b64__" in o:
            return base64.b64decode(o["__b64__"])
        if "__dc__" in o:
            cls = DATACLASSES[o["__dc__"]]
            return cls(**{k: dec(v) for k, v in o["f"].items()})
        if "__t__" in o:
            return tuple(dec(x) for x in o["__t__"])
        if "__d__" in o:
            return {k: dec(v) for k, v in o["__d__"].items()}
        if "__dk__" in o:
            return {dec(k): dec(v) for k, v in o["__dk__"]}
        return {k: dec(v) for k, v in o.items()}
    return o


# --------------------------------------------------------------------------- what may be called remotely

ALLOWED = {
    # settings, files, reading
    "get", "set", "get_machine", "files", "file", "find_file_by_path", "add_file", "add_folder", "update_file",
    "remove_file", "groups", "read_current", "write_file", "refresh_probes", "running_sim_processes",
    # history / snapshots / restore
    "snapshots", "snapshot", "latest_snapshot", "entries", "last_hash", "has_history", "snapshot_changes",
    "commit", "file_history", "file_status", "restore_points", "restore_point", "create_restore_point",
    "update_restore_point", "update_restore_point_snapshot", "delete_restore_point", "rp_hash", "preview_restore",
    "restore", "restore_version", "ensure_baseline", "get_object", "has_object", "put_object", "changelog",
    # versions
    "versions", "version", "latest_version", "version_status", "mismatches", "changes_between",
    "draft_release_notes", "create_version", "version_for_snapshot",
    # rules, ignores, rulebook, events
    "rules", "save_rule", "delete_rule", "ignores", "add_ignore", "delete_ignore", "rulebook", "save_rulebook_entry",
    "delete_rulebook_entry", "rulebook_status", "events", "clear_events", "log",
    # server-side helpers (core/remote_ops.py)
    "read_path", "write_path", "list_dir", "host_info", "drive_file_status", "save_drive_file",
    "export_archive_bytes", "import_archive_bytes", "expand_portable", "sync_configured", "sync_call",
    "list_proposals", "apply_proposal", "reject_proposal", "build_status", "discover", "build_hub_report",
    "hub_signature",
}


class RemoteError(Exception):
    pass


# --------------------------------------------------------------------------- server side

class RpcExecutor:
    """Runs remote calls one at a time on a dedicated thread with its own store connection."""

    def __init__(self, data_dir, probe_cache: Optional[dict] = None):
        self.data_dir = str(data_dir)
        self.probe_cache = probe_cache
        self.q: "queue.Queue" = queue.Queue()
        self.thread = threading.Thread(target=self._run, name="scm-rpc", daemon=True)
        self.thread.start()

    def _run(self):
        repo = core.Repo(self.data_dir)
        repo.background_probes = True
        if self.probe_cache is not None:
            repo._probe_cache = self.probe_cache       # share the app's system-info readings
        while True:
            job = self.q.get()
            if job is None:
                return
            method, args, kwargs, actor, box, ev = job
            try:
                repo.actor = actor
                if method == "get_machine":
                    box["result"] = repo.machine
                else:
                    box["result"] = getattr(repo, method)(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                box["error"] = f"{type(e).__name__}: {e}"
            finally:
                repo.actor = None
                ev.set()

    def call(self, method: str, args: list, kwargs: dict, actor: str, timeout: float = 600):
        if method not in ALLOWED:
            raise RemoteError(f"Not allowed: {method}")
        box: dict = {}
        ev = threading.Event()
        self.q.put((method, args, kwargs, actor, box, ev))
        if not ev.wait(timeout):
            raise RemoteError("The sim took too long to answer.")
        if "error" in box:
            raise RemoteError(box["error"])
        return box.get("result")

    def stop(self):
        self.q.put(None)


def handle_rpc(executor: RpcExecutor, body: bytes, actor: str) -> bytes:
    try:
        req = json.loads(body.decode("utf-8"))
        result = executor.call(req["method"], dec(req.get("args") or []), dec(req.get("kwargs") or {}), actor)
        return json.dumps({"result": enc(result)}).encode("utf-8")
    except RemoteError as e:
        return json.dumps({"error": str(e)}).encode("utf-8")
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8")


# --------------------------------------------------------------------------- client side

class RemoteRepo:
    """Looks like core.Repo to the app, but every call runs on the sim PC."""
    is_remote = True
    read_only = False
    data_dir = None
    background_probes = False

    def __init__(self, url: str, key: str, name: str = ""):
        url = url.strip().rstrip("/")
        for suffix in ("/mcp", "/rpc"):
            if url.endswith(suffix):
                url = url[: -len(suffix)]
        if "://" not in url:
            url = "http://" + url
        if url.count(":") < 2:
            url += f":{8765}"
        self.base = url
        self.key = key.strip()
        self.name = name
        self._machine: Optional[tuple[float, str]] = None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def __repr__(self):
        return f"<RemoteRepo {self.base}>"

    def call(self, method: str, *args, **kwargs):
        body = json.dumps({"method": method, "args": enc(list(args)), "kwargs": enc(kwargs)}).encode("utf-8")
        req = urllib.request.Request(self.base + "/rpc", data=body, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with self._opener.open(req, timeout=600) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", "")
            except ValueError:
                msg = ""
            if e.code == 401:
                raise RemoteError(msg or "The sim did not accept this access key.")
            if e.code == 403:
                raise RemoteError(msg or "This key does not have full control (remote app) access.")
            raise RemoteError(f"The sim answered with error {e.code}. {msg}")
        except (urllib.error.URLError, OSError) as e:
            raise RemoteError(f"Cannot reach the sim at {self.base} ({e}). Is it on, is SimConfigManager running "
                              "there with network access switched on, and are you on the same network?")
        if "error" in data:
            raise RemoteError(data["error"])
        return dec(data.get("result"))

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in ALLOWED:
            raise AttributeError(f"{name} is not available in remote mode")
        return lambda *a, **k: self.call(name, *a, **k)

    @property
    def machine(self) -> str:
        if self._machine is None or time.time() - self._machine[0] > 30:
            self._machine = (time.time(), self.call("get_machine"))
        return self._machine[1]

    def ping(self) -> dict:
        return self.call("host_info")


class _RemoteBackend:
    def __init__(self, repo: RemoteRepo):
        self.repo = repo

    def check(self) -> str:
        return self.repo.call("sync_call", "check")

    def describe(self) -> str:
        return self.repo.call("sync_call", "where")


class RemoteSync:
    """core.Sync of the sim, used from another computer. Archives travel as bytes."""

    def __init__(self, repo: RemoteRepo):
        self.repo = repo
        self.backend = _RemoteBackend(repo)

    @property
    def ok(self) -> bool:
        return self.repo.call("sync_call", "ok")

    @property
    def where(self) -> str:
        return self.repo.call("sync_call", "where")

    def __getattr__(self, name: str):
        if name in core.SYNC_REMOTE_METHODS:
            return lambda *a: self.repo.call("sync_call", name, *a)
        raise AttributeError(name)

    def upload_archive(self, zip_path: str) -> str:
        side = Path(zip_path + ".sha256")
        return self.repo.call("sync_call", "upload_archive_bytes", os.path.basename(zip_path),
                              Path(zip_path).read_bytes(), side.read_bytes() if side.exists() else None)

    def download_archive(self, name: str, dest_dir: str) -> str:
        data, side = self.repo.call("sync_call", "download_archive_bytes", name)
        dst = Path(dest_dir) / name
        dst.write_bytes(data)
        if side:
            Path(str(dst) + ".sha256").write_bytes(side)
        return str(dst)


# --------------------------------------------------------------------------- saved connections (client side)

def client_config_dir() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / core.APP_NAME
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA") or Path.home()) / core.APP_NAME
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / core.APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_connections() -> list[dict]:
    p = client_config_dir() / "connections.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_connections(conns: list[dict]) -> None:
    p = client_config_dir() / "connections.json"
    p.write_text(json.dumps(conns, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def connection_file(name: str, url: str, key: str) -> str:
    """Contents of a .scmconnect file (send privately; opens/imports in the app)."""
    return json.dumps({"app": core.APP_NAME, "type": "connection", "name": name, "url": url, "key": key}, indent=2)


def parse_connection_file(text: str) -> dict:
    d = json.loads(text)
    if d.get("type") != "connection" or not d.get("url") or not d.get("key"):
        raise ValueError("Not a SimConfigManager connection file.")
    return {"name": d.get("name") or d["url"], "url": d["url"], "key": d["key"]}
