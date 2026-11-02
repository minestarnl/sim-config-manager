"""Dialogs used by several pages."""
from __future__ import annotations


import html
import json
import os
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QHBoxLayout, QInputDialog, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import core, net, theme
from ..theme import TLabel as QLabel, TTextEdit as QTextEdit  # theme-aware versions
from ..core import (Repo, Rule, RULE_HELP, RULE_KINDS, fmt_ts)


from .common import (BAD, INFO, MUTED, OK, WARN, banner, button, button_row, colored, diff_html, fail, heading,
                     hint, inform, item, setup_table, warn)

class RemoteFileDialog(QDialog):
    """Browse the sim PC's disk from the remote app (folders + file names only)."""

    def __init__(self, repo, title: str, start: str = "", pick_folder: bool = False, multi: bool = False,
                 parent=None):
        super().__init__(parent)
        self.repo, self.pick_folder, self.multi = repo, pick_folder, multi
        self.setWindowTitle(title + " — on the sim PC")
        self.resize(720, 520)
        lay = QVBoxLayout(self)
        row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.returnPressed.connect(lambda: self.go(self.path.text()))
        row.addWidget(self.path, 1)
        b = QPushButton("Up")
        b.clicked.connect(self.up)
        row.addWidget(b)
        self.shortcuts = QComboBox()
        self.shortcuts.addItem("Go to…")
        self.shortcuts.activated.connect(self.jump)
        row.addWidget(self.shortcuts)
        lay.addLayout(row)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection if multi else QAbstractItemView.SingleSelection)
        self.list.itemDoubleClicked.connect(self.open_item)
        lay.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Use this folder" if pick_folder else "Choose")
        bb.accepted.connect(self.accept_sel)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.cur = None
        self.selected: list[str] = []
        self.go(start)

    def go(self, path: str):
        try:
            d = self.repo.list_dir(path)
        except Exception as e:  # noqa: BLE001
            fail(self, "open that folder on the sim", e)
            return
        self.cur = d
        self.path.setText(d["path"])
        self.list.clear()
        sep = d.get("sep", "\\")
        for name in d["dirs"]:
            it = QListWidgetItem("📁 " + name)
            it.setData(Qt.UserRole, ("dir", name if not d["path"] else d["path"].rstrip("\\/") + sep + name))
            self.list.addItem(it)
        if not self.pick_folder:
            for name, size in d["files"]:
                it = QListWidgetItem(f"📄 {name}   ({size:,} bytes)")
                it.setData(Qt.UserRole, ("file", d["path"].rstrip("\\/") + sep + name))
                self.list.addItem(it)
        self.shortcuts.clear()
        self.shortcuts.addItem("Go to…")
        self.shortcuts.addItem("Drives")
        for k, v in sorted(d.get("shortcuts", {}).items()):
            self.shortcuts.addItem(f"%{k}%  {v}", v)

    def jump(self, idx: int):
        if idx == 1:
            self.go("")
        elif idx > 1:
            self.go(self.shortcuts.itemData(idx))

    def up(self):
        if self.cur and self.cur.get("parent") is not None:
            self.go(self.cur["parent"])

    def open_item(self, it):
        kind, path = it.data(Qt.UserRole)
        if kind == "dir":
            self.go(path)
        else:
            self.selected = [path]
            self.accept()

    def accept_sel(self):
        if self.pick_folder:
            if not self.cur or not self.cur["path"]:
                return
            self.selected = [self.cur["path"]]
        else:
            self.selected = [it.data(Qt.UserRole)[1] for it in self.list.selectedItems()
                             if it.data(Qt.UserRole)[0] == "file"]
            if not self.selected:
                return
        self.accept()


def pick_files(parent, repo, title: str, multi: bool = True) -> list[str]:
    if core.is_remote(repo):
        dlg = RemoteFileDialog(repo, title, "", multi=multi, parent=parent)
        return dlg.selected if dlg.exec() == QDialog.Accepted else []
    if multi:
        return QFileDialog.getOpenFileNames(parent, title)[0]
    p = QFileDialog.getOpenFileName(parent, title)[0]
    return [p] if p else []


