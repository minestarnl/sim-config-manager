"""SimConfigManager desktop UI (PySide6)."""
from __future__ import annotations

import html
import threading
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap, QBrush, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
    QColorDialog, QScrollArea,
)

from . import core, net, theme
from .theme import TLabel as QLabel, TTextEdit as QTextEdit  # theme-aware versions
from .core import (APP_NAME, APP_VERSION, Repo, Rule, RULE_HELP, RULE_KINDS, fmt_ts,
                   unified_diff)

STATUS_COLORS = {"OK": "#2e7d32", "Changed": "#e65100", "Missing": "#c62828", "New": "#1565c0"}


# --------------------------------------------------------------------------- small helpers

def make_icon(alert: bool = False) -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor("#c62828" if alert else "#1f4e79")))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QPen(QColor("white"), 5))
    # a little "gear + check" glyph: circle and tick
    p.drawEllipse(14, 14, 36, 36)
    p.drawLine(23, 33, 30, 40)
    p.drawLine(30, 40, 42, 25)
    p.end()
    return QIcon(pm)


def make_icon_remote() -> QIcon:
    """Purple icon for windows connected to a sim over the network."""
    pm = make_icon().pixmap(64, 64)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor("#6a1b9a")))
    p.setPen(QPen(QColor("white"), 3))
    p.drawEllipse(36, 36, 26, 26)
    p.drawLine(42, 49, 56, 49)
    p.end()
    return QIcon(pm)


def item(text, data=None, color: Optional[str] = None, bold=False) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if data is not None:
        it.setData(Qt.UserRole, data)
    if color:
        it.setForeground(QBrush(QColor(theme.c(color))))
    if bold:
        f = it.font()
        f.setBold(True)
        it.setFont(f)
    return it


