"""
Install recordings (feature #7) and integrity manifests (feature #8). Both scan whole folders.

* **Record an install:** before running an add-on installer, start a recording on the folders it may touch
  (Prepar3D, ProSim, ProgramData, Documents add-ons …). Config-like files are copied into the store first.
  When the installer is done, stop: the app lists every file added, changed and removed (the *change package*),
  can create a version for it, and can roll it back — added files are removed and changed/removed config files
  are put back (large binaries that weren't copied are listed so they can be reinstalled).
* **Integrity manifests:** a checksum of every file in an installation (Prepar3D, ProSim, aircraft folders).
  Verifying later shows exactly which files were added, removed or changed — e.g. after a crash, a Windows
  update or an add-on uninstaller.
"""
from __future__ import annotations

import hashlib
import json
import os
import zlib
from pathlib import Path
from typing import Callable, Optional

from .repo import repo_method, rows, schema
from .util import now_ts, write_file_atomic

schema("""
CREATE TABLE IF NOT EXISTS installs (
    id INTEGER PRIMARY KEY, name TEXT, started REAL, ended REAL, roots TEXT, before_obj TEXT, after_obj TEXT,
    summary TEXT DEFAULT '{}', status TEXT DEFAULT 'recording', version_number INTEGER, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS manifests (
    id INTEGER PRIMARY KEY, name TEXT, created REAL, roots TEXT, obj TEXT, files INTEGER, bytes INTEGER,
    version_number INTEGER, last_check TEXT DEFAULT '');
""")

CONFIG_EXT = {".cfg", ".ini", ".xml", ".json", ".txt", ".fxml", ".dsd", ".ccp", ".inf", ".config", ".csv", ".dll.xml",
              ".exe.xml", ".lua", ".yaml", ".yml", ".flt", ".wx", ".pln"}
KEEP_MAX = 2 * 1024 * 1024           # config files up to this size are copied into the store before an install
FULL_HASH_MAX = 64 * 1024 * 1024     # bigger files: size + first and last MiB ("sampled")
SKIP_DIRS = {"$recycle.bin", "system volume information", "__pycache__", ".git", "shadercache", "shaders"}


def _hash_file(path: str, size: int) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        if size <= FULL_HASH_MAX:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
            return h.hexdigest()
        h.update(fh.read(1 << 20))
        fh.seek(max(0, size - (1 << 20)))
        h.update(fh.read(1 << 20))
        h.update(str(size).encode())
    return "s:" + h.hexdigest()


