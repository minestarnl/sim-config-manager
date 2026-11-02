"""
Sessions, performance and benchmarks (features #1, #2, #5).

* **Live sim data** comes from a small FSUIPC Lua script ("bridge") that Sim Config Manager installs next to
  FSUIPC. Every second it writes aircraft, position, frame rate and pause state to a JSON file the app reads.
  (FSUIPC runs Lua plugins in its registered version; without it, sessions are still recorded from the sim
  process, just without frame rate and position.)
* A **session** starts when the sim program starts and ends when it exits. It records which version was active
  and whether every file matched it, samples frame rate and GPU temperature, and on exit checks the Windows
  event log for crashes (Application Error 1000, with the faulting module) and display driver resets (4101).
* **Performance per version** groups sessions by the version they ran on: average fps, 1 % lows, stutters per
  hour, crashes per 10 hours.
* A **benchmark** is a timed recording (e.g. the standard benchmark flight) of the current setup; two
  benchmarks are an A/B test. The AI assistants can read them and propose the next change ("autotune"), which a
  person approves before anything is written.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import sys
from pathlib import Path
from typing import Optional

from . import winsys
from .repo import repo_method, rows, schema
from .util import now_ts, write_file_atomic

schema("""
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY, kind TEXT DEFAULT 'session', label TEXT DEFAULT '', started REAL, ended REAL,
    planned_end REAL, machine TEXT, version_number INTEGER, version_matches INTEGER, differing TEXT DEFAULT '[]',
    snapshot_id INTEGER, aircraft TEXT DEFAULT '', start_pos TEXT DEFAULT '', end_pos TEXT DEFAULT '',
    stats TEXT DEFAULT '{}', crash TEXT DEFAULT '', note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS session_samples (
    session_id INTEGER, ts REAL, fps REAL, gpu_temp REAL, gpu_util REAL, aircraft TEXT, lat REAL, lon REAL,
    alt REAL, paused INTEGER);
CREATE INDEX IF NOT EXISTS idx_samples_session ON session_samples(session_id, ts);
""")

DEFAULT_SIM_EXE = "Prepar3D.exe"
LIVE_MAX_AGE = 10          # seconds before the bridge's data counts as stale

# --------------------------------------------------------------------------- FSUIPC Lua bridge

LUA_NAME = "SimConfigManager"
LUA_BRIDGE = r"""-- Sim Config Manager bridge (installed by Sim Config Manager; safe to delete).
-- Writes live data for the app once a second. Read-only: it never changes anything in the sim.
local OUT = [[__OUT__]]
local function esc(s) return (string.gsub(s or "", '[%c"\\]', function(c)
  if c == '"' then return '\\"' elseif c == '\\' then return '\\\\' else return ' ' end end)) end
function scm_tick(t)
  local raw = ipc.readUW(0x0274)
  local fps = 0
  if raw and raw > 0 then fps = 32768 / raw end
  local lat = ipc.readDD(0x0560) * 90.0 / (10001750.0 * 65536.0 * 65536.0)
  local lon = ipc.readDD(0x0568) * 360.0 / (65536.0 * 65536.0 * 65536.0 * 65536.0)
  local alt = ipc.readDD(0x0570) / (65536.0 * 65536.0) * 3.28084
  local gs = ipc.readUD(0x02B4) / 65536.0 * 1.943844
  local title = ipc.readSTR(0x3D00, 256) or ""
  title = string.match(title, "^[^%z]*")
  local f = io.open(OUT .. ".tmp", "w")
  if f then
    f:write(string.format('{"ts":%d,"fps":%.1f,"lat":%.6f,"lon":%.6f,"alt_ft":%.0f,"gs_kt":%.0f,' ..
      '"on_ground":%d,"paused":%d,"aircraft":"%s"}', os.time(), fps, lat, lon, alt, gs,
      ipc.readUW(0x0366), ipc.readUW(0x0264), esc(title)))
    f:close()
    os.remove(OUT)
    os.rename(OUT .. ".tmp", OUT)
  end
end
event.timer(1000, "scm_tick")
"""


def live_file(repo) -> Path:
    """Where the bridge writes its data (a place both the sim and the app can write/read)."""
    custom = repo.get("simlink_file")
    if custom:
        return Path(custom)
    base = os.environ.get("PROGRAMDATA") or str(Path.home())
    return Path(base) / "SimConfigManager" / "simlink.json"


@repo_method
def live_data(repo, max_age: float = LIVE_MAX_AGE) -> Optional[dict]:
    """Latest data from the bridge, or None when the sim/bridge isn't running (or the data is stale)."""
    if "simlink" in winsys.FAKES:
        return winsys._fake("simlink")
    p = live_file(repo)
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if now_ts() - float(d.get("ts", 0)) > max_age:
        return None
    return d


