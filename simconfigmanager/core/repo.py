"""The repository: tracked files, snapshots, versions, restore points, rules and events."""
from __future__ import annotations

import csv
import fnmatch
import io

import json
import os
import re
import socket
import sqlite3
import zlib
from pathlib import Path
from typing import Iterable, Optional


from .util import APP_NAME, default_data_dir, fmt_ts, now_ts, read_file_bytes, sha256_bytes, write_file_atomic  # noqa: F401
from .models import Alert, Change, ConfigVersion, RestorePoint, Rule, Snapshot, TrackedFile, Version, VersionStatus  # noqa: F401
from .alerts import diff_stats, evaluate_rule  # noqa: F401

# --------------------------------------------------------------------------- repository

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY, path TEXT NOT NULL, label TEXT DEFAULT '', group_name TEXT DEFAULT '',
    added_at REAL, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY, created_at REAL, message TEXT, kind TEXT, machine TEXT);
CREATE TABLE IF NOT EXISTS entries (
    snapshot_id INTEGER, file_id INTEGER, hash TEXT, size INTEGER,
    PRIMARY KEY (snapshot_id, file_id));
CREATE INDEX IF NOT EXISTS idx_entries_file ON entries(file_id, snapshot_id);
CREATE TABLE IF NOT EXISTS restore_points (
    id INTEGER PRIMARY KEY, name TEXT UNIQUE, snapshot_id INTEGER, description TEXT,
    created_at REAL, color TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY, name TEXT, file_id INTEGER, kind TEXT, pattern TEXT, expected TEXT,
    action TEXT DEFAULT 'notify', enabled INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY, number INTEGER UNIQUE, title TEXT, notes TEXT, snapshot_id INTEGER,
    created_at REAL, created_by TEXT, machine TEXT);
