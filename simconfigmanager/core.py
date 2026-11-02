"""
SimConfigManager core engine.

A small, git-like version store for individual files that can live anywhere
on disk (P3D .cfg/.xml, ProSim config.xml, etc.).

Storage layout (data dir, default %APPDATA%\\SimConfigManager):
    repo.db                 SQLite: tracked files, snapshots, restore points, rules, events
    objects/ab/cdef...      zlib-compressed file contents, addressed by SHA-256 (like git blobs)

A *snapshot* records the content hash of every tracked file at one moment
(like a git commit of the whole tree). A *restore point* is a named snapshot
(like a git tag) that can be restored with one click.
"""
from __future__ import annotations

import csv
import difflib
import fnmatch
import glob
import hashlib
import io

import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional

APP_NAME = "SimConfigManager"
APP_VERSION = "1.11.0"
LEGACY_NAMES = ("SimConfigKeeper",)   # earlier name of this app (≤ 1.3)


# --------------------------------------------------------------------------- helpers

def default_data_dir() -> Path:
    """C:\\ProgramData\\SimConfigManager — shared by every account, so it is the same store whether the
    app runs elevated or not. Stores created by earlier versions (old name / %APPDATA%) keep being used."""
    appdata = Path(os.environ.get("APPDATA") or os.path.join(Path.home(), ".config"))
    progdata = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) if sys.platform == "win32" else None
    candidates = []
    for name in (APP_NAME,) + LEGACY_NAMES:
        if progdata:
            candidates.append(progdata / name)
        candidates.append(appdata / name)
    for c in candidates:
        if (c / "repo.db").exists():
            return c
    return (progdata or appdata) / APP_NAME


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_binary(data: Optional[bytes]) -> bool:
    if not data:
        return False
    return b"\x00" in data[:8192]


def decode_text(data: Optional[bytes]) -> str:
    if data is None:
        return ""
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


@dataclass
class TextFormat:
    encoding: str = "utf-8"
    bom: bytes = b""
    newline: str = "\r\n"