def scan_tree(roots: list[str], hashes: bool = True, keep: Optional[Callable[[str, int], None]] = None,
              previous: Optional[dict] = None) -> dict:
    """{path: [size, mtime, sha256 or None]} for every file under the roots. With ``previous``, files whose size
    and mtime didn't change keep their old hash (fast re-scan)."""
    out: dict = {}
    for root in roots:
        if not os.path.exists(root):
            continue
        if os.path.isfile(root):
            walk = [(os.path.dirname(root), [], [os.path.basename(root)])]
        else:
            walk = os.walk(root)
        for d, dirs, files in walk:
            dirs[:] = [x for x in dirs if x.lower() not in SKIP_DIRS]
            for n in files:
                p = os.path.join(d, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                size, mtime = st.st_size, int(st.st_mtime)
                h = None
                old = (previous or {}).get(p)
                if old and old[0] == size and old[1] == mtime and old[2]:
                    h = old[2]
                elif hashes:
                    try:
                        h = _hash_file(p, size)
                    except OSError:
                        h = None
                if keep is not None and size <= KEEP_MAX and Path(p).suffix.lower() in CONFIG_EXT:
                    keep(p, size)
                out[p] = [size, mtime, h]
    return out


def diff_trees(before: dict, after: dict) -> dict:
    added = sorted(p for p in after if p not in before)
    removed = sorted(p for p in before if p not in after)
    changed = sorted(p for p in after if p in before and
                     (after[p][0] != before[p][0] or (after[p][2] and before[p][2] and after[p][2] != before[p][2])
                      or (not after[p][2] and after[p][1] != before[p][1])))
    return {"added": added, "removed": removed, "changed": changed}


def _store_json(repo, data) -> str:
    return repo.put_object(zlib.compress(json.dumps(data).encode("utf-8"), 6))


def _load_json(repo, h: str):
    return json.loads(zlib.decompress(repo.get_object(h)).decode("utf-8"))


# --------------------------------------------------------------------------- install recordings

def _install(r) -> dict:
    d = dict(r)
    d["roots"] = json.loads(d.get("roots") or "[]")
    d["summary"] = json.loads(d.get("summary") or "{}")
    return d


@repo_method
def installs(repo) -> list[dict]:
    return [_install(r) for r in rows(repo, "SELECT * FROM installs ORDER BY started DESC")]


@repo_method
def install_record(repo, install_id: int) -> Optional[dict]:
    r = rows(repo, "SELECT * FROM installs WHERE id=?", (install_id,))
    return _install(r[0]) if r else None


@repo_method
def default_install_roots(repo) -> list[str]:
    """Suggested folders: parents of the tracked files plus the usual sim folders that exist."""
    cands = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    docs = os.path.join(os.path.expanduser("~"), "Documents")
    for v in ("v6", "v5"):
        cands += [os.path.join(pf, "Lockheed Martin", f"Prepar3D {v}"), os.path.join(pd, "Lockheed Martin",
                  f"Prepar3D {v}"), os.path.join(docs, f"Prepar3D {v} Add-ons")]
        app = os.environ.get("APPDATA")
        if app:
            cands.append(os.path.join(app, "Lockheed Martin", f"Prepar3D {v}"))
    cands += [r"C:\ProSim737", r"C:\ProSimA320", r"C:\ProSim-AR"]
    return [c for c in cands if os.path.isdir(c)]


@repo_method
def start_install(repo, name: str, roots: list[str]) -> dict:
    if any(i["status"] == "recording" for i in repo.installs()):
        raise RuntimeError("An install is already being recorded. Stop it first.")
    roots = [r for r in roots if r and os.path.exists(r)]
    if not roots:
        raise FileNotFoundError("None of the chosen folders exist.")
    kept: dict = {}

    def keep(p, _size):
        data = Path(p).read_bytes()
        kept[p] = repo.put_object(data)
    before = scan_tree(roots, hashes=True, keep=keep)
    for p, h in kept.items():           # the copy's hash is the file's hash (same algorithm for small files)
        before[p][2] = h
    repo.commit(f"Before install: {name}", kind="safety")
    cur = repo.db.execute("INSERT INTO installs(name, started, roots, before_obj) VALUES(?,?,?,?)",
                          (name, now_ts(), json.dumps(roots), _store_json(repo, before)))
    repo.db.commit()
    repo.log(f"Recording install '{name}' ({len(before):,} files in {len(roots)} folder(s))")
    return repo.install_record(cur.lastrowid)


@repo_method
def finish_install(repo, install_id: int, create_version: bool = False, author: str = "") -> dict:
    rec = repo.install_record(install_id)
    if not rec or rec["status"] != "recording":
        raise RuntimeError("This install is not being recorded.")
    before = _load_json(repo, rec["before_obj"])
    after = scan_tree(rec["roots"], hashes=True, previous=before)
    d = diff_trees(before, after)
    summary = {k: [{"path": p, "size": (after.get(p) or before.get(p))[0]} for p in v] for k, v in d.items()}
    summary["counts"] = {k: len(v) for k, v in d.items()}
    summary["bytes_added"] = sum(after[p][0] for p in d["added"])
    tracked = {f.path.lower() for f in repo.files()}
    summary["new_config_files"] = [p for p in d["added"] if Path(p).suffix.lower() in CONFIG_EXT
                                   and p.lower() not in tracked and os.path.getsize(p) <= KEEP_MAX][:200]
    repo.scan()  # make sure tracked files changed by the installer are recorded
    snap = repo.commit(f"After install: {rec['name']}", kind="manual")
    vnum = None
    if create_version:
        v = repo.create_version(f"Installed {rec['name']}",
                                f"Installed {rec['name']}: {len(d['added'])} file(s) added, {len(d['changed'])} "
                                f"changed, {len(d['removed'])} removed.", author or repo.get("version_author"))
        vnum = v.number
    repo.db.execute("UPDATE installs SET ended=?, after_obj=?, summary=?, status='done', version_number=? "
                    "WHERE id=?", (now_ts(), _store_json(repo, after), json.dumps(summary), vnum, install_id))
    repo.db.commit()
    c = summary["counts"]
    repo.log(f"Install '{rec['name']}' recorded: {c['added']} added, {c['changed']} changed, {c['removed']} removed"
             + (f" → v{vnum}" if vnum else ""))
    _ = snap
    return repo.install_record(install_id)


@repo_method
def cancel_install(repo, install_id: int) -> None:
    repo.db.execute("DELETE FROM installs WHERE id=? AND status='recording'", (install_id,))
    repo.db.commit()


@repo_method
def rollback_preview(repo, install_id: int) -> dict:
    rec = repo.install_record(install_id)
    before, after = _load_json(repo, rec["before_obj"]), _load_json(repo, rec["after_obj"])
    d = diff_trees(before, after)
    can_restore = [p for p in d["changed"] + d["removed"] if before[p][2] and repo.has_object(before[p][2])]
    cannot = [p for p in d["changed"] + d["removed"] if p not in can_restore]
    return {"remove": d["added"], "restore": can_restore, "cannot_restore": cannot}


@repo_method
def rollback_install(repo, install_id: int) -> dict:
    """Undo an install: remove added files that are still as installed, put back changed/removed config files."""
    rec = repo.install_record(install_id)
    if not rec or rec["status"] != "done":
        raise RuntimeError("Only a finished install can be rolled back.")
    before, after = _load_json(repo, rec["before_obj"]), _load_json(repo, rec["after_obj"])
    d = diff_trees(before, after)
    repo.commit(f"Before rolling back install: {rec['name']}", kind="safety")
    report = {"removed": [], "restored": [], "skipped": [], "not_restorable": []}
    for p in d["added"]:
        try:
            now = scan_tree([p]).get(p)
            if now and after[p][2] and now[2] != after[p][2]:
                report["skipped"].append(f"{p} (changed after the install)")
                continue
            os.remove(p)
            report["removed"].append(p)
        except FileNotFoundError:
            pass
        except OSError as e:
            report["skipped"].append(f"{p} ({e})")
    for p in d["changed"] + d["removed"]:
        h = before[p][2]
        if h and repo.has_object(h):
            try:
                write_file_atomic(p, repo.get_object(h))
                report["restored"].append(p)
            except OSError as e:
                report["skipped"].append(f"{p} ({e})")
        else:
            report["not_restorable"].append(p)
    for root in sorted({os.path.dirname(p) for p in report["removed"]}, key=len, reverse=True):
        try:
            while root and not any(root.lower() == r.lower() for r in rec["roots"]) and not os.listdir(root):
                os.rmdir(root)
                root = os.path.dirname(root)
        except OSError:
            pass
    repo.scan()
    repo.commit(f"Rolled back install: {rec['name']}", kind="restore")
    repo.db.execute("UPDATE installs SET status='rolled_back' WHERE id=?", (install_id,))
    repo.db.commit()
    repo.log(f"Install '{rec['name']}' rolled back: {len(report['removed'])} removed, {len(report['restored'])} "
             f"restored" + (f", {len(report['not_restorable'])} could not be restored (large files — reinstall "
                            "them)" if report["not_restorable"] else ""),
             level="warning" if report["not_restorable"] or report["skipped"] else "info")
    return report


# --------------------------------------------------------------------------- integrity manifests

def _manifest(r) -> dict:
    d = dict(r)
    d["roots"] = json.loads(d.get("roots") or "[]")
    d["last_check"] = json.loads(d["last_check"]) if d.get("last_check") else None
    return d


@repo_method
def manifests(repo) -> list[dict]:
    return [_manifest(r) for r in rows(repo, "SELECT * FROM manifests ORDER BY created DESC")]


@repo_method
def create_manifest(repo, name: str, roots: list[str], link_version: bool = True) -> dict:
    roots = [r for r in roots if r and os.path.exists(r)]
    if not roots:
        raise FileNotFoundError("None of the chosen folders exist.")
    tree = scan_tree(roots, hashes=True)
    ver = repo.latest_version() if link_version else None
    cur = repo.db.execute("INSERT INTO manifests(name, created, roots, obj, files, bytes, version_number) "
                          "VALUES(?,?,?,?,?,?,?)", (name, now_ts(), json.dumps(roots), _store_json(repo, tree),
                                                    len(tree), sum(v[0] for v in tree.values()),
                                                    ver.number if ver else None))
    repo.db.commit()
    repo.log(f"Integrity manifest '{name}' recorded: {len(tree):,} files")
    return next(m for m in repo.manifests() if m["id"] == cur.lastrowid)


@repo_method
def verify_manifest(repo, manifest_id: int) -> dict:
    m = next((x for x in repo.manifests() if x["id"] == manifest_id), None)
    if m is None:
        raise KeyError(manifest_id)
    base = _load_json(repo, m["obj"])
    now = scan_tree(m["roots"], hashes=True)
    d = diff_trees(base, now)
    res = {"ts": now_ts(), "ok": not any(d.values()), "counts": {k: len(v) for k, v in d.items()},
           **{k: v[:500] for k, v in d.items()}}
    repo.db.execute("UPDATE manifests SET last_check=? WHERE id=?", (json.dumps(res), manifest_id))
    repo.db.commit()
    c = res["counts"]
    repo.log(f"Integrity check '{m['name']}': " + ("all files match" if res["ok"] else
             f"{c['changed']} changed, {c['added']} added, {c['removed']} missing"),
             level="info" if res["ok"] else "warning")
    return res


@repo_method
def delete_manifest(repo, manifest_id: int) -> None:
    repo.db.execute("DELETE FROM manifests WHERE id=?", (manifest_id,))
    repo.db.commit()


@repo_method
def track_new_config_files(repo, paths: list[str], group: str) -> int:
    n = 0
    for p in paths:
        if os.path.isfile(p):
            repo.add_file(p, "", group)
            n += 1
    repo.ensure_baseline()
    return n


__all__ = ["scan_tree", "diff_trees", "CONFIG_EXT"]