@repo_method
def fsuipc_folders(repo) -> list[str]:
    """Folders with an FSUIPC ini: from the tracked files first, then the usual install places."""
    found = []
    for f in repo.files():
        if re.match(r"fsuipc\d*\.ini$", os.path.basename(f.path), re.I):
            found.append(os.path.dirname(f.path))
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        for v in ("v6", "v5", "v4"):
            found.append(os.path.join(pf, "Lockheed Martin", f"Prepar3D {v}", "Modules"))
        found += [r"C:\FSUIPC6", r"C:\FSUIPC5", os.path.join(os.path.expanduser("~"), "Documents", "FSUIPC6")]
    out = []
    for d in found:
        if d and os.path.isdir(d) and any(re.match(r"fsuipc\d*\.ini$", n, re.I) for n in os.listdir(d)) \
                and d not in out:
            out.append(d)
    return out


@repo_method
def bridge_installed(repo, folder: str) -> bool:
    return os.path.exists(os.path.join(folder, LUA_NAME + ".lua"))


@repo_method
def install_bridge(repo, folder: str) -> str:
    """Put the Lua bridge in the FSUIPC folder and start it automatically ([Auto] section of the FSUIPC ini).
    The ini change goes through the normal write path, so it is in History and can be undone."""
    ini = next((os.path.join(folder, n) for n in sorted(os.listdir(folder))
                if re.match(r"fsuipc\d*\.ini$", n, re.I)), None)
    if not ini:
        raise FileNotFoundError(f"No FSUIPC ini in {folder}")
    out = live_file(repo)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_file_atomic(os.path.join(folder, LUA_NAME + ".lua"),
                      LUA_BRIDGE.replace("__OUT__", str(out)).encode("utf-8"))
    text = Path(ini).read_bytes().decode("utf-8", errors="replace")    # keep CRLF line endings as they are
    if re.search(rf"^\s*\d+\s*=\s*Lua\s+{LUA_NAME}\s*$", text, re.I | re.M):
        return ini
    nl = "\r\n" if "\r\n" in text else "\n"
    m = re.search(r"^\[Auto\][^\S\r\n]*\r?$", text, re.I | re.M)
    if m:
        body_end = re.search(r"^\[", text[m.end():], re.M)
        section = text[m.end(): m.end() + body_end.start()] if body_end else text[m.end():]
        nums = [int(n) for n in re.findall(r"^\s*(\d+)\s*=", section, re.M)]
        line = f"{max(nums, default=0) + 1}=Lua {LUA_NAME}"
        insert_at = m.end() + len(section.rstrip("\r\n"))
        new = text[:insert_at] + nl + line + text[insert_at:]
    else:
        new = text.rstrip("\r\n") + f"{nl}{nl}[Auto]{nl}1=Lua {LUA_NAME}{nl}"
    f = repo.find_file_by_path(ini)
    if f is None:
        f = repo.add_file(ini, os.path.basename(ini), "FSUIPC")
        repo.ensure_baseline()
    repo.write_file(f, new.encode("utf-8"), "Start the Sim Config Manager bridge (live data for sessions)")
    repo.log(f"Installed the live-data bridge in {folder}")
    return ini


# --------------------------------------------------------------------------- sessions

def _session(r) -> dict:
    d = dict(r)
    for k in ("stats", "differing", "crash"):
        try:
            d[k] = json.loads(d.get(k) or ("{}" if k == "stats" else "[]" if k == "differing" else "null"))
        except ValueError:
            d[k] = None
    return d


@repo_method
def sessions(repo, limit: int = 200, kind: Optional[str] = None) -> list[dict]:
    q = "SELECT * FROM sessions" + (" WHERE kind=?" if kind else "") + " ORDER BY started DESC LIMIT ?"
    return [_session(r) for r in rows(repo, q, ((kind, limit) if kind else (limit,)))]


@repo_method
def session(repo, session_id: int) -> Optional[dict]:
    r = rows(repo, "SELECT * FROM sessions WHERE id=?", (session_id,))
    return _session(r[0]) if r else None


@repo_method
def session_samples(repo, session_id: int) -> list[dict]:
    return [dict(r) for r in rows(repo, "SELECT * FROM session_samples WHERE session_id=? ORDER BY ts",
                                  (session_id,))]


def open_session(repo, kind: str = "session") -> Optional[dict]:
    r = rows(repo, "SELECT * FROM sessions WHERE ended IS NULL AND kind=? ORDER BY id DESC LIMIT 1", (kind,))
    return _session(r[0]) if r else None


def _pos(live: Optional[dict]) -> str:
    if not live or (not live.get("lat") and not live.get("lon")):
        return ""
    return f"{live['lat']:.4f},{live['lon']:.4f}"


