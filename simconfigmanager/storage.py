"""
Where shared data (backups, other sims' configs, fleet status, archives) is stored.

    LocalBackend  – any folder: Google Drive for Desktop, a network share (\\\\server\\share), Dropbox …
    DriveBackend  – Google Drive directly over its API (no Drive app needed on the PC):
                    • service account  → a folder in a *Shared Drive* (recommended for sims: no login, no expiry)
                    • Google account   → sign in once in the browser (scope drive.file: the app only sees the
                                         files it created itself)

Keys look like paths: "objects/ab/cdef…", "machines/Sim_1.json", "status/Sim_1.json", "archives/x.zip".
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional

FOLDER_MIME = "application/vnd.google-apps.folder"
SCOPE_FULL = "https://www.googleapis.com/auth/drive"
SCOPE_FILE = "https://www.googleapis.com/auth/drive.file"


class StorageError(Exception):
    pass


class LocalBackend:
    kind = "folder"

    def __init__(self, folder: str):
        self.folder = Path(folder)

    def describe(self) -> str:
        return str(self.folder)

    def ok(self) -> bool:
        return bool(str(self.folder)) and self.folder.is_dir()

    def _p(self, key: str) -> Path:
        return self.folder.joinpath(*key.split("/"))

    def read(self, key: str) -> bytes:
        p = self._p(key)
        if not p.exists():
            raise FileNotFoundError(key)
        return p.read_bytes()

    def write(self, key: str, data: bytes) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, p)

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def list(self, prefix: str) -> list[str]:
        """Names directly in a top-level area (e.g. 'machines' → ['Sim_1.json'])."""
        d = self._p(prefix)
        return sorted(p.name for p in d.iterdir() if p.is_file() and not p.name.endswith(".tmp")) \
            if d.is_dir() else []

    def object_names(self) -> set[str]:
        """All stored object hashes."""
        d = self.folder / "objects"
        out = set()
        if d.is_dir():
            for sub in d.iterdir():
                if sub.is_dir():
                    out.update(sub.name + f.name for f in sub.iterdir() if not f.name.endswith(".tmp"))
        return out


# --------------------------------------------------------------------------- Google Drive

class DriveBackend:
    """Minimal Google Drive v3 client (stdlib HTTP). Works with My Drive folders and Shared Drives."""
    kind = "gdrive"
    _cache: dict = {}          # (root, key) -> (time, bytes) — short read cache shared by all instances
    _lock = threading.Lock()
    CACHE_SECONDS = 30

    def __init__(self, token_provider: Callable[[], str], root_id: str, api_base: Optional[str] = None,
                 label: str = "Google Drive"):
        self.token_provider = token_provider
        self.root_id = root_id
        self.api = (api_base or os.environ.get("SCM_DRIVE_API") or "https://www.googleapis.com").rstrip("/")
        self.label = label
        self._folders: dict[str, str] = {}
        self._ids: dict[tuple[str, str], str] = {}

    def describe(self) -> str:
        return f"{self.label} (folder id {self.root_id})"

    # ----- HTTP
    def _req(self, method: str, url: str, data: Optional[bytes] = None, headers: Optional[dict] = None,
             raw: bool = False, want_headers: bool = False):
        h = {"Authorization": f"Bearer {self.token_provider()}"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = r.read()
                    if want_headers:
                        return body, r.headers
                    return body if raw else (json.loads(body) if body else {})
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:500]
                if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                if e.code == 404:
                    raise FileNotFoundError(url)
                if e.code in (401, 403):
                    raise StorageError(f"Google Drive refused access ({e.code}). Check that the account has access "
                                       f"to the folder (Content manager on a Shared Drive). {text}")
                raise StorageError(f"Google Drive error {e.code}: {text}")
            except (urllib.error.URLError, OSError) as e:
                if attempt < 3:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise StorageError(f"Cannot reach Google Drive: {e}")

    def _q(self, **params) -> str:
        params.setdefault("supportsAllDrives", "true")
        return urllib.parse.urlencode(params)

    def _list_q(self, q: str, fields: str = "nextPageToken,files(id,name)") -> list[dict]:
        out, token = [], None
        while True:
            params = {"q": q, "fields": fields, "pageSize": "1000", "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            r = self._req("GET", f"{self.api}/drive/v3/files?{self._q(**params)}")
            out += r.get("files", [])
            token = r.get("nextPageToken")
            if not token:
                return out

    @staticmethod
    def _esc(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'")

    def _folder(self, name: str, create: bool = True) -> Optional[str]:
        if name in self._folders:
            return self._folders[name]
        hits = self._list_q(f"name='{self._esc(name)}' and '{self.root_id}' in parents and "
                            f"mimeType='{FOLDER_MIME}' and trashed=false")
        if hits:
            self._folders[name] = hits[0]["id"]
        elif create:
            r = self._req("POST", f"{self.api}/drive/v3/files?{self._q(fields='id')}",
                          json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": [self.root_id]}).encode(),
                          {"Content-Type": "application/json"})
            self._folders[name] = r["id"]
        else:
            return None
        return self._folders[name]

    @staticmethod
    def _split(key: str) -> tuple[str, str]:
        area, _, rest = key.partition("/")
        return area, rest.replace("/", "")      # objects/ab/cdef -> ("objects", "abcdef")

    def _file_id(self, key: str) -> Optional[str]:
        area, name = self._split(key)
        if (area, name) in self._ids:
            return self._ids[(area, name)]
        fid = self._folder(area, create=False)
        if not fid:
            return None
        hits = self._list_q(f"name='{self._esc(name)}' and '{fid}' in parents and trashed=false")
        if not hits:
            return None
        self._ids[(area, name)] = hits[0]["id"]
        return hits[0]["id"]

    # ----- backend interface
    def ok(self) -> bool:
        try:
            self._req("GET", f"{self.api}/drive/v3/files/{self.root_id}?{self._q(fields='id,name')}")
            return True
        except Exception:  # noqa: BLE001
            return False

    def check(self) -> str:
        """Raise a clear error if the folder can't be used; return its name."""
        r = self._req("GET", f"{self.api}/drive/v3/files/{self.root_id}?{self._q(fields='id,name,mimeType')}")
        if r.get("mimeType") and r["mimeType"] != FOLDER_MIME:
            raise StorageError("The given ID is not a folder.")
        return r.get("name", self.root_id)

    def read(self, key: str) -> bytes:
        ck = (self.root_id, key)
        with self._lock:
            hit = self._cache.get(ck)
        if hit and (key.startswith("objects/") or time.time() - hit[0] < self.CACHE_SECONDS):
            return hit[1]
        fid = self._file_id(key)
        if not fid:
            raise FileNotFoundError(key)
        data = self._req("GET", f"{self.api}/drive/v3/files/{fid}?{self._q(alt='media')}", raw=True)
        with self._lock:
            self._cache[ck] = (time.time(), data)
        return data

    def write(self, key: str, data: bytes) -> None:
        area, name = self._split(key)
        fid = self._file_id(key)
        if fid:   # update content
            url = f"{self.api}/upload/drive/v3/files/{fid}?{self._q(uploadType='resumable')}"
            _, hdrs = self._req("PATCH", url, b"{}", {"Content-Type": "application/json; charset=UTF-8"},
                                want_headers=True)
        else:
            parent = self._folder(area)
            meta = json.dumps({"name": name, "parents": [parent]}).encode()
            url = f"{self.api}/upload/drive/v3/files?{self._q(uploadType='resumable', fields='id')}"
            _, hdrs = self._req("POST", url, meta, {"Content-Type": "application/json; charset=UTF-8"},
                                want_headers=True)
        session = hdrs.get("Location")
        if not session:
            raise StorageError("Google Drive did not return an upload session.")
        r = self._req("PUT", session, data, {"Content-Type": "application/octet-stream"})
        if not fid and isinstance(r, dict) and r.get("id"):
            self._ids[(area, name)] = r["id"]
        with self._lock:
            self._cache[(self.root_id, key)] = (time.time(), data)

    def exists(self, key: str) -> bool:
        return self._file_id(key) is not None

    def list(self, prefix: str) -> list[str]:
        fid = self._folder(prefix, create=False)
        if not fid:
            return []
        files = self._list_q(f"'{fid}' in parents and trashed=false")
        for f in files:
            self._ids[(prefix, f["name"])] = f["id"]
        return sorted(f["name"] for f in files)

    def object_names(self) -> set[str]:
        return set(self.list("objects"))


