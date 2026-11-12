"""First-run setup: name the sim, pick the config files (found automatically), shared location, Hub, extras.

Shown the first time the app opens on a sim (and again from Settings → Run first-time setup…). Every step can be
skipped; everything can be changed later on its own page.
"""
from __future__ import annotations

import os
import re
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWizard, QWizardPage,
)

from .. import core
from ..core import APP_TITLE
from ..theme import TLabel as QLabel
from .common import OK, BAD, MUTED, button, colored, hint
from .dialogs import pick_files


def _page(title: str, subtitle: str) -> tuple[QWizardPage, QVBoxLayout]:
    p = QWizardPage()
    p.setTitle(title)
    p.setSubTitle(subtitle)
    return p, QVBoxLayout(p)


def recommended(group: str, path: str) -> bool:
    """Pre-tick the files people almost always want; the rest is listed but left unticked."""
    name = re.split(r"[\\/]", path)[-1].lower()       # Windows paths, also in the remote app on a Mac
    if group == "ProSim":
        return name in ("config.xml", "cockpitsetup.xml")
    if group.endswith("add-ons"):
        return False
    return name in ("prepar3d.cfg", "dll.xml", "exe.xml", "add-ons.cfg", "cameras.cfg", "scenery.cfg",
                    "standard.xml") or name.startswith("fsuipc")