def start_session(repo, kind: str = "session", label: str = "", minutes: float = 0) -> dict:
    ver = repo.latest_version()
    mism = repo.mismatches() if ver else []
    snap = repo.latest_snapshot()
    live = repo.live_data()
    now = now_ts()
    cur = repo.db.execute(
        "INSERT INTO sessions(kind, label, started, planned_end, machine, version_number, version_matches, "
        "differing, snapshot_id, aircraft, start_pos) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (kind, label, now, now + minutes * 60 if minutes else None, repo.machine, ver.number if ver else None,
         1 if ver and not mism else 0, json.dumps([m.file.display for m in mism]), snap.id if snap else None,
         (live or {}).get("aircraft", ""), _pos(live)))
    repo.db.commit()
    what = "Benchmark" if kind == "benchmark" else "Session"
    vtxt = (f"v{ver.number}" + ("" if not mism else f", {len(mism)} file(s) differ")) if ver else "no version"
    repo.log(f"{what} started{' — ' + label if label else ''} ({vtxt})")
    return repo.session(cur.lastrowid)


def add_sample(repo, s: dict, live: Optional[dict], gpu: Optional[dict]) -> None:
    repo.db.execute("INSERT INTO session_samples(session_id, ts, fps, gpu_temp, gpu_util, aircraft, lat, lon, alt, "
                    "paused) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (s["id"], now_ts(), (live or {}).get("fps"), (gpu or {}).get("temp"), (gpu or {}).get("util"),
                     (live or {}).get("aircraft"), (live or {}).get("lat"), (live or {}).get("lon"),
                     (live or {}).get("alt_ft"), int(bool((live or {}).get("paused")))))
    if live and live.get("aircraft") and not s.get("aircraft"):
        repo.db.execute("UPDATE sessions SET aircraft=?, start_pos=? WHERE id=?",
                        (live["aircraft"], _pos(live), s["id"]))
    repo.db.commit()


def compute_stats(samples: list[dict], started: float, ended: float) -> dict:
    fps = [x["fps"] for x in samples if x.get("fps") and not x.get("paused")]
    temps = [x["gpu_temp"] for x in samples if x.get("gpu_temp") is not None]
    utils = [x["gpu_util"] for x in samples if x.get("gpu_util") is not None]
    st: dict = {"minutes": round((ended - started) / 60, 1), "samples": len(samples)}
    if fps:
        srt = sorted(fps)
        med = statistics.median(fps)
        st.update(avg_fps=round(statistics.fmean(fps), 1), median_fps=round(med, 1),
                  low1_fps=round(srt[max(0, int(len(srt) * 0.01) - 1)], 1), min_fps=round(srt[0], 1),
                  stutters=sum(1 for v in fps if v < med * 0.5))
    if temps:
        st.update(max_gpu_temp=max(temps), avg_gpu_temp=round(statistics.fmean(temps), 1))
    if utils:
        st["avg_gpu_util"] = round(statistics.fmean(utils), 1)
    craft = []
    for x in samples:
        a = (x.get("aircraft") or "").strip()
        if a and a not in craft:
            craft.append(a)
    st["aircraft"] = craft
    return st


def end_session(repo, s: dict, sim_exe: str = DEFAULT_SIM_EXE) -> dict:
    now = now_ts()
    samples = repo.session_samples(s["id"])
    st = compute_stats(samples, s["started"], now)
    crash = None
    if s["kind"] == "session":
        crashes = winsys.app_crashes(s["started"] - 5, sim_exe)
        resets = winsys.display_resets(s["started"] - 5)
        if crashes or resets:
            crash = {"crashes": crashes, "display_resets": resets}
    last = next((x for x in reversed(samples) if x.get("lat") or x.get("lon")), None)
    end_pos = f"{last['lat']:.4f},{last['lon']:.4f}" if last else ""
    repo.db.execute("UPDATE sessions SET ended=?, stats=?, crash=?, end_pos=?, aircraft=? WHERE id=?",
                    (now, json.dumps(st), json.dumps(crash) if crash else "", end_pos,
                     ", ".join(st.get("aircraft", [])) or s.get("aircraft") or "", s["id"]))
    repo.db.commit()
    what = "Benchmark" if s["kind"] == "benchmark" else "Session"
    msg = f"{what} ended{' — ' + s['label'] if s.get('label') else ''}: {st['minutes']:.0f} min"
    if st.get("avg_fps"):
        msg += f", {st['avg_fps']} fps average, 1% low {st['low1_fps']}"
    if crash:
        parts = [f"{c.get('app')} crashed (faulting module {c.get('module')})" for c in crash["crashes"][:3]]
        if crash["display_resets"]:
            parts.append(f"{len(crash['display_resets'])} display driver reset(s)")
        repo.log(msg + " — " + "; ".join(parts), level="alert")
    else:
        repo.log(msg)
    return repo.session(s["id"])


