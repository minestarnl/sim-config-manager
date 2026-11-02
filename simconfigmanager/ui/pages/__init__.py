"""One module per page of the activity bar. Each page is a mixin of MainWindow and declares a ``PAGE``."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    key: str          # used by MainWindow.go(key) / set_badge(key, n)
    title: str        # activity-bar tooltip and page header
    icon: str         # codicon name (theme.ICONS)
    subtitle: str     # one line under the title
    build: str        # method that builds the page widget
    refresh: str = ""  # method that refreshes it when shown
