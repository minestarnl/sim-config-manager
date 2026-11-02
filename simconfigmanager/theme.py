"""
VS Code–style look (Dark Modern / Light Modern), the same on Windows and macOS.

* Fusion style + a palette + one stylesheet for the whole app.
* Icons from VS Code's Codicon font (assets/codicon.ttf, CC BY 4.0, © Microsoft).
* The rest of the UI was written with light-theme colours inline (status colours, banners, diff HTML).
  Those are translated to theme colours in one place (`fix`), applied by TLabel/TTextEdit/setStyleSheet.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QStyleFactory, QTextEdit, QWidget

ASSETS = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) / "simconfigmanager" / "assets"
if not ASSETS.exists():
    ASSETS = Path(__file__).resolve().parent / "assets"

ICONS = {  # codicon code points
    "fleet": 0xEBA3, "versions": 0xEB78, "restore": 0xEA82, "files": 0xEAF0, "history": 0xEAFC,
    "rulebook": 0xEB12, "alerts": 0xEAA2, "cloud": 0xEBAA, "claude": 0xEC10, "settings": 0xEB51,
    "chevron": 0xEAB4, "check": 0xEAB2, "remote": 0xEB3A, "plug": 0xEB2D, "warning": 0xEA6C,
    "broadcast": 0xEAAD, "dot": 0xEA71, "dash": 0xEACC, "tag": 0xEA66, "compare": 0xEAFD, "save": 0xEB4B, "refresh": 0xEB37,
    "add": 0xEA60, "edit": 0xEA73, "trash": 0xEA81, "export": 0xEBAC, "upload": 0xEAC3, "pulse": 0xEB31,
    "dashboard": 0xEACD, "hardware": 0xEABE, "health": 0xEB05, "tools": 0xEB6D, "record": 0xEBA7,
    "play": 0xEB2C, "beaker": 0xEA79, "book": 0xEAA4, "package": 0xEB29, "shield": 0xEB53,
    "phone": 0xEADB, "checklist": 0xEAB3, "calendar": 0xEAB0, "graph": 0xEB03, "person": 0xEA67,
    "layers": 0xEBD2, "verified": 0xEB77, "zip": 0xEAEF, "notebook": 0xEBAF, "flame": 0xEAF2,
}

DARK = {
    "name": "dark",
    "bg": "#1f1f1f", "sidebar": "#181818", "activity": "#181818", "panel": "#181818", "header": "#181818",
    "border": "#2b2b2b", "fg": "#cccccc", "fg_strong": "#ffffff", "muted": "#9d9d9d", "faint": "#6e7681",
    "input": "#313131", "input_border": "#3c3c3c", "button": "#313131", "button_hover": "#3c3c3c",
    "primary": "#0078d4", "primary_hover": "#026ec1", "focus": "#0078d4",
    "selection": "#04395e", "selection_fg": "#ffffff", "hover": "#2a2d2e", "alt_row": "#1c1c1c",
    "status": "#007acc", "status_remote": "#68217a", "status_fg": "#ffffff",
    "activity_fg": "#868686", "activity_active": "#e7e7e7", "badge": "#0078d4",
    "scroll": "#79797966", "scroll_hover": "#646464b3",
    "green": "#89d185", "orange": "#d7ba7d", "red": "#f48771", "blue": "#3794ff", "purple": "#c586c0",
    "teal": "#4ec9b0", "brown": "#ce9178",
}
LIGHT = {
    "name": "light",
    "bg": "#ffffff", "sidebar": "#f8f8f8", "activity": "#f8f8f8", "panel": "#f8f8f8", "header": "#f8f8f8",
    "border": "#e5e5e5", "fg": "#3b3b3b", "fg_strong": "#1f1f1f", "muted": "#6f6f6f", "faint": "#8b949e",
    "input": "#ffffff", "input_border": "#cecece", "button": "#e5e5e5", "button_hover": "#cccccc",
    "primary": "#005fb8", "primary_hover": "#0258a8", "focus": "#005fb8",
    "selection": "#e4e6f1", "selection_fg": "#1f1f1f", "hover": "#f2f2f2", "alt_row": "#fafafa",
    "status": "#005fb8", "status_remote": "#68217a", "status_fg": "#ffffff",
    "activity_fg": "#616161", "activity_active": "#1f1f1f", "badge": "#005fb8",
    "scroll": "#64646466", "scroll_hover": "#646464b3",
    "green": "#2e7d32", "orange": "#bf6900", "red": "#c62828", "blue": "#005fb8", "purple": "#8f3fa0",
    "teal": "#00838f", "brown": "#8d6e63",
}

EDITOR_DARK = {  # VS Code Dark+ syntax colours
    "bg": "#1f1f1f", "fg": "#cccccc", "gutter_bg": "#1f1f1f", "gutter_fg": "#6e7681", "gutter_fg_active": "#cccccc",
    "current_line": "#282828", "comment": "#6a9955", "section": "#569cd6", "key": "#9cdcfe", "value": "#ce9178",
    "number": "#b5cea8", "bool": "#569cd6", "operator": "#d4d4d4", "tag": "#569cd6", "attr": "#9cdcfe",
    "string": "#ce9178", "decl": "#c586c0", "entity": "#d7ba7d", "text": "#ffffff",
    "error_bg": "#5a1d1d", "error_gutter": "#f14c4c", "find_bg": "#623315", "selection": "#264f78",
}
EDITOR_LIGHT = {  # VS Code Light+ syntax colours
    "bg": "#ffffff", "fg": "#3b3b3b", "gutter_bg": "#ffffff", "gutter_fg": "#6e7681", "gutter_fg_active": "#171184",
    "current_line": "#f5f5f5", "comment": "#008000", "section": "#0000ff", "key": "#0451a5", "value": "#a31515",
    "number": "#098658", "bool": "#0000ff", "operator": "#3b3b3b", "tag": "#800000", "attr": "#e50000",
    "string": "#a31515", "decl": "#7a3e9d", "entity": "#b5651d", "text": "#000000",
    "error_bg": "#ffd7d7", "error_gutter": "#e51400", "find_bg": "#f8c9ab", "selection": "#add6ff",
}

T = dict(DARK)            # active palette (filled by init)
EDITOR = dict(EDITOR_DARK)
_MAP: dict[str, str] = {}
_ICON_FAMILY = ""
_MONO = ["Cascadia Mono", "Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "monospace"]


# --------------------------------------------------------------------------- preferences (per computer)

def _prefs_file() -> Path:
    if sys.platform == "darwin":
        d = Path.home() / "Library" / "Application Support" / "SimConfigManager"
    elif sys.platform == "win32":
        d = Path(os.environ.get("APPDATA") or Path.home()) / "SimConfigManager"
    else:
        d = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "SimConfigManager"
    return d / "settings.json"


def saved_choice() -> str:
    try:
        return json.loads(_prefs_file().read_text(encoding="utf-8")).get("theme", "dark")
    except (OSError, ValueError):
        return "dark"


def save_choice(choice: str) -> None:
    p = _prefs_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d["theme"] = choice
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


def is_dark() -> bool:
    return T["name"] == "dark"


# --------------------------------------------------------------------------- colour translation

def _build_map():
    """Old inline light-theme colours → active theme colours."""
    if not is_dark():
        return {"#1f4e79": T["primary"], "#555": T["muted"], "#555555": T["muted"], "#777": T["faint"],
                "#999": T["faint"]}
    m = {
        "#2e7d32": T["green"], "#1b5e20": T["green"], "#e65100": T["orange"], "#c62828": T["red"],
        "#b71c1c": T["red"], "#1565c0": T["blue"], "#0d47a1": "#75beff", "#6a1b9a": T["purple"],
        "#555555": T["muted"], "#555": T["muted"], "#777": T["faint"], "#999999": T["faint"], "#999": T["faint"],
        "#333333": T["fg"], "#1f4e79": T["primary"], "#8d6e63": T["brown"], "#00838f": T["teal"],
        "#fff3cd": "#3a3220", "#664d03": "#e2c08d", "#e3f2fd": "#14293d", "#eef4fb": "#1b2733",
        "#e6ffed": "#1e3a1e", "#ffeef0": "#4b1818", "#ffd7d7": "#5a1d1d", "#000": T["fg"],
    }
    return m


_HEX = re.compile(r"(background(?:-color)?\s*:\s*)?(#[0-9a-fA-F]{3,6})\b")
_BG: dict[str, str] = {}


def _bg_map() -> dict:
    """Background colours: keep white text readable (buttons) and darken light banners."""
    if not is_dark():
        return {"#1f4e79": T["primary"]}
    return {"#2e7d32": "#2f7d32", "#1f4e79": T["primary"], "#6a1b9a": "#7b3d91", "#c62828": "#b62a2a",
            "#fff3cd": "#3a3220", "#e3f2fd": "#14293d", "#eef4fb": "#1b2733", "#e6ffed": "#1e3a1e",
            "#ffeef0": "#4b1818", "#ffd7d7": "#5a1d1d"}


def fix(text):
    """Translate inline colours in HTML / stylesheets to the active theme."""
    if not text or not _MAP or not isinstance(text, str) or "#" not in text:
        return text

    def sub(m):
        prefix, hx = m.group(1), m.group(2)
        if prefix:  # backgrounds have their own map; unknown ones (e.g. user-picked button colours) stay
            return prefix + _BG.get(hx.lower(), hx)
        return _MAP.get(hx.lower(), hx)
    return _HEX.sub(sub, text)


def c(hexcolor: Optional[str]) -> Optional[str]:
    if not hexcolor:
        return hexcolor
    return _MAP.get(hexcolor.lower(), hexcolor)


class TLabel(QLabel):
    def __init__(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            args = (fix(args[0]),) + args[1:]
        super().__init__(*args, **kwargs)

    def setText(self, text):  # noqa: N802
        super().setText(fix(text))


class TTextEdit(QTextEdit):
    def setHtml(self, text):  # noqa: N802
        super().setHtml(fix(text))


_orig_set_ss = QWidget.setStyleSheet


def _themed_set_ss(self, ss):
    _orig_set_ss(self, fix(ss))


# --------------------------------------------------------------------------- icons

def icon_font(px: int = 16) -> QFont:
    f = QFont(_ICON_FAMILY or "codicon")
    f.setPixelSize(px)
    return f


def glyph_pixmap(name: str, color: str, size: int = 24, badge: Optional[str] = None,
                 scale: float = 2.0) -> QPixmap:
    pm = QPixmap(int(size * scale), int(size * scale))
    pm.setDevicePixelRatio(scale)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setFont(icon_font(size))
    p.setPen(QColor(color))
    p.drawText(0, 0, size, size, Qt.AlignCenter, chr(ICONS.get(name, ICONS["dot"])))
    if badge:
        bw = max(14, 7 * len(badge) + 7)
        p.setBrush(QColor(T["badge"]))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(size - bw + 3, size - 14 + 3, bw, 14, 7, 7)
        f = QFont()
        f.setPixelSize(9)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#ffffff"))
        p.drawText(size - bw + 3, size - 14 + 3, bw, 14, Qt.AlignCenter, badge)
    p.end()
    return pm


def icon(name: str, color: Optional[str] = None, size: int = 16) -> QIcon:
    return QIcon(glyph_pixmap(name, color or T["fg"], size))


def mono_font(pt: int = 11) -> QFont:
    f = QFont()
    f.setFamilies(_MONO)
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(pt)
    return f


def _glyph_file(name: str, color: str, size: int = 14) -> str:
    """Small glyph PNG for stylesheet images (combo arrow, checkbox tick)."""
    import tempfile
    d = Path(tempfile.gettempdir()) / "scm-theme"
    d.mkdir(exist_ok=True)
    p = d / f"{name}-{size}-{color.strip('#')}.png"
    if not p.exists():
        glyph_pixmap(name, color, size, scale=2.0).save(str(p))
    return p.as_posix()


# --------------------------------------------------------------------------- apply

def resolve(choice: str) -> str:
    if choice == "system":
        try:
            from PySide6.QtCore import Qt as _Qt
            scheme = QApplication.styleHints().colorScheme()
            return "dark" if scheme == _Qt.ColorScheme.Dark else "light"
        except Exception:  # noqa: BLE001
            return "dark"
    return "light" if choice == "light" else "dark"


def init(app: QApplication, choice: Optional[str] = None) -> str:
    """Call once, right after creating the QApplication and before building windows."""
    global _ICON_FAMILY, _MAP
    mode = resolve(choice or saved_choice())
    T.clear()
    T.update(DARK if mode == "dark" else LIGHT)
    EDITOR.clear()
    EDITOR.update(EDITOR_DARK if mode == "dark" else EDITOR_LIGHT)
    _MAP = _build_map()
    global _BG
    _BG = _bg_map()
    fid = QFontDatabase.addApplicationFont(str(ASSETS / "codicon.ttf"))
    fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
    _ICON_FAMILY = fams[0] if fams else "codicon"

    app.setStyle(QStyleFactory.create("Fusion"))
    pal = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.WindowText, "fg"), (QPalette.Base, "bg"),
                      (QPalette.AlternateBase, "alt_row"), (QPalette.Text, "fg"), (QPalette.Button, "button"),
                      (QPalette.ButtonText, "fg"), (QPalette.Highlight, "selection"),
                      (QPalette.HighlightedText, "selection_fg"), (QPalette.ToolTipBase, "sidebar"),
                      (QPalette.ToolTipText, "fg"), (QPalette.PlaceholderText, "faint"), (QPalette.Link, "blue"),
                      (QPalette.BrightText, "fg_strong")):
        pal.setColor(role, QColor(T[key]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor(T["faint"]))
    app.setPalette(pal)

    f = QFont()
    if sys.platform == "darwin":
        f.setFamilies([".AppleSystemUIFont", "Helvetica Neue"])
        f.setPointSize(13)
    elif sys.platform == "win32":
        f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
        f.setPointSize(9)
    else:
        f.setFamilies(["Inter", "Noto Sans", "DejaVu Sans"])
        f.setPointSize(9)
    app.setFont(f)
    app.setStyleSheet(stylesheet())
    QWidget.setStyleSheet = _themed_set_ss
    return mode


def stylesheet() -> str:
    t = T
    arrow = _glyph_file("chevron", t["muted"])
    tick = _glyph_file("check", "#ffffff", 12)
    dash = _glyph_file("dash", "#ffffff", 12)
    return f"""