def detect_text(data: Optional[bytes]) -> tuple[str, "TextFormat"]:
    """Decode bytes and remember encoding, BOM and line endings so a save writes the file back the same way."""
    if not data:
        return "", TextFormat()
    boms = [(b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
    for bom, enc in boms:
        if data.startswith(bom):
            text = data[len(bom):].decode(enc, errors="replace")
            break
    else:
        bom = b""
        # XML may declare its own encoding (P3D dll.xml / exe.xml use Windows-1252) — honour it
        m = re.match(rb'\s*<\?xml[^>]*encoding=["\']([A-Za-z0-9_-]+)["\']', data[:200])
        candidates = ([m.group(1).decode().lower()] if m else []) + ["utf-8", "cp1252"]
        text = None
        for enc in candidates:
            try:
                text = data.decode(enc)
                break
            except (LookupError, UnicodeDecodeError):
                continue
        if text is None:
            enc = "cp1252"
            text = data.decode(enc, errors="replace")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "\r\n" if crlf >= lf and crlf else ("\n" if lf else "\r\n")
    return text.replace("\r\n", "\n").replace("\r", "\n"), TextFormat(enc, bom, newline)


def encode_text(text: str, fmt: "TextFormat") -> bytes:
    body = text.replace("\r\n", "\n").replace("\n", fmt.newline)
    try:
        return fmt.bom + body.encode(fmt.encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end] if hasattr(e, "start") else "?"
        raise ValueError(f"The character {bad!r} cannot be saved in this file's encoding ({fmt.encoding}).")


def validate_text(text: str, kind: str) -> list[tuple[int, str]]:
    """Return [(line number, problem)] — empty list means it looks fine."""
    problems: list[tuple[int, str]] = []
    if kind == "xml":
        try:
            ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8"))
        except ET.ParseError as e:
            line = e.position[0] if getattr(e, "position", None) else 1
            problems.append((line, f"XML error: {e}"))
    elif kind == "json":
        try:
            json.loads(text)
        except ValueError as e:
            problems.append((getattr(e, "lineno", 1), f"JSON error: {e}"))
    elif kind == "ini":
        section, seen = "", {}
        for n, raw in enumerate(text.split("\n"), 1):
            line = raw.strip()
            if not line or line.startswith((";", "#", "//")):
                continue
            if line.startswith("["):
                if "]" not in line:
                    problems.append((n, "Section header is missing ']'"))
                    continue
                section = line[1:line.index("]")].strip().lower()
                continue
            if "=" not in line:
                problems.append((n, "Line has no '=' (not a key=value line)"))
                continue
            key = (section, line.split("=", 1)[0].strip().lower())
            if key in seen:
                problems.append((n, f"Duplicate key '{key[1]}' in [{section}] (first on line {seen[key]})"))
            else:
                seen[key] = n
    return problems


def read_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        raise


def write_file_atomic(path: str, data: bytes) -> None:
    """Write via temp file + replace so a crash never leaves a half-written config."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".sck-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


# --------------------------------------------------------------------------- data classes

@dataclass
class TrackedFile:
    id: int
    path: str
    label: str
    group: str
    added_at: float
    active: bool = True

    @property
    def display(self) -> str:
        return self.label or os.path.basename(self.path)


@dataclass
class Snapshot:
    id: int
    created_at: float
    message: str
    kind: str          # manual | auto | baseline | safety | restore | import
    machine: str


@dataclass
class ConfigVersion:
    """A numbered, permanent release of the whole configuration (all tracked files)."""
    id: int
    number: int
    title: str
    notes: str
    snapshot_id: int
    created_at: float
    created_by: str
    machine: str

    @property
    def label(self) -> str:
        return f"v{self.number}" + (f" – {self.title}" if self.title else "")


@dataclass
class VersionStatus:
    file: "TrackedFile"
    status: str                 # match | modified | missing | new | restored-missing
    version_hash: Optional[str]
    current_hash: Optional[str]
    in_version: bool


STATUS_TEXT = {
    "match": "Matches",
    "modified": "Modified",
    "missing": "Missing on disk",
    "new": "Not in version",
    "gone": "Deleted (as in version)",
    "untracked": "No longer tracked",
}


@dataclass
class RestorePoint:
    id: int
    name: str
    snapshot_id: int
    description: str
    created_at: float
    color: str = ""


@dataclass
class Version:
    """One distinct version of a single file in its history."""
    snapshot_id: int
    created_at: float
    message: str
    hash: Optional[str]   # None = file did not exist


@dataclass
class Rule:
    id: int
    name: str
    file_id: Optional[int]        # None = applies to every tracked file
    kind: str
    pattern: str = ""
    expected: str = ""
    action: str = "notify"        # notify | revert
    enabled: bool = True


@dataclass
class Change:
    file: TrackedFile
    old_hash: Optional[str]
    new_hash: Optional[str]
    old_data: Optional[bytes]
    new_data: Optional[bytes]


@dataclass
class Alert:
    rule: Rule
    file: TrackedFile
    message: str
    reverted: bool = False


# --------------------------------------------------------------------------- rule kinds

RULE_KINDS = {
    "any_change": "Any change to the file",
    "deleted": "File was deleted / went missing",
    "line_added": "A line matching a pattern was ADDED",
    "line_removed": "A line matching a pattern was REMOVED",
    "ini_value": "INI/CFG value changed  (pattern = Section/Key)",
    "xml_value": "XML value changed  (pattern = element path, optional @attribute)",
    "differs_from_rp": "File no longer matches a restore point  (pattern = restore point name)",
}

RULE_HELP = {
    "any_change": "Fires on every change. Pattern/expected are ignored.",
    "deleted": "Fires when the file disappears. Pattern/expected are ignored.",
    "line_added": "Pattern is a regular expression, e.g.  ^\\s*<Path>.*fsuipc  or  TEXTURE_MAX_LOAD",
    "line_removed": "Pattern is a regular expression matched against removed lines.",
    "ini_value": "Pattern 'GRAPHICS/TEXTURE_MAX_LOAD' (section/key, case-insensitive). "
                 "Expected (optional): alert only when the new value is NOT this value.",
    "xml_value": "Pattern is an ElementTree path, e.g.  .//Launch.Addon/Name  or  .//hardware@ip . "
                 "Expected (optional): alert only when the new value is NOT this value.",
    "differs_from_rp": "Pattern = restore point name. Alerts when the file drifts from that restore point.",
}


def parse_ini(text: str) -> dict:
    """Tolerant INI/CFG parser (P3D cfg files are not strict INI). Last key wins."""
    out: dict = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#", "//")):
            continue
        if line.startswith("[") and "]" in line:
            section = line[1:line.index("]")].strip().lower()
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[(section, k.strip().lower())] = v.strip()
    return out


def xml_values(data: Optional[bytes], path: str) -> Optional[list]:
    if data is None:
        return None
    attr = None
    if "@" in path:
        path, attr = path.rsplit("@", 1)
        path = path or "."
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Some sim XML files have junk before the root element; try the text route.
        try:
            root = ET.fromstring(decode_text(data).encode("utf-8").lstrip())
        except ET.ParseError:
            return None
    try:
        elems = [root] if path in (".", "") else root.findall(path)
    except SyntaxError:
        return None
    vals = []
    for el in elems:
        if attr:
            vals.append(el.get(attr))
        else:
            vals.append((el.text or "").strip())
    return vals


def changed_lines(old: Optional[bytes], new: Optional[bytes]) -> tuple[list, list]:
    a = decode_text(old).splitlines()
    b = decode_text(new).splitlines()
    added, removed = [], []
    for op in difflib.ndiff(a, b):
        if op.startswith("+ "):
            added.append(op[2:])
        elif op.startswith("- "):
            removed.append(op[2:])
    return added, removed


def evaluate_rule(rule: Rule, change: Change, rp_hash_lookup: Callable[[str, int], Optional[str]] | None = None
                  ) -> Optional[str]:
    """Return an alert message if the rule matches this change, else None."""
    k = rule.kind
    name = change.file.display
    try:
        if k == "any_change":
            if change.new_hash is None:
                return f"{name} was deleted"
            if change.old_hash is None:
                return f"{name} appeared"
            return f"{name} changed"
        if k == "deleted":
            return f"{name} is missing / was deleted" if change.new_hash is None and change.old_hash else None
        if k in ("line_added", "line_removed"):
            if is_binary(change.new_data) or is_binary(change.old_data):
                return None
            rx = re.compile(rule.pattern or ".*", re.IGNORECASE)
            added, removed = changed_lines(change.old_data, change.new_data)
            lines = added if k == "line_added" else removed
            hits = [ln.strip() for ln in lines if rx.search(ln)]
            if hits:
                verb = "added" if k == "line_added" else "removed"
                more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
                return f"{name}: line {verb}: {hits[0][:120]}{more}"
            return None
        if k == "ini_value":
            if "/" in rule.pattern:
                sec, key = rule.pattern.split("/", 1)
            else:
                sec, key = "", rule.pattern
            sk = (sec.strip().lower(), key.strip().lower())
            old = parse_ini(decode_text(change.old_data)).get(sk) if change.old_data is not None else None
            new = parse_ini(decode_text(change.new_data)).get(sk) if change.new_data is not None else None
            if rule.expected:
                if new != rule.expected and old != new:
                    return f"{name}: {rule.pattern} = {new!r} (expected {rule.expected!r})"
                return None
            if old != new:
                return f"{name}: {rule.pattern} changed {old!r} → {new!r}"
            return None
        if k == "xml_value":
            old = xml_values(change.old_data, rule.pattern)
            new = xml_values(change.new_data, rule.pattern)
            if rule.expected:
                if new is not None and old != new and any(v != rule.expected for v in new):
                    return f"{name}: {rule.pattern} = {new} (expected {rule.expected!r})"
                return None
            if old != new:
                return f"{name}: {rule.pattern} changed {old} → {new}"
            return None
        if k == "differs_from_rp":
            if rp_hash_lookup is None:
                return None
            target = rp_hash_lookup(rule.pattern, change.file.id)
            if target is None:
                return None
            if change.new_hash != target:
                return f"{name} no longer matches restore point '{rule.pattern}'"
            return None
    except re.error as e:
        return f"Rule '{rule.name}' has an invalid pattern: {e}"
    return None


# --------------------------------------------------------------------------- diff

def unified_diff(old: Optional[bytes], new: Optional[bytes], old_label="before", new_label="after",
                 context: int = 3) -> str:
    if is_binary(old) or is_binary(new):
        o = "missing" if old is None else f"{len(old)} bytes, sha {sha256_bytes(old)[:10]}"
        n = "missing" if new is None else f"{len(new)} bytes, sha {sha256_bytes(new)[:10]}"
        return f"Binary file: {o}  →  {n}"
    a = decode_text(old).splitlines()
    b = decode_text(new).splitlines()
    return "\n".join(difflib.unified_diff(a, b, old_label, new_label, n=context, lineterm=""))


def diff_stats(old: Optional[bytes], new: Optional[bytes]) -> tuple[int, int]:
    if is_binary(old) or is_binary(new):
        return (0, 0)
    added, removed = changed_lines(old, new)
    return len(added), len(removed)


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
        from .storage import LocalBackend
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
        from .remote import RemoteSync
        return RemoteSync(repo) if repo.call("sync_configured") else None
    mode = repo.get("sync_mode") or ("folder" if repo.get("sync_folder") else "")
    if mode == "folder" and repo.get("sync_folder"):
        return FolderSync(repo, repo.get("sync_folder"))
    if mode == "gdrive":
        from . import storage
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
    }


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


# --------------------------------------------------------------------------- discovery of sim configs

def discover_sim_configs() -> list[tuple[str, str, str]]:
    """Suggest well-known P3D / ProSim config files that exist on this PC: [(label, group, path)]."""
    appdata = os.environ.get("APPDATA", "")
    progdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    home = str(Path.home())
    found: list[tuple[str, str, str]] = []
    seen = set()

    def add(label, group, path):
        key = os.path.normcase(os.path.abspath(path))
        if os.path.isfile(path) and key not in seen:
            seen.add(key)
            found.append((label, group, os.path.abspath(path)))

    for ver in ("v5", "v6", "v4"):
        grp = f"P3D {ver}"
        for base in (os.path.join(appdata, "Lockheed Martin", f"Prepar3D {ver}"),
                     os.path.join(progdata, "Lockheed Martin", f"Prepar3D {ver}")):
            scope = "AppData" if base.startswith(appdata) and appdata else "ProgramData"
            for name in ("Prepar3D.cfg", "dll.xml", "exe.xml", "add-ons.cfg", "Cameras.cfg", "scenery.cfg",
                         "Controls\\Standard.xml", "terrain.cfg", "SimObjects.cfg"):
                add(f"{os.path.basename(name)} ({scope})", grp, os.path.join(base, name))
            for x in glob.glob(os.path.join(base, "Controls", "*.xml")):
                add(f"Controls/{os.path.basename(x)} ({scope})", grp, x)
        docs = os.path.join(home, "Documents", f"Prepar3D {ver} Add-ons")
        for x in glob.glob(os.path.join(docs, "*", "add-on.xml")):
            add(f"add-on.xml: {Path(x).parent.name}", grp + " add-ons", x)

    # ProSim: install location varies — look in the usual roots, two levels deep.
    roots = ["C:\\", "D:\\", "E:\\", r"C:\Program Files", r"C:\Program Files (x86)",
             os.path.join(home, "Documents"), os.path.join(home, "Desktop")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for d in entries:
            if "prosim" not in d.lower():
                continue
            top = os.path.join(root, d)
            for pattern in ("config.xml", "*/config.xml", "*/*/config.xml", "*.ini", "*/*.ini"):
                for x in glob.glob(os.path.join(top, pattern)):
                    rel = os.path.relpath(x, top)
                    add(f"ProSim {rel}", "ProSim", x)
    return found


# --------------------------------------------------------------------------- OS integration

def running_processes(names: Iterable[str]) -> list[str]:
    wanted = {n.strip().lower() for n in names if n.strip()}
    if not wanted:
        return []
    try:
        import psutil  # type: ignore
    except ImportError:
        return []
    hits = set()
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info.get("name") or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if n in wanted:
            hits.add(p.info["name"])
    return sorted(hits)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = APP_NAME


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def launch_parts() -> tuple[str, str]:
    """(program, arguments) used to start the app at logon."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--minimized"
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return exe, f'"{os.path.abspath(sys.argv[0])}" --minimized'


def relaunch_as_admin(extra_args: list[str]) -> bool:
    """Ask Windows (UAC) to start a new, elevated copy of this program. True if it started."""
    if sys.platform != "win32":
        return False
    import ctypes
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, extra_args
    else:
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            exe = exe[:-10] + "pythonw.exe"
        params = [os.path.abspath(sys.argv[0])] + extra_args
    import subprocess
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(params), None, 1)
    return rc > 32


def _no_window():
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}  # CREATE_NO_WINDOW


