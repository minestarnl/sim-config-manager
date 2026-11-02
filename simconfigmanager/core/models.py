"""Data classes shared by the whole app."""
from __future__ import annotations


import os
from dataclasses import dataclass
from typing import Optional



# --------------------------------------------------------------------------- data classes

@dataclass
class TrackedFile:
    id: int
    path: str
    label: str
    group: str
    added_at: float
    active: bool = True

    @property
    def display(self) -> str:
        return self.label or os.path.basename(self.path)


@dataclass
class Snapshot:
    id: int
    created_at: float
    message: str
    kind: str          # manual | auto | baseline | safety | restore | import
    machine: str


@dataclass
class ConfigVersion:
    """A numbered, permanent release of the whole configuration (all tracked files)."""
    id: int
    number: int
    title: str
    notes: str
    snapshot_id: int
    created_at: float
    created_by: str
    machine: str

    @property
    def label(self) -> str:
        return f"v{self.number}" + (f" – {self.title}" if self.title else "")


@dataclass
class VersionStatus:
    file: "TrackedFile"
    status: str                 # match | modified | missing | new | restored-missing
    version_hash: Optional[str]
    current_hash: Optional[str]
    in_version: bool


STATUS_TEXT = {
    "match": "Matches",
    "modified": "Changed",
    "missing": "Missing on disk",
    "new": "Not in version",
    "gone": "Deleted (as in version)",
    "untracked": "No longer tracked",
}


@dataclass
class RestorePoint:
    id: int
    name: str
    snapshot_id: int
    description: str
    created_at: float
    color: str = ""


@dataclass
class Version:
    """One distinct version of a single file in its history."""
    snapshot_id: int
    created_at: float
    message: str
    hash: Optional[str]   # None = file did not exist


@dataclass
class Rule:
    id: int
    name: str
    file_id: Optional[int]        # None = applies to every tracked file
    kind: str
    pattern: str = ""
    expected: str = ""
    action: str = "notify"        # notify | revert
    enabled: bool = True


@dataclass
class Change:
    file: TrackedFile
    old_hash: Optional[str]
    new_hash: Optional[str]
    old_data: Optional[bytes]
    new_data: Optional[bytes]


@dataclass
class Alert:
    rule: Rule
    file: TrackedFile
    message: str
    reverted: bool = False

__all__ = ['TrackedFile', 'Snapshot', 'ConfigVersion', 'VersionStatus', 'STATUS_TEXT', 'RestorePoint', 'Version', 'Rule', 'Change', 'Alert']
