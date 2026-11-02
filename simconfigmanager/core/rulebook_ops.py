"""Rulebook: documented and locked settings."""
from __future__ import annotations


from typing import Optional


from .repo import Repo  # noqa: F401
from .models import TrackedFile  # noqa: F401
from .util import detect_text, now_ts  # noqa: F401
from .settings import kind_for_path  # noqa: F401
from .alerts import parse_ini, xml_values  # noqa: F401

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

__all__ = ['_rb_row', 'rulebook', 'save_rulebook_entry', 'delete_rulebook_entry', 'rule_value', 'rulebook_status', 'rulebook_violations_for']