@repo_method
def session_tick(repo, gpu: Optional[dict] = None) -> dict:
    """Called every few seconds on the sim PC: starts/ends sessions and benchmarks, records samples."""
    sim_exe = repo.get("session_process") or DEFAULT_SIM_EXE
    running = bool(repo.running_processes_named([sim_exe]))
    live = repo.live_data()
    gpu = gpu if gpu is not None else winsys.gpu_stats()
    s = open_session(repo)
    state = {"running": running, "live": live, "gpu": gpu}
    if running and s is None:
        s = start_session(repo)
    if s is not None:
        if running:
            add_sample(repo, s, live, gpu)
        else:
            end_session(repo, s, sim_exe)
            s = None
    b = open_session(repo, "benchmark")
    if b is not None:
        if running and live:
            add_sample(repo, b, live, gpu)
        if not running or (b.get("planned_end") and now_ts() >= b["planned_end"]):
            end_session(repo, b, sim_exe)
            b = None
    state["session"] = s
    state["benchmark"] = b
    return state


@repo_method
def start_benchmark(repo, label: str, minutes: float = 5) -> dict:
    if open_session(repo, "benchmark"):
        raise RuntimeError("A benchmark is already running.")
    if not repo.live_data():
        raise RuntimeError("No live data from the sim. Start Prepar3D with the bridge installed and load the "
                           "benchmark flight first.")
    return start_session(repo, "benchmark", label or "Benchmark", minutes)


@repo_method
def stop_benchmark(repo) -> Optional[dict]:
    b = open_session(repo, "benchmark")
    return end_session(repo, b) if b else None


@repo_method
def set_session_note(repo, session_id: int, note: str) -> None:
    repo.db.execute("UPDATE sessions SET note=? WHERE id=?", (note, session_id))
    repo.db.commit()


@repo_method
def delete_session(repo, session_id: int) -> None:
    repo.db.execute("DELETE FROM session_samples WHERE session_id=?", (session_id,))
    repo.db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    repo.db.commit()


@repo_method
def performance_by_version(repo) -> list[dict]:
    """Sessions grouped by the version they ran on (only sessions where all files matched it)."""
    groups: dict = {}
    for s in repo.sessions(5000, "session"):
        if not s.get("ended"):
            continue
        key = s["version_number"] if s.get("version_matches") else None
        groups.setdefault(key, []).append(s)
    out = []
    for key, ss in sorted(groups.items(), key=lambda kv: (kv[0] is None, -(kv[0] or 0))):
        hours = sum((x["ended"] - x["started"]) for x in ss) / 3600
        fps = [x["stats"].get("avg_fps") for x in ss if x["stats"] and x["stats"].get("avg_fps")]
        lows = [x["stats"].get("low1_fps") for x in ss if x["stats"] and x["stats"].get("low1_fps")]
        stut = sum((x["stats"] or {}).get("stutters", 0) for x in ss)
        crashes = sum(len((x["crash"] or {}).get("crashes", [])) for x in ss)
        resets = sum(len((x["crash"] or {}).get("display_resets", [])) for x in ss)
        out.append({"version": key, "label": f"v{key}" if key else "Not matching a version", "sessions": len(ss),
                    "hours": round(hours, 1), "avg_fps": round(statistics.fmean(fps), 1) if fps else None,
                    "low1_fps": round(statistics.fmean(lows), 1) if lows else None,
                    "stutters_per_hour": round(stut / hours, 1) if hours else None, "crashes": crashes,
                    "display_resets": resets,
                    "crashes_per_10h": round((crashes + resets) / hours * 10, 2) if hours else None})
    return out


@repo_method
def sim_hours(repo) -> float:
    r = rows(repo, "SELECT SUM(COALESCE(ended, ?) - started) AS t FROM sessions WHERE kind='session'", (now_ts(),))
    return round(((r[0]["t"] if r else 0) or 0) / 3600, 1)


@repo_method
def compare_benchmarks(repo, ids: list[int]) -> list[dict]:
    out = []
    for i in ids:
        b = repo.session(i)
        if b:
            out.append({"id": b["id"], "label": b["label"], "started": b["started"],
                        "version": b["version_number"], "matches": bool(b["version_matches"]),
                        "differing": b["differing"], "snapshot_id": b["snapshot_id"], **(b["stats"] or {})})
    return out


@repo_method
def running_processes_named(repo, names: list[str]) -> list[str]:
    if "processes" in winsys.FAKES:
        running = winsys._fake("processes")
        return [n for n in names if n.lower() in {x.lower() for x in running}]
    from .system import running_processes
    return running_processes(names)


__all__ = ["LUA_BRIDGE", "LUA_NAME", "live_file", "compute_stats", "DEFAULT_SIM_EXE"]
