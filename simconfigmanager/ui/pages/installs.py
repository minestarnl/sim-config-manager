"""Installs & integrity: record what an add-on installer changes (with rollback), and checksum whole installations."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QLineEdit, QListWidget, QSplitter, QTableWidget, QTextEdit, QVBoxLayout,
    QWidget,
)

from ...core import fmt_ts
from ...theme import TLabel as QLabel
from ..common import (BAD, MUTED, OK, WARN, banner, button_row, colored, confirm, fail, heading, hint, inform, item,
                      selected_data, setup_table)
from ..dialogs import pick_folder
from . import Page


def _size(n) -> str:
    n = float(n or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return ""


class FolderListDialog(QDialog):
    """Name + list of folders on the sim (used for install recordings and manifests)."""

    def __init__(self, parent, title: str, intro: str, name: str, folders: list[str], extra: str = ""):
        super().__init__(parent)
        self.win = parent
        self.setWindowTitle(title)
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        lay.addWidget(hint(intro))
        self.name = QLineEdit(name)
        lay.addWidget(QLabel("Name:"))
        lay.addWidget(self.name)
        lay.addWidget(QLabel("Folders:"))
        self.list = QListWidget()
        self.list.addItems(folders)
        lay.addWidget(self.list, 1)
        lay.addLayout(button_row(("Add folder…", self.add), ("Remove", self.remove)))
        self.extra = QCheckBox(extra) if extra else None
        if self.extra:
            lay.addWidget(self.extra)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def add(self):
        d = pick_folder(self, self.win.repo, "Folder")
        if d:
            self.list.addItem(d)

    def remove(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def values(self):
        return self.name.text().strip(), [self.list.item(i).text() for i in range(self.list.count())]


class InstallsPage:
    """Mixin for MainWindow — Installs & integrity page."""

    PAGE = Page("installs", "Installs & integrity", "package",
                "What an installer changed (with rollback) and checksums of whole installations", "build_installs_page",
                "refresh_installs")

    def build_installs_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        split = QSplitter(Qt.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(heading("Recorded installs"))
        self.inst_status = QLabel()
        self.inst_status.setTextFormat(Qt.RichText)
        self.inst_status.setWordWrap(True)
        tl.addWidget(self.inst_status)
        tl.addLayout(button_row(("Start recording…", self.start_install_dialog, {"primary": True, "icon": "record"}),
                                ("Stop and save…", self.finish_install_dialog), ("Cancel recording", self.cancel_install)))
        self.inst_table = QTableWidget()
        setup_table(self.inst_table, ["When", "Name", "Added", "Changed", "Removed", "Status", "Version"], 1)
        self.inst_table.doubleClicked.connect(lambda _i: self.show_install())
        tl.addWidget(self.inst_table)
        tl.addLayout(button_row(("Details…", self.show_install), ("Track new config files…", self.track_install_files),
                                trailing=[("Roll back…", self.rollback_install_dialog, {"danger": True})]))
        split.addWidget(top)

        low = QWidget()
        bl = QVBoxLayout(low)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(heading("Integrity manifests"))
        bl.addWidget(hint("A checksum of every file in an installation (Prepar3D, ProSim, aircraft folders). Verify "
                          "later to see exactly which files were added, removed or changed."))
        bl.addLayout(button_row(("Record manifest…", self.create_manifest_dialog), ("Verify now", self.verify_manifest),
                                ("Details…", self.show_manifest), trailing=[("Delete…", self.delete_manifest)]))
        self.man_table = QTableWidget()
        setup_table(self.man_table, ["Name", "Recorded", "Files", "Size", "Version", "Last check", "Result"], 0)
        self.man_table.doubleClicked.connect(lambda _i: self.show_manifest())
        bl.addWidget(self.man_table)
        split.addWidget(low)
        split.setSizes([360, 300])
        lay.addWidget(split, 1)
        return w

    def refresh_installs(self):
        ins = self.repo.installs()
        rec = next((i for i in ins if i["status"] == "recording"), None)
        self.inst_status.setText(
            colored(f"● Recording '{rec['name']}' since {fmt_ts(rec['started'])}", WARN, True) +
            " — run the installer now, then click <b>Stop and save</b>." if rec else
            "Before running an add-on installer, start a recording. Afterwards you see every file it added, "
            "changed or removed, can make a version for it — and can roll it back.")
        self.inst_table.setRowCount(len(ins))
        for r, i in enumerate(ins):
            c = i["summary"].get("counts", {})
            st = {"recording": ("Recording…", WARN), "done": ("Recorded", OK),
                  "rolled_back": ("Rolled back", MUTED)}.get(i["status"], (i["status"], None))
            self.inst_table.setItem(r, 0, item(fmt_ts(i["started"]), i["id"]))
            self.inst_table.setItem(r, 1, item(i["name"], bold=True))
            for col, k in ((2, "added"), (3, "changed"), (4, "removed")):
                self.inst_table.setItem(r, col, item(c.get(k, "–")))
            self.inst_table.setItem(r, 5, item(st[0], color=st[1]))
            self.inst_table.setItem(r, 6, item(f"v{i['version_number']}" if i.get("version_number") else ""))
        mans = self.repo.manifests()
        self.man_table.setRowCount(len(mans))
        for r, m in enumerate(mans):
            lc = m["last_check"]
            if lc:
                c = lc["counts"]
                res = ("✔ All files match", OK) if lc["ok"] else (
                    f"✖ {c['changed']} changed, {c['added']} added, {c['removed']} missing", BAD)
            else:
                res = ("Not verified yet", MUTED)
            self.man_table.setItem(r, 0, item(m["name"], m["id"], bold=True))
            self.man_table.setItem(r, 1, item(fmt_ts(m["created"])))
            self.man_table.setItem(r, 2, item(f"{m['files']:,}"))
            self.man_table.setItem(r, 3, item(_size(m["bytes"])))
            self.man_table.setItem(r, 4, item(f"v{m['version_number']}" if m.get("version_number") else ""))
            self.man_table.setItem(r, 5, item(fmt_ts(lc["ts"]) if lc else "–"))
            self.man_table.setItem(r, 6, item(res[0], color=res[1], bold=bool(lc and not lc["ok"])))

    # ---------------------------------------------------------------- installs
    def _bg_action(self, what: str, fn, done):
        if (getattr(self, "live", {}) or {}).get("running") and not confirm(
                self, "The sim is running.", "Continue anyway",
                details="Scanning big folders reads every file and uses disk and CPU time. Best done when the sim "
                        "is not in use."):
            return
        self.statusBar().showMessage(f"{what}… (this can take a few minutes for big folders)")

        def failed(e):
            self.statusBar().clearMessage()
            fail(self, what[0].lower() + what[1:], e)
        self.run_bg(fn, done, failed, write=True)

    def start_install_dialog(self):
        dlg = FolderListDialog(self, "Start recording an install",
                               "Folders the installer may change. Config files in them are copied first, so changes "
                               "can be rolled back.", "", self.repo.default_install_roots())
        if dlg.exec() != QDialog.Accepted:
            return
        name, roots = dlg.values()
        if not name or not roots:
            fail(self, "start the recording", "Give it a name and at least one folder.")
            return

        def done(rec):
            self.notify(f"Recording '{rec['name']}' — run the installer now, then click Stop and save")
            self.refresh_installs()
        self._bg_action("Scanning the folders", lambda r: r.start_install(name, roots), done)

    def _recording(self):
        return next((i for i in self.repo.installs() if i["status"] == "recording"), None)

    def finish_install_dialog(self):
        rec = self._recording()
        if not rec:
            inform(self, "No install is being recorded.")
            return
        box = QCheckBox(f"Create a new version 'Installed {rec['name']}'")
        box.setChecked(True)
        dlg = QDialog(self)
        dlg.setWindowTitle("Stop and save")
        lay = QVBoxLayout(dlg)
        lay.addWidget(hint(f"Is the installer for <b>{html.escape(rec['name'])}</b> finished? The folders are "
                           "scanned again and the changes saved."))
        lay.addWidget(box)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Stop and save")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        author = self.repo.get("version_author")

        def done(res):
            c = res["summary"].get("counts", {})
            self.notify(f"'{res['name']}': {c.get('added', 0)} added, {c.get('changed', 0)} changed, "
                        f"{c.get('removed', 0)} removed")
            self.sync_dirty = True
            self.refresh_all()
        self._bg_action("Comparing the folders", lambda r: r.finish_install(rec["id"], box.isChecked(), author), done)

    def cancel_install(self):
        rec = self._recording()
        if rec and confirm(self, f"Cancel the recording of '{rec['name']}'?", "Cancel recording", danger=True):
            self.repo.cancel_install(rec["id"])
            self.refresh_installs()

    def selected_install(self):
        ids = selected_data(self.inst_table)
        return self.repo.install_record(ids[0]) if ids else None

    def show_install(self):
        i = self.selected_install()
        if not i:
            return
        s = i["summary"]
        parts = [f"<h3 style='margin:0'>{html.escape(i['name'])}</h3><p style='color:{MUTED}'>"
                 f"{fmt_ts(i['started'])} · {html.escape(', '.join(i['roots']))}</p>"]
        for key, title in (("added", "Added"), ("changed", "Changed"), ("removed", "Removed")):
            lst = s.get(key) or []
            parts.append(f"<b>{title} ({len(lst)})</b><ul>" + "".join(
                f"<li>{html.escape(x['path'])} <span style='color:{MUTED}'>{_size(x['size'])}</span></li>"
                for x in lst[:400]) + ("<li>…</li>" if len(lst) > 400 else "") + "</ul>")
        self._text_dialog(f"Install: {i['name']}", "".join(parts))

    def _text_dialog(self, title: str, html_text: str):
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(860, 600)
        lay = QVBoxLayout(dlg)
        t = QTextEdit()
        t.setReadOnly(True)
        t.setHtml(html_text)
        lay.addWidget(t)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def track_install_files(self):
        i = self.selected_install()
        if not i:
            return
        new = i["summary"].get("new_config_files") or []
        if not new:
            inform(self, "This install added no new config files that aren't tracked yet.")
            return
        if confirm(self, f"Track {len(new)} new config file(s) from '{i['name']}'?", f"Track {len(new)} files",
                   details="\n".join(new[:20]) + ("\n…" if len(new) > 20 else "")):
            n = self.repo.track_new_config_files(new, i["name"])
            self.sync_dirty = True
            self.notify(f"Now tracking {n} more file(s)")

    def rollback_install_dialog(self):
        i = self.selected_install()
        if not i or i["status"] != "done":
            inform(self, "Select a recorded install that hasn't been rolled back.")
            return
        p = self.repo.rollback_preview(i["id"])
        details = (f"{len(p['remove'])} added file(s) will be removed and {len(p['restore'])} changed or removed "
                   "config file(s) put back.")
        if p["cannot_restore"]:
            details += (f"\n\n⚠ {len(p['cannot_restore'])} large file(s) were changed but not copied beforehand; "
                        "reinstall to repair them:\n" + "\n".join(p["cannot_restore"][:10]))
        if not confirm(self, f"Roll back '{i['name']}'?", "Roll back", danger=True, details=details +
                       "\n\nClose the sim first. A safety snapshot of the tracked files is taken first."):
            return

        def done(rep):
            self.sync_dirty = True
            msg = f"{len(rep['removed'])} removed, {len(rep['restored'])} restored"
            if rep["skipped"] or rep["not_restorable"]:
                self._text_dialog("Rollback report", "<p>" + msg + "</p>" + "".join(
                    f"<b>{t}</b><ul>" + "".join(f"<li>{html.escape(x)}</li>" for x in rep[k]) + "</ul>"
                    for k, t in (("skipped", "Skipped"), ("not_restorable", "Not restorable — reinstall"))
                    if rep[k]))
            self.notify(f"Rolled back '{i['name']}': {msg}")
            self.refresh_all()
        self._bg_action("Rolling back", lambda r: r.rollback_install(i["id"]), done)

    # ---------------------------------------------------------------- integrity manifests
    def create_manifest_dialog(self):
        dlg = FolderListDialog(self, "Record integrity manifest",
                               "A checksum of every file in these folders is recorded. Large folders (scenery) take a "
                               "while; it runs in the background.", "Prepar3D + ProSim installation",
                               self.repo.default_install_roots(), "Link it to the current version")
        dlg.extra.setChecked(True)
        if dlg.exec() != QDialog.Accepted:
            return
        name, roots = dlg.values()
        link = dlg.extra.isChecked()

        def done(m):
            self.notify(f"Manifest '{m['name']}' recorded: {m['files']:,} files")
            self.refresh_installs()
        self._bg_action("Recording the manifest", lambda r: r.create_manifest(name, roots, link), done)

    def verify_manifest(self):
        ids = selected_data(self.man_table)
        if not ids:
            inform(self, "Select a manifest first.")
            return

        def done(res):
            c = res["counts"]
            self.notify("All files match" if res["ok"] else
                        f"Differences: {c['changed']} changed, {c['added']} added, {c['removed']} missing",
                        warning=not res["ok"])
            self.refresh_installs()
        self._bg_action("Verifying", lambda r: r.verify_manifest(ids[0]), done)

    def show_manifest(self):
        ids = selected_data(self.man_table)
        m = next((x for x in self.repo.manifests() if ids and x["id"] == ids[0]), None)
        if not m:
            return
        lc = m["last_check"]
        parts = [f"<h3 style='margin:0'>{html.escape(m['name'])}</h3><p style='color:{MUTED}'>Recorded "
                 f"{fmt_ts(m['created'])} · {m['files']:,} files · {_size(m['bytes'])}<br>"
                 f"{'<br>'.join(html.escape(r) for r in m['roots'])}</p>"]
        if not lc:
            parts.append("<p>Not verified yet.</p>")
        else:
            parts.append(f"<p>Last check {fmt_ts(lc['ts'])}: " + ("all files match.</p>" if lc["ok"] else "</p>"))
            for key, title in (("changed", "Changed"), ("added", "Added"), ("removed", "Missing")):
                if lc.get(key):
                    parts.append(f"<b>{title} ({lc['counts'][key]})</b><ul>" + "".join(
                        f"<li>{html.escape(x)}</li>" for x in lc[key]) + "</ul>")
        self._text_dialog(f"Manifest: {m['name']}", "".join(parts))

    def delete_manifest(self):
        ids = selected_data(self.man_table)
        if ids and confirm(self, "Delete this manifest?", "Delete", danger=True):
            self.repo.delete_manifest(ids[0])
            self.refresh_installs()


__all__ = ["InstallsPage", "banner"]
