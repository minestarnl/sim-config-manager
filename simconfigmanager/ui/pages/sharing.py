"""Sharing: the shared location (folder or Google Drive), the Hub and other sims."""
from __future__ import annotations


import html
import json
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QInputDialog,
    QLineEdit, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import (fmt_ts, unified_diff)


from ..dialogs import DiffDialog, pick_folder
from ..common import (BAD, MUTED, OK, button, button_row, colored, confirm, fail, hint, inform, item,
                      running_warning, section, selected_data, setup_table, warn)

from . import Page


class SharingPage:
    """Mixin for MainWindow — Sharing page."""

    PAGE = Page("sharing", "Sharing", "cloud", "Shared location, Hub and the other sims", "build_sharing_page", "refresh_sharing")

    def build_sharing_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box, bl = section("Shared location — history backup and fleet, the same place for every sim")
        row = QHBoxLayout()
        self.sync_mode = QComboBox()
        self.sync_mode.addItem("Off", "")
        self.sync_mode.addItem("Shared folder (Google Drive for Desktop, network share, …)", "folder")
        self.sync_mode.addItem("Google Drive, direct (no Drive app needed)", "gdrive")
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
        fl.addWidget(button("Browse…", self.browse_sync))
        bl.addWidget(self.folder_box)

        self.drive_box = QWidget()
        dl = self.drive_form = QFormLayout(self.drive_box)
        dl.setContentsMargins(0, 0, 0, 0)
        self.drive_auth = QComboBox()
        self.drive_auth.addItem("Service account on a Shared Drive (recommended: no sign-in, never expires)",
                                "service_account")
        self.drive_auth.addItem("Google account (sign in once in the browser)", "user")
        dl.addRow("Sign in with:", self.drive_auth)
        self.sa_row = QWidget()
        sl = QHBoxLayout(self.sa_row)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sa_label = QLabel()
        self.sa_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self.sa_label, 1)
        sl.addWidget(button("Choose key file (.json)…", self.choose_sa_key))
        dl.addRow("Service account:", self.sa_row)
        self.user_row = QWidget()
        ul = QHBoxLayout(self.user_row)
        ul.setContentsMargins(0, 0, 0, 0)
        self.user_label = QLabel()
        ul.addWidget(self.user_label, 1)
        ul.addWidget(button("Choose OAuth client file…", self.choose_oauth_client))
        ul.addWidget(button("Sign in…", self.drive_sign_in))
        dl.addRow("Google account:", self.user_row)
        self.drive_folder = QLineEdit(self.repo.get("gdrive_folder_id"))
        self.drive_folder.setPlaceholderText("Paste the folder's link from drive.google.com (or its ID)")
        dl.addRow("Folder:", self.drive_folder)
        bl.addWidget(self.drive_box)

        self.auto_push_cb = QCheckBox("Upload automatically (every minute when something changed)")
        self.auto_push_cb.setChecked(self.repo.get("auto_push") == "1")
        self.auto_push_cb.toggled.connect(lambda v: self.repo.set("auto_push", "1" if v else "0"))
        bl.addLayout(button_row(("Save and test", self.save_sync, {"primary": True}), self.auto_push_cb,
                                trailing=[("Upload now", self.push_now, {"icon": "upload"})]))
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

        hub, hl = section("Hub — optional central web dashboard", QFormLayout)
        self.hub_enable = QCheckBox("Report this sim's status to a Hub")
        self.hub_enable.setChecked(self.repo.get("hub_enabled") == "1")
        hl.addRow("", self.hub_enable)
        self.hub_url = QLineEdit(self.repo.get("hub_url"))
        self.hub_url.setPlaceholderText("e.g. https://hub.example.com or http://192.168.10.5:8780")
        hl.addRow("Hub address:", self.hub_url)
        self.hub_key = QLineEdit(self.repo.get("hub_key"))
        self.hub_key.setEchoMode(QLineEdit.Password)
        self.hub_key.setPlaceholderText("Key for this sim (in the Hub: Sims & settings → Add sim)")
        hl.addRow("Sim key:", self.hub_key)
        self.hub_status = QLabel()
        self.hub_status.setTextFormat(Qt.RichText)
        self.hub_status.setWordWrap(True)
        hl.addRow("", _row_widget(button_row(("Save and send now", self.save_hub), self.hub_status)))
        hl.addRow("", hint("Sends a status summary (versions, files that differ, rulebook, alerts, system "
                           "information — never file contents) every 5 minutes and right after changes. The Hub is a "
                           "small web server you host yourself (see the README). Leave this off if you don't use one."))
        lay.addWidget(hub)
        self.update_hub_status()

        box2, b2 = section("Other sims in the shared location")
        self.machine_combo = QComboBox()
        self.machine_combo.currentIndexChanged.connect(lambda _i: self.refresh_remote_files())
        b2.addLayout(button_row(QLabel("Sim:"), self.machine_combo, stretch=False,
                                trailing=[("Refresh", self.refresh_sharing, {"icon": "refresh"})]))
        self.remote_table = QTableWidget()
        setup_table(self.remote_table, ["Name", "Group", "Path on that sim"], 2)
        b2.addWidget(self.remote_table)
        b2.addLayout(button_row(("Compare with a file here…", self.compare_remote),
                                ("Copy to this sim…", self.copy_remote),
                                trailing=[("Rebuild this sim's history from it…", self.import_remote)]))
        lay.addWidget(box2, 1)
        return w

    def sync(self):
        """The configured sync location (folder or Google Drive), or None."""
        try:
            s = core.make_sync(self.repo)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Shared location not available: {e}")
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
            self.user_label.setText(colored("Signed in.", OK) + " The app keeps its files in a "
                                    "'SimConfigManager' folder in that account's Drive.")
        elif st["client_secrets"]:
            self.user_label.setText("OAuth client file chosen. Now click Sign in.")
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
            warn(self, "That file is not a service account key (.json from Google Cloud → IAM → Service accounts → "
                       "Keys).")
            return
        self.repo.save_drive_file("service_account", json.dumps(data))
        self.update_sync_widgets()
        inform(self, f"Key saved. In Google Drive, open the Shared Drive → Manage members → add\n\n"
                     f"{data['client_email']}\n\nas Content manager. Then paste the link of the folder to use and "
                     "click Save and test.")

    def choose_oauth_client(self):
        p, _ = QFileDialog.getOpenFileName(self, "OAuth client (Desktop app)", "", "JSON (*.json)")
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            assert "installed" in data
        except Exception:  # noqa: BLE001
            warn(self, "That file is not an OAuth client of type 'Desktop app'.")
            return
        self.repo.save_drive_file("client_secrets", json.dumps(data))
        self.update_sync_widgets()

    def drive_sign_in(self):
        from ... import storage
        if self.remote:
            inform(self, "Signing in to Google opens a browser on the sim PC itself, so do this step at the sim "
                         "(or use the service account option, which needs no sign-in).")
            return
        files = core.drive_files(self.repo)
        if not files["client_secrets"].exists():
            inform(self, "First choose the OAuth client file.")
            return
        inform(self, "Your browser opens now. Sign in with the Google account for the sims and allow access, "
                     "then come back here.")
        try:
            storage.user_sign_in(str(files["client_secrets"]), str(files["token"]))
            prov = storage.user_token_provider(str(files["token"]))
            folder_id = storage.ensure_user_root(prov)
        except Exception as e:  # noqa: BLE001
            fail(self, "sign in to Google", e)
            return
        self.repo.set("gdrive_folder_id", folder_id)
        self.drive_folder.setText(folder_id)
        self.update_sync_widgets()
        self.save_sync()

    def browse_sync(self):
        d = pick_folder(self, self.repo, "Shared folder", self.sync_edit.text())
        if d:
            self.sync_edit.setText(d if self.remote else os.path.normpath(d))

    def save_sync(self):
        mode = self.sync_mode.currentData()
        self.repo.set("sync_mode", mode or "off")
        if mode == "folder":
            self.repo.set("sync_folder", self.sync_edit.text().strip())
        if mode == "gdrive":
            self.repo.set("gdrive_auth", self.drive_auth.currentData())
            if self.drive_auth.currentData() == "service_account":
                self.repo.set("gdrive_folder_id", core.parse_drive_folder(self.drive_folder.text()))
        if not mode:
            self.sync_status.setText("Off — this sim keeps its history only on this PC.")
            return
        s = self.sync()
        if s is None:
            self.sync_status.setText(colored("Not complete yet — see the fields above.", BAD))
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if hasattr(s.backend, "check"):
                name = s.backend.check()
                self.sync_status.setText(colored(f"✔ Connected to the Google Drive folder '{name}'.", OK)
                                         + " Uploading…")
            elif not s.ok:
                raise FileNotFoundError(f"Folder not reachable: {s.where}")
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            self.sync_status.setText(colored(f"✖ {e}", BAD))
            return
        QApplication.restoreOverrideCursor()
        self.sync_dirty = True
        self.push_now()

    def update_hub_status(self):
        if not hasattr(self, "hub_status"):
            return
        if self.repo.get("hub_enabled") != "1":
            self.hub_status.setText(colored("Off.", MUTED))
            return
        ok = float(self.repo.get("hub_last_ok") or 0)
        err = self.repo.get("hub_last_error")
        if err:
            self.hub_status.setText(colored(f"✖ {err}", BAD))
        elif ok:
            self.hub_status.setText(colored(f"✔ Last report {fmt_ts(ok)}", OK))
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
            self.hub_status.setText("Saved. The sim PC sends the reports itself.")
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
            return core.hub_push(r, url, key)   # may apply commands from the Hub (needs a writable store)

        def ok(res):
            self._hub_busy = False
            self._hub_sig = sig
            self.repo.set("hub_last_ok", core.now_ts())
            self.repo.set("hub_last_error", "")
            self.update_hub_status()
            if force:
                self.notify(f"Reported to the Hub as '{res.get('sim', '?')}'")

        def fail(e):
            self._hub_busy = False
            self.repo.set("hub_last_error", str(e))
            self.update_hub_status()
        self.run_bg(work, ok, fail, write=True)

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
                self.statusBar().showMessage(f"Could not upload to the shared location: {e}")
            self.run_bg(lambda r: core.make_sync(r).push(), ok, fail)
        elif now - getattr(self, "_last_status", 0) > 300 and not getattr(self, "_pushing", False):
            self._last_status = now   # fleet heartbeat every 5 minutes
            self.run_bg(lambda r: core.make_sync(r).push_status(), None, lambda e: None)

    def push_now(self):
        if self.sync() is None:
            inform(self, "No shared location is set up yet (see the fields above).")
            return
        self.sync_status.setText(self.sync_status.text().replace(" Uploading…", "") + " Uploading…")

        def ok(res):
            self.sync_dirty = False
            self.repo.set("last_push", core.now_ts())
            self.notify(f"Uploaded to the shared location ({res['objects_copied']} new file revision(s))")
            self.refresh_sharing()

        def failed(e):
            fail(self, "upload to the shared location", e)
            self.refresh_sharing()
        self.run_bg(lambda r: core.make_sync(r).push(), ok, failed)

    def refresh_sharing(self):
        last = self.repo.get("last_push")
        s = self.sync()
        mode = self.repo.get("sync_mode") or ("folder" if self.repo.get("sync_folder") else "")
        if mode in ("", "off"):
            self.sync_status.setText("Off — this sim keeps its history only on this PC.")
            s = None
        elif not s:
            self.sync_status.setText(colored("Not complete or not reachable right now.", BAD))
        else:
            self.sync_status.setText(f"Using <b>{html.escape(s.where)}</b> · last upload: "
                                     f"{fmt_ts(float(last)) if last else 'never'}"
                                     + ("  ·  changes waiting" if self.sync_dirty else ""))
        if not s:
            self._fill_machines([])
            return

        def failed(e):
            self.statusBar().showMessage(f"Could not read the shared location: {e}")
        self.run_bg(lambda r: core.make_sync(r).machines(), self._fill_machines, failed)

    def _fill_machines(self, machines):
        cur = self.machine_combo.currentData()
        self.machine_combo.blockSignals(True)
        self.machine_combo.clear()
        for m in machines:
            me = " (this sim)" if m["machine"] == self.repo.machine else ""
            self.machine_combo.addItem(f"{m['machine']}{me} — {m['files']} files, updated {fmt_ts(m['updated'])}",
                                       m["machine"])
        i = self.machine_combo.findData(cur)
        self.machine_combo.setCurrentIndex(max(i, 0))
        self.machine_combo.blockSignals(False)
        self.refresh_remote_files()

    def refresh_remote_files(self):
        self.remote_table.setRowCount(0)
        m = self.machine_combo.currentData()
        if not m or self.sync() is None:
            return

        def fill(files):
            if self.machine_combo.currentData() != m:
                return
            self.remote_table.setRowCount(len(files))
            for r, f in enumerate(files):
                self.remote_table.setItem(r, 0, item(f.get("label") or os.path.basename(f["path"]), f))
                self.remote_table.setItem(r, 1, item(f.get("group", "")))
                self.remote_table.setItem(r, 2, item(f["path"] + ("   (missing)" if f["hash"] is None else "")))

        def failed(e):
            self.statusBar().showMessage(f"Could not read {m}: {e}")
        self.run_bg(lambda r: [f for f in core.make_sync(r).remote_latest(m) if f.get("active", True)], fill, failed)

    def selected_remote(self):
        d = selected_data(self.remote_table)
        return d[0] if d else None

    def pick_local_target(self, rf) -> Optional[str]:
        files = self.repo.files()
        names = [f"{f.display}  [{f.group}]  {f.path}" for f in files] + ["Other location…"]
        # pre-select a file with the same name
        default = next((i for i, f in enumerate(files) if f.display == rf.get("label")), 0)
        choice, ok = QInputDialog.getItem(self, "File on this sim", "Which file on this sim?", names, default, False)
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
            fail(self, "download the file from the shared location", e)
            return
        lf = self.repo.find_file_by_path(target)
        mine = self.repo.read_current(lf) if lf else self.repo.read_path(target)
        DiffDialog(f"{self.machine_combo.currentData()} → this sim",
                   unified_diff(remote, mine, f"{self.machine_combo.currentData()}", "this sim"), self,
                   summary=core.setting_changes(remote, mine, target)).exec()

    def copy_remote(self):
        rf, s = self.selected_remote(), self.sync()
        if not rf or not s or rf["hash"] is None:
            return
        target = self.pick_local_target(rf)
        if not target:
            return
        src = self.machine_combo.currentData()
        if not confirm(self, f"Overwrite this file with the one from {src}?", "Overwrite",
                       details=f"{target}\n\nA safety snapshot is taken first." + running_warning(self.repo)):
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
            self.notify(f"Copied {rf.get('label')} from {src}")
            self.refresh_all()
        except Exception as e:  # noqa: BLE001
            fail(self, "copy the file", e)

    def import_remote(self):
        s, m = self.sync(), self.machine_combo.currentData()
        if not s or not m:
            return
        if self.repo.snapshots(1):
            inform(self, "This only works on a fresh install (no history yet). It is meant for a rebuilt or "
                         "replacement PC: install the app, set up the shared location, then use this to get back "
                         "all files, history, versions, restore points and rules.")
            return
        if confirm(self, f"Rebuild this sim's history from '{m}'?", "Rebuild",
                   details="Imports all files, history, versions, restore points and rules. Nothing is written "
                           "to the config files yet."):
            try:
                res = s.import_machine(m)
                inform(self, f"Imported {res['files']} files and {res['snapshots']} snapshots. Use Restore points "
                             "or Versions to put the files back on disk.")
            except Exception as e:  # noqa: BLE001
                fail(self, "rebuild the history", e)
            self.refresh_all()


def _row_widget(layout) -> QWidget:
    w = QWidget()
    layout.setContentsMargins(0, 0, 0, 0)
    w.setLayout(layout)
    return w
