"""Settings: this computer's preferences, startup and updates."""
from __future__ import annotations


import html
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox,
    QPushButton, QSpinBox, QSystemTrayIcon, QWidget,
)

from ... import core, theme
from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import (APP_NAME, APP_VERSION)


from ..connect import open_connect_dialog
from ..common import open_in_explorer

class SettingsPage:
    """Mixin for MainWindow — Settings page."""

    def build_settings_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.machine_edit = QLineEdit(self.repo.machine)
        self.machine_edit.editingFinished.connect(self.save_machine)
        form.addRow("This PC's name (e.g. 'Sim 1')", self.machine_edit)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 3600)
        self.poll_spin.setSuffix(" s")
        self.poll_spin.setValue(int(self.repo.get("poll_seconds") or 5))
        self.poll_spin.valueChanged.connect(lambda v: (self.repo.set("poll_seconds", v), self.apply_poll_interval()))
        form.addRow("Check files every", self.poll_spin)
        cb = QCheckBox("Automatically record a snapshot whenever a file changes (recommended)")
        cb.setChecked(self.repo.get("auto_snapshot") == "1")
        cb.toggled.connect(lambda v: self.repo.set("auto_snapshot", "1" if v else "0"))
        form.addRow("", cb)
        cbv = QCheckBox("Notify me when files start to differ from the current version")
        cbv.setChecked(self.repo.get("notify_version_drift") != "0")
        cbv.toggled.connect(lambda v: self.repo.set("notify_version_drift", "1" if v else "0"))
        form.addRow("", cbv)
        self.autostart_cb = QCheckBox("Start with Windows as administrator (runs in the system tray)")
        self.autostart_cb.setChecked(False if self.remote else core.autostart_enabled())
        self.autostart_cb.setEnabled(sys.platform == "win32")
        self.autostart_cb.toggled.connect(self.toggle_autostart)
        if not self.remote:
            form.addRow("", self.autostart_cb)
        self.proc_edit = QLineEdit(self.repo.get("warn_processes"))
        self.proc_edit.editingFinished.connect(lambda: self.repo.set("warn_processes", self.proc_edit.text()))
        form.addRow("Warn before restoring while these run", self.proc_edit)
        if self.remote:
            form.addRow("Connected to", QLabel(f"<b>{html.escape(self.repo.machine)}</b> at "
                                               f"{html.escape(self.repo.base)} — {html.escape(self.host.get('os', ''))}"))
            form.addRow("Sim's app", QLabel(f"{self.host.get('app_version', '?')}"
                                            + ("" if self.host.get("admin") else "  (not running as administrator)")))
        else:
            row = QHBoxLayout()
            lbl = QLabel(str(self.repo.data_dir))
            row.addWidget(lbl)
            b = QPushButton("Open")
            b.clicked.connect(lambda: open_in_explorer(str(self.repo.data_dir)))
            row.addWidget(b)
            row.addStretch()
            holder = QWidget()
            holder.setLayout(row)
            form.addRow("Data folder", holder)
        adm = True if self.remote else core.is_admin()
        if not self.remote:
            form.addRow("Administrator rights", QLabel("<span style='color:#2e7d32'>yes</span>" if adm else
                                                       "<span style='color:#c62828'>no — restart the app and accept "
                                                       "the Windows prompt</span>"))
        b = QPushButton("Connect to another sim…")
        b.clicked.connect(self.connect_other_sim)
        form.addRow("Remote", b)
        if sys.platform == "win32" and not adm:
            self.autostart_cb.setEnabled(False)
            self.autostart_cb.setToolTip("Needs administrator rights to set up")
        form.addRow("", QLabel(f"{APP_NAME} {APP_VERSION}"))
        upd = QWidget()
        ul = QHBoxLayout(upd)
        ul.setContentsMargins(0, 0, 0, 0)
        b = QPushButton("Check for updates")
        b.clicked.connect(lambda: self.check_updates(manual=True))
        ul.addWidget(b)
        self.upd_daily = QCheckBox("Check daily")
        self.upd_daily.setChecked(self.pref("update_check") != "0")
        self.upd_daily.toggled.connect(lambda v: self.set_pref("update_check", "1" if v else "0"))
        ul.addWidget(self.upd_daily)
        ul.addStretch()
        form.addRow("Updates", upd)
        self.upd_token = QLineEdit(self.pref("github_token"))
        self.upd_token.setEchoMode(QLineEdit.Password)
        self.upd_token.setPlaceholderText("Only needed while the GitHub repository is private (read-only token)")
        self.upd_token.editingFinished.connect(lambda: self.set_pref("github_token", self.upd_token.text().strip()))
        form.addRow("GitHub access token", self.upd_token)
        self.theme_combo = QComboBox()
        for label, val in (("Dark (VS Code Dark Modern)", "dark"), ("Light (VS Code Light Modern)", "light"),
                           ("Follow the system setting", "system")):
            self.theme_combo.addItem(label, val)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(theme.saved_choice())))
        self.theme_combo.currentIndexChanged.connect(self.change_theme)
        form.addRow("Theme", self.theme_combo)
        self.probe_spin = QSpinBox()
        self.probe_spin.setRange(1, 1440)
        self.probe_spin.setSuffix(" min")
        self.probe_spin.setValue(int(float(self.repo.get("probe_minutes") or 15)))
        self.probe_spin.valueChanged.connect(lambda v: self.repo.set("probe_minutes", v))
        form.addRow("Re-read system info every", self.probe_spin)
        return w

    def change_theme(self, _i=None):
        theme.save_choice(self.theme_combo.currentData())
        QMessageBox.information(self, APP_NAME, "The new theme is used the next time the app starts.")

    def connect_other_sim(self):
        win = open_connect_dialog(self)
        if win is not None:
            win.show_window()

    def check_updates(self, manual: bool = False):
        from ... import updater
        token = self.pref("github_token")

        def done(rel):
            self.set_pref("last_update_check", core.now_ts())
            if rel is None:
                if manual:
                    QMessageBox.information(self, APP_NAME, f"You have the latest version ({APP_VERSION}).")
                return
            if not manual:
                self.tray.showMessage(APP_NAME, f"Update available: {rel['tag']}. Open Settings → Check for updates.",
                                      QSystemTrayIcon.Information, 10000)
                return
            notes = rel["notes"][:1500] or "(no release notes)"
            if not updater.can_self_update():
                QMessageBox.information(self, APP_NAME, f"Version {rel['tag']} is available:\n{rel['url']}\n\n{notes}")
                return
            if QMessageBox.question(self, APP_NAME, f"Version {rel['tag']} is available (you have {APP_VERSION}).\n\n"
                                    f"{notes}\n\nDownload and install now? The app restarts by itself; your data "
                                    "and settings are kept.") != QMessageBox.Yes:
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                path = updater.fetch_installer(rel, token)
            except Exception as e:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(self, APP_NAME, f"Update failed: {e}")
                return
            QApplication.restoreOverrideCursor()
            self.repo.log(f"Installing update {rel['tag']}")
            updater.run_installer(path)
            self.quit_app()

        def fail(e):
            if manual:
                QMessageBox.warning(self, APP_NAME, str(e))
        self.run_bg(lambda r: updater.check(APP_VERSION, updater.DEFAULT_REPO, token), done, fail)

    def daily_update_check(self):
        if self.repo.get("update_check") == "0":
            return
        last = float(self.repo.get("last_update_check") or 0)
        if core.now_ts() - last > 86400:
            self.set_pref("last_update_check", core.now_ts())
            self.check_updates(manual=False)

    def save_machine(self):
        name = self.machine_edit.text().strip()
        if name and name != self.repo.machine:
            self.repo.set("machine_name", name)
            self.setWindowTitle(f"{APP_NAME} — {name}")
            self.sync_dirty = True

    def toggle_autostart(self, on: bool):
        try:
            core.set_autostart(on)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not change startup setting: {e}")
