"""Alert rules: rule kinds, evaluation and text diffs."""
from __future__ import annotations

import difflib

import re
import xml.etree.ElementTree as ET
from typing import Callable, Optional


from .models import Change, Rule  # noqa: F401
from .util import decode_text, is_binary, sha256_bytes  # noqa: F401

# --------------------------------------------------------------------------- rule kinds

RULE_KINDS = {
    "any_change": "Any change to the file",
    "deleted": "File was deleted / went missing",
    "line_added": "A line matching a pattern was ADDED",
    "line_removed": "A line matching a pattern was REMOVED",
    "ini_value": "INI/CFG value changed  (pattern = Section/Key)",
    "xml_value": "XML value changed  (pattern = element path, optional @attribute)",
    "differs_from_rp": "File no longer matches a restore point  (pattern = restore point name)",
}

RULE_HELP = {
    "any_change": "Fires on every change. Pattern/expected are ignored.",
    "deleted": "Fires when the file disappears. Pattern/expected are ignored.",
    "line_added": "Pattern is a regular expression, e.g.  ^\\s*<Path>.*fsuipc  or  TEXTURE_MAX_LOAD",
    "line_removed": "Pattern is a regular expression matched against removed lines.",
    "ini_value": "Pattern 'GRAPHICS/TEXTURE_MAX_LOAD' (section/key, case-insensitive). "
                 "Expected (optional): alert only when the new value is NOT this value.",
    "xml_value": "Pattern is an ElementTree path, e.g.  .//Launch.Addon/Name  or  .//hardware@ip . "
                 "Expected (optional): alert only when the new value is NOT this value.",
    "differs_from_rp": "Pattern = restore point name. Alerts when the file drifts from that restore point.",
}


def parse_ini(text: str) -> dict:
    """Tolerant INI/CFG parser (P3D cfg files are not strict INI). Last key wins."""
    out: dict = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((";", "#", "//")):
            continue
        if line.startswith("[") and "]" in line:
            section = line[1:line.index("]")].strip().lower()
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            out[(section, k.strip().lower())] = v.strip()
    return out


def xml_values(data: Optional[bytes], path: str) -> Optional[list]:
    if data is None:
        return None
    attr = None
    if "@" in path:
        path, attr = path.rsplit("@", 1)
        path = path or "."
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        # Some sim XML files have junk before the root element; try the text route.
        try:
            root = ET.fromstring(decode_text(data).encode("utf-8").lstrip())
        except ET.ParseError:
            return None
    try:
        elems = [root] if path in (".", "") else root.findall(path)
    except SyntaxError:
        return None
    vals = []
    for el in elems:
        if attr:
            vals.append(el.get(attr))
        else:
            vals.append((el.text or "").strip())
    return vals


def changed_lines(old: Optional[bytes], new: Optional[bytes]) -> tuple[list, list]:
    a = decode_text(old).splitlines()
    b = decode_text(new).splitlines()
    added, removed = [], []
    for op in difflib.ndiff(a, b):
        if op.startswith("+ "):
            added.append(op[2:])
        elif op.startswith("- "):
            removed.append(op[2:])
    return added, removed


def evaluate_rule(rule: Rule, change: Change, rp_hash_lookup: Callable[[str, int], Optional[str]] | None = None
                  ) -> Optional[str]:
    """Return an alert message if the rule matches this change, else None."""
    k = rule.kind
    name = change.file.display
    try:
        if k == "any_change":
            if change.new_hash is None:
                return f"{name} was deleted"
            if change.old_hash is None:
                return f"{name} appeared"
            return f"{name} changed"
        if k == "deleted":
            return f"{name} is missing / was deleted" if change.new_hash is None and change.old_hash else None
        if k in ("line_added", "line_removed"):
            if is_binary(change.new_data) or is_binary(change.old_data):
                return None
            rx = re.compile(rule.pattern or ".*", re.IGNORECASE)
            added, removed = changed_lines(change.old_data, change.new_data)
            lines = added if k == "line_added" else removed
            hits = [ln.strip() for ln in lines if rx.search(ln)]
            if hits:
                verb = "added" if k == "line_added" else "removed"
                more = f" (+{len(hits) - 1} more)" if len(hits) > 1 else ""
                return f"{name}: line {verb}: {hits[0][:120]}{more}"
            return None
        if k == "ini_value":
            if "/" in rule.pattern:
                sec, key = rule.pattern.split("/", 1)
            else:
                sec, key = "", rule.pattern
            sk = (sec.strip().lower(), key.strip().lower())
            old = parse_ini(decode_text(change.old_data)).get(sk) if change.old_data is not None else None
            new = parse_ini(decode_text(change.new_data)).get(sk) if change.new_data is not None else None
            if rule.expected:
                if new != rule.expected and old != new:
                    return f"{name}: {rule.pattern} = {new!r} (expected {rule.expected!r})"
                return None
            if old != new:
                return f"{name}: {rule.pattern} changed {old!r} → {new!r}"
            return None
        if k == "xml_value":
            old = xml_values(change.old_data, rule.pattern)
            new = xml_values(change.new_data, rule.pattern)
            if rule.expected:
                if new is not None and old != new and any(v != rule.expected for v in new):
                    return f"{name}: {rule.pattern} = {new} (expected {rule.expected!r})"
                return None
            if old != new:
                return f"{name}: {rule.pattern} changed {old} → {new}"
            return None
        if k == "differs_from_rp":
            if rp_hash_lookup is None:
                return None
            target = rp_hash_lookup(rule.pattern, change.file.id)
            if target is None:
                return None
            if change.new_hash != target:
                return f"{name} no longer matches restore point '{rule.pattern}'"
            return None
    except re.error as e:
        return f"Rule '{rule.name}' has an invalid pattern: {e}"
    return None


# --------------------------------------------------------------------------- diff

def unified_diff(old: Optional[bytes], new: Optional[bytes], old_label="before", new_label="after",
                 context: int = 3) -> str:
    if is_binary(old) or is_binary(new):
        o = "missing" if old is None else f"{len(old)} bytes, sha {sha256_bytes(old)[:10]}"
        n = "missing" if new is None else f"{len(new)} bytes, sha {sha256_bytes(new)[:10]}"
        return f"Binary file: {o}  →  {n}"
    a = decode_text(old).splitlines()
    b = decode_text(new).splitlines()
    return "\n".join(difflib.unified_diff(a, b, old_label, new_label, n=context, lineterm=""))


def diff_stats(old: Optional[bytes], new: Optional[bytes]) -> tuple[int, int]:
    if is_binary(old) or is_binary(new):
        return (0, 0)
    added, removed = changed_lines(old, new)
    return len(added), len(removed)

__all__ = ['RULE_KINDS', 'RULE_HELP', 'parse_ini', 'xml_values', 'changed_lines', 'evaluate_rule', 'unified_diff', 'diff_stats']