def _task_xml() -> str:
    from xml.sax.saxutils import escape
    prog, args = launch_parts()
    user = os.environ.get("USERNAME", "")
    dom = os.environ.get("USERDOMAIN", "")
    uid = f"{dom}\\{user}" if dom else user
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts {APP_NAME} with administrator rights at logon.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(uid)}</UserId><Delay>PT20S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(uid)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(prog)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""


def _remove_run_key():
    for name in (APP_NAME,) + LEGACY_NAMES:
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass


def _task_exists(name: str) -> bool:
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, **_no_window())
    return r.returncode == 0


def migrate_legacy_autostart() -> bool:
    """If the app was set to start at logon under its old name, move that to the new task. Needs admin."""
    if sys.platform != "win32":
        return False
    import subprocess
    found = False
    for name in LEGACY_NAMES:
        if _task_exists(name):
            subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, **_no_window())
            found = True
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, name)
                found = True
        except OSError:
            pass
    if found:
        set_autostart(True)
    return found


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    return _task_exists(TASK_NAME)


def set_autostart(enabled: bool) -> bool:
    """
    Start at logon WITH admin rights. The usual 'Run' registry key cannot start elevated programs
    (Windows silently skips them), so a Task Scheduler task with 'highest privileges' is used.
    Creating the task needs admin rights itself.
    """
    if sys.platform != "win32":
        return False
    import subprocess
    import tempfile
    _remove_run_key()  # v1.0 used the Run key
    if not enabled:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, **_no_window())
        return True
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(xml_path).write_text(_task_xml(), encoding="utf-16")
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"],
                           capture_output=True, text=True, **_no_window())
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout or "schtasks failed").strip())
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass
    return True