CREATE TABLE IF NOT EXISTS ignores (
    id INTEGER PRIMARY KEY, file_id INTEGER, kind TEXT, pattern TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS rulebook (
    id INTEGER PRIMARY KEY, file_id INTEGER, selector TEXT, required TEXT, note TEXT, author TEXT, created REAL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL, level TEXT, file_id INTEGER, message TEXT);
"""

DEFAULT_SETTINGS = {
    "machine_name": "",
    "poll_seconds": "5",
    "auto_snapshot": "1",
    "sync_folder": "",
    "auto_push": "1",
    "warn_processes": "Prepar3D.exe;ProSim737.exe;ProSimA320.exe;ProSimA322.exe;ProSimIOS.exe;"
                      "ProSimCDU.exe;ProSimMCP.exe;ProSimDisplay.exe;ProSimAudio.exe",
    "start_minimized": "1",
}


class Repo:
    def __init__(self, data_dir: Path | str | None = None, read_only: bool = False):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        self.obj_dir = self.data_dir / "objects"
        self.read_only = read_only
        if read_only:
            # used by the Claude connector, which runs without admin rights next to the main app
            db = self.data_dir / "repo.db"
            if not db.exists():
                raise FileNotFoundError(f"No {APP_NAME} data found in {self.data_dir}. Start the app once first.")
            self.db = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True, timeout=10)
            self.db.row_factory = sqlite3.Row
        else:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.obj_dir.mkdir(exist_ok=True)
            self.db = sqlite3.connect(str(self.data_dir / "repo.db"), timeout=10)
            self.db.row_factory = sqlite3.Row
            self.db.executescript(SCHEMA)
            self.db.commit()
            (self.data_dir / "proposals").mkdir(exist_ok=True)
        # in-memory stat cache: file_id -> (mtime_ns, size, hash)
        self._stat_cache: dict[int, tuple] = {}
        self._alerted: dict[tuple, Optional[str]] = {}

    # ---------------- settings
    def get(self, key: str) -> str:
        row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is not None:
            return row["value"]
        if key == "machine_name":
            return socket.gethostname()
        return DEFAULT_SETTINGS.get(key, "")

    def set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (key, str(value)))
        self.db.commit()

    @property
    def machine(self) -> str:
        return self.get("machine_name") or socket.gethostname()

    # ---------------- objects
    def _obj_path(self, h: str) -> Path:
        return self.obj_dir / h[:2] / h[2:]

    def put_object(self, data: bytes) -> str:
        h = sha256_bytes(data)
        p = self._obj_path(h)
        if not p.exists():
            p.parent.mkdir(exist_ok=True)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(zlib.compress(data, 6))
            os.replace(tmp, p)
        return h

    def has_object(self, h: str) -> bool:
        return self._obj_path(h).exists()

    def get_object(self, h: Optional[str]) -> Optional[bytes]:
        if h is None:
            return None
        p = self._obj_path(h)
        if not p.exists():
            raise FileNotFoundError(f"Object {h[:12]} is missing from the store")
        return zlib.decompress(p.read_bytes())

    def object_path_raw(self, h: str) -> Path:
        return self._obj_path(h)

    # ---------------- events
    def log(self, message: str, level: str = "info", file_id: Optional[int] = None) -> None:
        actor = getattr(self, "actor", None)
        if actor:
            message = f"{message} — by {actor}"
        self.db.execute("INSERT INTO events(ts, level, file_id, message) VALUES(?,?,?,?)",
                        (now_ts(), level, file_id, message))
        self.db.commit()

    def events(self, limit: int = 500) -> list[dict]:
        rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_events(self) -> None:
        self.db.execute("DELETE FROM events")
        self.db.commit()

    # ---------------- files
    def files(self, include_inactive: bool = False) -> list[TrackedFile]:
        q = "SELECT * FROM files" + ("" if include_inactive else " WHERE active=1") + \
            " ORDER BY group_name, label, path"
        return [TrackedFile(r["id"], r["path"], r["label"], r["group_name"], r["added_at"], bool(r["active"]))
                for r in self.db.execute(q)]

    def file(self, file_id: int) -> Optional[TrackedFile]:
        r = self.db.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
        return TrackedFile(r["id"], r["path"], r["label"], r["group_name"], r["added_at"], bool(r["active"])) \
            if r else None

    def find_file_by_path(self, path: str) -> Optional[TrackedFile]:
        norm = norm_path(path)
        for f in self.files(include_inactive=True):
            if norm_path(f.path) == norm:
                return f
        return None

    def add_file(self, path: str, label: str = "", group: str = "") -> TrackedFile:
        path = path if path.startswith(PROBE_PREFIX) else os.path.abspath(path)
        existing = self.find_file_by_path(path)
        if existing:
            if not existing.active:
                self.db.execute("UPDATE files SET active=1 WHERE id=?", (existing.id,))
                self.db.commit()
                existing.active = True
            return existing
        cur = self.db.execute("INSERT INTO files(path, label, group_name, added_at, active) VALUES(?,?,?,?,1)",
                              (path, label or os.path.basename(path), group, now_ts()))
        self.db.commit()
        f = self.file(cur.lastrowid)
        self.log(f"Started tracking {path}", file_id=f.id)
        return f

    def add_folder(self, folder: str, patterns: str = "*.cfg;*.xml;*.ini;*.json;*.txt",
                   recursive: bool = False, group: str = "") -> list[TrackedFile]:
        pats = [p.strip() for p in patterns.split(";") if p.strip()]
        added = []
        walker = os.walk(folder) if recursive else [(folder, [], os.listdir(folder))]
        for root, _dirs, names in walker:
            for n in names:
                full = os.path.join(root, n)
                if os.path.isfile(full) and any(fnmatch.fnmatch(n.lower(), p.lower()) for p in pats):
                    added.append(self.add_file(full, group=group))
        return added

    def update_file(self, file_id: int, label: Optional[str] = None, group: Optional[str] = None,
                    path: Optional[str] = None) -> None:
        if label is not None:
            self.db.execute("UPDATE files SET label=? WHERE id=?", (label, file_id))
        if group is not None:
            self.db.execute("UPDATE files SET group_name=? WHERE id=?", (group, file_id))
        if path is not None:
            self.db.execute("UPDATE files SET path=? WHERE id=?", (path if path.startswith(PROBE_PREFIX)
                                                                   else os.path.abspath(path), file_id))
            self._stat_cache.pop(file_id, None)
        self.db.commit()

    def remove_file(self, file_id: int) -> None:
        """Stop tracking (history is kept so old snapshots stay restorable)."""
        f = self.file(file_id)
        self.db.execute("UPDATE files SET active=0 WHERE id=?", (file_id,))
        self.db.commit()
        self._stat_cache.pop(file_id, None)
        if f:
            self.log(f"Stopped tracking {f.path}", file_id=file_id)

    def groups(self) -> list[str]:
        return sorted({f.group for f in self.files() if f.group})

    # ---------------- reading current state
    def current_hash(self, f: TrackedFile, use_cache: bool = True) -> tuple[Optional[str], Optional[bytes]]:
        """Return (hash, data). data is None if unchanged-from-cache and not needed."""
        if is_probe(f):
            data = self.probe_output(f)
            return (sha256_bytes(data) if data is not None else None), data
        try:
            st = os.stat(f.path)
        except (FileNotFoundError, NotADirectoryError):
            self._stat_cache[f.id] = (None, None, None)
            return None, None
        except OSError:
            # network path unavailable etc. — treat as unknown (no change)
            cached = self._stat_cache.get(f.id)
            return (cached[2] if cached else self.last_hash(f.id)), None
        key = (st.st_mtime_ns, st.st_size)
        cached = self._stat_cache.get(f.id)
        if use_cache and cached and cached[:2] == key and cached[2] is not None:
            return cached[2], None
        try:
            data = read_file_bytes(f.path)
        except PermissionError:
            cached = self._stat_cache.get(f.id)
            return (cached[2] if cached else self.last_hash(f.id)), None
        if data is None:
            self._stat_cache[f.id] = (None, None, None)
            return None, None
        h = sha256_bytes(data)
        self._stat_cache[f.id] = (key[0], key[1], h)
        return h, data

    def read_current(self, f: TrackedFile) -> Optional[bytes]:
        if is_probe(f):
            return self.probe_output(f)
        try:
            return read_file_bytes(f.path)
        except PermissionError:
            return None

    def write_file(self, f: TrackedFile, data: bytes, note: str = "", kind: str = "manual") -> None:
        """Write new content to a tracked file with a safety snapshot before and a history entry after.
        Every edit (editor, Claude/ChatGPT proposals, remote app) goes through here."""
        if is_probe(f):
            raise ValueError("System information can't be edited — it is read from the PC.")
        cur = self.read_current(f)
        if cur is not None:
            self.put_object(cur)
        self.commit(f"Before edit of {f.display}", kind="safety", file_ids=[f.id])
        write_file_atomic(f.path, data)
        self._stat_cache.pop(f.id, None)
        self.commit(f"Edited {f.display}" + (f": {note}" if note else ""), kind=kind, file_ids=[f.id])

    # ---------------- system information ("probes": command output tracked like a file)
    def probe_output(self, f: TrackedFile, force: bool = False) -> Optional[bytes]:
        cache = getattr(self, "_probe_cache", None)
        if cache is None:
            cache = self._probe_cache = {}
        try:
            minutes = float(self.get("probe_minutes") or 15)
        except ValueError:
            minutes = 15
        hit = cache.get(f.path)
        if hit and not force and (now_ts() - hit[0] < minutes * 60 or getattr(self, "background_probes", False)):
            return hit[1]
        if not hit and not force and getattr(self, "background_probes", False):
            # the app refreshes probes in the background; until then use the last recorded value
            last = self.last_hash(f.id)
            if last:
                try:
                    data = self.get_object(last)
                    cache[f.path] = (0, data)
                    return data
                except FileNotFoundError:
                    pass
        cmd = probe_command(self, f.path)
        if cmd is None:
            return None
        data = run_probe(cmd)
        if data is None and hit:       # keep the last good value if a run fails
            data = hit[1]
        cache[f.path] = (now_ts(), data)
        return data

    def running_sim_processes(self) -> list[str]:
        return running_processes(self.get("warn_processes").split(";"))

    def probe_due(self) -> list[TrackedFile]:
        """Probes whose cached output is older than the refresh interval."""
        try:
            minutes = float(self.get("probe_minutes") or 15)
        except ValueError:
            minutes = 15
        cache = getattr(self, "_probe_cache", {}) or {}
        return [f for f in self.files() if is_probe(f)
                and now_ts() - cache.get(f.path, (0, None))[0] >= minutes * 60]

    def refresh_probes(self) -> None:
        for f in self.files():
            if is_probe(f):
                self.probe_output(f, force=True)

    # ---------------- ignore rules (#1)
    def ignores(self, file_id: Optional[int] = None) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM ignores ORDER BY id")]
        if file_id is None:
            return rows
        return [r for r in rows if r["file_id"] in (None, file_id)]

    def add_ignore(self, file_id: Optional[int], kind: str, pattern: str, note: str = "") -> int:
        cur = self.db.execute("INSERT INTO ignores(file_id, kind, pattern, note) VALUES(?,?,?,?)",
                              (file_id, kind, pattern, note))
        self.db.commit()
        self._same_cache = {}
        f = self.file(file_id) if file_id else None
        self.log(f"Now ignoring {IGNORE_KINDS.get(kind, kind)} '{pattern}'" + (f" in {f.display}" if f else ""))
        return cur.lastrowid

    def delete_ignore(self, ignore_id: int) -> None:
        self.db.execute("DELETE FROM ignores WHERE id=?", (ignore_id,))
        self.db.commit()
        self._same_cache = {}

    def same_content(self, f: TrackedFile, h1: Optional[str], h2: Optional[str],
                     data2: Optional[bytes] = None) -> bool:
        """True if two versions are equal once ignored settings are left out."""
        if h1 == h2:
            return True
        if h1 is None or h2 is None:
            return False
        rules = self.ignores(f.id)
        if not rules:
            return False
        cache = getattr(self, "_same_cache", None)
        if cache is None:
            cache = self._same_cache = {}
        key = (f.id, h1, h2)
        if key not in cache:
            try:
                a = self.get_object(h1)
                b = data2 if data2 is not None else self.get_object(h2)
            except FileNotFoundError:
                return False
            cache[key] = normalize_content(a, f.path, rules) == normalize_content(b, f.path, rules)
        return cache[key]

    # ---------------- snapshots
    def snapshots(self, limit: int = 1000) -> list[Snapshot]:
        rows = self.db.execute("SELECT * FROM snapshots ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [Snapshot(r["id"], r["created_at"], r["message"], r["kind"], r["machine"]) for r in rows]

    def snapshot(self, snapshot_id: int) -> Optional[Snapshot]:
        r = self.db.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return Snapshot(r["id"], r["created_at"], r["message"], r["kind"], r["machine"]) if r else None

    def latest_snapshot(self) -> Optional[Snapshot]:
        s = self.snapshots(1)
        return s[0] if s else None

    def entries(self, snapshot_id: int) -> dict[int, Optional[str]]:
        rows = self.db.execute("SELECT file_id, hash FROM entries WHERE snapshot_id=?", (snapshot_id,))
        return {r["file_id"]: r["hash"] for r in rows}

    def last_hash(self, file_id: int) -> Optional[str]:
        r = self.db.execute("SELECT hash FROM entries WHERE file_id=? ORDER BY snapshot_id DESC LIMIT 1",
                            (file_id,)).fetchone()
        return r["hash"] if r else None

    def has_history(self, file_id: int) -> bool:
        return self.db.execute("SELECT 1 FROM entries WHERE file_id=? LIMIT 1", (file_id,)).fetchone() is not None

    def snapshot_changes(self, snapshot_id: int) -> list[tuple[int, Optional[str], Optional[str]]]:
        """Files whose content differs from the previous snapshot: [(file_id, old, new)]."""
        cur = self.entries(snapshot_id)
        out = []
        for fid, h in cur.items():
            r = self.db.execute("SELECT hash FROM entries WHERE file_id=? AND snapshot_id<? "
                                "ORDER BY snapshot_id DESC LIMIT 1", (fid, snapshot_id)).fetchone()
            prev = r["hash"] if r else "__none__"
            if prev != h:
                out.append((fid, None if prev == "__none__" else prev, h))
        return out

    def commit(self, message: str = "", kind: str = "manual", force: bool = False,
               file_ids: Optional[Iterable[int]] = None) -> Optional[Snapshot]:
        """Record the current state of all tracked files. Returns None if nothing changed."""
        files = self.files()
        only = set(file_ids) if file_ids is not None else None
        rows = []
        changed = []
        for f in files:
            prev = self.last_hash(f.id)
            if only is not None and f.id not in only:
                if not self.has_history(f.id):
                    continue  # never recorded and not part of this commit
                rows.append((f.id, prev, None))
                continue
            else:
                h, data = self.current_hash(f, use_cache=False)
                if data is not None:
                    self.put_object(data)
                elif h is not None and not self.has_object(h):
                    raw = self.read_current(f)
                    if raw is not None:
                        h = self.put_object(raw)
                size = os.path.getsize(f.path) if h is not None and os.path.exists(f.path) else None
            if h != prev or not self.has_history(f.id):
                changed.append(f)
            rows.append((f.id, h, size))
        if not changed and not force:
            return None
        if not message:
            names = ", ".join(f.display for f in changed[:4])
            more = f" +{len(changed) - 4} more" if len(changed) > 4 else ""
            message = f"Changed: {names}{more}" if changed else "No changes"
        actor = getattr(self, "actor", None)
        if actor and kind in ("manual", "restore"):
            message = f"{message} — by {actor}"
        cur = self.db.execute("INSERT INTO snapshots(created_at, message, kind, machine) VALUES(?,?,?,?)",
                              (now_ts(), message, kind, self.machine))
        sid = cur.lastrowid
        self.db.executemany("INSERT INTO entries(snapshot_id, file_id, hash, size) VALUES(?,?,?,?)",
                            [(sid, fid, h, sz) for fid, h, sz in rows])
        self.db.commit()
        return self.snapshot(sid)

    def file_history(self, file_id: int) -> list[Version]:
        """Distinct versions of one file, newest first."""
        rows = self.db.execute(
            "SELECT s.id, s.created_at, s.message, e.hash FROM entries e JOIN snapshots s ON s.id=e.snapshot_id "
            "WHERE e.file_id=? ORDER BY s.id ASC", (file_id,)).fetchall()
        out: list[Version] = []
        last = "__start__"
        for r in rows:
            if r["hash"] != last:
                out.append(Version(r["id"], r["created_at"], r["message"], r["hash"]))
                last = r["hash"]
        return list(reversed(out))

    def file_status(self, f: TrackedFile) -> str:
        """OK | Changed | Missing | New"""
        h, _ = self.current_hash(f)
        if not self.has_history(f.id):
            return "New" if h else "Missing"
        last = self.last_hash(f.id)
        if h is None:
            return "Missing"
        return "OK" if self.same_content(f, last, h) else "Changed"

    # ---------------- restore points
    def restore_points(self) -> list[RestorePoint]:
        return [RestorePoint(r["id"], r["name"], r["snapshot_id"], r["description"] or "", r["created_at"],
                             r["color"] or "")
                for r in self.db.execute("SELECT * FROM restore_points ORDER BY name COLLATE NOCASE")]

    def restore_point(self, name: str) -> Optional[RestorePoint]:
        for rp in self.restore_points():
            if rp.name.lower() == name.lower():
                return rp
        return None

    def create_restore_point(self, name: str, description: str = "", snapshot_id: Optional[int] = None,
                             color: str = "") -> RestorePoint:
        if snapshot_id is None:
            snap = self.commit(f"Restore point: {name}", kind="manual") or self.latest_snapshot()
            if snap is None:
                snap = self.commit(f"Restore point: {name}", kind="manual", force=True)
            snapshot_id = snap.id
        self.db.execute("INSERT OR REPLACE INTO restore_points(name, snapshot_id, description, created_at, color) "
                        "VALUES(?,?,?,?,?)", (name, snapshot_id, description, now_ts(), color))
        self.db.commit()
        self.log(f"Restore point '{name}' saved (snapshot #{snapshot_id})")
        return self.restore_point(name)

    def update_restore_point(self, rp_id: int, name: str, description: str, color: str = "") -> None:
        self.db.execute("UPDATE restore_points SET name=?, description=?, color=? WHERE id=?",
                        (name, description, color, rp_id))
        self.db.commit()

    def update_restore_point_snapshot(self, rp_id: int, snapshot_id: int) -> None:
        self.db.execute("UPDATE restore_points SET snapshot_id=?, created_at=? WHERE id=?",
                        (snapshot_id, now_ts(), rp_id))
        self.db.commit()

    def delete_restore_point(self, rp_id: int) -> None:
        self.db.execute("DELETE FROM restore_points WHERE id=?", (rp_id,))
        self.db.commit()

    def rp_hash(self, rp_name: str, file_id: int) -> Optional[str]:
        rp = self.restore_point(rp_name)
        if not rp:
            return None
        return self.entries(rp.snapshot_id).get(file_id)

    # ---------------- restore
    def preview_restore(self, snapshot_id: int, file_ids: Optional[Iterable[int]] = None
                        ) -> list[tuple[TrackedFile, str]]:
        """What would change: [(file, action)] where action is write|unchanged|skip-missing."""
        ents = self.entries(snapshot_id)
        only = set(file_ids) if file_ids is not None else None
        out = []
        for fid, h in ents.items():
            if only is not None and fid not in only:
                continue
            f = self.file(fid)
            if f is None:
                continue
            cur, _ = self.current_hash(f)
            if is_probe(f):
                continue  # system information is read from the PC, never written
            if h is None:
                out.append((f, "skip (did not exist then)"))
            elif cur == h:
                out.append((f, "unchanged"))
            else:
                out.append((f, "will be restored"))
        return out

    def restore(self, snapshot_id: int, file_ids: Optional[Iterable[int]] = None,
                label: str = "") -> list[tuple[TrackedFile, str]]:
        """Restore files to the state in a snapshot. Takes a safety snapshot first."""
        snap = self.snapshot(snapshot_id)
        if snap is None:
            raise ValueError("Unknown snapshot")
        label = label or f"snapshot #{snapshot_id}"
        self.commit(f"Safety copy before restoring to {label}", kind="safety")
        ents = self.entries(snapshot_id)
        only = set(file_ids) if file_ids is not None else None
        report = []
        for fid, h in ents.items():
            if only is not None and fid not in only:
                continue
            f = self.file(fid)
            if f is None:
                continue
            if is_probe(f):
                continue
            if h is None:
                report.append((f, "skipped (did not exist in that version)"))
                continue
            cur, _ = self.current_hash(f, use_cache=False)
            if cur == h:
                report.append((f, "already matches"))
                continue
            try:
                write_file_atomic(f.path, self.get_object(h))
                self._stat_cache.pop(fid, None)
                report.append((f, "restored"))
            except Exception as e:  # noqa: BLE001
                report.append((f, f"FAILED: {e}"))
        restored = sum(1 for _, r in report if r == "restored")
        failed = sum(1 for _, r in report if r.startswith("FAILED"))
        self.commit(f"Restored {restored} file(s) to {label}", kind="restore")
        self.log(f"Restored {restored} file(s) to {label}" + (f", {failed} failed" if failed else ""),
                 level="warning" if failed else "info")
        return report

    def restore_version(self, file_id: int, h: str, label: str = "") -> None:
        f = self.file(file_id)
        if is_probe(f):
            raise ValueError("System information can't be restored — it is read from the PC.")
        self.commit(f"Safety copy before restoring {f.display}", kind="safety")
        write_file_atomic(f.path, self.get_object(h))
        self._stat_cache.pop(file_id, None)
        self.commit(f"Restored {f.display} to {label or h[:10]}", kind="restore")
        self.log(f"Restored {f.display} to {label or h[:10]}", file_id=file_id)

    # ---------------- versions
    def versions(self) -> list[ConfigVersion]:
        return [ConfigVersion(r["id"], r["number"], r["title"] or "", r["notes"] or "", r["snapshot_id"],
                              r["created_at"], r["created_by"] or "", r["machine"] or "")
                for r in self.db.execute("SELECT * FROM versions ORDER BY number DESC")]

    def version(self, number: int) -> Optional[ConfigVersion]:
        return next((v for v in self.versions() if v.number == number), None)

    def latest_version(self) -> Optional[ConfigVersion]:
        v = self.versions()
        return v[0] if v else None

    def version_status(self, number: Optional[int] = None) -> tuple[Optional[ConfigVersion], list[VersionStatus]]:
        """Compare what is on disk NOW with a version (default: the latest)."""
        ver = self.version(number) if number is not None else self.latest_version()
        files = self.files()
        if ver is None:
            return None, [VersionStatus(f, "new", None, self.current_hash(f)[0], False) for f in files]
        ents = self.entries(ver.snapshot_id)
        out = []
        for f in files:
            cur, _ = self.current_hash(f)
            if f.id not in ents:
                out.append(VersionStatus(f, "new", None, cur, False))
                continue
            vh = ents[f.id]
            if vh == cur:
                st = "match" if cur is not None else "gone"
            elif cur is None:
                st = "missing"
            elif self.same_content(f, vh, cur):
                st = "match"
            else:
                st = "modified"
            out.append(VersionStatus(f, st, vh, cur, True))
        # files that are in the version but are no longer tracked
        tracked = {f.id for f in files}
        for fid, vh in ents.items():
            if fid not in tracked and vh is not None:
                f = self.file(fid)
                if f and not is_probe(f):
                    cur = sha256_bytes(read_file_bytes(f.path) or b"") if os.path.exists(f.path) else None
                    out.append(VersionStatus(f, "untracked", vh, cur, True))
        return ver, out

    def mismatches(self, number: Optional[int] = None) -> list[VersionStatus]:
        _v, st = self.version_status(number)
        return [x for x in st if x.status in ("modified", "missing", "new")]

    def changes_between(self, snap_a: Optional[int], snap_b: int) -> list[tuple[TrackedFile, Optional[str],
                                                                              Optional[str]]]:
        """Files that differ between two snapshots (snap_a None = nothing before)."""
        a = self.entries(snap_a) if snap_a else {}
        b = self.entries(snap_b)
        out = []
        for fid in sorted(set(a) | set(b)):
            if a.get(fid) != b.get(fid):
                f = self.file(fid)
                if f:
                    out.append((f, a.get(fid), b.get(fid)))
        return out

    def draft_release_notes(self) -> str:
        """Auto-written notes: which files changed since the last version, plus the notes typed when editing."""
        prev = self.latest_version()
        since = prev.created_at if prev else 0
        lines = []
        mism = self.mismatches()
        for vs in mism:
            kind = {"modified": "changed", "missing": "deleted", "new": "added"}[vs.status]
            extra = ""
            if vs.status == "modified":
                try:
                    a, d = diff_stats(self.get_object(vs.version_hash), self.read_current(vs.file))
                    extra = f" (+{a}/-{d} lines)"
                except FileNotFoundError:
                    pass
            lines.append(f"- {vs.file.display} [{vs.file.group}] {kind}{extra}")
        notes = []
        for r in self.db.execute("SELECT message FROM snapshots WHERE created_at>? AND kind='manual' "
                                 "AND message LIKE 'Edited %: %' ORDER BY id", (since,)):
            notes.append("  · " + r["message"][len("Edited "):])
        if notes:
            lines += ["", "Notes from edits:"] + notes
        return "\n".join(lines)

    def create_version(self, title: str = "", notes: str = "", created_by: str = "") -> ConfigVersion:
        self.refresh_probes()
        self.ensure_baseline()
        snap = self.commit(f"Version: {title}" if title else "New version", kind="manual") or self.latest_snapshot()
        if snap is None:
            snap = self.commit("New version", kind="manual", force=True)
        row = self.db.execute("SELECT COALESCE(MAX(number), 0) AS n FROM versions").fetchone()
        number = row["n"] + 1
        self.db.execute("INSERT INTO versions(number, title, notes, snapshot_id, created_at, created_by, machine) "
                        "VALUES(?,?,?,?,?,?,?)", (number, title, notes, snap.id, now_ts(),
                                                  created_by or os.environ.get("USERNAME", ""), self.machine))
        self.db.commit()
        self.log(f"Version v{number} created" + (f": {title}" if title else ""))
        return self.version(number)

    def version_for_snapshot(self, snapshot_id: int) -> tuple[Optional[ConfigVersion], list[str]]:
        """Which version does a snapshot correspond to? Returns (latest version, files that differ from it)."""
        ver = self.latest_version()
        if not ver:
            return None, []
        a, b = self.entries(ver.snapshot_id), self.entries(snapshot_id)
        tracked = {f.id for f in self.files()}
        diff = []
        for fid in tracked:
            if a.get(fid) != b.get(fid):
                f = self.file(fid)
                diff.append(f.display if f else str(fid))
        return ver, diff

    # ---------------- rules
    def rules(self) -> list[Rule]:
        return [Rule(r["id"], r["name"], r["file_id"], r["kind"], r["pattern"] or "", r["expected"] or "",
                     r["action"] or "notify", bool(r["enabled"]))
                for r in self.db.execute("SELECT * FROM rules ORDER BY id")]

    def save_rule(self, rule: Rule) -> Rule:
        if rule.id:
            self.db.execute("UPDATE rules SET name=?, file_id=?, kind=?, pattern=?, expected=?, action=?, enabled=? "
                            "WHERE id=?", (rule.name, rule.file_id, rule.kind, rule.pattern, rule.expected,
                                           rule.action, int(rule.enabled), rule.id))
        else:
            cur = self.db.execute("INSERT INTO rules(name, file_id, kind, pattern, expected, action, enabled) "
                                  "VALUES(?,?,?,?,?,?,?)", (rule.name, rule.file_id, rule.kind, rule.pattern,
                                                            rule.expected, rule.action, int(rule.enabled)))
            rule.id = cur.lastrowid
        self.db.commit()
        return rule

    def delete_rule(self, rule_id: int) -> None:
        self.db.execute("DELETE FROM rules WHERE id=?", (rule_id,))
        self.db.commit()

    # ---------------- the scan loop
    def scan(self) -> list[Change]:
        """Compare every tracked file with its last recorded version. Cheap: hashes only when mtime/size moved."""
        changes = []
        for f in self.files():
            new_h, new_data = self.current_hash(f)
            if not self.has_history(f.id):
                continue  # baseline not taken yet
            old_h = self.last_hash(f.id)
            if new_h == old_h:
                continue
            if new_data is None and new_h is not None:
                new_data = self.read_current(f)
            if self.same_content(f, old_h, new_h, new_data):
                continue  # only ignored settings changed (e.g. values the sim rewrites on exit)
            try:
                old_data = self.get_object(old_h)
            except FileNotFoundError:
                old_data = None
            changes.append(Change(f, old_h, new_h, old_data, new_data))
        return changes

    def process_changes(self, changes: list[Change]) -> list[Alert]:
        """Evaluate rules (once per new version), run auto-revert, auto-snapshot."""
        alerts: list[Alert] = []
        reverted_ids = set()
        rules = [r for r in self.rules() if r.enabled]
        for ch in changes:
            for rule in rules:
                if rule.file_id is not None and rule.file_id != ch.file.id:
                    continue
                key = (rule.id, ch.file.id)
                if self._alerted.get(key) == ch.new_hash:
                    continue
                msg = evaluate_rule(rule, ch, self.rp_hash)
                self._alerted[key] = ch.new_hash
                if not msg:
                    continue
                alert = Alert(rule, ch.file, msg)
                if rule.action == "revert" and ch.file.id not in reverted_ids:
                    target = None
                    if rule.kind == "differs_from_rp":
                        target = self.rp_hash(rule.pattern, ch.file.id)
                    target = target or ch.old_hash
                    if target:
                        try:
                            if ch.new_data is not None:
                                self.put_object(ch.new_data)  # keep the rejected version for reference
                            write_file_atomic(ch.file.path, self.get_object(target))
                            self._stat_cache.pop(ch.file.id, None)
                            reverted_ids.add(ch.file.id)
                            alert.reverted = True
                            alert.message += "  → automatically reverted"
                        except Exception as e:  # noqa: BLE001
                            alert.message += f"  → auto-revert FAILED: {e}"
                level = "alert"
                self.log(f"[{rule.name}] {alert.message}", level=level, file_id=ch.file.id)
                alerts.append(alert)
        # rulebook requirements (#6): alert once per new version of a file that breaks one
        for ch in changes:
            if ch.file.id in reverted_ids or ch.new_data is None:
                continue
            for e in rulebook_violations_for(self, ch.file, ch.new_data):
                key = ("rb", e["id"], ch.file.id)
                if self._alerted.get(key) == ch.new_hash:
                    continue
                self._alerted[key] = ch.new_hash
                msg = (f"{ch.file.display}: {e['selector']} = {e['current']!r}, rulebook requires "
                       f"{e['required']!r}" + (f" — {e['note']}" if e.get("note") else ""))
                self.log(f"[Rulebook] {msg}", level="alert", file_id=ch.file.id)
                alerts.append(Alert(Rule(0, "Rulebook", ch.file.id, "rulebook"), ch.file, msg))
        if self.get("auto_snapshot") == "1":
            ids = [c.file.id for c in changes if c.file.id not in reverted_ids]
            if ids:
                self.commit(kind="auto", file_ids=ids)
        return alerts

    def ensure_baseline(self) -> Optional[Snapshot]:
        """Take a first snapshot for files that have no history yet."""
        missing = [f.id for f in self.files() if not self.has_history(f.id)]
        if missing:
            return self.commit("Baseline (started tracking)", kind="baseline", file_ids=missing)
        return None

    # ---------------- export
    def export_changelog(self, path: str, fmt: str = "md") -> None:
        Path(path).write_text(self.changelog(fmt), encoding="utf-8", newline="" if fmt == "csv" else None)

    def changelog(self, fmt: str = "md") -> str:
        snaps = list(reversed(self.snapshots(100000)))
        rows = []
        for s in snaps:
            for fid, old, new in self.snapshot_changes(s.id):
                f = self.file(fid)
                if f is None:
                    continue
                a = d = 0
                try:
                    a, d = diff_stats(self.get_object(old), self.get_object(new))
                except FileNotFoundError:
                    pass
                rows.append({"time": fmt_ts(s.created_at), "machine": s.machine, "kind": s.kind,
                             "message": s.message, "file": f.display, "group": f.group, "path": f.path,
                             "lines_added": a, "lines_removed": d,
                             "status": "deleted" if new is None else ("new" if old is None else "modified")})
        if fmt == "csv":
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["time"])
            w.writeheader()
            w.writerows(rows)
            return buf.getvalue()
        else:
            out = [f"# {APP_NAME} changelog — {self.machine}", "",
                   "| Time | Machine | File | Group | Change | +/- | Note |", "|---|---|---|---|---|---|---|"]
            for r in reversed(rows):
                note = r["message"].replace("|", "/")
                out.append(f"| {r['time']} | {r['machine']} | {r['file']} | {r['group']} | {r['status']} | "
                           f"+{r['lines_added']}/-{r['lines_removed']} | {note} |")
            return "\n".join(out)

    def export_snapshot_zip(self, snapshot_id: int, zip_path: str) -> None:
        import zipfile
        ents = self.entries(snapshot_id)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            manifest = []
            for fid, h in ents.items():
                f = self.file(fid)
                if f is None or h is None:
                    continue
                safe = re.sub(r"[:\\/]+", "_", f.path).strip("_")
                arc = f"{f.group or 'files'}/{safe}"
                z.writestr(arc, self.get_object(h))
                manifest.append({"path": f.path, "label": f.label, "group": f.group, "sha256": h, "zip": arc})
            z.writestr("manifest.json", json.dumps(manifest, indent=2))

    def close(self):
        self.db.close()


# imported last: these modules build on Repo
from .settings import IGNORE_KINDS, normalize_content  # noqa: F401
from .probes import PROBE_PREFIX, is_probe, norm_path, probe_command, run_probe  # noqa: F401
from .rulebook_ops import rulebook_violations_for  # noqa: F401
from .system import running_processes  # noqa: F401

__all__ = ['SCHEMA', 'DEFAULT_SETTINGS', 'Repo']
