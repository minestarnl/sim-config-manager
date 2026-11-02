"""Claude Desktop hookup and helpers for AI assistants."""
from __future__ import annotations

from typing import TYPE_CHECKING

import glob

import json
import os
import shutil
import sys
from pathlib import Path


from .util import APP_NAME, detect_text, fmt_ts, is_binary, now_ts  # noqa: F401
from .models import STATUS_TEXT, TrackedFile  # noqa: F401
from .util import _no_window  # noqa: F401

if TYPE_CHECKING:
    from .repo import Repo

# --------------------------------------------------------------------------- Claude Desktop hookup

MCP_SERVER_KEY = "sim-config-manager"


def connector_command(data_dir: Path) -> tuple[str, list[str]]:
    """Command Claude Desktop should run to start the connector."""
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).with_name("SimConfigManager-MCP.exe")
        return str(exe), ["--data", str(data_dir)]
    script = Path(__file__).resolve().parents[2] / "SimConfigManager-MCP.py"
    return sys.executable.replace("pythonw.exe", "python.exe"), [str(script), "--data", str(data_dir)]


def claude_desktop_config_paths() -> list[Path]:
    """Claude Desktop's config file(s): the normal install and the Microsoft Store (MSIX) install."""
    paths = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        paths.append(Path(appdata) / "Claude" / "claude_desktop_config.json")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for pkg in glob.glob(os.path.join(local, "Packages", "Claude_*")):
            paths.append(Path(pkg) / "LocalCache" / "Roaming" / "Claude" / "claude_desktop_config.json")
    if sys.platform == "darwin":
        paths.append(Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json")
    return paths


def claude_desktop_connected() -> bool:
    for p in claude_desktop_config_paths():
        try:
            if MCP_SERVER_KEY in json.loads(p.read_text(encoding="utf-8")).get("mcpServers", {}):
                return True
        except (OSError, ValueError):
            continue
    return False


def connect_claude_desktop(data_dir: Path, enable: bool = True) -> list[str]:
    """Add (or remove) the connector in Claude Desktop's config. Keeps every other setting; makes a backup."""
    command, args = connector_command(data_dir)
    targets = [p for p in claude_desktop_config_paths() if p.parent.is_dir()] or claude_desktop_config_paths()[:1]
    written = []
    for p in targets:
        cfg = {}
        if p.exists():
            raw = p.read_text(encoding="utf-8")
            try:
                cfg = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise ValueError(f"{p} is not valid JSON — fix or delete it first.")
            shutil.copyfile(p, p.with_name(p.name + ".bak"))
        servers = cfg.setdefault("mcpServers", {})
        if enable:
            servers[MCP_SERVER_KEY] = {"command": command, "args": args}
        else:
            servers.pop(MCP_SERVER_KEY, None)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, p)
        written.append(str(p))
    return written


def files_as_markdown(repo: "Repo", files: list[TrackedFile], max_bytes: int = 200_000) -> str:
    """Selected files as one Markdown block — to paste into any Claude chat."""
    ver, status = repo.version_status()
    st = {x.file.id: x.status for x in status}
    out = [f"Config files from simulator PC **{repo.machine}** ({APP_NAME}, {fmt_ts(now_ts())})."]
    if ver:
        out.append(f"Current configuration version: {ver.label}.")
    used = 0
    for f in files:
        data = repo.read_current(f)
        out.append(f"\n### {f.display}  ({f.group})\n`{f.path}` — status vs version: "
                   f"{STATUS_TEXT.get(st.get(f.id, 'new'), '?')}")
        if data is None:
            out.append("_missing on disk_")
            continue
        if is_binary(data):
            out.append(f"_binary file, {len(data)} bytes_")
            continue
        text = detect_text(data)[0]
        if used + len(text) > max_bytes:
            out.append("_left out: size limit reached_")
            continue
        used += len(text)
        lang = "xml" if f.path.lower().endswith(".xml") else ("json" if f.path.lower().endswith(".json") else "ini")
        out.append(f"```{lang}\n{text.rstrip()}\n```")
    return "\n".join(out)


def allow_users_to_propose(data_dir: Path) -> None:
    """The connector runs as a normal user: make sure it may drop proposals into the proposals folder."""
    if sys.platform != "win32":
        return
    import subprocess
    d = Path(data_dir) / "proposals"
    d.mkdir(parents=True, exist_ok=True)
    # S-1-5-32-545 = the local "Users" group (works in every Windows language)
    subprocess.run(["icacls", str(d), "/grant", "*S-1-5-32-545:(OI)(CI)M", "/Q"], capture_output=True,
                   **_no_window())

__all__ = ['MCP_SERVER_KEY', 'connector_command', 'claude_desktop_config_paths', 'claude_desktop_connected', 'connect_claude_desktop', 'files_as_markdown', 'allow_users_to_propose']
