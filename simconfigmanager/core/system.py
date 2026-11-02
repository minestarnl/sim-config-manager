"""Operating-system integration: sim discovery, processes, elevation and startup."""
from __future__ import annotations

import glob

import json
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional


from .util import APP_NAME, LEGACY_NAMES, _no_window  # noqa: F401
from .assistants import MCP_SERVER_KEY, claude_desktop_config_paths, connect_claude_desktop, connector_command  # noqa: F401

# --------------------------------------------------------------------------- discovery of sim configs

def discover_sim_configs() -> list[tuple[str, str, str]]:
    """Suggest well-known P3D / ProSim config files that exist on this PC: [(label, group, path)]."""
    appdata = os.environ.get("APPDATA", "")
    progdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    home = str(Path.home())
    found: list[tuple[str, str, str]] = []
    seen = set()

    def add(label, group, path):
        key = os.path.normcase(os.path.abspath(path))
        if os.path.isfile(path) and key not in seen:
            seen.add(key)
            found.append((label, group, os.path.abspath(path)))

    for ver in ("v5", "v6", "v4"):
        grp = f"P3D {ver}"
        for base in (os.path.join(appdata, "Lockheed Martin", f"Prepar3D {ver}"),
                     os.path.join(progdata, "Lockheed Martin", f"Prepar3D {ver}")):
            scope = "AppData" if base.startswith(appdata) and appdata else "ProgramData"
            for name in ("Prepar3D.cfg", "dll.xml", "exe.xml", "add-ons.cfg", "Cameras.cfg", "scenery.cfg",
                         "Controls\\Standard.xml", "terrain.cfg", "SimObjects.cfg"):
                add(f"{os.path.basename(name)} ({scope})", grp, os.path.join(base, name))
            for x in glob.glob(os.path.join(base, "Controls", "*.xml")):
                add(f"Controls/{os.path.basename(x)} ({scope})", grp, x)
        docs = os.path.join(home, "Documents", f"Prepar3D {ver} Add-ons")
        for x in glob.glob(os.path.join(docs, "*", "add-on.xml")):
            add(f"add-on.xml: {Path(x).parent.name}", grp + " add-ons", x)

    # ProSim: install location varies — look in the usual roots, two levels deep.
    roots = ["C:\\", "D:\\", "E:\\", r"C:\Program Files", r"C:\Program Files (x86)",
             os.path.join(home, "Documents"), os.path.join(home, "Desktop")]
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            entries = os.listdir(root)
        except OSError:
            continue
        for d in entries:
            if "prosim" not in d.lower():
                continue
            top = os.path.join(root, d)
            for pattern in ("config.xml", "*/config.xml", "*/*/config.xml", "*.ini", "*/*.ini"):
                for x in glob.glob(os.path.join(top, pattern)):
                    rel = os.path.relpath(x, top)
                    add(f"ProSim {rel}", "ProSim", x)
    return found


# --------------------------------------------------------------------------- OS integration

def running_processes(names: Iterable[str]) -> list[str]:
    wanted = {n.strip().lower() for n in names if n.strip()}
    if not wanted:
        return []
    try:
        import psutil  # type: ignore
    except ImportError:
        return []
    hits = set()
    for p in psutil.process_iter(["name"]):
        try:
            n = (p.info.get("name") or "").lower()
        except Exception:  # noqa: BLE001
            continue
        if n in wanted:
            hits.add(p.info["name"])
    return sorted(hits)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
TASK_NAME = APP_NAME


def is_admin() -> bool:
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def launch_parts() -> tuple[str, str]:
    """(program, arguments) used to start the app at logon."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--minimized"
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[:-10] + "pythonw.exe"
    return exe, f'"{os.path.abspath(sys.argv[0])}" --minimized'


def relaunch_as_admin(extra_args: list[str]) -> bool:
    """Ask Windows (UAC) to start a new, elevated copy of this program. True if it started."""
    if sys.platform != "win32":
        return False
    import ctypes
    if getattr(sys, "frozen", False):
        exe, params = sys.executable, extra_args
    else:
        exe = sys.executable
        if exe.lower().endswith("python.exe"):
            exe = exe[:-10] + "pythonw.exe"
        params = [os.path.abspath(sys.argv[0])] + extra_args
    import subprocess
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, subprocess.list2cmdline(params), None, 1)
    return rc > 32


def _task_xml() -> str:
    from xml.sax.saxutils import escape
    prog, args = launch_parts()
    user = os.environ.get("USERNAME", "")
    dom = os.environ.get("USERDOMAIN", "")
    uid = f"{dom}\\{user}" if dom else user
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Starts {APP_NAME} with administrator rights at logon.</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(uid)}</UserId><Delay>PT20S</Delay></LogonTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>{escape(uid)}</UserId><LogonType>InteractiveToken</LogonType>
    <RunLevel>HighestAvailable</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author"><Exec><Command>{escape(prog)}</Command><Arguments>{escape(args)}</Arguments></Exec></Actions>
</Task>
"""


