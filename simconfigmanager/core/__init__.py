"""
SimConfigManager core engine.

A small, git-like version store for individual files that can live anywhere on disk (P3D .cfg/.xml,
ProSim config.xml, ...), plus everything built on it.

Storage layout (data dir, default %APPDATA%\\SimConfigManager):
    repo.db                 SQLite: tracked files, snapshots, versions, restore points, rules, events
    objects/ab/cdef...      zlib-compressed file contents, addressed by SHA-256 (like git blobs)

Terms used everywhere (code, UI, connector, Hub):
    tracked file   a file (or system:// probe) the app records
    snapshot       the content of every tracked file at one moment (like a git commit); recorded automatically
    version        a numbered, named snapshot (v1, v2, ...) the team signs off on; "current version" = latest
    restore point  a named snapshot that can be restored with one click (like a git tag)
    alert rule     notifies when a file changes in a certain way
    rulebook       documented / locked setting values
    shared location  a shared folder or Google Drive used by all sims (history backup + fleet)
    Hub            the optional central web dashboard

Modules:
    util          Constants and small helpers: paths, time, hashing, text encoding, atomic writes.
    models        Data classes shared by the whole app.
    alerts        Alert rules: rule kinds, evaluation and text diffs.
    repo          The repository: tracked files, snapshots, versions, restore points, rules and events.
    settings      Ignored settings and setting-level diffs for .ini/.cfg and .xml files.
    probes        System information tracked like files (system://...).
    system        Operating-system integration: sim discovery, processes, elevation and startup.
    assistants    Claude Desktop hookup and helpers for AI assistants.
    proposals     Changes proposed by an AI assistant; applied only after approval in the app.
    archive       Portable paths, full-history manifests and the certification archive.
    rulebook_ops  Rulebook: documented and locked settings.
    sync          Sharing between sims: shared folder or Google Drive, fleet status.
    remote_ops    Server-side operations used by the remote app (another computer on the LAN).
    hub           Optional Hub: status reports for the central web dashboard.
    winsys        Windows system access (PowerShell, nvidia-smi, event log) with fakes for tests.
    sessions      Sessions, live sim data (FSUIPC bridge), performance per version, benchmarks.
    audit         Who changed a file (Windows file auditing).
    hardware      Cockpit hardware watchdog (required USB/HID/COM devices).
    installs      Install recordings with rollback, and integrity manifests.
    templates     Shared templates with per-sim variables and staged rollout.
    maintenance   Health (disks, GPU, projectors over PJLink) and the maintenance schedule.
    manual        Auto-generated configuration manual.
"""
from .util import *  # noqa: F401,F403
from .models import *  # noqa: F401,F403
from .alerts import *  # noqa: F401,F403
from .repo import *  # noqa: F401,F403
from .settings import *  # noqa: F401,F403
from .probes import *  # noqa: F401,F403
from .system import *  # noqa: F401,F403
from .assistants import *  # noqa: F401,F403
from .proposals import *  # noqa: F401,F403
from .archive import *  # noqa: F401,F403
from .rulebook_ops import *  # noqa: F401,F403
from .sync import *  # noqa: F401,F403
from .remote_ops import *  # noqa: F401,F403
from .hub import *  # noqa: F401,F403
from . import winsys  # noqa: F401,E402
from .sessions import *  # noqa: F401,F403,E402
from .audit import *  # noqa: F401,F403,E402
from .hardware import *  # noqa: F401,F403,E402
from .installs import *  # noqa: F401,F403,E402
from .templates import *  # noqa: F401,F403,E402
from .maintenance import *  # noqa: F401,F403,E402
from .manual import *  # noqa: F401,F403,E402
