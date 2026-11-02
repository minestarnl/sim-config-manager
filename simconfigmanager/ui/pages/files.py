"""Files: tracked files, their revisions, the editor and system information."""
from __future__ import annotations


import html
import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
    QInputDialog, QLineEdit, QMenu, QMessageBox, QSplitter, QTableWidget, QVBoxLayout,
    QWidget,
)

from ... import core
from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import (fmt_ts, unified_diff)


from ..dialogs import DiffDialog, DiscoverDialog, pick_files, pick_folder
from ..common import (button, button_row, confirm, fail, file_state_item, hint, inform, item, open_in_explorer,
                      running_warning, selected_data, setup_table)

from . import Page


class FilesPage:
    """Mixin for MainWindow — Files page."""

    PAGE = Page("files", "Files", "files", "Tracked files, their revisions and the editor", "build_files_page", "refresh_files")

    def build_files_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        add = button("Add", primary=True, icon="add")
        menu = QMenu(add)
        menu.addAction("Files…", self.add_files)
        menu.addAction("Folder…", self.add_folder)
        menu.addAction("Find Prepar3D / ProSim config files…", self.discover)
        menu.addAction("System information (GPU, drivers, …)…", self.add_system_info)
        add.setMenu(menu)
        self.group_filter = QComboBox()
        self.group_filter.currentIndexChanged.connect(lambda _i: self.refresh_files())
        bar = button_row(add, ("Open in editor", self.edit_contents), ("Rename…", self.edit_file),
                         ("Stop tracking…", self.remove_files),
                         trailing=[QLabel("Group:"), self.group_filter, ("Record snapshot…", self.snapshot_now)])
        lay.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.files_table = QTableWidget()
        setup_table(self.files_table, ["Status", "Name", "Group", "Revisions", "Last change", "Path"], 5)
        self.files_table.itemSelectionChanged.connect(self.refresh_file_history)
        self.files_table.doubleClicked.connect(lambda _i: self.edit_contents())
        self.files_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files_table.customContextMenuRequested.connect(self.files_context)
        split.addWidget(self.files_table)

        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.setContentsMargins(0, 0, 0, 0)
        self.hist_label = QLabel()
        ll.addWidget(self.hist_label)
        self.ver_table = QTableWidget()
        setup_table(self.ver_table, ["When", "Snapshot", "Lines +/-", "Changed by", "Note"], 4)
        self.ver_table.doubleClicked.connect(lambda _i: self.diff_version_prev())
        ll.addWidget(self.ver_table)
        ll.addLayout(button_row(("Open this revision", self.view_version),
                                ("Compare with previous", self.diff_version_prev),
                                ("Compare with file on disk", self.diff_version_current),
                                ("Restore this revision…", self.restore_version),
                                ("Save copy as…", self.save_version_as),
                                trailing=[("Show in folder", self.open_location)]))
        split.addWidget(lower)
        split.setSizes([420, 300])
        lay.addWidget(split)
        return w

    def refresh_files(self):
        cur_group = self.group_filter.currentText()
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem("(all)")
        self.group_filter.addItems(self.repo.groups())
        i = self.group_filter.findText(cur_group)
        self.group_filter.setCurrentIndex(max(i, 0))
        self.group_filter.blockSignals(False)
        g = self.group_filter.currentText()
        sel = set(selected_data(self.files_table))
        files = [f for f in self.repo.files() if g == "(all)" or f.group == g]
        self.files_table.setRowCount(len(files))
        for r, f in enumerate(files):
            st = self.repo.file_status(f)
            hist = self.repo.file_history(f.id)
            self.files_table.setItem(r, 0, file_state_item(st, f.id))
            self.files_table.setItem(r, 1, item(f.display))
            self.files_table.setItem(r, 2, item(f.group))
            self.files_table.setItem(r, 3, item(len(hist)))
            self.files_table.setItem(r, 4, item(fmt_ts(hist[0].created_at) if hist else "–"))
            self.files_table.setItem(r, 5, item(f.path))
            if f.id in sel:
                self.files_table.selectRow(r)

    def current_file(self):
        ids = selected_data(self.files_table)
        return self.repo.file(ids[0]) if ids else None

    def refresh_file_history(self):
        f = self.current_file()
        self.ver_table.setRowCount(0)
        if not f:
            self.hist_label.setText("<b>Revisions</b> — select a file above")
            return
        self.hist_label.setText(f"<b>Revisions of {html.escape(f.display)}</b> — {html.escape(f.path)}")
        hist = self.repo.file_history(f.id)
        actors = self.repo.change_actors(f.id)
        self.ver_table.setRowCount(len(hist))
        for r, v in enumerate(hist):
            prev = hist[r + 1].hash if r + 1 < len(hist) else None
            try:
                a, d = core.diff_stats(self.repo.get_object(prev), self.repo.get_object(v.hash))
                pm = f"+{a} / -{d}" if v.hash else "deleted"
            except FileNotFoundError:
                pm = "?"
            self.ver_table.setItem(r, 0, item(fmt_ts(v.created_at), (v.snapshot_id, v.hash, prev)))
            self.ver_table.setItem(r, 1, item(f"#{v.snapshot_id}"))
            self.ver_table.setItem(r, 2, item(pm))
            self.ver_table.setItem(r, 3, item(actors.get(v.hash or "", "")))
            self.ver_table.setItem(r, 4, item(v.message))

    def current_version(self):
        d = selected_data(self.ver_table)
        return d[0] if d else None

    def files_context(self, pos):
        m = QMenu(self)
        m.addAction("Open in editor", self.edit_contents)
        m.addAction("Compare with last snapshot", self.diff_file_with_last)
        m.addAction("Copy for an AI chat", self.copy_for_claude)
        m.addSeparator()
        m.addAction("Rename…", self.edit_file)
        m.addAction("Add alert rule…", lambda: self.add_rule(self.current_file()))
        m.addAction("Show in folder", self.open_location)
        m.addSeparator()
        m.addAction("Stop tracking…", self.remove_files)
        m.exec(self.files_table.viewport().mapToGlobal(pos))

    def add_files(self):
        paths = pick_files(self, self.repo, "Choose config files to track")
        if not paths:
            return
        groups = [""] + self.repo.groups()
        group, ok = QInputDialog.getItem(self, "Group", "Group (e.g. 'Sim 1 – P3D v5', 'ProSim'):", groups, 0, True)
        if not ok:
            return
        for p in paths:
            self.repo.add_file(p, group=group)
        self.repo.ensure_baseline()
        self.sync_dirty = True
        self.refresh_files()

    def add_folder(self):
        d = pick_folder(self, self.repo, "Choose folder")
        if not d:
            return
        pats, ok = QInputDialog.getText(self, "File types", "Which files? (separate with ;)",
                                        text="*.cfg;*.xml;*.ini;*.json")
        if not ok:
            return
        rec = QMessageBox.question(self, "Add folder", "Include sub-folders?") == QMessageBox.Yes
        group, ok = QInputDialog.getText(self, "Group", "Group name:", text=os.path.basename(d))
        if not ok:
            return
        added = self.repo.add_folder(d, pats, rec, group)
        if len(added) > 200 and not confirm(self, f"That folder has {len(added)} matching files. Track them all?",
                                            f"Track {len(added)} files"):
            for f in added:
                self.repo.remove_file(f.id)
            return
        self.repo.ensure_baseline()
        self.sync_dirty = True
        self.refresh_files()
        self.notify(f"Now tracking {len(added)} more file(s)")

    def discover(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            cands = self.repo.discover()
        finally:
            QApplication.restoreOverrideCursor()
        if not cands:
            inform(self, "No Prepar3D or ProSim config files were found in the usual places. "
                         "Use Add → Files… to pick them yourself.")
            return
        dlg = DiscoverDialog(cands, self)
        if dlg.exec() == QDialog.Accepted:
            for label, group, path in dlg.chosen():
                self.repo.add_file(path, label, group)
            self.repo.ensure_baseline()
            self.sync_dirty = True
            self.refresh_files()

    def file_diff(self, f, old, new, la, lb, title):
        summary = core.setting_changes(old, new, f.path)
        DiffDialog(title, unified_diff(old, new, la, lb), self, summary=summary,
                   ignore_cb=(lambda c, ff=f: self.ignore_setting(ff, c)) if summary else None).exec()

    def ignore_setting(self, f, change: dict):
        setting = change["setting"]
        if change.get("ftype") == "ini":
            kind, pattern = "ini_key", setting
        else:
            kind = "xml"
            path = setting.split("@")[0]
            parts = path.split("/")[1:]          # drop the root element
            pattern = ".//" + "/".join(parts) if parts else "."
        pattern, ok = QInputDialog.getText(
            self, "Ignore setting", f"Changes to this will no longer count for {f.display}.\n"
            f"Pattern ({core.IGNORE_KINDS[kind]}):", text=pattern)
        if not ok or not pattern.strip():
            return
        note, _ = QInputDialog.getText(self, "Ignore setting", "Why? (optional, e.g. 'P3D rewrites this on exit')")
        self.repo.add_ignore(f.id, kind, pattern.strip(), note)
        self.statusBar().showMessage(f"Ignoring {pattern} in {f.display}", 6000)
        self.refresh_all()

    def open_editor(self, f, version_hash=None, label=""):
        from ...editor import EditorWindow
        if not hasattr(self, "editors"):
            self.editors = []
        self.editors = [e for e in self.editors if e.isVisible()]
        if version_hash is None:
            for e in self.editors:  # already open → bring to front
                if e.file.id == f.id and not e.read_only:
                    e.showNormal()
                    e.raise_()
                    e.activateWindow()
                    return e
        try:
            ed = EditorWindow(self.repo, f, on_saved=self.after_edit, version_hash=version_hash,
                              version_label=label)
        except Exception as e:  # noqa: BLE001
            fail(self, f"open {f.display}", e)
            return None
        ed.setWindowIcon(self.windowIcon())
        ed.show()
        self.editors.append(ed)
        return ed

    def after_edit(self):
        self.sync_dirty = True
        if self.isVisible():
            self.refresh_all()

    def edit_contents(self):
        f = self.current_file()
        if f:
            self.open_editor(f)

    def view_version(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        if v[1] is None:
            inform(self, "The file did not exist at that moment (it was deleted or not created yet).")
            return
        self.open_editor(f, v[1], f"snapshot #{v[0]}")

    def add_system_info(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Track system information")
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)
        lay.addWidget(hint("These are read from the sim PC every few minutes and tracked like files: history, "
                           "versions, alert rules (e.g. 'GPU driver changed'), archives and the AI assistants can all "
                           "use them. They are never written back."))
        tracked = {f.path for f in self.repo.files()}
        boxes = {}
        for key, (label, _cmd) in core.BUILTIN_PROBES.items():
            cb = QCheckBox(label)
            path = core.PROBE_PREFIX + key
            cb.setChecked(path in tracked or key in ("gpu", "windows", "sim_software", "displays", "hardware"))
            cb.setEnabled(self.host.get("platform") == "win32" and path not in tracked)
            if path in tracked:
                cb.setText(label + "  (already tracked)")
            boxes[key] = cb
            lay.addWidget(cb)
        if self.host.get("platform") != "win32":
            lay.addWidget(QLabel("<i>The built-in items are only available on Windows.</i>"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Group:"))
        grp = QLineEdit(f"{self.repo.machine} – System")
        row.addWidget(grp)
        lay.addLayout(row)
        lay.addWidget(QLabel("<b>Own command</b> (optional) — e.g. <i>Get-Content C:\\ProSim737\\version.txt</i>"))
        cname = QLineEdit()
        cname.setPlaceholderText("Name, e.g. 'ProSim version'")
        ccmd = QLineEdit()
        ccmd.setPlaceholderText("PowerShell command whose output should be tracked")
        lay.addWidget(cname)
        lay.addWidget(ccmd)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for key, cb in boxes.items():
                if cb.isEnabled() and cb.isChecked():
                    core.add_builtin_probe(self.repo, key, grp.text().strip())
            if cname.text().strip() and ccmd.text().strip():
                core.add_custom_probe(self.repo, cname.text().strip(), ccmd.text().strip(), grp.text().strip())
            if self.remote:
                self.repo.refresh_probes()      # first reading, on the sim
            else:
                self.repo.background_probes = False   # first reading now
            self.repo.ensure_baseline()
        finally:
            if not self.remote:
                self.repo.background_probes = True
            QApplication.restoreOverrideCursor()
        self.sync_dirty = True
        self.refresh_files()

    def refresh_probes_bg(self):
        if self.remote or (hasattr(self, "flying") and self.flying()):
            return   # system information is read again after the session
        due = self.repo.probe_due()
        if not due or getattr(self, "_probing", False):
            return
        self._probing = True
        paths = [f.path for f in due]

        def work(r):
            out = {}
            for pth in paths:
                cmd = core.probe_command(r, pth)
                if cmd:
                    out[pth] = core.run_probe(cmd)
            return out

        def done(res):
            self._probing = False
            cache = getattr(self.repo, "_probe_cache", None)
            if cache is None:
                cache = self.repo._probe_cache = {}
            for pth, data in res.items():
                if data is not None:
                    cache[pth] = (core.now_ts(), data)
                elif pth in cache:
                    cache[pth] = (core.now_ts(), cache[pth][1])

        def fail(e):
            self._probing = False
        self.run_bg(work, done, fail)

    def edit_file(self):
        f = self.current_file()
        if not f:
            return
        label, ok = QInputDialog.getText(self, "Rename", "Name shown in the app:", text=f.label)
        if not ok:
            return
        group, ok = QInputDialog.getItem(self, "Rename", "Group:", [f.group] + self.repo.groups(), 0, True)
        if not ok:
            return
        self.repo.update_file(f.id, label=label, group=group)
        self.sync_dirty = True
        self.refresh_files()

    def remove_files(self):
        ids = selected_data(self.files_table)
        if not ids:
            return
        if confirm(self, f"Stop tracking {len(ids)} file(s)?", "Stop tracking",
                   details="The files themselves are not touched and their history is kept."):
            for i in ids:
                self.repo.remove_file(i)
            self.sync_dirty = True
            self.refresh_files()

    def snapshot_now(self):
        msg, ok = QInputDialog.getText(self, "Record snapshot", "What did you change? (optional note)")
        if not ok:
            return
        snap = self.repo.commit(msg, kind="manual")
        if snap is None:
            if not confirm(self, "Nothing changed since the last snapshot.", "Record anyway"):
                return
            snap = self.repo.commit(msg or "Manual snapshot", kind="manual", force=True)
        self.notify(f"Snapshot #{snap.id} recorded" if snap else "Snapshot recorded")
        self.sync_dirty = True
        self.refresh_all()

    def diff_file_with_last(self):
        f = self.current_file()
        if not f:
            return
        old = self.repo.get_object(self.repo.last_hash(f.id))
        new = self.repo.read_current(f)
        self.file_diff(f, old, new, "last snapshot", "on disk now", f"{f.display}: last snapshot → on disk now")

    def diff_version_prev(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        _sid, h, prev = v
        self.file_diff(f, self.repo.get_object(prev), self.repo.get_object(h), "previous revision",
                       f"snapshot #{v[0]}", f"{f.display}: changes in snapshot #{v[0]}")

    def diff_version_current(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        self.file_diff(f, self.repo.get_object(v[1]), self.repo.read_current(f), f"snapshot #{v[0]}",
                       "on disk now", f"{f.display}: snapshot #{v[0]} → on disk now")

    def restore_version(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        if v[1] is None:
            inform(self, "The file did not exist at that moment (it was deleted or not created yet).")
            return
        if not confirm(self, f"Restore {f.display} to how it was in snapshot #{v[0]}?", "Restore",
                       details=f"{f.path}\n\nA safety snapshot is taken first." + running_warning(self.repo)):
            return
        try:
            self.repo.restore_version(f.id, v[1], f"snapshot #{v[0]}")
        except Exception as e:  # noqa: BLE001
            fail(self, f"restore {f.display}", e, "Is the file open in the sim, or is the app not running as "
                 "administrator?")
            return
        self.sync_dirty = True
        self.notify(f"{f.display} restored to snapshot #{v[0]}")
        self.refresh_all()

    def save_version_as(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f or v[1] is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save copy", f"{f.display}.snapshot{v[0]}")
        if path:
            Path(path).write_bytes(self.repo.get_object(v[1]))

    def open_location(self):
        f = self.current_file()
        if f and self.remote:
            inform(self, f"This file is on the sim PC:\n{f.path}")
            return
        if f and not core.is_probe(f):
            open_in_explorer(f.path)