class OnboardingWizard(QWizard):
    def __init__(self, win):
        super().__init__(win)
        self.win, self.repo = win, win.repo
        self.setWindowTitle(f"Set up {APP_TITLE}")
        self.setWizardStyle(QWizard.ModernStyle)
        self.setOption(QWizard.NoBackButtonOnStartPage, True)
        self.resize(820, 620)
        self.setButtonText(QWizard.FinishButton, "Finish")
        self.addPage(self._welcome())
        self.addPage(self._files())
        self.addPage(self._sharing())
        self.addPage(self._hub())
        self.addPage(self._extras())
        self.addPage(self._finish())
        self.currentIdChanged.connect(self._entered)

    # ------------------------------------------------------------------ pages
    def _welcome(self):
        p, lay = _page(f"Welcome to {APP_TITLE}",
                       "A few questions to get this sim set up. Every step can be skipped and changed later.")
        lay.addWidget(hint("The app keeps track of this sim's config files: every change is recorded, you can see "
                           "exactly what changed, and put everything back with one click."))
        form = QFormLayout()
        self.name = QLineEdit(self.repo.machine)
        self.name.setPlaceholderText("e.g. Sim 1")
        form.addRow("Name of this sim:", self.name)
        self.author = QLineEdit(self.repo.get("version_author") or os.environ.get("USERNAME", ""))
        self.author.setPlaceholderText("Shown with versions and changes you make")
        form.addRow("Your name:", self.author)
        lay.addLayout(form)
        lay.addWidget(hint("Use the same naming on every sim (Sim 1, Sim 2 …) — the Fleet page and the Hub show "
                           "them by this name."))
        lay.addStretch(1)
        return p

    def _files(self):
        p, lay = _page("Config files to track",
                       "Found on this PC. The usual ones are ticked — untick what you don't need, or add others.")
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["File", "Location"])
        self.tree.setColumnWidth(0, 300)
        self.tree.setRootIsDecorated(True)
        lay.addWidget(self.tree, 1)
        self.found_label = QLabel()
        row = QHBoxLayout()
        row.addWidget(self.found_label, 1)
        row.addWidget(button("Add other files…", self._add_files))
        row.addWidget(button("Search again", self._scan))
        lay.addLayout(row)
        self.sysinfo = QCheckBox("Also track system information: graphics driver, Windows version, sim software "
                                 "versions, displays, hardware")
        self.sysinfo.setChecked(sys.platform == "win32")
        self.sysinfo.setEnabled(sys.platform == "win32")
        lay.addWidget(self.sysinfo)
        self._scanned = False
        return p

    def _sharing(self):
        p, lay = _page("Shared location (optional)",
                       "A folder all sims use: an off-site backup of the history, and the Fleet page shows every "
                       "sim.")
        self.share = QComboBox()
        self.share.addItem("Not now", "")
        self.share.addItem("A shared folder (Google Drive for Desktop, network share …)", "folder")
        self.share.addItem("Google Drive, direct — set up later on the Sharing page", "gdrive")
        lay.addWidget(self.share)
        row = QHBoxLayout()
        self.share_folder = QLineEdit(self.repo.get("sync_folder"))
        self.share_folder.setPlaceholderText(r"e.g. G:\My Drive\SimConfigManager  or  \\SERVER\SimConfig")
        row.addWidget(self.share_folder, 1)
        self.share_browse = button("Browse…", self._browse_share)
        row.addWidget(self.share_browse)
        lay.addLayout(row)
        lay.addWidget(hint("Use the same folder on every sim. Google Drive direct needs a key file or a sign-in; "
                           "that's easier on the Sharing page afterwards."))
        lay.addStretch(1)
        self.share.currentIndexChanged.connect(self._share_mode)
        self.share.setCurrentIndex(1 if self.repo.get("sync_folder") else 0)
        self._share_mode()
        return p

    def _hub(self):
        p, lay = _page("Hub dashboard (optional)",
                       "If you run the Sim Config Manager Hub, this sim reports its status to it.")
        form = QFormLayout()
        self.hub_url = QLineEdit(self.repo.get("hub_url"))
        self.hub_url.setPlaceholderText("e.g. https://hub.example.com or http://192.168.10.5:8780 — leave empty "
                                        "if you don't use one")
        self.hub_key = QLineEdit(self.repo.get("hub_key"))
        self.hub_key.setEchoMode(QLineEdit.Password)
        self.hub_key.setPlaceholderText("Key for this sim (in the Hub: Sims & settings → Add sim)")
        form.addRow("Hub address:", self.hub_url)
        form.addRow("Sim key:", self.hub_key)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addWidget(button("Test connection", self._test_hub))
        self.hub_result = QLabel()
        self.hub_result.setTextFormat(Qt.RichText)
        self.hub_result.setWordWrap(True)
        row.addWidget(self.hub_result, 1)
        lay.addLayout(row)
        lay.addWidget(hint("Only a status summary is sent (versions, which files differ, alerts, hardware) — never "
                           "file contents."))
        lay.addStretch(1)
        return p

    def _extras(self):
        p, lay = _page("Options", "Recommended for a sim; all can be changed in Settings.")
        win = sys.platform == "win32"
        self.autostart = QCheckBox("Start with Windows (in the system tray, as administrator)")
        self.autostart.setChecked(win and core.is_admin())
        self.autostart.setEnabled(win and core.is_admin())
        self.quiet = QCheckBox("Quiet while flying: postpone background checks while the sim runs")
        self.quiet.setChecked(self.repo.get("quiet_while_flying") != "0")
        self.audit = QCheckBox("Record which program and Windows user changed a file (Windows file auditing)")
        self.audit.setChecked(win)
        self.audit.setEnabled(win)
        self.bridge = QCheckBox("Install the live-data bridge for sessions (frame rate, aircraft) — needs "
                                "registered FSUIPC")
        self.bridge_folders = self.repo.fsuipc_folders() if win else []
        self.bridge.setEnabled(bool(self.bridge_folders))
        self.bridge.setChecked(bool(self.bridge_folders))
        if not self.bridge_folders:
            self.bridge.setText(self.bridge.text() + "  (FSUIPC not found)")
        for cb in (self.autostart, self.quiet, self.audit, self.bridge):
            lay.addWidget(cb)
        lay.addStretch(1)
        return p

    def _finish(self):
        p, lay = _page("Ready", "")
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.RichText)
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        self.make_version = QCheckBox("Create version v1 from these files now (the starting point to compare "
                                      "against)")
        self.make_version.setChecked(self.repo.latest_version() is None)
        self.make_version.setEnabled(self.repo.latest_version() is None)
        lay.addWidget(self.make_version)
        lay.addWidget(hint("Tip: when the sim is set up the way you want it, also save it as a restore point "
                           "(Restore points page) — then one click puts everything back."))
        lay.addStretch(1)
        return p

    # ------------------------------------------------------------------ behaviour
    def _entered(self, pid: int):
        if self.page(pid) is self.page(1) and not self._scanned:
            QTimer.singleShot(50, self._scan)
        if pid == len(self.pageIds()) - 1:
            n = len(self.chosen_files())
            self.summary.setText(
                f"<b>{n} file(s)</b> will be tracked on <b>{self.name.text().strip() or self.repo.machine}</b>."
                + ("<br>Shared location: " + (self.share_folder.text() if self.share.currentData() == "folder"
                                               else "Google Drive (finish on the Sharing page)")
                   if self.share.currentData() else "")
                + ("<br>Reports to the Hub at " + self.hub_url.text().strip() if self.hub_url.text().strip() else ""))

    def _scan(self):
        self._scanned = True
        self.tree.clear()
        self.found_label.setText("Searching…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            found = self.repo.discover()
        finally:
            QApplication.restoreOverrideCursor()
        tracked = {os.path.normcase(f.path) for f in self.repo.files()}
        groups: dict = {}
        for label, group, path in found:
            g = groups.get(group)
            if g is None:
                g = groups[group] = QTreeWidgetItem([group, ""])
                g.setFlags(g.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
                self.tree.addTopLevelItem(g)
            it = QTreeWidgetItem([label, path])
            it.setData(0, Qt.UserRole, (label, group, path))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            already = os.path.normcase(path) in tracked
            it.setCheckState(0, Qt.Checked if already or recommended(group, path) else Qt.Unchecked)
            if already:
                it.setText(0, label + "  (already tracked)")
                it.setDisabled(True)
            g.addChild(it)
        self.tree.expandAll()
        self.found_label.setText(colored(f"Found {len(found)} file(s) in {len(groups)} group(s).", OK) if found else
                                 colored("No Prepar3D / ProSim files found in the usual places — add them yourself.",
                                         BAD))

    def _add_files(self):
        paths = pick_files(self, self.repo, "Choose config files to track")
        if not paths:
            return
        g = QTreeWidgetItem(["Other", ""])
        g.setFlags(g.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsAutoTristate)
        self.tree.addTopLevelItem(g)
        for pth in paths:
            it = QTreeWidgetItem([os.path.basename(pth), pth])
            it.setData(0, Qt.UserRole, (os.path.basename(pth), "Other", pth))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(0, Qt.Checked)
            g.addChild(it)
        g.setExpanded(True)

    def chosen_files(self) -> list[tuple]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                it = g.child(j)
                if it.checkState(0) == Qt.Checked and not it.isDisabled():
                    out.append(it.data(0, Qt.UserRole))
        return out

    def _share_mode(self, *_):
        on = self.share.currentData() == "folder"
        self.share_folder.setEnabled(on)
        self.share_browse.setEnabled(on)

    def _browse_share(self):
        from .dialogs import pick_folder   # noqa: PLC0415
        d = pick_folder(self, self.repo, "Shared folder", self.share_folder.text())
        if d:
            self.share_folder.setText(os.path.normpath(d))

    def _test_hub(self):
        url, key = self.hub_url.text().strip(), self.hub_key.text().strip()
        if not url or not key:
            self.hub_result.setText(colored("Fill in the address and the key.", MUTED))
            return
        self.hub_result.setText("Testing…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.repo.set("machine_name", self.name.text().strip() or self.repo.machine)
            res = core.hub_push(self.repo, url, key, timeout=10)
            self.hub_result.setText(colored(f"✔ Connected — the Hub knows this sim as '{res.get('sim', '?')}'.", OK))
        except Exception as e:  # noqa: BLE001
            self.hub_result.setText(colored(f"✖ {e}", BAD))
        finally:
            QApplication.restoreOverrideCursor()

    # ------------------------------------------------------------------ apply
    def accept(self):
        repo, done, problems = self.repo, [], []
        name = self.name.text().strip()
        if name and name != repo.machine:
            repo.set("machine_name", name)
        if self.author.text().strip():
            repo.set("version_author", self.author.text().strip())
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            files = self.chosen_files()
            prefix = repo.machine
            for label, group, path in files:
                repo.add_file(path, label, f"{prefix} – {group}")
            if files:
                done.append(f"{len(files)} file(s) tracked")
            if self.sysinfo.isChecked():
                for key in ("gpu", "windows", "sim_software", "displays", "hardware"):
                    core.add_builtin_probe(repo, key, f"{prefix} – System")
                repo.background_probes = False          # first reading now
            repo.ensure_baseline()
            repo.background_probes = True
            mode = self.share.currentData()
            if mode == "folder" and self.share_folder.text().strip():
                repo.set("sync_mode", "folder")
                repo.set("sync_folder", self.share_folder.text().strip())
                done.append("shared folder set")
            elif mode == "gdrive":
                repo.set("sync_mode", "gdrive")
            if self.hub_url.text().strip() and self.hub_key.text().strip():
                repo.set("hub_url", self.hub_url.text().strip())
                repo.set("hub_key", self.hub_key.text().strip())
                repo.set("hub_enabled", "1")
                done.append("reporting to the Hub")
            repo.set("quiet_while_flying", "1" if self.quiet.isChecked() else "0")
            if self.autostart.isChecked() and self.autostart.isEnabled():
                try:
                    core.set_autostart(True)
                except OSError as e:
                    problems.append(f"Start with Windows: {e}")
            if self.audit.isChecked() and self.audit.isEnabled():
                res = repo.enable_file_auditing()
                if not res.get("ok"):
                    problems.append(f"Who changed it: {res.get('message')}")
                else:
                    repo.set("audit_enabled", "1")
            if self.bridge.isChecked() and self.bridge.isEnabled():
                try:
                    repo.install_bridge(self.bridge_folders[0])
                    done.append("live-data bridge installed (restart Prepar3D)")
                except Exception as e:  # noqa: BLE001
                    problems.append(f"Live-data bridge: {e}")
            if self.make_version.isChecked() and self.make_version.isEnabled() and repo.files():
                v = repo.create_version("Initial setup", "Created during first-time setup.",
                                        repo.get("version_author") or "")
                done.append(f"{v.label} created")
        finally:
            QApplication.restoreOverrideCursor()
        repo.set("onboarding_done", "1")
        repo.log("First-time setup: " + (", ".join(done) or "nothing changed")
                 + (" — problems: " + "; ".join(problems) if problems else ""))
        self.win.sync_dirty = True
        super().accept()
        if problems:
            from .common import warn
            warn(self.win, "Setup finished, but some things didn't work:\n\n" + "\n".join(problems))
        self.win.setWindowTitle(f"{APP_TITLE} — {repo.machine}")
        self.win.go("versions" if repo.latest_version() else "files")
        self.win.refresh_all()
        self.win.notify("Setup done: " + (", ".join(done) or "you can change everything later"))

    def reject(self):
        self.repo.set("onboarding_done", "1")   # don't ask again; Settings → Run first-time setup…
        super().reject()


def maybe_run_onboarding(win) -> None:
    """First start on a sim: no tracked files and setup never done or skipped."""
    if win.remote or win.repo.get("onboarding_done") == "1" or win.repo.files():
        return
    if getattr(win, "_onboarding", None) is not None and win._onboarding.isVisible():
        return
    win._onboarding = OnboardingWizard(win)
    win._onboarding.open()
