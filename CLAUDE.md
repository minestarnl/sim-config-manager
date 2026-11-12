# CLAUDE.md — working on Sim Config Manager

Instructions for Claude Code (and anyone else) continuing this project. Read this first.

## What this is

A Windows tray app for a flight-sim center (Prepar3D v5/v6 + ProSim, several sims) that tracks config files
like git: snapshots, numbered versions, restore points, alerts, rulebook, editor, certification archives,
sharing via a folder or Google Drive, an AI connector (Claude/ChatGPT over MCP), a remote app for Macs/laptops
on the LAN, sessions/performance, hardware & health, install recording, templates, and an optional
self-hosted Node.js **Hub** web dashboard.

* Repo: https://github.com/minestarnl/sim-config-manager (public). The owner does not write code; everything is
  AI-made. Keep the README disclaimer.
* Users are sim technicians and instructors, not developers. Write UI text and README for them.
* **The sim comes first.** Nothing may cost the sim frame rate (see "Lightweight rules").

## Layout

```
SimConfigManager.pyw        entry point (tray app; --remote, --minimized, --data, UAC relaunch, below-normal priority)
SimConfigManager-MCP.py     entry point of the AI connector (stdio MCP; --remote for laptops)
simconfigmanager/
  core/                     the engine — no Qt here. __init__ re-exports everything (`from simconfigmanager import core`)
    util.py                 APP_NAME (technical), APP_TITLE (shown to people), APP_VERSION, helpers
    models.py repo.py       dataclasses; Repo (SQLite + zlib object store); repo_method / schema / rows helpers
    alerts settings rulebook_ops probes archive sync proposals assistants system remote_ops hub
    sessions audit hardware installs templates maintenance manual   (features added in 1.12)
    winsys.py               the ONLY place that talks to Windows (PowerShell, nvidia-smi/NVML, event log); FAKES for tests
  ui/                       the desktop app (PySide6)
    window.py               MainWindow: tray, monitoring tick, activity bar, run_bg(), notify(), go(), set_badge()
    services.py             background work on the sim (sessions, hardware, health, audit, templates; quiet while flying)
    common.py               shared building blocks — ALWAYS use these (see "UI conventions")
    dialogs.py connect.py onboarding.py
    pages/*.py              one mixin per page; each declares PAGE = Page(key, title, icon, subtitle, build, refresh)
  editor.py                 built-in editor (uses ui.common helpers)
  mcp_server.py             MCP tools + prompts (read-only; changes only via propose_change)
  net.py remote.py          LAN access: /mcp for AI, /rpc for the remote app (RemoteRepo forwards ALLOWED methods)
  storage.py updater.py theme.py
hub/                        Node 18+, zero dependencies (src/server.js, store.js, explain.js, notify.js; public/ SPA)
tests/                      pytest; test_windows.py runs only on the Windows CI runner
tools/                      make_screenshots.py, hub_screenshots.py (README images)
installer/                  Inno Setup script (ASCII only!)
.github/workflows/build.yml CI: tests (Windows/mac/hub), PyInstaller exe + MCP exe, installer, mac .app, release
```

## Vocabulary — use exactly these words (app, editor, connector, Hub, README)

| Term | Meaning |
|---|---|
| tracked file | a file (or `system://` probe) the app records — not "monitored/watched" |
| snapshot | automatic record of all tracked files at one moment |
| revision | one entry in a single file's history (`core.Revision`; old alias `Version`) |
| version | numbered, named configuration (v1, v2 …); "current version" = latest |
| restore point | a named setup restored with one click |
| alert rule, ignored setting, rulebook (locked setting = has a required value) | |
| shared location | shared folder or Google Drive used by all sims |
| Hub | the optional web dashboard; a sim has a "sim key" there |
| AI assistants | Claude / ChatGPT; their edits are "proposed changes" |
| Sim Config Manager | the name shown to people (`APP_TITLE`); `SimConfigManager` only for files/exe/paths |

Page names: Fleet, Versions, Restore points, Files, History, Sessions, Hardware & health, Installs & integrity,
Templates, Rulebook, Alerts, Sharing, AI & network, Settings. Refer to UI places as "Page → Button".

## UI conventions (ui/common.py)

* Buttons: `button(text, slot, primary=, danger=, icon=, tip=)`, rows with `button_row(..., trailing=[...])`.
  Labels are verbs; add "…" when a dialog follows.
* Layout: `section(title)`, `heading()`, `hint()` (muted explanation), `banner(text, level)`.
* Messages: `confirm(parent, question, action_label, danger=, details=)` (button says the action, never Yes/No),
  `inform`, `warn`, `fail(parent, "do something", err, hint)` → "Could not do something." Results of actions:
  `self.notify(text)` (status bar, tray balloon when hidden).
* Colours: semantic constants `OK WARN BAD INFO MUTED ACCENT` (light values; `theme.fix` maps to dark). Use
  `colored()`, `file_state_item()`, `version_state_item()`, `diff_html()`. No new hard-coded colours.
