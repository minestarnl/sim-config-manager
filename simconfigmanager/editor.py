"""Built-in config editor: syntax highlighting (CFG/INI, XML, JSON), line numbers, find/replace,
validation, and saving that keeps the file's encoding/line endings and records a snapshot."""
from __future__ import annotations

import os
import re
from typing import Optional

from PySide6.QtCore import QRect, QRegularExpression, QSize, Qt, QTimer
from PySide6.QtGui import (QAction, QColor, QFont, QKeySequence, QPainter, QSyntaxHighlighter, QTextCharFormat,
                           QTextCursor, QTextDocument, QTextFormat, QShortcut)
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
                               QMessageBox, QPlainTextEdit, QPushButton, QTextEdit, QToolBar, QVBoxLayout,
                               QWidget)

from . import core
from .core import APP_NAME, Repo, TrackedFile

# --------------------------------------------------------------------------- colours (light theme)

PALETTE = {
    "bg": "#ffffff", "fg": "#1e1e1e", "gutter_bg": "#f3f3f3", "gutter_fg": "#999999",
    "current_line": "#fffbdd", "comment": "#6a9955", "section": "#0000c0", "key": "#0451a5",
    "value": "#a31515", "number": "#098658", "bool": "#0000ff", "operator": "#555555",
    "tag": "#800000", "attr": "#e50000", "string": "#a31515", "decl": "#7a3e9d", "entity": "#b5651d",
    "error_bg": "#ffd7d7", "find_bg": "#ffe680",
}


def fmt(color: str, bold: bool = False, italic: bool = False) -> QTextCharFormat:
    f = QTextCharFormat()
    f.setForeground(QColor(color))
    if bold:
        f.setFontWeight(QFont.Bold)
    if italic:
        f.setFontItalic(True)
    return f


