"""Versions: numbered configurations and what differs from them."""
from __future__ import annotations


import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QMessageBox, QPushButton, QSplitter,
    QSystemTrayIcon, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...theme import TLabel as QLabel, TTextEdit as QTextEdit  # theme-aware versions
from ...core import (APP_NAME, fmt_ts, unified_diff)


from ..dialogs import CreateVersionDialog, DiffDialog
from ..common import item, selected_data, setup_table

class VersionsPage:
    """Mixin for MainWindow — Versions page."""

    def build_versions_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.ver_banner = QLabel()
        self.ver_banner.setWordWrap(True)
        self.ver_banner.setTextFormat(Qt.RichText)
        top.addWidget(self.ver_banner, 1)
        b = QPushButton("★ Create new version…")
        b.setMinimumHeight(44)
        b.setStyleSheet("QPushButton { background:#1f4e79; color:white; font-weight:bold; font-size:14px;"
                        " padding:6px 16px; border-radius:6px; }")
        b.clicked.connect(self.create_version)
        top.addWidget(b)
        lay.addLayout(top)

        row = QHBoxLayout()
        row.addWidget(QLabel("Compare files on disk with:"))
        self.ver_combo = QComboBox()
        self.ver_combo.setMinimumWidth(320)
        self.ver_combo.currentIndexChanged.connect(lambda _i: self.refresh_version_overview())
        row.addWidget(self.ver_combo)
        self.ver_only_mism = QCheckBox("Only show files that differ")
        self.ver_only_mism.toggled.connect(lambda _v: self.refresh_version_overview())
        row.addWidget(self.ver_only_mism)
        row.addStretch()
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_versions)
        row.addWidget(b)
        lay.addLayout(row)

        split = QSplitter(Qt.Vertical)
        upper = QWidget()
        ul = QVBoxLayout(upper)
        ul.setContentsMargins(0, 0, 0, 0)
        self.vo_table = QTableWidget()
        setup_table(self.vo_table, ["Status", "File", "Group", "Last changed", "Path"], 4)
        self.vo_table.doubleClicked.connect(lambda _i: self.ver_show_diff())
        ul.addWidget(self.vo_table)
        br = QHBoxLayout()
        for text, fn in (("Show difference", self.ver_show_diff), ("Open in editor", self.ver_open_editor),
                         ("Put selected back to this version", self.ver_put_back_selected),
                         ("Put ALL differing files back", self.ver_put_back_all),
                         ("Export this version (archive)…", self.ver_export)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            br.addWidget(b)
        br.addStretch()
        ul.addLayout(br)
        split.addWidget(upper)

        lower = QWidget()
        ll = QHBoxLayout(lower)
        ll.setContentsMargins(0, 0, 0, 0)
        left = QVBoxLayout()
        left.addWidget(QLabel("<b>All versions</b>"))
        self.ver_list = QTableWidget()
        setup_table(self.ver_list, ["Version", "Title", "Date", "By", "Files changed"], 1)
        self.ver_list.itemSelectionChanged.connect(self.show_version_notes)
        left.addWidget(self.ver_list)
        lb = QHBoxLayout()
        b = QPushButton("Compare with previous version")
        b.clicked.connect(self.ver_compare_prev)
        lb.addWidget(b)
        b = QPushButton("Use for the overview above")
        b.clicked.connect(self.ver_select_for_overview)
        lb.addWidget(b)
        lb.addStretch()
        left.addLayout(lb)
        ll.addLayout(left, 3)
        right = QVBoxLayout()
        right.addWidget(QLabel("<b>Release notes</b>"))
        self.ver_notes = QTextEdit()
        self.ver_notes.setReadOnly(True)
        right.addWidget(self.ver_notes)
        ll.addLayout(right, 2)
        split.addWidget(lower)
        split.setSizes([430, 260])
        lay.addWidget(split)
        return w

    def refresh_versions(self):
        vers = self.repo.versions()
        cur = self.ver_combo.currentData()
        self.ver_combo.blockSignals(True)
        self.ver_combo.clear()
        for v in vers:
            self.ver_combo.addItem(("Latest: " if v is vers[0] else "") +
                                   f"{v.label}  ({fmt_ts(v.created_at)})", v.number)
        i = self.ver_combo.findData(cur)
        self.ver_combo.setCurrentIndex(max(i, 0))
        self.ver_combo.blockSignals(False)
        # version list
        self.ver_list.setRowCount(len(vers))
        for r, v in enumerate(vers):
            prev = vers[r + 1].snapshot_id if r + 1 < len(vers) else None
            n = len(self.repo.changes_between(prev, v.snapshot_id))
            self.ver_list.setItem(r, 0, item(f"v{v.number}", v.number, bold=True))
            self.ver_list.setItem(r, 1, item(v.title))
            self.ver_list.setItem(r, 2, item(fmt_ts(v.created_at)))
            self.ver_list.setItem(r, 3, item(v.created_by))
            self.ver_list.setItem(r, 4, item(n if prev else f"{n} (first)"))
        self.refresh_version_overview()

    def refresh_version_overview(self):
        number = self.ver_combo.currentData()
        ver, status = self.repo.version_status(number)
        latest = self.repo.latest_version()
        if ver is None:
            self.ver_banner.setText(
                "<h2 style='margin:0'>No version yet</h2>"
                f"{len(status)} files are tracked. When the sims are set up the way you want them, click "
                "<b>Create new version</b>. From then on this page shows whether anything differs from it.")
            self.vo_table.setRowCount(0)
            return
        mism = [x for x in status if x.status in ("modified", "missing", "new")]
        which = "current version" if latest and ver.number == latest.number else "selected (older) version"
        if mism:
            col, head = "#e65100", f"⚠ {len(mism)} of {len(status)} files differ from {ver.label}"
            sub = ("Either create a new version to accept these changes, or put the files back.")
        else:
            col, head = "#2e7d32", f"✔ All {len(status)} files match {ver.label}"
            sub = "Nothing has changed since this version."
        self.ver_banner.setText(f"<h2 style='margin:0;color:{col}'>{html.escape(head)}</h2>"
                                f"<span style='color:#555'>{which.capitalize()}, created "
                                f"{fmt_ts(ver.created_at)} by {html.escape(ver.created_by or '-')} on "
                                f"{html.escape(ver.machine)}. {sub}</span>")
        order = {"modified": 0, "missing": 1, "new": 2, "untracked": 3, "match": 4, "gone": 5}
        rows = sorted(status, key=lambda x: (order.get(x.status, 9), x.file.group, x.file.display))
        if self.ver_only_mism.isChecked():
            rows = [x for x in rows if x.status in ("modified", "missing", "new")]
        colors = {"match": "#2e7d32", "modified": "#e65100", "missing": "#c62828", "new": "#1565c0",
                  "gone": "#777", "untracked": "#777"}
        icons = {"match": "✔", "modified": "✎", "missing": "✖", "new": "＋", "gone": "–", "untracked": "–"}
        self.vo_table.setRowCount(len(rows))
        for r, x in enumerate(rows):
            hist = self.repo.file_history(x.file.id)
            self.vo_table.setItem(r, 0, item(f"{icons.get(x.status, '')} {core.STATUS_TEXT.get(x.status, x.status)}",
                                              x, colors.get(x.status), bold=x.status != "match"))
            self.vo_table.setItem(r, 1, item(x.file.display))
            self.vo_table.setItem(r, 2, item(x.file.group))
            self.vo_table.setItem(r, 3, item(fmt_ts(hist[0].created_at) if hist else "–"))
            self.vo_table.setItem(r, 4, item(x.file.path))

    def selected_vstatus(self):
        return selected_data(self.vo_table)

    def ver_show_diff(self):
        sel = self.selected_vstatus()
        if not sel:
            return
        x = sel[0]
        number = self.ver_combo.currentData()
        self.file_diff(x.file, self.repo.get_object(x.version_hash), self.repo.read_current(x.file),
                       f"v{number}", "on disk now", f"{x.file.display}: v{number} → now")

    def ver_open_editor(self):
        sel = self.selected_vstatus()
        if sel:
            self.open_editor(sel[0].file)

    def ver_put_back(self, statuses):
        number = self.ver_combo.currentData()
        ver = self.repo.version(number) if number is not None else None
        if not ver:
            return
        todo = [x for x in statuses if x.status in ("modified", "missing")]
        new_files = [x for x in statuses if x.status == "new"]
        if not todo:
            msg = "Nothing to put back."
            if new_files:
                msg += ("\n\nFiles marked 'Not in version' were added to tracking after this version; there "
                        "is no copy of them in it. Create a new version to include them.")
            QMessageBox.information(self, APP_NAME, msg)
            return
        self.do_restore(ver.snapshot_id, ver.label, [x.file.id for x in todo])

    def ver_put_back_selected(self):
        self.ver_put_back(self.selected_vstatus())

    def ver_put_back_all(self):
        self.ver_put_back(self.repo.mismatches(self.ver_combo.currentData()))

    def ver_export(self):
        number = self.ver_combo.currentData()
        ver = self.repo.version(number) if number is not None else None
        if ver:
            self.export_archive(snapshot_id=ver.snapshot_id, name=f"v{ver.number}")

    def selected_version_number(self):
        d = selected_data(self.ver_list)
        return d[0] if d else None

    def show_version_notes(self):
        n = self.selected_version_number()
        v = self.repo.version(n) if n is not None else None
        if not v:
            self.ver_notes.clear()
            return
        vers = self.repo.versions()
        idx = next(i for i, x in enumerate(vers) if x.number == v.number)
        prev = vers[idx + 1] if idx + 1 < len(vers) else None
        ch = self.repo.changes_between(prev.snapshot_id if prev else None, v.snapshot_id)
        files = "".join(f"<li>{html.escape(f.display)} <span style='color:#777'>[{html.escape(f.group)}]</span> "
                        f"{'added' if a is None else ('deleted' if b is None else 'changed')}</li>"
                        for f, a, b in ch)
        self.ver_notes.setHtml(
            f"<h3 style='margin:0'>{html.escape(v.label)}</h3>"
            f"<p style='color:#555'>{fmt_ts(v.created_at)} · {html.escape(v.created_by or '-')} · "
            f"{html.escape(v.machine)}</p>"
            f"<pre style='white-space:pre-wrap;font-family:Segoe UI,sans-serif'>{html.escape(v.notes or '')}</pre>"
            f"<p><b>{'Files in this first version' if not prev else 'Files changed since v' + str(prev.number)}:"
            f"</b></p><ul>{files or '<li>none</li>'}</ul>")

    def ver_select_for_overview(self):
        n = self.selected_version_number()
        if n is not None:
            self.ver_combo.setCurrentIndex(self.ver_combo.findData(n))

    def ver_compare_prev(self):
        n = self.selected_version_number()
        if n is None:
            return
        vers = self.repo.versions()
        idx = next(i for i, x in enumerate(vers) if x.number == n)
        if idx + 1 >= len(vers):
            QMessageBox.information(self, APP_NAME, "This is the first version — nothing to compare with.")
            return
        prev, v = vers[idx + 1], vers[idx]
        parts = []
        for f, a, b in self.repo.changes_between(prev.snapshot_id, v.snapshot_id):
            parts.append(f"=== {f.display}  [{f.group}]  {f.path}\n" +
                         unified_diff(self.repo.get_object(a), self.repo.get_object(b), f"v{prev.number}",
                                      f"v{v.number}"))
        DiffDialog(f"v{prev.number} → v{v.number}", "\n\n".join(parts), self).exec()

    def create_version(self):
        self.repo.ensure_baseline()
        dlg = CreateVersionDialog(self.repo, self)
        if dlg.exec() != QDialog.Accepted:
            return
        title, notes, by = dlg.values()
        self.repo.set("version_author", by)
        v = self.repo.create_version(title, notes, by)
        self.sync_dirty = True
        self._last_mismatch_count = 0
        self.tray.showMessage(APP_NAME, f"{v.label} created — all {len(self.repo.files())} files recorded.",
                              QSystemTrayIcon.Information, 5000)
        self.show_window()
        self.tabs.setCurrentWidget(self.tab_versions)
        self.ver_combo.setCurrentIndex(0)
        self.refresh_all()