* {{ outline: 0; }}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QWidget {{ color: {t['fg']}; }}
QToolTip {{ background: {t['sidebar']}; color: {t['fg']}; border: 1px solid {t['border']}; padding: 4px 6px; }}

/* activity bar, page header, status bar */
#ActivityBar {{ background: {t['activity']}; border-right: 1px solid {t['border']}; }}
#ActivityBar QToolButton {{ border: none; border-left: 2px solid transparent; background: transparent;
    padding: 0; margin: 0; }}
#ActivityBar QToolButton:checked {{ border-left: 2px solid {t['primary']}; }}
#PageHeader {{ background: {t['header']}; border-bottom: 1px solid {t['border']}; }}
#PageTitle {{ color: {t['fg_strong']}; font-weight: 600; letter-spacing: 0.4px; }}
#PageSubtitle {{ color: {t['muted']}; }}
QStatusBar {{ background: {t['status']}; color: {t['status_fg']}; border: none; }}
QStatusBar[remote="true"] {{ background: {t['status_remote']}; }}
QStatusBar QLabel {{ color: {t['status_fg']}; padding: 0 8px; }}
QStatusBar::item {{ border: none; }}

/* buttons */
QPushButton {{ background: {t['button']}; color: {t['fg']}; border: 1px solid {t['input_border']};
    border-radius: 4px; padding: 4px 12px; min-height: 18px; }}
