"""History: every snapshot, plus certification archives and the changelog."""
from __future__ import annotations


import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QHBoxLayout, QInputDialog, QMessageBox, QPushButton,
    QSplitter, QSystemTrayIcon, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import (APP_NAME, fmt_ts, unified_diff)


from ..dialogs import DiffDialog, ExportArchiveDialog, ImportArchiveDialog
from ..common import item, open_in_explorer, selected_data, setup_table

class HistoryPage:
    """Mixin for MainWindow — History page."""

    def build_history_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        for text, fn in (("Restore ALL files to this snapshot", self.restore_snapshot),
                         ("Save as restore point…", self.snapshot_to_rp),
                         ("Compare with current files", self.compare_selected_snapshot),
                         ("Export snapshot as zip…", self.export_selected_zip),
                         ("Export changelog…", self.export_changelog)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)
        split = QSplitter(Qt.Vertical)
        self.snap_table = QTableWidget()
        setup_table(self.snap_table, ["#", "When", "Type", "PC", "Files changed", "Note"], 5)
        self.snap_table.itemSelectionChanged.connect(self.refresh_snapshot_detail)
        split.addWidget(self.snap_table)
        self.snap_detail = QTableWidget()
        setup_table(self.snap_detail, ["File", "Group", "Change", "+/-"], 0)
        self.snap_detail.doubleClicked.connect(self.diff_snapshot_file)
        split.addWidget(self.snap_detail)
        split.setSizes([450, 250])
        lay.addWidget(split)
        lay.addWidget(QLabel("Tip: double-click a file below to see exactly what changed. "
                             "'safety' snapshots are taken automatically before every restore."))
        return w

    def refresh_history(self):
        snaps = self.repo.snapshots(2000)
        rp_by_snap = {}
        for v in self.repo.versions():
            rp_by_snap.setdefault(v.snapshot_id, []).append(f"VERSION v{v.number}")
        for rp in self.repo.restore_points():
            rp_by_snap.setdefault(rp.snapshot_id, []).append(rp.name)
        kind_col = {"auto": "#555", "manual": "#1565c0", "safety": "#8d6e63", "restore": "#2e7d32",
                    "baseline": "#6a1b9a", "import": "#00838f"}
        self.snap_table.setRowCount(len(snaps))
        for r, s in enumerate(snaps):
            n = len(self.repo.snapshot_changes(s.id))
            note = s.message + ("   🏷 " + ", ".join(rp_by_snap[s.id]) if s.id in rp_by_snap else "")
            self.snap_table.setItem(r, 0, item(s.id, s.id))
            self.snap_table.setItem(r, 1, item(fmt_ts(s.created_at)))
            self.snap_table.setItem(r, 2, item(s.kind, color=kind_col.get(s.kind)))
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
            kind = "deleted" if new is None else ("added" if old is None else "modified")
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
                         unified_diff(old, cur, label, "current file"))
        DiffDialog(f"'{label}' → current files",
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
            QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")
            return
        QApplication.restoreOverrideCursor()
        self.set_pref("last_archive_dir", os.path.dirname(path))
        self.sync_dirty = True
        if upload:
            self.statusBar().showMessage("Uploading archive…")
            self.run_bg(lambda r: core.make_sync(r).upload_archive(path),
                        lambda name: self.tray.showMessage(APP_NAME, f"Archive uploaded to the shared location: {name}",
                                                           QSystemTrayIcon.Information, 5000),
                        lambda e: QMessageBox.warning(self, APP_NAME, f"Archive saved, but the upload failed: {e}"))
        miss = f"\n⚠ {res['missing']} tracked file(s) were missing on disk (listed in REPORT.txt)." \
            if res["missing"] else ""
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
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
            choice, ok = QInputDialog.getItem(self, "Import archive", "Where is the archive?",
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
                    QMessageBox.information(self, APP_NAME, "No archives in the shared location yet.")
                    return
                name, ok = QInputDialog.getItem(self, "Import archive", "Archive:", sorted(names, reverse=True), 0,
                                                False)
                if not ok:
                    return
                import tempfile
                QApplication.setOverrideCursor(Qt.WaitCursor)
                try:
                    path = s.download_archive(name, tempfile.mkdtemp(prefix="scm-archive-"))
                except Exception as e:  # noqa: BLE001
                    QMessageBox.warning(self, APP_NAME, f"Download failed: {e}")
                    return
                finally:
                    QApplication.restoreOverrideCursor()
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Open SimConfigManager archive",
                                                  self.pref("last_archive_dir"), "Archive (*.zip)")
        if not path:
            return
        try:
            arc = core.Archive(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Cannot open archive: {e}")
            return
        try:
            dlg = ImportArchiveDialog(self.repo, arc, self)
            if dlg.exec() != QDialog.Accepted:
                return
            targets, write, track, hist = dlg.choices()
            if write:
                running = self.repo.running_sim_processes()
                if running and QMessageBox.question(
                        self, APP_NAME, "These sim programs are running: " + ", ".join(running) +
                        "\nThey may overwrite the imported files when they exit. Continue anyway?") \
                        != QMessageBox.Yes:
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
                QMessageBox.warning(self, APP_NAME, msg + "\n\nProblems:\n" + "\n".join(failed))
            else:
                QMessageBox.information(self, APP_NAME, msg + " A safety snapshot was taken first, so this can "
                                        "be undone from History.")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Import failed: {e}")
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
            self.statusBar().showMessage(f"Changelog written to {path}")
