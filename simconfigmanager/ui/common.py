"""
Shared UI building blocks, so every page looks and behaves the same:

* colours and file-state labels (one vocabulary for Files, Versions, Fleet and the Hub)
* tables: ``setup_table`` / ``item`` / ``selected_data``
* buttons: ``button`` and ``button_row`` (primary / danger styles come from the theme)
* sections: ``section`` (titled box) and ``hint`` (muted explanatory text)
* messages: ``confirm`` (verb-labelled buttons), ``inform``, ``warn`` and ``fail`` ("Could not …: reason")
"""
from __future__ import annotations

import html
import os
import subprocess
import sys
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QBrush, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import core, theme
from ..core import APP_TITLE
from ..theme import TLabel as QLabel

# semantic colours, written as light-theme values; theme.fix() maps them to the dark palette
OK, WARN, BAD, INFO, MUTED, ACCENT = "#2e7d32", "#e65100", "#c62828", "#1565c0", "#777", "#1f4e79"

# a tracked file compared with its last snapshot (Files page)
FILE_STATE = {
    "OK": ("✔", "Up to date", OK),
    "Changed": ("✎", "Changed", WARN),
    "Missing": ("✖", "Missing", BAD),
    "New": ("＋", "Not recorded yet", INFO),
}
# a tracked file compared with a version (Versions page, Fleet, Hub) — texts from core.STATUS_TEXT
VERSION_STATE_STYLE = {"match": ("✔", OK), "modified": ("✎", WARN), "missing": ("✖", BAD), "new": ("＋", INFO),
                       "gone": ("–", MUTED), "untracked": ("–", MUTED)}
STATUS_COLORS = {k: v[2] for k, v in FILE_STATE.items()}


SNAPSHOT_KIND = {  # snapshot.kind → (label, colour)
    "auto": ("Automatic", MUTED), "manual": ("Manual", INFO), "safety": ("Safety copy", "#8d6e63"),
    "restore": ("Restore", OK), "baseline": ("Baseline", "#6a1b9a"), "import": ("Import", "#00838f"),
}


def running_warning(repo) -> str:
    """Extra line for restore/write confirmations when sim programs are running."""
    try:
        running = repo.running_sim_processes()
    except Exception:  # noqa: BLE001
        running = []
    if not running:
        return ""
    return ("\n\n⚠ Running now: " + ", ".join(running) + ". They may overwrite the files when they exit "
            "(Prepar3D rewrites Prepar3D.cfg on exit) — best to close them first.")


def file_state_item(state: str, data=None) -> QTableWidgetItem:
    glyph, text, col = FILE_STATE.get(state, ("", state, None))
    return item(f"{glyph} {text}", data, col, bold=state != "OK")


def version_state_item(state: str, data=None) -> QTableWidgetItem:
    glyph, col = VERSION_STATE_STYLE.get(state, ("", None))
    return item(f"{glyph} {core.STATUS_TEXT.get(state, state)}", data, col, bold=state != "match")


def colored(text: str, color: str, bold: bool = False) -> str:
    """Escaped HTML span in a semantic colour."""
    t = html.escape(str(text))
    return f"<span style='color:{color}'>{'<b>' + t + '</b>' if bold else t}</span>"


def diff_html(diff_text: str) -> str:
    """Unified diff → coloured HTML (used by every diff view)."""
    if not diff_text.strip():
        return "<pre>(no differences)</pre>"
    out = []
    for ln in diff_text.splitlines():
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
    return "<pre>" + "<br>".join(out) + "</pre>"


def banner(text: str, level: str = "warn") -> QLabel:
    """Highlighted message inside a page or dialog: level = warn | info | bad | ok."""
    bg, fg = {"warn": ("#fff3cd", "#664d03"), "info": ("#eef4fb", ""), "bad": ("#ffeef0", "#b71c1c"),
              "ok": ("#e6ffed", "#1b5e20")}[level]
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setStyleSheet(f"background:{bg};{' color:' + fg + ';' if fg else ''} padding:8px; border-radius:4px")
    return lab


# --------------------------------------------------------------------------- buttons and layout

def button(text: str, slot: Optional[Callable] = None, *, primary: bool = False, danger: bool = False,
           tip: str = "", icon: str = "") -> QPushButton:
    b = QPushButton(text)
    if slot is not None:
        b.clicked.connect(lambda _c=False: slot())
    if primary:
        b.setProperty("primary", True)
    if danger:
        b.setProperty("danger", True)
    if tip:
        b.setToolTip(tip)
    if icon:
        b.setIcon(theme.icon(icon, "#ffffff" if primary else None))
    return b


def button_row(*specs, stretch: bool = True, trailing=()) -> QHBoxLayout:
    """A row of buttons. Each spec is a QWidget or (text, slot[, {button kwargs}]). ``trailing`` go right."""
    row = QHBoxLayout()
    for s in specs:
        row.addWidget(s if isinstance(s, QWidget) else button(s[0], s[1], **(s[2] if len(s) > 2 else {})))
    if stretch:
        row.addStretch(1)
    for s in trailing:
        row.addWidget(s if isinstance(s, QWidget) else button(s[0], s[1], **(s[2] if len(s) > 2 else {})))
    return row


def section(title: str, layout_cls=QVBoxLayout):
    """Titled box (rendered as a VS Code-style section). Returns (box, layout)."""
    box = QGroupBox(title)
    return box, layout_cls(box)


def hint(text: str) -> QLabel:
    """Muted, word-wrapped explanation under a heading or inside a section."""
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setTextFormat(Qt.RichText)
    lab.setOpenExternalLinks(True)
    lab.setProperty("hint", True)
    return lab


def heading(text: str) -> QLabel:
    lab = QLabel(html.escape(text))
    lab.setProperty("heading", True)
    return lab


# --------------------------------------------------------------------------- messages

def confirm(parent, text: str, action: str, *, title: str = "", danger: bool = False, details: str = "") -> bool:
    """Ask before doing something. The OK button is labelled with the action ("Restore 3 files")."""
    box = QMessageBox(parent)
    box.setWindowTitle(title or APP_TITLE)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setText(text)
    if details:
        box.setInformativeText(details)
    ok = box.addButton(action, QMessageBox.AcceptRole)
    box.addButton("Cancel", QMessageBox.RejectRole)
    box.setDefaultButton(ok)
    if danger:
        ok.setProperty("danger", True)
    box.exec()
    return box.clickedButton() is ok


def inform(parent, text: str, title: str = "") -> None:
    QMessageBox.information(parent, title or APP_TITLE, text)


def warn(parent, text: str, title: str = "") -> None:
    QMessageBox.warning(parent, title or APP_TITLE, text)


def fail(parent, action: str, err, hint_text: str = "") -> None:
    """Uniform error: 'Could not <action>.' + reason (+ what to try)."""
    msg = f"Could not {action}."
    if err:
        msg += f"\n\n{err}"
    if hint_text:
        msg += f"\n\n{hint_text}"
    QMessageBox.warning(parent, APP_TITLE, msg)


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