def pick_folder(parent, repo, title: str, start: str = "") -> str:
    if core.is_remote(repo):
        dlg = RemoteFileDialog(repo, title, start, pick_folder=True, parent=parent)
        return dlg.selected[0] if dlg.exec() == QDialog.Accepted and dlg.selected else ""
    return QFileDialog.getExistingDirectory(parent, title, start)


class DiffDialog(QDialog):
    def __init__(self, title: str, diff_text: str, parent=None, summary: Optional[list] = None, ignore_cb=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 680)
        lay = QVBoxLayout(self)
        if summary:
            lay.addWidget(heading(f"Setting changes ({len(summary)})"))
            self.sum_list = QListWidget()
            self.sum_list.setMaximumHeight(170)
            colors = {"added": "#1b5e20", "removed": "#b71c1c", "changed": "#e65100"}
            for c in summary:
                it = QListWidgetItem(core.describe_change(c))
                it.setData(Qt.UserRole, c)
                it.setForeground(QBrush(QColor(theme.c(colors.get(c["kind"], "#000")))))
                self.sum_list.addItem(it)
            lay.addWidget(self.sum_list)
            if ignore_cb:
                lay.addLayout(button_row(button(
                    "Ignore the selected setting from now on…",
                    lambda: [ignore_cb(it.data(Qt.UserRole)) for it in self.sum_list.selectedItems()[:1]],
                    tip="For values the sim rewrites by itself (e.g. window positions): changes to them no longer "
                        "count as changes or trigger alerts.")))
            lay.addWidget(heading("Line by line"))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(theme.mono_font(10))
        view.setHtml(diff_html(diff_text))
        lay.addWidget(view)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class RuleDialog(QDialog):
    def __init__(self, repo: Repo, rule: Optional[Rule] = None, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.rule = rule or Rule(0, "", None, "any_change")
        self.setWindowTitle("Alert rule")
        self.resize(560, 320)
        form = QFormLayout(self)
        self.name = QLineEdit(self.rule.name)
        self.file = QComboBox()
        self.file.addItem("(all tracked files)", None)
        for f in repo.files():
            self.file.addItem(f"{f.display}  [{f.group}]  {f.path}", f.id)
        idx = self.file.findData(self.rule.file_id)
        self.file.setCurrentIndex(max(idx, 0))
        self.kind = QComboBox()
        for k, label in RULE_KINDS.items():
            self.kind.addItem(label, k)
        self.kind.setCurrentIndex(max(self.kind.findData(self.rule.kind), 0))
        self.pattern = QLineEdit(self.rule.pattern)
        self.rp_combo = QComboBox()
        self.rp_combo.addItems([rp.name for rp in repo.restore_points()])
        self.expected = QLineEdit(self.rule.expected)
        self.action = QComboBox()
        self.action.addItem("Notify", "notify")
        self.action.addItem("Notify and undo the change automatically", "revert")
        self.action.setCurrentIndex(max(self.action.findData(self.rule.action), 0))
        self.enabled = QCheckBox("On")
        self.enabled.setChecked(self.rule.enabled)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setProperty("hint", True)
        form.addRow("Name:", self.name)
        form.addRow("File:", self.file)
        form.addRow("When:", self.kind)
        form.addRow("Pattern:", self.pattern)
        form.addRow("Restore point:", self.rp_combo)
        form.addRow("Expected value:", self.expected)
        form.addRow("Then:", self.action)
        form.addRow("", self.enabled)
        form.addRow("", self.help)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept_rule)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.kind.currentIndexChanged.connect(self.update_fields)
        if self.rule.kind == "differs_from_rp":
            self.rp_combo.setCurrentText(self.rule.pattern)
        self.update_fields()

    def update_fields(self):
        k = self.kind.currentData()
        self.help.setText(RULE_HELP.get(k, ""))
        self.pattern.setEnabled(k in ("line_added", "line_removed", "ini_value", "xml_value"))
        self.expected.setEnabled(k in ("ini_value", "xml_value"))
        self.rp_combo.setEnabled(k == "differs_from_rp")

    def accept_rule(self):
        k = self.kind.currentData()
        if k in ("line_added", "line_removed", "ini_value", "xml_value") and not self.pattern.text().strip():
            warn(self, "This kind of rule needs a pattern.")
            return
        if k == "differs_from_rp" and not self.rp_combo.currentText():
            warn(self, "Save a restore point first (Restore points page).")
            return
        r = self.rule
        r.name = self.name.text().strip() or self.kind.currentText()
        r.file_id = self.file.currentData()
        r.kind = k
        r.pattern = self.rp_combo.currentText() if k == "differs_from_rp" else self.pattern.text().strip()
        r.expected = self.expected.text().strip() if self.expected.isEnabled() else ""
        r.action = self.action.currentData()
        r.enabled = self.enabled.isChecked()
        self.repo.save_rule(r)
        self.accept()