def _remove_run_key():
    for name in (APP_NAME,) + LEGACY_NAMES:
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, name)
        except OSError:
            pass


def _task_exists(name: str) -> bool:
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", name], capture_output=True, **_no_window())
    return r.returncode == 0


def migrate_legacy_autostart() -> bool:
    """If the app was set to start at logon under its old name, move that to the new task. Needs admin."""
    if sys.platform != "win32":
        return False
    import subprocess
    found = False
    for name in LEGACY_NAMES:
        if _task_exists(name):
            subprocess.run(["schtasks", "/Delete", "/TN", name, "/F"], capture_output=True, **_no_window())
            found = True
        try:
            import winreg  # type: ignore
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                winreg.QueryValueEx(k, name)
                found = True
        except OSError:
            pass
    if found:
        set_autostart(True)
    return found


def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    return _task_exists(TASK_NAME)


def set_autostart(enabled: bool) -> bool:
    """
    Start at logon WITH admin rights. The usual 'Run' registry key cannot start elevated programs
    (Windows silently skips them), so a Task Scheduler task with 'highest privileges' is used.
    Creating the task needs admin rights itself.
    """
    if sys.platform != "win32":
        return False
    import subprocess
    import tempfile
    _remove_run_key()  # v1.0 used the Run key
    if not enabled:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], capture_output=True, **_no_window())
        return True
    fd, xml_path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        Path(xml_path).write_text(_task_xml(), encoding="utf-16")
        r = subprocess.run(["schtasks", "/Create", "/TN", TASK_NAME, "/XML", xml_path, "/F"],
                           capture_output=True, text=True, **_no_window())
        if r.returncode != 0:
            raise OSError((r.stderr or r.stdout or "schtasks failed").strip())
    finally:
        try:
            os.remove(xml_path)
        except OSError:
            pass
    return True



# =========================================================================== keep startup / Claude paths current

def autostart_command() -> Optional[str]:
    """The program the start-at-logon task runs (None if no task)."""
    if sys.platform != "win32":
        return None
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", TASK_NAME, "/XML"], capture_output=True, **_no_window())
    if r.returncode != 0:
        return None
    text = r.stdout.decode("utf-16", errors="replace") if r.stdout[:2] in (b"\xff\xfe", b"\xfe\xff") \
        else r.stdout.decode(errors="replace")
    m = re.search(r"<Command>(.*?)</Command>", text, re.S)
    return m.group(1).strip() if m else None


def repair_paths_after_move(data_dir: Path) -> list[str]:
    """After installing/updating into another folder, point the logon task and Claude Desktop at this exe."""
    fixed = []
    if not getattr(sys, "frozen", False):
        return fixed
    cmd = autostart_command()
    if cmd and os.path.normcase(cmd) != os.path.normcase(sys.executable):
        set_autostart(True)
        fixed.append("start-at-logon task")
    want, _args = connector_command(data_dir)
    for p in claude_desktop_config_paths():
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cur = cfg.get("mcpServers", {}).get(MCP_SERVER_KEY, {}).get("command")
        if cur and os.path.normcase(cur) != os.path.normcase(want):
            connect_claude_desktop(data_dir, True)
            fixed.append("Claude Desktop connector")
            break
    return fixed

__all__ = ['discover_sim_configs', 'running_processes', 'RUN_KEY', 'TASK_NAME', 'is_admin', 'launch_parts', 'relaunch_as_admin', '_task_xml', '_remove_run_key', '_task_exists', 'migrate_legacy_autostart', 'autostart_enabled', 'set_autostart', 'autostart_command', 'repair_paths_after_move']
