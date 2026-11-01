"""SimConfigManager desktop UI (PySide6)."""
from __future__ import annotations

import html
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPixmap, QBrush, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QSystemTrayIcon, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
    QColorDialog, QScrollArea,
)

from . import core
from .core import (APP_NAME, APP_VERSION, FolderSync, Repo, Rule, RULE_HELP, RULE_KINDS, fmt_ts,
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


def item(text, data=None, color: Optional[str] = None, bold=False) -> QTableWidgetItem:
    it = QTableWidgetItem(str(text))
    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
    if data is not None:
        it.setData(Qt.UserRole, data)
    if color:
        it.setForeground(QBrush(QColor(color)))
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

class DiffDialog(QDialog):
    def __init__(self, title: str, diff_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 650)
        lay = QVBoxLayout(self)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
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
        default = os.path.join(repo.get("last_archive_dir") or str(Path.home() / "Documents"),
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
            tgt = arc.suggested_target(e)
            exists = os.path.exists(tgt)
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
        running = core.running_processes(repo.get("warn_processes").split(";"))
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


# --------------------------------------------------------------------------- main window

class MainWindow(QMainWindow):
    def __init__(self, repo: Repo, start_hidden: bool = False):
        super().__init__()
        self.repo = repo
        self.paused = False
        self.sync_dirty = False
        self.setWindowTitle(f"{APP_NAME} — {repo.machine}")
        self.setWindowIcon(make_icon())
        self.resize(1200, 760)

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.tab_versions = self.build_versions_tab()
        self.tab_quick = self.build_quick_tab()
        self.tab_files = self.build_files_tab()
        self.tab_history = self.build_history_tab()
        self.tab_alerts = self.build_alerts_tab()
        self.tab_sync = self.build_sync_tab()
        self.tab_settings = self.build_settings_tab()
        self.tabs.addTab(self.tab_versions, "Versions")
        self.tabs.addTab(self.tab_quick, "Quick restore")
        self.tabs.addTab(self.tab_files, "Files")
        self.tabs.addTab(self.tab_history, "History")
        self.tabs.addTab(self.tab_alerts, "Alerts")
        self.tabs.addTab(self.tab_sync, "Cloud / other sims")
        self.tabs.addTab(self.tab_settings, "Settings")
        self.tabs.currentChanged.connect(lambda _i: self.refresh_all())
        self.statusBar().showMessage("Ready")

        self.build_tray()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.apply_poll_interval()
        self.sync_timer = QTimer(self)
        self.sync_timer.timeout.connect(self.auto_push)
        self.sync_timer.start(60_000)

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
        if self.sync_dirty:
            self.auto_push()
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

    def auto_push(self):
        folder = self.repo.get("sync_folder")
        if not folder or self.repo.get("auto_push") != "1" or not self.sync_dirty:
            return
        try:
            FolderSync(self.repo, folder).push()
            self.sync_dirty = False
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Cloud sync failed: {e}")

    # ================================================================= refresh
    def refresh_all(self):
        cur = self.tabs.currentWidget()
        self.rebuild_tray_rp_menu()
        fn = {self.tab_versions: self.refresh_versions, self.tab_quick: self.refresh_quick,
              self.tab_files: self.refresh_files, self.tab_history: self.refresh_history,
              self.tab_alerts: self.refresh_alerts, self.tab_sync: self.refresh_sync}.get(cur)
        if fn:
            fn()
        self.update_version_indicator()

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
        self.setWindowTitle(f"{APP_NAME} — {self.repo.machine} — {txt}")
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
        DiffDialog(f"{x.file.display}: v{number} → now",
                   unified_diff(self.repo.get_object(x.version_hash), self.repo.read_current(x.file),
                                f"v{number}", "on disk now"), self).exec()

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
            self.repo.db.execute("UPDATE restore_points SET snapshot_id=?, created_at=? WHERE id=?",
                                 (snap.id, core.now_ts(), rp.id))
            self.repo.db.commit()
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
        running = core.running_processes(self.repo.get("warn_processes").split(";"))
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
                         ("Find P3D / ProSim configs…", self.discover), ("✎ Open in editor", self.edit_contents),
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
        m.addAction("Show changes since last snapshot", self.diff_file_with_last)
        m.addAction("Rename / change group…", self.edit_file)
        m.addAction("Open file location", self.open_location)
        m.addAction("Add alert rule for this file…", lambda: self.add_rule(self.current_file()))
        m.addSeparator()
        m.addAction("Stop tracking", self.remove_files)
        m.exec(self.files_table.viewport().mapToGlobal(pos))

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose config files to track")
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
        d = QFileDialog.getExistingDirectory(self, "Choose folder")
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
            cands = core.discover_sim_configs()
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
        DiffDialog(f"{f.display}: last snapshot → now",
                   unified_diff(old, new, "last snapshot", "current file"), self).exec()

    def diff_version_prev(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        _sid, h, prev = v
        DiffDialog(f"{f.display}: changes in snapshot #{v[0]}",
                   unified_diff(self.repo.get_object(prev), self.repo.get_object(h), "previous version",
                                f"snapshot #{v[0]}"), self).exec()

    def diff_version_current(self):
        v = self.current_version()
        f = self.current_file()
        if not v or not f:
            return
        DiffDialog(f"{f.display}: snapshot #{v[0]} → current file",
                   unified_diff(self.repo.get_object(v[1]), self.repo.read_current(f), f"snapshot #{v[0]}",
                                "current file"), self).exec()

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
        if f:
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
        DiffDialog(f"{f.display if f else fid}: snapshot #{self.selected_snapshot()}",
                   unified_diff(self.repo.get_object(old), self.repo.get_object(new), "before", "after"),
                   self).exec()

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
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = core.export_archive(self.repo, path, note, hist, snapshot_id)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            QMessageBox.warning(self, APP_NAME, f"Export failed: {e}")
            return
        QApplication.restoreOverrideCursor()
        self.repo.set("last_archive_dir", os.path.dirname(path))
        self.sync_dirty = True
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
        path, _ = QFileDialog.getOpenFileName(self, "Open SimConfigManager archive",
                                              self.repo.get("last_archive_dir"), "Archive (*.zip)")
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
                running = core.running_processes(self.repo.get("warn_processes").split(";"))
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
            self.repo.export_changelog(path, "csv" if path.lower().endswith(".csv") else "md")
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

    def refresh_alerts(self):
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
        box = QGroupBox("Sync folder (Google Drive, or a shared folder on your local server)")
        bl = QVBoxLayout(box)
        bl.addWidget(QLabel(
            "Pick a folder that Google Drive for Desktop syncs (e.g. <i>G:\\My Drive\\SimConfigManager</i>) "
            "or a network share on another PC (e.g. <i>\\\\SERVER\\SimConfig</i>). Use the SAME folder on every "
            "sim. Each PC only writes its own file, so the sims never conflict, and every config version is "
            "backed up off-site."))
        row = QHBoxLayout()
        self.sync_edit = QLineEdit(self.repo.get("sync_folder"))
        row.addWidget(self.sync_edit)
        b = QPushButton("Browse…")
        b.clicked.connect(self.browse_sync)
        row.addWidget(b)
        b = QPushButton("Save")
        b.clicked.connect(self.save_sync)
        row.addWidget(b)
        bl.addLayout(row)
        row = QHBoxLayout()
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
        bl.addWidget(self.sync_status)
        lay.addWidget(box)

        box2 = QGroupBox("Other sims in the sync folder")
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

    def sync(self) -> Optional[FolderSync]:
        folder = self.repo.get("sync_folder")
        if not folder:
            return None
        s = FolderSync(self.repo, folder)
        return s if s.ok else None

    def browse_sync(self):
        d = QFileDialog.getExistingDirectory(self, "Sync folder", self.sync_edit.text())
        if d:
            self.sync_edit.setText(os.path.normpath(d))
            self.save_sync()

    def save_sync(self):
        self.repo.set("sync_folder", self.sync_edit.text().strip())
        self.sync_dirty = True
        self.push_now()

    def push_now(self):
        s = self.sync()
        if not s:
            QMessageBox.warning(self, APP_NAME, "Sync folder is not set or not reachable.")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = s.push()
            self.sync_dirty = False
            self.statusBar().showMessage(f"Uploaded: {res['objects_copied']} new file version(s)")
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Upload failed: {e}")
        finally:
            QApplication.restoreOverrideCursor()
        self.refresh_sync()

    def refresh_sync(self):
        last = self.repo.get("last_push")
        s = self.sync()
        if not self.repo.get("sync_folder"):
            self.sync_status.setText("Not configured.")
        elif not s:
            self.sync_status.setText("<span style='color:#c62828'>Folder not reachable right now.</span>")
        else:
            self.sync_status.setText(f"Last upload: {fmt_ts(float(last)) if last else 'never'}"
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
        DiffDialog(f"{self.machine_combo.currentData()} → this PC",
                   unified_diff(remote, core.read_file_bytes(target), f"{self.machine_combo.currentData()}",
                                "this PC"), self).exec()

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
            self.repo.commit("Safety copy before copying from another PC", kind="safety")
            core.write_file_atomic(target, data)
            self.repo.log(f"Copied {rf.get('label')} from {self.machine_combo.currentData()} to {target}")
            f = self.repo.find_file_by_path(target)
            if f:
                self.repo.commit(f"Copied {f.display} from {self.machine_combo.currentData()}", kind="restore")
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
        self.autostart_cb.setChecked(core.autostart_enabled())
        self.autostart_cb.setEnabled(sys.platform == "win32")
        self.autostart_cb.toggled.connect(self.toggle_autostart)
        form.addRow("", self.autostart_cb)
        self.proc_edit = QLineEdit(self.repo.get("warn_processes"))
        self.proc_edit.editingFinished.connect(lambda: self.repo.set("warn_processes", self.proc_edit.text()))
        form.addRow("Warn before restoring while these run", self.proc_edit)
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
        adm = core.is_admin()
        form.addRow("Administrator rights", QLabel("<span style='color:#2e7d32'>yes</span>" if adm else
                                                   "<span style='color:#c62828'>no — restart the app and accept "
                                                   "the Windows prompt</span>"))
        if sys.platform == "win32" and not adm:
            self.autostart_cb.setEnabled(False)
            self.autostart_cb.setToolTip("Needs administrator rights to set up")
        form.addRow("", QLabel(f"{APP_NAME} {APP_VERSION}"))
        return w

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
