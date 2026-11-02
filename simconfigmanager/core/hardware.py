"""
Cockpit hardware watchdog (feature #4).

Lists the USB / HID / serial (COM) devices that are plugged in. Devices marked *required* (yokes, MCP/FCU
panels, throttle quadrants, interface boards, …) are checked every 30 seconds: when one disappears there is an
alert, the sim reports NO-GO to the Hub, and when it comes back that is logged too.
"""
from __future__ import annotations

import fnmatch
import json
from typing import Optional

from . import winsys
from .repo import repo_method, rows, schema
from .util import now_ts

schema("""
CREATE TABLE IF NOT EXISTS required_devices (
    id INTEGER PRIMARY KEY, label TEXT, match TEXT, note TEXT DEFAULT '', created REAL);
""")

DEVICE_CLASSES = ("HIDClass", "USB", "Ports", "Image", "Media", "Keyboard", "Mouse", "Monitor", "Bluetooth")

_LIST = ("Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue | Where-Object { $_.Class -in @("
         + ",".join(f"'{c}'" for c in DEVICE_CLASSES) + ") } | Select-Object @{n='name';e={$_.FriendlyName}}, "
         "@{n='cls';e={$_.Class}}, @{n='id';e={$_.InstanceId}}, @{n='status';e={$_.Status}}")


def list_devices() -> Optional[list[dict]]:
    """Present devices: [{name, cls, id, status}] (None when not available on this OS)."""
    out = winsys.powershell_json("devices", _LIST, 60)
    if out is None:
        return None
    seen, devs = set(), []
    for d in out:
        if not d.get("id") or d["id"] in seen:
            continue
        seen.add(d["id"])
        devs.append({"name": d.get("name") or d["id"], "cls": d.get("cls") or "", "id": d["id"],
                     "status": d.get("status") or ""})
    return sorted(devs, key=lambda d: (d["cls"], d["name"].lower()))


def device_key(instance_id: str) -> str:
    """Pattern that survives re-plugging: vendor/product part of the instance id, serial kept if present."""
    parts = instance_id.split("\\")
    return "\\".join(parts[:2]) + "*" if len(parts) >= 2 else instance_id


@repo_method
def required_devices(repo) -> list[dict]:
    return [dict(r) for r in rows(repo, "SELECT * FROM required_devices ORDER BY label COLLATE NOCASE")]


@repo_method
def add_required_device(repo, label: str, match: str, note: str = "") -> dict:
    cur = repo.db.execute("INSERT INTO required_devices(label, match, note, created) VALUES(?,?,?,?)",
                          (label, match, note, now_ts()))
    repo.db.commit()
    repo.log(f"Required cockpit hardware added: {label}")
    return {"id": cur.lastrowid, "label": label, "match": match, "note": note}


@repo_method
def remove_required_device(repo, device_id: int) -> None:
    repo.db.execute("DELETE FROM required_devices WHERE id=?", (device_id,))
    repo.db.commit()


def match_devices(required: list[dict], devices: list[dict]) -> list[dict]:
    out = []
    for r in required:
        pat = r["match"].lower()
        hit = next((d for d in devices if fnmatch.fnmatch(d["id"].lower(), pat)
                    or fnmatch.fnmatch(d["name"].lower(), pat)), None)
        out.append({**r, "present": hit is not None, "device": hit,
                    "ok": hit is not None and (hit.get("status") or "OK").upper() in ("OK", "")})
    return out


@repo_method
def hardware_check(repo, devices: Optional[list] = None) -> dict:
    """Run the watchdog once (on the sim). Logs devices that went missing / came back. Returns the status."""
    if devices is None:
        devices = list_devices()
    if devices is None:
        return {"available": False, "devices": [], "required": [], "missing": []}
    req = match_devices(repo.required_devices(), devices)
    missing = [r["label"] for r in req if not r["ok"]]
    try:
        before = set(json.loads(repo.get("hw_missing") or "[]"))
    except ValueError:
        before = set()
    for label in sorted(set(missing) - before):
        repo.log(f"[Cockpit hardware] {label} is not connected (or not working)", level="alert")
    for label in sorted(before - set(missing)):
        repo.log(f"[Cockpit hardware] {label} is connected again")
    status = {"available": True, "ts": now_ts(), "devices": devices, "required": req, "missing": missing}
    if not repo.read_only:
        repo.set("hw_missing", json.dumps(missing))
        repo.set("hw_last", json.dumps(status))
    return status


@repo_method
def hardware_status(repo) -> dict:
    """Last watchdog result (cheap: no device scan)."""
    try:
        st = json.loads(repo.get("hw_last") or "{}")
    except ValueError:
        st = {}
    if not st:
        return {"available": False, "devices": [], "required": match_devices(repo.required_devices(), []),
                "missing": [], "ts": 0}
    st["required"] = match_devices(repo.required_devices(), st.get("devices", []))
    st["missing"] = [r["label"] for r in st["required"] if not r["ok"]]
    return st


__all__ = ["DEVICE_CLASSES", "list_devices", "device_key", "match_devices"]
