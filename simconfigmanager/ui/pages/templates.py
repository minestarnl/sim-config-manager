"""Templates: one config shared by all sims, with per-sim variables and a staged rollout."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QPlainTextEdit, QSplitter, QTableWidget,
    QTextEdit, QVBoxLayout, QWidget,
)

from ... import core, theme
from ...core import decode_text, fmt_ts
from ...theme import TLabel as QLabel
from ..common import (MUTED, OK, WARN, button_row, colored, confirm, diff_html, fail, heading, hint, inform, item,
                      selected_data, setup_table)
from . import Page


class TemplateDialog(QDialog):
    def __init__(self, parent, t: dict):
        super().__init__(parent)
        self.t = dict(t)
        self.setWindowTitle("Template")
        self.resize(900, 680)
        lay = QVBoxLayout(self)
        lay.addWidget(hint("Write <code>{{name}}</code> where a value differs per sim (e.g. "
                           "<code>Resolution={{width}}x{{height}}</code>). Saving a changed content makes a new "
                           "revision and starts the rollout again at stage 1."))
        form = QFormLayout()
        self.name = QLineEdit(t.get("name", ""))
        self.name.setPlaceholderText("e.g. 'Prepar3D.cfg — display settings'")
        form.addRow("Name:", self.name)
        self.stages = QLineEdit(" | ".join(", ".join(s) for s in t.get("stages") or []))
        self.stages.setPlaceholderText("Sim 1 | Sim 2, Sim 3      (stages separated by |, sims by commas)")
        form.addRow("Rollout stages:", self.stages)
        self.notes = QLineEdit(t.get("notes", ""))
        form.addRow("What changed:", self.notes)
        lay.addLayout(form)
        self.content = QPlainTextEdit(t.get("content", ""))
        self.content.setFont(theme.mono_font(10))
        lay.addWidget(self.content, 1)
        self.vars = QLabel()
        lay.addWidget(self.vars)
        self.content.textChanged.connect(self.update_vars)
        self.update_vars()
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def update_vars(self):
        v = core.variables_in(self.content.toPlainText())
        self.vars.setText("Variables: " + (", ".join(f"<b>{html.escape(x)}</b>" for x in v) if v else "none"))

    def value(self) -> dict:
        stages = [[s.strip() for s in part.split(",") if s.strip()] for part in self.stages.text().split("|")]
        self.t.update(name=self.name.text().strip(), content=self.content.toPlainText(),
                      stages=[s for s in stages if s], notes=self.notes.text().strip())
        return self.t


class TemplatesPage:
    """Mixin for MainWindow — Templates page."""

    PAGE = Page("templates", "Templates", "layers", "One config for all sims, per-sim variables, staged rollout",
                "build_templates_page", "refresh_templates")

    def build_templates_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(hint("Templates live in the shared location so every sim sees them. Each sim fills in its own "
                           "values and chooses which tracked file the template writes. A new revision reaches the "
                           "sims stage by stage, and is only written after someone clicks <b>Apply</b> on that sim."))
        lay.addLayout(button_row(("New from a tracked file…", self.new_template, {"primary": True}),
                                 ("Edit…", self.edit_template), ("Set up for this sim…", self.bind_template),
                                 ("Preview and apply…", self.apply_template_dialog),
                                 trailing=[("Open next stage…", self.advance_template), ("Delete…", self.delete_tpl)]))
        split = QSplitter(Qt.Vertical)
        self.tpl_table = QTableWidget()
        setup_table(self.tpl_table, ["Template", "Revision", "This sim", "Writes to", "Rollout", "Updated"], 4)
        self.tpl_table.itemSelectionChanged.connect(self.show_template_detail)
        self.tpl_table.doubleClicked.connect(lambda _i: self.apply_template_dialog())
        split.addWidget(self.tpl_table)
        self.tpl_detail = QTextEdit()
        self.tpl_detail.setReadOnly(True)
        split.addWidget(self.tpl_detail)
        split.setSizes([300, 260])
        lay.addWidget(split, 1)
        self._templates = []
        return w

    def refresh_templates(self):
        def done(tpls):
            self._templates = tpls
            self.fill_templates()

        def failed(e):
            self._templates = []
            self.fill_templates()
            self.tpl_detail.setHtml(f"<p>{html.escape(str(e))}</p>")
        self.run_bg(lambda r: r.list_templates(), done, failed)

    def fill_templates(self):
        files = {f.id: f.display for f in self.repo.files()}
        self.tpl_table.setRowCount(len(self._templates))
        waiting = 0
        for r, t in enumerate(self._templates):
            if t["update_ready"]:
                me, col = f"Update ready: r{t['rev']}", WARN
                waiting += 1
            elif t["applied_rev"] == t["rev"]:
                me, col = f"✔ Runs r{t['rev']}", OK
            elif t["my_stage"] is None:
                me, col = "Not in the rollout", MUTED
            elif not t["eligible"]:
                me, col = f"Waiting for stage {t['my_stage'] + 1}", MUTED
            else:
                me, col = "Not applied", None
            n_done = sum(1 for s in t["status"].values() if s.get("rev") == t["rev"])
            n_all = sum(len(s) for s in t.get("stages") or [])
            self.tpl_table.setItem(r, 0, item(t["name"], t["id"], bold=True))
            self.tpl_table.setItem(r, 1, item(f"r{t['rev']}"))
            self.tpl_table.setItem(r, 2, item(me, color=col, bold=t["update_ready"]))
            self.tpl_table.setItem(r, 3, item(files.get(t["target_file"], "— choose (Set up for this sim)")))
            self.tpl_table.setItem(r, 4, item(f"stage {t.get('open_stage', 0) + 1} of {len(t.get('stages') or [])} "
                                              f"open · {n_done}/{n_all} sims on r{t['rev']}"))
            self.tpl_table.setItem(r, 5, item(f"{fmt_ts(t.get('updated', 0))} by {t.get('author', '')}"))
        self.set_badge("templates", waiting)

    def selected_template(self):
        ids = selected_data(self.tpl_table)
        return next((t for t in self._templates if ids and t["id"] == ids[0]), None)

    def show_template_detail(self):
        t = self.selected_template()
        if not t:
            self.tpl_detail.clear()
            return
        stages = []
        for i, st in enumerate(t.get("stages") or []):
            open_ = i <= t.get("open_stage", 0)
            sims = ", ".join(
                (colored(f"✔ {m}", OK) if t["status"].get(m, {}).get("rev") == t["rev"] else html.escape(m))
                for m in st)
            stages.append(f"<li>Stage {i + 1} {'(open)' if open_ else '(waiting)'}: {sims}</li>")
        vals = ", ".join(f"{html.escape(k)} = <b>{html.escape(str(v))}</b>" for k, v in t["values"].items()) or "none set"
        self.tpl_detail.setHtml(
            f"<h3 style='margin:0'>{html.escape(t['name'])} — r{t['rev']}</h3>"
            f"<p style='color:{MUTED}'>{html.escape(t.get('notes') or '')}</p>"
            f"<p><b>Rollout</b></p><ul>{''.join(stages)}</ul>"
            f"<p><b>Variables:</b> {', '.join(t.get('variables') or []) or 'none'}<br>"
            f"<b>Values on this sim:</b> {vals}</p>")

    # ---------------------------------------------------------------- actions
    def new_template(self):
        files = [f for f in self.repo.files() if not core.is_probe(f)]
        if not files:
            inform(self, "Track a config file first.")
            return
        names = [f"{f.display}  [{f.group}]" for f in files]
        from PySide6.QtWidgets import QInputDialog
        choice, ok = QInputDialog.getItem(self, "New template", "Start from this file (as it is on this sim):",
                                          names, 0, False)
        if not ok:
            return
        f = files[names.index(choice)]
        data = self.repo.read_current(f)
        t = {"name": f.display, "content": decode_text(data) if data else "",
             "stages": [[self.repo.machine]], "target_name": f.display}
        dlg = TemplateDialog(self, t)
        if dlg.exec() != QDialog.Accepted:
            return
        self._save_template(dlg.value(), bind_file=f.id)

    def _save_template(self, t: dict, bind_file=None):
        def done(saved):
            if bind_file is not None:
                self.repo.set_template_binding(saved["id"], bind_file, {})
            self.notify(f"Template '{saved['name']}' saved as r{saved['rev']}")
            self.refresh_templates()
        self.run_bg(lambda r: r.save_template(t), done, lambda e: fail(self, "save the template", e), write=True)

    def edit_template(self):
        t = self.selected_template()
        if not t:
            return
        dlg = TemplateDialog(self, t)
        if dlg.exec() == QDialog.Accepted:
            self._save_template(dlg.value())

    def bind_template(self):
        t = self.selected_template()
        if not t:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"{t['name']} on this sim")
        form = QFormLayout(dlg)
        target = QComboBox()
        for f in self.repo.files():
            if not core.is_probe(f):
                target.addItem(f"{f.display}  [{f.group}]", f.id)
        target.setCurrentIndex(max(0, target.findData(t["target_file"])))
        form.addRow("Writes to:", target)
        edits = {}
        for v in t.get("variables") or []:
            edits[v] = QLineEdit(str(t["values"].get(v, "")))
            form.addRow(f"{v}:", edits[v])
        if not edits:
            form.addRow(hint("This template has no variables."))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        self.repo.set_template_binding(t["id"], target.currentData(), {k: e.text().strip() for k, e in edits.items()})
        self.refresh_templates()

    def apply_template_dialog(self):
        t = self.selected_template()
        if not t:
            return
        if not t["eligible"]:
            inform(self, "This sim's rollout stage isn't open yet." if t["my_stage"] is not None else
                   "This sim is not in the template's rollout stages (Edit… to add it).")
            return
        try:
            p = self.repo.preview_template(t["id"])
        except Exception as e:  # noqa: BLE001
            fail(self, "prepare the template", e, "Use 'Set up for this sim…' to choose the file and fill in the "
                 "values.")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle(f"Apply {t['name']} r{t['rev']} to {p['file'].display}")
        dlg.resize(980, 640)
        lay = QVBoxLayout(dlg)
        lay.addWidget(heading("Changes to the file on this sim"))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(theme.mono_font(10))
        view.setHtml(diff_html(core.unified_diff(p["current"].encode(), p["new"].encode(), "on disk now",
                                                 f"template r{t['rev']}")))
        lay.addWidget(view, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Apply")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            self.repo.apply_template(t["id"])
        except Exception as e:  # noqa: BLE001
            fail(self, "apply the template", e)
            return
        self.sync_dirty = True
        self.notify(f"Template '{t['name']}' r{t['rev']} applied")
        self.refresh_all()

    def advance_template(self):
        t = self.selected_template()
        if not t:
            return
        nxt = t.get("open_stage", 0) + 1
        if nxt >= len(t.get("stages") or []):
            inform(self, "All stages are already open.")
            return
        if confirm(self, f"Open stage {nxt + 1} of '{t['name']}' r{t['rev']}?", "Open stage",
                   details="Those sims are told an update is ready; it is applied on each sim by someone there."):
            self.run_bg(lambda r: r.advance_rollout(t["id"]), lambda _t: self.refresh_templates(),
                        lambda e: fail(self, "open the next stage", e), write=True)

    def delete_tpl(self):
        t = self.selected_template()
        if t and confirm(self, f"Delete the template '{t['name']}'?", "Delete", danger=True,
                         details="Files on the sims are not changed."):
            self.run_bg(lambda r: r.delete_template(t["id"]), lambda _x: self.refresh_templates(),
                        lambda e: fail(self, "delete the template", e), write=True)


__all__ = ["TemplatesPage", "TemplateDialog"]