class DiscoverDialog(QDialog):
    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Prepar3D / ProSim config files found")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(hint("Tick the files to track. Group names can be changed later (e.g. 'Sim 1 – P3D v5')."))
        self.list = QListWidget()
        for label, group, path in candidates:
            it = QListWidgetItem(f"[{group}]  {label}    —  {path}")
            it.setData(Qt.UserRole, (label, group, path))
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if group != "ProSim" or path.lower().endswith("config.xml")
                             else Qt.Unchecked)
            self.list.addItem(it)
        lay.addWidget(self.list)
        row = QHBoxLayout()
        row.addWidget(QLabel("Prefix group with:"))
        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("e.g. Sim 1")
        row.addWidget(self.prefix)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self):
        pre = self.prefix.text().strip()
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                label, group, path = it.data(Qt.UserRole)
                out.append((label, f"{pre} – {group}" if pre else group, path))
        return out


class ExportArchiveDialog(QDialog):
    def __init__(self, repo: Repo, name: str, current: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export certification archive")
        self.resize(640, 260)
        form = QFormLayout(self)
        what = "every tracked file as it is now" if current else f"the files in '{name}'"
        form.addRow(hint(f"Creates one zip with {what}, a REPORT.txt with paths, dates and SHA-256 checksums "
                         "(for certification), and a .sha256 file that proves the zip was not changed later."))
        stamp = time.strftime("%Y-%m-%d_%H%M")
        start_dir = (parent.pref("last_archive_dir") if parent is not None and hasattr(parent, "pref")
                     else repo.get("last_archive_dir"))
        default = os.path.join(start_dir or str(Path.home() / "Documents"),
                               f"{core.safe_name(repo.machine)}_{core.safe_name(name) + '_' if name else ''}"
                               f"configs_{stamp}.zip")
        row = QHBoxLayout()
        self.path = QLineEdit(default)
        row.addWidget(self.path)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse)
        row.addWidget(b)
        holder = QWidget()
        holder.setLayout(row)
        form.addRow("Save to:", holder)
        self.note = QLineEdit()
        self.note.setPlaceholderText("e.g. 'Annual certification 2026 – inspector J. Smith'")
        form.addRow("Note (in the report):", self.note)
        self.hist = QCheckBox("Include the full history, versions, restore points and rules "
                              "(lets you rebuild everything on another PC)")
        self.hist.setChecked(True)
        form.addRow("", self.hist)
        self.upload = QCheckBox("Also upload to the shared location (archives folder)")
        has_sync = (repo.get("sync_mode") or ("folder" if repo.get("sync_folder") else "")) not in ("", "off")
        self.upload.setEnabled(has_sync)
        self.upload.setChecked(has_sync and repo.get("archive_upload") != "0")
        form.addRow("", self.upload)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Export")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def browse(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save archive", self.path.text(), "Zip (*.zip)")
        if p:
            self.path.setText(p if p.lower().endswith(".zip") else p + ".zip")

    def result_values(self):
        p = self.path.text().strip()
        if not p.lower().endswith(".zip"):
            p += ".zip"
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        return p, self.note.text().strip(), self.hist.isChecked()


