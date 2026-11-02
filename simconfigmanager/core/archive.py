"""Portable paths, full-history manifests and the certification archive."""
from __future__ import annotations

import hashlib

import json
import os
import re
import socket
import zlib
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional


from .util import APP_NAME, APP_VERSION, fmt_ts, now_ts, read_file_bytes, sha256_bytes, write_file_atomic  # noqa: F401
from .models import Rule  # noqa: F401
from .probes import is_remote  # noqa: F401

if TYPE_CHECKING:
    from .repo import Repo

# --------------------------------------------------------------------------- portable paths

_PORTABLE_VARS = ("LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "USERPROFILE")


def to_portable(path: str) -> str:
    """C:\\Users\\Sim1\\AppData\\Roaming\\X  ->  %APPDATA%\\X  so it lands right on a PC with another user name."""
    norm = os.path.normcase(os.path.abspath(path))
    for var in _PORTABLE_VARS:
        base = os.environ.get(var)
        if not base:
            continue
        b = os.path.normcase(os.path.abspath(base))
        if norm == b or norm.startswith(b.rstrip("\\/") + os.sep):
            return f"%{var}%" + os.path.abspath(path)[len(os.path.abspath(base)):]
    return os.path.abspath(path)


def from_portable(path: str) -> str:
    def repl(m):
        return os.environ.get(m.group(1), m.group(0))
    return re.sub(r"%([A-Za-z_]+)%", repl, path)


# --------------------------------------------------------------------------- full-history manifest

def build_manifest(repo: "Repo") -> dict:
    return {
        "app": APP_NAME, "version": APP_VERSION, "machine": repo.machine, "updated": now_ts(),
        "files": [dict(vars(f), portable_path=to_portable(f.path)) for f in repo.files(include_inactive=True)],
        "snapshots": [dict(vars(s), entries={str(k): v for k, v in repo.entries(s.id).items()})
                      for s in reversed(repo.snapshots(10 ** 7))],
        "restore_points": [vars(rp) for rp in repo.restore_points()],
        "rules": [vars(r) for r in repo.rules()],
        "versions": [vars(v) for v in reversed(repo.versions())],
    }


def import_manifest(repo: "Repo", m: dict, fetch: Callable[[str], bytes], source: str,
                    path_map: Optional[dict] = None) -> dict:
    """Load files, snapshots, restore points and rules from a manifest into an EMPTY store.
    path_map: optional {original manifest file id (str): new path on this PC}."""
    if repo.snapshots(1):
        raise RuntimeError("Full-history import is only allowed into an empty store "
                           "(this PC already has history).")
    path_map = path_map or {}
    id_map = {}
    for f in m["files"]:
        target = path_map.get(str(f["id"])) or (from_portable(f["portable_path"]) if f.get("portable_path")
                                                 else f["path"])
        nf = repo.add_file(target, f.get("label", ""), f.get("group", ""))
        if not f.get("active", True):
            repo.remove_file(nf.id)
        id_map[str(f["id"])] = nf.id
    needed = {h for s in m["snapshots"] for h in s["entries"].values() if h}
    missing = 0
    for h in needed:
        if not repo.has_object(h):
            try:
                data = fetch(h)
                if sha256_bytes(data) != h:
                    missing += 1
                    continue
                repo.put_object(data)
            except FileNotFoundError:
                missing += 1
    snap_map = {}
    for s in m["snapshots"]:
        cur = repo.db.execute("INSERT INTO snapshots(created_at, message, kind, machine) VALUES(?,?,?,?)",
                              (s["created_at"], s["message"], s["kind"], s.get("machine", m.get("machine", ""))))
        snap_map[s["id"]] = cur.lastrowid
        repo.db.executemany("INSERT INTO entries(snapshot_id, file_id, hash, size) VALUES(?,?,?,NULL)",
                            [(cur.lastrowid, id_map[k], v) for k, v in s["entries"].items() if k in id_map])
    for rp in m.get("restore_points", []):
        if rp["snapshot_id"] in snap_map:
            repo.db.execute("INSERT OR REPLACE INTO restore_points(name, snapshot_id, description, created_at, "
                            "color) VALUES(?,?,?,?,?)", (rp["name"], snap_map[rp["snapshot_id"]],
                                                         rp.get("description", ""), rp["created_at"],
                                                         rp.get("color", "")))
    for v in m.get("versions", []):
        if v["snapshot_id"] in snap_map:
            repo.db.execute("INSERT OR IGNORE INTO versions(number, title, notes, snapshot_id, created_at, created_by, "
                            "machine) VALUES(?,?,?,?,?,?,?)",
                            (v["number"], v.get("title", ""), v.get("notes", ""), snap_map[v["snapshot_id"]],
                             v["created_at"], v.get("created_by", ""), v.get("machine", "")))
    for r in m.get("rules", []):
        fid = id_map.get(str(r["file_id"])) if r.get("file_id") is not None else None
        repo.save_rule(Rule(0, r["name"], fid, r["kind"], r.get("pattern", ""), r.get("expected", ""),
                            r.get("action", "notify"), r.get("enabled", True)))
    repo.db.commit()
    repo._stat_cache.clear()
    repo.log(f"Imported full history from {source}" + (f" ({missing} versions missing)" if missing else ""))
    return {"files": len(id_map), "snapshots": len(snap_map), "missing_versions": missing}


# --------------------------------------------------------------------------- certification archive

ARCHIVE_FORMAT = "SimConfigManager-archive/1"   # older archives say SimConfigKeeper-archive/1


def export_archive(repo: "Repo", zip_path: str, note: str = "", include_history: bool = True,
                   snapshot_id: Optional[int] = None) -> dict:
    """
    One self-contained zip of every monitored file:
        files/<group>/<name>         readable copies of the files
        archive.json                 machine-readable index (paths, SHA-256, sizes, dates)
        REPORT.txt                   human-readable configuration report for auditors
        CHECKSUMS.sha256             'sha256sum -c' compatible checksum list
        history/                     (optional) full version history, restore points and alert rules
    plus <zip>.sha256 next to the zip with the hash of the archive itself.
    """
    if is_remote(repo):   # built on the sim, saved here
        res = repo.call("export_archive_bytes", note, include_history, snapshot_id, os.path.basename(zip_path))
        zp = Path(zip_path)
        zp.write_bytes(res.pop("zip"))
        Path(str(zp) + ".sha256").write_text(f"{res['sha256']} *{zp.name}\n", encoding="utf-8")
        return dict(res, path=str(zp))
    import platform
    import zipfile
    if snapshot_id is None:
        repo.ensure_baseline()
        snap = repo.commit("Snapshot for archive export", kind="manual") or repo.latest_snapshot()
        snapshot_id = snap.id
    snap = repo.snapshot(snapshot_id)
    ents = repo.entries(snapshot_id)
    created = now_ts()
    files_info, checksums, used = [], [], set()
    zp = Path(zip_path)
    tmp = zp.with_name(zp.name + ".partial")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in repo.files():
            h = ents.get(f.id)
            info = {"id": f.id, "label": f.label, "group": f.group, "path": f.path,
                    "portable_path": to_portable(f.path), "sha256": h, "size": None, "modified": None,
                    "zip": None}
            if h is not None:
                data = repo.get_object(h)
                base = re.sub(r'[<>:"/\\|?*]+', "_", f.display).strip() or "file"
                grp = re.sub(r'[<>:"/\\|?*]+', "_", f.group).strip() or "Ungrouped"
                arc, n = f"files/{grp}/{base}", 2
                while arc.lower() in used:
                    stem, ext = os.path.splitext(base)
                    arc = f"files/{grp}/{stem} ({n}){ext}"
                    n += 1
                used.add(arc.lower())
                z.writestr(arc, data)
                info.update(size=len(data), zip=arc)
                try:
                    info["modified"] = os.path.getmtime(f.path) if sha256_bytes(read_file_bytes(f.path) or b"") == h \
                        else None
                except (OSError, TypeError):
                    pass
                checksums.append(f"{h} *{arc}")
            files_info.append(info)

        rps = repo.restore_points()
        ver, ver_diff = repo.version_for_snapshot(snapshot_id)
        if ver is None:
            ver_line = "none defined yet"
        elif not ver_diff:
            ver_line = f"{ver.label}  — all files MATCH this version"
        else:
            ver_line = f"{ver.label}  — {len(ver_diff)} file(s) DIFFER: {', '.join(ver_diff)}"
        index = {"format": ARCHIVE_FORMAT, "app": APP_NAME, "version": APP_VERSION, "machine": repo.machine,
                 "hostname": socket.gethostname(), "user": os.environ.get("USERNAME", ""),
                 "os": platform.platform(), "created": created, "note": note,
                 "snapshot": {"id": snap.id, "created_at": snap.created_at, "message": snap.message},
                 "include_history": include_history, "files": files_info,
                 "config_version": ({"number": ver.number, "title": ver.title, "created_at": ver.created_at,
                                     "created_by": ver.created_by, "differs": ver_diff} if ver else None),
                 "restore_points": [{"name": r.name, "description": r.description, "created_at": r.created_at}
                                    for r in rps]}
        z.writestr("archive.json", json.dumps(index, indent=2))

        ok = [x for x in files_info if x["sha256"]]
        miss = [x for x in files_info if not x["sha256"]]
        lines = [
            f"{APP_NAME} — CONFIGURATION ARCHIVE REPORT",
            "=" * 72,
            f"Simulator / PC     : {repo.machine}   (host {socket.gethostname()})",
            f"Created            : {fmt_ts(created)}",
            f"Created by         : {os.environ.get('USERNAME', '-')}",
            f"Operating system   : {platform.platform()}",
            f"Software           : {APP_NAME} {APP_VERSION}",
            f"State as of        : snapshot #{snap.id}, {fmt_ts(snap.created_at)}",
            f"Config version     : {ver_line}",
            f"Note               : {note or '-'}",
            f"Files archived     : {len(ok)}" + (f"   (+{len(miss)} missing on disk)" if miss else ""),
            f"Full history incl. : {'yes — ' + str(len(repo.snapshots(10 ** 7))) + ' snapshots' if include_history else 'no'}",
            "",
            "Integrity: every file below is listed with its SHA-256 checksum (also in CHECKSUMS.sha256).",
            "The checksum of this zip file itself is stored next to it in the matching .sha256 file.",
            "",
        ]
        for grp in sorted({x["group"] for x in files_info}):
            lines.append(f"[{grp or 'Ungrouped'}]")
            for x in [y for y in files_info if y["group"] == grp]:
                lines.append(f"  {x['label']}")
                lines.append(f"      path     : {x['path']}")
                if x["sha256"]:
                    mod = fmt_ts(x["modified"]) if x["modified"] else "-"
                    lines.append(f"      size     : {x['size']} bytes   last modified: {mod}")
                    lines.append(f"      sha256   : {x['sha256']}")
                else:
                    lines.append("      ** MISSING on disk at time of export **")
            lines.append("")
        vers = repo.versions()
        if vers:
            lines.append("Configuration versions:")
            for v in vers[:20]:
                lines.append(f"  - {v.label}  ({fmt_ts(v.created_at)}, by {v.created_by or '-'})")
            lines.append("")
        if rps:
            lines.append("Restore points:")
            for r in rps:
                lines.append(f"  - {r.name}  (saved {fmt_ts(r.created_at)})  {r.description}")
            lines.append("")
        lines.append("Signature: ______________________     Date: ____________")
        z.writestr("REPORT.txt", "\r\n".join(lines))
        z.writestr("CHECKSUMS.sha256", "\n".join(checksums) + "\n")

        if include_history:
            m = build_manifest(repo)
            z.writestr("history/manifest.json", json.dumps(m))
            for h in {h for s in m["snapshots"] for h in s["entries"].values() if h}:
                p = repo.object_path_raw(h)
                if p.exists():
                    z.write(p, f"history/objects/{h[:2]}/{h[2:]}", compress_type=zipfile.ZIP_STORED)
    os.replace(tmp, zp)
    zip_hash = hashlib.sha256(zp.read_bytes()).hexdigest()
    Path(str(zp) + ".sha256").write_text(f"{zip_hash} *{zp.name}\n", encoding="utf-8")
    repo.log(f"Archive exported: {zp} ({len(ok)} files, sha256 {zip_hash[:16]}…)")
    return {"files": len(ok), "missing": len(miss), "sha256": zip_hash, "path": str(zp)}


class Archive:
    """Read / verify / import an archive made by export_archive()."""

    def __init__(self, zip_path: str):
        import zipfile
        self.path = zip_path
        self.zip = zipfile.ZipFile(zip_path)
        names = set(self.zip.namelist())
        if "archive.json" not in names:
            raise ValueError("This zip is not a SimConfigManager archive (no archive.json).")
        self.index = json.loads(self.zip.read("archive.json"))
        self.has_history = "history/manifest.json" in names

    def close(self):
        self.zip.close()

    @property
    def files(self) -> list[dict]:
        return self.index["files"]

    def read(self, entry: dict) -> Optional[bytes]:
        return self.zip.read(entry["zip"]) if entry.get("zip") else None

    def verify(self) -> dict:
        """Check every file against its recorded SHA-256, and the zip against its .sha256 sidecar."""
        bad, ok = [], 0
        for e in self.files:
            if not e.get("zip"):
                continue
            try:
                if sha256_bytes(self.read(e)) == e["sha256"]:
                    ok += 1
                else:
                    bad.append(e["label"])
            except Exception:  # noqa: BLE001
                bad.append(e["label"])
        sidecar = Path(self.path + ".sha256")
        zip_check = "no .sha256 file next to the zip"
        if sidecar.exists():
            want = sidecar.read_text(encoding="utf-8").split()[0].lower()
            have = hashlib.sha256(Path(self.path).read_bytes()).hexdigest()
            zip_check = "matches" if want == have else "DOES NOT MATCH — the zip was changed or damaged"
        return {"ok": ok, "bad": bad, "zip_check": zip_check}

    def suggested_target(self, entry: dict) -> str:
        return from_portable(entry.get("portable_path") or entry["path"])

    def _fetch(self, h: str) -> bytes:
        try:
            return zlib.decompress(self.zip.read(f"history/objects/{h[:2]}/{h[2:]}"))
        except KeyError:
            raise FileNotFoundError(h)

    def apply(self, repo: "Repo", targets: dict, write_files: bool = True, track: bool = True,
              import_history: bool = False) -> list[tuple[str, str]]:
        """targets: {archive file id (int): target path}. Returns [(label, result)]."""
        if is_remote(repo):   # the files are written on the sim
            return repo.call("import_archive_bytes", Path(self.path).read_bytes(), os.path.basename(self.path),
                             targets, write_files, track, import_history)
        report = []
        if import_history:
            if not self.has_history:
                raise ValueError("This archive was exported without history.")
            m = json.loads(self.zip.read("history/manifest.json"))
            import_manifest(repo, m, self._fetch, f"archive {os.path.basename(self.path)}",
                            {str(k): v for k, v in targets.items()})
        elif repo.files():
            repo.commit(f"Safety copy before importing {os.path.basename(self.path)}", kind="safety")
        for e in self.files:
            target = targets.get(e["id"])
            if not target:
                continue
            if write_files and e.get("zip"):
                data = self.read(e)
                if sha256_bytes(data) != e["sha256"]:
                    report.append((e["label"], "SKIPPED: checksum mismatch in archive"))
                    continue
                try:
                    if read_file_bytes(target) == data:
                        report.append((e["label"], "already identical"))
                    else:
                        write_file_atomic(target, data)
                        report.append((e["label"], "written"))
                except Exception as ex:  # noqa: BLE001
                    report.append((e["label"], f"FAILED: {ex}"))
                    continue
            if track or import_history:
                f = repo.add_file(target, e["label"], e["group"])
                if not write_files:
                    report.append((e["label"], "tracked"))
                repo._stat_cache.pop(f.id, None)
        repo.ensure_baseline()
        repo.commit(f"Imported archive {os.path.basename(self.path)} "
                    f"(from {self.index.get('machine')}, {fmt_ts(self.index.get('created', 0))})", kind="restore")
        repo.log(f"Imported archive {self.path}: "
                 f"{sum(1 for _, r in report if r == 'written')} file(s) written")
        return report


def safe_name(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s) or "machine"

__all__ = ['_PORTABLE_VARS', 'to_portable', 'from_portable', 'build_manifest', 'import_manifest', 'ARCHIVE_FORMAT', 'export_archive', 'Archive', 'safe_name']
