# Sim Config Manager

Keeps track of Prepar3D / ProSim (or any) config files on your sims, like git: every change is
recorded, you can see exactly what changed, and you can put everything back with one button.

![Versions overview — which files differ from the current version](docs/screenshots/versions.png)

## Screenshots

| | |
|---|---|
| **Quick restore** — one button per saved setup ![](docs/screenshots/quick-restore.png) | **Files** — every tracked file with its full history ![](docs/screenshots/files.png) |
| **Editor** — syntax highlighting, find & replace ![](docs/screenshots/editor-cfg.png) | **Live syntax check** — broken XML marked before you save ![](docs/screenshots/editor-xml-check.png) |
| **Alerts** — notifications and auto-undo for specific changes ![](docs/screenshots/alerts.png) | **Create version** — release notes pre-filled with what changed ![](docs/screenshots/create-version.png) |
| **Differences** — exactly what changed, line by line ![](docs/screenshots/diff.png) | |

## Download

Get **SimConfigManager.exe** from the [latest release](../../releases/latest) — no Python needed.
Every push to `main` is also built automatically: *Actions* tab → latest run → *Artifacts*.

To publish a new release, raise `APP_VERSION` in `simconfigmanager/core.py` and push to `main` — the build
publishes `v<version>` automatically (existing releases are never overwritten).

## Install (on each sim PC)

1. Download `SimConfigManager.exe` (or build it yourself: install Python 3.10+ from python.org, tick
   "Add python.exe to PATH", then double-click **build_exe.bat** → `dist\SimConfigManager.exe`).
2. Copy the exe to e.g. `C:\SimConfigManager\` on each sim and run it.
3. Windows asks for administrator rights when it starts (needed to restore files under Program Files /
   ProgramData) — click **Yes**.
4. *Settings* tab: give the PC a name (`Sim 1`, `Sim 2`) and tick **Start with Windows as administrator**.
   This creates a Task Scheduler task ("SimConfigManager", run with highest privileges at logon), so it starts
   elevated at boot **without** a UAC prompt. (The normal Startup/Run method cannot start admin programs.)

(The exe can be built once and copied to both sims. `run_from_source.bat` runs it without building.)

## First-time setup

1. **Files → Find P3D / ProSim configs…** suggests the usual files (Prepar3D.cfg, dll.xml, exe.xml,
   add-ons.cfg, Cameras.cfg, scenery.cfg, Controls, ProSim config.xml). Type a group prefix like `Sim 1`.
   Anything else: **Add files…** (any location) or **Add folder…** (all *.xml/*.cfg in a folder).
2. Set the sim up the way it should be, then **Quick restore → Save current setup as restore point…**
   (e.g. "Customer default", "Checkride 737", "Maintenance"). Right-click a button to colour it.
3. **Cloud / other sims**: choose a folder inside Google Drive (e.g. `G:\My Drive\SimConfigManager`)
   or a share on your server (`\\SERVER\SimConfig`). Use the same folder on both sims.

## What it does

| Feature | Where |
|---|---|
| **Versions**: number the whole configuration (v1, v2, …) and see at a glance what differs | Versions |
| Watches tracked files (every 5 s by default) and records a snapshot whenever one changes | automatic |
| Full history per file, with coloured line-by-line diffs | Files, History |
| Restore one file to any older version | Files → Restore this version |
| **Built-in editor** with syntax highlighting (CFG/INI, XML, JSON) | Files → Open in editor (or double-click) |
| One-click "restore everything" buttons (restore points), or only one group | Quick restore, tray menu |
| Safety snapshot before every restore, so every restore can be undone | History ("safety") |
| Warns if Prepar3D / ProSim are running before a restore (P3D rewrites Prepar3D.cfg on exit) | automatic |
| Alert rules with Windows notifications | Alerts |
| Optional **auto-undo**: lock a file to a restore point, or undo specific changes | Alerts → Action |
| Sync / off-site backup through Google Drive or a network share | Cloud / other sims |
| See the other sim's configs, compare them, copy a file across | Cloud / other sims |
| Rebuild a reinstalled PC from the backup (files, history, restore points, rules) | Cloud / other sims |
| Export changelog as Markdown (paste into Notion) or CSV (Notion database / Google Sheets) | History |
| **Certification archive**: all monitored files in one zip + REPORT.txt + checksums | Files → Export archive, tray |
| **Import archive** on this or another PC (paths translated, checksums verified) | Files → Import archive |
| Runs in the tray, starts with Windows, single instance | Settings |

### Alert rule types

* **Any change** / **deleted**
* **Line added / removed** matching a regex — e.g. removed line matching `FSUIPC` in dll.xml
* **INI/CFG value** — `GRAPHICS/TEXTURE_MAX_LOAD`, optionally "expected = 4096" → alert only if it isn't 4096
* **XML value** — ElementTree path, e.g. `.//Launch.Addon/Name`, or an attribute with `@`, e.g. `.//Server@ip`
* **No longer matches restore point** — drift alert; with "auto-undo" this *locks* the file to that setup