class ImportArchiveDialog(QDialog):
    def __init__(self, repo: Repo, arc: "core.Archive", parent=None):
        super().__init__(parent)
        self.repo, self.arc = repo, arc
        idx = arc.index
        self.setWindowTitle("Import certification archive")
        self.resize(1100, 600)
        lay = QVBoxLayout(self)
        v = arc.verify()
        bad = colored(f"{len(v['bad'])} damaged: {', '.join(v['bad'])}", BAD, True) if v["bad"] \
            else colored("all files OK", OK)
        zc = v["zip_check"]
        zcol = OK if zc == "matches" else (BAD if "NOT" in zc else MUTED)
        lay.addWidget(QLabel(
            f"<b>{html.escape(os.path.basename(arc.path))}</b><br>"
            f"From <b>{html.escape(idx.get('machine', '?'))}</b>, created {fmt_ts(idx.get('created', 0))}"
            f"{' — ' + html.escape(idx['note']) if idx.get('note') else ''}<br>"
            f"Checksums: {v['ok']} verified, {bad} · zip checksum: <span style='color:{zcol}'>{zc}</span>"))
        lay.addWidget(hint("Check the target paths (double-click a target to change it). User-folder paths were "
                           "translated to this PC automatically."))
        self.table = QTableWidget()
        setup_table(self.table, ["Import", "Name", "Group", "Original path", "Target on this sim"], 4)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        files = [e for e in arc.files if e.get("zip")]
        remote = core.is_remote(repo)
        remote_targets = repo.expand_portable([e.get("portable_path") or e["path"] for e in files]) if remote else []
        self.table.setRowCount(len(files))
        for r, e in enumerate(files):
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked if e["label"] in v["bad"] else Qt.Checked)
            chk.setData(Qt.UserRole, e)
            self.table.setItem(r, 0, chk)
            self.table.setItem(r, 1, item(e["label"]))
            self.table.setItem(r, 2, item(e["group"]))
            self.table.setItem(r, 3, item(e["path"]))
            tgt = remote_targets[r] if remote else arc.suggested_target(e)
            exists = True if remote else os.path.exists(tgt)
            self.table.setItem(r, 4, item(tgt, color=None if exists else INFO))
        self.table.cellDoubleClicked.connect(self.edit_target)
        lay.addWidget(self.table)
        self.write = QCheckBox("Write the files to the target paths (overwrites them; a safety copy is recorded first)")
        self.write.setChecked(True)
        self.track = QCheckBox("Track these files on this sim")
        self.track.setChecked(True)
        self.hist = QCheckBox("Also import the full history, versions, restore points and rules")
        empty = not repo.snapshots(1)
        self.hist.setEnabled(arc.has_history and empty)
        self.hist.setChecked(arc.has_history and empty)
        if not arc.has_history:
            self.hist.setToolTip("This archive was exported without history.")
        elif not empty:
            self.hist.setText(self.hist.text() + "  (only possible on a fresh install)")
        for w in (self.write, self.track, self.hist):
            lay.addWidget(w)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Import")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def edit_target(self, row, col):
        if col != 4:
            return
        cur = self.table.item(row, 4).text()
        if core.is_remote(self.repo):
            p, ok = QInputDialog.getText(self, "Target path on the sim", "Path:", text=cur)
            if ok and p.strip():
                self.table.setItem(row, 4, item(p.strip()))
            return
        p, _ = QFileDialog.getSaveFileName(self, "Target path", cur)
        if p:
            self.table.setItem(row, 4, item(os.path.normpath(p)))

    def choices(self):
        targets = {}
        for r in range(self.table.rowCount()):
            chk = self.table.item(r, 0)
            if chk.checkState() == Qt.Checked:
                targets[chk.data(Qt.UserRole)["id"]] = self.table.item(r, 4).text()
        return targets, self.write.isChecked(), self.track.isChecked(), self.hist.isChecked()


