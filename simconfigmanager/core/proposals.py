"""Changes proposed by an AI assistant; applied only after approval in the app."""
from __future__ import annotations


import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional


from .models import TrackedFile  # noqa: F401
from .probes import is_remote  # noqa: F401
from .util import now_ts, read_file_bytes, sha256_bytes  # noqa: F401

if TYPE_CHECKING:
    from .repo import Repo

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

__all__ = ['proposals_dir', 'create_proposal', 'list_proposals', 'proposal_data', '_save_proposal', 'reject_proposal', 'apply_proposal']
