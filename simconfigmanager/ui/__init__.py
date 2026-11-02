"""SimConfigManager desktop UI (PySide6).

    common.py      shared building blocks (icons, tables, buttons, messages)
    dialogs.py     dialogs used by several pages
    window.py      MainWindow: tray, monitoring loop, activity bar, status bar
    pages/*.py     one mixin per page of the activity bar
    connect.py     remote mode: connect to a sim PC on the network
"""
from .common import make_icon, make_icon_remote  # noqa: F401
from .connect import open_connect_dialog  # noqa: F401
from .window import MainWindow  # noqa: F401
__all__ = ["MainWindow", "make_icon", "make_icon_remote", "open_connect_dialog"]
