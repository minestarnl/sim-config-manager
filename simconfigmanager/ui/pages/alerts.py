"""Alerts: alert rules, ignored settings and the event log."""
from __future__ import annotations



from PySide6.QtWidgets import (
    QInputDialog, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...core import (Rule, RULE_KINDS, fmt_ts)


from ..dialogs import RuleDialog
from ..common import BAD, MUTED, OK, WARN, button_row, confirm, heading, hint, item, selected_data, setup_table

from . import Page


class AlertsPage:
    """Mixin for MainWindow — Alerts page."""

    PAGE = Page("alerts", "Alerts", "alerts", "Alert rules, ignored settings and the event log", "build_alerts_page", "refresh_alerts")

    def build_alerts_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addLayout(button_row(heading("Alert rules"), ("Add rule…", lambda: self.add_rule(), {"primary": True}),
                                 ("Edit…", self.edit_rule), ("Turn on / off", self.toggle_rule),
                                 ("Delete…", self.delete_rule)))
        lay.addWidget(hint("A notification on the sim PC (and optionally an automatic undo) when a file changes "
                           "in a specific way."))
        self.rule_table = QTableWidget()
        setup_table(self.rule_table, ["On", "Name", "File", "When", "Pattern", "Expected", "Then"], 3)
        self.rule_table.doubleClicked.connect(lambda _i: self.edit_rule())
        lay.addWidget(self.rule_table, 2)
        lay.addLayout(button_row(heading("Ignored settings"), ("Add…", self.add_ignore_dialog),
                                 ("Remove…", self.remove_ignore)))
        lay.addWidget(hint("Values that change by themselves (e.g. Prepar3D rewrites window positions on exit). "
                           "A change to only these doesn't count as a change."))
        self.ign_table = QTableWidget()
        setup_table(self.ign_table, ["File", "Kind", "Pattern", "Why"], 3)
        lay.addWidget(self.ign_table, 1)
        lay.addLayout(button_row(heading("Event log"), ("Clear…", self.clear_log)))
        self.log_table = QTableWidget()
        setup_table(self.log_table, ["When", "Level", "Message"], 2)
        lay.addWidget(self.log_table, 3)
        return w

    def refresh_ignores(self):
        rows = self.repo.ignores()
        self.ign_table.setRowCount(len(rows))
        for r, x in enumerate(rows):
            f = self.repo.file(x["file_id"]) if x["file_id"] else None
            self.ign_table.setItem(r, 0, item(f.display if f else "(all files)", x["id"]))
            self.ign_table.setItem(r, 1, item(core.IGNORE_KINDS.get(x["kind"], x["kind"]).split(" (")[0]))
            self.ign_table.setItem(r, 2, item(x["pattern"]))
            self.ign_table.setItem(r, 3, item(x["note"] or ""))

    def add_ignore_dialog(self):
        files = self.repo.files()
        names = ["(all files)"] + [f"{f.display}  [{f.group}]" for f in files]
        choice, ok = QInputDialog.getItem(self, "Ignore a setting", "Which file?", names, 0, False)
        if not ok:
            return
        fid = None if choice == names[0] else files[names.index(choice) - 1].id
        kinds = list(core.IGNORE_KINDS.items())
        k, ok = QInputDialog.getItem(self, "Ignore a setting", "What kind of setting?", [v for _k, v in kinds], 0, False)
        if not ok:
            return
        kind = kinds[[v for _k, v in kinds].index(k)][0]
        pattern, ok = QInputDialog.getText(self, "Ignore a setting", f"Pattern — {k}:",
                                           text={"ini_key": "Section/Key", "ini_section": "Window.*",
                                                 "xml": ".//Window", "line": "^LastUsed="}[kind])
        if not ok or not pattern.strip():
            return
        note, _ = QInputDialog.getText(self, "Ignore a setting", "Why? (optional)")
        self.repo.add_ignore(fid, kind, pattern.strip(), note)
        self.sync_dirty = True
        self.refresh_alerts()

    def remove_ignore(self):
        ids = selected_data(self.ign_table)
        if not ids or not confirm(self, f"Stop ignoring {len(ids)} setting(s)?", "Stop ignoring",
                                  details="Changes to them count as changes again from now on."):
            return
        for i in ids:
            self.repo.delete_ignore(i)
        self.sync_dirty = True
        self.refresh_alerts()

    def refresh_alerts(self):
        self.refresh_ignores()
        rules = self.repo.rules()
        self.rule_table.setRowCount(len(rules))
        for r, ru in enumerate(rules):
            f = self.repo.file(ru.file_id) if ru.file_id else None
            self.rule_table.setItem(r, 0, item("✔" if ru.enabled else "–", ru.id,
                                               OK if ru.enabled else MUTED))
            self.rule_table.setItem(r, 1, item(ru.name))
            self.rule_table.setItem(r, 2, item(f.display if f else "(all files)"))
            self.rule_table.setItem(r, 3, item(RULE_KINDS.get(ru.kind, ru.kind)))
            self.rule_table.setItem(r, 4, item(ru.pattern))
            self.rule_table.setItem(r, 5, item(ru.expected))
            self.rule_table.setItem(r, 6, item("Notify + undo" if ru.action == "revert" else "Notify",
                                               color=BAD if ru.action == "revert" else None))
        ev = self.repo.events(500)
        self.log_table.setRowCount(len(ev))
        col = {"alert": BAD, "warning": WARN}
        for r, e in enumerate(ev):
            self.log_table.setItem(r, 0, item(fmt_ts(e["ts"])))
            self.log_table.setItem(r, 1, item(e["level"].capitalize(), color=col.get(e["level"])))
            self.log_table.setItem(r, 2, item(e["message"]))

    def selected_rule(self):
        ids = selected_data(self.rule_table)
        if not ids:
            return None
        return next((r for r in self.repo.rules() if r.id == ids[0]), None)

    def add_rule(self, f=None):
        r = Rule(0, "", f.id if f else None, "any_change")
        if RuleDialog(self.repo, r, self).exec():
            self.sync_dirty = True
            self.go("alerts")
            self.refresh_alerts()

    def edit_rule(self):
        r = self.selected_rule()
        if r and RuleDialog(self.repo, r, self).exec():
            self.sync_dirty = True
            self.refresh_alerts()

    def toggle_rule(self):
        r = self.selected_rule()
        if r:
            r.enabled = not r.enabled
            self.repo.save_rule(r)
            self.sync_dirty = True
            self.refresh_alerts()

    def delete_rule(self):
        r = self.selected_rule()
        if r and confirm(self, f"Delete the alert rule '{r.name}'?", "Delete", danger=True):
            self.repo.delete_rule(r.id)
            self.sync_dirty = True
            self.refresh_alerts()

    def clear_log(self):
        if confirm(self, "Clear the event log?", "Clear", danger=True,
                   details="Snapshots and history are not affected."):
            self.repo.clear_events()
            self.refresh_alerts()
