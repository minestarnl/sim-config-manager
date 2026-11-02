"""Update check + install from GitHub releases (installer asset, verified against SHA256SUMS.txt)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from typing import Optional

DEFAULT_REPO = "minestarnl/sim-config-manager"
API = "https://api.github.com"


class UpdateError(Exception):
    pass


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:4]) or (0,)


def is_newer(tag: str, current: str) -> bool:
    return version_tuple(tag) > version_tuple(current)


def _headers(token: str, accept: str = "application/vnd.github+json") -> dict:
    h = {"Accept": accept, "User-Agent": "SimConfigManager-updater", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def latest_release(repo: str = DEFAULT_REPO, token: str = "", api: str = API) -> dict:
    req = urllib.request.Request(f"{api}/repos/{repo}/releases/latest", headers=_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise UpdateError("Can't see the releases. The repository is private: add a GitHub access token in "
                              "Settings (read-only access to this repository's contents), or make it public.")
        raise UpdateError(f"GitHub error {e.code}")
    except (urllib.error.URLError, OSError) as e:
        raise UpdateError(f"No connection to GitHub: {e}")
    return {"tag": data.get("tag_name", ""), "name": data.get("name", ""), "notes": data.get("body") or "",
            "url": data.get("html_url", ""),
            "assets": [{"name": a["name"], "api_url": a["url"], "download_url": a["browser_download_url"],
                        "size": a.get("size", 0)} for a in data.get("assets", [])]}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download_asset(asset: dict, token: str = "") -> bytes:
    """Private repos: fetch via the API with the token, then follow the storage redirect WITHOUT the token."""
    if not token:
        with urllib.request.urlopen(urllib.request.Request(asset["download_url"],
                                                           headers=_headers("", "*/*")), timeout=300) as r:
            return r.read()
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(asset["api_url"], headers=_headers(token, "application/octet-stream"))
    try:
        with opener.open(req, timeout=300) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            with urllib.request.urlopen(urllib.request.Request(e.headers["Location"],
                                                               headers={"User-Agent": "SimConfigManager-updater"}),
                                        timeout=300) as r:
                return r.read()
        raise UpdateError(f"Download failed ({e.code}).")


def fetch_installer(release: dict, token: str = "") -> str:
    """Download the installer and check it against SHA256SUMS.txt. Returns the local path."""
    inst = next((a for a in release["assets"] if a["name"].lower().startswith("simconfigmanager-setup")
                 and a["name"].lower().endswith(".exe")), None)
    sums = next((a for a in release["assets"] if a["name"] == "SHA256SUMS.txt"), None)
    if not inst:
        raise UpdateError("This release has no installer.")
    data = download_asset(inst, token)
    if sums:
        want = None
        for ln in download_asset(sums, token).decode("ascii", "replace").splitlines():
            parts = ln.strip().split()
            if len(parts) == 2 and parts[1].lstrip("*") == inst["name"]:
                want = parts[0].lower()
        if want and hashlib.sha256(data).hexdigest() != want:
            raise UpdateError("The downloaded installer does not match its checksum — not installing it.")
    d = tempfile.mkdtemp(prefix="scm-update-")
    path = os.path.join(d, inst["name"])
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def run_installer(path: str) -> None:
    """Start the installer silently; it closes the app, updates both programs and starts the app again."""
    if sys.platform != "win32":
        raise UpdateError("Installing updates is only supported on Windows.")
    subprocess.Popen([path, "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     close_fds=True, creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP


def can_self_update() -> bool:
    return sys.platform == "win32" and getattr(sys, "frozen", False)


def check(current: str, repo: str = DEFAULT_REPO, token: str = "") -> Optional[dict]:
    rel = latest_release(repo, token)
    return rel if is_newer(rel["tag"], current) else None
