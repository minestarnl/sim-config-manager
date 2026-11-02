"""Optional Hub: status reports for the central web dashboard."""
from __future__ import annotations


import json
import socket
from typing import Optional


from .util import APP_NAME, APP_VERSION  # noqa: F401
from .repo import Repo  # noqa: F401
from .models import STATUS_TEXT  # noqa: F401
from .sync import build_status  # noqa: F401
from .system import is_admin  # noqa: F401

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
    for key, fn in (
            ("sessions", lambda: [{k: x[k] for k in ("id", "kind", "label", "started", "ended", "version_number",
                                                      "version_matches", "aircraft", "stats", "crash")}
                                  for x in repo.sessions(15)]),
            ("performance", lambda: repo.performance_by_version()),
            ("hardware", lambda: [{"label": r["label"], "ok": r["ok"], "device": (r.get("device") or {}).get("name"),
                                   "note": r.get("note", "")} for r in repo.hardware_status().get("required", [])]),
            ("health", lambda: {k: v for k, v in repo.health_status().items() if k != "ts"}),
            ("maintenance", lambda: [{k: t.get(k) for k in ("id", "name", "every_days", "every_hours", "last_done",
                                                             "due_in_days", "due_in_hours", "due_date", "state",
                                                             "note")} for t in repo.maintenance_tasks()]),
            ("manifests", lambda: [{"name": m["name"], "created": m["created"], "files": m["files"],
                                    "last_check": {k: (m["last_check"] or {}).get(k) for k in ("ts", "ok", "counts")}
                                    if m["last_check"] else None} for m in repo.manifests()]),
            ("installs", lambda: [{"name": i["name"], "started": i["started"], "status": i["status"],
                                   "counts": i["summary"].get("counts", {})} for i in repo.installs()[:10]])):
        try:
            st[key] = fn()
        except Exception:  # noqa: BLE001 - optional sections
            pass
    return st


def apply_hub_commands(repo, commands: list) -> list[str]:
    """Things a technician did on the Hub (e.g. from the phone page) that the sim has to record."""
    done = []
    for c in commands or []:
        try:
            if c.get("type") == "maintenance_done":
                repo.maintenance_done(int(c["task"]), c.get("by", ""), c.get("note", "") + " (via the Hub)")
                done.append(c.get("id"))
            elif c.get("type") == "note":
                repo.log(f"[Hub] {c.get('by', '')}: {c.get('text', '')}")
                done.append(c.get("id"))
        except Exception as e:  # noqa: BLE001
            repo.log(f"Could not apply a Hub command ({c.get('type')}): {e}", level="warning")
    return [d for d in done if d]


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
            res = json.loads(r.read() or b"{}")
        if res.get("commands") and not repo.read_only:
            res["applied"] = apply_hub_commands(repo, res["commands"])
        return res
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
    return (snap.id if snap else 0, ev[0]["id"] if ev else 0, repo.get("hw_missing"), repo.get("health_last")[:40])


for _n, _f in (("build_hub_report", build_hub_report), ("hub_signature", hub_signature)):
    setattr(Repo, _n, _f)

__all__ = ['build_hub_report', 'hub_push', 'hub_signature', 'apply_hub_commands']
