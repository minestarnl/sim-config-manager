"""
Health and predictive maintenance (feature #14).

* **Health:** disk health and wear (Windows storage reliability counters / SMART), GPU temperature, and the
  projectors over **PJLink** (the standard projector network protocol: power, lamp hours, error status).
  Readings are taken every 15 minutes on the sim; thresholds raise warnings in the event log and the Hub.
* **Maintenance schedule:** recurring tasks (clean filters every 90 days, replace lamps every 3,000 lamp hours,
  recalibrate the yoke every 200 sim hours …). Sim hours come from the recorded sessions. Due and overdue tasks
  show in the app and on the Hub, where a technician can also mark them done from a phone.
"""
from __future__ import annotations

import hashlib
import json
import socket
from typing import Optional

from . import winsys
from .repo import repo_method, rows, schema
from .util import now_ts

schema("""
CREATE TABLE IF NOT EXISTS projectors (
    id INTEGER PRIMARY KEY, name TEXT, host TEXT, password TEXT DEFAULT '', lamp_life INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS maint_tasks (
    id INTEGER PRIMARY KEY, name TEXT, every_days REAL, every_hours REAL, last_done REAL, last_done_hours REAL,
    note TEXT DEFAULT '', created REAL);
CREATE TABLE IF NOT EXISTS maint_log (
    id INTEGER PRIMARY KEY, task_id INTEGER, ts REAL, by TEXT, note TEXT);
""")

LIMITS = {"disk_temp": 60, "disk_wear": 80, "gpu_temp": 85, "lamp_pct": 90}
SOON_DAYS = 7
SOON_HOURS = 10

# --------------------------------------------------------------------------- PJLink (projectors)


def pjlink_query(host: str, password: str = "", commands=("POWR", "LAMP", "ERST", "NAME", "INF1", "INF2"),
                 port: int = 4352, timeout: float = 4) -> dict:
    """Ask a projector over PJLink class 1. Returns {"POWR": "1", "LAMP": "1234 1", ...} or raises OSError."""
    out: dict = {}
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        f = s.makefile("rb")
        greet = f.readline().decode("ascii", "replace").strip()
        prefix = ""
        if greet.startswith("PJLINK 1"):
            if not password:
                raise PermissionError("The projector asks for a PJLink password")
            prefix = hashlib.md5((greet.split()[2] + password).encode("ascii")).hexdigest()
        elif not greet.startswith("PJLINK 0"):
            raise OSError(f"Not a PJLink projector (got {greet[:40]!r})")
        for cmd in commands:
            s.sendall(f"{prefix}%1{cmd} ?\r".encode("ascii"))
            prefix = ""   # the digest is only sent with the first command
            line = f.readline().decode("utf-8", "replace").strip()
            if line.upper().startswith("PJLINK ERRA"):
                raise PermissionError("Wrong PJLink password")
            if "=" in line:
                val = line.split("=", 1)[1]
                out[cmd] = None if val.startswith("ERR") else val
    return out


POWER = {"0": "Off", "1": "On", "2": "Cooling down", "3": "Warming up"}
_ERST = ("Fan", "Lamp", "Temperature", "Cover open", "Filter", "Other")


def describe_projector(raw: dict, lamp_life: int = 0) -> dict:
    d = {"power": POWER.get(raw.get("POWR") or "", raw.get("POWR")), "name": raw.get("NAME") or "",
         "model": " ".join(x for x in (raw.get("INF1"), raw.get("INF2")) if x), "lamp_hours": None, "errors": []}
    lamp = (raw.get("LAMP") or "").split()
    if lamp and lamp[0].isdigit():
        d["lamp_hours"] = int(lamp[0])
        if lamp_life:
            d["lamp_pct"] = round(d["lamp_hours"] / lamp_life * 100)
    erst = raw.get("ERST") or ""
    for i, ch in enumerate(erst[:6]):
        if ch in "12":
            d["errors"].append(f"{_ERST[i]} {'warning' if ch == '1' else 'error'}")
    return d


@repo_method
def projectors(repo) -> list[dict]:
    return [dict(r) for r in rows(repo, "SELECT * FROM projectors ORDER BY name COLLATE NOCASE")]


@repo_method
def save_projector(repo, p: dict) -> None:
    if p.get("id"):
        repo.db.execute("UPDATE projectors SET name=?, host=?, password=?, lamp_life=? WHERE id=?",
                        (p["name"], p["host"], p.get("password", ""), int(p.get("lamp_life") or 0), p["id"]))
    else:
        repo.db.execute("INSERT INTO projectors(name, host, password, lamp_life) VALUES(?,?,?,?)",
                        (p["name"], p["host"], p.get("password", ""), int(p.get("lamp_life") or 0)))
    repo.db.commit()


@repo_method
def delete_projector(repo, projector_id: int) -> None:
    repo.db.execute("DELETE FROM projectors WHERE id=?", (projector_id,))
    repo.db.commit()


# --------------------------------------------------------------------------- disks and temperatures

_DISKS = ("Get-PhysicalDisk | ForEach-Object { $r = $_ | Get-StorageReliabilityCounter -ErrorAction "
          "SilentlyContinue; [pscustomobject]@{name=$_.FriendlyName; health=[string]$_.HealthStatus; "
          "media=[string]$_.MediaType; size=$_.Size; temp=$r.Temperature; wear=$r.Wear; "
          "read_errors=$r.ReadErrorsTotal; hours=$r.PowerOnHours} }")


def disk_health() -> Optional[list[dict]]:
    return winsys.powershell_json("disks", _DISKS, 60)


