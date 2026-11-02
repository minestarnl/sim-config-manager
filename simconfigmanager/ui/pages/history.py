"""History: every snapshot, plus certification archives and the changelog."""
from __future__ import annotations


import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QMenu,
    QApplication, QDialog, QFileDialog, QInputDialog, QMessageBox, QSplitter, QTableWidget,
    QVBoxLayout, QWidget,
)

from ... import core
from ...core import (APP_TITLE, fmt_ts, unified_diff)


from ..dialogs import DiffDialog, ExportArchiveDialog, ImportArchiveDialog
from ..common import (SNAPSHOT_KIND, button_row, confirm, fail, hint, inform, item, open_in_explorer,
                      running_warning, selected_data, setup_table, warn)

from . import Page


class HistoryPage:
    """Mixin for MainWindow — History page."""

    PAGE = Page("history", "History", "history", "Every snapshot, certification archives and the changelog", "build_history_page", "refresh_history")

    def build_history_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(button_row(("Restore all files to this snapshot…", self.restore_snapshot),
                                 ("Save as restore point…", self.snapshot_to_rp),
                                 ("Compare with the files on disk", self.compare_selected_snapshot),
                                 trailing=[("Export certification archive…", lambda: self.export_archive(), {"icon": "zip"}),
                                           ("Import certification archive…", self.import_archive),
                                           ("Export changelog…", self.export_changelog),
                                           ("Configuration manual…", self.export_manual, {"icon": "book"})]))
        split = QSplitter(Qt.Vertical)
        self.snap_table = QTableWidget()
        setup_table(self.snap_table, ["#", "When", "Kind", "PC", "Files changed", "Note"], 5)
        self.snap_table.itemSelectionChanged.connect(self.refresh_snapshot_detail)
        self.snap_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.snap_table.customContextMenuRequested.connect(self.snapshot_context)
        split.addWidget(self.snap_table)
        self.snap_detail = QTableWidget()
        setup_table(self.snap_detail, ["File", "Group", "Change", "Lines +/-"], 0)
        self.snap_detail.doubleClicked.connect(self.diff_snapshot_file)
        split.addWidget(self.snap_detail)
        split.setSizes([450, 250])
        lay.addWidget(split)
        lay.addWidget(hint("Double-click a file below to see exactly what changed. A safety copy is recorded "
                           "automatically before every restore, so a restore can always be undone here."))
        return w

    def snapshot_context(self, pos):
        if self.selected_snapshot() is None:
            return
        m = QMenu(self)
        m.addAction("Restore all files to this snapshot…", self.restore_snapshot)
        m.addAction("Save as restore point…", self.snapshot_to_rp)
        m.addAction("Compare with the files on disk", self.compare_selected_snapshot)
        m.addSeparator()
        m.addAction("Export this snapshot as archive…", self.export_selected_zip)
        m.exec(self.snap_table.viewport().mapToGlobal(pos))

    def refresh_history(self):
        snaps = self.repo.snapshots(2000)
        rp_by_snap = {}
        for v in self.repo.versions():
            rp_by_snap.setdefault(v.snapshot_id, []).append(f"v{v.number}")
        for rp in self.repo.restore_points():
            rp_by_snap.setdefault(rp.snapshot_id, []).append(rp.name)
        self.snap_table.setRowCount(len(snaps))
        for r, s in enumerate(snaps):
            n = len(self.repo.snapshot_changes(s.id))
            note = s.message + ("   🏷 " + ", ".join(rp_by_snap[s.id]) if s.id in rp_by_snap else "")
            self.snap_table.setItem(r, 0, item(s.id, s.id))
            self.snap_table.setItem(r, 1, item(fmt_ts(s.created_at)))
            kind, col = SNAPSHOT_KIND.get(s.kind, (s.kind, None))
            self.snap_table.setItem(r, 2, item(kind, color=col))
            self.snap_table.setItem(r, 3, item(s.machine))
            self.snap_table.setItem(r, 4, item(n))
            self.snap_table.setItem(r, 5, item(note, bold=s.id in rp_by_snap))

    def selected_snapshot(self):
        d = selected_data(self.snap_table)
        return d[0] if d else None

    def refresh_snapshot_detail(self):
        sid = self.selected_snapshot()
        self.snap_detail.setRowCount(0)
        if sid is None:
            return
        ch = self.repo.snapshot_changes(sid)
        self.snap_detail.setRowCount(len(ch))
        for r, (fid, old, new) in enumerate(ch):
            f = self.repo.file(fid)
            try:
                a, d = core.diff_stats(self.repo.get_object(old), self.repo.get_object(new))
                pm = f"+{a} / -{d}"
            except FileNotFoundError:
                pm = "?"
            kind = "deleted" if new is None else ("added" if old is None else "changed")
            self.snap_detail.setItem(r, 0, item(f.display if f else fid, (fid, old, new)))
            self.snap_detail.setItem(r, 1, item(f.group if f else ""))
            self.snap_detail.setItem(r, 2, item(kind))
            self.snap_detail.setItem(r, 3, item(pm))

    def diff_snapshot_file(self):
        d = selected_data(self.snap_detail)
        if not d:
            return
        fid, old, new = d[0]
        f = self.repo.file(fid)
        if f is None:
            return
        self.file_diff(f, self.repo.get_object(old), self.repo.get_object(new), "before", "after",
                       f"{f.display}: snapshot #{self.selected_snapshot()}")

    def restore_snapshot(self):
        sid = self.selected_snapshot()
        if sid is not None:
            self.do_restore(sid, f"snapshot #{sid}")

    def snapshot_to_rp(self):
        sid = self.selected_snapshot()
        if sid is not None:
            self.new_restore_point(snapshot_id=sid)

    def compare_selected_snapshot(self):
        sid = self.selected_snapshot()
        if sid is not None:
            self.compare_snapshot_with_current(sid, f"snapshot #{sid}")

    def compare_snapshot_with_current(self, sid: int, label: str):
        parts = []
        for fid, h in self.repo.entries(sid).items():
            f = self.repo.file(fid)
            if not f or not f.active:
                continue
            cur = self.repo.read_current(f)
            try:
                old = self.repo.get_object(h)
            except FileNotFoundError:
                continue
            if (old or b"") == (cur or b"") and (old is None) == (cur is None):
                continue
            parts.append(f"=== {f.display}  [{f.group}]  {f.path}\n" +
                         unified_diff(old, cur, label, "on disk now"))
        DiffDialog(f"{label} → on disk now",
                   "\n\n".join(parts) if parts else "", self).exec()

    def export_selected_zip(self):
        sid = self.selected_snapshot()
        if sid is not None:
            self.export_zip(sid, f"snapshot-{sid}")

    def export_zip(self, sid: int, name: str):
        self.export_archive(snapshot_id=sid, name=name)

    def export_archive(self, snapshot_id: Optional[int] = None, name: str = ""):
        dlg = ExportArchiveDialog(self.repo, name, snapshot_id is None, self)
        if dlg.exec() != QDialog.Accepted:
            return
        path, note, hist = dlg.result_values()
        upload = dlg.upload.isChecked()
        self.repo.set("archive_upload", "1" if upload else "0")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = core.export_archive(self.repo, path, note, hist, snapshot_id)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            fail(self, "export the archive", e)
            return
        QApplication.restoreOverrideCursor()
        self.set_pref("last_archive_dir", os.path.dirname(path))
        self.sync_dirty = True
        if upload:
            self.statusBar().showMessage("Uploading archive…")
            self.run_bg(lambda r: core.make_sync(r).upload_archive(path),
                        lambda name: self.notify(f"Archive uploaded to the shared location: {name}"),
                        lambda e: fail(self, "upload the archive to the shared location", e,
                                       "The archive itself was saved."))
        miss = f"\n⚠ {res['missing']} tracked file(s) were missing on disk (listed in REPORT.txt)." \
            if res["missing"] else ""
        box = QMessageBox(self)
        box.setWindowTitle(APP_TITLE)
        box.setText(f"Archive saved with {res['files']} file(s).{miss}\n\n{path}\n\n"
                    f"SHA-256 of the zip (also saved as .sha256 next to it):\n{res['sha256']}\n\n"
                    "Keep the zip AND the .sha256 file together somewhere safe.")
        box.addButton(QMessageBox.Ok)
        openb = box.addButton("Open folder", QMessageBox.ActionRole)
        box.exec()
        if box.clickedButton() is openb:
            open_in_explorer(path)
        self.refresh_all()

    def import_archive(self):
        path = None
        s = self.sync()
        if s is not None:
            choice, ok = QInputDialog.getItem(self, "Import certification archive", "Where is the archive?",
                                              ["On this computer…", "In the shared location (archives)…"], 0, False)
            if not ok:
                return
            if choice.startswith("In the shared"):
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    names = s.list_archives()
                finally:
                    QApplication.restoreOverrideCursor()
                if not names:
                    inform(self, "There are no archives in the shared location yet.")
                    return
                name, ok = QInputDialog.getItem(self, "Import certification archive", "Archive:", sorted(names, reverse=True), 0,
                                                False)
                if not ok:
                    return
                import tempfile
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    path = s.download_archive(name, tempfile.mkdtemp(prefix="scm-archive-"))
                except Exception as e:  # noqa: BLE001
                    fail(self, "download the archive", e)
                    return
                finally:
                    QApplication.restoreOverrideCursor()
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Import certification archive",
                                                  self.pref("last_archive_dir"), "Archive (*.zip)")
        if not path:
            return
        try:
            arc = core.Archive(path)
        except Exception as e:  # noqa: BLE001
            fail(self, "open the archive", e)
            return
        try:
            dlg = ImportArchiveDialog(self.repo, arc, self)
            if dlg.exec() != QDialog.Accepted:
                return
            targets, write, track, hist = dlg.choices()
            if write:
                warning = running_warning(self.repo)
                if warning and not confirm(self, "Write the files from the archive now?", "Write files",
                                           details=warning.strip()):
                    return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                report = arc.apply(self.repo, targets, write, track, hist)
            finally:
                QApplication.restoreOverrideCursor()
            failed = [f"{lbl}: {r}" for lbl, r in report if r.startswith(("FAILED", "SKIPPED"))]
            done = sum(1 for _, r in report if r == "written")
            msg = f"{done} file(s) written."
            if failed:
                warn(self, msg + "\n\nProblems:\n" + "\n".join(failed))
            else:
                inform(self, msg + " A safety copy was recorded first, so this can be undone from History.")
        except Exception as e:  # noqa: BLE001
            fail(self, "import the archive", e)
        finally:
            arc.close()
            self.sync_dirty = True
            self.refresh_all()

    def export_changelog(self):
        path, flt = QFileDialog.getSaveFileName(self, "Export changelog", "changelog.md",
                                                "Markdown – paste into Notion (*.md);;CSV – import into "
                                                "Notion database or Google Sheets (*.csv)")
        if path:
            fmt = "csv" if path.lower().endswith(".csv") else "md"
            Path(path).write_text(self.repo.changelog(fmt), encoding="utf-8", newline="" if fmt == "csv" else None)
            self.notify(f"Changelog saved: {path}")

    def export_manual(self):
        """Configuration manual (#13): Markdown for Notion, HTML, or PDF."""
        name = f"{core.safe_name(self.repo.machine)}_configuration_manual"
        path, _flt = QFileDialog.getSaveFileName(
            self, "Export configuration manual", os.path.join(self.pref("last_archive_dir") or "", name + ".pdf"),
            "PDF (*.pdf);;Markdown – paste into Notion (*.md);;Web page (*.html)")
        if not path:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            md = self.repo.build_manual()
            title = f"{self.repo.machine} — configuration manual"
            if path.lower().endswith(".md"):
                Path(path).write_text(md, encoding="utf-8")
            elif path.lower().endswith((".html", ".htm")):
                Path(path).write_text(core.markdown_to_html(md, title), encoding="utf-8")
            else:
                from PySide6.QtGui import QPageSize, QPdfWriter, QTextDocument
                if not path.lower().endswith(".pdf"):
                    path += ".pdf"
                doc = QTextDocument()
                doc.setHtml(core.markdown_to_html(md, title))
                w = QPdfWriter(path)
                w.setPageSize(QPageSize(QPageSize.A4))
                w.setResolution(110)
                w.setTitle(title)
                doc.print_(w)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            fail(self, "export the manual", e)
            return
        QApplication.restoreOverrideCursor()
        self.notify(f"Configuration manual saved: {path}")
        open_in_explorer(path)