* Before writing files to disk: include `running_warning(repo)` in the confirmation.
* Slow work never on the UI thread: `self.run_bg(fn, done, err, write=False)`; `write=True` for jobs that change
  the store (separate writable connection). In remote mode `fn` receives the RemoteRepo.
* New page: create `ui/pages/<name>.py` with a mixin class and `PAGE = Page(...)`, add it to `PAGE_ORDER` in
  `window.py`. Keep each page's minimum width under ~1000 px (group extra actions in a menu button).
* Window/page must work in **remote mode** (Mac/laptop): only call store methods (`self.repo.x()`), never
  touch local paths of the sim directly.

## Core conventions

* New store features: module-level functions decorated with `@repo_method` (attaches to `Repo` and allows it in
  remote mode), tables via `schema("CREATE TABLE IF NOT EXISTS …")`, queries via `rows(repo, sql, args)` (returns
  [] when an old/read-only store lacks the table). Add new columns with a guarded `ALTER TABLE`.
* All file writes go through `Repo.write_file` (safety snapshot before, history after, actor attribution).
* Anything Windows-specific goes through `core/winsys.py` with a tag so tests can fake it
  (`winsys.FAKES["devices"] = [...]`). Helper processes use `_no_window()` (no console, below-normal priority).
* Windows paths may be handled on a Mac/Linux (remote app, tests): split names with `re.split(r"[\\/]", p)`,
  not `os.path.basename`. Read files whose line endings matter with `read_bytes()` (keep CRLF).
* Things to also update when adding a feature: `mcp_server.py` tool(s), `core/hub.py` report section (+ Hub
  UI), `core/sync.readiness_extras` if it affects GO/NO-GO, `core/manual.py`, README, screenshots, tests.
* Version: bump `APP_VERSION` in `core/util.py` (semver). Hub version: `HUB_VERSION` in hub/src/server.js and
  hub/package.json.

## Lightweight rules (do not break)

* The app runs at below-normal priority; helper programs too.
* No polling of expensive things. File checks are stat-only. Hardware checks are triggered by
  WM_DEVICECHANGE (10-min fallback). GPU via NVML (pynvml), nvidia-smi max once a minute.
* While the sim runs ("quiet while flying", `self.flying()` in services.py): only session samples (10 s) and
  file checks. Postpone PowerShell, probes, health, template and other network checks until `catch_up()`.
* Heavy scans (manifests, install records, archives) only on user request, and ask first when the sim runs.
* The FSUIPC Lua bridge must stay read-only and tiny (reads once a second, writes every 5 s).

## Test and verify

```bash
pip install -r requirements.txt pytest pyflakes        # Python 3.12; on Linux add --break-system-packages if needed
python -m pyflakes simconfigmanager tools tests        # must be clean, except the intended re-exports in
                                                       # core/__init__.py and gui.py ("imported but unused", "import *")
python -m pytest -q tests                              # test_windows.py skips off Windows
cd hub && npm test                                     # node --test test/*.test.js
QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py   # also the UI smoke test; SCM_SHOTS_OUT=<dir> to not overwrite docs/
python tools/hub_screenshots.py                        # needs Python playwright + chromium
```

* Look at the screenshots after UI changes (they reveal squeezed columns, unchecked boxes, too-wide pages).
* Windows-only behaviour (PowerShell, auditing, devices, event log) is only proven by `tests/test_windows.py` on
  the Windows CI runner — add a test there for new Windows integrations.
* Not testable here: real Prepar3D/FSUIPC, PJLink projectors, real cockpit hardware. Say so honestly.

## Release

1. Bump `APP_VERSION`, update README (feature list at the top, sections, screenshots), regenerate screenshots.
2. Commit to `main` and push. CI builds and **publishes the GitHub release automatically when APP_VERSION is
   new** (tags can't be pushed from the sandbox; don't try).
3. Wait for CI and check it: `gh api repos/minestarnl/sim-config-manager/actions/runs --jq '.workflow_runs[0]'`
   (use `gh api` REST — `gh` GraphQL commands are blocked by the proxy). Logs of a job are only available
   after it finishes. Confirm the release has 6 assets.
4. The app's updater installs releases (checks SHA256SUMS.txt).

## Gotchas learned the hard way

* `installer/SimConfigManager.iss` must stay ASCII.
* PJLink lines end with CR only (not LF).
* `Set-Acl` also rewrites the owner and fails; write only the SACL (`GetAccessControl('Audit')`).
* Event-log timestamps: `([DateTimeOffset]$_.TimeCreated).ToUnixTimeSeconds()` (not `Get-Date -UFormat %s`).
* PySide6 native event filter: `event_type` is a QByteArray (`.data()`), message via `ctypes.wintypes.MSG`.
* `core` star-exports only `__all__`; stdlib modules are not reachable as `core.time` etc.
* `QAction.triggered`/`clicked` pass a bool — connect slots with lambdas or via `common.button`.
* Smoke scripts: use `python -u`, `PYTHONPATH=.`, and `os._exit(0)` if Qt keeps threads alive.

## Commits

Small, focused commits with a clear message; end with the attribution lines your environment asks for.
Never commit secrets (service-account keys, Hub keys, access keys).
