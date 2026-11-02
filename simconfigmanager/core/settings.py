"""Ignored settings and setting-level diffs for .ini/.cfg and .xml files."""
from __future__ import annotations

import fnmatch

import re
import xml.etree.ElementTree as ET
from typing import Optional


from .probes import PROBE_PREFIX  # noqa: F401
from .util import detect_text, is_binary  # noqa: F401

# =========================================================================== ignore rules + setting diffs (#1)

IGNORE_KINDS = {
    "ini_key": "CFG/INI setting (Section/Key, wildcards * allowed)",
    "ini_section": "Whole CFG/INI section (wildcards * allowed)",
    "xml": "XML element (path, e.g. .//Window or .//Launch.Addon/Name)",
    "line": "Lines matching a regular expression",
}


def kind_for_path(path: str, text: str = "") -> str:
    p = path.lower()
    if p.endswith((".xml", ".fxml", ".config")):
        return "xml"
    if p.endswith(".json"):
        return "json"
    if p.startswith(PROBE_PREFIX):
        return "text"
    if p.endswith((".cfg", ".ini", ".inf", ".dsd", ".ccp")):
        return "xml" if text.lstrip().startswith("<") else "ini"
    s = text.lstrip()
    if s.startswith("<"):
        return "xml"
    return "ini" if re.search(r"^\s*\[[^\]]+\]\s*$", text, re.M) else "text"


def normalize_content(data: Optional[bytes], path: str, rules: list[dict]) -> Optional[bytes]:
    """Content with ignored parts removed — only used to decide whether something *really* changed."""
    if data is None or not rules or is_binary(data):
        return data
    text, _fmt = detect_text(data)
    kind = kind_for_path(path, text)
    lines = text.split("\n")
    line_rx = []
    for r in rules:
        if r["kind"] == "line":
            try:
                line_rx.append(re.compile(r["pattern"]))
            except re.error:
                pass
    if kind == "ini":
        keys = [r["pattern"] for r in rules if r["kind"] == "ini_key"]
        secs = [r["pattern"].lower() for r in rules if r["kind"] == "ini_section"]
        out, section = [], ""
        for ln in lines:
            st = ln.strip()
            if st.startswith("[") and "]" in st:
                section = st[1:st.index("]")].strip()
                if any(fnmatch.fnmatch(section.lower(), p) for p in secs):
                    continue
                out.append(ln)
                continue
            if any(fnmatch.fnmatch(section.lower(), p) for p in secs):
                continue
            if "=" in st and not st.startswith((";", "#", "//")):
                k = st.split("=", 1)[0].strip()
                if any(_ini_key_match(section, k, pat) for pat in keys):
                    continue
            if any(rx.search(ln) for rx in line_rx):
                continue
            out.append(ln)
        return "\n".join(out).encode("utf-8")
    if kind == "xml":
        paths = [r["pattern"] for r in rules if r["kind"] == "xml"]
        if paths:
            try:
                root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8"))
                parent = {c: p for p in root.iter() for c in p}
                for pth in paths:
                    try:
                        for el in root.findall(pth):
                            if el in parent:
                                parent[el].remove(el)
                            else:
                                el.clear()
                    except SyntaxError:
                        continue
                text = ET.tostring(root, encoding="unicode")
                lines = text.split("\n")
            except ET.ParseError:
                pass
    return "\n".join(ln for ln in lines if not any(rx.search(ln) for rx in line_rx)).encode("utf-8")


def _ini_key_match(section: str, key: str, pattern: str) -> bool:
    if "/" in pattern:
        ps, pk = pattern.split("/", 1)
    else:
        ps, pk = "*", pattern
    return fnmatch.fnmatch(section.lower(), ps.strip().lower()) and fnmatch.fnmatch(key.lower(), pk.strip().lower())


def setting_values(data: Optional[bytes], path: str) -> Optional[dict]:
    """Flatten a config into {setting: value}. INI: 'Section/Key'. XML: 'Root/Child[2]/Name' (+ '@attr')."""
    if data is None or is_binary(data):
        return None
    text, _ = detect_text(data)
    kind = kind_for_path(path, text)
    out: dict[str, str] = {}
    if kind == "ini":
        section = ""
        for ln in text.split("\n"):
            st = ln.strip()
            if not st or st.startswith((";", "#", "//")):
                continue
            if st.startswith("[") and "]" in st:
                section = st[1:st.index("]")].strip()
                continue
            if "=" in st:
                k, v = st.split("=", 1)
                out[f"{section}/{k.strip()}"] = v.strip()
        return out
    if kind == "xml":
        try:
            root = ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8"))
        except ET.ParseError:
            return None

        def walk(el, prefix):
            counts: dict[str, int] = {}
            totals: dict[str, int] = {}
            for c in el:
                totals[c.tag] = totals.get(c.tag, 0) + 1
            for k, v in el.attrib.items():
                out[f"{prefix}@{k}"] = v
            txt = (el.text or "").strip()
            if txt and len(el) == 0:
                out[prefix] = txt
            for c in el:
                if not isinstance(c.tag, str):
                    continue
                counts[c.tag] = counts.get(c.tag, 0) + 1
                name = c.tag + (f"[{counts[c.tag]}]" if totals[c.tag] > 1 else "")
                walk(c, f"{prefix}/{name}")
        walk(root, root.tag)
        return out
    return None


def setting_changes(old: Optional[bytes], new: Optional[bytes], path: str) -> Optional[list[dict]]:
    """Readable per-setting changes, or None if the file type has no settings structure."""
    a, b = setting_values(old, path), setting_values(new, path)
    if a is None and b is None:
        return None
    a, b = a or {}, b or {}
    sample = new if new is not None else old
    ftype = kind_for_path(path, detect_text(sample)[0] if sample else "")
    out = []
    for k in sorted(set(a) | set(b), key=lambda x: (x.split("/")[0], x)):
        if a.get(k) != b.get(k):
            kind = "added" if k not in a else ("removed" if k not in b else "changed")
            out.append({"setting": k, "old": a.get(k), "new": b.get(k), "kind": kind, "ftype": ftype})
    return out


def describe_change(c: dict) -> str:
    s = c["setting"]
    if c.get("ftype", "ini") == "ini" and "/" in s:
        sec, key = s.split("/", 1)
        label = f"[{sec}] {key}" if sec else key
    else:
        label = s
    if c["kind"] == "added":
        return f"{label}: added = {c['new']}"
    if c["kind"] == "removed":
        return f"{label}: removed (was {c['old']})"
    return f"{label}: {c['old']} → {c['new']}"

__all__ = ['IGNORE_KINDS', 'kind_for_path', 'normalize_content', '_ini_key_match', 'setting_values', 'setting_changes', 'describe_change']
