"""Sharing between sims: shared folder or Google Drive, fleet status."""
from __future__ import annotations


import json
import os
import re
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Optional


from .util import APP_VERSION, decode_text, now_ts  # noqa: F401
from .archive import build_manifest, import_manifest, safe_name  # noqa: F401
from .probes import is_probe, is_remote  # noqa: F401
from .proposals import list_proposals  # noqa: F401
from .rulebook_ops import rulebook_status  # noqa: F401

if TYPE_CHECKING:
    from .repo import Repo

# --------------------------------------------------------------------------- cloud / network-folder sync

class Sync:
    """
    Shares this PC's history with the other sims and keeps an off-site backup, through a storage backend:
    a folder (Google Drive for Desktop, network share …) or Google Drive directly (see storage.py).

    Each PC only ever writes its own files (machines/<name>.json, status/<name>.json); objects are immutable
    and content-addressed, so two sims syncing at the same time never conflict.
    """

    def __init__(self, repo: Repo, backend):
        self.repo = repo
        self.backend = backend

    @property
    def ok(self) -> bool:
        return self.backend.ok()

    @property
    def where(self) -> str:
        return self.backend.describe()

    @staticmethod
    def _okey(h: str) -> str:
        return f"objects/{h[:2]}/{h[2:]}"

    def push(self) -> dict:
        if not self.ok:
            raise FileNotFoundError(f"Sync location not reachable: {self.where}")
        repo = self.repo
        have = self.backend.object_names()
        copied = 0
        hashes = {r["hash"] for r in repo.db.execute("SELECT DISTINCT hash FROM entries WHERE hash IS NOT NULL")}
        for h in sorted(hashes - have):
            src = repo.object_path_raw(h)
            if src.exists():
                self.backend.write(self._okey(h), src.read_bytes())
                copied += 1
        manifest = build_manifest(repo)
        self.backend.write(f"machines/{safe_name(repo.machine)}.json", json.dumps(manifest).encode("utf-8"))
        self.push_status()
        if not repo.read_only:
            repo.set("last_push", now_ts())
        return {"objects_copied": copied, "snapshots": len(manifest["snapshots"])}

    def push_status(self, extra: Optional[dict] = None) -> dict:
        st = build_status(self.repo)
        if extra:
            st.update(extra)
        self.backend.write(f"status/{safe_name(self.repo.machine)}.json", json.dumps(st).encode("utf-8"))
        return st

    def statuses(self) -> list[dict]:
        out = []
        for name in self.backend.list("status"):
            if not name.endswith(".json"):
                continue
            try:
                out.append(json.loads(self.backend.read(f"status/{name}")))
            except (FileNotFoundError, ValueError):
                continue
        return out

    def machines(self) -> list[dict]:
        out = []
        for name in self.backend.list("machines"):
            if not name.endswith(".json"):
                continue
            try:
                m = json.loads(self.backend.read(f"machines/{name}"))
                out.append({"machine": m.get("machine", name[:-5]), "updated": m.get("updated", 0),
                            "files": len(m.get("files", [])), "snapshots": len(m.get("snapshots", [])),
                            "file": name})
            except (FileNotFoundError, ValueError):
                continue
        return out

    def manifest(self, machine: str) -> dict:
        return json.loads(self.backend.read(f"machines/{safe_name(machine)}.json"))

    def fetch(self, h: str) -> bytes:
        try:
            return zlib.decompress(self.backend.read(self._okey(h)))
        except FileNotFoundError:
            raise FileNotFoundError(f"Version {h[:12]} is not (yet) in the sync location — still uploading?")

    def remote_latest(self, machine: str) -> list[dict]:
        """Latest version of every file on another machine."""
        m = self.manifest(machine)
        files = {str(f["id"]): f for f in m["files"]}
        latest: dict[str, Optional[str]] = {}
        for s in m["snapshots"]:
            latest.update(s["entries"])
        return [dict(files[fid], hash=h) for fid, h in latest.items() if fid in files]

    def import_machine(self, machine: str) -> dict:
        """Rebuild this PC's history from the shared backup (disaster recovery / new PC)."""
        return import_manifest(self.repo, self.manifest(machine), self.fetch, f"shared backup '{machine}'")

    def upload_archive(self, zip_path: str) -> str:
        """Put a certification archive (and its .sha256) in archives/ of the sync location."""
        name = os.path.basename(zip_path)
        self.backend.write(f"archives/{name}", Path(zip_path).read_bytes())
        side = Path(zip_path + ".sha256")
        if side.exists():
            self.backend.write(f"archives/{name}.sha256", side.read_bytes())
        return name

    def list_archives(self) -> list[str]:
        return [n for n in self.backend.list("archives") if n.endswith(".zip")]

    def download_archive(self, name: str, dest_dir: str) -> str:
        dst = Path(dest_dir) / name
        dst.write_bytes(self.backend.read(f"archives/{name}"))
        try:
            Path(str(dst) + ".sha256").write_bytes(self.backend.read(f"archives/{name}.sha256"))
        except FileNotFoundError:
            pass
        return str(dst)


class FolderSync(Sync):
    """Sync through a plain folder (kept for compatibility)."""

    def __init__(self, repo: Repo, folder: str):
        from ..storage import LocalBackend
        super().__init__(repo, LocalBackend(folder))
        self.folder = Path(folder)