def detect_kind(path: str, text: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xml", ".fxml", ".xsd", ".config", ".spb.xml"):
        return "xml"
    if ext in (".json",):
        return "json"
    if ext in (".cfg", ".ini", ".inf", ".txt", ".ccp", ".dsd"):
        if text.lstrip().startswith("<"):
            return "xml"
        return "ini"
    s = text.lstrip()
    if s.startswith("<"):
        return "xml"
    if s.startswith(("{", "[")) and not re.match(r"\[[^\]\n]+\]\s*\n", s):
        return "json"
    if re.search(r"^\s*\[[^\]]+\]\s*$", text, re.M) or re.search(r"^\s*\w[\w.\s]*=", text, re.M):
        return "ini"
    return "text"


# --------------------------------------------------------------------------- highlighters

class IniHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.f_comment = fmt(PALETTE["comment"], italic=True)
        self.f_section = fmt(PALETTE["section"], bold=True)
        self.f_key = fmt(PALETTE["key"])
        self.f_op = fmt(PALETTE["operator"])
        self.f_value = fmt(PALETTE["value"])
        self.f_number = fmt(PALETTE["number"])
        self.f_bool = fmt(PALETTE["bool"], bold=True)
        self.re_num = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$|^0x[0-9a-fA-F]+$")
        self.re_list_num = re.compile(r"[-+]?\d+\.?\d*")

    def highlightBlock(self, text: str):
        s = text.lstrip()
        off = len(text) - len(s)
        if not s:
            return
        if s.startswith((";", "#", "//")):
            self.setFormat(0, len(text), self.f_comment)
            return
        if s.startswith("["):
            self.setFormat(off, len(s), self.f_section)
            return
        if "=" in text:
            eq = text.index("=")
            self.setFormat(0, eq, self.f_key)
            self.setFormat(eq, 1, self.f_op)
            val = text[eq + 1:]
            # trailing comment
            cpos = None
            for m in re.finditer(r"\s(;|//)", val):
                cpos = m.start()
                break
            body = val if cpos is None else val[:cpos]
            vstart = eq + 1
            stripped = body.strip()
            if stripped.lower() in ("true", "false", "yes", "no", "on", "off"):
                self.setFormat(vstart, len(body), self.f_bool)
            elif self.re_num.match(stripped):
                self.setFormat(vstart, len(body), self.f_number)
            else:
                self.setFormat(vstart, len(body), self.f_value)
                for m in self.re_list_num.finditer(body):  # numbers inside comma lists
                    if re.fullmatch(r"[\d.,\s+-]+", stripped):
                        self.setFormat(vstart + m.start(), len(m.group()), self.f_number)
            if cpos is not None:
                self.setFormat(vstart + cpos, len(val) - cpos, self.f_comment)


class XmlHighlighter(QSyntaxHighlighter):
    IN_COMMENT, IN_CDATA = 1, 2

    def __init__(self, doc):
        super().__init__(doc)
        self.f_comment = fmt(PALETTE["comment"], italic=True)
        self.f_tag = fmt(PALETTE["tag"])
        self.f_attr = fmt(PALETTE["attr"])
        self.f_str = fmt(PALETTE["string"])
        self.f_decl = fmt(PALETTE["decl"])
        self.f_entity = fmt(PALETTE["entity"])
        self.f_text = fmt(PALETTE["fg"], bold=True)
        self.re_tag = QRegularExpression(r"</?[\w:.\-]+|/?>")
        self.re_attr = QRegularExpression(r"[\w:.\-]+(?=\s*=)")
        self.re_str = QRegularExpression(r"\"[^\"]*\"|'[^']*'")
        self.re_decl = QRegularExpression(r"<\?.*?\?>|<!DOCTYPE[^>]*>")
        self.re_entity = QRegularExpression(r"&[#\w]+;")
        self.re_text = QRegularExpression(r">([^<]+)<")

    def _apply(self, rx, text, f, group=0):
        it = rx.globalMatch(text)
        while it.hasNext():
            m = it.next()
            self.setFormat(m.capturedStart(group), m.capturedLength(group), f)

    def highlightBlock(self, text: str):
        # element text content (the values you usually edit) in bold
        self._apply(self.re_text, text, self.f_text, 1)
        self._apply(self.re_tag, text, self.f_tag)
        self._apply(self.re_attr, text, self.f_attr)
        self._apply(self.re_str, text, self.f_str)
        self._apply(self.re_entity, text, self.f_entity)
        self._apply(self.re_decl, text, self.f_decl)
        # multi-line comments / CDATA
        self.setCurrentBlockState(0)
        start = 0
        state = self.previousBlockState()
        while True:
            if state == self.IN_COMMENT:
                end = text.find("-->", start)
                if end < 0:
                    self.setFormat(start, len(text) - start, self.f_comment)
                    self.setCurrentBlockState(self.IN_COMMENT)
                    return
                self.setFormat(start, end + 3 - start, self.f_comment)
                start = end + 3
            elif state == self.IN_CDATA:
                end = text.find("]]>", start)
                if end < 0:
                    self.setFormat(start, len(text) - start, self.f_decl)
                    self.setCurrentBlockState(self.IN_CDATA)
                    return
                self.setFormat(start, end + 3 - start, self.f_decl)
                start = end + 3
            c = text.find("<!--", start)
            d = text.find("<![CDATA[", start)
            if c < 0 and d < 0:
                return
            if c >= 0 and (d < 0 or c < d):
                start, state = c, self.IN_COMMENT
            else:
                start, state = d, self.IN_CDATA


class JsonHighlighter(QSyntaxHighlighter):
    def __init__(self, doc):
        super().__init__(doc)
        self.rules = [
            (QRegularExpression(r"-?\b\d+(\.\d+)?([eE][-+]?\d+)?\b"), fmt(PALETTE["number"])),
            (QRegularExpression(r"\b(true|false|null)\b"), fmt(PALETTE["bool"], bold=True)),
            (QRegularExpression(r"\"(\\.|[^\"\\])*\""), fmt(PALETTE["string"])),
            (QRegularExpression(r"\"(\\.|[^\"\\])*\"(?=\s*:)"), fmt(PALETTE["key"])),
        ]

    def highlightBlock(self, text: str):
        for rx, f in self.rules:
            it = rx.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), f)


HIGHLIGHTERS = {"ini": IniHighlighter, "xml": XmlHighlighter, "json": JsonHighlighter}
KIND_NAMES = {"ini": "CFG / INI", "xml": "XML", "json": "JSON", "text": "Plain text"}


# --------------------------------------------------------------------------- code editor widget

class LineNumberArea(QWidget):
    def __init__(self, editor: "CodeEditor"):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_gutter(event)


class CodeEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        font = QFont("Consolas")
        font.setStyleHint(QFont.Monospace)
        font.setPointSize(11)
        self.setFont(font)
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setStyleSheet(f"QPlainTextEdit {{ background:{PALETTE['bg']}; color:{PALETTE['fg']}; "
                           "selection-background-color:#add6ff; selection-color:#000; }")
        self.gutter = LineNumberArea(self)
        self.error_lines: set[int] = set()
        self.find_term = ""
        self.find_case = False
        self.blockCountChanged.connect(self.update_gutter_width)
        self.updateRequest.connect(self.update_gutter)
        self.cursorPositionChanged.connect(self.refresh_extra)
        self.update_gutter_width()
        self.refresh_extra()

    # gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 14 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_gutter_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_gutter_width()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

    def paint_gutter(self, event):
        p = QPainter(self.gutter)
        p.fillRect(event.rect(), QColor(PALETTE["gutter_bg"]))
        block = self.firstVisibleBlock()
        num = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        cur = self.textCursor().blockNumber()
        h = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                if (num + 1) in self.error_lines:
                    p.fillRect(0, top, self.gutter.width(), h, QColor("#ff8a80"))
                p.setPen(QColor("#333333" if num == cur else PALETTE["gutter_fg"]))
                p.drawText(0, top, self.gutter.width() - 6, h, Qt.AlignRight, str(num + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            num += 1

    # current line + error lines + find matches
    def refresh_extra(self):
        sels = []
        for ln in sorted(self.error_lines):
            blk = self.document().findBlockByNumber(ln - 1)
            if blk.isValid():
                s = QTextEdit.ExtraSelection()
                s.format.setBackground(QColor(PALETTE["error_bg"]))
                s.format.setProperty(QTextFormat.FullWidthSelection, True)
                s.cursor = QTextCursor(blk)
                sels.append(s)
        if True:
            s = QTextEdit.ExtraSelection()
            s.format.setBackground(QColor(PALETTE["current_line"]))
            s.format.setProperty(QTextFormat.FullWidthSelection, True)
            s.cursor = self.textCursor()
            s.cursor.clearSelection()
            sels.append(s)
        if self.find_term:
            flags = QTextDocument.FindCaseSensitively if self.find_case else QTextDocument.FindFlag(0)
            c = QTextCursor(self.document())
            count = 0
            while count < 2000:
                c = self.document().find(self.find_term, c, flags)
                if c.isNull():
                    break
                s = QTextEdit.ExtraSelection()
                s.format.setBackground(QColor(PALETTE["find_bg"]))
                s.cursor = c
                sels.append(s)
                count += 1
        self.setExtraSelections(sels)
        self.gutter.update()

    def goto_line(self, line: int):
        blk = self.document().findBlockByNumber(max(0, line - 1))
        if blk.isValid():
            c = QTextCursor(blk)
            self.setTextCursor(c)
            self.centerCursor()
            self.setFocus()

    def keyPressEvent(self, e):
        # keep indentation on Enter
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and not e.modifiers():
            line = self.textCursor().block().text()
            indent = re.match(r"[ \t]*", line).group()
            super().keyPressEvent(e)
            self.insertPlainText(indent)
            return
        super().keyPressEvent(e)


# --------------------------------------------------------------------------- find bar

class FindBar(QFrame):
    def __init__(self, editor: CodeEditor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.setFrameShape(QFrame.StyledPanel)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 3, 6, 3)
        self.find = QLineEdit()
        self.find.setPlaceholderText("Find")
        self.replace = QLineEdit()
        self.replace.setPlaceholderText("Replace with")
        self.case = QCheckBox("Match case")
        self.info = QLabel()
        lay.addWidget(self.find, 2)
        for text, fn in (("▲", lambda: self.next(back=True)), ("▼", self.next)):
            b = QPushButton(text)
            b.setFixedWidth(32)
            b.clicked.connect(fn)
            lay.addWidget(b)
        lay.addWidget(self.case)
        lay.addWidget(self.replace, 2)
        for text, fn in (("Replace", self.replace_one), ("Replace all", self.replace_all), ("✕", self.close_bar)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            lay.addWidget(b)
        lay.addWidget(self.info)
        self.find.textChanged.connect(self.update_highlight)
        self.case.toggled.connect(self.update_highlight)
        self.find.returnPressed.connect(self.next)
        self.replace.returnPressed.connect(self.replace_one)

    def open_bar(self, replace: bool = False):
        self.show()
        sel = self.editor.textCursor().selectedText()
        if sel and "\u2029" not in sel:
            self.find.setText(sel)
        (self.replace if replace and self.find.text() else self.find).setFocus()
        self.find.selectAll()
        self.update_highlight()

    def close_bar(self):
        self.hide()
        self.editor.find_term = ""
        self.editor.refresh_extra()
        self.editor.setFocus()

    def flags(self, back=False):
        f = QTextDocument.FindFlag(0)
        if self.case.isChecked():
            f |= QTextDocument.FindCaseSensitively
        if back:
            f |= QTextDocument.FindBackward
        return f

    def update_highlight(self):
        self.editor.find_term = self.find.text()
        self.editor.find_case = self.case.isChecked()
        self.editor.refresh_extra()
        t = self.find.text()
        if t:
            hay = self.editor.toPlainText()
            n = hay.count(t) if self.case.isChecked() else hay.lower().count(t.lower())
            self.info.setText(f"{n} found")
        else:
            self.info.setText("")

    def next(self, back=False):
        t = self.find.text()
        if not t:
            return
        if not self.editor.find(t, self.flags(back)):
            c = self.editor.textCursor()
            c.movePosition(QTextCursor.End if back else QTextCursor.Start)
            self.editor.setTextCursor(c)
            self.editor.find(t, self.flags(back))

    def replace_one(self):
        c = self.editor.textCursor()
        t = self.find.text()
        if c.hasSelection() and (c.selectedText() == t or
                                 (not self.case.isChecked() and c.selectedText().lower() == t.lower())):
            c.insertText(self.replace.text())
        self.next()
        self.update_highlight()

    def replace_all(self):
        t = self.find.text()
        if not t:
            return
        c = QTextCursor(self.editor.document())
        c.beginEditBlock()
        n = 0
        cur = QTextCursor(self.editor.document())
        while True:
            cur = self.editor.document().find(t, cur, self.flags())
            if cur.isNull():
                break
            cur.insertText(self.replace.text())
            n += 1
        c.endEditBlock()
        self.info.setText(f"{n} replaced")
        self.editor.find_term = t
        self.editor.refresh_extra()


# --------------------------------------------------------------------------- save confirmation

class SaveDialog(QDialog):
    def __init__(self, title: str, diff: str, warn: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(900, 560)
        lay = QVBoxLayout(self)
        if warn:
            w = QLabel(warn)
            w.setWordWrap(True)
            w.setStyleSheet("background:#fff3cd; color:#664d03; padding:6px; border-radius:4px")
            lay.addWidget(w)
        lay.addWidget(QLabel("These changes will be written to the file:"))
        view = QTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("Consolas", 10))
        import html as _h
        out = []
        for ln in diff.splitlines():
            e = _h.escape(ln) or "&nbsp;"
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
        lay.addWidget(view)
        row = QHBoxLayout()
        row.addWidget(QLabel("Note for the history:"))
        self.note = QLineEdit()
        self.note.setPlaceholderText("e.g. 'Raised texture resolution for new GPU'")
        row.addWidget(self.note)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


# --------------------------------------------------------------------------- editor window

class EditorWindow(QMainWindow):
    """Edit a tracked file (or view an old version read-only)."""

    def __init__(self, repo: Repo, f: TrackedFile, on_saved=None, version_hash: Optional[str] = None,
                 version_label: str = "", parent=None):
        super().__init__(parent)
        self.repo, self.file, self.on_saved = repo, f, on_saved
        self.read_only = version_hash is not None
        self.version_hash = version_hash
        self.version_label = version_label
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.resize(1100, 760)

        if self.read_only:
            data = repo.get_object(version_hash)
            self.disk_hash = None
        else:
            data = repo.read_current(f)
            self.disk_hash = core.sha256_bytes(data) if data is not None else None
        if data is not None and core.is_binary(data):
            raise ValueError(f"{f.display} is a binary file and cannot be edited as text.")
        self.missing = data is None
        text, self.text_fmt = core.detect_text(data)
        self.kind = detect_kind(f.path, text)

        self.editor = CodeEditor()
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        self.editor.setReadOnly(self.read_only)
        self.highlighter = None
        self.set_kind(self.kind)
        self.findbar = FindBar(self.editor)
        self.findbar.hide()

        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.RichText)
        self.banner.linkActivated.connect(self.banner_link)
        self.banner.hide()

        self.problems = QListWidget()
        self.problems.setMaximumHeight(110)
        self.problems.itemActivated.connect(lambda it: self.editor.goto_line(it.data(Qt.UserRole)))
        self.problems.itemClicked.connect(lambda it: self.editor.goto_line(it.data(Qt.UserRole)))
        self.problems.hide()

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self.banner)
        lay.addWidget(self.editor, 1)
        lay.addWidget(self.findbar)
        lay.addWidget(self.problems)
        self.setCentralWidget(central)
        self.build_toolbar()

        self.pos_label = QLabel()
        self.statusBar().addPermanentWidget(self.pos_label)
        enc = self.text_fmt.encoding.upper() + (" BOM" if self.text_fmt.bom else "")
        nl = "CRLF" if self.text_fmt.newline == "\r\n" else "LF"
        self.statusBar().addPermanentWidget(QLabel(f"  {enc}  ·  {nl}  "))
        self.editor.cursorPositionChanged.connect(self.update_pos)
        self.editor.document().modificationChanged.connect(self.update_title)
        self.editor.textChanged.connect(self.schedule_validate)
        self.validate_timer = QTimer(self)
        self.validate_timer.setSingleShot(True)
        self.validate_timer.timeout.connect(self.validate)
        self.update_pos()
        self.update_title()

        if self.read_only:
            self.show_banner(f"Read-only view of <b>{version_label}</b>. "
                             "<a href='restore'>Restore this version to disk…</a>", "#e3f2fd", "#0d47a1")
        elif self.missing:
            self.show_banner("This file does not exist on disk right now. Saving will create it.",
                             "#fff3cd", "#664d03")
        else:
            self.disk_timer = QTimer(self)
            self.disk_timer.timeout.connect(self.check_disk)
            self.disk_timer.start(2000)
        self.validate()

        for seq, fn in ((QKeySequence.Find, lambda: self.findbar.open_bar()),
                        (QKeySequence.Replace, lambda: self.findbar.open_bar(True)),
                        (QKeySequence("Ctrl+H"), lambda: self.findbar.open_bar(True)),
                        (QKeySequence("Ctrl+G"), self.goto_line),
                        (QKeySequence("F3"), self.findbar.next),
                        (QKeySequence("Shift+F3"), lambda: self.findbar.next(back=True)),
                        (QKeySequence("Escape"), self.findbar.close_bar)):
            QShortcut(seq, self, fn)

    # ---------- UI
    def build_toolbar(self):
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)
        if not self.read_only:
            a = QAction("💾 Save", self)
            a.setShortcut(QKeySequence.Save)
            a.triggered.connect(self.save)
            tb.addAction(a)
            a = QAction("↶ Undo", self)
            a.triggered.connect(self.editor.undo)
            tb.addAction(a)
            a = QAction("↷ Redo", self)
            a.triggered.connect(self.editor.redo)
            tb.addAction(a)
            a = QAction("Show my changes", self)
            a.triggered.connect(self.show_changes)
            tb.addAction(a)
            a = QAction("Revert to disk", self)
            a.triggered.connect(self.reload)
            tb.addAction(a)
            tb.addSeparator()
        a = QAction("Find / replace", self)
        a.triggered.connect(lambda: self.findbar.open_bar())
        tb.addAction(a)
        a = QAction("Go to line", self)
        a.triggered.connect(self.goto_line)
        tb.addAction(a)
        a = QAction("Check syntax", self)
        a.triggered.connect(lambda: self.validate(announce=True))
        tb.addAction(a)
        tb.addSeparator()
        self.wrap = QAction("Wrap lines", self, checkable=True)
        self.wrap.toggled.connect(lambda on: self.editor.setLineWrapMode(
            QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap))
        tb.addAction(self.wrap)
        a = QAction("A+", self)
        a.setShortcut(QKeySequence.ZoomIn)
        a.triggered.connect(lambda: self.zoom(1))
        tb.addAction(a)
        a = QAction("A−", self)
        a.setShortcut(QKeySequence.ZoomOut)
        a.triggered.connect(lambda: self.zoom(-1))
        tb.addAction(a)
        tb.addSeparator()
        tb.addWidget(QLabel(" Highlight as: "))
        self.kind_combo = QComboBox()
        for k, name in KIND_NAMES.items():
            self.kind_combo.addItem(name, k)
        self.kind_combo.setCurrentIndex(self.kind_combo.findData(self.kind))
        self.kind_combo.currentIndexChanged.connect(lambda _i: (self.set_kind(self.kind_combo.currentData()),
                                                                self.validate()))
        tb.addWidget(self.kind_combo)

    def zoom(self, d):
        f = self.editor.font()
        f.setPointSize(max(7, min(30, f.pointSize() + d)))
        self.editor.setFont(f)
        self.editor.update_gutter_width()

    def set_kind(self, kind: str):
        self.kind = kind
        if self.highlighter is not None:
            self.highlighter.setDocument(None)
            self.highlighter = None
        cls = HIGHLIGHTERS.get(kind)
        if cls:
            self.highlighter = cls(self.editor.document())

    def update_title(self, *_):
        mod = "● " if self.editor.document().isModified() else ""
        ro = f" — {self.version_label} (read-only)" if self.read_only else ""
        self.setWindowTitle(f"{mod}{self.file.display}{ro} — {self.file.path}")

    def update_pos(self):
        c = self.editor.textCursor()
        self.pos_label.setText(f"Line {c.blockNumber() + 1}, Col {c.positionInBlock() + 1}  ")

    def show_banner(self, html_text: str, bg: str, fg: str):
        self.banner.setText(html_text)
        self.banner.setStyleSheet(f"background:{bg}; color:{fg}; padding:6px 10px;")
        self.banner.show()

    def banner_link(self, link: str):
        if link == "reload":
            self.reload(ask=False)
        elif link == "diff":
            self.show_disk_changes()
        elif link == "keep":
            data = self.repo.read_current(self.file)
            self.disk_hash = core.sha256_bytes(data) if data is not None else None
            self.banner.hide()
        elif link == "restore":
            self.restore_version()

    def goto_line(self):
        n, ok = QInputDialog.getInt(self, "Go to line", "Line:", self.editor.textCursor().blockNumber() + 1, 1,
                                    self.editor.blockCount())
        if ok:
            self.editor.goto_line(n)

    # ---------- validation
    def schedule_validate(self):
        self.validate_timer.start(600)

    def validate(self, announce: bool = False):
        probs = core.validate_text(self.editor.toPlainText(), self.kind)
        self.editor.error_lines = {ln for ln, _ in probs}
        self.editor.refresh_extra()
        self.problems.clear()
        for ln, msg in probs:
            it = QListWidgetItem(f"Line {ln}: {msg}")
            it.setData(Qt.UserRole, ln)
            it.setForeground(QColor("#b71c1c"))
            self.problems.addItem(it)
        self.problems.setVisible(bool(probs))
        if announce:
            if probs:
                self.editor.goto_line(probs[0][0])
            else:
                QMessageBox.information(self, APP_NAME, f"No problems found ({KIND_NAMES[self.kind]}).")
        return probs

    # ---------- disk sync
    def current_disk(self):
        data = self.repo.read_current(self.file)
        return data, (core.sha256_bytes(data) if data is not None else None)

    def check_disk(self):
        _data, h = self.current_disk()
        if h != self.disk_hash and not self.banner.isVisible():
            if self.editor.document().isModified():
                self.show_banner("⚠ The file was changed on disk by another program since you opened it. "
                                 "<a href='diff'>See what changed</a> · <a href='reload'>Load the disk version "
                                 "(lose my edits)</a> · <a href='keep'>Keep my edits</a>", "#fff3cd", "#664d03")
            else:
                self.reload(ask=False)
                self.statusBar().showMessage("File changed on disk — reloaded.", 5000)

    def show_disk_changes(self):
        data, _ = self.current_disk()
        from .gui import DiffDialog
        DiffDialog("Disk version vs your edits",
                   core.unified_diff(data, core.encode_text(self.editor.toPlainText(), self.text_fmt),
                                     "on disk now", "your edits"), self).exec()

    def reload(self, ask: bool = True):
        if ask and self.editor.document().isModified() and QMessageBox.question(
                self, APP_NAME, "Throw away your edits and reload the file from disk?") != QMessageBox.Yes:
            return
        data, h = self.current_disk()
        text, self.text_fmt = core.detect_text(data)
        c = self.editor.textCursor()
        pos = c.position()
        self.editor.setPlainText(text)
        c = self.editor.textCursor()
        c.setPosition(min(pos, len(text)))
        self.editor.setTextCursor(c)
        self.editor.document().setModified(False)
        self.disk_hash = h
        self.missing = data is None
        self.banner.hide()
        self.validate()

    def show_changes(self):
        from .gui import DiffDialog
        data, _ = self.current_disk()
        DiffDialog(f"{self.file.display}: your unsaved changes",
                   core.unified_diff(data, core.encode_text(self.editor.toPlainText(), self.text_fmt),
                                     "on disk", "your edits"), self).exec()

    # ---------- save
    def save(self) -> bool:
        if self.read_only:
            return False
        try:
            new = core.encode_text(self.editor.toPlainText(), self.text_fmt)
        except ValueError as e:
            QMessageBox.warning(self, APP_NAME, str(e))
            return False
        disk, h = self.current_disk()
        if disk == new:
            self.editor.document().setModified(False)
            self.statusBar().showMessage("No changes to save.", 4000)
            return True
        warn = []
        if h != self.disk_hash:
            warn.append("⚠ The file was changed on disk by another program after you opened it — saving will "
                        "overwrite that change (it is kept in History).")
        probs = self.validate()
        if probs:
            warn.append(f"⚠ {len(probs)} syntax problem(s): first on line {probs[0][0]}: {probs[0][1]}. "
                        "The sim may not read a broken file.")
        running = core.running_processes(self.repo.get("warn_processes").split(";"))
        if running:
            warn.append("⚠ Running now: " + ", ".join(running) + ". Prepar3D rewrites Prepar3D.cfg when it exits, "
                        "which can undo your edit — best to close the sim first.")
        locks = [r for r in self.repo.rules() if r.enabled and r.action == "revert"
                 and r.file_id in (None, self.file.id)]
        if locks:
            warn.append("ℹ This file has an auto-undo rule (" + ", ".join(r.name for r in locks) + "). Saving here "
                        "records your edit as the new accepted version, so it will not be undone — except a "
                        "'no longer matches restore point' rule, which compares against the restore point.")
        dlg = SaveDialog(f"Save {self.file.display}", core.unified_diff(disk, new, "on disk", "after save"),
                         "<br>".join(warn), self)
        if dlg.exec() != QDialog.Accepted:
            return False
        try:
            if disk is not None:
                self.repo.put_object(disk)
            self.repo.commit(f"Before edit of {self.file.display}", kind="safety", file_ids=[self.file.id])
            core.write_file_atomic(self.file.path, new)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Could not save: {e}\n\nIs the file open in the sim, or does "
                                "the app need administrator rights?")
            return False
        note = dlg.note.text().strip()
        self.repo._stat_cache.pop(self.file.id, None)
        self.repo.commit(f"Edited {self.file.display}" + (f": {note}" if note else ""), kind="manual",
                         file_ids=[self.file.id])
        self.repo.log(f"Edited {self.file.display} in the built-in editor" + (f" — {note}" if note else ""),
                      file_id=self.file.id)
        self.disk_hash = core.sha256_bytes(new)
        self.missing = False
        self.editor.document().setModified(False)
        self.banner.hide()
        self.statusBar().showMessage("Saved and recorded in History.", 5000)
        if self.on_saved:
            self.on_saved()
        return True

    def restore_version(self):
        if QMessageBox.question(self, APP_NAME, f"Overwrite {self.file.path}\nwith {self.version_label}?") \
                != QMessageBox.Yes:
            return
        try:
            self.repo.restore_version(self.file.id, self.version_hash, self.version_label)
            if self.on_saved:
                self.on_saved()
            self.statusBar().showMessage("Restored.", 5000)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, APP_NAME, f"Could not restore: {e}")

    def closeEvent(self, ev):
        if not self.read_only and self.editor.document().isModified():
            r = QMessageBox.question(self, APP_NAME, f"Save changes to {self.file.display}?",
                                     QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
            if r == QMessageBox.Cancel or (r == QMessageBox.Save and not self.save()):
                ev.ignore()
                return
        ev.accept()
