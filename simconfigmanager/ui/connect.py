"""Remote mode: pick a sim PC on the network and open a window for it."""
from __future__ import annotations


from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QDialog, QFileDialog, QInputDialog, QLineEdit, QListWidget, QListWidgetItem,
    QVBoxLayout,
)



from .common import button_row, confirm, fail, hint, make_icon_remote

_REMOTE_WINDOWS: list = []


class ConnectDialog(QDialog):
    """Pick (or add) a sim PC to work on over the network."""

    def __init__(self, parent=None):
        super().__init__(parent)
        from .. import remote
        self.remote = remote
        self.setWindowTitle("Connect to a sim")
        self.setWindowIcon(make_icon_remote())
        self.resize(620, 400)
        lay = QVBoxLayout(self)
        lay.addWidget(hint("Work on a sim PC over the network, as if you were sitting at it. The sim needs network "
                           "access switched on and a <b>Full control</b> key for you (on the sim: AI &amp; network → "
                           "Add person…)."))
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _it: self.connect())
        lay.addWidget(self.list, 1)
        lay.addLayout(button_row(("Add…", self.add), ("Add from file…", self.add_file), ("Remove…", self.remove),
                                 trailing=[("Connect", self.connect, {"primary": True})]))
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
        key, ok = QInputDialog.getText(self, "Add sim", "Access key (on the sim: AI & network → Setup instructions → "
                                       "Remote app):", QLineEdit.Password)
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
            fail(self, "read the connection file", e)
            return
        self._store(conn)

    def remove(self):
        it = self.list.currentItem()
        if it and confirm(self, f"Forget {it.data(Qt.UserRole)['name']}?", "Forget",
                          details="Only removes it from this list; the sim itself is not changed."):
            name = it.data(Qt.UserRole)["name"]
            self.remote.save_connections([c for c in self.remote.load_connections() if c["name"] != name])
            self.reload()

    def connect(self):
        it = self.list.currentItem()
        if not it:
            return
        c = it.data(Qt.UserRole)
        from .window import MainWindow
        rr = self.remote.RemoteRepo(c["url"], c["key"], c["name"])
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            rr.ping()
            win = MainWindow(rr)
        except Exception as e:  # noqa: BLE001
            QApplication.restoreOverrideCursor()
            fail(self, f"connect to {c['name']}", e, "Is the sim PC on, is network access switched on there, and is "
                 "the key still valid?")
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