@repo_method
def health_check(repo) -> dict:
    """Read disks, GPU and projectors; log new warnings; remember the result (runs on the sim)."""
    res: dict = {"ts": now_ts(), "disks": disk_health() or [], "gpu": winsys.gpu_stats(), "projectors": []}
    for p in repo.projectors():
        entry = {"id": p["id"], "name": p["name"], "host": p["host"]}
        if "pjlink" in winsys.FAKES:
            raw = winsys._fake("pjlink", p["host"])
        else:
            try:
                raw = pjlink_query(p["host"], p.get("password") or "")
            except OSError as e:
                entry["error"] = str(e) or type(e).__name__
                raw = None
        if raw is not None:
            entry.update(describe_projector(raw, p.get("lamp_life") or 0))
        res["projectors"].append(entry)
    res["warnings"] = health_warnings(res)
    try:
        before = set(json.loads(repo.get("health_last") or "{}").get("warnings", []))
    except ValueError:
        before = set()
    for w in res["warnings"]:
        if w not in before:
            repo.log(f"[Health] {w}", level="warning")
    if not repo.read_only:
        repo.set("health_last", json.dumps(res))
    return res


def health_warnings(res: dict) -> list[str]:
    out = []
    for d in res.get("disks") or []:
        name = d.get("name") or "Disk"
        if d.get("health") and d["health"] not in ("Healthy", "0"):
            out.append(f"{name}: health {d['health']}")
        if d.get("temp") and d["temp"] >= LIMITS["disk_temp"]:
            out.append(f"{name}: {d['temp']} °C")
        if d.get("wear") and d["wear"] >= LIMITS["disk_wear"]:
            out.append(f"{name}: {d['wear']}% worn")
    g = res.get("gpu") or {}
    if g.get("temp") and g["temp"] >= LIMITS["gpu_temp"]:
        out.append(f"GPU at {g['temp']:.0f} °C")
    for p in res.get("projectors") or []:
        if p.get("error"):
            out.append(f"Projector {p['name']}: not reachable ({p['error']})")
        for e in p.get("errors") or []:
            out.append(f"Projector {p['name']}: {e}")
        if p.get("lamp_pct") and p["lamp_pct"] >= LIMITS["lamp_pct"]:
            out.append(f"Projector {p['name']}: lamp at {p['lamp_hours']} h ({p['lamp_pct']}% of its life)")
    return out


@repo_method
def health_status(repo) -> dict:
    try:
        return json.loads(repo.get("health_last") or "{}")
    except ValueError:
        return {}


# --------------------------------------------------------------------------- maintenance schedule

@repo_method
def maintenance_tasks(repo) -> list[dict]:
    hours = repo.sim_hours()
    now = now_ts()
    out = []
    for r in rows(repo, "SELECT * FROM maint_tasks ORDER BY name COLLATE NOCASE"):
        t = dict(r)
        base = t["last_done"] or t["created"] or now
        due_in_days = due_in_hours = None
        if t["every_days"]:
            due_in_days = round((base + t["every_days"] * 86400 - now) / 86400, 1)
        if t["every_hours"]:
            due_in_hours = round((t["last_done_hours"] or 0) + t["every_hours"] - hours, 1)
        overdue = (due_in_days is not None and due_in_days < 0) or (due_in_hours is not None and due_in_hours < 0)
        soon = (due_in_days is not None and due_in_days <= SOON_DAYS) or \
               (due_in_hours is not None and due_in_hours <= SOON_HOURS)
        t.update(due_in_days=due_in_days, due_in_hours=due_in_hours,
                 state="overdue" if overdue else ("soon" if soon else "ok"))
        if due_in_days is not None:
            t["due_date"] = base + t["every_days"] * 86400
        out.append(t)
    return out


@repo_method
def save_maintenance_task(repo, t: dict) -> None:
    if t.get("id"):
        repo.db.execute("UPDATE maint_tasks SET name=?, every_days=?, every_hours=?, note=? WHERE id=?",
                        (t["name"], t.get("every_days") or None, t.get("every_hours") or None, t.get("note", ""),
                         t["id"]))
    else:
        repo.db.execute("INSERT INTO maint_tasks(name, every_days, every_hours, note, created, last_done_hours) "
                        "VALUES(?,?,?,?,?,?)", (t["name"], t.get("every_days") or None, t.get("every_hours") or None,
                                                t.get("note", ""), now_ts(), repo.sim_hours()))
    repo.db.commit()


@repo_method
def delete_maintenance_task(repo, task_id: int) -> None:
    repo.db.execute("DELETE FROM maint_tasks WHERE id=?", (task_id,))
    repo.db.commit()


@repo_method
def maintenance_done(repo, task_id: int, by: str = "", note: str = "") -> None:
    t = rows(repo, "SELECT name FROM maint_tasks WHERE id=?", (task_id,))
    if not t:
        raise KeyError(task_id)
    now = now_ts()
    repo.db.execute("UPDATE maint_tasks SET last_done=?, last_done_hours=? WHERE id=?", (now, repo.sim_hours(),
                                                                                        task_id))
    repo.db.execute("INSERT INTO maint_log(task_id, ts, by, note) VALUES(?,?,?,?)", (task_id, now, by, note))
    repo.db.commit()
    repo.log(f"Maintenance done: {t[0]['name']}" + (f" — {note}" if note else "") + (f" (by {by})" if by else ""))


@repo_method
def maintenance_log(repo, limit: int = 100) -> list[dict]:
    return [dict(r) for r in rows(repo, "SELECT l.*, t.name FROM maint_log l LEFT JOIN maint_tasks t ON "
                                        "t.id=l.task_id ORDER BY l.ts DESC LIMIT ?", (limit,))]


__all__ = ["pjlink_query", "describe_projector", "health_warnings", "LIMITS"]
