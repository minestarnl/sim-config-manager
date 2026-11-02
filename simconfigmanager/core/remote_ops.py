"""Server-side operations used by the remote app (another computer on the LAN)."""
from __future__ import annotations


import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Optional


from .util import APP_VERSION, read_file_bytes, write_file_atomic  # noqa: F401
from .archive import Archive, export_archive, from_portable  # noqa: F401
from .repo import Repo  # noqa: F401
from .proposals import apply_proposal, list_proposals, reject_proposal  # noqa: F401
from .sync import build_status, drive_files, make_sync  # noqa: F401
from .system import discover_sim_configs, is_admin  # noqa: F401

# =========================================================================== server-side operations for remote mode
# Everything the app does that touches the sim PC's own disk/OS outside the store goes through these
# methods, so the same UI works locally and from another computer (see remote.py).

SAFE_EXTENSIONS = (".cfg", ".xml", ".ini", ".json", ".txt", ".fxml", ".dsd", ".ccp", ".inf", ".config", ".csv",
                   ".log", ".yaml", ".yml", ".dat")


def _path_allowed(repo, path: str) -> bool:
    if repo.find_file_by_path(path):
        return True
    return os.path.splitext(path)[1].lower() in SAFE_EXTENSIONS


def _read_path(self, path: str) -> Optional[bytes]:
    if not _path_allowed(self, path):
        raise PermissionError("Only config-type files can be read remotely.")
    try:
        return read_file_bytes(path)
    except PermissionError:
        return None


def _write_path(self, path: str, data: bytes) -> None:
    if not _path_allowed(self, path):
        raise PermissionError("Only config-type files can be written remotely.")
    f = self.find_file_by_path(path)
    if f and f.active:
        self.write_file(f, data, "written remotely")
    else:
        write_file_atomic(path, data)
        self.log(f"Wrote {path}")


def _list_dir(self, path: str = "") -> dict:
    """Folder browser for the remote app (names and sizes only)."""
    if not path:
        if sys.platform == "win32":
            drives = [f"{c}:\\" for c in "CDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{c}:\\")]
            return {"path": "", "parent": None, "dirs": drives, "files": [],
                    "shortcuts": {k: os.environ.get(k, "") for k in ("APPDATA", "PROGRAMDATA", "USERPROFILE",
                                                                      "LOCALAPPDATA") if os.environ.get(k)}}
        path = str(Path.home())
    p = Path(path)
    if not p.is_dir():
        raise FileNotFoundError(f"Not a folder: {path}")
    dirs, files = [], []
    try:
        for e in sorted(os.scandir(p), key=lambda e: e.name.lower()):
            try:
                if e.is_dir():
                    dirs.append(e.name)
                elif e.is_file():
                    files.append((e.name, e.stat().st_size))
            except OSError:
                continue
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else ""
    return {"path": str(p), "parent": parent, "dirs": dirs, "files": files, "sep": os.sep,
            "shortcuts": {k: os.environ.get(k, "") for k in ("APPDATA", "PROGRAMDATA", "USERPROFILE", "LOCALAPPDATA")
                          if os.environ.get(k)}}


def _host_info(self) -> dict:
    import platform
    return {"hostname": socket.gethostname(), "os": platform.platform(), "platform": sys.platform,
            "user": os.environ.get("USERNAME") or os.environ.get("USER", ""), "app_version": APP_VERSION,
            "admin": is_admin(), "machine": self.machine}


def _drive_file_status(self) -> dict:
    from .. import storage
    f = drive_files(self)
    return {"service_account": storage.service_account_email(str(f["service_account"]))
            if f["service_account"].exists() else "",
            "client_secrets": f["client_secrets"].exists(), "token": f["token"].exists()}


def _save_drive_file(self, kind: str, text: str) -> None:
    if kind not in ("service_account", "client_secrets"):
        raise ValueError("unknown file")
    drive_files(self)[kind].write_text(text, encoding="utf-8")
    self.log(f"Google Drive {kind.replace('_', ' ')} file updated")


