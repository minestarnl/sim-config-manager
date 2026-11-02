"""Settings: this computer's preferences, startup and updates."""
from __future__ import annotations


import html
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout,
    QApplication, QCheckBox, QComboBox, QFormLayout, QLineEdit, QSpinBox, QWidget,
)

from ... import core, theme
from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import (APP_TITLE, APP_VERSION)


from ..connect import open_connect_dialog
from ..common import BAD, OK, button_row, colored, confirm, fail, inform, open_in_explorer, section
from .sharing import _row_widget

from . import Page


class SettingsPage:
    """Mixin for MainWindow — Settings page."""

    PAGE = Page("settings", "Settings", "settings", "This computer's preferences, startup and updates", "build_settings_page")

    def build_settings_page(self):
        w = QWidget()
        outer = QVBoxLayout(w)
        sim_box, form = section("This sim" + (f" — {self.repo.machine} (remote)" if self.remote else ""), QFormLayout)
        self.machine_edit = QLineEdit(self.repo.machine)
        self.machine_edit.editingFinished.connect(self.save_machine)
        form.addRow("Name (e.g. 'Sim 1'):", self.machine_edit)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 3600)
        self.poll_spin.setSuffix(" s")
        self.poll_spin.setValue(int(self.repo.get("poll_seconds") or 5))
        self.poll_spin.valueChanged.connect(lambda v: (self.repo.set("poll_seconds", v), self.apply_poll_interval()))
        form.addRow("Check files every:", self.poll_spin)
        self.probe_spin = QSpinBox()
        self.probe_spin.setRange(1, 1440)
        self.probe_spin.setSuffix(" min")
        self.probe_spin.setValue(int(float(self.repo.get("probe_minutes") or 15)))
        self.probe_spin.valueChanged.connect(lambda v: self.repo.set("probe_minutes", v))
        form.addRow("Read system information every:", self.probe_spin)
        cb = QCheckBox("Record a snapshot automatically whenever a file changes (recommended)")
        cb.setChecked(self.repo.get("auto_snapshot") == "1")
        cb.toggled.connect(lambda v: self.repo.set("auto_snapshot", "1" if v else "0"))
        form.addRow("", cb)
        cbv = QCheckBox("Notify when files start to differ from the current version")
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
        self.proc_edit.setPlaceholderText("Prepar3D.exe; ProSim737.exe; …")
        self.proc_edit.editingFinished.connect(lambda: self.repo.set("warn_processes", self.proc_edit.text()))
        form.addRow("Warn before restoring while these run:", self.proc_edit)
        if self.remote:
            form.addRow("Connected to:", QLabel(f"<b>{html.escape(self.repo.machine)}</b> at "
                                                f"{html.escape(self.repo.base)} — {html.escape(self.host.get('os', ''))}"))
            form.addRow("App on the sim:", QLabel(f"{self.host.get('app_version', '?')}"
                                                  + ("" if self.host.get("admin") else
                                                     "  (not running as administrator)")))
        else:
            form.addRow("Data folder:", _row_widget(button_row(QLabel(str(self.repo.data_dir)),
                                                               ("Open", lambda: open_in_explorer(str(self.repo.data_dir))))))
        adm = True if self.remote else core.is_admin()
        if not self.remote:
            form.addRow("Administrator rights:", QLabel(colored("Yes", OK) if adm else
                                                        colored("No — restart the app and accept the Windows prompt",
                                                                BAD)))
        if sys.platform == "win32" and not adm:
            self.autostart_cb.setEnabled(False)
            self.autostart_cb.setToolTip("Needs administrator rights to set up")
        outer.addWidget(sim_box)

        me_box, me = section("This computer", QFormLayout)
        self.theme_combo = QComboBox()
        for label, val in (("Dark (VS Code Dark Modern)", "dark"), ("Light (VS Code Light Modern)", "light"),
                           ("Follow the system setting", "system")):
            self.theme_combo.addItem(label, val)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(theme.saved_choice())))
        self.theme_combo.currentIndexChanged.connect(self.change_theme)
        me.addRow("Theme:", self.theme_combo)
        self.upd_daily = QCheckBox("Check daily")
        self.upd_daily.setChecked(self.pref("update_check") != "0")
        self.upd_daily.toggled.connect(lambda v: self.set_pref("update_check", "1" if v else "0"))
        me.addRow("Updates:", _row_widget(button_row(("Check for updates", lambda: self.check_updates(manual=True)),
                                                     self.upd_daily)))
        me.addRow("Other sims:", _row_widget(button_row(("Connect to another sim…", self.connect_other_sim))))
        me.addRow("Version:", QLabel(f"{APP_TITLE} {APP_VERSION}"))
        outer.addWidget(me_box)
        outer.addStretch(1)
        return w

    def change_theme(self, _i=None):
        theme.save_choice(self.theme_combo.currentData())
        inform(self, "The new theme is used the next time the app starts.")

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
                    inform(self, f"You have the latest version ({APP_VERSION}).")
                return
            if not manual:
                self.notify(f"Update available: {rel['tag']}. Open Settings → Check for updates.", ms=10000)
                return
            notes = rel["notes"][:1500] or "(no release notes)"
            if not updater.can_self_update():
                inform(self, f"Version {rel['tag']} is available:\n{rel['url']}\n\n{notes}")
                return
            if not confirm(self, f"Version {rel['tag']} is available (you have {APP_VERSION}).", "Install now",
                           title="Update", details=f"{notes}\n\nThe app restarts by itself; your data and settings "
                                                   "are kept."):
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                path = updater.fetch_installer(rel, token)
            except Exception as e:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                fail(self, "download the update", e)
                return
            QApplication.restoreOverrideCursor()
            self.repo.log(f"Installing update {rel['tag']}")
            updater.run_installer(path)
            self.quit_app()

        def failed(e):
            if manual:
                fail(self, "check for updates", e)
        self.run_bg(lambda r: updater.check(APP_VERSION, updater.DEFAULT_REPO, token), done, failed)

    def daily_update_check(self):
        if self.pref("update_check") == "0":
            return
        last = float(self.pref("last_update_check") or 0)
        if core.now_ts() - last > 86400:
            self.set_pref("last_update_check", core.now_ts())
            self.check_updates(manual=False)

    def save_machine(self):
        name = self.machine_edit.text().strip()
        if name and name != self.repo.machine:
            self.repo.set("machine_name", name)
            self.setWindowTitle(f"{APP_TITLE} — {name}")
            self.sync_dirty = True

    def toggle_autostart(self, on: bool):
        try:
            core.set_autostart(on)
        except OSError as e:
            fail(self, "change the startup setting", e)
