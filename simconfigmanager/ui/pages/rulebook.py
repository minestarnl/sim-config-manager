"""Rulebook: documented and locked settings."""
from __future__ import annotations


from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QTableWidget, QVBoxLayout, QWidget,
)

from ...theme import TLabel as QLabel  # theme-aware versions


from ..dialogs import RulebookDialog
from ..common import BAD, MUTED, OK, button_row, colored, confirm, hint, item, selected_data, setup_table

from . import Page


class RulebookPage:
    """Mixin for MainWindow — Rulebook page."""

    PAGE = Page("rulebook", "Rulebook", "rulebook", "Documented and locked settings", "build_rulebook_page", "refresh_rulebook")

    def build_rulebook_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(hint("Document why settings have their value. A <b>required value</b> locks a setting: you get "
                           "an alert when it changes, the editor warns before saving, and the AI assistants won't "
                           "propose changing it. Entries without a required value are notes the assistants read."))
        self.rb_summary = QLabel()
        self.rb_summary.setTextFormat(Qt.RichText)
        lay.addLayout(button_row(("Add entry…", lambda: self.edit_rulebook(None), {"primary": True}),
                                 ("Edit…", self.edit_rulebook_selected), ("Delete…", self.delete_rulebook),
                                 trailing=[self.rb_summary]))
        self.rb_table = QTableWidget()
        setup_table(self.rb_table, ["Status", "File", "Setting", "Required", "Current value", "Note", "By"], 5)
        self.rb_table.doubleClicked.connect(lambda _i: self.edit_rulebook_selected())
        lay.addWidget(self.rb_table, 1)
        return w

    def refresh_rulebook(self):
        rows = self.repo.rulebook_status()
        bad = sum(1 for r in rows if r["ok"] is False)
        locked = sum(1 for r in rows if r["ok"] is not None)
        self.rb_summary.setText(colored(f"✖ {bad} setting(s) break the rulebook", BAD, True) if bad else
                                (colored(f"✔ All {locked} locked settings match", OK) if locked else ""))
        self.rb_table.setRowCount(len(rows))
        for i, r in enumerate(sorted(rows, key=lambda r: (r["ok"] is not False, r["file"], r["selector"]))):
            st = {True: ("✔ Matches", OK), False: ("✖ Broken", BAD), None: ("ℹ Note", MUTED)}[r["ok"]]
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
        if ids and confirm(self, "Delete this rulebook entry?", "Delete", danger=True):
            self.repo.delete_rulebook_entry(ids[0])
            self.sync_dirty = True
            self.refresh_rulebook()