# --------------------------------------------------------------------------- change proposals (from Claude)
#
# The Claude connector runs without admin rights, so it never writes config files itself. It drops a
# proposal (JSON) into <data dir>/proposals/; the main app shows it with a diff and only applies it
# after a person clicks Apply. Applying goes through the normal safety-snapshot + history path.

def proposals_dir(repo: "Repo") -> Path:
    return repo.data_dir / "proposals"


def create_proposal(repo: "Repo", f: TrackedFile, new_data: bytes, reason: str, author: str = "Claude") -> dict:
    import base64
    import uuid
    cur = read_file_bytes(f.path)
    prop = {
        "id": time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "created": now_ts(), "author": author, "status": "pending",
        "file_id": f.id, "label": f.display, "path": f.path, "reason": reason.strip(),
        "base_hash": sha256_bytes(cur) if cur is not None else None,
        "new_hash": sha256_bytes(new_data), "new_b64": base64.b64encode(new_data).decode("ascii"),
    }
    d = proposals_dir(repo)
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f"{prop['id']}.tmp"
    tmp.write_text(json.dumps(prop, indent=1), encoding="utf-8")
    os.replace(tmp, d / f"{prop['id']}.json")
    return prop


def list_proposals(repo: "Repo", status: Optional[str] = None) -> list[dict]:
    if is_remote(repo):
        return repo.call("list_proposals", status)
    d = proposals_dir(repo)
    out = []
    if d.is_dir():
        for p in sorted(d.glob("*.json")):
            try:
                prop = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if status is None or prop.get("status") == status:
                out.append(prop)
    return out


def proposal_data(prop: dict) -> bytes:
    import base64
    return base64.b64decode(prop["new_b64"])


def _save_proposal(repo: "Repo", prop: dict) -> None:
    p = proposals_dir(repo) / f"{prop['id']}.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(prop, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def reject_proposal(repo: "Repo", prop: dict, note: str = "") -> None:
    if is_remote(repo):
        return repo.call("reject_proposal", prop, note)
    prop.update(status="rejected", decided=now_ts(), note=note)
    _save_proposal(repo, prop)
    repo.log(f"Rejected Claude's proposed change to {prop['label']}" + (f": {note}" if note else ""),
             file_id=prop.get("file_id"))


def apply_proposal(repo: "Repo", prop: dict, approved_by: str = "") -> None:
    """Write the proposed content (safety snapshot first, recorded in History)."""
    if is_remote(repo):
        return repo.call("apply_proposal", prop, approved_by)
    f = repo.file(prop["file_id"])
    if f is None:
        raise ValueError("That file is no longer tracked.")
    data = proposal_data(prop)
    if sha256_bytes(data) != prop["new_hash"]:
        raise ValueError("Proposal is damaged (checksum mismatch).")
    who = f" (approved by {approved_by})" if approved_by else ""
    author = prop.get("author", "Claude")
    repo.write_file(f, data, f"{author} — {prop['reason'][:150]}{who}")
    repo.log(f"Applied {author}'s change to {f.display}{who}: {prop['reason'][:200]}", file_id=f.id)
    prop.update(status="applied", decided=now_ts(), approved_by=approved_by)
    _save_proposal(repo, prop)


# --------------------------------------------------------------------------- Claude Desktop hookup

MCP_SERVER_KEY = "sim-config-manager"


