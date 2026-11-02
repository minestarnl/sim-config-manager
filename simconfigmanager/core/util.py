"""Constants and small helpers: paths, time, hashing, text encoding, atomic writes."""
from __future__ import annotations

import hashlib

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional



APP_NAME = "SimConfigManager"          # technical name: folders, executables, connector
APP_TITLE = "Sim Config Manager"       # name shown to people
APP_VERSION = "1.13.0"
LEGACY_NAMES = ("SimConfigKeeper",)   # earlier name of this app (≤ 1.3)


# --------------------------------------------------------------------------- helpers

def default_data_dir() -> Path:
    """C:\\ProgramData\\SimConfigManager — shared by every account, so it is the same store whether the
    app runs elevated or not. Stores created by earlier versions (old name / %APPDATA%) keep being used."""
    appdata = Path(os.environ.get("APPDATA") or os.path.join(Path.home(), ".config"))
    progdata = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) if sys.platform == "win32" else None
    candidates = []
    for name in (APP_NAME,) + LEGACY_NAMES:
        if progdata:
            candidates.append(progdata / name)
        candidates.append(appdata / name)
    for c in candidates:
        if (c / "repo.db").exists():
            return c
    return (progdata or appdata) / APP_NAME


def now_ts() -> float:
    return time.time()


def fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_binary(data: Optional[bytes]) -> bool:
    if not data:
        return False
    return b"\x00" in data[:8192]


def decode_text(data: Optional[bytes]) -> str:
    if data is None:
        return ""
    for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
        try:
            if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace")


@dataclass
class TextFormat:
    encoding: str = "utf-8"
    bom: bytes = b""
    newline: str = "\r\n"


def detect_text(data: Optional[bytes]) -> tuple[str, "TextFormat"]:
    """Decode bytes and remember encoding, BOM and line endings so a save writes the file back the same way."""
    if not data:
        return "", TextFormat()
    boms = [(b"\xef\xbb\xbf", "utf-8"), (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")]
    for bom, enc in boms:
        if data.startswith(bom):
            text = data[len(bom):].decode(enc, errors="replace")
            break
    else:
        bom = b""
        # XML may declare its own encoding (P3D dll.xml / exe.xml use Windows-1252) — honour it
        m = re.match(rb'\s*<\?xml[^>]*encoding=["\']([A-Za-z0-9_-]+)["\']', data[:200])
        candidates = ([m.group(1).decode().lower()] if m else []) + ["utf-8", "cp1252"]
        text = None
        for enc in candidates:
            try:
                text = data.decode(enc)
                break
            except (LookupError, UnicodeDecodeError):
                continue
        if text is None:
            enc = "cp1252"
            text = data.decode(enc, errors="replace")
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    newline = "\r\n" if crlf >= lf and crlf else ("\n" if lf else "\r\n")
    return text.replace("\r\n", "\n").replace("\r", "\n"), TextFormat(enc, bom, newline)


def encode_text(text: str, fmt: "TextFormat") -> bytes:
    body = text.replace("\r\n", "\n").replace("\n", fmt.newline)
    try:
        return fmt.bom + body.encode(fmt.encoding)
    except UnicodeEncodeError as e:
        bad = text[e.start:e.end] if hasattr(e, "start") else "?"
        raise ValueError(f"The character {bad!r} cannot be saved in this file's encoding ({fmt.encoding}).")


def validate_text(text: str, kind: str) -> list[tuple[int, str]]:
    """Return [(line number, problem)] — empty list means it looks fine."""
    problems: list[tuple[int, str]] = []
    if kind == "xml":
        try:
            ET.fromstring(re.sub(r"^\s*<\?xml[^>]*\?>", "", text).encode("utf-8"))
        except ET.ParseError as e:
            line = e.position[0] if getattr(e, "position", None) else 1
            problems.append((line, f"XML error: {e}"))
    elif kind == "json":
        try:
            json.loads(text)
        except ValueError as e:
            problems.append((getattr(e, "lineno", 1), f"JSON error: {e}"))
    elif kind == "ini":
        section, seen = "", {}
        for n, raw in enumerate(text.split("\n"), 1):
            line = raw.strip()
            if not line or line.startswith((";", "#", "//")):
                continue
            if line.startswith("["):
                if "]" not in line:
                    problems.append((n, "Section header is missing ']'"))
                    continue
                section = line[1:line.index("]")].strip().lower()
                continue
            if "=" not in line:
                problems.append((n, "Line has no '=' (not a key=value line)"))
                continue
            key = (section, line.split("=", 1)[0].strip().lower())
            if key in seen:
                problems.append((n, f"Duplicate key '{key[1]}' in [{section}] (first on line {seen[key]})"))
            else:
                seen[key] = n
    return problems


def read_file_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except (FileNotFoundError, NotADirectoryError):
        return None
    except PermissionError:
        raise


def write_file_atomic(path: str, data: bytes) -> None:
    """Write via temp file + replace so a crash never leaves a half-written config."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".sck-tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)

def _no_window():
    """Flags for helper programs (PowerShell, nvidia-smi …): no console window, below-normal priority so they
    never compete with the sim for CPU time."""
    return {"creationflags": 0x08000000 | 0x00004000} if sys.platform == "win32" else {}


__all__ = ['APP_TITLE', '_no_window', 'APP_NAME', 'APP_VERSION', 'LEGACY_NAMES', 'default_data_dir', 'now_ts', 'fmt_ts', 'sha256_bytes', 'is_binary', 'decode_text', 'TextFormat', 'detect_text', 'encode_text', 'validate_text', 'read_file_bytes', 'write_file_atomic']
