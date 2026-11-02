"""
Claude connector for SimConfigManager — a Model Context Protocol (MCP) server over stdio.

Lets Claude (Claude Desktop on the sim PC) read the tracked config files, their history, versions and the
other sims' files from the sync folder, and *propose* edits. It never writes a config file itself:
proposals wait in the main app until a person approves them.

Run:   SimConfigManager-MCP.exe            (built exe)
       python -m simconfigmanager.mcp_server [--data DIR]
"""
from __future__ import annotations

import json
import re
import sys
import threading
import traceback
from typing import Any, Optional

from . import core
from .core import APP_NAME, APP_VERSION, FolderSync, Repo, TrackedFile

SERVER_NAME = "sim-config-manager"
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Read-only access to the flight-simulator configuration files tracked by SimConfigManager (Prepar3D "
    "cfg/xml, ProSim config.xml, etc.), their full change history, numbered configuration versions, and the "
    "other simulators' files via the sync folder. Start with `overview`. Use `read_file` to read a file "
    "(with line numbers), `search` to find settings across all files, `diff` to see what changed. "
    "To change a file use `propose_change`: the change is NOT applied until a person approves it in the "
    "SimConfigManager app, so explain clearly why in `reason`. Prepar3D rewrites Prepar3D.cfg when it exits, "
    "so changes to it should be applied with the sim closed."
)


class ToolError(Exception):
    pass


# who is calling (set per request): local Claude Desktop, or a named access key over the network
CTX = threading.local()


def ctx_author() -> str:
    return getattr(CTX, "author", "Claude")


def ctx_may_propose() -> bool:
    return getattr(CTX, "may_propose", True)


# --------------------------------------------------------------------------- helpers