def connector_command(data_dir: Path) -> tuple[str, list[str]]:
    """Command Claude Desktop should run to start the connector."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("SimConfigManager-MCP.exe")
        return str(exe), ["--data", str(data_dir)]
    script = Path(__file__).resolve().parents[1] / "SimConfigManager-MCP.py"
    return sys.executable.replace("pythonw.exe", "python.exe"), [str(script), "--data", str(data_dir)]


def claude_desktop_config_paths() -> list[Path]:
    """Claude Desktop's config file(s): the normal install and the Microsoft Store (MSIX) install."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for pkg in glob.glob(os.path.join(local, "Packages", "Claude_*")):
            paths.append(Path(pkg) / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    if sys.platform == "darwin":
        paths.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    return paths


def claude_desktop_connected() -> bool:
    for p in claude_desktop_config_paths():
        try:
            if MCP_SERVER_KEY in json.loads(p.read_text(encoding="utf-8")).get("mcpServers", {}):
                return True
        except (OSError, ValueError):
            continue
    return False


def connect_claude_desktop(data_dir: Path, enable: bool = True) -> list[str]:
    """Add (or remove) the connector in Claude Desktop's config. Keeps every other setting; makes a backup."""
    command, args = connector_command(data_dir)
    targets = [p for p in claude_desktop_config_paths() if p.parent.is_dir()] or claude_desktop_config_paths()[:1]
    written = []
    for p in targets:
        cfg = {}
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            try:
                cfg = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise ValueError(f"{p} is not valid JSON — fix or delete it first.")
            shutil.copyfile(p, p.with_name(p.name + ".bak"))
        servers = cfg.setdefault("mcpServers", {})
        if enable:
            servers[MCP_SERVER_KEY] = {"command": command, "args": args}
        else:
            servers.pop(MCP_SERVER_KEY, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        written.append(str(p))
    return written


def files_as_markdown(repo: "Repo", files: list[TrackedFile], max_bytes: int = 200_000) -> str:
    """Selected files as one Markdown block — to paste into any Claude chat."""
    ver, status = repo.version_status()
    st = {x.file.id: x.status for x in status}
    out = [f"Config files from simulator PC **{repo.machine}** ({APP_NAME}, {fmt_ts(now_ts())})."]
    if ver:
        out.append(f"Current configuration version: {ver.label}.")
    used = 0
    for f in files:
        data = repo.read_current(f)
        out.append(f"\n### {f.display}  ({f.group})\n`{f.path}` — status vs version: "
                   f"{STATUS_TEXT.get(st.get(f.id, 'new'), '?')}")
        if data is None:
            out.append("_missing on disk_")
            continue
        if is_binary(data):
            out.append(f"_binary file, {len(data)} bytes_")
            continue
        text = detect_text(data)[0]
        if used + len(text) > max_bytes:
            out.append("_left out: size limit reached_")
            continue
        used += len(text)
        lang = "xml" if f.path.lower().endswith(".xml") else ("json" if f.path.lower().endswith(".json") else "ini")
        out.append(f"```{lang}\n{text.rstrip()}\n```")
    return "\n".join(out)


def allow_users_to_propose(data_dir: Path) -> None:
    """The connector runs as a normal user: make sure it may drop proposals into the proposals folder."""
    if sys.platform != "win32":
        return
    import subprocess
    d = Path(data_dir) / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    # S-1-5-32-545 = the local "Users" group (works in every Windows language)
    subprocess.run(["icacls", str(d), "/grant", "*S-1-5-32-545:(OI)(CI)M", "/Q"], capture_output=True,
                   **_no_window())


# =========================================================================== system information (#3)

PROBE_PREFIX = "system://"


def is_remote(repo) -> bool:
    return bool(getattr(repo, "is_remote", False))


def is_probe(f) -> bool:
    return bool(f) and str(getattr(f, "path", f)).startswith(PROBE_PREFIX)


def norm_path(path: str) -> str:
    return path.lower() if path.startswith(PROBE_PREFIX) else os.path.normcase(os.path.abspath(path))


_PS_UTF8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "

# key -> (label, PowerShell). Output must be stable (no timestamps) so a change means something changed.
BUILTIN_PROBES = {
    "gpu": ("Graphics cards & driver", r"""
Get-CimInstance Win32_VideoController | Sort-Object Name | ForEach-Object {
  "$($_.Name)"; "  Driver version : $($_.DriverVersion)"
  if ($_.DriverDate) { "  Driver date    : $($_.DriverDate.ToString('yyyy-MM-dd'))" }
  "  Video processor: $($_.VideoProcessor)" }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  ""; "nvidia-smi:"; nvidia-smi --query-gpu=name,driver_version,vbios_version,pcie.link.gen.max --format=csv,noheader }
"""),
    "windows": ("Windows version", r"""
$v = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$os = Get-CimInstance Win32_OperatingSystem
"$($os.Caption)"; "Version $($v.DisplayVersion)  build $($v.CurrentBuild).$($v.UBR)"; "Edition $($v.EditionID)"
"Architecture $($os.OSArchitecture)"
"""),
    "sim_software": ("Sim software versions", r"""
$keys = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
$pat = '__PATTERN__'
Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and $_.DisplayName -match $pat } |
  Sort-Object DisplayName -Unique | ForEach-Object { "{0}  |  {1}  |  {2}" -f $_.DisplayName, $_.DisplayVersion, $_.Publisher }
"""),
    "all_software": ("All installed software", r"""
$keys = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and -not $_.SystemComponent } |
  Sort-Object DisplayName -Unique | ForEach-Object { "{0}  |  {1}" -f $_.DisplayName, $_.DisplayVersion }
"""),
    "displays": ("Displays / projectors", r"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens | Sort-Object DeviceName | ForEach-Object {
  "{0}  {1}x{2} at {3},{4}{5}" -f $_.DeviceName, $_.Bounds.Width, $_.Bounds.Height, $_.Bounds.X, $_.Bounds.Y, $(if ($_.Primary) {'  (primary)'} else {''}) }
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object {
  $n = ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
  $m = ($_.ManufacturerName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
  "Monitor: $m $n" } | Sort-Object
"""),
    "hardware": ("CPU, memory, motherboard", r"""
$c = Get-CimInstance Win32_Processor | Select-Object -First 1
"CPU: $($c.Name.Trim()) ($($c.NumberOfCores) cores / $($c.NumberOfLogicalProcessors) threads)"
"RAM: {0:N0} GB" -f ((Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum).Sum / 1GB)
$b = Get-CimInstance Win32_BaseBoard; "Motherboard: $($b.Manufacturer) $($b.Product)"
"BIOS: $((Get-CimInstance Win32_BIOS).SMBIOSBIOSVersion)"
Get-CimInstance Win32_DiskDrive | Sort-Object Index | ForEach-Object { "Disk $($_.Index): $($_.Model) {0:N0} GB" -f ($_.Size/1GB) }
"""),
    "network": ("Network addresses", r"""
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
  Sort-Object InterfaceAlias | ForEach-Object { "{0}: {1}/{2} ({3})" -f $_.InterfaceAlias, $_.IPAddress, $_.PrefixLength, $_.PrefixOrigin }
"""),
}
DEFAULT_SIM_SOFTWARE_PATTERN = ("Prepar3D|Lockheed|ProSim|FSUIPC|SimConnect|Orbx|Active ?Sky|ChasePlane|PMDG|Aerosoft|"
                                "NVIDIA|Leo Bodnar|Saitek|Logitech|Thrustmaster|Honeycomb|VATSIM|vPilot|Navigraph|"
                                "FlyTampa|FSDreamTeam|REX|Envtex|EZdok|SODE|Immersive Display|Fly\\.?Elise|"
                                "Visual C\\+\\+|DirectX|\\.NET")


def probe_command(repo, path: str) -> Optional[list[str]]:
    key = path[len(PROBE_PREFIX):]
    if key in BUILTIN_PROBES:
        if sys.platform != "win32":
            return None
        script = BUILTIN_PROBES[key][1]
        if key == "sim_software":
            pat = repo.get("sim_software_pattern") or DEFAULT_SIM_SOFTWARE_PATTERN
            script = script.replace("__PATTERN__", pat.replace("'", "''"))
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                _PS_UTF8 + script]
    custom = custom_probes(repo).get(key)
    if not custom:
        return None
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                _PS_UTF8 + custom["command"]]
    return ["/bin/sh", "-c", custom["command"]]


def custom_probes(repo) -> dict:
    try:
        return json.loads(repo.get("custom_probes") or "{}")
    except ValueError:
        return {}


def add_custom_probe(repo, label: str, command: str, group: str = "System") -> TrackedFile:
    probes = custom_probes(repo)
    key = "custom_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
    probes[key] = {"label": label, "command": command}
    repo.set("custom_probes", json.dumps(probes))
    return repo.add_file(PROBE_PREFIX + key, label, group)


def run_probe(cmd: list[str], timeout: int = 90) -> Optional[bytes]:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.decode("utf-8", errors="replace")
    lines = [ln.rstrip() for ln in out.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    text = "\n".join(lines)
    if r.returncode != 0 and not text:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        text = f"(command failed: {err[:300]})"
    return (text + "\n").encode("utf-8")


def add_builtin_probe(repo, key: str, group: str = "System") -> TrackedFile:
    label = BUILTIN_PROBES[key][0]
    return repo.add_file(PROBE_PREFIX + key, label, group)


# =========================================================================== ignore rules + setting diffs (#1)

IGNORE_KINDS = {
    "ini_key": "CFG/INI setting (Section/Key, wildcards * allowed)",
    "ini_section": "Whole CFG/INI section (wildcards * allowed)",
    "xml": "XML element (path, e.g. .//Window or .//Launch.Addon/Name)",
    "line": "Lines matching a regular expression",
}


def kind_for_path(path: str, text: str = "") -> str:
    p = path.lower()
    if p.endswith((".xml", ".fxml", ".config")):
        return "xml"
    if p.endswith(".json"):
        return "json"
    if p.startswith(PROBE_PREFIX):
        return "text"
    if p.endswith((".cfg", ".ini", ".inf", ".dsd", ".ccp")):
        return "xml" if text.lstrip().startswith("<") else "ini"
    s = text.lstrip()
    if s.startswith("<"):
        return "xml"
    return "ini" if re.search(r"^\s*\[[^\]]+\]\s*$", text, re.M) else "text"


def normalize_content(data: Optional[bytes], path: str, rules: list[dict]) -> Optional[bytes]:
    """Content with ignored parts removed — only used to decide whether something *really* changed."""
    if data is None or not rules or is_binary(data):
        return data
    text, _fmt = detect_text(data)
    kind = kind_for_path(path, text)
    lines = text.split("\n")
    line_rx = []
    for r in rules:
        if r["kind"] == "line":
            try:
                line_rx.append(re.compile(r["pattern"]))
            except re.error:
                pass
    if kind == "ini":
        keys = [r["pattern"] for r in rules if r["kind"] == "ini_key"]
        secs = [r["pattern"].lower() for r in rules if r["kind"] == "ini_section"]
        out, section = [], ""
        for ln in lines:
            st = ln.strip()
            if st.startswith("[") and "]" in st:
                section = st[1:st.index("]")].strip()
                if any(fnmatch.fnmatch(section.lower(), p) for p in secs):
                    continue
                out.append(ln)
                continue
            if any(fnmatch.fnmatch(section.lower(), p) for p in secs):
                continue
            if "=" in st and not st.startswith((";", "#", "//")):
                k = st.split("=", 1)[0].strip()
                if any(_ini_key_match(section, k, pat) for pat in keys):
                    continue
            if any(rx.search(ln) for rx in line_rx):
                continue
            out.append(ln)
        return "\n".join(out).encode("utf-8")
    if kind == "xml":
        paths = [r["pattern"] for r in rules if r["kind"] == "xml"]
        if paths:
            try:
                root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8"))
                parent = {c: p for p in root.iter() for c in p}
                for pth in paths:
                    try:
                        for el in root.findall(pth):
                            if el in parent:
                                parent[el].remove(el)
                            else:
                                el.clear()
                    except SyntaxError:
                        continue
                text = ET.tostring(root, encoding="unicode")
                lines = text.split("\n")
            except ET.ParseError:
                pass
    return "\n".join(ln for ln in lines if not any(rx.search(ln) for rx in line_rx)).encode("utf-8")


def _ini_key_match(section: str, key: str, pattern: str) -> bool:
    if "/" in pattern:
        ps, pk = pattern.split("/", 1)
    else:
        ps, pk = "*", pattern
    return fnmatch.fnmatch(section.lower(), ps.strip().lower()) and fnmatch.fnmatch(key.lower(), pk.strip().lower())


def setting_values(data: Optional[bytes], path: str) -> Optional[dict]:
    """Flatten a config into {setting: value}. INI: 'Section/Key'. XML: 'Root/Child[2]/Name' (+ '@attr')."""
    if data is None or is_binary(data):
        return None
    text, _ = detect_text(data)
    kind = kind_for_path(path, text)
    out: dict[str, str] = {}
    if kind == "ini":
        section = ""
        for ln in text.split("\n"):
            st = ln.strip()
            if not st or st.startswith((";", "#", "//")):
                continue
            if st.startswith("[") and "]" in st:
                section = st[1:st.index("]")].strip()
                continue
            if "=" in st:
                k, v = st.split("=", 1)
                out[f"{section}/{k.strip()}"] = v.strip()
        return out
    if kind == "xml":
        try:
            root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8"))
        except ET.ParseError:
            return None

        def walk(el, prefix):
            counts: dict[str, int] = {}
            totals: dict[str, int] = {}
            for c in el:
                totals[c.tag] = totals.get(c.tag, 0) + 1
            for k, v in el.attrib.items():
                out[f"{prefix}@{k}"] = v
            txt = (el.text or "").strip()
            if txt and len(el) == 0:
                out[prefix] = txt
            for c in el:
                if not isinstance(c.tag, str):
                    continue
                counts[c.tag] = counts.get(c.tag, 0) + 1
                name = c.tag + (f"[{counts[c.tag]}]" if totals[c.tag] > 1 else "")
                walk(c, f"{prefix}/{name}")
        walk(root, root.tag)
        return out
    return None


def setting_changes(old: Optional[bytes], new: Optional[bytes], path: str) -> Optional[list[dict]]:
    """Readable per-setting changes, or None if the file type has no settings structure."""
    a, b = setting_values(old, path), setting_values(new, path)
    if a is None and b is None:
        return None
    a, b = a or {}, b or {}
    sample = new if new is not None else old
    ftype = kind_for_path(path, detect_text(sample)[0] if sample else "")
    out = []
    for k in sorted(set(a) | set(b), key=lambda x: (x.split("/")[0], x)):
        if a.get(k) != b.get(k):
            kind = "added" if k not in a else ("removed" if k not in b else "changed")
            out.append({"setting": k, "old": a.get(k), "new": b.get(k), "kind": kind, "ftype": ftype})
    return out


def describe_change(c: dict) -> str:
    s = c["setting"]
    if c.get("ftype", "ini") == "ini" and "/" in s:
        sec, key = s.split("/", 1)
        label = f"[{sec}] {key}" if sec else key
    else:
        label = s
    if c["kind"] == "added":
        return f"{label}: added = {c['new']}"
    if c["kind"] == "removed":
        return f"{label}: removed (was {c['old']})"
    return f"{label}: {c['old']} → {c['new']}"


# =========================================================================== rulebook (#6)

def _rb_row(r) -> dict:
    return dict(r)


def rulebook(repo) -> list[dict]:
    return [_rb_row(r) for r in repo.db.execute("SELECT * FROM rulebook ORDER BY file_id, selector")]


def save_rulebook_entry(repo, entry: dict) -> dict:
    if entry.get("id"):
        repo.db.execute("UPDATE rulebook SET file_id=?, selector=?, required=?, note=?, author=? WHERE id=?",
                        (entry.get("file_id"), entry["selector"], entry.get("required") or "", entry.get("note", ""),
                         entry.get("author", ""), entry["id"]))
    else:
        cur = repo.db.execute("INSERT INTO rulebook(file_id, selector, required, note, author, created) "
                              "VALUES(?,?,?,?,?,?)", (entry.get("file_id"), entry["selector"],
                                                      entry.get("required") or "", entry.get("note", ""),
                                                      entry.get("author", ""), now_ts()))
        entry["id"] = cur.lastrowid
    repo.db.commit()
    f = repo.file(entry["file_id"]) if entry.get("file_id") else None
    repo.log(f"Rulebook: {f.display + ' ' if f else ''}{entry['selector']}"
             + (f" must be {entry['required']}" if entry.get("required") else " (note)"))
    return entry


def delete_rulebook_entry(repo, entry_id: int) -> None:
    repo.db.execute("DELETE FROM rulebook WHERE id=?", (entry_id,))
    repo.db.commit()


def rule_value(data: Optional[bytes], path: str, selector: str) -> Optional[str]:
    """Current value of one setting. INI: 'Section/Key' (case-insensitive). XML: ElementTree path (@attr)."""
    if data is None:
        return None
    text, _ = detect_text(data)
    kind = kind_for_path(path, text)
    if kind == "ini":
        sec, key = selector.split("/", 1) if "/" in selector else ("", selector)
        return parse_ini(text).get((sec.strip().lower(), key.strip().lower()))
    if kind == "xml":
        vals = xml_values(data, selector)
        if not vals:
            return None
        return vals[0] if len(vals) == 1 else ", ".join(str(v) for v in vals)
    return None


def rulebook_status(repo, overrides: Optional[dict] = None) -> list[dict]:
    """Every rulebook entry with the live value and whether it complies. overrides: {file_id: bytes}."""
    out = []
    cache: dict[int, Optional[bytes]] = {}
    for e in rulebook(repo):
        f = repo.file(e["file_id"]) if e["file_id"] else None
        cur = None
        if f:
            if overrides and f.id in overrides:
                data = overrides[f.id]
            else:
                if f.id not in cache:
                    cache[f.id] = repo.read_current(f)
                data = cache[f.id]
            cur = rule_value(data, f.path, e["selector"])
        ok = None
        if e["required"]:
            ok = cur is not None and str(cur).strip() == e["required"].strip()
        out.append(dict(e, file=f.display if f else "", label=(f"{f.display}: " if f else "") + e["selector"],
                        current=cur, ok=ok))
    return out


def rulebook_violations_for(repo, f: TrackedFile, new_data: bytes) -> list[dict]:
    """Rulebook requirements that new_data for file f would break."""
    return [e for e in rulebook_status(repo, {f.id: new_data}) if e["file_id"] == f.id and e["ok"] is False]


for _fn in (rulebook, save_rulebook_entry, delete_rulebook_entry, rulebook_status):
    setattr(Repo, _fn.__name__, _fn)


# =========================================================================== keep startup / Claude paths current

def autostart_command() -> Optional[str]:
    """The program the start-at-logon task runs (None if no task)."""
    if sys.platform != "win32":
        return None
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], capture_output=True, **_no_window())
    if r.returncode != 0:
        return None
    text = r.stdout.decode("utf-16", errors="replace") if r.stdout[:2] in (b"\xff\xfe", b"\xfe\xff") \
        else r.stdout.decode(errors="replace")
    m = re.search(r"<Command>(.*?)</Command>", text, re.S)
    return m.group(1).strip() if m else None


def repair_paths_after_move(data_dir: Path) -> list[str]:
    """After installing/updating into another folder, point the logon task and Claude Desktop at this exe."""
    fixed = []
    if not getattr(sys, "frozen", False):
        return fixed
    cmd = autostart_command()
    if cmd and os.path.normcase(cmd) != os.path.normcase(sys.executable):
        set_autostart(True)
        fixed.append("start-at-logon task")
    want, _args = connector_command(data_dir)
    for p in claude_desktop_config_paths():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cur = cfg.get("mcpServers", {}).get(MCP_SERVER_KEY, {}).get("command")
        if cur and os.path.normcase(cur) != os.path.normcase(want):
            connect_claude_desktop(data_dir, True)
            fixed.append("Claude Desktop connector")
            break
    return fixed



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
    from . import storage
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


# =========================================================================== central dashboard (optional hub)

def build_hub_report(repo) -> dict:
    """Status sent to the optional SimConfigManager Hub (web dashboard). Read-only summary, no file contents."""
    import platform
    st = build_status(repo)
    ver, status = repo.version_status()
    st["files_status"] = [{"file": x.file.display, "group": x.file.group, "status": x.status,
                           "status_text": STATUS_TEXT.get(x.status, x.status)} for x in status]
    st["rulebook"] = [{"label": b["label"], "required": b["required"], "current": b["current"], "ok": b["ok"],
                       "note": b["note"]} for b in repo.rulebook_status()]
    st["versions"] = [{"number": v.number, "title": v.title, "created_at": v.created_at, "created_by": v.created_by}
                      for v in repo.versions()[:15]]
    st["restore_points"] = [r.name for r in repo.restore_points()]
    st["events"] = [{"ts": e["ts"], "level": e["level"], "message": e["message"]} for e in repo.events(30)]
    st["host"] = {"hostname": socket.gethostname(), "os": platform.platform(), "admin": is_admin()}
    return st


def hub_push(repo, url: Optional[str] = None, key: Optional[str] = None, timeout: float = 20) -> dict:
    """Send the report to the hub. Raises with a readable message on failure."""
    import urllib.error
    import urllib.request
    url = (url or repo.get("hub_url") or "").strip().rstrip("/")
    key = (key or repo.get("hub_key") or "").strip()
    if not url or not key:
        raise ValueError("Fill in the hub address and key.")
    if "://" not in url:
        url = "https://" + url
    body = json.dumps(build_hub_report(repo), default=str).encode("utf-8")
    req = urllib.request.Request(url + "/api/v1/report", data=body, method="POST", headers={
        "Content-Type": "application/json", "Authorization": f"Bearer {key}",
        "User-Agent": f"{APP_NAME}/{APP_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error", "")
        except ValueError:
            msg = ""
        raise ValueError(f"The hub answered {e.code}: {msg or e.reason}")
    except (urllib.error.URLError, OSError) as e:
        raise ValueError(f"Cannot reach the hub at {url} ({getattr(e, 'reason', e)}).")


def hub_signature(repo) -> tuple:
    """Changes when anything worth reporting changed (new snapshot or event)."""
    snap = repo.latest_snapshot()
    ev = repo.events(1)
    return (snap.id if snap else 0, ev[0]["id"] if ev else 0)


for _n, _f in (("build_hub_report", build_hub_report), ("hub_signature", hub_signature)):
    setattr(Repo, _n, _f)