class CreateVersionDialog(QDialog):
    def __init__(self, repo: Repo, parent=None):
        super().__init__(parent)
        self.repo = repo
        latest = repo.latest_version()
        number = (latest.number + 1) if latest else 1
        self.setWindowTitle(f"Create version v{number}")
        self.resize(760, 600)
        lay = QVBoxLayout(self)
        mism = repo.mismatches()
        if latest is None:
            intro = (f"This records <b>all {len(repo.files())} tracked files</b> as they are now as "
                     f"<b>v{number}</b>. Versions are permanent and cannot be changed afterwards.")
        elif mism:
            intro = (f"<b>{len(mism)} file(s) changed since {html.escape(latest.label)}.</b> "
                     f"v{number} will record all {len(repo.files())} tracked files as they are now.")
        else:
            intro = (colored(f"Nothing changed since {latest.label}.", WARN) + " "
                     "You can still create a version (e.g. to mark an inspection).")
        lab = QLabel(intro)
        lab.setWordWrap(True)
        lay.addWidget(lab)
        if mism:
            lst = QListWidget()
            lst.setMaximumHeight(150)
            for m in mism:
                lst.addItem(f"{core.STATUS_TEXT[m.status]}:  {m.file.display}   [{m.file.group}]")
            lay.addWidget(lst)
        running = repo.running_sim_processes()
        if running:
            lay.addWidget(banner("⚠ Running now: " + html.escape(", ".join(running)) + ". Prepar3D rewrites "
                                 "Prepar3D.cfg when it exits — it is safer to create the version with the sim closed."))
        form = QFormLayout()
        self.title = QLineEdit()
        self.title.setPlaceholderText("e.g. 'ProSim 3.30 update + new display settings'")
        form.addRow("Title:", self.title)
        self.by = QLineEdit(repo.get("version_author") or os.environ.get("USERNAME", ""))
        form.addRow("Created by:", self.by)
        lay.addLayout(form)
        lay.addWidget(QLabel("Release notes (pre-filled with what changed — edit freely):"))
        self.notes = QTextEdit()
        self.notes.setAcceptRichText(False)
        self.notes.setPlainText(repo.draft_release_notes())
        lay.addWidget(self.notes)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText(f"Create v{number}")
        bb.accepted.connect(self.check)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def check(self):
        if not self.by.text().strip():
            warn(self, "Fill in who is creating this version.")
            return
        self.accept()

    def values(self):
        return self.title.text().strip(), self.notes.toPlainText().strip(), self.by.text().strip()


