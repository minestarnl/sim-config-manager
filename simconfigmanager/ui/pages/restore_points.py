"""Restore points: put every file back to a saved setup in one click."""
from __future__ import annotations


from typing import Optional

from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QInputDialog, QMenu, QPushButton, QVBoxLayout, QWidget,
    QColorDialog, QScrollArea,
)

from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import fmt_ts



from ..common import ACCENT, OK, WARN, button, colored, confirm, fail, hint, inform
from . import Page


class RestorePointsPage:
    """Mixin for MainWindow — Restore points page."""

    PAGE = Page("restore", "Restore points", "restore", "Put every file back to a saved setup in one click", "build_restore_page", "refresh_restore_points")

    def build_restore_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.quick_info = hint("")
        top.addWidget(self.quick_info, 1)
        top.addWidget(button("Save current setup as restore point…", self.new_restore_point, primary=True,
                             icon="save"), 0, Qt.AlignTop)
        lay.addLayout(top)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.quick_holder = QWidget()
        self.quick_grid = QGridLayout(self.quick_holder)
        self.quick_grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        scroll.setWidget(self.quick_holder)
        lay.addWidget(scroll)
        self.quick_status = QLabel()
        lay.addWidget(self.quick_status)
        return w

    def refresh_restore_points(self):
        while self.quick_grid.count():
            wdg = self.quick_grid.takeAt(0).widget()
            if wdg:
                wdg.hide()
                wdg.setParent(None)
                wdg.deleteLater()
        rps = self.repo.restore_points()
        self.quick_info.setText("Each button restores every tracked file to how it was when the restore point was "
                                "saved. A safety snapshot is always taken first, so a restore can be undone from "
                                "History. Right-click a button for more options (restore one group, compare, edit…)." if rps else
                                "No restore points yet. Set the sims up the way you want them, then click "
                                "'Save current setup as restore point…'.")
        for i, rp in enumerate(rps):
            btn = QPushButton(f"{rp.name}\n{rp.description[:40]}" if rp.description else rp.name)
            btn.setMinimumSize(QSize(240, 90))
            col = rp.color or ACCENT
            btn.setStyleSheet(f"QPushButton {{ background:{col}; color:white; font-size:15px; font-weight:bold;"
                              f" border-radius:10px; padding:8px; }} QPushButton:hover {{ border:3px solid #ffb300; }}")
            btn.setToolTip(f"Saved {fmt_ts(rp.created_at)} — snapshot #{rp.snapshot_id}\n{rp.description}")
            btn.clicked.connect(lambda _=False, r=rp: self.restore_rp(r))
            btn.setContextMenuPolicy(Qt.CustomContextMenu)
            btn.customContextMenuRequested.connect(lambda _pos, r=rp, b=btn: self.rp_context(r, b))
            self.quick_grid.addWidget(btn, i // 4, i % 4)
        files = self.repo.files()
        changed = [f for f in files if self.repo.file_status(f) in ("Changed", "Missing")]
        ver = self.repo.latest_version()
        vtxt = ""
        if ver:
            n = len(self.repo.mismatches())
            vtxt = (" · " + colored(f"{n} differ from v{ver.number}", WARN, True) if n
                    else " · " + colored(f"all match v{ver.number}", OK))
        self.quick_status.setText(f"{len(files)} tracked files{vtxt} · "
                                  + (colored(f"{len(changed)} changed since the last snapshot", WARN, True)
                                     if changed else "nothing changed since the last snapshot"))

    def rp_context(self, rp, btn):
        m = QMenu(self)
        m.addAction("Restore all files…", lambda: self.restore_rp(rp))
        grp = m.addMenu("Restore one group")
        for g in self.repo.groups():
            grp.addAction(g, lambda gg=g: self.restore_rp(rp, group=gg))
        m.addAction("Compare with the files on disk…", lambda: self.compare_snapshot_with_current(rp.snapshot_id,
                                                                                              rp.name))
        m.addSeparator()
        m.addAction("Edit name, description and colour…", lambda: self.edit_rp(rp))
        m.addAction("Replace with the files on disk now…", lambda: self.overwrite_rp(rp))
        m.addAction("Export files as zip…", lambda: self.export_zip(rp.snapshot_id, rp.name))
        m.addSeparator()
        m.addAction("Delete restore point…", lambda: self.delete_rp(rp))
        m.exec(btn.mapToGlobal(btn.rect().center()))

    def new_restore_point(self, snapshot_id: Optional[int] = None):
        name, ok = QInputDialog.getText(self, "Restore point", "Name (e.g. 'Customer default', 'Checkride B737'):")
        if not ok or not name.strip():
            return
        desc, _ = QInputDialog.getText(self, "Restore point", "Short description (optional):")
        if self.repo.restore_point(name.strip()):
            if not confirm(self, f"A restore point called '{name.strip()}' already exists.", "Replace it"):
                return
        self.repo.create_restore_point(name.strip(), desc, snapshot_id=snapshot_id)
        self.sync_dirty = True
        self.refresh_all()

    def edit_rp(self, rp):
        name, ok = QInputDialog.getText(self, "Restore point", "Name:", text=rp.name)
        if not ok:
            return
        desc, ok = QInputDialog.getText(self, "Restore point", "Description:", text=rp.description)
        if not ok:
            return
        col = QColorDialog.getColor(QColor(rp.color or ACCENT), self, "Button colour")
        self.repo.update_restore_point(rp.id, name.strip() or rp.name, desc, col.name() if col.isValid()
                                       else rp.color)
        self.sync_dirty = True
        self.refresh_all()

    def overwrite_rp(self, rp):
        if confirm(self, f"Replace '{rp.name}' with the files as they are on disk right now?", "Replace"):
            snap = self.repo.commit(f"Restore point update: {rp.name}") or self.repo.latest_snapshot()
            self.repo.update_restore_point_snapshot(rp.id, snap.id)
            self.sync_dirty = True
            self.refresh_all()

    def delete_rp(self, rp):
        if confirm(self, f"Delete restore point '{rp.name}'?", "Delete", danger=True,
                   details="The snapshot itself stays in History."):
            self.repo.delete_restore_point(rp.id)
            self.sync_dirty = True
            self.refresh_all()

    def restore_rp(self, rp, group: Optional[str] = None):
        ids = None
        if group:
            ids = [f.id for f in self.repo.files() if f.group == group]
        self.do_restore(rp.snapshot_id, rp.name + (f" (group {group})" if group else ""), ids)

    def do_restore(self, snapshot_id: int, label: str, file_ids=None):
        preview = self.repo.preview_restore(snapshot_id, file_ids)
        todo = [(f, a) for f, a in preview if a == "will be restored"]
        if not todo:
            inform(self, f"Nothing to restore — every file already matches '{label}'.")
            return
        running = self.repo.running_sim_processes()
        txt = "\n".join(f"• {f.display}  [{f.group}]" for f, _ in todo[:25])
        if len(todo) > 25:
            txt += f"\n… and {len(todo) - 25} more"
        warn = ""
        if running:
            warn = ("\n\n⚠ These sim programs are running: " + ", ".join(running) +
                    "\nThey may overwrite restored files when they exit (P3D rewrites Prepar3D.cfg on exit). "
                    "Best to close them first.")
        if not confirm(self, f"Restore {len(todo)} file(s) to '{label}'?", f"Restore {len(todo)} file(s)",
                       title="Restore", details=f"{txt}{warn}\n\nA safety snapshot is taken first, so this can be "
                       "undone from History."):
            return
        report = self.repo.restore(snapshot_id, file_ids, label=label)
        failed = [(f, r) for f, r in report if r.startswith("FAILED")]
        self.sync_dirty = True
        self.refresh_all()
        if failed:
            fail(self, "restore every file", "\n".join(f"{f.display}: {r}" for f, r in failed),
                 "Is the file open in the sim, or is the app not running as administrator?")
        else:
            n = sum(1 for _, r in report if r == "restored")
            self.notify(f"Restored {n} file(s) to '{label}'")
