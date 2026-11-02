"""
Who changed it (feature #3): which program and which Windows user wrote a tracked file.

Windows can record every write to a file in the Security event log (event 4663), but only when
*object access auditing* is on and the file has an audit entry (SACL). ``enable_file_auditing`` switches both
on for the tracked files (needs administrator rights, which the app has). After a change is detected, the app
looks up the matching 4663 events and stores "changed by <program> (<user>)" next to that revision.
"""
from __future__ import annotations

import os
import re
from typing import Optional

from . import winsys
from .probes import is_probe
from .repo import repo_method, rows, schema
from .util import now_ts

schema("""
CREATE TABLE IF NOT EXISTS change_actors (
    file_id INTEGER, hash TEXT, ts REAL, process TEXT, user TEXT, PRIMARY KEY (file_id, hash));
""")

_WRITE_MASK = 0x2 | 0x4 | 0x10000 | 0x100   # WriteData, AppendData, DELETE, WriteAttributes


@repo_method
def enable_file_auditing(repo) -> dict:
    """Turn on File System auditing and add a write-audit entry to every tracked file. Returns a report."""
    paths = [f.path for f in repo.files() if not is_probe(f) and os.path.exists(f.path)]
    if not paths:
        return {"ok": False, "message": "There are no tracked files on disk yet."}
    script = (
        "$ErrorActionPreference='Continue'; "
        "auditpol /set /subcategory:'{0CCE921D-69AE-11D9-BED3-505054503030}' /success:enable | Out-Null; "
        "$rule = New-Object System.Security.AccessControl.FileSystemAuditRule('Everyone', "
        "'WriteData,AppendData,Delete,WriteAttributes', 'None', 'None', 'Success'); "
        "$ok = 0; $bad = @(); foreach ($p in @(" + ",".join(winsys.ps_quote(p) for p in paths) + ")) { "
        "try { $acl = Get-Acl -LiteralPath $p -Audit; $acl.AddAuditRule($rule); "
        "Set-Acl -LiteralPath $p -AclObject $acl -ErrorAction Stop; $ok++ } catch { $bad += $p } }; "
        "[pscustomobject]@{ok=$ok; failed=$bad}")
    res = winsys.powershell_json("enable_audit", script, 120, paths)
    if not res:
        return {"ok": False, "message": "Auditing could only be switched on on Windows, with administrator rights."}
    r = res[0]
    failed = r.get("failed") or []
    if isinstance(failed, str):
        failed = [failed]
    repo.set("audit_enabled", "1")
    repo.set("audit_files", str(len(paths)))
    repo.log(f"'Who changed it' switched on for {r.get('ok', 0)} file(s)"
             + (f"; {len(failed)} failed" if failed else ""))
    return {"ok": True, "files": r.get("ok", 0), "failed": failed}


def query_writers(path: str, since: float, until: float) -> list[dict]:
    """4663 events for one file between two times: [{ts, process, user, mask}] (newest last)."""
    xp = (f"*[System[(EventID=4663) and TimeCreated[@SystemTime>='{winsys._iso(since)}' and "
          f"@SystemTime<='{winsys._iso(until)}']]] and *[EventData[Data[@Name='ObjectName']="
          f"'{path.replace(chr(39), '&apos;')}']]")
    script = (f"Get-WinEvent -LogName Security -FilterXPath \"{xp}\" -ErrorAction SilentlyContinue | "
              "ForEach-Object { $x = [xml]$_.ToXml(); $d = @{}; $x.Event.EventData.Data | ForEach-Object "
              "{ $d[$_.Name] = $_.'#text' }; [pscustomobject]@{ts=([DateTimeOffset]$_.TimeCreated)."
              "ToUnixTimeSeconds(); process=$d['ProcessName']; user=($d['SubjectDomainName'] + '\\' + "
              "$d['SubjectUserName']); mask=$d['AccessMask']} }")
    out = winsys.powershell_json("writers", script, 60, path, since, until) or []
    good = []
    for e in out:
        try:
            mask = int(str(e.get("mask") or "0"), 0)
        except ValueError:
            mask = _WRITE_MASK
        if mask & _WRITE_MASK:
            good.append(e)
    return sorted(good, key=lambda e: e.get("ts", 0))


@repo_method
def attribute_change(repo, file_id: int, new_hash: Optional[str], since: float) -> Optional[dict]:
    """Find who wrote a file since a time and remember it for that revision. Returns {process, user} or None."""
    if repo.get("audit_enabled") != "1":
        return None
    f = repo.file(file_id)
    if f is None or is_probe(f):
        return None
    ev = query_writers(f.path, since - 2, now_ts() + 2)
    if not ev:
        return None
    last = ev[-1]
    proc = re.split(r"[\\/]", str(last.get("process") or ""))[-1] or "?"   # Windows path, also when tested elsewhere
    who = {"process": proc, "user": str(last.get("user") or "").strip("\\"), "ts": last.get("ts")}
    repo.db.execute("INSERT OR REPLACE INTO change_actors(file_id, hash, ts, process, user) VALUES(?,?,?,?,?)",
                    (file_id, new_hash or "", who["ts"] or now_ts(), who["process"], who["user"]))
    repo.db.commit()
    repo.log(f"{f.display} was changed by {proc}" + (f" ({who['user']})" if who["user"] else ""), file_id=file_id)
    return who


@repo_method
def change_actors(repo, file_id: int) -> dict:
    """{hash: 'program (user)'} for a file's revisions."""
    return {r["hash"]: f"{r['process']}" + (f" ({r['user']})" if r["user"] else "")
            for r in rows(repo, "SELECT * FROM change_actors WHERE file_id=?", (file_id,))}


__all__: list = []