class RulebookDialog(QDialog):
    def __init__(self, repo: Repo, entry: Optional[dict] = None, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.entry = dict(entry or {})
        self.setWindowTitle("Rulebook entry")
        self.resize(640, 360)
        form = QFormLayout(self)
        self.file = QComboBox()
        self.file.addItem("(general note — no file)", None)
        for f in repo.files():
            if not core.is_probe(f):
                self.file.addItem(f"{f.display}  [{f.group}]", f.id)
        self.file.setCurrentIndex(max(0, self.file.findData(self.entry.get("file_id"))))
        self.setting = QComboBox()
        self.setting.setEditable(True)
        self.file.currentIndexChanged.connect(self.fill_settings)
        self.current = QLabel()
        self.current.setProperty("hint", True)
        self.setting.currentTextChanged.connect(self.show_current)
        self.required = QLineEdit(self.entry.get("required", ""))
        self.required.setPlaceholderText("leave empty for a note only")
        self.note = QTextEdit()
        self.note.setAcceptRichText(False)
        self.note.setPlainText(self.entry.get("note", ""))
        self.note.setMaximumHeight(90)
        self.author = QLineEdit(self.entry.get("author") or repo.get("version_author") or os.environ.get("USERNAME", ""))
        form.addRow("File:", self.file)
        form.addRow("Setting:", self.setting)
        form.addRow("", self.current)
        form.addRow("Required value:", self.required)
        form.addRow("Why:", self.note)
        form.addRow("By:", self.author)
        form.addRow("", hint("CFG/INI: <i>Section/Key</i> (e.g. GRAPHICS/TEXTURE_MAX_LOAD). XML: element path, "
                             "optionally @attribute (e.g. .//Server@ip)."))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.save)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.fill_settings()
        if self.entry.get("selector"):
            self.setting.setEditText(self.entry["selector"])
        self.show_current()

    def fill_settings(self, *_):
        fid = self.file.currentData()
        self.setting.blockSignals(True)
        cur = self.setting.currentText()
        self.setting.clear()
        f = self.repo.file(fid) if fid else None
        if f:
            vals = core.setting_values(self.repo.read_current(f), f.path) or {}
            for k in sorted(vals):
                if core.kind_for_path(f.path) == "xml":
                    path, _, attr = k.partition("@")
                    parts = path.split("/")[1:]
                    sel = (".//" + "/".join(parts) if parts else ".") + (f"@{attr}" if attr else "")
                else:
                    sel = k
                self.setting.addItem(sel)
        self.setting.setEditText(cur)
        self.setting.blockSignals(False)
        self.show_current()

    def show_current(self, *_):
        fid = self.file.currentData()
        f = self.repo.file(fid) if fid else None
        if not f or not self.setting.currentText().strip():
            self.current.setText("")
            return
        v = core.rule_value(self.repo.read_current(f), f.path, self.setting.currentText().strip())
        self.current.setText(f"Current value: {v!r}" if v is not None else "Not found in the current file.")
        if v is not None and not self.required.text() and not self.entry.get("id"):
            self.required.setPlaceholderText(f"e.g. {v}   (leave empty for a note only)")

    def save(self):
        sel = self.setting.currentText().strip()
        if not sel:
            warn(self, "Fill in the setting.")
            return
        self.entry.update(file_id=self.file.currentData(), selector=sel, required=self.required.text().strip(),
                          note=self.note.toPlainText().strip(), author=self.author.text().strip())
        self.repo.save_rulebook_entry(self.entry)
        self.accept()


