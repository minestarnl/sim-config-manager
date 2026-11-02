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

__all__ = ['build_hub_report', 'hub_push', 'hub_signature']