## Versions

At the end of a day of changes, click **★ Create new version…** (Versions tab or tray menu). The
version records **every tracked file** as it is at that moment, gets the next number (v1, v2, …), a title,
who made it, and release notes that are pre-filled with which files changed and the notes typed in the
editor. Versions are permanent — they can't be edited or deleted, so they work as a configuration record.

The **Versions** tab is the overview:

* A banner: **✔ All 23 files match v7**, or **⚠ 3 of 23 files differ from v7**
* Every file with its status: *Matches*, *Modified*, *Missing on disk*, or *Not in version* (added to
  tracking after the version was made). Tick "Only show files that differ" to filter.
* Compare with an older version via the drop-down
* **Show difference** / **Open in editor** / **Put selected back** / **Put ALL differing files back**
  (with a safety snapshot first)
* The list of all versions with their release notes, and **Compare with previous version** to see
  exactly what changed from v6 to v7
* **Export this version** creates the certification archive for that version

The tray tooltip always shows the status ("all files match v7" / "2 files differ from v7"), and you get a
notification the moment files start to differ (can be turned off in Settings). Archive reports state the
configuration version and whether the files match it. Versions are included in cloud sync and archives.

Restore points vs versions: **restore points** are named presets you switch between (e.g. "Checkride");
**versions** are the numbered, official history of the configuration.

## Editor

Double-click a file (or **Files → ✎ Open in editor**). Features:

* Syntax highlighting for P3D/ProSim CFG/INI, XML and JSON (switchable), line numbers
* Find / replace (Ctrl+F, Ctrl+H, F3), go to line (Ctrl+G), undo/redo, zoom, line wrap
* Live syntax check: broken XML, duplicate CFG keys, malformed lines are marked red in the margin
* **Save (Ctrl+S)** shows exactly what will change, asks for a short note, warns if the sim is running,
  and records the edit in History (with a safety copy first) — so every edit can be undone
* Keeps the file's original encoding (e.g. Windows-1252 in dll.xml, UTF-8 BOM) and Windows line endings
* Notices when the sim changes the file while you edit, and lets you compare / reload / keep your edits
* **View this version** in the file history opens an old version read-only, with a one-click restore

## Certification archive

**Files → Export archive (zip)…** creates e.g. `Sim_1_configs_2026-11-01_1650.zip` plus
`Sim_1_configs_2026-11-01_1650.zip.sha256`. Keep both together. The zip contains:

* `files/<group>/…` — readable copies of every monitored file
* `REPORT.txt` — PC name, date, who exported it, OS, your note, and for every file: path, size,
  last-modified date and SHA-256 checksum, plus the restore points and a signature line
* `CHECKSUMS.sha256` — standard checksum list (`sha256sum -c` compatible)
* `archive.json` — machine-readable index used for importing
* `history/` — (optional) the full version history, restore points and alert rules

The `.sha256` file next to the zip proves the zip has not been altered since export; the import screen
checks it automatically. You can also check it on any Windows PC with
`certutil -hashfile <zip> SHA256`.

**Files → Import archive…** shows every file, verifies its checksum, and suggests where it goes on this PC
(paths in the user profile, AppData and ProgramData are translated automatically — double-click a target to
change it). Options: write the files, keep monitoring them, and — on a fresh install — bring in the full
history, restore points and rules. A safety snapshot is taken first.

Restore points can also be exported this way (right-click a restore point → Export).

## Development

```
pip install -r requirements.txt pytest
python -m pytest tests                                   # engine tests
python SimConfigManager.pyw --no-elevate --data .\testdata  # run with a throw-away data folder
```

`simconfigmanager/core.py` — storage, snapshots, versions, rules, sync, archives ·
`gui.py` — main window · `editor.py` — built-in editor.
Regenerate the README screenshots with `QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py`.

## Upgrading from SimConfigKeeper (≤ 1.3)

The app was renamed in 1.4. Quit the old *SimConfigKeeper* from the tray, then start
`SimConfigManager.exe`: it picks up the existing data (history, versions, restore points, rules), and if
"Start with Windows" was on, it replaces the old startup task with the new one. The old exe can be deleted.

## Notes

* Data lives in `C:\ProgramData\SimConfigManager` (SQLite + compressed copies of every version). Installs
  from before the rename (*SimConfigKeeper*, ≤ 1.3) keep using their existing data folder automatically.
  Nothing is ever deleted from history.
* Sync never conflicts: each PC writes only its own `machines\<name>.json`; file versions are stored
  by content hash. Changing a PC's name starts a new manifest (the old one stays as a backup).
* Rename `Sim 1` / `Sim 2` in Settings *before* setting up sync.
