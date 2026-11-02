"""System information tracked like files (system://...)."""
from __future__ import annotations


import json
import os
import re
import sys
from typing import Optional


from .models import TrackedFile  # noqa: F401
from .util import _no_window  # noqa: F401

# =========================================================================== system information (#3)

PROBE_PREFIX = "system://"


def is_remote(repo) -> bool:
    return bool(getattr(repo, "is_remote", False))


def is_probe(f) -> bool:
    return bool(f) and str(getattr(f, "path", f)).startswith(PROBE_PREFIX)


def norm_path(path: str) -> str:
    return path.lower() if path.startswith(PROBE_PREFIX) else os.path.normcase(os.path.abspath(path))


_PS_UTF8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "

# key -> (label, PowerShell). Output must be stable (no timestamps) so a change means something changed.
BUILTIN_PROBES = {
    "gpu": ("Graphics cards & driver", r"""
Get-CimInstance Win32_VideoController | Sort-Object Name | ForEach-Object {
  "$($_.Name)"; "  Driver version : $($_.DriverVersion)"
  if ($_.DriverDate) { "  Driver date    : $($_.DriverDate.ToString('yyyy-MM-dd'))" }
  "  Video processor: $($_.VideoProcessor)" }
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
  ""; "nvidia-smi:"; nvidia-smi --query-gpu=name,driver_version,vbios_version,pcie.link.gen.max --format=csv,noheader }
"""),
    "windows": ("Windows version", r"""
$v = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$os = Get-CimInstance Win32_OperatingSystem
"$($os.Caption)"; "Version $($v.DisplayVersion)  build $($v.CurrentBuild).$($v.UBR)"; "Edition $($v.EditionID)"
"Architecture $($os.OSArchitecture)"
"""),
    "sim_software": ("Sim software versions", r"""
$keys = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*','HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
$pat = '__PATTERN__'
Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and $_.DisplayName -match $pat } |
  Sort-Object DisplayName -Unique | ForEach-Object { "{0}  |  {1}  |  {2}" -f $_.DisplayName, $_.DisplayVersion, $_.Publisher }
"""),
    "all_software": ("All installed software", r"""
$keys = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*','HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
Get-ItemProperty $keys -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -and -not $_.SystemComponent } |
  Sort-Object DisplayName -Unique | ForEach-Object { "{0}  |  {1}" -f $_.DisplayName, $_.DisplayVersion }
"""),
    "displays": ("Displays / projectors", r"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Screen]::AllScreens | Sort-Object DeviceName | ForEach-Object {
  "{0}  {1}x{2} at {3},{4}{5}" -f $_.DeviceName, $_.Bounds.Width, $_.Bounds.Height, $_.Bounds.X, $_.Bounds.Y, $(if ($_.Primary) {'  (primary)'} else {''}) }
Get-CimInstance -Namespace root\wmi -ClassName WmiMonitorID -ErrorAction SilentlyContinue | ForEach-Object {
  $n = ($_.UserFriendlyName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
  $m = ($_.ManufacturerName | Where-Object { $_ -ne 0 } | ForEach-Object { [char]$_ }) -join ''
  "Monitor: $m $n" } | Sort-Object
"""),
    "hardware": ("CPU, memory, motherboard", r"""
$c = Get-CimInstance Win32_Processor | Select-Object -First 1
"CPU: $($c.Name.Trim()) ($($c.NumberOfCores) cores / $($c.NumberOfLogicalProcessors) threads)"
"RAM: {0:N0} GB" -f ((Get-CimInstance Win32_PhysicalMemory | Measure-Object Capacity -Sum).Sum / 1GB)
$b = Get-CimInstance Win32_BaseBoard; "Motherboard: $($b.Manufacturer) $($b.Product)"
"BIOS: $((Get-CimInstance Win32_BIOS).SMBIOSBIOSVersion)"
Get-CimInstance Win32_DiskDrive | Sort-Object Index | ForEach-Object { "Disk $($_.Index): $($_.Model) {0:N0} GB" -f ($_.Size/1GB) }
"""),
    "network": ("Network addresses", r"""
Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' } |
  Sort-Object InterfaceAlias | ForEach-Object { "{0}: {1}/{2} ({3})" -f $_.InterfaceAlias, $_.IPAddress, $_.PrefixLength, $_.PrefixOrigin }
"""),
}
DEFAULT_SIM_SOFTWARE_PATTERN = ("Prepar3D|Lockheed|ProSim|FSUIPC|SimConnect|Orbx|Active ?Sky|ChasePlane|PMDG|Aerosoft|"
                                "NVIDIA|Leo Bodnar|Saitek|Logitech|Thrustmaster|Honeycomb|VATSIM|vPilot|Navigraph|"
                                "FlyTampa|FSDreamTeam|REX|Envtex|EZdok|SODE|Immersive Display|Fly\\.?Elise|"
                                "Visual C\\+\\+|DirectX|\\.NET")


def probe_command(repo, path: str) -> Optional[list[str]]:
    key = path[len(PROBE_PREFIX):]
    if key in BUILTIN_PROBES:
        if sys.platform != "win32":
            return None
        script = BUILTIN_PROBES[key][1]
        if key == "sim_software":
            pat = repo.get("sim_software_pattern") or DEFAULT_SIM_SOFTWARE_PATTERN
            script = script.replace("__PATTERN__", pat.replace("'", "''"))
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                _PS_UTF8 + script]
    custom = custom_probes(repo).get(key)
    if not custom:
        return None
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command",
                _PS_UTF8 + custom["command"]]
    return ["/bin/sh", "-c", custom["command"]]


def custom_probes(repo) -> dict:
    try:
        return json.loads(repo.get("custom_probes") or "{}")
    except ValueError:
        return {}


def add_custom_probe(repo, label: str, command: str, group: str = "System") -> TrackedFile:
    probes = custom_probes(repo)
    key = "custom_" + re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40]
    probes[key] = {"label": label, "command": command}
    repo.set("custom_probes", json.dumps(probes))
    return repo.add_file(PROBE_PREFIX + key, label, group)


def run_probe(cmd: list[str], timeout: int = 90) -> Optional[bytes]:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = r.stdout.decode("utf-8", errors="replace")
    lines = [ln.rstrip() for ln in out.replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    text = "\n".join(lines)
    if r.returncode != 0 and not text:
        err = r.stderr.decode("utf-8", errors="replace").strip()
        text = f"(command failed: {err[:300]})"
    return (text + "\n").encode("utf-8")


def add_builtin_probe(repo, key: str, group: str = "System") -> TrackedFile:
    label = BUILTIN_PROBES[key][0]
    return repo.add_file(PROBE_PREFIX + key, label, group)

__all__ = ['PROBE_PREFIX', 'is_remote', 'is_probe', 'norm_path', '_PS_UTF8', 'BUILTIN_PROBES', 'DEFAULT_SIM_SOFTWARE_PATTERN', 'probe_command', 'custom_probes', 'add_custom_probe', 'run_probe', 'add_builtin_probe']
