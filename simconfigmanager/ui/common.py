"""Shared UI building blocks: icons, tables, buttons and messages."""
from __future__ import annotations


import os
import subprocess
import sys
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap, QBrush, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QTableWidget, QTableWidgetItem,
)

from .. import theme



STATUS_COLORS = {"OK": "#2e7d32", "Changed": "#e65100", "Missing": "#c62828", "New": "#1565c0"}


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