def _export_archive_bytes(self, note: str = "", include_history: bool = True, snapshot_id: Optional[int] = None,
                          name: str = "archive.zip") -> dict:
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="scm-export-"))
    try:
        res = export_archive(self, str(d / (name or "archive.zip")), note, include_history, snapshot_id)
        return {"zip": Path(res["path"]).read_bytes(), "sha256": res["sha256"], "files": res["files"],
                "missing": res["missing"]}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _import_archive_bytes(self, data: bytes, name: str, targets: dict, write_files: bool = True, track: bool = True,
                          import_history: bool = False):
    import tempfile
    d = Path(tempfile.mkdtemp(prefix="scm-import-"))
    try:
        zp = d / (os.path.basename(name) or "archive.zip")
        zp.write_bytes(data)
        arc = Archive(str(zp))
        try:
            return arc.apply(self, {int(k): v for k, v in targets.items()}, write_files, track, import_history)
        finally:
            arc.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _expand_portable(self, paths: list) -> list:
    return [from_portable(p) for p in paths]


SYNC_REMOTE_METHODS = {"push", "push_status", "statuses", "machines", "manifest", "fetch", "remote_latest",
                       "import_machine", "list_archives"}


def _sync_configured(self) -> bool:
    try:
        return make_sync(self) is not None
    except Exception:  # noqa: BLE001
        return False


def _sync_call(self, name: str, *args):
    s = make_sync(self)
    if s is None:
        raise ValueError("No sharing location is set up on the sim.")
    if name == "ok":
        return s.ok
    if name == "where":
        return s.where
    if name == "check":
        if hasattr(s.backend, "check"):
            return s.backend.check()
        if not s.ok:
            raise FileNotFoundError(f"Folder not reachable: {s.where}")
        return s.where
    if name == "upload_archive_bytes":
        import tempfile
        fname, data, side = args
        d = Path(tempfile.mkdtemp(prefix="scm-up-"))
        try:
            zp = d / os.path.basename(fname)
            zp.write_bytes(data)
            if side:
                Path(str(zp) + ".sha256").write_bytes(side)
            return s.upload_archive(str(zp))
        finally:
            shutil.rmtree(d, ignore_errors=True)
    if name == "download_archive_bytes":
        import tempfile
        d = Path(tempfile.mkdtemp(prefix="scm-down-"))
        try:
            p = Path(s.download_archive(args[0], str(d)))
            side = Path(str(p) + ".sha256")
            return p.read_bytes(), (side.read_bytes() if side.exists() else None)
        finally:
            shutil.rmtree(d, ignore_errors=True)
    if name in SYNC_REMOTE_METHODS:
        return getattr(s, name)(*args)
    raise ValueError(f"Not allowed: {name}")


for _name, _fn in (("read_path", _read_path), ("write_path", _write_path), ("list_dir", _list_dir),
                   ("host_info", _host_info), ("drive_file_status", _drive_file_status),
                   ("save_drive_file", _save_drive_file), ("export_archive_bytes", _export_archive_bytes),
                   ("import_archive_bytes", _import_archive_bytes), ("expand_portable", _expand_portable),
                   ("sync_configured", _sync_configured), ("sync_call", _sync_call),
                   ("list_proposals", list_proposals), ("apply_proposal", apply_proposal),
                   ("reject_proposal", reject_proposal), ("build_status", build_status),
                   ("discover", lambda self: discover_sim_configs())):
    setattr(Repo, _name, _fn)

__all__ = ['SAFE_EXTENSIONS', '_path_allowed', '_read_path', '_write_path', '_list_dir', '_host_info', '_drive_file_status', '_save_drive_file', '_export_archive_bytes', '_import_archive_bytes', '_expand_portable', 'SYNC_REMOTE_METHODS', '_sync_configured', '_sync_call']