def setup_table(t: QTableWidget, headers: list[str], stretch_col: int = -1):
    t.setColumnCount(len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.verticalHeader().setVisible(False)
    t.setAlternatingRowColors(True)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch_col >= 0:
        hh.setSectionResizeMode(stretch_col, QHeaderView.Stretch)


def selected_data(t: QTableWidget, col: int = 0):
    rows = sorted({i.row() for i in t.selectedIndexes()})
    out = []
    for r in rows:
        it = t.item(r, col)
        if it is not None:
            out.append(it.data(Qt.UserRole))
    return out


def open_in_explorer(path: str):
    if sys.platform == "win32":
        if os.path.isfile(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            os.startfile(path if os.path.isdir(path) else os.path.dirname(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])


# --------------------------------------------------------------------------- dialogs

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
            QMessageBox.warning(self, APP_NAME, str(e))
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
            lay.addWidget(QLabel(f"<b>Setting changes ({len(summary)})</b>"))
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
                row = QHBoxLayout()
                b = QPushButton("Ignore the selected setting from now on…")
                b.setToolTip("For values the sim rewrites by itself (e.g. window positions): changes to them will no "
                             "longer count as changes or trigger alerts.")
                b.clicked.connect(lambda: [ignore_cb(it.data(Qt.UserRole)) for it in self.sum_list.selectedItems()[:1]])
                row.addWidget(b)
                row.addStretch()
                lay.addLayout(row)
            lay.addWidget(QLabel("<b>Line by line</b>"))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(theme.mono_font(10))
        if not diff_text.strip():
            diff_text = "(no differences)"
        lines = []
        for ln in diff_text.splitlines():
            esc = html.escape(ln) or "&nbsp;"
            if ln.startswith("+++") or ln.startswith("---"):
                lines.append(f"<span style='color:#555;font-weight:bold'>{esc}</span>")
            elif ln.startswith("@@"):
                lines.append(f"<span style='color:#6a1b9a'>{esc}</span>")
            elif ln.startswith("+"):
                lines.append(f"<span style='background:#e6ffed;color:#1b5e20'>{esc}</span>")
            elif ln.startswith("-"):
                lines.append(f"<span style='background:#ffeef0;color:#b71c1c'>{esc}</span>")
            else:
                lines.append(esc)
        view.setHtml("<pre style='font-family:Consolas,monospace'>" + "<br>".join(lines) + "</pre>")
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
        self.action.addItem("Notify only", "notify")
        self.action.addItem("Notify AND automatically undo the change", "revert")
        self.action.setCurrentIndex(max(self.action.findData(self.rule.action), 0))
        self.enabled = QCheckBox("Enabled")
        self.enabled.setChecked(self.rule.enabled)
        self.help = QLabel()
        self.help.setWordWrap(True)
        self.help.setStyleSheet("color:#555")
        form.addRow("Name", self.name)
        form.addRow("File", self.file)
        form.addRow("Trigger", self.kind)
        form.addRow("Pattern", self.pattern)
        form.addRow("Restore point", self.rp_combo)
        form.addRow("Expected value", self.expected)
        form.addRow("Action", self.action)
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
            QMessageBox.warning(self, APP_NAME, "This trigger needs a pattern.")
            return
        if k == "differs_from_rp" and not self.rp_combo.currentText():
            QMessageBox.warning(self, APP_NAME, "Create a restore point first.")
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
        self.setWindowTitle("Found sim config files")
        self.resize(900, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Tick the files to track. Group names can be edited later (e.g. 'Sim 1 – P3D v5')."))
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
        self.setWindowTitle("Export archive")
        self.resize(640, 260)
        form = QFormLayout(self)
        what = "all monitored files as they are now" if current else f"the files in '{name}'"
        form.addRow(QLabel(f"Creates one zip with {what}, a REPORT.txt with paths, dates and SHA-256 checksums "
                           "(for certification), and a .sha256 file to prove the zip was not changed later."))
        stamp = core.time.strftime("%Y-%m-%d_%H%M")
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
        form.addRow("Save to", holder)
        self.note = QLineEdit()
        self.note.setPlaceholderText("e.g. 'Annual certification 2026 – inspector J. Smith'")
        form.addRow("Note (in report)", self.note)
        self.hist = QCheckBox("Include full history, restore points and alert rules "
                              "(lets you rebuild everything on another PC)")
        self.hist.setChecked(True)
        form.addRow("", self.hist)
        self.upload = QCheckBox("Also upload to the shared location (Google Drive / sync folder) → archives")
        has_sync = (repo.get("sync_mode") or ("folder" if repo.get("sync_folder") else "")) not in ("", "off")
        self.upload.setEnabled(has_sync)
        self.upload.setChecked(has_sync and repo.get("archive_upload") != "0")
        form.addRow("", self.upload)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
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
        self.setWindowTitle("Import archive")
        self.resize(1100, 600)
        lay = QVBoxLayout(self)
        v = arc.verify()
        bad = f"<span style='color:#c62828'><b>{len(v['bad'])} damaged: {', '.join(v['bad'])}</b></span>" \
            if v["bad"] else "<span style='color:#2e7d32'>all files OK</span>"
        zc = v["zip_check"]
        zcol = "#2e7d32" if zc == "matches" else ("#c62828" if "NOT" in zc else "#555")
        lay.addWidget(QLabel(
            f"<b>{html.escape(os.path.basename(arc.path))}</b><br>"
            f"From <b>{html.escape(idx.get('machine', '?'))}</b>, created {fmt_ts(idx.get('created', 0))}"
            f"{' — ' + html.escape(idx['note']) if idx.get('note') else ''}<br>"
            f"Checksums: {v['ok']} verified, {bad} · zip checksum: <span style='color:{zcol}'>{zc}</span>"))
        lay.addWidget(QLabel("Check the target paths (double-click a target to change it). "
                             "User-folder paths were translated to this PC automatically."))
        self.table = QTableWidget()
        setup_table(self.table, ["Import", "Name", "Group", "Original path", "Target on this PC"], 4)
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
            self.table.setItem(r, 4, item(tgt, color=None if exists else "#1565c0"))
        self.table.cellDoubleClicked.connect(self.edit_target)
        lay.addWidget(self.table)
        self.write = QCheckBox("Write the files to the target paths (overwrites; safety snapshot taken first)")
        self.write.setChecked(True)
        self.track = QCheckBox("Keep monitoring these files on this PC")
        self.track.setChecked(True)
        self.hist = QCheckBox("Also import full history, restore points and alert rules")
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
            intro = (f"<span style='color:#e65100'>Nothing changed since {html.escape(latest.label)}.</span> "
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
            w = QLabel("⚠ Running now: " + ", ".join(running) + ". P3D rewrites Prepar3D.cfg when it exits — "
                       "it is safer to create the version with the sim closed.")
            w.setWordWrap(True)
            w.setStyleSheet("background:#fff3cd; color:#664d03; padding:6px")
            lay.addWidget(w)
        form = QFormLayout()
        self.title = QLineEdit()
        self.title.setPlaceholderText("e.g. 'ProSim 3.30 update + new display settings'")
        form.addRow("Title", self.title)
        self.by = QLineEdit(repo.get("version_author") or os.environ.get("USERNAME", ""))
        form.addRow("Created by", self.by)
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
            QMessageBox.warning(self, APP_NAME, "Please fill in who is creating this version.")
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
        self.current.setStyleSheet("color:#555")
        self.setting.currentTextChanged.connect(self.show_current)
        self.required = QLineEdit(self.entry.get("required", ""))
        self.required.setPlaceholderText("leave empty for a note only")
        self.note = QTextEdit()
        self.note.setAcceptRichText(False)
        self.note.setPlainText(self.entry.get("note", ""))
        self.note.setMaximumHeight(90)
        self.author = QLineEdit(self.entry.get("author") or repo.get("version_author") or os.environ.get("USERNAME", ""))
        form.addRow("File", self.file)
        form.addRow("Setting", self.setting)
        form.addRow("", self.current)
        form.addRow("Required value", self.required)
        form.addRow("Note / why", self.note)
        form.addRow("By", self.author)
        hint = QLabel("CFG/INI: <i>Section/Key</i> (e.g. GRAPHICS/TEXTURE_MAX_LOAD). XML: element path, "
                      "optionally @attribute (e.g. .//Server@ip).")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555")
        form.addRow("", hint)
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
            QMessageBox.warning(self, APP_NAME, "Fill in the setting.")
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
        self.setWindowTitle(f"Setup for {key['name']} (" + {"mac": "Claude on a Mac", "windows": "Claude on a Windows "
                            "laptop", "chatgpt": "ChatGPT via a Mac", "app": "remote app"}[kind] + ")")
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
                     "the network — edit files, restore, create versions, review Claude's proposals — as if sitting "
                     "here. Everything is logged with their name." +
                     ("" if key.get("full_control") else "<br><b style='color:#c62828'>This person's key is not "
                      "'Full control'. Remove and add them again with Full control.</b>") +
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
        warn = QLabel("⚠ This contains the access key for <b>" + html.escape(key["name"]) + "</b>. Send it "
                      "privately (e.g. AirDrop or a direct message), not in a group chat. Remove the person in the "
                      "Claude tab to revoke it.")
        warn.setWordWrap(True)
        warn.setTextFormat(Qt.RichText)
        warn.setStyleSheet("background:#fff3cd; color:#664d03; padding:6px")
        lay.addWidget(warn)
        row = QHBoxLayout()
        b = QPushButton("Copy to clipboard")
        b.clicked.connect(lambda: QApplication.clipboard().setText(self.content()))
        row.addWidget(b)
        b = QPushButton("Save as file…")
        b.clicked.connect(self.save_file)
        row.addWidget(b)
        row.addStretch()
        b = QPushButton("Close")
        b.clicked.connect(self.accept)
        row.addWidget(b)
        lay.addLayout(row)
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
            from .remote import connection_file
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
        head = QLabel(f"<b>{html.escape(prop['label'])}</b> — {html.escape(prop['path'])}<br>"
                      f"Proposed {fmt_ts(prop['created'])} by <b>{html.escape(prop.get('author', 'Claude'))}</b>"
                      f" · status: <b>{prop['status']}</b>")
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)
        reason = QLabel("<b>Reason given:</b><br>" + html.escape(prop["reason"]).replace("\n", "<br>"))
        reason.setWordWrap(True)
        reason.setTextFormat(Qt.RichText)
        reason.setStyleSheet("background:#eef4fb; padding:8px; border-radius:4px")
        lay.addWidget(reason)
        warns = []
        if f is None:
            warns.append("This file is no longer tracked.")
        elif (core.sha256_bytes(cur) if cur is not None else None) != prop.get("base_hash"):
            warns.append("⚠ The file changed after Claude read it. The diff below compares the file as it is NOW "
                         "with Claude's version — applying would also undo those newer changes.")
        running = repo.running_sim_processes()
        if running:
            warns.append("⚠ Running now: " + ", ".join(running) + ". Prepar3D rewrites Prepar3D.cfg when it exits; "
                         "apply with the sim closed.")
        for wtxt in warns:
            lab = QLabel(wtxt)
            lab.setWordWrap(True)
            lab.setStyleSheet("background:#fff3cd; color:#664d03; padding:6px")
            lay.addWidget(lab)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(theme.mono_font(10))
        out = []
        for ln in core.unified_diff(cur, new, "now", "proposed").splitlines():
            e = html.escape(ln) or "&nbsp;"
            if ln.startswith(("+++", "---")):
                out.append(f"<b style='color:#555'>{e}</b>")
            elif ln.startswith("@@"):
                out.append(f"<span style='color:#6a1b9a'>{e}</span>")
            elif ln.startswith("+"):
                out.append(f"<span style='background:#e6ffed;color:#1b5e20'>{e}</span>")
            elif ln.startswith("-"):
                out.append(f"<span style='background:#ffeef0;color:#b71c1c'>{e}</span>")
            else:
                out.append(e)
        view.setHtml("<pre>" + "<br>".join(out) + "</pre>")
        lay.addWidget(view, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Approved by:"))
        self.by = QLineEdit(repo.get("version_author") or os.environ.get("USERNAME", ""))
        row.addWidget(self.by)
        row.addStretch()
        if prop["status"] == "pending" and f is not None:
            b = QPushButton("✔ Apply change")
            b.setStyleSheet("QPushButton { background:#2e7d32; color:white; font-weight:bold; padding:6px 14px; }")
            b.clicked.connect(self.apply)
            row.addWidget(b)
            b = QPushButton("✖ Reject")
            b.clicked.connect(self.reject_prop)
            row.addWidget(b)
        b = QPushButton("Close")
        b.clicked.connect(self.reject)
        row.addWidget(b)
        lay.addLayout(row)

    def apply(self):
        try:
            core.apply_proposal(self.repo, self.prop, self.by.text().strip())
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Could not apply: {e}")
            return
        QMessageBox.information(self, APP_NAME, "Applied and recorded in History. Tell Claude it's done — it can "
                                "read the file again to check.")
        self.accept()

    def reject_prop(self):
        note, ok = QInputDialog.getText(self, APP_NAME, "Why not? (optional — Claude can see this)")
        if not ok:
            return
        core.reject_proposal(self.repo, self.prop, note)
        self.accept()


# --------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, repo: Repo, start_hidden: bool = False):
        super().__init__()
        self.repo = repo
        self.remote = core.is_remote(repo)
        self.host = repo.host_info() if self.remote else {"platform": sys.platform}
        self.paused = False
        self.sync_dirty = False
        self.setWindowTitle(f"{APP_NAME} — {repo.machine}")
        self.setWindowIcon(make_icon())
        self.resize(1200, 760)

        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.setDocumentMode(True)
        self.tab_versions = self.build_versions_tab()
        self.tab_quick = self.build_quick_tab()
        self.tab_files = self.build_files_tab()
        self.tab_history = self.build_history_tab()
        self.tab_alerts = self.build_alerts_tab()
        self.tab_sync = self.build_sync_tab()
        self.tab_claude = self.build_claude_tab()
        self.tab_rulebook = self.build_rulebook_tab()
        self.tab_fleet = self.build_fleet_tab()
        self.tab_settings = self.build_settings_tab()
        self.tabs.addTab(self.tab_fleet, "Fleet")
        self.tabs.addTab(self.tab_versions, "Versions")
        self.tabs.addTab(self.tab_quick, "Quick restore")
        self.tabs.addTab(self.tab_files, "Files")
        self.tabs.addTab(self.tab_history, "History")
        self.tabs.addTab(self.tab_rulebook, "Rulebook")
        self.tabs.addTab(self.tab_alerts, "Alerts")
        self.tabs.addTab(self.tab_sync, "Cloud / other sims")
        self.tabs.addTab(self.tab_claude, "Claude")
        self.tabs.addTab(self.tab_settings, "Settings")
        self.tabs.currentChanged.connect(lambda _i: self.refresh_all())
        self.build_chrome()
        self.statusBar().showMessage("Ready")

        self.build_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.apply_poll_interval()
        self.hub_timer = QTimer(self)
        self.hub_timer.timeout.connect(self.hub_tick)
        self.hub_timer.start(30_000)
        QTimer.singleShot(8000, self.hub_tick)
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.auto_push)
        self.sync_timer.start(60_000)

        if self.remote:
            self.setWindowTitle(f"{APP_NAME} — {repo.machine} (remote, {repo.base})")
            self.setWindowIcon(make_icon_remote())
        else:
            self.repo.background_probes = True
        self.probe_timer = QTimer(self)
        self.probe_timer.timeout.connect(self.refresh_probes_bg)
        self.probe_timer.start(60_000)
        QTimer.singleShot(3000, self.refresh_probes_bg)
        QTimer.singleShot(20_000, self.daily_update_check)
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.daily_update_check)
        self.update_timer.start(3_600_000)
        if not self.remote:
            self.repo.ensure_baseline()
        self.refresh_all()
        if not start_hidden:
            self.show()

    # ================================================================= tray
    def build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(f"{APP_NAME} — watching config files")
        menu = QMenu()
        menu.addAction("Open", self.show_window)
        self.tray_version_action = menu.addAction("Version status…", self.open_versions)
        menu.addAction("Create new version…", self.create_version)
        menu.addSeparator()
        menu.addAction("Snapshot now…", self.snapshot_now)
        menu.addAction("Export archive (zip)…", self.export_archive)
        self.tray_rp_menu = menu.addMenu("Restore all to…")
        self.pause_action = QAction("Pause monitoring", self, checkable=True)
        self.pause_action.toggled.connect(self.set_paused)
        menu.addAction(self.pause_action)
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.show_window()
                                    if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.messageClicked.connect(self.open_alerts)
        self.tray.show()

    def rebuild_tray_rp_menu(self):
        self.tray_rp_menu.clear()
        rps = self.repo.restore_points()
        if not rps:
            a = self.tray_rp_menu.addAction("(no restore points yet)")
            a.setEnabled(False)
        for rp in rps:
            self.tray_rp_menu.addAction(rp.name, lambda r=rp: self.restore_rp(r))

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh_all()

    def open_alerts(self):
        self.show_window()
        self.tabs.setCurrentWidget(self.tab_alerts)

    def closeEvent(self, ev):
        # closing the window just hides to tray; monitoring keeps running
        ev.ignore()
        self.hide()
        if not self.repo.get("tray_hint_shown"):
            self.tray.showMessage(APP_NAME, "Still running in the tray and watching your files.",
                                  QSystemTrayIcon.Information, 4000)
            self.repo.set("tray_hint_shown", "1")

    def quit_app(self):
        for e in list(getattr(self, "editors", [])):
            if e.isVisible() and not e.close():
                return
        if self.sync_dirty and not self.remote:
            try:  # last upload before quitting (synchronous)
                s = self.sync()
                if s is not None:
                    s.push()
            except Exception:  # noqa: BLE001
                pass
        self.stop_network()
        self.tray.hide()
        QApplication.quit()

    def set_paused(self, on: bool):
        self.paused = on
        self.tray.setToolTip(f"{APP_NAME} — {'PAUSED' if on else 'watching config files'}")
        self.statusBar().showMessage("Monitoring paused" if on else "Monitoring")

    # ================================================================= monitoring
    def apply_poll_interval(self):
        try:
            secs = max(1, int(self.repo.get("poll_seconds")))
        except ValueError:
            secs = 5
        self.timer.start(secs * 1000)

    def tick(self):
        if self.remote:
            # the sim itself watches its files; here we only keep the views fresh
            self._remote_ticks = getattr(self, "_remote_ticks", 0) + 1
            try:
                self.check_proposals()
                if self.isVisible() and self._remote_ticks % 3 == 0 and not any(
                        e.isVisible() and e.isActiveWindow() for e in getattr(self, "editors", [])):
                    self.refresh_all()
            except Exception as e:  # noqa: BLE001
                self.statusBar().showMessage(f"Connection problem: {e}")
            return
        self.check_proposals()
        if getattr(self, "_net_activity", None):
            self.drain_net_activity()
        if self.paused:
            return
        try:
            changes = self.repo.scan()
            if not changes:
                return
            alerts = self.repo.process_changes(changes)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Scan error: {e}")
            return
        self.sync_dirty = True
        for a in alerts[:5]:
            icon = QSystemTrayIcon.Warning
            self.tray.showMessage(f"{APP_NAME}: {a.rule.name}", a.message, icon, 10000)
        if alerts:
            self.tray.setIcon(make_icon(alert=True))
            QTimer.singleShot(30_000, lambda: self.tray.setIcon(make_icon()))
        self.statusBar().showMessage(f"{fmt_ts(core.now_ts())}: {len(changes)} file(s) changed"
                                     + (f", {len(alerts)} alert(s)" if alerts else ""))
        if self.isVisible():
            self.refresh_all()
        else:
            self.update_version_indicator()


    # ================================================================= refresh
    def refresh_all(self):
        cur = self.tabs.currentWidget()
        self.rebuild_tray_rp_menu()
        fn = {self.tab_versions: self.refresh_versions, self.tab_quick: self.refresh_quick,
              self.tab_files: self.refresh_files, self.tab_history: self.refresh_history,
              self.tab_alerts: self.refresh_alerts, self.tab_sync: self.refresh_sync,
              self.tab_claude: self.refresh_claude, self.tab_rulebook: self.refresh_rulebook,
              self.tab_fleet: self.refresh_fleet}.get(cur)
        if fn:
            fn()
        self.update_version_indicator()

    # ================================================================= VS Code-style chrome
    PAGES = {  # tab attribute: (title, codicon, subtitle)
        "tab_fleet": ("Fleet", "fleet", "Every sim at a glance"),
        "tab_versions": ("Versions", "versions", "Numbered configurations and what differs from them"),
        "tab_quick": ("Quick restore", "restore", "Put all files back to a saved setup"),
        "tab_files": ("Files", "files", "Tracked files and their history"),
        "tab_history": ("History", "history", "Every recorded snapshot"),
        "tab_rulebook": ("Rulebook", "rulebook", "Documented and locked settings"),
        "tab_alerts": ("Alerts", "alerts", "Rules, ignored settings and the event log"),
        "tab_sync": ("Cloud & other sims", "cloud", "Google Drive or a shared folder"),
        "tab_claude": ("AI assistants & network", "claude", "Claude, ChatGPT and remote access"),
        "tab_settings": ("Settings", "settings", ""),
    }

    def build_chrome(self):
        central = QWidget()
        h = QHBoxLayout(central)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("ActivityBar")
        bar.setFixedWidth(48)
        bl = QVBoxLayout(bar)
        bl.setContentsMargins(0, 4, 0, 4)
        bl.setSpacing(0)
        from PySide6.QtWidgets import QButtonGroup, QToolButton
        self.activity = {}
        group = QButtonGroup(self)
        group.setExclusive(True)
        for idx in range(self.tabs.count()):
            page = self.tabs.widget(idx)
            attr = next(a for a in self.PAGES if getattr(self, a, None) is page)
            title, glyph, _sub = self.PAGES[attr]
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(48, 48)
            b.setIconSize(QSize(24, 24))
            b.setToolTip(title)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, i=idx: self.tabs.setCurrentIndex(i))
            group.addButton(b)
            self.activity[idx] = (b, glyph)
            if attr == "tab_settings":
                bl.addStretch(1)
            bl.addWidget(b)
        h.addWidget(bar)
        right = QWidget()
        v = QVBoxLayout(right)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        header = QWidget()
        header.setObjectName("PageHeader")
        header.setFixedHeight(40)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(16, 0, 16, 0)
        self.page_title = QLabel()
        self.page_title.setObjectName("PageTitle")
        self.page_subtitle = QLabel()
        self.page_subtitle.setObjectName("PageSubtitle")
        hl.addWidget(self.page_title)
        hl.addSpacing(10)
        hl.addWidget(self.page_subtitle)
        hl.addStretch(1)
        v.addWidget(header)
        body = QWidget()
        bv = QVBoxLayout(body)
        bv.setContentsMargins(12, 10, 12, 8)
        bv.addWidget(self.tabs)
        v.addWidget(body, 1)
        h.addWidget(right, 1)
        self.setCentralWidget(central)

        sb = self.statusBar()
        sb.setSizeGripEnabled(False)
        sb.setProperty("remote", self.remote)
        self.sb_host = QLabel()
        self.sb_version = QLabel()
        self.sb_net = QLabel()
        self.sb_app = QLabel(f"{APP_NAME} {APP_VERSION}")
        for w in (self.sb_host, self.sb_version, self.sb_net, self.sb_app):
            sb.addPermanentWidget(w)
        self.tabs.currentChanged.connect(lambda _i: self.sync_activity())
        self.sync_activity()

    @staticmethod
    def _glyph(name: str) -> str:
        return (f"<span style=\"font-family:'{theme._ICON_FAMILY}'\">"
                f"{chr(theme.ICONS.get(name, theme.ICONS['dot']))}</span>")

    def sync_activity(self):
        if not hasattr(self, "activity"):
            return
        cur = self.tabs.currentIndex()
        for idx, (b, glyph) in self.activity.items():
            text = self.tabs.tabText(idx)
            badge = text[text.rfind("(") + 1:-1] if text.endswith(")") and "(" in text else None
            ico = QIcon()
            ico.addPixmap(theme.glyph_pixmap(glyph, theme.T["activity_fg"], 24, badge), QIcon.Normal, QIcon.Off)
            ico.addPixmap(theme.glyph_pixmap(glyph, theme.T["activity_active"], 24, badge), QIcon.Normal, QIcon.On)
            ico.addPixmap(theme.glyph_pixmap(glyph, theme.T["activity_active"], 24, badge), QIcon.Active, QIcon.Off)
            b.setIcon(ico)
            b.setChecked(idx == cur)
            b.setToolTip(text)
        page = self.tabs.currentWidget()
        attr = next((a for a in self.PAGES if getattr(self, a, None) is page), None)
        if attr:
            title, _g, sub = self.PAGES[attr]
            self.page_title.setText(title.upper())
            self.page_subtitle.setText(sub)

    def update_statusbar(self, version_text: str = ""):
        if not hasattr(self, "sb_host"):
            return
        if self.remote:
            self.sb_host.setText(f"{self._glyph('remote')} Remote: {html.escape(self.repo.machine)}")
        else:
            self.sb_host.setText(f"{self._glyph('fleet')} {html.escape(self.repo.machine)}")
        if version_text:
            self.sb_version.setText(version_text)
        srv = getattr(self, "net_server", None)
        if self.remote:
            self.sb_net.setText(f"{self._glyph('plug')} {html.escape(self.repo.base.split('//')[-1])}")
        elif srv:
            self.sb_net.setText(f"{self._glyph('broadcast')} Network :{srv.port}")
        else:
            self.sb_net.setText("")

    def update_version_indicator(self):
        """Tray tooltip + window title show whether the files still match the current version."""
        try:
            ver = self.repo.latest_version()
            mm = self.repo.mismatches() if ver else []
        except Exception:  # noqa: BLE001
            return
        if ver is None:
            txt = "no version created yet"
        elif mm:
            txt = f"{len(mm)} file(s) differ from v{ver.number}"
        else:
            txt = f"all files match v{ver.number}"
        state = "PAUSED · " if self.paused else ""
        self.tray.setToolTip(f"{APP_NAME} — {state}{txt}")
        self.tray_version_action.setText(f"Version status: {txt}")
        self.setWindowTitle(f"{APP_NAME} — {self.repo.machine}{' (remote)' if self.remote else ''} — {txt}")
        glyph = "warning" if (ver and mm) else ("check" if ver else "tag")
        self.update_statusbar(f"{self._glyph(glyph)} {html.escape(txt)}")
        prev = getattr(self, "_last_mismatch_count", None)
        self._last_mismatch_count = len(mm)
        if ver and prev == 0 and mm and self.repo.get("notify_version_drift") != "0":
            self.tray.showMessage(f"{APP_NAME}: files differ from v{ver.number}",
                                  ", ".join(m.file.display for m in mm[:5]), QSystemTrayIcon.Warning, 8000)

    def open_versions(self):
        self.show_window()
        self.tabs.setCurrentWidget(self.tab_versions)

    # ================================================================= versions tab
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

    # ================================================================= quick restore tab
    def build_quick_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        title = QLabel("<h2>Restore all files to a saved setup</h2>")
        top.addWidget(title)
        top.addStretch()
        b = QPushButton("Save current setup as restore point…")
        b.clicked.connect(self.new_restore_point)
        top.addWidget(b)
        lay.addLayout(top)
        self.quick_info = QLabel()
        self.quick_info.setStyleSheet("color:#555")
        lay.addWidget(self.quick_info)
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

    def refresh_quick(self):
        while self.quick_grid.count():
            wdg = self.quick_grid.takeAt(0).widget()
            if wdg:
                wdg.hide()
                wdg.setParent(None)
                wdg.deleteLater()
        rps = self.repo.restore_points()
        self.quick_info.setText("Each button restores every tracked file to how it was when the restore point was "
                                "saved. A safety snapshot is always taken first, so a restore can be undone from "
                                "History. Right-click a button for more options." if rps else
                                "No restore points yet. Set the sims up the way you want them, then click "
                                "'Save current setup as restore point…'.")
        for i, rp in enumerate(rps):
            btn = QPushButton(f"{rp.name}\n{rp.description[:40]}" if rp.description else rp.name)
            btn.setMinimumSize(QSize(240, 90))
            col = rp.color or "#1f4e79"
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
            vtxt = (f" · <b style='color:#e65100'>{n} differ from v{ver.number}</b>" if n
                    else f" · <span style='color:#2e7d32'>matches v{ver.number}</span>")
        self.quick_status.setText(f"{len(files)} files tracked{vtxt} · "
                                  + (f"<b style='color:#e65100'>{len(changed)} changed since last snapshot</b>"
                                     if changed else "all files match the last snapshot"))

    def rp_context(self, rp, btn):
        m = QMenu(self)
        m.addAction("Restore all files", lambda: self.restore_rp(rp))
        grp = m.addMenu("Restore only group")
        for g in self.repo.groups():
            grp.addAction(g, lambda gg=g: self.restore_rp(rp, group=gg))
        m.addAction("Compare with current files…", lambda: self.compare_snapshot_with_current(rp.snapshot_id,
                                                                                              rp.name))
        m.addSeparator()
        m.addAction("Edit name / description / colour…", lambda: self.edit_rp(rp))
        m.addAction("Update to the CURRENT files…", lambda: self.overwrite_rp(rp))
        m.addAction("Export as zip…", lambda: self.export_zip(rp.snapshot_id, rp.name))
        m.addAction("Delete restore point", lambda: self.delete_rp(rp))
        m.exec(btn.mapToGlobal(btn.rect().center()))

    def new_restore_point(self, snapshot_id: Optional[int] = None):
        name, ok = QInputDialog.getText(self, "Restore point", "Name (e.g. 'Customer default', 'Checkride B737'):")
        if not ok or not name.strip():
            return
        desc, _ = QInputDialog.getText(self, "Restore point", "Short description (optional):")
        if self.repo.restore_point(name.strip()):
            if QMessageBox.question(self, APP_NAME, f"'{name}' already exists. Replace it?") != QMessageBox.Yes:
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
        col = QColorDialog.getColor(QColor(rp.color or "#1f4e79"), self, "Button colour")
        self.repo.update_restore_point(rp.id, name.strip() or rp.name, desc, col.name() if col.isValid()
                                       else rp.color)
        self.sync_dirty = True
        self.refresh_all()

    def overwrite_rp(self, rp):
        if QMessageBox.question(self, APP_NAME, f"Replace '{rp.name}' with the files as they are right now?") \
                == QMessageBox.Yes:
            snap = self.repo.commit(f"Restore point update: {rp.name}") or self.repo.latest_snapshot()
            self.repo.update_restore_point_snapshot(rp.id, snap.id)
            self.sync_dirty = True
            self.refresh_all()

    def delete_rp(self, rp):
        if QMessageBox.question(self, APP_NAME, f"Delete restore point '{rp.name}'?\n"
                                "(The snapshot itself stays in History.)") == QMessageBox.Yes:
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
            QMessageBox.information(self, APP_NAME, f"Everything already matches '{label}'. Nothing to do.")
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
        if QMessageBox.question(self, f"Restore to '{label}'",
                                f"{len(todo)} file(s) will be overwritten:\n\n{txt}{warn}\n\n"
                                "A safety snapshot is taken first. Continue?") != QMessageBox.Yes:
            return
        report = self.repo.restore(snapshot_id, file_ids, label=label)
        failed = [(f, r) for f, r in report if r.startswith("FAILED")]
        self.sync_dirty = True
        self.refresh_all()
        if failed:
            QMessageBox.warning(self, APP_NAME, "Some files could not be restored (in use or no permission? "
                                "Try closing the sim or running as administrator):\n\n" +
                                "\n".join(f"{f.display}: {r}" for f, r in failed))
        else:
            n = sum(1 for _, r in report if r == "restored")
            self.tray.showMessage(APP_NAME, f"Restored {n} file(s) to '{label}'", QSystemTrayIcon.Information,
                                  5000)

    # ================================================================= files tab
    def build_files_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        bar = QHBoxLayout()
        for text, fn in (("Add files…", self.add_files), ("Add folder…", self.add_folder),
                         ("Find P3D / ProSim configs…", self.discover),
                         ("Track system info…", self.add_system_info), ("✎ Open in editor", self.edit_contents),
                         ("Rename / group…", self.edit_file),
                         ("Stop tracking", self.remove_files), ("Snapshot now…", self.snapshot_now),
                         ("Export archive (zip)…", self.export_archive), ("Import archive…", self.import_archive)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        self.group_filter = QComboBox()
        self.group_filter.currentIndexChanged.connect(lambda _i: self.refresh_files())
        bar.addWidget(QLabel("Group:"))
        bar.addWidget(self.group_filter)
        lay.addLayout(bar)

        split = QSplitter(Qt.Vertical)
        self.files_table = QTableWidget()
        setup_table(self.files_table, ["Status", "Name", "Group", "Versions", "Last change", "Path"], 5)
        self.files_table.itemSelectionChanged.connect(self.refresh_file_history)
        self.files_table.doubleClicked.connect(lambda _i: self.edit_contents())
        self.files_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files_table.customContextMenuRequested.connect(self.files_context)
        split.addWidget(self.files_table)

        lower = QWidget()
        ll = QVBoxLayout(lower)
        ll.setContentsMargins(0, 0, 0, 0)
        self.hist_label = QLabel("<b>History of selected file</b>")
        ll.addWidget(self.hist_label)
        self.ver_table = QTableWidget()
        setup_table(self.ver_table, ["When", "Snapshot", "+/-", "Note"], 3)
        self.ver_table.doubleClicked.connect(lambda _i: self.diff_version_prev())
        ll.addWidget(self.ver_table)
        vb = QHBoxLayout()
        for text, fn in (("View this version", self.view_version), ("Changes in this version", self.diff_version_prev),
                         ("Compare with current file", self.diff_version_current),
                         ("Restore this version", self.restore_version),
                         ("Save copy as…", self.save_version_as), ("Open file location", self.open_location)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            vb.addWidget(b)
        vb.addStretch()
        ll.addLayout(vb)
        split.addWidget(lower)
        split.setSizes([420, 300])
        lay.addWidget(split)
        return w

    def refresh_files(self):
        cur_group = self.group_filter.currentText()
        self.group_filter.blockSignals(True)
        self.group_filter.clear()
        self.group_filter.addItem("(all)")
        self.group_filter.addItems(self.repo.groups())
        i = self.group_filter.findText(cur_group)
        self.group_filter.setCurrentIndex(max(i, 0))
        self.group_filter.blockSignals(False)
        g = self.group_filter.currentText()
        sel = set(selected_data(self.files_table))
        files = [f for f in self.repo.files() if g == "(all)" or f.group == g]
        self.files_table.setRowCount(len(files))
        for r, f in enumerate(files):
            st = self.repo.file_status(f)
            hist = self.repo.file_history(f.id)
            self.files_table.setItem(r, 0, item(st, f.id, STATUS_COLORS.get(st), bold=st != "OK"))
            self.files_table.setItem(r, 1, item(f.display))
            self.files_table.setItem(r, 2, item(f.group))
            self.files_table.setItem(r, 3, item(len(hist)))
            self.files_table.setItem(r, 4, item(fmt_ts(hist[0].created_at) if hist else "–"))
            self.files_table.setItem(r, 5, item(f.path))
            if f.id in sel:
                self.files_table.selectRow(r)

    def current_file(self):
        ids = selected_data(self.files_table)
        return self.repo.file(ids[0]) if ids else None

    def refresh_file_history(self):
        f = self.current_file()
        self.ver_table.setRowCount(0)
        if not f:
            self.hist_label.setText("<b>History of selected file</b>")
            return
        self.hist_label.setText(f"<b>History of {html.escape(f.display)}</b> — {html.escape(f.path)}")
        hist = self.repo.file_history(f.id)
        self.ver_table.setRowCount(len(hist))
        for r, v in enumerate(hist):
            prev = hist[r + 1].hash if r + 1 < len(hist) else None
            try:
                a, d = core.diff_stats(self.repo.get_object(prev), self.repo.get_object(v.hash))
                pm = f"+{a} / -{d}" if v.hash else "deleted"
            except FileNotFoundError:
                pm = "?"
            self.ver_table.setItem(r, 0, item(fmt_ts(v.created_at), (v.snapshot_id, v.hash, prev)))
            self.ver_table.setItem(r, 1, item(f"#{v.snapshot_id}"))
            self.ver_table.setItem(r, 2, item(pm))
            self.ver_table.setItem(r, 3, item(v.message))

    def current_version(self):
        d = selected_data(self.ver_table)
        return d[0] if d else None

    def files_context(self, pos):
        m = QMenu(self)
        m.addAction("Open in editor", self.edit_contents)
        m.addAction("Copy for Claude (paste into any chat)", self.copy_for_claude)
        m.addAction("Show changes since last snapshot", self.diff_file_with_last)
        m.addAction("Rename / change group…", self.edit_file)
        m.addAction("Open file location", self.open_location)
        m.addAction("Add alert rule for this file…", lambda: self.add_rule(self.current_file()))
        m.addSeparator()
        m.addAction("Stop tracking", self.remove_files)
        m.exec(self.files_table.viewport().mapToGlobal(pos))

    def add_files(self):
        paths = pick_files(self, self.repo, "Choose config files to track")
        if not paths:
            return
        groups = [""] + self.repo.groups()
        group, ok = QInputDialog.getItem(self, "Group", "Group (e.g. 'Sim 1 – P3D v5', 'ProSim'):", groups, 0, True)
        if not ok:
            return
        for p in paths:
            self.repo.add_file(p, group=group)
        self.repo.ensure_baseline()
        self.sync_dirty = True
        self.refresh_files()

    def add_folder(self):
        d = pick_folder(self, self.repo, "Choose folder")
        if not d:
            return
        pats, ok = QInputDialog.getText(self, "File types", "Which files? (separate with ;)",
                                        text="*.cfg;*.xml;*.ini;*.json")
        if not ok:
            return
        rec = QMessageBox.question(self, APP_NAME, "Include sub-folders?") == QMessageBox.Yes
        group, ok = QInputDialog.getText(self, "Group", "Group name:", text=os.path.basename(d))
        if not ok:
            return
        added = self.repo.add_folder(d, pats, rec, group)
        if len(added) > 200 and QMessageBox.question(
                self, APP_NAME, f"That is {len(added)} files — keep them all?") != QMessageBox.Yes:
            for f in added:
                self.repo.remove_file(f.id)
            return
        self.repo.ensure_baseline()
        self.sync_dirty = True
        self.refresh_files()
        self.statusBar().showMessage(f"Added {len(added)} file(s)")

    def discover(self):
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            cands = self.repo.discover()
        finally:
            QApplication.restoreOverrideCursor()
        if not cands:
            QMessageBox.information(self, APP_NAME, "No known Prepar3D / ProSim config files found in the usual "
                                    "places. Use 'Add files…' to pick them manually.")
            return
        dlg = DiscoverDialog(cands, self)
        if dlg.exec() == QDialog.Accepted:
            for label, group, path in dlg.chosen():
                self.repo.add_file(path, label, group)
            self.repo.ensure_baseline()
            self.sync_dirty = True
            self.refresh_files()

    # ---------------- diffs with setting summaries + ignore (#1)
    def file_diff(self, f, old, new, la, lb, title):
        summary = core.setting_changes(old, new, f.path)
        DiffDialog(title, unified_diff(old, new, la, lb), self, summary=summary,
                   ignore_cb=(lambda c, ff=f: self.ignore_setting(ff, c)) if summary else None).exec()

    def ignore_setting(self, f, change: dict):
        setting = change["setting"]
        if change.get("ftype") == "ini":
            kind, pattern = "ini_key", setting
        else:
            kind = "xml"
            path = setting.split("@")[0]
            parts = path.split("/")[1:]          # drop the root element
            pattern = ".//" + "/".join(parts) if parts else "."
        pattern, ok = QInputDialog.getText(
            self, "Ignore setting", f"Changes to this will no longer count for {f.display}.\n"
            f"Pattern ({core.IGNORE_KINDS[kind]}):", text=pattern)
        if not ok or not pattern.strip():
            return
        note, _ = QInputDialog.getText(self, "Ignore setting", "Why? (optional, e.g. 'P3D rewrites this on exit')")
        self.repo.add_ignore(f.id, kind, pattern.strip(), note)
        self.statusBar().showMessage(f"Ignoring {pattern} in {f.display}", 6000)
        self.refresh_all()

    # ---------------- built-in editor
    def open_editor(self, f, version_hash=None, label=""):
        from .editor import EditorWindow
        if not hasattr(self, "editors"):
            self.editors = []
        self.editors = [e for e in self.editors if e.isVisible()]
        if version_hash is None:
            for e in self.editors:  # already open → bring to front
                if e.file.id == f.id and not e.read_only:
                    e.showNormal()
                    e.raise_()
                    e.activateWindow()
                    return e
        try:
            ed = EditorWindow(self.repo, f, on_saved=self.after_edit, version_hash=version_hash,
                              version_label=label)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return None
        ed.setWindowIcon(self.windowIcon())
        ed.show()
        self.editors.append(ed)
        return ed

    def after_edit(self):
        self.sync_dirty = True
        if self.isVisible():
            self.refresh_all()

    def edit_contents(self):
        f = self.current_file()
        if f:
            self.open_editor(f)

    def view_version(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        if v[1] is None:
            QMessageBox.information(self, APP_NAME, "The file did not exist in that version.")
            return
        self.open_editor(f, v[1], f"snapshot #{v[0]}")

    def add_system_info(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Track system information")
        dlg.resize(560, 460)
        lay = QVBoxLayout(dlg)
        lab = QLabel("These are read from the PC every few minutes and tracked like files: history, versions, "
                     "alerts (e.g. 'GPU driver changed'), archives and Claude can all use them. They are never "
                     "written back.")
        lab.setWordWrap(True)
        lay.addWidget(lab)
        tracked = {f.path for f in self.repo.files()}
        boxes = {}
        for key, (label, _cmd) in core.BUILTIN_PROBES.items():
            cb = QCheckBox(label)
            path = core.PROBE_PREFIX + key
            cb.setChecked(path in tracked or key in ("gpu", "windows", "sim_software", "displays", "hardware"))
            cb.setEnabled(self.host.get("platform") == "win32" and path not in tracked)
            if path in tracked:
                cb.setText(label + "  (already tracked)")
            boxes[key] = cb
            lay.addWidget(cb)
        if self.host.get("platform") != "win32":
            lay.addWidget(QLabel("<i>The built-in items are only available on Windows.</i>"))
        row = QHBoxLayout()
        row.addWidget(QLabel("Group:"))
        grp = QLineEdit(f"{self.repo.machine} – System")
        row.addWidget(grp)
        lay.addLayout(row)
        lay.addWidget(QLabel("<b>Own command</b> (optional) — e.g. <i>Get-Content C:\\ProSim737\\version.txt</i>"))
        cname = QLineEdit()
        cname.setPlaceholderText("Name, e.g. 'ProSim version'")
        ccmd = QLineEdit()
        ccmd.setPlaceholderText("PowerShell command whose output should be tracked")
        lay.addWidget(cname)
        lay.addWidget(ccmd)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if dlg.exec() != QDialog.Accepted:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for key, cb in boxes.items():
                if cb.isEnabled() and cb.isChecked():
                    core.add_builtin_probe(self.repo, key, grp.text().strip())
            if cname.text().strip() and ccmd.text().strip():
                core.add_custom_probe(self.repo, cname.text().strip(), ccmd.text().strip(), grp.text().strip())
            if self.remote:
                self.repo.refresh_probes()      # first reading, on the sim
            else:
                self.repo.background_probes = False   # first reading now
            self.repo.ensure_baseline()
        finally:
            if not self.remote:
                self.repo.background_probes = True
            QApplication.restoreOverrideCursor()
        self.sync_dirty = True
        self.refresh_files()

    def refresh_probes_bg(self):
        if self.remote:
            return
        due = self.repo.probe_due()
        if not due or getattr(self, "_probing", False):
            return
        self._probing = True
        paths = [f.path for f in due]

        def work(r):
            out = {}
            for pth in paths:
                cmd = core.probe_command(r, pth)
                if cmd:
                    out[pth] = core.run_probe(cmd)
            return out

        def done(res):
            self._probing = False
            cache = getattr(self.repo, "_probe_cache", None)
            if cache is None:
                cache = self.repo._probe_cache = {}
            for pth, data in res.items():
                if data is not None:
                    cache[pth] = (core.now_ts(), data)
                elif pth in cache:
                    cache[pth] = (core.now_ts(), cache[pth][1])

        def fail(e):
            self._probing = False
        self.run_bg(work, done, fail)

    def edit_file(self):
        f = self.current_file()
        if not f:
            return
        label, ok = QInputDialog.getText(self, "Edit", "Display name:", text=f.label)
        if not ok:
            return
        group, ok = QInputDialog.getItem(self, "Edit", "Group:", [f.group] + self.repo.groups(), 0, True)
        if not ok:
            return
        self.repo.update_file(f.id, label=label, group=group)
        self.sync_dirty = True
        self.refresh_files()

    def remove_files(self):
        ids = selected_data(self.files_table)
        if not ids:
            return
        if QMessageBox.question(self, APP_NAME, f"Stop tracking {len(ids)} file(s)?\n"
                                "The files themselves are not touched and their history is kept.") \
                == QMessageBox.Yes:
            for i in ids:
                self.repo.remove_file(i)
            self.sync_dirty = True
            self.refresh_files()

    def snapshot_now(self):
        msg, ok = QInputDialog.getText(self, "Snapshot", "What did you change? (optional note)")
        if not ok:
            return
        snap = self.repo.commit(msg, kind="manual")
        if snap is None:
            if QMessageBox.question(self, APP_NAME, "Nothing changed since the last snapshot. Record one anyway?") \
                    == QMessageBox.Yes:
                self.repo.commit(msg or "Manual snapshot", kind="manual", force=True)
        self.sync_dirty = True
        self.refresh_all()

    def diff_file_with_last(self):
        f = self.current_file()
        if not f:
            return
        old = self.repo.get_object(self.repo.last_hash(f.id))
        new = self.repo.read_current(f)
        self.file_diff(f, old, new, "last snapshot", "current file", f"{f.display}: last snapshot → now")

    def diff_version_prev(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        _sid, h, prev = v
        self.file_diff(f, self.repo.get_object(prev), self.repo.get_object(h), "previous version",
                       f"snapshot #{v[0]}", f"{f.display}: changes in snapshot #{v[0]}")

    def diff_version_current(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        self.file_diff(f, self.repo.get_object(v[1]), self.repo.read_current(f), f"snapshot #{v[0]}",
                       "current file", f"{f.display}: snapshot #{v[0]} → current file")

    def restore_version(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        if v[1] is None:
            QMessageBox.information(self, APP_NAME, "The file did not exist in that version.")
            return
        if QMessageBox.question(self, APP_NAME, f"Overwrite {f.path}\nwith the version from snapshot #{v[0]}?"
                                ) != QMessageBox.Yes:
            return
        try:
            self.repo.restore_version(f.id, v[1], f"snapshot #{v[0]}")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Could not restore: {e}")
        self.sync_dirty = True
        self.refresh_files()

    def save_version_as(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f or v[1] is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save copy", f"{f.display}.snapshot{v[0]}")
        if path:
            Path(path).write_bytes(self.repo.get_object(v[1]))

    def open_location(self):
        f = self.current_file()
        if f and self.remote:
            QMessageBox.information(self, APP_NAME, f"This file is on the sim PC:\n{f.path}")
            return
        if f and not core.is_probe(f):
            open_in_explorer(f.path)

    # ================================================================= history tab
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

    # ---------------- certification archive
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

    # ================================================================= alerts tab
    def build_alerts_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(QLabel("<b>Alert rules</b> — get a Windows notification (and optionally auto-undo) when a "
                             "file changes in a specific way."))
        bar = QHBoxLayout()
        for text, fn in (("Add rule…", lambda: self.add_rule()), ("Edit…", self.edit_rule),
                         ("Enable / disable", self.toggle_rule), ("Delete", self.delete_rule)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch()
        lay.addLayout(bar)
        self.rule_table = QTableWidget()
        setup_table(self.rule_table, ["On", "Name", "File", "Trigger", "Pattern", "Expected", "Action"], 3)
        self.rule_table.doubleClicked.connect(lambda _i: self.edit_rule())
        lay.addWidget(self.rule_table, 2)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Ignored settings</b> — values that change by themselves (e.g. P3D rewrites window "
                             "positions on exit). Changes to only these don't count as changes."))
        row.addStretch()
        for text, fn in (("Add…", self.add_ignore_dialog), ("Remove", self.remove_ignore)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.ign_table = QTableWidget()
        setup_table(self.ign_table, ["File", "Type", "Pattern", "Why"], 3)
        lay.addWidget(self.ign_table, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Event log</b>"))
        row.addStretch()
        b = QPushButton("Clear log")
        b.clicked.connect(self.clear_log)
        row.addWidget(b)
        lay.addLayout(row)
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
        choice, ok = QInputDialog.getItem(self, "Ignore", "Which file?", names, 0, False)
        if not ok:
            return
        fid = None if choice == names[0] else files[names.index(choice) - 1].id
        kinds = list(core.IGNORE_KINDS.items())
        k, ok = QInputDialog.getItem(self, "Ignore", "What kind of setting?", [v for _k, v in kinds], 0, False)
        if not ok:
            return
        kind = kinds[[v for _k, v in kinds].index(k)][0]
        pattern, ok = QInputDialog.getText(self, "Ignore", f"Pattern — {k}:",
                                           text={"ini_key": "Section/Key", "ini_section": "Window.*",
                                                 "xml": ".//Window", "line": "^LastUsed="}[kind])
        if not ok or not pattern.strip():
            return
        note, _ = QInputDialog.getText(self, "Ignore", "Why? (optional)")
        self.repo.add_ignore(fid, kind, pattern.strip(), note)
        self.refresh_alerts()

    def remove_ignore(self):
        for i in selected_data(self.ign_table):
            self.repo.delete_ignore(i)
        self.refresh_alerts()

    def refresh_alerts(self):
        self.refresh_ignores()
        rules = self.repo.rules()
        self.rule_table.setRowCount(len(rules))
        for r, ru in enumerate(rules):
            f = self.repo.file(ru.file_id) if ru.file_id else None
            self.rule_table.setItem(r, 0, item("✔" if ru.enabled else "–", ru.id,
                                               "#2e7d32" if ru.enabled else "#999"))
            self.rule_table.setItem(r, 1, item(ru.name))
            self.rule_table.setItem(r, 2, item(f.display if f else "(all files)"))
            self.rule_table.setItem(r, 3, item(RULE_KINDS.get(ru.kind, ru.kind)))
            self.rule_table.setItem(r, 4, item(ru.pattern))
            self.rule_table.setItem(r, 5, item(ru.expected))
            self.rule_table.setItem(r, 6, item("auto-undo" if ru.action == "revert" else "notify",
                                               color="#c62828" if ru.action == "revert" else None))
        ev = self.repo.events(500)
        self.log_table.setRowCount(len(ev))
        col = {"alert": "#c62828", "warning": "#e65100"}
        for r, e in enumerate(ev):
            self.log_table.setItem(r, 0, item(fmt_ts(e["ts"])))
            self.log_table.setItem(r, 1, item(e["level"], color=col.get(e["level"])))
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
            self.tabs.setCurrentWidget(self.tab_alerts)
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
            self.refresh_alerts()

    def delete_rule(self):
        r = self.selected_rule()
        if r and QMessageBox.question(self, APP_NAME, f"Delete rule '{r.name}'?") == QMessageBox.Yes:
            self.repo.delete_rule(r.id)
            self.sync_dirty = True
            self.refresh_alerts()

    def clear_log(self):
        if QMessageBox.question(self, APP_NAME, "Clear the event log?") == QMessageBox.Yes:
            self.repo.clear_events()
            self.refresh_alerts()

    # ================================================================= sync tab
    def build_sync_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        box = QGroupBox("Where to share and back up (all sims use the same place)")
        bl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.sync_mode = QComboBox()
        self.sync_mode.addItem("Off", "")
        self.sync_mode.addItem("Shared folder (Google Drive for Desktop, network share, …)", "folder")
        self.sync_mode.addItem("Google Drive — direct (no Drive app needed)", "gdrive")
        row.addWidget(QLabel("Use:"))
        row.addWidget(self.sync_mode, 1)
        bl.addLayout(row)

        self.folder_box = QWidget()
        fl = QHBoxLayout(self.folder_box)
        fl.setContentsMargins(0, 0, 0, 0)
        self.sync_edit = QLineEdit(self.repo.get("sync_folder"))
        self.sync_edit.setPlaceholderText(r"e.g. G:\My Drive\SimConfigManager  or  \\SERVER\SimConfig")
        fl.addWidget(QLabel("Folder:"))
        fl.addWidget(self.sync_edit, 1)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_sync)
        fl.addWidget(b)
        bl.addWidget(self.folder_box)

        self.drive_box = QWidget()
        dl = self.drive_form = QFormLayout(self.drive_box)
        dl.setContentsMargins(0, 0, 0, 0)
        self.drive_auth = QComboBox()
        self.drive_auth.addItem("Service account on a Shared Drive (recommended — no login, never expires)",
                                "service_account")
        self.drive_auth.addItem("Google account (sign in once in the browser)", "user")
        dl.addRow("Sign in with:", self.drive_auth)
        self.sa_row = QWidget()
        sl = QHBoxLayout(self.sa_row)
        sl.setContentsMargins(0, 0, 0, 0)
        self.sa_label = QLabel()
        self.sa_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self.sa_label, 1)
        b = QPushButton("Choose key file (.json)…")
        b.clicked.connect(self.choose_sa_key)
        sl.addWidget(b)
        dl.addRow("Service account:", self.sa_row)
        self.user_row = QWidget()
        ul = QHBoxLayout(self.user_row)
        ul.setContentsMargins(0, 0, 0, 0)
        self.user_label = QLabel()
        ul.addWidget(self.user_label, 1)
        b = QPushButton("Choose OAuth client file…")
        b.clicked.connect(self.choose_oauth_client)
        ul.addWidget(b)
        b = QPushButton("Sign in…")
        b.clicked.connect(self.drive_sign_in)
        ul.addWidget(b)
        dl.addRow("Google account:", self.user_row)
        self.drive_folder = QLineEdit(self.repo.get("gdrive_folder_id"))
        self.drive_folder.setPlaceholderText("Paste the folder's link from drive.google.com (or its ID)")
        dl.addRow("Folder:", self.drive_folder)
        bl.addWidget(self.drive_box)

        row = QHBoxLayout()
        b = QPushButton("Save && test connection")
        b.clicked.connect(self.save_sync)
        row.addWidget(b)
        self.auto_push_cb = QCheckBox("Upload automatically (every minute when something changed)")
        self.auto_push_cb.setChecked(self.repo.get("auto_push") == "1")
        self.auto_push_cb.toggled.connect(lambda v: self.repo.set("auto_push", "1" if v else "0"))
        row.addWidget(self.auto_push_cb)
        row.addStretch()
        b = QPushButton("Upload now")
        b.clicked.connect(self.push_now)
        row.addWidget(b)
        bl.addLayout(row)
        self.sync_status = QLabel()
        self.sync_status.setWordWrap(True)
        self.sync_status.setTextFormat(Qt.RichText)
        bl.addWidget(self.sync_status)
        lay.addWidget(box)
        mode = self.repo.get("sync_mode") or ("folder" if self.repo.get("sync_folder") else "")
        self.sync_mode.setCurrentIndex(max(0, self.sync_mode.findData(mode)))
        self.drive_auth.setCurrentIndex(max(0, self.drive_auth.findData(self.repo.get("gdrive_auth")
                                                                         or "service_account")))
        self.sync_mode.currentIndexChanged.connect(lambda _i: self.update_sync_widgets())
        self.drive_auth.currentIndexChanged.connect(lambda _i: self.update_sync_widgets())
        self.update_sync_widgets()

        hub = QGroupBox("Central dashboard (optional) — report this sim's status to a SimConfigManager Hub web page")
        hl = QFormLayout(hub)
        self.hub_enable = QCheckBox("Report to a central dashboard")
        self.hub_enable.setChecked(self.repo.get("hub_enabled") == "1")
        hl.addRow("", self.hub_enable)
        self.hub_url = QLineEdit(self.repo.get("hub_url"))
        self.hub_url.setPlaceholderText("Address of your hub, e.g. https://hub.example.com or http://192.168.10.5:8780")
        hl.addRow("Hub address", self.hub_url)
        self.hub_key = QLineEdit(self.repo.get("hub_key"))
        self.hub_key.setEchoMode(QLineEdit.Password)
        self.hub_key.setPlaceholderText("Key for this sim (hub → Sims & settings → Add sim)")
        hl.addRow("Hub key", self.hub_key)
        row = QHBoxLayout()
        b = QPushButton("Save && send now")
        b.clicked.connect(self.save_hub)
        row.addWidget(b)
        self.hub_status = QLabel()
        self.hub_status.setTextFormat(Qt.RichText)
        self.hub_status.setWordWrap(True)
        row.addWidget(self.hub_status, 1)
        holder = QWidget()
        holder.setLayout(row)
        hl.addRow("", holder)
        hint = QLabel("Sends a status summary (versions, which files differ, rulebook, alerts, system info — never file "
                      "contents) every 5 minutes and right after changes. The hub is a separate small web server you "
                      "host yourself; see the README. Leave this off if you don't use one.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#555")
        hl.addRow("", hint)
        lay.addWidget(hub)
        self.update_hub_status()

        box2 = QGroupBox("Other sims (from the shared location)")
        b2 = QVBoxLayout(box2)
        row = QHBoxLayout()
        self.machine_combo = QComboBox()
        self.machine_combo.currentIndexChanged.connect(lambda _i: self.refresh_remote_files())
        row.addWidget(QLabel("PC:"))
        row.addWidget(self.machine_combo, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_sync)
        row.addWidget(b)
        b2.addLayout(row)
        self.remote_table = QTableWidget()
        setup_table(self.remote_table, ["Name", "Group", "Path on that PC"], 2)
        b2.addWidget(self.remote_table)
        row = QHBoxLayout()
        for text, fn in (("Compare with a file on this PC…", self.compare_remote),
                         ("Copy to this PC…", self.copy_remote),
                         ("Rebuild this PC's history from this backup…", self.import_remote)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        b2.addLayout(row)
        lay.addWidget(box2, 1)
        return w

    def sync(self):
        """The configured sync location (folder or Google Drive), or None."""
        try:
            s = core.make_sync(self.repo)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Sync not available: {e}")
            return None
        return s

    def update_sync_widgets(self):
        mode = self.sync_mode.currentData()
        self.folder_box.setVisible(mode == "folder")
        self.drive_box.setVisible(mode == "gdrive")
        auth = self.drive_auth.currentData()
        try:
            st = self.repo.drive_file_status()
        except Exception:  # noqa: BLE001
            st = {"service_account": "", "client_secrets": False, "token": False}
        self.drive_form.setRowVisible(self.sa_row, auth == "service_account")
        self.drive_form.setRowVisible(self.user_row, auth == "user")
        self.drive_folder.setEnabled(auth == "service_account")
        if st["service_account"]:
            email = st["service_account"]
            self.sa_label.setText(f"<b>{html.escape(email)}</b> — add this address as <i>Content manager</i> of "
                                  "the Shared Drive.")
        else:
            self.sa_label.setText("No key file yet. Create a service account in Google Cloud and download a JSON "
                                  "key (see README).")
        if st["token"]:
            self.user_label.setText("<span style='color:#2e7d32'>Signed in.</span> The app keeps its files in a "
                                    "'SimConfigManager' folder in that account's Drive.")
        elif st["client_secrets"]:
            self.user_label.setText("OAuth client set. Click Sign in.")
        else:
            self.user_label.setText("Needs an OAuth client file (Desktop app) from Google Cloud, then sign in.")

    def choose_sa_key(self):
        p, _ = QFileDialog.getOpenFileName(self, "Service account key", "", "JSON key (*.json)")
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            assert data.get("type") == "service_account" and data.get("client_email")
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, "That is not a service account key file.")
            return
        self.repo.save_drive_file("service_account", json.dumps(data))
        self.update_sync_widgets()
        QMessageBox.information(self, APP_NAME, f"Key saved. In Google Drive, open the Shared Drive → Manage members "
                                f"→ add\n\n{data['client_email']}\n\nas Content manager. Then paste the link of "
                                "the folder to use and click Save & test.")

    def choose_oauth_client(self):
        p, _ = QFileDialog.getOpenFileName(self, "OAuth client (Desktop app)", "", "JSON (*.json)")
        if not p:
            return
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            assert "installed" in data
        except Exception:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, "That is not an OAuth client file of type 'Desktop app'.")
            return
        self.repo.save_drive_file("client_secrets", json.dumps(data))
        self.update_sync_widgets()

    def drive_sign_in(self):
        from . import storage
        if self.remote:
            QMessageBox.information(self, APP_NAME, "Signing in to Google opens a browser on the sim PC itself, so "
                                    "do this step at the sim (or use the service-account option, which needs no "
                                    "sign-in).")
            return
        files = core.drive_files(self.repo)
        if not files["client_secrets"].exists():
            QMessageBox.information(self, APP_NAME, "First choose the OAuth client file.")
            return
        QMessageBox.information(self, APP_NAME, "Your browser opens now. Sign in with the Google account for the "
                                "sims and allow access. Come back here afterwards.")
        try:
            storage.user_sign_in(str(files["client_secrets"]), str(files["token"]))
            prov = storage.user_token_provider(str(files["token"]))
            folder_id = storage.ensure_user_root(prov)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Sign-in failed: {e}")
            return
        self.repo.set("gdrive_folder_id", folder_id)
        self.drive_folder.setText(folder_id)
        self.update_sync_widgets()
        self.save_sync()

    def browse_sync(self):
        d = pick_folder(self, self.repo, "Sync folder", self.sync_edit.text())
        if d:
            self.sync_edit.setText(d if self.remote else os.path.normpath(d))

    def save_sync(self):
        mode = self.sync_mode.currentData()
        self.repo.set("sync_mode", mode or "off")
        self.repo.set("sync_folder", self.sync_edit.text().strip() if mode == "folder" else
                      (self.repo.get("sync_folder") if mode != "gdrive" else self.repo.get("sync_folder")))
        if mode == "gdrive":
            self.repo.set("gdrive_auth", self.drive_auth.currentData())
            if self.drive_auth.currentData() == "service_account":
                self.repo.set("gdrive_folder_id", core.parse_drive_folder(self.drive_folder.text()))
        if not mode:
            self.sync_status.setText("Sharing is off.")
            return
        s = self.sync()
        if s is None:
            self.sync_status.setText("<span style='color:#c62828'>Not complete yet — see the fields above.</span>")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            if hasattr(s.backend, "check"):
                name = s.backend.check()
                self.sync_status.setText(f"<span style='color:#2e7d32'>✔ Connected to Google Drive folder "
                                         f"<b>{html.escape(name)}</b>.</span> Uploading…")
            elif not s.ok:
                raise FileNotFoundError(f"Folder not reachable: {s.where}")
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            self.sync_status.setText(f"<span style='color:#c62828'>✖ {html.escape(str(e))}</span>")
            return
        QApplication.restoreOverrideCursor()
        self.sync_dirty = True
        self.push_now()

    def run_bg(self, fn, done=None, err=None):
        """Run fn(bg_repo) on the background worker (its own read-only store connection); callbacks on the UI."""
        import concurrent.futures
        if not hasattr(self, "_bg_pool"):
            self._bg_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="scm-bg")
            self._bg_local = threading.local()
        data_dir = self.repo.data_dir
        remote_repo = self.repo if self.remote else None

        def job():
            if remote_repo is not None:
                return fn(remote_repo)
            r = getattr(self._bg_local, "repo", None)
            if r is None:
                r = self._bg_local.repo = Repo(data_dir, read_only=True)
            return fn(r)
        fut = self._bg_pool.submit(job)

        def poll():
            if not fut.done():
                QTimer.singleShot(150, poll)
                return
            e = fut.exception()
            if e is not None:
                (err or (lambda x: self.statusBar().showMessage(f"Sync: {x}")))(e)
            elif done:
                done(fut.result())
        QTimer.singleShot(150, poll)

    # ---------------- central dashboard (hub)
    def update_hub_status(self):
        if not hasattr(self, "hub_status"):
            return
        if self.repo.get("hub_enabled") != "1":
            self.hub_status.setText("<span style='color:#777'>Off.</span>")
            return
        ok = float(self.repo.get("hub_last_ok") or 0)
        err = self.repo.get("hub_last_error")
        if err:
            self.hub_status.setText(f"<span style='color:#c62828'>✖ {html.escape(err)}</span>")
        elif ok:
            self.hub_status.setText(f"<span style='color:#2e7d32'>✔ Last report {fmt_ts(ok)}</span>")
        else:
            self.hub_status.setText("Waiting for the first report…")

    def save_hub(self):
        self.repo.set("hub_enabled", "1" if self.hub_enable.isChecked() else "0")
        self.repo.set("hub_url", self.hub_url.text().strip())
        self.repo.set("hub_key", self.hub_key.text().strip())
        self.repo.set("hub_last_error", "")
        if not self.hub_enable.isChecked():
            self.update_hub_status()
            return
        if self.remote:
            self.hub_status.setText("Saved. The sim itself sends the reports.")
            return
        self.hub_tick(force=True)

    def hub_tick(self, force: bool = False):
        if self.remote or self.repo.get("hub_enabled") != "1" or getattr(self, "_hub_busy", False):
            return
        try:
            sig = self.repo.hub_signature()
        except Exception:  # noqa: BLE001
            return
        last = float(self.repo.get("hub_last_try") or 0)
        due = force or sig != getattr(self, "_hub_sig", None) or core.now_ts() - last >= 300
        if not due:
            return
        self._hub_busy = True
        self.repo.set("hub_last_try", core.now_ts())
        url, key = self.repo.get("hub_url"), self.repo.get("hub_key")
        cache = getattr(self.repo, "_probe_cache", None)

        def work(r):
            if cache is not None:
                r._probe_cache = cache          # reuse the app's system-info readings
                r.background_probes = True
            return core.hub_push(r, url, key)

        def ok(res):
            self._hub_busy = False
            self._hub_sig = sig
            self.repo.set("hub_last_ok", core.now_ts())
            self.repo.set("hub_last_error", "")
            self.update_hub_status()
            if force:
                self.statusBar().showMessage(f"Reported to the hub as '{res.get('sim', '?')}'", 6000)

        def fail(e):
            self._hub_busy = False
            self.repo.set("hub_last_error", str(e))
            self.update_hub_status()
        self.run_bg(work, ok, fail)

    def auto_push(self):
        if self.remote:
            return  # the sim uploads by itself
        if not (self.repo.get("sync_mode") not in ("", "off") and (self.repo.get("sync_folder") or
                                                                   self.repo.get("sync_mode") == "gdrive")):
            return
        now = core.now_ts()
        if self.repo.get("auto_push") == "1" and self.sync_dirty and not getattr(self, "_pushing", False):
            self._pushing = True
            self.sync_dirty = False

            def ok(res):
                self._pushing = False
                self.repo.set("last_push", core.now_ts())
                self._last_status = core.now_ts()

            def fail(e):
                self._pushing = False
                self.sync_dirty = True
                self.statusBar().showMessage(f"Upload failed: {e}")
            self.run_bg(lambda r: core.make_sync(r).push(), ok, fail)
        elif now - getattr(self, "_last_status", 0) > 300 and not getattr(self, "_pushing", False):
            self._last_status = now   # fleet heartbeat every 5 minutes
            self.run_bg(lambda r: core.make_sync(r).push_status(), None, lambda e: None)

    def push_now(self):
        if self.sync() is None:
            QMessageBox.warning(self, APP_NAME, "No sharing location is set up yet.")
            return
        self.sync_status.setText(self.sync_status.text().replace(" Uploading…", "") + " Uploading…")

        def ok(res):
            self.sync_dirty = False
            self.repo.set("last_push", core.now_ts())
            self.statusBar().showMessage(f"Uploaded: {res['objects_copied']} new file version(s)")
            self.refresh_sync()

        def fail(e):
            QMessageBox.warning(self, APP_NAME, f"Upload failed: {e}")
            self.refresh_sync()
        self.run_bg(lambda r: core.make_sync(r).push(), ok, fail)

    def refresh_sync(self):
        last = self.repo.get("last_push")
        s = self.sync()
        mode = self.repo.get("sync_mode") or ("folder" if self.repo.get("sync_folder") else "")
        if mode in ("", "off"):
            self.sync_status.setText("Sharing is off.")
            s = None
        elif not s:
            self.sync_status.setText("<span style='color:#c62828'>Not complete or not reachable right now.</span>")
        else:
            self.sync_status.setText(f"Using <b>{html.escape(s.where)}</b> · last upload: "
                                     f"{fmt_ts(float(last)) if last else 'never'}"
                                     + ("  ·  changes waiting" if self.sync_dirty else ""))
        cur = self.machine_combo.currentData()
        self.machine_combo.blockSignals(True)
        self.machine_combo.clear()
        if s:
            for m in s.machines():
                me = " (this PC)" if m["machine"] == self.repo.machine else ""
                self.machine_combo.addItem(f"{m['machine']}{me} — {m['files']} files, "
                                           f"updated {fmt_ts(m['updated'])}", m["machine"])
        i = self.machine_combo.findData(cur)
        self.machine_combo.setCurrentIndex(max(i, 0))
        self.machine_combo.blockSignals(False)
        self.refresh_remote_files()

    def refresh_remote_files(self):
        self.remote_table.setRowCount(0)
        s = self.sync()
        m = self.machine_combo.currentData()
        if not s or not m:
            return
        try:
            files = [f for f in s.remote_latest(m) if f.get("active", True)]
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Could not read {m}: {e}")
            return
        self.remote_table.setRowCount(len(files))
        for r, f in enumerate(files):
            self.remote_table.setItem(r, 0, item(f.get("label") or os.path.basename(f["path"]), f))
            self.remote_table.setItem(r, 1, item(f.get("group", "")))
            self.remote_table.setItem(r, 2, item(f["path"] + ("   (missing)" if f["hash"] is None else "")))

    def selected_remote(self):
        d = selected_data(self.remote_table)
        return d[0] if d else None

    def pick_local_target(self, rf) -> Optional[str]:
        files = self.repo.files()
        names = [f"{f.display}  [{f.group}]  {f.path}" for f in files] + ["Other location…"]
        # pre-select a file with the same name
        default = next((i for i, f in enumerate(files) if f.display == rf.get("label")), 0)
        choice, ok = QInputDialog.getItem(self, "Target on this PC", "Which file on THIS PC?", names, default, False)
        if not ok:
            return None
        if choice == "Other location…":
            if self.remote:
                folder = pick_folder(self, self.repo, "Folder on the sim to save to")
                return (folder.rstrip("\\/") + "\\" + rf["path"].replace("/", "\\").split("\\")[-1]) \
                    if folder else None
            p, _ = QFileDialog.getSaveFileName(self, "Save to", os.path.basename(rf["path"]))
            return p or None
        return files[names.index(choice)].path

    def compare_remote(self):
        rf, s = self.selected_remote(), self.sync()
        if not rf or not s or rf["hash"] is None:
            return
        target = self.pick_local_target(rf)
        if not target:
            return
        try:
            remote = s.fetch(rf["hash"])
        except FileNotFoundError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        lf = self.repo.find_file_by_path(target)
        mine = self.repo.read_current(lf) if lf else self.repo.read_path(target)
        DiffDialog(f"{self.machine_combo.currentData()} → this PC",
                   unified_diff(remote, mine, f"{self.machine_combo.currentData()}", "this PC"), self,
                   summary=core.setting_changes(remote, mine, target)).exec()

    def copy_remote(self):
        rf, s = self.selected_remote(), self.sync()
        if not rf or not s or rf["hash"] is None:
            return
        target = self.pick_local_target(rf)
        if not target:
            return
        if QMessageBox.question(self, APP_NAME, f"Overwrite\n{target}\nwith the version from "
                                f"{self.machine_combo.currentData()}?\n(A safety snapshot is taken first.)") \
                != QMessageBox.Yes:
            return
        try:
            data = s.fetch(rf["hash"])
            f = self.repo.find_file_by_path(target)
            if f and f.active:
                self.repo.write_file(f, data, f"copied from {self.machine_combo.currentData()}")
            else:
                self.repo.write_path(target, data)
            self.repo.log(f"Copied {rf.get('label')} from {self.machine_combo.currentData()} to {target}")
            self.sync_dirty = True
            self.statusBar().showMessage("Copied.")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Copy failed: {e}")

    def import_remote(self):
        s, m = self.sync(), self.machine_combo.currentData()
        if not s or not m:
            return
        if self.repo.snapshots(1):
            QMessageBox.information(self, APP_NAME,
                                    "This only works on a fresh install (no history yet). It is meant for a "
                                    "rebuilt or replacement PC: install the app, set the sync folder, then use "
                                    "this button to get back all files, history, restore points and rules.")
            return
        if QMessageBox.question(self, APP_NAME, f"Import all history, restore points and rules from '{m}'?") \
                == QMessageBox.Yes:
            try:
                res = s.import_machine(m)
                QMessageBox.information(self, APP_NAME, f"Imported {res['files']} files and {res['snapshots']} "
                                        "snapshots. Use Quick restore to put the configs back on disk.")
            except Exception as e:  # noqa: BLE001
                QMessageBox.warning(self, APP_NAME, f"Import failed: {e}")
            self.refresh_all()



    # ================================================================= rulebook tab (#6)
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

    # ================================================================= fleet tab (#4)
    def build_fleet_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.fleet_banner = QLabel()
        self.fleet_banner.setTextFormat(Qt.RichText)
        self.fleet_banner.setWordWrap(True)
        top.addWidget(self.fleet_banner, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_fleet)
        top.addWidget(b)
        lay.addLayout(top)
        split = QSplitter(Qt.Vertical)
        self.fleet_table = QTableWidget()
        setup_table(self.fleet_table, ["Sim", "Online", "Config version", "Files differ", "Rulebook",
                                       "Alerts (24 h)", "Waiting for approval", "Last change", "Graphics driver",
                                       "App"], 8)
        self.fleet_table.itemSelectionChanged.connect(self.show_fleet_detail)
        split.addWidget(self.fleet_table)
        self.fleet_detail = QTextEdit()
        self.fleet_detail.setReadOnly(True)
        split.addWidget(self.fleet_detail)
        split.setSizes([360, 260])
        lay.addWidget(split, 1)
        self._fleet = []
        return w

    def refresh_fleet(self):
        local = core.build_status(self.repo)
        self._fleet = [local]
        self.render_fleet()
        if self.sync() is None:
            self.fleet_banner.setText("<b>Fleet overview</b><br>Only this PC is shown. Set up sharing (Cloud tab: a "
                                      "shared folder or Google Drive) on every sim to see them all here.")
            return
        self.fleet_banner.setText("<b>Fleet overview</b> — loading the other sims…")

        def done(statuses):
            others = [x for x in statuses if x.get("machine") != self.repo.machine]
            self._fleet = [local] + sorted(others, key=lambda x: x.get("machine", ""))
            self.render_fleet()

        def fail(e):
            self.fleet_banner.setText(f"<b>Fleet overview</b> — could not read the other sims: {html.escape(str(e))}")
        self.run_bg(lambda r: core.make_sync(r).statuses(), done, fail)

    def render_fleet(self):
        now = core.now_ts()
        rows = self._fleet
        problems = 0
        self.fleet_table.setRowCount(len(rows))
        for i, st in enumerate(rows):
            age = now - st.get("heartbeat", 0)
            me = st.get("machine") == self.repo.machine
            online = me or age < 900
            mism = st.get("mismatches", [])
            viol = st.get("rulebook_violations", [])
            ok = online and not mism and not viol
            problems += 0 if ok else 1
            ver = st.get("version")
            gpu = (st.get("system", {}).get("gpu", "") or "").split("\n")
            drv = next((ln.split(":", 1)[1].strip() for ln in gpu if "Driver version" in ln), "–")
            self.fleet_table.setItem(i, 0, item(("● " if ok else "▲ ") + st.get("machine", "?") +
                                               ("  (this PC)" if me else ""), st.get("machine"),
                                               "#2e7d32" if ok else "#e65100", bold=True))
            self.fleet_table.setItem(i, 1, item("yes" if online else f"no — last seen {fmt_ts(st.get('heartbeat', 0))}",
                                               color=None if online else "#c62828"))
            self.fleet_table.setItem(i, 2, item(f"v{ver['number']} – {ver['title']}" if ver else "no version yet",
                                               color=None if ver else "#777"))
            self.fleet_table.setItem(i, 3, item(len(mism) if mism else "✔ none", color="#e65100" if mism else
                                               "#2e7d32"))
            self.fleet_table.setItem(i, 4, item(f"{len(viol)} broken" if viol else "✔", color="#c62828" if viol else
                                               "#2e7d32"))
            self.fleet_table.setItem(i, 5, item(st.get("alerts_24h", 0)))
            self.fleet_table.setItem(i, 6, item(st.get("pending_proposals", 0) or ""))
            self.fleet_table.setItem(i, 7, item(fmt_ts(st["last_change"]) if st.get("last_change") else "–"))
            self.fleet_table.setItem(i, 8, item(drv))
            self.fleet_table.setItem(i, 9, item(st.get("app_version", "")))
        n = len(rows)
        if not self.fleet_banner.text().endswith("loading the other sims…") or n > 1:
            self.fleet_banner.setText(
                f"<b>Fleet overview</b> — {n} sim(s): " +
                ("<span style='color:#2e7d32'>all OK</span>" if not problems else
                 f"<span style='color:#e65100'>{problems} need attention</span>"))

    def show_fleet_detail(self):
        ids = selected_data(self.fleet_table)
        st = next((x for x in self._fleet if ids and x.get("machine") == ids[0]), None)
        if not st:
            self.fleet_detail.clear()
            return
        parts = [f"<h3 style='margin:0'>{html.escape(st.get('machine', ''))}</h3>",
                 f"<p style='color:#555'>Last heartbeat {fmt_ts(st.get('heartbeat', 0))} · app "
                 f"{html.escape(st.get('app_version', ''))} · {st.get('files', 0)} files tracked</p>"]
        if st.get("mismatches"):
            parts.append("<b>Files that differ from the version:</b><ul>" + "".join(
                f"<li>{html.escape(m['file'])} [{html.escape(m['group'])}] — "
                f"{html.escape(core.STATUS_TEXT.get(m['status'], m['status']))}</li>" for m in st["mismatches"]) + "</ul>")
        if st.get("rulebook_violations"):
            parts.append("<b style='color:#c62828'>Rulebook broken:</b><ul>" + "".join(
                f"<li>{html.escape(v)}</li>" for v in st["rulebook_violations"]) + "</ul>")
        if st.get("recent_alerts"):
            parts.append("<b>Recent alerts:</b><ul>" + "".join(
                f"<li>{fmt_ts(a['ts'])} — {html.escape(a['message'])}</li>" for a in st["recent_alerts"]) + "</ul>")
        for k, v in (st.get("system") or {}).items():
            parts.append(f"<b>{html.escape(core.BUILTIN_PROBES.get(k, (k,))[0])}</b><pre>{html.escape(v)}</pre>")
        self.fleet_detail.setHtml("".join(parts))

    # ================================================================= Claude tab
    def build_claude_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        intro = QLabel(
            "<h2 style='margin:0'>Let Claude read your configs</h2>"
            "<p>With the <b>Claude Desktop</b> app on this PC, Claude can read every tracked file, its history and "
            "versions, and the other sims' files (via the sync folder) — to troubleshoot problems and help dial in "
            "settings. Claude can also <b>propose</b> changes, but nothing is written until you approve it here; "
            "every applied change is recorded in History and can be undone.</p>"
            "<p style='color:#555'>Try asking Claude: <i>“Why do we get stutters on Sim 2 but not Sim 1?”</i> · "
            "<i>“Compare the ProSim configs of both sims”</i> · <i>“What changed since v3?”</i> · "
            "<i>“Suggest Prepar3D.cfg settings for an RTX 4080 at 3 projectors.”</i></p>")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.RichText)
        lay.addWidget(intro)

        box = self.claude_setup_box = QGroupBox("Setup")
        bl = QHBoxLayout(box)
        self.claude_status = QLabel()
        self.claude_status.setTextFormat(Qt.RichText)
        bl.addWidget(self.claude_status, 1)
        self.claude_connect_btn = QPushButton("Connect to Claude Desktop")
        self.claude_connect_btn.clicked.connect(self.toggle_claude_connection)
        bl.addWidget(self.claude_connect_btn)
        b = QPushButton("Show manual setup")
        b.clicked.connect(self.show_claude_manual)
        bl.addWidget(b)
        lay.addWidget(box)

        self.claude_allow = QCheckBox("Allow Claude to propose changes (you always approve before anything is written)")
        self.claude_allow.setChecked(self.repo.get("claude_proposals") != "0")
        self.claude_allow.toggled.connect(lambda v: self.repo.set("claude_proposals", "1" if v else "0"))
        lay.addWidget(self.claude_allow)

        # ---- network access (Claude Desktop on mechanics' laptops)
        nbox = QGroupBox("Network access — Claude on other computers on this network (e.g. MacBooks)")
        nl = QVBoxLayout(nbox)
        row = QHBoxLayout()
        self.net_enable = QCheckBox("Allow connections from the local network")
        self.net_enable.setChecked(self.repo.get("net_enabled") == "1")
        self.net_enable.toggled.connect(self.toggle_network)
        row.addWidget(self.net_enable)
        row.addWidget(QLabel("Port:"))
        self.net_port = QSpinBox()
        self.net_port.setRange(1024, 65535)
        self.net_port.setValue(int(self.repo.get("net_port") or net.DEFAULT_PORT))
        self.net_port.editingFinished.connect(self.change_net_port)
        row.addWidget(self.net_port)
        row.addStretch()
        nl.addLayout(row)
        self.net_status = QLabel()
        self.net_status.setTextFormat(Qt.RichText)
        self.net_status.setWordWrap(True)
        self.net_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        nl.addWidget(self.net_status)
        self.key_table = QTableWidget()
        setup_table(self.key_table, ["Person / computer", "Access", "Key created", "Last connected"], 0)
        self.key_table.setMaximumHeight(140)
        nl.addWidget(self.key_table)
        row = QHBoxLayout()
        for text, fn in (("Add person…", self.add_net_key), ("Remove access", self.remove_net_key),
                         ("Setup for a Mac…", lambda: self.show_net_setup("mac")),
                         ("Setup for a Windows laptop…", lambda: self.show_net_setup("windows")),
                         ("Setup for ChatGPT (via a Mac)…", lambda: self.show_net_setup("chatgpt")),
                         ("Setup for the remote app…", lambda: self.show_net_setup("app"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        nl.addLayout(row)
        lay.addWidget(nbox)
        self.net_server = None
        self._net_activity = []
        if self.remote:
            # Claude Desktop hookup and the network server belong to the sim PC itself
            self.claude_setup_box.hide()
            self.net_enable.setEnabled(False)
            self.net_port.setEnabled(False)
        elif self.net_enable.isChecked():
            QTimer.singleShot(500, lambda: self.start_network(quiet=True))

        row = QHBoxLayout()
        row.addWidget(QLabel("<b>Changes proposed by Claude</b>"))
        row.addStretch()
        for text, fn in (("Review…", self.review_selected_proposal), ("Copy files for Claude…", self.copy_for_claude)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.prop_table = QTableWidget()
        setup_table(self.prop_table, ["Status", "When", "From", "File", "Reason"], 4)
        self.prop_table.doubleClicked.connect(lambda _i: self.review_selected_proposal())
        lay.addWidget(self.prop_table, 1)
        self._seen_proposals = {p["id"] for p in core.list_proposals(self.repo, "pending")}
        QTimer.singleShot(1500, lambda: self.check_proposals(startup=True))
        return w

    # ---------------- network access
    def start_network(self, quiet: bool = False) -> bool:
        self.stop_network()
        port = self.net_port.value()
        srv = net.NetworkServer(self.repo.data_dir, port=port, on_activity=self._net_activity.append)
        srv.keys = net.load_keys(self.repo)
        try:
            srv.start()
        except OSError as e:
            if not quiet:
                QMessageBox.warning(self, APP_NAME, f"Could not open port {port}: {e}\nIs another program using it? "
                                    "Pick another port.")
            self.net_status.setText(f"<span style='color:#c62828'>Could not open port {port}: "
                                    f"{html.escape(str(e))}</span>")
            return False
        self.net_server = srv
        ok, msg = net.set_firewall_rule(port, True)
        if not ok and not quiet:
            QMessageBox.warning(self, APP_NAME, "The Windows Firewall rule could not be added (run the app as "
                                f"administrator), so other computers may be blocked:\n{msg}")
        self.repo.log(f"Network access for Claude switched on (port {port})")
        self.refresh_network()
        return True

    def stop_network(self):
        if self.net_server:
            self.net_server.stop()
            self.net_server = None

    def toggle_network(self, on: bool):
        self.repo.set("net_enabled", "1" if on else "0")
        if on:
            if not net.load_keys(self.repo):
                QMessageBox.information(self, APP_NAME, "Network access is on. Now click 'Add person…' to create an "
                                        "access key for each mechanic's laptop.")
            if not self.start_network():
                self.net_enable.setChecked(False)
        else:
            self.stop_network()
            net.set_firewall_rule(self.net_port.value(), False)
            self.repo.log("Network access for Claude switched off")
        self.refresh_network()

    def change_net_port(self):
        old = int(self.repo.get("net_port") or net.DEFAULT_PORT)
        new = self.net_port.value()
        if new == old:
            return
        self.repo.set("net_port", new)
        if self.net_server:
            net.set_firewall_rule(old, False)
            self.start_network()
            QMessageBox.information(self, APP_NAME, "Port changed — laptops need the new setup (Setup for a Mac…).")

    def net_urls(self) -> list[str]:
        port = self.net_port.value()
        if self.remote:
            return [f"{self.repo.base}/mcp"]
        hosts = net.lan_addresses() + [f"{net.hostname().lower()}.local"]
        return [f"http://{h}:{port}/mcp" for h in hosts]

    def refresh_network(self):
        self.update_statusbar()
        keys = net.load_keys(self.repo)
        seen = self.net_server.last_seen if self.net_server else {}
        if self.net_server:
            self.net_server.keys = keys
            urls = self.net_urls()
            self.net_status.setText(
                "<span style='color:#2e7d32'><b>✔ Listening.</b></span> Laptops connect to "
                + " or ".join(f"<b>{html.escape(u)}</b>" for u in urls[:2])
                + ". Only computers on this local network are let through the firewall, and each needs its own key. "
                  "Tip: give this PC a fixed IP address (DHCP reservation in the router) so the setup keeps working.")
        elif self.remote:
            self.net_status.setText("You are connected remotely. Network access is switched on/off at the sim PC; "
                                    "people and keys can be managed here.")
        elif self.net_enable.isChecked():
            pass
        else:
            self.net_status.setText("Off. Switch on to let Claude Desktop on a mechanic's laptop read this sim's "
                                    "configs (and propose changes you approve here).")
        self.key_table.setRowCount(len(keys))
        for r, k in enumerate(keys):
            ls = seen.get(k["name"])
            self.key_table.setItem(r, 0, item(k["name"], k["key"], bold=True))
            lvl = net.key_level(k)
            self.key_table.setItem(r, 1, item({"read": "Claude/ChatGPT read-only", "propose": "Claude/ChatGPT + propose",
                                               "full": "Full control (remote app)"}[lvl],
                                              color="#6a1b9a" if lvl == "full" else None, bold=lvl == "full"))
            self.key_table.setItem(r, 2, item(fmt_ts(k["created"])))
            self.key_table.setItem(r, 3, item(f"{fmt_ts(ls[0])} from {ls[1]}" if ls else "–"))

    def add_net_key(self):
        name, ok = QInputDialog.getText(self, "Add person", "Name of the person or laptop (e.g. 'Jan – MacBook Pro'):")
        if not ok or not name.strip():
            return
        if any(k["name"].lower() == name.strip().lower() for k in net.load_keys(self.repo)):
            QMessageBox.warning(self, APP_NAME, "That name already has a key.")
            return
        levels = list(net.ACCESS_LEVELS.items())
        choice, ok = QInputDialog.getItem(self, "Access", f"What may {name} do?", [v for _k, v in levels], 1, False)
        if not ok:
            return
        lvl = levels[[v for _k, v in levels].index(choice)][0]
        if lvl == "full" and QMessageBox.question(
                self, APP_NAME, f"Full control lets {name} change, restore and version every tracked file on this sim "
                "from another computer (everything is logged with their name). Continue?") != QMessageBox.Yes:
            return
        k = net.add_key(self.repo, name, lvl != "read", full_control=lvl == "full")
        self.repo.log(f"Network access key created for {k['name']} ({net.ACCESS_LEVELS[lvl]})")
        self.refresh_network()
        for r in range(self.key_table.rowCount()):
            if self.key_table.item(r, 0).text() == k["name"]:
                self.key_table.selectRow(r)
        if not self.net_enable.isChecked() and not self.remote:
            self.net_enable.setChecked(True)
        self.show_net_setup("app" if lvl == "full" else "mac")

    def remove_net_key(self):
        ids = selected_data(self.key_table)
        if not ids:
            return
        keys = net.load_keys(self.repo)
        k = next(k for k in keys if k["key"] == ids[0])
        if QMessageBox.question(self, APP_NAME, f"Remove access for {k['name']}? Their Claude can no longer "
                                "connect.") == QMessageBox.Yes:
            net.save_keys(self.repo, [x for x in keys if x["key"] != k["key"]])
            self.repo.log(f"Network access removed for {k['name']}")
            self.refresh_network()

    def show_net_setup(self, kind: str):
        keys = net.load_keys(self.repo)
        if not keys:
            QMessageBox.information(self, APP_NAME, "First click 'Add person…' to create a key.")
            return
        ids = selected_data(self.key_table)
        key = next((k for k in keys if ids and k["key"] == ids[0]), keys[-1])
        dlg = NetSetupDialog(self.repo.machine, key, self.net_urls(), kind, self)
        dlg.exec()

    def drain_net_activity(self):
        acts, self._net_activity[:] = list(self._net_activity), []
        for a in acts:
            self.repo.log(a)
        if acts and self.tabs.currentWidget() is self.tab_claude and self.isVisible():
            self.refresh_network()

    def refresh_claude(self):
        self.refresh_network()
        if self.remote:
            self.refresh_proposals_table()
            return
        connected = core.claude_desktop_connected()
        if connected:
            self.claude_status.setText("<span style='color:#2e7d32'><b>✔ Connected to Claude Desktop.</b></span> "
                                       "Restart Claude Desktop once after connecting. Look for "
                                       "<b>sim-config-manager</b> under the tools (🔨) menu in a chat.")
            self.claude_connect_btn.setText("Disconnect")
        else:
            self.claude_status.setText("Not connected. Install Claude Desktop (claude.ai/download) on this PC, then "
                                       "click <b>Connect</b>.")
            self.claude_connect_btn.setText("Connect to Claude Desktop")
        self.refresh_proposals_table()

    def refresh_proposals_table(self):
        props = list(reversed(core.list_proposals(self.repo)))
        colors = {"pending": "#e65100", "applied": "#2e7d32", "rejected": "#777"}
        self.prop_table.setRowCount(len(props))
        for r, p in enumerate(props):
            self.prop_table.setItem(r, 0, item(p["status"], p["id"], colors.get(p["status"]),
                                               bold=p["status"] == "pending"))
            self.prop_table.setItem(r, 1, item(fmt_ts(p["created"])))
            self.prop_table.setItem(r, 2, item(p.get("author", "Claude")))
            self.prop_table.setItem(r, 3, item(p["label"]))
            self.prop_table.setItem(r, 4, item(p["reason"].replace("\n", " ")[:200]))

    def toggle_claude_connection(self):
        enable = not core.claude_desktop_connected()
        try:
            paths = core.connect_claude_desktop(self.repo.data_dir, enable)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Could not update Claude Desktop's settings: {e}")
            return
        cmd, _args = core.connector_command(self.repo.data_dir)
        if enable and getattr(sys, "frozen", False) and not os.path.exists(cmd):
            QMessageBox.warning(self, APP_NAME, f"Put SimConfigManager-MCP.exe next to SimConfigManager.exe:\n{cmd}")
        QMessageBox.information(
            self, APP_NAME, ("Connected" if enable else "Disconnected") + ". Updated:\n" + "\n".join(paths) +
            "\n\nQuit Claude Desktop completely (also from its tray icon) and start it again.")
        self.refresh_claude()

    def show_claude_manual(self):
        cmd, args = core.connector_command(self.repo.data_dir)
        snippet = json.dumps({"mcpServers": {core.MCP_SERVER_KEY: {"command": cmd, "args": args}}}, indent=2)
        dlg = QDialog(self)
        dlg.setWindowTitle("Manual setup")
        dlg.resize(720, 360)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("In Claude Desktop: Settings → Developer → Edit Config, and add this to "
                             "claude_desktop_config.json (merge with what is there), then restart Claude Desktop:"))
        t = QTextEdit()
        t.setPlainText(snippet)
        t.setFont(theme.mono_font(10))
        lay.addWidget(t)
        b = QPushButton("Copy")
        b.clicked.connect(lambda: QApplication.clipboard().setText(snippet))
        lay.addWidget(b)
        dlg.exec()

    def check_proposals(self, startup: bool = False):
        try:
            pend = core.list_proposals(self.repo, "pending")
        except Exception:  # noqa: BLE001
            return
        seen = getattr(self, "_seen_proposals", set())
        new = [p for p in pend if p["id"] not in seen]
        self._seen_proposals = seen | {p["id"] for p in pend}
        if startup and pend:
            new = pend
        if new:
            p = new[-1]
            self.tray.showMessage(f"{p.get('author', 'Claude')} proposes a change to {p['label']}",
                                  p["reason"][:180] + "\n\nOpen Sim Config Manager → Claude to review.",
                                  QSystemTrayIcon.Information, 15000)
            if self.tabs.currentWidget() is self.tab_claude and self.isVisible():
                self.refresh_claude()
        self.tabs.setTabText(self.tabs.indexOf(self.tab_claude), f"Claude ({len(pend)})" if pend else "Claude")
        self.sync_activity()

    def review_selected_proposal(self):
        ids = selected_data(self.prop_table)
        pend = core.list_proposals(self.repo)
        prop = next((p for p in pend if ids and p["id"] == ids[0]), None)
        if prop is None:
            prop = next((p for p in reversed(pend) if p["status"] == "pending"), None)
        if prop is None:
            QMessageBox.information(self, APP_NAME, "There are no proposals to review.")
            return
        ProposalDialog(self.repo, prop, self).exec()
        self.sync_dirty = True
        self.check_proposals()
        self.refresh_all()

    def copy_for_claude(self):
        files = []
        if self.tabs.currentWidget() is self.tab_files:
            files = [self.repo.file(i) for i in selected_data(self.files_table)]
        if not files:
            names = [f"{f.display}  [{f.group}]" for f in self.repo.files()]
            dlg = QDialog(self)
            dlg.setWindowTitle("Copy files for Claude")
            dlg.resize(560, 480)
            lay = QVBoxLayout(dlg)
            lay.addWidget(QLabel("Tick the files to copy. Paste the result into any Claude chat "
                                 "(also on your phone or claude.ai)."))
            lst = QListWidget()
            for f, n in zip(self.repo.files(), names):
                it = QListWidgetItem(n)
                it.setData(Qt.UserRole, f.id)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
                lst.addItem(it)
            lay.addWidget(lst)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            lay.addWidget(bb)
            if dlg.exec() != QDialog.Accepted:
                return
            files = [self.repo.file(lst.item(i).data(Qt.UserRole)) for i in range(lst.count())
                     if lst.item(i).checkState() == Qt.Checked]
        files = [f for f in files if f]
        if not files:
            return
        md = core.files_as_markdown(self.repo, files)
        QApplication.clipboard().setText(md)
        self.statusBar().showMessage(f"Copied {len(files)} file(s) for Claude ({len(md):,} characters) — paste "
                                     "into a Claude chat.", 8000)

    # ================================================================= settings tab
    def build_settings_tab(self):
        w = QWidget()
        form = QFormLayout(w)
        self.machine_edit = QLineEdit(self.repo.machine)
        self.machine_edit.editingFinished.connect(self.save_machine)
        form.addRow("This PC's name (e.g. 'Sim 1')", self.machine_edit)
        self.poll_spin = QSpinBox()
        self.poll_spin.setRange(1, 3600)
        self.poll_spin.setSuffix(" s")
        self.poll_spin.setValue(int(self.repo.get("poll_seconds") or 5))
        self.poll_spin.valueChanged.connect(lambda v: (self.repo.set("poll_seconds", v), self.apply_poll_interval()))
        form.addRow("Check files every", self.poll_spin)
        cb = QCheckBox("Automatically record a snapshot whenever a file changes (recommended)")
        cb.setChecked(self.repo.get("auto_snapshot") == "1")
        cb.toggled.connect(lambda v: self.repo.set("auto_snapshot", "1" if v else "0"))
        form.addRow("", cb)
        cbv = QCheckBox("Notify me when files start to differ from the current version")
        cbv.setChecked(self.repo.get("notify_version_drift") != "0")
        cbv.toggled.connect(lambda v: self.repo.set("notify_version_drift", "1" if v else "0"))
        form.addRow("", cbv)
        self.autostart_cb = QCheckBox("Start with Windows as administrator (runs in the system tray)")
        self.autostart_cb.setChecked(False if self.remote else core.autostart_enabled())
        self.autostart_cb.setEnabled(sys.platform == "win32")
        self.autostart_cb.toggled.connect(self.toggle_autostart)
        if not self.remote:
            form.addRow("", self.autostart_cb)
        self.proc_edit = QLineEdit(self.repo.get("warn_processes"))
        self.proc_edit.editingFinished.connect(lambda: self.repo.set("warn_processes", self.proc_edit.text()))
        form.addRow("Warn before restoring while these run", self.proc_edit)
        if self.remote:
            form.addRow("Connected to", QLabel(f"<b>{html.escape(self.repo.machine)}</b> at "
                                               f"{html.escape(self.repo.base)} — {html.escape(self.host.get('os', ''))}"))
            form.addRow("Sim's app", QLabel(f"{self.host.get('app_version', '?')}"
                                            + ("" if self.host.get("admin") else "  (not running as administrator)")))
        else:
            row = QHBoxLayout()
            lbl = QLabel(str(self.repo.data_dir))
            row.addWidget(lbl)
            b = QPushButton("Open")
            b.clicked.connect(lambda: open_in_explorer(str(self.repo.data_dir)))
            row.addWidget(b)
            row.addStretch()
            holder = QWidget()
            holder.setLayout(row)
            form.addRow("Data folder", holder)
        adm = True if self.remote else core.is_admin()
        if not self.remote:
            form.addRow("Administrator rights", QLabel("<span style='color:#2e7d32'>yes</span>" if adm else
                                                       "<span style='color:#c62828'>no — restart the app and accept "
                                                       "the Windows prompt</span>"))
        b = QPushButton("Connect to another sim…")
        b.clicked.connect(self.connect_other_sim)
        form.addRow("Remote", b)
        if sys.platform == "win32" and not adm:
            self.autostart_cb.setEnabled(False)
            self.autostart_cb.setToolTip("Needs administrator rights to set up")
        form.addRow("", QLabel(f"{APP_NAME} {APP_VERSION}"))
        upd = QWidget()
        ul = QHBoxLayout(upd)
        ul.setContentsMargins(0, 0, 0, 0)
        b = QPushButton("Check for updates")
        b.clicked.connect(lambda: self.check_updates(manual=True))
        ul.addWidget(b)
        self.upd_daily = QCheckBox("Check daily")
        self.upd_daily.setChecked(self.pref("update_check") != "0")
        self.upd_daily.toggled.connect(lambda v: self.set_pref("update_check", "1" if v else "0"))
        ul.addWidget(self.upd_daily)
        ul.addStretch()
        form.addRow("Updates", upd)
        self.upd_token = QLineEdit(self.pref("github_token"))
        self.upd_token.setEchoMode(QLineEdit.Password)
        self.upd_token.setPlaceholderText("Only needed while the GitHub repository is private (read-only token)")
        self.upd_token.editingFinished.connect(lambda: self.set_pref("github_token", self.upd_token.text().strip()))
        form.addRow("GitHub access token", self.upd_token)
        self.theme_combo = QComboBox()
        for label, val in (("Dark (VS Code Dark Modern)", "dark"), ("Light (VS Code Light Modern)", "light"),
                           ("Follow the system setting", "system")):
            self.theme_combo.addItem(label, val)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(theme.saved_choice())))
        self.theme_combo.currentIndexChanged.connect(self.change_theme)
        form.addRow("Theme", self.theme_combo)
        self.probe_spin = QSpinBox()
        self.probe_spin.setRange(1, 1440)
        self.probe_spin.setSuffix(" min")
        self.probe_spin.setValue(int(float(self.repo.get("probe_minutes") or 15)))
        self.probe_spin.valueChanged.connect(lambda v: self.repo.set("probe_minutes", v))
        form.addRow("Re-read system info every", self.probe_spin)
        return w

    # preferences of THIS computer (in remote mode they must not go to the sim)
    def pref(self, key: str) -> str:
        if not self.remote:
            return self.repo.get(key)
        from .remote import client_config_dir
        try:
            return str(json.loads((client_config_dir() / "settings.json").read_text(encoding="utf-8")).get(key, ""))
        except (OSError, ValueError):
            return ""

    def set_pref(self, key: str, value) -> None:
        if not self.remote:
            self.repo.set(key, value)
            return
        from .remote import client_config_dir
        p = client_config_dir() / "settings.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[key] = str(value)
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")

    def change_theme(self, _i=None):
        theme.save_choice(self.theme_combo.currentData())
        QMessageBox.information(self, APP_NAME, "The new theme is used the next time the app starts.")

    def connect_other_sim(self):
        win = open_connect_dialog(self)
        if win is not None:
            win.show_window()

    def check_updates(self, manual: bool = False):
        from . import updater
        token = self.pref("github_token")

        def done(rel):
            self.set_pref("last_update_check", core.now_ts())
            if rel is None:
                if manual:
                    QMessageBox.information(self, APP_NAME, f"You have the latest version ({APP_VERSION}).")
                return
            if not manual:
                self.tray.showMessage(APP_NAME, f"Update available: {rel['tag']}. Open Settings → Check for updates.",
                                      QSystemTrayIcon.Information, 10000)
                return
            notes = rel["notes"][:1500] or "(no release notes)"
            if not updater.can_self_update():
                QMessageBox.information(self, APP_NAME, f"Version {rel['tag']} is available:\n{rel['url']}\n\n{notes}")
                return
            if QMessageBox.question(self, APP_NAME, f"Version {rel['tag']} is available (you have {APP_VERSION}).\n\n"
                                    f"{notes}\n\nDownload and install now? The app restarts by itself; your data "
                                    "and settings are kept.") != QMessageBox.Yes:
                return
            QApplication.setOverrideCursor(Qt.WaitCursor)
            try:
                path = updater.fetch_installer(rel, token)
            except Exception as e:  # noqa: BLE001
                QApplication.restoreOverrideCursor()
                QMessageBox.warning(self, APP_NAME, f"Update failed: {e}")
                return
            QApplication.restoreOverrideCursor()
            self.repo.log(f"Installing update {rel['tag']}")
            updater.run_installer(path)
            self.quit_app()

        def fail(e):
            if manual:
                QMessageBox.warning(self, APP_NAME, str(e))
        self.run_bg(lambda r: updater.check(APP_VERSION, updater.DEFAULT_REPO, token), done, fail)

    def daily_update_check(self):
        if self.repo.get("update_check") == "0":
            return
        last = float(self.repo.get("last_update_check") or 0)
        if core.now_ts() - last > 86400:
            self.set_pref("last_update_check", core.now_ts())
            self.check_updates(manual=False)

    def save_machine(self):
        name = self.machine_edit.text().strip()
        if name and name != self.repo.machine:
            self.repo.set("machine_name", name)
            self.setWindowTitle(f"{APP_NAME} — {name}")
            self.sync_dirty = True

    def toggle_autostart(self, on: bool):
        try:
            core.set_autostart(on)
        except OSError as e:
            QMessageBox.warning(self, APP_NAME, f"Could not change startup setting: {e}")



# --------------------------------------------------------------------------- remote: connect to a sim

_REMOTE_WINDOWS: list = []


class ConnectDialog(QDialog):
    """Pick (or add) a sim PC to work on over the network."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from . import remote
        self.remote = remote
        self.setWindowTitle("Connect to a sim")
        self.setWindowIcon(make_icon_remote())
        self.resize(620, 400)
        lay = QVBoxLayout(self)
        lab = QLabel("Work on a sim PC over the network, as if you were sitting at it. The sim needs network access "
                     "switched on and a <b>Full control</b> key for you (Claude tab → Network access → Add person).")
        lab.setWordWrap(True)
        lab.setTextFormat(Qt.RichText)
        lay.addWidget(lab)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self.connect())
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Add…", self.add), ("Add from file…", self.add_file), ("Remove", self.remove)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch()
        b = QPushButton("Connect")
        b.setDefault(True)
        b.setStyleSheet("QPushButton { background:#6a1b9a; color:white; font-weight:bold; padding:6px 18px; }")
        b.clicked.connect(self.connect)
        row.addWidget(b)
        lay.addLayout(row)
        self.window = None
        self.reload()

    def reload(self):
        self.list.clear()
        for c in self.remote.load_connections():
            it = QListWidgetItem(f"{c['name']}    —  {c['url']}")
            it.setData(Qt.UserRole, c)
            self.list.addItem(it)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _store(self, conn: dict):
        conns = [c for c in self.remote.load_connections() if c["name"] != conn["name"]]
        conns.append(conn)
        self.remote.save_connections(conns)
        self.reload()

    def add(self):
        name, ok = QInputDialog.getText(self, "Add sim", "Name (e.g. Sim 1):")
        if not ok or not name.strip():
            return
        url, ok = QInputDialog.getText(self, "Add sim", "Address of the sim PC (e.g. 192.168.10.11 or sim1.local):")
        if not ok or not url.strip():
            return
        key, ok = QInputDialog.getText(self, "Add sim", "Access key (from the sim's Claude tab → Setup for the "
                                       "remote app):", QLineEdit.Password)
        if not ok or not key.strip():
            return
        self._store({"name": name.strip(), "url": url.strip(), "key": key.strip()})

    def add_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Connection file", str(Path.home() / "Downloads"),
                                           "Connection (*.scmconnect);;All files (*)")
        if not p:
            return
        try:
            conn = self.remote.parse_connection_file(Path(p).read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        self._store(conn)

    def remove(self):
        it = self.list.currentItem()
        if it and QMessageBox.question(self, APP_NAME, f"Forget {it.data(Qt.UserRole)['name']}?") == QMessageBox.Yes:
            name = it.data(Qt.UserRole)["name"]
            self.remote.save_connections([c for c in self.remote.load_connections() if c["name"] != name])
            self.reload()

    def connect(self):
        it = self.list.currentItem()
        if not it:
            return
        c = it.data(Qt.UserRole)
        rr = self.remote.RemoteRepo(c["url"], c["key"], c["name"])
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rr.ping()
            win = MainWindow(rr)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, str(e))
            return
        QApplication.restoreOverrideCursor()
        _REMOTE_WINDOWS.append(win)
        self.window = win
        self.accept()


def open_connect_dialog(parent=None):
    dlg = ConnectDialog(parent)
    if dlg.exec() == QDialog.Accepted:
        return dlg.window
    return None