# --------------------------------------------------------------------------- credentials

def service_account_token_provider(key_file: str) -> Callable[[], str]:
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2 import service_account  # type: ignore
    creds = service_account.Credentials.from_service_account_file(key_file, scopes=[SCOPE_FULL])
    lock = threading.Lock()

    def token() -> str:
        with lock:
            if not creds.valid:
                creds.refresh(Request())
            return creds.token
    return token


def service_account_email(key_file: str) -> str:
    try:
        return json.loads(Path(key_file).read_text(encoding="utf-8")).get("client_email", "")
    except (OSError, ValueError):
        return ""


def user_token_provider(token_file: str) -> Callable[[], str]:
    from google.auth.transport.requests import Request  # type: ignore
    from google.oauth2.credentials import Credentials  # type: ignore
    creds = Credentials.from_authorized_user_file(token_file, [SCOPE_FILE])
    lock = threading.Lock()

    def token() -> str:
        with lock:
            if not creds.valid:
                try:
                    creds.refresh(Request())
                except Exception as e:  # noqa: BLE001
                    raise StorageError(f"Google sign-in expired or was revoked — sign in again in the app ({e}).")
                try:
                    Path(token_file).write_text(creds.to_json(), encoding="utf-8")
                except OSError:
                    pass  # read-only process (connector): fine
            return creds.token
    return token


def user_sign_in(client_secrets_file: str, token_file: str) -> str:
    """Browser sign-in (Desktop OAuth client). Returns the signed-in email if available."""
    from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
    flow = InstalledAppFlow.from_client_secrets_file(client_secrets_file, scopes=[SCOPE_FILE])
    creds = flow.run_local_server(port=0, open_browser=True,
                                  success_message="Signed in. You can close this tab and return to SimConfigManager.")
    Path(token_file).write_text(creds.to_json(), encoding="utf-8")
    return getattr(creds, "account", "") or ""


def ensure_user_root(provider: Callable[[], str], name: str = "SimConfigManager", api_base: Optional[str] = None) -> str:
    """With drive.file, the app keeps its data in a folder it created itself in that account's My Drive."""
    tmp = DriveBackend(provider, "root", api_base)
    hits = tmp._list_q(f"name='{DriveBackend._esc(name)}' and 'root' in parents and mimeType='{FOLDER_MIME}' "
                       f"and trashed=false")
    if hits:
        return hits[0]["id"]
    r = tmp._req("POST", f"{tmp.api}/drive/v3/files?{tmp._q(fields='id')}",
                 json.dumps({"name": name, "mimeType": FOLDER_MIME, "parents": ["root"]}).encode(),
                 {"Content-Type": "application/json"})
    return r["id"]


def new_id() -> str:
    return uuid.uuid4().hex