def drive_files(repo: Repo) -> dict:
    d = repo.data_dir
    return {"service_account": d / "gdrive_service_account.json", "client_secrets": d / "gdrive_client.json",
            "token": d / "gdrive_token.json"}


_TOKEN_PROVIDERS: dict = {}


def parse_drive_folder(text: str) -> str:
    """Accept a Drive folder link or a bare ID."""
    text = text.strip()
    m = re.search(r"/folders/([A-Za-z0-9_-]+)", text) or re.search(r"[?&]id=([A-Za-z0-9_-]+)", text)
    return m.group(1) if m else text


def make_sync(repo: Repo) -> Optional[Sync]:
    """The configured sync location, or None. Settings: sync_mode = folder | gdrive."""
    if is_remote(repo):
        from ..remote import RemoteSync
        return RemoteSync(repo) if repo.call("sync_configured") else None
    mode = repo.get("sync_mode") or ("folder" if repo.get("sync_folder") else "")
    if mode == "folder" and repo.get("sync_folder"):
        return FolderSync(repo, repo.get("sync_folder"))
    if mode == "gdrive":
        from .. import storage
        files = drive_files(repo)
        auth = repo.get("gdrive_auth") or "service_account"
        root = repo.get("gdrive_folder_id")
        if not root:
            return None
        key_file = files["service_account"] if auth == "service_account" else files["token"]
        if not key_file.exists():
            return None
        ck = (auth, str(key_file), key_file.stat().st_mtime_ns)
        prov = _TOKEN_PROVIDERS.get(ck)
        if prov is None:
            prov = (storage.service_account_token_provider(str(key_file)) if auth == "service_account"
                    else storage.user_token_provider(str(key_file)))
            _TOKEN_PROVIDERS[ck] = prov
        return Sync(repo, storage.DriveBackend(prov, root, label="Google Drive"))
    return None


def build_status(repo: Repo) -> dict:
    """Small heartbeat for the fleet overview (written every few minutes)."""
    if is_remote(repo):
        return repo.call("build_status")
    ver, status = repo.version_status()
    mism = [x for x in status if x.status in ("modified", "missing", "new")] if ver else []
    day = now_ts() - 86400
    alerts = [e for e in repo.events(500) if e["level"] in ("alert", "warning") and e["ts"] > day]
    last = repo.latest_snapshot()
    probes = {}
    for f in repo.files():
        if is_probe(f) and f.path in ("system://gpu", "system://windows"):
            data = repo.read_current(f)
            if data:
                probes[f.path[9:]] = decode_text(data).strip()[:600]
    try:
        book = rulebook_status(repo)
    except Exception:  # noqa: BLE001
        book = []
    return {
        "machine": repo.machine, "app_version": APP_VERSION, "heartbeat": now_ts(),
        "files": len(repo.files()),
        "version": ({"number": ver.number, "title": ver.title, "created_at": ver.created_at} if ver else None),
        "mismatches": [{"file": x.file.display, "group": x.file.group, "status": x.status} for x in mism],
        "alerts_24h": len(alerts),
        "recent_alerts": [{"ts": e["ts"], "message": e["message"]} for e in alerts[:5]],
        "last_change": last.created_at if last else None,
        "pending_proposals": len(list_proposals(repo, "pending")) if (repo.data_dir / "proposals").is_dir() else 0,
        "rulebook_violations": [b["label"] for b in book if b["ok"] is False],
        "system": probes,
        **readiness_extras(repo),
    }


def readiness_extras(repo) -> dict:
    """Cockpit hardware, health, maintenance, integrity and the last session — for the fleet and the Hub."""
    out: dict = {}
    for key, fn in (("hardware_missing", lambda: repo.hardware_status().get("missing", [])),
                    ("health_warnings", lambda: repo.health_status().get("warnings", [])),
                    ("maintenance_overdue", lambda: [t["name"] for t in repo.maintenance_tasks()
                                                     if t["state"] == "overdue"]),
                    ("maintenance_soon", lambda: [t["name"] for t in repo.maintenance_tasks() if t["state"] == "soon"]),
                    ("integrity_problems", lambda: [m["name"] for m in repo.manifests()
                                                    if m["last_check"] and not m["last_check"]["ok"]]),
                    ("sim_hours", lambda: repo.sim_hours())):
        try:
            out[key] = fn()
        except Exception:  # noqa: BLE001 - a feature that isn't set up must never break the heartbeat
            out[key] = [] if key != "sim_hours" else 0
    try:
        last = next((x for x in repo.sessions(5, "session")), None)
        if last:
            out["last_session"] = {"started": last["started"], "ended": last["ended"],
                                   "version": last["version_number"], "matches": bool(last["version_matches"]),
                                   "avg_fps": (last["stats"] or {}).get("avg_fps"),
                                   "low1_fps": (last["stats"] or {}).get("low1_fps"),
                                   "aircraft": last["aircraft"], "crashed": bool(last["crash"])}
    except Exception:  # noqa: BLE001
        pass
    return out

__all__ = ['Sync', 'FolderSync', 'drive_files', '_TOKEN_PROVIDERS', 'parse_drive_folder', 'make_sync', 'build_status', 'readiness_extras']