class NetSetupDialog(QDialog):
    """Shows the Claude Desktop config for a laptop, ready to copy."""

    def __init__(self, sim_name: str, key: dict, urls: list[str], kind: str, parent=None):
        super().__init__(parent)
        self.sim_name, self.key, self.kind = sim_name, key, kind
        self.setWindowTitle(f"Setup for {key['name']} — " + {"mac": "Claude Desktop on a Mac",
                            "windows": "Claude Desktop on a Windows laptop", "chatgpt": "ChatGPT through a Mac",
                            "app": "remote app"}[kind])
        self.resize(820, 600)
        lay = QVBoxLayout(self)
        if kind == "mac":
            steps = ("<ol><li>On the Mac, install <b>Claude Desktop</b> (claude.ai/download) and sign in.</li>"
                     "<li>Claude menu → <b>Settings… → Developer → Edit Config</b>. This opens "
                     "<i>claude_desktop_config.json</i> in Finder — open it with TextEdit.</li>"
                     "<li>Replace its contents with the text below (if it already has <i>mcpServers</i>, add just "
                     "the inner entry). Save.</li>"
                     "<li>Quit Claude Desktop (⌘Q) and open it again. In a chat, the tools menu now shows "
                     f"<b>{html.escape(net.server_key_name(sim_name))}</b>.</li></ol>"
                     "Nothing needs to be installed on the Mac — it uses the built-in <i>bash</i> and <i>curl</i>.")
        elif kind == "app":
            steps = ("Lets this person run <b>Sim Config Manager on their own PC or Mac</b> and work on this sim over "
                     "the network — edit files, restore, create versions, review proposed changes — as if sitting "
                     "here. Everything is logged with their name." +
                     ("" if key.get("full_control") else "<br>" + colored("This person's key is not 'Full control'. "
                                                                        "Remove and add them again with Full control.",
                                                                        BAD, True)) +
                     "<ol><li><b>Windows:</b> install with the normal installer and start <i>Sim Config Manager – "
                     "connect to a sim</i>. <b>Mac:</b> download <i>SimConfigManager-mac.zip</i> from the release, "
                     "unzip, move to Applications, then right-click → Open the first time.</li>"
                     "<li>Save the connection file below and send it privately. In the app: <b>Add from file…</b> "
                     "(or paste the address and key by hand).</li>"
                     "<li>Make sure network access is switched on here.</li></ol>")
        elif kind == "chatgpt":
            steps = ("ChatGPT connects from OpenAI's cloud, so it goes through OpenAI's <b>Secure MCP Tunnel</b> "
                     "running on this person's Mac: the Mac only connects <i>outward</i> to OpenAI and passes requests "
                     "to this sim over the LAN. Nothing on the sim PC or your network is opened to the internet, and "
                     "ChatGPT can only reach the sim while that Mac has it switched on."
                     "<ol><li><b>OpenAI (once):</b> platform.openai.com → Settings → Organization → <b>Tunnels</b>: "
                     "create a tunnel, link it to your ChatGPT workspace, copy the <i>tunnel ID</i>. Create a "
                     "<i>runtime API key</i> with Tunnels <i>Read + Use</i>.</li>"
                     "<li><b>Mac (once):</b> Save the script below and AirDrop it to the Mac. In Terminal run "
                     "<code>bash ~/Downloads/" + f"setup-chatgpt-{html.escape(net.server_key_name(sim_name))}.command"
                     + "</code> and paste the tunnel ID and key when asked (the key goes into the macOS Keychain). "
                     "It installs OpenAI's tunnel-client with Homebrew.</li>"
                     "<li><b>ChatGPT (once):</b> Settings → Apps &amp; Connectors (turn on Developer mode if needed) "
                     "→ <b>Add custom MCP server</b> → Connection: <b>Tunnel</b> → choose the tunnel. If asked for "
                     "authentication, choose none: the tunnel and this sim's personal key control access.</li>"
                     "<li><b>Daily use:</b> on the Mac, double-click <i>Start ChatGPT access</i> in the "
                     "<i>SimConfigManager</i> folder. Close that window to cut ChatGPT off.</li></ol>")
        else:
            steps = ("<ol><li>Copy <b>SimConfigManager-MCP.exe</b> to the laptop, e.g. "
                     "<i>C:\\SimConfigManager\\</i> (adjust the path below if you put it elsewhere).</li>"
                     "<li>Install <b>Claude Desktop</b>, then Settings → Developer → Edit Config, paste the text "
                     "below into <i>claude_desktop_config.json</i>, save, and restart Claude Desktop.</li></ol>")
        lab = QLabel(steps)
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.RichText)
        lay.addWidget(lab)
        row = QHBoxLayout()
        row.addWidget(QLabel("Address of this sim PC:"))
        self.url = QComboBox()
        self.url.setEditable(True)
        self.url.addItems(urls)
        self.url.currentTextChanged.connect(self.update_text)
        row.addWidget(self.url, 1)
        lay.addLayout(row)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(theme.mono_font(9))
        lay.addWidget(self.text, 1)
        lay.addWidget(banner("⚠ This contains the access key for <b>" + html.escape(key["name"]) + "</b>. Send it "
                             "privately (e.g. AirDrop or a direct message), not in a group chat. To revoke it, remove "
                             "the person on the AI &amp; network page."))
        lay.addLayout(button_row(("Copy to clipboard", lambda: QApplication.clipboard().setText(self.content())),
                                 ("Save as file…", self.save_file), trailing=[("Close", self.accept)]))
        self.update_text()

    def config(self) -> dict:
        url = self.url.currentText().strip()
        if self.kind == "chatgpt":
            return {}
        if self.kind == "mac":
            return net.mac_config(self.sim_name, url, self.key["key"])
        return net.windows_config(self.sim_name, url, self.key["key"])

    def content(self) -> str:
        if self.kind == "app":
            from ..remote import connection_file
            return connection_file(self.sim_name, self.url.currentText().strip().rsplit("/mcp", 1)[0],
                                   self.key["key"])
        if self.kind == "chatgpt":
            return net.chatgpt_setup_script(self.sim_name, self.key["name"], self.url.currentText().strip(),
                                            self.key["key"])
        return json.dumps(self.config(), indent=2)

    def update_text(self, *_):
        self.text.setPlainText(self.content())

    def save_file(self):
        if self.kind == "app":
            p, _ = QFileDialog.getSaveFileName(self, "Save", f"{net.server_key_name(self.sim_name)}.scmconnect",
                                               "Connection (*.scmconnect)")
            if p:
                Path(p).write_text(self.content(), encoding="utf-8")
            return
        if self.kind == "chatgpt":
            name = f"setup-chatgpt-{net.server_key_name(self.sim_name)}.command"
            p, _ = QFileDialog.getSaveFileName(self, "Save", name, "Mac script (*.command)")
        else:
            p, _ = QFileDialog.getSaveFileName(self, "Save", "claude_desktop_config.json", "JSON (*.json)")
        if p:
            Path(p).write_bytes(self.content().encode("utf-8"))  # LF line endings, also when saved on Windows