def resolve_file(repo: Repo, ref: Any) -> TrackedFile:
    """Find a tracked file by id, name/label, file name, or a unique part of its path."""
    files = repo.files()
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        f = repo.file(int(ref))
        if f and f.active:
            return f
    s = str(ref).strip().lower()
    for test in (lambda f: f.display.lower() == s,
                 lambda f: f.path.lower() == s,
                 lambda f: f.path.replace("\\", "/").split("/")[-1].lower() == s,
                 lambda f: s in f.display.lower() or s in f.path.lower()):
        hits = [f for f in files if test(f)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ToolError("More than one file matches '%s' — use the id:\n%s" % (
                ref, "\n".join(f"  id {f.id}: {f.display} [{f.group}] {f.path}" for f in hits)))
    raise ToolError(f"No tracked file matches '{ref}'. Use list_files to see what is tracked.")


def remote(repo: Repo) -> FolderSync:
    folder = repo.get("sync_folder")
    if not folder:
        raise ToolError("No sync folder is set up in SimConfigManager, so other sims can't be read.")
    s = FolderSync(repo, folder)
    if not s.ok:
        raise ToolError(f"The sync folder is not reachable right now: {folder}")
    return s


def remote_file(repo: Repo, machine: str, ref: Any) -> tuple[dict, Optional[bytes]]:
    s = remote(repo)
    names = [m["machine"] for m in s.machines()]
    match = next((n for n in names if n.lower() == str(machine).lower()), None)
    if not match:
        raise ToolError(f"No sim called '{machine}' in the sync folder. Known: {', '.join(names) or 'none'}")
    files = [f for f in s.remote_latest(match) if f.get("active", True)]
    r = str(ref).lower()
    hits = [f for f in files if (f.get("label") or "").lower() == r] or \
           [f for f in files if f["path"].replace("\\", "/").split("/")[-1].lower() == r] or \
           [f for f in files if r in (f.get("label") or "").lower() or r in f["path"].lower()]
    if not hits:
        raise ToolError(f"No file matching '{ref}' on {match}.")
    if len(hits) > 1:
        raise ToolError("Several files match on %s: %s" % (match, ", ".join(f.get("label") or f["path"]
                                                                          for f in hits)))
    f = hits[0]
    return f, (s.fetch(f["hash"]) if f["hash"] else None)


def version_bytes(repo: Repo, f: TrackedFile, spec: Optional[str]) -> tuple[Optional[bytes], str]:
    """spec: current (default) | vN | #snapshot | restore point name."""
    spec = (spec or "current").strip()
    if spec.lower() in ("current", "now", "disk", ""):
        return repo.read_current(f), "current file on disk"
    m = re.fullmatch(r"[vV](\d+)", spec)
    if m:
        v = repo.version(int(m.group(1)))
        if not v:
            raise ToolError(f"There is no version {spec}.")
        ents = repo.entries(v.snapshot_id)
        if f.id not in ents:
            raise ToolError(f"{f.display} was not tracked yet in {v.label}.")
        return repo.get_object(ents[f.id]), v.label
    m = re.fullmatch(r"#?(\d+)", spec)
    if m:
        ents = repo.entries(int(m.group(1)))
        if f.id not in ents:
            raise ToolError(f"Snapshot #{m.group(1)} has no copy of {f.display}.")
        return repo.get_object(ents[f.id]), f"snapshot #{m.group(1)}"
    rp = repo.restore_point(spec)
    if rp:
        ents = repo.entries(rp.snapshot_id)
        if f.id not in ents:
            raise ToolError(f"Restore point '{rp.name}' has no copy of {f.display}.")
        return repo.get_object(ents[f.id]), f"restore point '{rp.name}'"
    raise ToolError(f"Unknown version '{spec}'. Use current, v3, #42 or a restore point name.")


def numbered(text: str, offset: int = 1, limit: int = 400) -> tuple[str, int]:
    lines = text.split("\n")
    total = len(lines)
    start = max(1, offset)
    chunk = lines[start - 1:start - 1 + limit]
    width = len(str(start + len(chunk)))
    body = "\n".join(f"{n:>{width}}| {ln}" for n, ln in enumerate(chunk, start))
    return body, total


def as_text(data: Optional[bytes], label: str) -> str:
    if data is None:
        raise ToolError(f"{label}: the file does not exist.")
    if core.is_binary(data):
        raise ToolError(f"{label} is a binary file ({len(data)} bytes) and can't be shown as text.")
    return core.detect_text(data)[0]


# --------------------------------------------------------------------------- tools

def t_overview(repo: Repo, a: dict) -> str:
    ver, status = repo.version_status()
    files = repo.files()
    out = [f"{APP_NAME} {APP_VERSION} on PC '{repo.machine}' — {len(files)} tracked files"]
    groups: dict[str, int] = {}
    for f in files:
        groups[f.group or "(no group)"] = groups.get(f.group or "(no group)", 0) + 1
    out.append("Groups: " + ", ".join(f"{g} ({n})" for g, n in sorted(groups.items())))
    if ver:
        mism = [x for x in status if x.status in ("modified", "missing", "new")]
        out.append(f"Current configuration version: {ver.label} ({core.fmt_ts(ver.created_at)}, by "
                   f"{ver.created_by or '-'})")
        if mism:
            out.append(f"{len(mism)} file(s) differ from it:")
            out += [f"  - {x.file.display} [{x.file.group}]: {core.STATUS_TEXT[x.status]}" for x in mism]
        else:
            out.append("All files match that version.")
    else:
        out.append("No configuration version has been created yet.")
    rps = repo.restore_points()
    if rps:
        out.append("Restore points: " + ", ".join(r.name for r in rps))
    alerts = [e for e in repo.events(200) if e["level"] in ("alert", "warning")][:8]
    if alerts:
        out.append("Recent alerts:")
        out += [f"  {core.fmt_ts(e['ts'])}  {e['message']}" for e in alerts]
    if repo.get("sync_folder"):
        try:
            others = [m["machine"] for m in remote(repo).machines() if m["machine"] != repo.machine]
            if others:
                out.append("Other sims readable via the sync folder: " + ", ".join(others))
        except ToolError as e:
            out.append(str(e))
    pend = core.list_proposals(repo, "pending")
    if pend:
        out.append(f"{len(pend)} proposed change(s) are waiting for approval in the app.")
    return "\n".join(out)


def t_list_files(repo: Repo, a: dict) -> str:
    if a.get("machine") and str(a["machine"]).lower() != repo.machine.lower():
        s = remote(repo)
        names = [x["machine"] for x in s.machines()]
        name = next((n for n in names if n.lower() == str(a["machine"]).lower()), None)
        if not name:
            raise ToolError(f"No sim called '{a['machine']}' in the sync folder. Known: {', '.join(names) or 'none'}")
        files = [f for f in s.remote_latest(name) if f.get("active", True)]
        return "\n".join(f"{f.get('label')}  [{f.get('group', '')}]  {f['path']}" +
                         ("  (missing)" if not f["hash"] else "") for f in files) or "No files."
    _ver, status = repo.version_status()
    st = {x.file.id: x.status for x in status}
    rows = []
    for f in repo.files():
        data = repo.read_current(f)
        size = f"{len(data)} bytes" if data is not None else "MISSING"
        rows.append(f"id {f.id}: {f.display}  [{f.group}]  {size}  status vs version: "
                    f"{core.STATUS_TEXT.get(st.get(f.id, 'new'), '?')}\n        {f.path}")
    return "\n".join(rows) or "No files are tracked yet."


def t_read_file(repo: Repo, a: dict) -> str:
    offset, limit = int(a.get("offset") or 1), min(int(a.get("limit") or 400), 2000)
    if a.get("machine") and str(a["machine"]).lower() != repo.machine.lower():
        info, data = remote_file(repo, a["machine"], a["file"])
        text = as_text(data, info.get("label") or info["path"])
        head = f"{info.get('label')} on {a['machine']} — {info['path']} (latest synced version)"
    else:
        f = resolve_file(repo, a["file"])
        data, which = version_bytes(repo, f, a.get("version"))
        text = as_text(data, f.display)
        _, fmt = core.detect_text(data)
        head = (f"{f.display} [{f.group}] — {f.path}\n{which}; encoding {fmt.encoding}"
                f"{' BOM' if fmt.bom else ''}, {'CRLF' if fmt.newline == chr(13) + chr(10) else 'LF'}")
    body, total = numbered(text, offset, limit)
    end = min(total, offset + limit - 1)
    more = f"\n… lines {end + 1}-{total} not shown (use offset={end + 1})" if end < total else ""
    return f"{head}\nlines {offset}-{end} of {total}\n\n{body}{more}"


def t_search(repo: Repo, a: dict) -> str:
    try:
        rx = re.compile(a["pattern"], 0 if a.get("case_sensitive") else re.IGNORECASE)
    except re.error as e:
        raise ToolError(f"Invalid regular expression: {e}")
    files = repo.files()
    if a.get("file"):
        files = [resolve_file(repo, a["file"])]
    if a.get("group"):
        files = [f for f in files if a["group"].lower() in f.group.lower()]
    hits, limit = [], 300
    for f in files:
        data = repo.read_current(f)
        if data is None or core.is_binary(data):
            continue
        section = ""
        for n, ln in enumerate(core.detect_text(data)[0].split("\n"), 1):
            st = ln.strip()
            if st.startswith("[") and "]" in st:
                section = st
            if rx.search(ln):
                ctx = f" {section}" if section and not st.startswith("[") else ""
                hits.append(f"{f.display}:{n}{ctx}  {ln.strip()[:200]}")
                if len(hits) >= limit:
                    return "\n".join(hits) + f"\n… stopped after {limit} matches — narrow the search."
    return "\n".join(hits) or "No matches."


def t_diff(repo: Repo, a: dict) -> str:
    f = resolve_file(repo, a["file"])
    latest = repo.latest_version()
    frm = a.get("from") or (f"v{latest.number}" if latest else None)
    if frm is None:
        hist = repo.file_history(f.id)
        if len(hist) < 2:
            return "No earlier version to compare with."
        frm = f"#{hist[1].snapshot_id}"
    old, lo = version_bytes(repo, f, frm)
    new, ln = version_bytes(repo, f, a.get("to") or "current")
    d = core.unified_diff(old, new, lo, ln)
    return d if d.strip() else f"No differences between {lo} and {ln}."


def t_file_history(repo: Repo, a: dict) -> str:
    f = resolve_file(repo, a["file"])
    hist = repo.file_history(f.id)
    lim = int(a.get("limit") or 30)
    rows = []
    for i, v in enumerate(hist[:lim]):
        prev = hist[i + 1].hash if i + 1 < len(hist) else None
        try:
            ad, rm = core.diff_stats(repo.get_object(prev), repo.get_object(v.hash))
            pm = f"+{ad}/-{rm}"
        except FileNotFoundError:
            pm = "?"
        rows.append(f"#{v.snapshot_id}  {core.fmt_ts(v.created_at)}  {pm:>9}  "
                    f"{'(deleted) ' if v.hash is None else ''}{v.message}")
    tags = [f"{x.label} = #{x.snapshot_id}" for x in repo.versions()]
    return (f"History of {f.display} ({len(hist)} versions, newest first). Use '#<n>' as a version in read_file "
            f"or diff.\n" + "\n".join(rows) + ("\n\nConfiguration versions: " + ", ".join(tags) if tags else ""))


def t_list_versions(repo: Repo, a: dict) -> str:
    vers = repo.versions()
    if not vers:
        return "No configuration versions yet."
    out = []
    for i, v in enumerate(vers):
        prev = vers[i + 1] if i + 1 < len(vers) else None
        ch = repo.changes_between(prev.snapshot_id if prev else None, v.snapshot_id)
        out.append(f"{v.label} — {core.fmt_ts(v.created_at)} by {v.created_by or '-'} on {v.machine}; "
                   f"{len(ch)} file(s) {'recorded' if not prev else 'changed'}")
        if v.notes:
            out += ["    " + ln for ln in v.notes.splitlines()]
    return "\n".join(out)


def t_version_status(repo: Repo, a: dict) -> str:
    num = a.get("version")
    if isinstance(num, str):
        num = int(num.lstrip("vV"))
    ver, status = repo.version_status(num)
    if not ver:
        return "No configuration version exists yet."
    rows = [f"Files on disk compared with {ver.label}:"]
    for x in sorted(status, key=lambda x: (x.status == "match", x.file.display)):
        rows.append(f"  {core.STATUS_TEXT.get(x.status, x.status):<22} {x.file.display} [{x.file.group}]")
    return "\n".join(rows)


def t_recent_events(repo: Repo, a: dict) -> str:
    ev = repo.events(int(a.get("limit") or 40))
    return "\n".join(f"{core.fmt_ts(e['ts'])}  {e['level']:<7} {e['message']}" for e in ev) or "No events."


def t_list_other_sims(repo: Repo, a: dict) -> str:
    ms = remote(repo).machines()
    return "\n".join(f"{m['machine']}{' (this PC)' if m['machine'] == repo.machine else ''}: {m['files']} files, "
                     f"last upload {core.fmt_ts(m['updated'])}" for m in ms) or "Nothing in the sync folder yet."


def t_compare_with_other_sim(repo: Repo, a: dict) -> str:
    f = resolve_file(repo, a["file"])
    info, theirs = remote_file(repo, a["machine"], a.get("other_file") or f.display)
    d = core.unified_diff(theirs, repo.read_current(f), f"{a['machine']}: {info['path']}",
                          f"{repo.machine}: {f.path}")
    return d if d.strip() else f"{f.display} is identical on {repo.machine} and {a['machine']}."


def t_propose_change(repo: Repo, a: dict) -> str:
    if repo.get("claude_proposals") == "0" or not ctx_may_propose():
        raise ToolError("Proposing changes is switched off in SimConfigManager → Settings. Describe the change "
                        "to the user instead so they can make it in the editor.")
    f = resolve_file(repo, a["file"])
    reason = (a.get("reason") or "").strip()
    if not reason:
        raise ToolError("Please give a reason — it is shown to the person who approves the change.")
    cur = repo.read_current(f)
    if cur is not None and core.is_binary(cur):
        raise ToolError("Binary files can't be changed this way.")
    text, fmt = core.detect_text(cur)
    if a.get("new_content") is not None:
        new_text = str(a["new_content"]).replace("\r\n", "\n")
    else:
        reps = a.get("replacements") or []
        if not reps:
            raise ToolError("Give either 'replacements' (preferred) or 'new_content'.")
        new_text = text
        for i, r in enumerate(reps, 1):
            find, repl = str(r.get("find", "")).replace("\r\n", "\n"), str(r.get("replace", "")).replace("\r\n", "\n")
            if not find:
                raise ToolError(f"Replacement {i}: 'find' is empty.")
            n = new_text.count(find)
            if n == 0:
                raise ToolError(f"Replacement {i}: the text to find was not found exactly. Read the file again "
                                f"and copy the exact text (without line numbers).")
            if n > 1 and not r.get("all"):
                raise ToolError(f"Replacement {i}: the text occurs {n} times — include more surrounding text to "
                                f"make it unique, or set all=true.")
            new_text = new_text.replace(find, repl)
    if new_text == text:
        raise ToolError("That change would leave the file exactly as it is.")
    kind = "xml" if f.path.lower().endswith(".xml") else ("json" if f.path.lower().endswith(".json") else
                                                          ("ini" if f.path.lower().endswith((".cfg", ".ini")) else ""))
    probs = core.validate_text(new_text, kind) if kind else []
    if kind == "xml" and probs:
        raise ToolError(f"The result would not be valid XML: {probs[0][1]} (line {probs[0][0]}). Nothing proposed.")
    try:
        new_data = core.encode_text(new_text, fmt)
    except ValueError as e:
        raise ToolError(str(e))
    try:
        prop = core.create_proposal(repo, f, new_data, reason, author=ctx_author())
    except OSError as e:
        raise ToolError(f"Could not save the proposal ({e}). Start SimConfigManager once as administrator so it "
                        f"can set up its proposals folder, then try again.")
    diff = core.unified_diff(cur, new_data, "current", "proposed")
    warn = "\nNote: " + "; ".join(p[1] for p in probs[:3]) if probs else ""
    return (f"Proposal {prop['id']} created for {f.display}. It has NOT been applied: the user must click Apply "
            f"in SimConfigManager (a notification is shown). Tell the user it is waiting for them.{warn}\n\n{diff}")


def t_list_proposals(repo: Repo, a: dict) -> str:
    props = core.list_proposals(repo)[-30:]
    return "\n".join(f"{p['id']}  {p['status']:<9} {p['label']}: {p['reason'][:120]}"
                     + (f"\n    reviewer's note: {p['note']}" if p.get("note") else "")
                     for p in reversed(props)) or "No proposals yet."


FILE = {"type": ["string", "integer"], "description": "File id, name (e.g. 'Prepar3D.cfg', 'ProSim config.xml') "
                                                      "or a unique part of its path"}
VERSION = {"type": "string", "description": "'current' (default), a configuration version like 'v3', a snapshot "
                                            "like '#42', or a restore point name"}

TOOLS = {
    "overview": (t_overview, "Start here: this sim PC, tracked file groups, current configuration version and "
                             "which files differ from it, recent alerts, other sims, pending proposals.", {}, []),
    "list_files": (t_list_files, "List tracked config files with id, group, size, path and status versus the "
                                 "current version. With 'machine', lists another sim's files from the sync folder.",
                   {"machine": {"type": "string", "description": "Another sim's PC name (optional)"}}, []),
    "read_file": (t_read_file, "Read a config file with line numbers — now, or as it was in a version/snapshot/"
                               "restore point, or on another sim. Long files are paged (offset/limit).",
                  {"file": FILE, "version": VERSION,
                   "machine": {"type": "string", "description": "Read the file from another sim (optional)"},
                   "offset": {"type": "integer", "description": "First line (default 1)"},
                   "limit": {"type": "integer", "description": "Number of lines (default 400, max 2000)"}},
                  ["file"]),
    "search": (t_search, "Regex search across the current contents of all tracked files (or one file/group). "
                         "Shows file:line and the [section] for cfg files. Great for finding a setting.",
               {"pattern": {"type": "string", "description": "Regular expression, case-insensitive by default"},
                "file": FILE, "group": {"type": "string", "description": "Only files whose group contains this"},
                "case_sensitive": {"type": "boolean"}}, ["pattern"]),
    "diff": (t_diff, "Unified diff of one file between two versions. Defaults: from = current configuration "
                     "version, to = the file on disk now.",
             {"file": FILE, "from": VERSION, "to": VERSION}, ["file"]),
    "file_history": (t_file_history, "Every recorded version of one file with time, size of change and note.",
                     {"file": FILE, "limit": {"type": "integer"}}, ["file"]),
    "list_versions": (t_list_versions, "All numbered configuration versions with their release notes.", {}, []),
    "version_status": (t_version_status, "Which files match / differ from a configuration version (default latest).",
                       {"version": {"type": ["integer", "string"], "description": "e.g. 3 or 'v3'"}}, []),
    "recent_events": (t_recent_events, "Recent events: alerts, restores, edits, versions created.",
                      {"limit": {"type": "integer"}}, []),
    "list_other_sims": (t_list_other_sims, "Other simulator PCs whose configs are available via the sync folder.",
                        {}, []),
    "compare_with_other_sim": (t_compare_with_other_sim, "Diff a file on this PC against the same file on another "
                                                         "sim (latest synced version).",
                               {"file": FILE, "machine": {"type": "string"},
                                "other_file": {"type": "string", "description": "Name on the other sim if different"}},
                               ["file", "machine"]),
    "propose_change": (t_propose_change, "Propose an edit to a tracked file. NOT applied until a person approves "
                                         "it in the app. Prefer 'replacements' (exact text find/replace, copied "
                                         "from read_file without the line numbers). Encoding and line endings "
                                         "are kept. Returns the diff.",
                       {"file": FILE,
                        "reason": {"type": "string", "description": "Why — shown to the approver"},
                        "replacements": {"type": "array", "items": {
                            "type": "object", "properties": {
                                "find": {"type": "string"}, "replace": {"type": "string"},
                                "all": {"type": "boolean", "description": "Replace every occurrence"}},
                            "required": ["find", "replace"]}},
                        "new_content": {"type": "string", "description": "Whole new file content (only if a "
                                                                         "rewrite is really needed)"}},
                       ["file", "reason"]),
    "list_proposals": (t_list_proposals, "Proposed changes and whether they were applied or rejected.", {}, []),
}
READ_ONLY = {k for k in TOOLS if k != "propose_change"}


def tool_list() -> list[dict]:
    out = []
    for name, (_fn, desc, props, req) in TOOLS.items():
        out.append({"name": name, "description": desc,
                    "inputSchema": {"type": "object", "properties": props, "required": req},
                    "annotations": {"readOnlyHint": name in READ_ONLY, "openWorldHint": False}})
    return out


# --------------------------------------------------------------------------- JSON-RPC over stdio

class Server:
    def __init__(self, data_dir: Optional[str] = None, author: str = "Claude", may_propose: bool = True):
        self.data_dir = data_dir
        self.author = author
        self.may_propose = may_propose
        self._repo: Optional[Repo] = None

    def repo(self) -> Repo:
        if self._repo is None:
            try:
                self._repo = Repo(self.data_dir, read_only=True)
            except Exception as e:  # noqa: BLE001
                raise ToolError(str(e))
        return self._repo

    def handle(self, msg: dict) -> Optional[dict]:
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        if mid is None:  # notification (e.g. notifications/initialized)
            return None
        try:
            if method == "initialize":
                want = params.get("protocolVersion")
                result = {"protocolVersion": want if want in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0],
                          "capabilities": {"tools": {"listChanged": False}},
                          "serverInfo": {"name": SERVER_NAME, "title": "Sim Config Manager",
                                         "version": APP_VERSION},
                          "instructions": INSTRUCTIONS}
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": tool_list()}
            elif method == "tools/call":
                name = params.get("name")
                if name not in TOOLS:
                    return {"jsonrpc": "2.0", "id": mid,
                            "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
                CTX.author, CTX.may_propose = self.author, self.may_propose
                try:
                    text = TOOLS[name][0](self.repo(), params.get("arguments") or {})
                    result = {"content": [{"type": "text", "text": text}], "isError": False}
                except ToolError as e:
                    result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
                except Exception as e:  # noqa: BLE001
                    result = {"content": [{"type": "text", "text": f"Internal error: {e}\n"
                                                                    f"{traceback.format_exc(limit=3)}"}],
                              "isError": True}
            elif method in ("resources/list", "prompts/list"):
                result = {method.split("/")[0]: []}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Unknown method {method}"}}
        except Exception as e:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def serve(self, inp=None, out=None):
        inp = inp or sys.stdin.buffer
        out = out or sys.stdout.buffer
        for raw in inp:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                resp = [r for r in (self.handle(m) for m in msg) if r] if isinstance(msg, list) else self.handle(msg)
            if resp:
                out.write(json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n")
                out.flush()


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="SimConfigManager connector for Claude (MCP over stdio)")
    ap.add_argument("--data", default=None, help="data folder (default: the app's data folder)")
    ap.add_argument("--remote", default=None, metavar="URL",
                    help="relay to a sim PC on the network, e.g. http://192.168.1.20:8765/mcp")
    ap.add_argument("--key", default=None, help="access key for --remote (from the sim PC's Claude tab)")
    args = ap.parse_args(argv)
    # Nothing but protocol messages may go to stdout.
    real_out = sys.stdout.buffer
    sys.stdout = sys.stderr
    if args.remote:
        from .net import relay
        relay(args.remote, args.key or "", sys.stdin.buffer, real_out)
    else:
        Server(args.data).serve(sys.stdin.buffer, real_out)


if __name__ == "__main__":
    main()
