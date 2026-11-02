"""Sharing: the shared location (folder or Google Drive), the Hub and other sims."""
from __future__ import annotations


import html
import json
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLineEdit, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import (APP_NAME, fmt_ts, unified_diff)


from ..dialogs import DiffDialog, pick_folder
from ..common import item, selected_data, setup_table

class SharingPage:
    """Mixin for MainWindow — Sharing page."""

    def build_sync_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Where to share and back up (all sims use the same place)")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.sync_mode = QComboBox()
        self.sync_mode.addItem("Off", "")
        self.sync_mode.addItem("Shared folder (Google Drive for Desktop, network share, …)", "folder")
        self.sync_mode.addItem("Google Drive — direct (no Drive app needed)", "gdrive")
        row.addWidget(QLabel("Use:"))
        row.addWidget(self.sync_mode, 1)
        bl.addLayout(row)

        self.folder_box = QWidget()
        fl = QHBoxLayout(self.folder_box)
        fl.setContentsMargins(0, 0, 0, 0)
        self.sync_edit = QLineEdit(self.repo.get("sync_folder"))
        self.sync_edit.setPlaceholderText(r"e.g. G:\My Drive\SimConfigManager  or  \\SERVER\SimConfig")
        fl.addWidget(QLabel("Folder:"))
        fl.addWidget(self.sync_edit, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_sync)
        fl.addWidget(b)
        bl.addWidget(self.folder_box)

        self.drive_box = QWidget()
        dl = self.drive_form = QFormLayout(self.drive_box)
        dl.setContentsMargins(0, 0, 0, 0)
        self.drive_auth = QComboBox()
        self.drive_auth.addItem("Service account on a Shared Drive (recommended — no login, never expires)",
                                "service_account")
        self.drive_auth.addItem("Google account (sign in once in the browser)", "user")
        dl.addRow("Sign in with:", self.drive_auth)
        self.sa_row = QWidget()
        sl = QHBoxLayout(self.sa_row)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sa_label = QLabel()
        self.sa_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self.sa_label, 1)
        b = QPushButton("Choose key file (.json)…")
        b.clicked.connect(self.choose_sa_key)
        sl.addWidget(b)
        dl.addRow("Service account:", self.sa_row)
        self.user_row = QWidget()
        ul = QHBoxLayout(self.user_row)
        ul.setContentsMargins(0, 0, 0, 0)
        self.user_label = QLabel()
        ul.addWidget(self.user_label, 1)
        b = QPushButton("Choose OAuth client file…")
        b.clicked.connect(self.choose_oauth_client)
        ul.addWidget(b)
        b = QPushButton("Sign in…")
        b.clicked.connect(self.drive_sign_in)
        ul.addWidget(b)
        dl.addRow("Google account:", self.user_row)
        self.drive_folder = QLineEdit(self.repo.get("gdrive_folder_id"))
        self.drive_folder.setPlaceholderText("Paste the folder's link from drive.google.com (or its ID)")
        dl.addRow("Folder:", self.drive_folder)
        bl.addWidget(self.drive_box)

        row = QHBoxLayout()
        b = QPushButton("Save && test connection")
        b.clicked.connect(self.save_sync)
        row.addWidget(b)
        self.auto_push_cb = QCheckBox("Upload automatically (every minute when something changed)")
        self.auto_push_cb.setChecked(self.repo.get("auto_push") == "1")
        self.auto_push_cb.toggled.connect(lambda v: self.repo.set("auto_push", "1" if v else "0"))
        row.addWidget(self.auto_push_cb)
        row.addStretch()
        b = QPushButton("Upload now")
        b.clicked.connect(self.push_now)
        row.addWidget(b)
        bl.addLayout(row)
        self.sync_status = QLabel()
        self.sync_status.setWordWrap(True)
        self.sync_status.setTextFormat(Qt.RichText)
        bl.addWidget(self.sync_status)
        lay.addWidget(box)
        mode = self.repo.get("sync_mode") or ("folder" if self.repo.get("sync_folder") else "")
        self.sync_mode.setCurrentIndex(max(0, self.sync_mode.findData(mode)))
        self.drive_auth.setCurrentIndex(max(0, self.drive_auth.findData(self.repo.get("gdrive_auth")
                                                                         or "service_account")))
        self.sync_mode.currentIndexChanged.connect(lambda _i: self.update_sync_widgets())
        self.drive_auth.currentIndexChanged.connect(lambda _i: self.update_sync_widgets())
        self.update_sync_widgets()

        hub = QGroupBox("Central dashboard (optional) — report this sim's status to a SimConfigManager Hub web page")
        hl = QFormLayout(hub)
        self.hub_enable = QCheckBox("Report to a central dashboard")
        self.hub_enable.setChecked(self.repo.get("hub_enabled") == "1")
        hl.addRow("", self.hub_enable)
        self.hub_url = QLineEdit(self.repo.get("hub_url"))
        self.hub_url.setPlaceholderText("Address of your hub, e.g. https://hub.example.com or http://192.168.10.5:8780")
        hl.addRow("Hub address", self.hub_url)
        self.hub_key = QLineEdit(self.repo.get("hub_key"))
        self.hub_key.setEchoMode(QLineEdit.Password)
        self.hub_key.setPlaceholderText("Key for this sim (hub → Sims & settings → Add sim)")
        hl.addRow("Hub key", self.hub_key)
        row = QHBoxLayout()
        b = QPushButton("Save && send now")
        b.clicked.connect(self.save_hub)
        row.addWidget(b)
        self.hub_status = QLabel()
        self.hub_status.setTextFormat(Qt.RichText)
        self.hub_status.setWordWrap(True)
        row.addWidget(self.hub_status, 1)
        holder = QWidget()
        holder.setLayout(row)
        hl.addRow("", holder)
        hint = QLabel("Sends a status summary (versions, which files differ, rulebook, alerts, system info — never file "
                      "contents) every 5 minutes and right after changes. The hub is a separate small web server you "
                      "host yourself; see the README. Leave this off if you don't use one.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555")
        hl.addRow("", hint)
        lay.addWidget(hub)
        self.update_hub_status()

        box2 = QGroupBox("Other sims (from the shared location)")
        b2 = QVBoxLayout(box2)
        row = QHBoxLayout()
        self.machine_combo = QComboBox()
        self.machine_combo.currentIndexChanged.connect(lambda _i: self.refresh_remote_files())
        row.addWidget(QLabel("PC:"))
        row.addWidget(self.machine_combo, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_sync)
        row.addWidget(b)
        b2.addLayout(row)
        self.remote_table = QTableWidget()
        setup_table(self.remote_table, ["Name", "Group", "Path on that PC"], 2)
        b2.addWidget(self.remote_table)
        row = QHBoxLayout()
        for text, fn in (("Compare with a file on this PC…", self.compare_remote),
                         ("Copy to this PC…", self.copy_remote),
                         ("Rebuild this PC's history from this backup…", self.import_remote)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        b2.addLayout(row)
        lay.addWidget(box2, 1)
        return w

    def sync(self):
        """The configured sync location (folder or Google Drive), or None."""
        try:
            s = core.make_sync(self.repo)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Sync not available: {e}")
            return None
        return s

    def update_sync_widgets(self):
        mode = self.sync_mode.currentData()
        self.folder_box.setVisible(mode == "folder")
        self.drive_box.setVisible(mode == "gdrive")
        auth = self.drive_auth.currentData()
        try:
            st = self.repo.drive_file_status()
        except Exception:  # noqa: BLE001
            st = {"service_account": "", "client_secrets": False, "token": False}
        self.drive_form.setRowVisible(self.sa_row, auth == "service_account")
        self.drive_form.setRowVisible(self.user_row, auth == "user")
        self.drive_folder.setEnabled(auth == "service_account")
        if st["service_account"]:
            email = st["service_account"]
            self.sa_label.setText(f"<b>{html.escape(email)}</b> — add this address as <i>Content manager</i> of "
                                  "the Shared Drive.")
        else:
            self.sa_label.setText("No key file yet. Create a service account in Google Cloud and download a JSON "
                                  "key (see README).")
        if st["token"]:
            self.user_label.setText("<span style='color:#2e7d32'>Signed in.</span> The app keeps its files in a "
                                    "'SimConfigManager' folder in that account's Drive.")
        elif st["client_secrets"]:
            self.user_label.setText("OAuth client set. Click Sign in.")
        else:
            self.user_label.setText("Needs an OAuth client file (Desktop app) from Google Cloud, then sign in.")

    def choose_sa_key(self):
        p, _ = QFileDialog.getOpenFileName(self, "Service account key", "", "JSON key (*.json)")
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            assert data.get("type") == "service_account" and data.get("client_email")
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, "That is not a service account key file.")
            return
        self.repo.save_drive_file("service_account", json.dumps(data))
        self.update_sync_widgets()
        QMessageBox.information(self, APP_NAME, f"Key saved. In Google Drive, open the Shared Drive → Manage members "
                                f"→ add\n\n{data['client_email']}\n\nas Content manager. Then paste the link of "
                                "the folder to use and click Save & test.")

    def choose_oauth_client(self):
        p, _ = QFileDialog.getOpenFileName(self, "OAuth client (Desktop app)", "", "JSON (*.json)")
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            assert "installed" in data
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, "That is not an OAuth client file of type 'Desktop app'.")
            return
        self.repo.save_drive_file("client_secrets", json.dumps(data))
        self.update_sync_widgets()

    def drive_sign_in(self):
        from ... import storage
        if self.remote:
            QMessageBox.information(self, APP_NAME, "Signing in to Google opens a browser on the sim PC itself, so "
                                    "do this step at the sim (or use the service-account option, which needs no "
                                    "sign-in).")
            return
        files = core.drive_files(self.repo)
        if not files["client_secrets"].exists():
            QMessageBox.information(self, APP_NAME, "First choose the OAuth client file.")
            return
        QMessageBox.information(self, APP_NAME, "Your browser opens now. Sign in with the Google account for the "
                                "sims and allow access. Come back here afterwards.")
        try:
            storage.user_sign_in(str(files["client_secrets"]), str(files["token"]))
            prov = storage.user_token_provider(str(files["token"]))
            folder_id = storage.ensure_user_root(prov)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Sign-in failed: {e}")
            return
        self.repo.set("gdrive_folder_id", folder_id)
        self.drive_folder.setText(folder_id)
        self.update_sync_widgets()
        self.save_sync()

    def browse_sync(self):
        d = pick_folder(self, self.repo, "Sync folder", self.sync_edit.text())
        if d:
            self.sync_edit.setText(d if self.remote else os.path.normpath(d))

    def save_sync(self):
        mode = self.sync_mode.currentData()
        self.repo.set("sync_mode", mode or "off")
        self.repo.set("sync_folder", self.sync_edit.text().strip() if mode == "folder" else
                      (self.repo.get("sync_folder") if mode != "gdrive" else self.repo.get("sync_folder")))
        if mode == "gdrive":
            self.repo.set("gdrive_auth", self.drive_auth.currentData())
            if self.drive_auth.currentData() == "service_account":
                self.repo.set("gdrive_folder_id", core.parse_drive_folder(self.drive_folder.text()))
        if not mode:
            self.sync_status.setText("Sharing is off.")
            return
        s = self.sync()
        if s is None:
            self.sync_status.setText("<span style='color:#c62828'>Not complete yet — see the fields above.</span>")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if hasattr(s.backend, "check"):
                name = s.backend.check()
                self.sync_status.setText(f"<span style='color:#2e7d32'>✔ Connected to Google Drive folder "
                                         f"<b>{html.escape(name)}</b>.</span> Uploading…")
            elif not s.ok:
                raise FileNotFoundError(f"Folder not reachable: {s.where}")
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            self.sync_status.setText(f"<span style='color:#c62828'>✖ {html.escape(str(e))}</span>")
            return
        QApplication.restoreOverrideCursor()
        self.sync_dirty = True
        self.push_now()

    def update_hub_status(self):
        if not hasattr(self, "hub_status"):
            return
        if self.repo.get("hub_enabled") != "1":
            self.hub_status.setText("<span style='color:#777'>Off.</span>")
            return
        ok = float(self.repo.get("hub_last_ok") or 0)
        err = self.repo.get("hub_last_error")
        if err:
            self.hub_status.setText(f"<span style='color:#c62828'>✖ {html.escape(err)}</span>")
        elif ok:
            self.hub_status.setText(f"<span style='color:#2e7d32'>✔ Last report {fmt_ts(ok)}</span>")
        else:
            self.hub_status.setText("Waiting for the first report…")

    def save_hub(self):
        self.repo.set("hub_enabled", "1" if self.hub_enable.isChecked() else "0")
        self.repo.set("hub_url", self.hub_url.text().strip())
        self.repo.set("hub_key", self.hub_key.text().strip())
        self.repo.set("hub_last_error", "")
        if not self.hub_enable.isChecked():
            self.update_hub_status()
            return
        if self.remote:
            self.hub_status.setText("Saved. The sim itself sends the reports.")
            return
        self.hub_tick(force=True)

    def hub_tick(self, force: bool = False):
        if self.remote or self.repo.get("hub_enabled") != "1" or getattr(self, "_hub_busy", False):
            return
        try:
            sig = self.repo.hub_signature()
        except Exception:  # noqa: BLE001
            return
        last = float(self.repo.get("hub_last_try") or 0)
        due = force or sig != getattr(self, "_hub_sig", None) or core.now_ts() - last >= 300
        if not due:
            return
        self._hub_busy = True
        self.repo.set("hub_last_try", core.now_ts())
        url, key = self.repo.get("hub_url"), self.repo.get("hub_key")
        cache = getattr(self.repo, "_probe_cache", None)

        def work(r):
            if cache is not None:
                r._probe_cache = cache          # reuse the app's system-info readings
                r.background_probes = True
            return core.hub_push(r, url, key)

        def ok(res):
            self._hub_busy = False
            self._hub_sig = sig
            self.repo.set("hub_last_ok", core.now_ts())
            self.repo.set("hub_last_error", "")
            self.update_hub_status()
            if force:
                self.statusBar().showMessage(f"Reported to the hub as '{res.get('sim', '?')}'", 6000)

        def fail(e):
            self._hub_busy = False
            self.repo.set("hub_last_error", str(e))
            self.update_hub_status()
        self.run_bg(work, ok, fail)

    def auto_push(self):
        if self.remote:
            return  # the sim uploads by itself
        if not (self.repo.get("sync_mode") not in ("", "off") and (self.repo.get("sync_folder") or
                                                                   self.repo.get("sync_mode") == "gdrive")):
            return
        now = core.now_ts()
        if self.repo.get("auto_push") == "1" and self.sync_dirty and not getattr(self, "_pushing", False):
            self._pushing = True
            self.sync_dirty = False

            def ok(res):
                self._pushing = False
                self.repo.set("last_push", core.now_ts())
                self._last_status = core.now_ts()

            def fail(e):
                self._pushing = False
                self.sync_dirty = True
                self.statusBar().showMessage(f"Upload failed: {e}")
            self.run_bg(lambda r: core.make_sync(r).push(), ok, fail)
        elif now - getattr(self, "_last_status", 0) > 300 and not getattr(self, "_pushing", False):
            self._last_status = now   # fleet heartbeat every 5 minutes
            self.run_bg(lambda r: core.make_sync(r).push_status(), None, lambda e: None)

    def push_now(self):
        if self.sync() is None:
            QMessageBox.warning(self, APP_NAME, "No sharing location is set up yet.")
            return
        self.sync_status.setText(self.sync_status.text().replace(" Uploading…", "") + " Uploading…")

        def ok(res):
            self.sync_dirty = False
            self.repo.set("last_push", core.now_ts())
            self.statusBar().showMessage(f"Uploaded: {res['objects_copied']} new file version(s)")
            self.refresh_sync()

        def fail(e):
            QMessageBox.warning(self, APP_NAME, f"Upload failed: {e}")
            self.refresh_sync()
        self.run_bg(lambda r: core.make_sync(r).push(), ok, fail)

    def refresh_sync(self):
        last = self.repo.get("last_push")
        s = self.sync()
        mode = self.repo.get("sync_mode") or ("folder" if self.repo.get("sync_folder") else "")
        if mode in ("", "off"):
            self.sync_status.setText("Sharing is off.")
            s = None
        elif not s:
            self.sync_status.setText("<span style='color:#c62828'>Not complete or not reachable right now.</span>")
        else:
            self.sync_status.setText(f"Using <b>{html.escape(s.where)}</b> · last upload: "
                                     f"{fmt_ts(float(last)) if last else 'never'}"
                                     + ("  ·  changes waiting" if self.sync_dirty else ""))
        cur = self.machine_combo.currentData()
        self.machine_combo.blockSignals(True)
        self.machine_combo.clear()
        if s:
            for m in s.machines():
                me = " (this PC)" if m["machine"] == self.repo.machine else ""
                self.machine_combo.addItem(f"{m['machine']}{me} — {m['files']} files, "
                                           f"updated {fmt_ts(m['updated'])}", m["machine"])
        i = self.machine_combo.findData(cur)
        self.machine_combo.setCurrentIndex(max(i, 0))
        self.machine_combo.blockSignals(False)
        self.refresh_remote_files()

    def refresh_remote_files(self):
        self.remote_table.setRowCount(0)
        s = self.sync()
        m = self.machine_combo.currentData()
        if not s or not m:
            return
        try:
            files = [f for f in s.remote_latest(m) if f.get("active", True)]
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Could not read {m}: {e}")
            return
        self.remote_table.setRowCount(len(files))
        for r, f in enumerate(files):
            self.remote_table.setItem(r, 0, item(f.get("label") or os.path.basename(f["path"]), f))
            self.remote_table.setItem(r, 1, item(f.get("group", "")))
            self.remote_table.setItem(r, 2, item(f["path"] + ("   (missing)" if f["hash"] is None else "")))

    def selected_remote(self):
        d = selected_data(self.remote_table)
        return d[0] if d else None

    def pick_local_target(self, rf) -> Optional[str]:
        files = self.repo.files()
        names = [f"{f.display}  [{f.group}]  {f.path}" for f in files] + ["Other location…"]
        # pre-select a file with the same name
        default = next((i for i, f in enumerate(files) if f.display == rf.get("label")), 0)
        choice, ok = QInputDialog.getItem(self, "Target on this PC", "Which file on THIS PC?", names, default, False)
        if not ok:
            return None
        if choice == "Other location…":
            if self.remote:
                folder = pick_folder(self, self.repo, "Folder on the sim to save to")
                return (folder.rstrip("\\/") + "\\" + rf["path"].replace("/", "\\").split("\\")[-1]) \
                    if folder else None
            p, _ = QFileDialog.getSaveFileName(self, "Save to", os.path.basename(rf["path"]))
            return p or None
        return files[names.index(choice)].path

    def compare_remote(self):
        rf, s = self.selected_remote(), self.sync()
        if not rf or not s or rf["hash"] is None:
            return
        target = self.pick_local_target(rf)
        if not target:
            return
        try:
            remote = s.fetch(rf["hash"])
        except FileNotFoundError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        lf = self.repo.find_file_by_path(target)
        mine = self.repo.read_current(lf) if lf else self.repo.read_path(target)
        DiffDialog(f"{self.machine_combo.currentData()} → this PC",
                   unified_diff(remote, mine, f"{self.machine_combo.currentData()}", "this PC"), self,
                   summary=core.setting_changes(remote, mine, target)).exec()

    def copy_remote(self):
        rf, s = self.selected_remote(), self.sync()
        if not rf or not s or rf["hash"] is None:
            return
        target = self.pick_local_target(rf)
        if not target:
            return
        if QMessageBox.question(self, APP_NAME, f"Overwrite\n{target}\nwith the version from "
                                f"{self.machine_combo.currentData()}?\n(A safety snapshot is taken first.)") \
                != QMessageBox.Yes:
            return
        try:
            data = s.fetch(rf["hash"])
            f = self.repo.find_file_by_path(target)
            if f and f.active:
                self.repo.write_file(f, data, f"copied from {self.machine_combo.currentData()}")
            else:
                self.repo.write_path(target, data)
            self.repo.log(f"Copied {rf.get('label')} from {self.machine_combo.currentData()} to {target}")
            self.sync_dirty = True
            self.statusBar().showMessage("Copied.")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Copy failed: {e}")

    def import_remote(self):
        s, m = self.sync(), self.machine_combo.currentData()
        if not s or not m:
            return
        if self.repo.snapshots(1):
            QMessageBox.information(self, APP_NAME,
                                    "This only works on a fresh install (no history yet). It is meant for a "
                                    "rebuilt or replacement PC: install the app, set the sync folder, then use "
                                    "this button to get back all files, history, restore points and rules.")
            return
        if QMessageBox.question(self, APP_NAME, f"Import all history, restore points and rules from '{m}'?") \
                == QMessageBox.Yes:
            try:
                res = s.import_machine(m)
                QMessageBox.information(self, APP_NAME, f"Imported {res['files']} files and {res['snapshots']} "
                                        "snapshots. Use Quick restore to put the configs back on disk.")
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Import failed: {e}")
            self.refresh_all()
