"""
Shared templates with per-sim variables and staged rollout (feature #9).

A template is the content of a config file with ``{{variables}}`` for what differs per sim (projector
resolution, COM ports, IP addresses …). Templates live in the shared location, so every sim sees them; each sim
keeps its own variable values and which tracked file the template writes to.

A new revision of a template is rolled out in **stages** (e.g. stage 1: Sim 1, stage 2: Sim 2 + Sim 3). A sim
only gets the update once its stage is open; applying it is always a deliberate click (with a safety snapshot),
and each sim reports back which revision it runs, so the rollout can be followed and advanced.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional

from .archive import safe_name
from .repo import repo_method
from .util import decode_text, now_ts

VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.-]*)\s*\}\}")


def variables_in(content: str) -> list[str]:
    seen = []
    for m in VAR.finditer(content):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def render_template(content: str, values: dict) -> str:
    missing = [v for v in variables_in(content) if str(values.get(v, "")).strip() == ""]
    if missing:
        raise ValueError("No value for: " + ", ".join(missing))
    return VAR.sub(lambda m: str(values[m.group(1)]), content)


def _sync(repo):
    from .sync import make_sync
    s = make_sync(repo)
    if s is None:
        raise RuntimeError("Templates are shared through the shared location. Set one up on the Sharing page first.")
    return s


def _local(repo, key: str) -> dict:
    try:
        return json.loads(repo.get(key) or "{}")
    except ValueError:
        return {}


@repo_method
def list_templates(repo) -> list[dict]:
    s = _sync(repo)
    out = []
    for n in s.backend.list("templates"):
        if not n.endswith(".json") or "@" in n:
            continue
        try:
            t = json.loads(s.backend.read(f"templates/{n}"))
        except (FileNotFoundError, ValueError):
            continue
        if t.get("deleted"):
            continue
        t["status"] = {}
        out.append(t)
    names = s.backend.list("templates")
    for t in out:
        for n in names:
            if n.startswith(t["id"] + "@") and n.endswith(".json"):
                try:
                    st = json.loads(s.backend.read(f"templates/{n}"))
                    t["status"][st.get("machine", n)] = st
                except (FileNotFoundError, ValueError):
                    pass
        t.update(template_state(repo, t))
    return sorted(out, key=lambda t: t.get("name", "").lower())


def template_state(repo, t: dict) -> dict:
    """This sim's view of a template: is it in an open stage, which revision runs here, update waiting?"""
    me = repo.machine
    stages = t.get("stages") or []
    my_stage = next((i for i, st in enumerate(stages) if me in st), None)
    open_stage = t.get("open_stage", 0)
    eligible = my_stage is not None and my_stage <= open_stage
    applied = _local(repo, "template_applied").get(t["id"], {})
    target = _local(repo, "template_targets").get(t["id"])
    return {"my_stage": my_stage, "eligible": eligible, "applied_rev": applied.get("rev"), "target_file": target,
            "update_ready": eligible and applied.get("rev") != t.get("rev"),
            "values": _local(repo, "template_vars").get(t["id"], {})}


@repo_method
def save_template(repo, t: dict) -> dict:
    """Create or update a template. A content change makes a new revision and restarts the rollout at stage 1."""
    s = _sync(repo)
    t = {k: v for k, v in t.items() if k not in ("status", "my_stage", "eligible", "applied_rev", "target_file",
                                                    "update_ready", "values")}
    t.setdefault("id", uuid.uuid4().hex[:10])
    old = None
    try:
        old = json.loads(s.backend.read(f"templates/{t['id']}.json"))
    except (FileNotFoundError, ValueError):
        pass
    t["variables"] = variables_in(t.get("content", ""))
    if old is None or old.get("content") != t.get("content"):
        t["rev"] = (old or {}).get("rev", 0) + 1
        t["open_stage"] = 0
        t["updated"] = now_ts()
        t["author"] = getattr(repo, "actor", None) or repo.get("version_author") or repo.machine
    t.setdefault("stages", [[repo.machine]])
    s.backend.write(f"templates/{t['id']}.json", json.dumps(t, indent=2).encode("utf-8"))
    repo.log(f"Template '{t.get('name')}' saved (r{t['rev']})")
    return t


@repo_method
def advance_rollout(repo, template_id: str) -> dict:
    s = _sync(repo)
    t = json.loads(s.backend.read(f"templates/{template_id}.json"))
    if t.get("open_stage", 0) + 1 >= len(t.get("stages") or []):
        raise RuntimeError("All stages are already open.")
    t["open_stage"] = t.get("open_stage", 0) + 1
    s.backend.write(f"templates/{template_id}.json", json.dumps(t, indent=2).encode("utf-8"))
    repo.log(f"Template '{t.get('name')}' r{t['rev']}: stage {t['open_stage'] + 1} opened")
    return t


@repo_method
def set_template_binding(repo, template_id: str, file_id: Optional[int], values: dict) -> None:
    targets = _local(repo, "template_targets")
    targets[template_id] = file_id
    repo.set("template_targets", json.dumps(targets))
    allv = _local(repo, "template_vars")
    allv[template_id] = values
    repo.set("template_vars", json.dumps(allv))


@repo_method
def preview_template(repo, template_id: str) -> dict:
    t = next((x for x in repo.list_templates() if x["id"] == template_id), None)
    if t is None:
        raise KeyError(template_id)
    f = repo.file(t["target_file"]) if t.get("target_file") else None
    if f is None:
        raise RuntimeError("Choose which tracked file this template writes to on this sim.")
    new = render_template(t["content"], t["values"])
    cur = decode_text(repo.read_current(f)) if repo.read_current(f) is not None else ""
    return {"template": t, "file": f, "current": cur, "new": new}


@repo_method
def apply_template(repo, template_id: str) -> dict:
    p = repo.preview_template(template_id)
    t, f = p["template"], p["file"]
    if not t["eligible"]:
        raise RuntimeError("This sim's rollout stage is not open yet.")
    repo.write_file(f, p["new"].encode("utf-8"), f"Template '{t['name']}' r{t['rev']}")
    applied = _local(repo, "template_applied")
    applied[template_id] = {"rev": t["rev"], "ts": now_ts()}
    repo.set("template_applied", json.dumps(applied))
    st = {"machine": repo.machine, "rev": t["rev"], "applied": now_ts(),
          "by": getattr(repo, "actor", None) or repo.get("version_author") or repo.machine, "file": f.display}
    _sync(repo).backend.write(f"templates/{template_id}@{safe_name(repo.machine)}.json",
                              json.dumps(st).encode("utf-8"))
    repo.log(f"Template '{t['name']}' r{t['rev']} applied to {f.display}")
    return st


@repo_method
def delete_template(repo, template_id: str) -> None:
    s = _sync(repo)
    t = json.loads(s.backend.read(f"templates/{template_id}.json"))
    t["deleted"] = True
    s.backend.write(f"templates/{template_id}.json", json.dumps(t).encode("utf-8"))


@repo_method
def templates_waiting(repo) -> list[dict]:
    """Templates with an update ready for this sim (for the notification and the Hub)."""
    try:
        return [t for t in repo.list_templates() if t["update_ready"]]
    except Exception:  # noqa: BLE001 - no shared location, offline …
        return []


__all__ = ["variables_in", "render_template", "template_state"]
