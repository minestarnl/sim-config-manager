"""
Windows system access in one place: PowerShell queries, nvidia-smi, the event log.

Every query has a *tag*. Tests (and the screenshot tool) put fake answers in ``FAKES[tag]`` — a value or a
callable — so all features can be exercised on any OS. On other systems without a fake, queries return None.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable, Optional, Union

from .util import _no_window

FAKES: dict[str, Union[Any, Callable[..., Any]]] = {}
_PS = ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command"]
_UTF8 = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "


def _fake(tag: str, *args):
    v = FAKES[tag]
    return v(*args) if callable(v) else v


def run(cmd: list[str], timeout: float = 60) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, **_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 and not r.stdout:
        return None
    return r.stdout.decode("utf-8", errors="replace")


def powershell(tag: str, script: str, timeout: float = 60, *args) -> Optional[str]:
    if tag in FAKES:
        return _fake(tag, *args)
    if sys.platform != "win32":
        return None
    return run(_PS + [_UTF8 + script], timeout)


def powershell_json(tag: str, script: str, timeout: float = 60, *args) -> Optional[list]:
    """Run a PowerShell pipeline and return its objects as a list of dicts (None if not available)."""
    if tag in FAKES:
        return _fake(tag, *args)
    out = powershell(tag, f"@({script}) | ConvertTo-Json -Depth 4 -Compress", timeout)
    if out is None:
        return None
    out = out.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, list) else [data]


def ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# --------------------------------------------------------------------------- GPU

_NVML: dict = {}
_SMI_CACHE: list = [0.0, None]


def gpu_stats() -> Optional[dict]:
    """{'temp': °C, 'util': %, 'mem_used': MB, 'name': str} for the first NVIDIA GPU, or None.
    Uses the NVIDIA driver library directly (NVML: no extra process, microseconds); falls back to nvidia-smi at
    most once a minute."""
    if "gpu" in FAKES:
        return _fake("gpu")
    if _NVML.get("ok") is not False:
        try:
            import pynvml
            if not _NVML.get("ok"):
                pynvml.nvmlInit()
                _NVML["h"] = pynvml.nvmlDeviceGetHandleByIndex(0)
                name = pynvml.nvmlDeviceGetName(_NVML["h"])
                _NVML["name"] = name.decode() if isinstance(name, bytes) else name
                _NVML["ok"] = True
            h = _NVML["h"]
            return {"name": _NVML["name"],
                    "temp": float(pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)),
                    "util": float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu),
                    "mem_used": pynvml.nvmlDeviceGetMemoryInfo(h).used / 1048576}
        except Exception:  # noqa: BLE001 - no NVIDIA GPU / driver / package
            _NVML["ok"] = False
    import time as _t
    if _t.time() - _SMI_CACHE[0] < 60:
        return _SMI_CACHE[1]
    _SMI_CACHE[0] = _t.time()
    out = run(["nvidia-smi", "--query-gpu=name,temperature.gpu,utilization.gpu,memory.used",
               "--format=csv,noheader,nounits"], timeout=10)
    res = None
    if out:
        try:
            name, temp, util, mem = [x.strip() for x in out.strip().splitlines()[0].split(",")[:4]]
            res = {"name": name, "temp": float(temp), "util": float(util), "mem_used": float(mem)}
        except (ValueError, IndexError):
            res = None
    _SMI_CACHE[1] = res
    return res


# --------------------------------------------------------------------------- event log

def _iso(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def app_crashes(since: float, process: str = "") -> list[dict]:
    """Application Error (1000) events since a time: [{ts, app, module, code}]. Optionally for one exe."""
    script = (f"Get-WinEvent -FilterXPath \"*[System[Provider[@Name='Application Error'] and (EventID=1000) and "
              f"TimeCreated[@SystemTime>='{_iso(since)}']]]\" -LogName Application -ErrorAction SilentlyContinue | "
              "ForEach-Object { $p = $_.Properties; [pscustomobject]@{ts=([DateTimeOffset]$_.TimeCreated)."
              "ToUnixTimeSeconds(); app=$p[0].Value; module=$p[3].Value; code=$p[6].Value} }")
    out = powershell_json("app_crashes", script, 60, since, process) or []
    if process:
        out = [c for c in out if str(c.get("app", "")).lower() == process.lower()]
    return out


def display_resets(since: float) -> list[dict]:
    """Display driver stopped responding and recovered (TDR, System log 4101): [{ts, message}]."""
    script = (f"Get-WinEvent -FilterXPath \"*[System[(EventID=4101) and TimeCreated[@SystemTime>='{_iso(since)}']]]\" "
              "-LogName System -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{ts="
              "([DateTimeOffset]$_.TimeCreated).ToUnixTimeSeconds(); message=$_.Message} }")
    return powershell_json("display_resets", script, 60, since) or []


__all__ = ["FAKES", "run", "powershell", "powershell_json", "ps_quote", "gpu_stats", "app_crashes", "display_resets"]