class ProposalDialog(QDialog):
    """Review a change Claude proposed: reason, diff, Apply / Reject."""

    def __init__(self, repo: Repo, prop: dict, parent=None):
        super().__init__(parent)
        self.repo, self.prop = repo, prop
        self.setWindowTitle(f"Proposed change — {prop['label']}")
        self.resize(980, 640)
        lay = QVBoxLayout(self)
        f = repo.file(prop["file_id"])
        cur = repo.read_current(f) if f else None
        new = core.proposal_data(prop)
        status = {"pending": "waiting for approval", "applied": "applied", "rejected": "rejected"}.get(prop["status"],
                                                                                                    prop["status"])
        head = QLabel(f"<b>{html.escape(prop['label'])}</b> — {html.escape(prop['path'])}<br>"
                      f"Proposed {fmt_ts(prop['created'])} by <b>{html.escape(prop.get('author', 'Claude'))}</b>"
                      f" · {status}")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)
        lay.addWidget(banner("<b>Why:</b><br>" + html.escape(prop["reason"]).replace("\n", "<br>"), "info"))
        warns = []
        if f is None:
            warns.append("This file is no longer tracked.")
        elif (core.sha256_bytes(cur) if cur is not None else None) != prop.get("base_hash"):
            warns.append("⚠ The file changed after the assistant read it. The comparison below is between the file "
                         "as it is now and the proposal — applying would also undo those newer changes.")
        running = repo.running_sim_processes()
        if running:
            warns.append("⚠ Running now: " + ", ".join(running) + ". Prepar3D rewrites Prepar3D.cfg when it exits; "
                         "apply with the sim closed.")
        for wtxt in warns:
            lay.addWidget(banner(html.escape(wtxt)))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(theme.mono_font(10))
        view.setHtml(diff_html(core.unified_diff(cur, new, "on disk now", "proposed")))
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Approved by:"))
        self.by = QLineEdit(repo.get("version_author") or os.environ.get("USERNAME", ""))
        row.addWidget(self.by)
        row.addStretch()
        if prop["status"] == "pending" and f is not None:
            row.addWidget(button("Apply change", self.apply, primary=True, icon="check"))
            row.addWidget(button("Reject…", self.reject_prop, danger=True))
        row.addWidget(button("Close", self.reject))
        lay.addLayout(row)

    def apply(self):
        try:
            core.apply_proposal(self.repo, self.prop, self.by.text().strip())
        except Exception as e:  # noqa: BLE001
            fail(self, "apply the change", e)
            return
        inform(self, "Applied and recorded in History. Tell the assistant it's done — it can read the file again "
                     "to check.")
        self.accept()

    def reject_prop(self):
        note, ok = QInputDialog.getText(self, "Reject", "Why not? (optional — the assistant can see this)")
        if not ok:
            return
        core.reject_proposal(self.repo, self.prop, note)
        self.accept()