QPushButton:hover {{ background: {t['button_hover']}; }}
QPushButton:pressed {{ background: {t['input_border']}; }}
QPushButton:default, QPushButton[primary="true"] {{ background: {t['primary']}; color: #ffffff;
    border-color: {t['primary']}; }}
QPushButton:default:hover, QPushButton[primary="true"]:hover {{ background: {t['primary_hover']}; }}
QPushButton[danger="true"] {{ color: {t['red']}; }}
QPushButton[danger="true"]:hover {{ background: {t['red']}; color: #ffffff; border-color: {t['red']}; }}
QPushButton:disabled {{ color: {t['faint']}; background: {t['bg']}; border-color: {t['border']}; }}
QToolButton {{ background: transparent; border: 1px solid transparent; border-radius: 4px; padding: 3px 6px;
    color: {t['fg']}; }}
QToolButton:hover {{ background: {t['hover']}; }}
QToolButton:checked {{ background: {t['selection']}; }}
QToolBar {{ background: {t['sidebar']}; border: none; border-bottom: 1px solid {t['border']}; spacing: 2px;
    padding: 3px; }}

/* inputs */
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {{ background: {t['input']}; color: {t['fg']};
    border: 1px solid {t['input_border']}; border-radius: 4px; selection-background-color: {t['selection']};
    selection-color: {t['selection_fg']}; }}
QLineEdit, QSpinBox, QComboBox {{ padding: 3px 6px; min-height: 18px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus, QTextEdit:focus {{
    border: 1px solid {t['focus']}; }}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{ color: {t['faint']}; background: {t['bg']}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox::down-arrow {{ image: url("{arrow}"); width: 12px; height: 12px; }}
QComboBox QAbstractItemView {{ background: {t['sidebar']}; border: 1px solid {t['border']};
    selection-background-color: {t['selection']}; padding: 2px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}
QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid {t['input_border']}; border-radius: 3px;
    background: {t['input']}; }}
QCheckBox::indicator:hover {{ border-color: {t['focus']}; }}
QCheckBox::indicator:checked {{ background: {t['primary']}; border-color: {t['primary']}; image: url("{tick}"); }}
QCheckBox::indicator:disabled {{ background: {t['bg']}; border-color: {t['border']}; }}
QTreeView::indicator, QListView::indicator, QTableView::indicator {{ width: 14px; height: 14px;
    border: 1px solid {t['input_border']}; border-radius: 3px; background: {t['input']}; }}
QTreeView::indicator:checked, QListView::indicator:checked, QTableView::indicator:checked {{
    background: {t['primary']}; border-color: {t['primary']}; image: url("{tick}"); }}
QTreeView::indicator:indeterminate {{ background: {t['primary']}; border-color: {t['primary']}; image: url("{dash}"); }}
QTreeView::indicator:disabled {{ background: {t['border']}; border-color: {t['border']}; }}
QTextEdit[readOnly="true"] {{ background: {t['bg']}; }}

/* lists and tables */
QTableWidget, QTableView, QListWidget, QTreeView {{ background: {t['bg']}; alternate-background-color: {t['alt_row']};
    border: 1px solid {t['border']}; border-radius: 4px; gridline-color: transparent;
    selection-background-color: {t['selection']}; selection-color: {t['selection_fg']}; }}
QTableWidget::item, QListWidget::item {{ padding: 3px 6px; border: none; }}
QTableWidget::item:hover, QListWidget::item:hover {{ background: {t['hover']}; }}
QTableWidget::item:selected, QListWidget::item:selected {{ background: {t['selection']};
    color: {t['selection_fg']}; }}
QHeaderView {{ background: {t['sidebar']}; border: none; }}
QHeaderView::section {{ background: {t['sidebar']}; color: {t['muted']}; border: none;
    border-bottom: 1px solid {t['border']}; border-right: 1px solid {t['border']}; padding: 5px 8px;
    font-weight: 600; }}
QTableCornerButton::section {{ background: {t['sidebar']}; border: none; }}

/* group boxes = VS Code "sections" */
QGroupBox {{ border: 1px solid {t['border']}; border-radius: 6px; margin-top: 14px; padding: 12px 10px 10px 10px;
    background: {t['panel']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t['muted']};
    font-weight: 600; }}

QSplitter::handle {{ background: {t['border']}; }}
QSplitter::handle:vertical {{ height: 1px; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QTabWidget::pane {{ border: none; }}
QMenu {{ background: {t['sidebar']}; border: 1px solid {t['border']}; padding: 4px; }}
QMenu::item {{ padding: 5px 24px 5px 20px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['selection']}; color: {t['selection_fg']}; }}
QMenu::separator {{ height: 1px; background: {t['border']}; margin: 4px 6px; }}
QScrollArea {{ border: none; }}

/* thin VS Code scrollbars */
QScrollBar:vertical {{ background: transparent; width: 12px; margin: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 12px; margin: 0; }}
QScrollBar::handle {{ background: {t['scroll']}; border-radius: 0; min-height: 24px; min-width: 24px; }}
QScrollBar::handle:hover {{ background: {t['scroll_hover']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QMessageBox QLabel {{ min-width: 280px; }}
QLabel[hint="true"] {{ color: {t['muted']}; }}
QLabel[heading="true"] {{ color: {t['fg_strong']}; font-weight: 600; font-size: 14px; }}
QLabel[banner="true"] {{ background: {t['panel']}; border: 1px solid {t['border']}; border-radius: 6px;
    padding: 10px 12px; }}
"""
