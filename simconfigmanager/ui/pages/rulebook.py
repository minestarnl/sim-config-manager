"""Rulebook: documented and locked settings."""
from __future__ import annotations


from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QMessageBox, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from ...theme import TLabel as QLabel  # theme-aware versions
from ...core import APP_NAME


from ..dialogs import RulebookDialog
from ..common import item, selected_data, setup_table

class RulebookPage:
    """Mixin for MainWindow — Rulebook page."""

    def build_rulebook_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lab = QLabel("<b>Rulebook</b> — document why settings have their value. A <b>required value</b> locks a "
                     "setting: you get an alert when it changes, the editor warns before saving, and Claude/ChatGPT "
                     "won't propose changing it. Notes without a required value are documentation that Claude reads.")
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.RichText)
        lay.addWidget(lab)
        row = QHBoxLayout()
        for text, fn in (("Add…", lambda: self.edit_rulebook(None)), ("Edit…", self.edit_rulebook_selected),
                         ("Delete", self.delete_rulebook)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        self.rb_summary = QLabel()
        self.rb_summary.setTextFormat(Qt.RichText)
        row.addWidget(self.rb_summary)
        lay.addLayout(row)
        self.rb_table = QTableWidget()
        setup_table(self.rb_table, ["Status", "File", "Setting", "Required", "Current value", "Note", "By"], 5)
        self.rb_table.doubleClicked.connect(lambda _i: self.edit_rulebook_selected())
        lay.addWidget(self.rb_table, 1)
        return w

    def refresh_rulebook(self):
        rows = self.repo.rulebook_status()
        bad = sum(1 for r in rows if r["ok"] is False)
        self.rb_summary.setText(f"<b style='color:#c62828'>{bad} setting(s) break the rulebook</b>" if bad else
                                (f"<span style='color:#2e7d32'>✔ all {sum(1 for r in rows if r['ok'])} locked "
                                 "settings OK</span>" if rows else ""))
        self.rb_table.setRowCount(len(rows))
        for i, r in enumerate(sorted(rows, key=lambda r: (r["ok"] is not False, r["file"], r["selector"]))):
            st = {True: ("✔ OK", "#2e7d32"), False: ("✖ Violated", "#c62828"), None: ("ℹ note", "#555")}[r["ok"]]
            self.rb_table.setItem(i, 0, item(st[0], r["id"], st[1], bold=r["ok"] is False))
            self.rb_table.setItem(i, 1, item(r["file"] or "(general)"))
            self.rb_table.setItem(i, 2, item(r["selector"]))
            self.rb_table.setItem(i, 3, item(r["required"] or "–"))
            self.rb_table.setItem(i, 4, item("–" if r["current"] is None else r["current"]))
            self.rb_table.setItem(i, 5, item(r["note"] or ""))
            self.rb_table.setItem(i, 6, item(r["author"] or ""))

    def edit_rulebook_selected(self):
        ids = selected_data(self.rb_table)
        if ids:
            entry = next((e for e in self.repo.rulebook() if e["id"] == ids[0]), None)
            self.edit_rulebook(entry)

    def edit_rulebook(self, entry: Optional[dict]):
        dlg = RulebookDialog(self.repo, entry, self)
        if dlg.exec() == QDialog.Accepted:
            self.sync_dirty = True
            self.refresh_rulebook()

    def delete_rulebook(self):
        ids = selected_data(self.rb_table)
        if ids and QMessageBox.question(self, APP_NAME, "Delete this rulebook entry?") == QMessageBox.Yes:
            self.repo.delete_rulebook_entry(ids[0])
            self.sync_dirty = True
            self.refresh_rulebook()
