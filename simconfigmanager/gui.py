"""Compatibility entry point: the desktop UI lives in simconfigmanager/ui/."""
from .ui import MainWindow, make_icon, make_icon_remote, open_connect_dialog  # noqa: F401
from .ui.dialogs import *  # noqa: F401,F403
