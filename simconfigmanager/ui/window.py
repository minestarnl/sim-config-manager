"""The main window: tray icon, monitoring loop, VS Code-style chrome."""
from __future__ import annotations


import html
import threading
import json
import sys
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QSize
from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QMainWindow, QMenu, QSystemTrayIcon, QTabWidget, QVBoxLayout,
    QWidget,
)

from .. import core, theme
from ..theme import TLabel as QLabel  # theme-aware versions
from ..core import (APP_TITLE, APP_VERSION, Repo, fmt_ts)


from .pages.alerts import AlertsPage
from .pages.assistants import AssistantsPage
from .pages.files import FilesPage
from .pages.fleet import FleetPage
from .pages.history import HistoryPage
from .pages.restore_points import RestorePointsPage
from .pages.rulebook import RulebookPage
from .pages.settings import SettingsPage
from .pages.sharing import SharingPage
from .pages.versions import VersionsPage
from .common import make_icon, make_icon_remote
from .pages import Page
from .pages.sessions import SessionsPage
from .pages.hardware import HardwarePage
from .pages.installs import InstallsPage
from .pages.templates import TemplatesPage
from .services import SimServices
from .onboarding import maybe_run_onboarding

PAGE_ORDER = (FleetPage, VersionsPage, RestorePointsPage, FilesPage, HistoryPage, SessionsPage, HardwarePage,
              InstallsPage, TemplatesPage, RulebookPage, AlertsPage, SharingPage, AssistantsPage,
              SettingsPage)   # activity bar order; Settings sits at the bottom


class MainWindow(*PAGE_ORDER, SimServices, QMainWindow):
    """The main window: activity bar + one page per area. Each page is a mixin in ui/pages/."""

    def __init__(self, repo: Repo, start_hidden: bool = False):
        super().__init__()
        self.repo = repo
        self.remote = core.is_remote(repo)
        self.host = repo.host_info() if self.remote else {"platform": sys.platform}
        self.paused = False
        self.sync_dirty = False
        self.setWindowTitle(f"{APP_TITLE} — {repo.machine}")
        self.setWindowIcon(make_icon())
        self.resize(1200, 760)

        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.setDocumentMode(True)
        self.pages: dict[str, QWidget] = {}
        self.badges: dict[str, int] = {}
        for cls in PAGE_ORDER:
            spec = cls.PAGE
            page = getattr(self, spec.build)()
            self.pages[spec.key] = page
            self.tabs.addTab(page, spec.title)
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
            self.setWindowTitle(f"{APP_TITLE} — {repo.machine} (remote, {repo.base})")
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
        self.start_services()
        self.refresh_all()
        if not start_hidden:
            self.show()
            QTimer.singleShot(400, lambda: maybe_run_onboarding(self))

    def build_tray(self):
        self.tray = QSystemTrayIcon(make_icon(), self)
        self.tray.setToolTip(f"{APP_TITLE} — watching config files")
        menu = QMenu()
        menu.addAction("Open", self.show_window)
        self.tray_version_action = menu.addAction("Version status…", self.open_versions)
        menu.addAction("Create version…", self.create_version)
        menu.addSeparator()
        menu.addAction("Record snapshot…", self.snapshot_now)
        menu.addAction("Export certification archive…", lambda: self.export_archive())
        self.tray_rp_menu = menu.addMenu("Restore point")
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
            a = self.tray_rp_menu.addAction("No restore points yet")
            a.setEnabled(False)
        for rp in rps:
            self.tray_rp_menu.addAction(rp.name, lambda r=rp: self.restore_rp(r))

    def show_window(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.refresh_all()
        maybe_run_onboarding(self)

    def open_alerts(self):
        self.show_window()
        self.go("alerts")

    def closeEvent(self, ev):
        # closing the window just hides to tray; monitoring keeps running
        ev.ignore()
        self.hide()
        if not self.repo.get("tray_hint_shown"):
            self.tray.showMessage(APP_TITLE, "Still running in the tray and watching your files.",
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

    def notify(self, text: str, title: str = "", warning: bool = False, ms: int = 6000) -> None:
        """One way to report a result: status bar always, tray balloon when the window is hidden or it's a warning."""
        self.statusBar().showMessage(text, max(ms, 8000))
        if warning or not self.isVisible():
            self.tray.showMessage(title or APP_TITLE, text,
                                  QSystemTrayIcon.Warning if warning else QSystemTrayIcon.Information, ms)

    def set_paused(self, on: bool):
        self.paused = on
        self.tray.setToolTip(f"{APP_TITLE} — {'PAUSED' if on else 'watching config files'}")
        self.statusBar().showMessage("Monitoring paused" if on else "Monitoring resumed", 8000)

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
            self.service_attribute(changes)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Could not check the files: {e}")
            return
        self.sync_dirty = True
        for a in alerts[:5]:
            icon = QSystemTrayIcon.Warning
            self.tray.showMessage(f"{APP_TITLE}: {a.rule.name}", a.message, icon, 10000)
        if alerts:
            self.tray.setIcon(make_icon(alert=True))
            QTimer.singleShot(30_000, lambda: self.tray.setIcon(make_icon()))
        self.statusBar().showMessage(f"{fmt_ts(core.now_ts())}: {len(changes)} file(s) changed"
                                     + (f", {len(alerts)} alert(s)" if alerts else ""))
        if self.isVisible():
            self.refresh_all()
        else:
            self.update_version_indicator()

    def refresh_all(self):
        self.rebuild_tray_rp_menu()
        spec = self.current_spec()
        if spec and spec.refresh:
            getattr(self, spec.refresh)()
        self.update_version_indicator()

    # ================================================================= page navigation
    def current_page(self) -> str:
        page = self.tabs.currentWidget()
        return next((k for k, w in self.pages.items() if w is page), "")

    def current_spec(self) -> Optional[Page]:
        key = self.current_page()
        return next((c.PAGE for c in PAGE_ORDER if c.PAGE.key == key), None)

    def is_current(self, key: str) -> bool:
        return self.isVisible() and self.current_page() == key

    def go(self, key: str) -> None:
        """Show a page (by key, see PAGE_ORDER)."""
        self.tabs.setCurrentWidget(self.pages[key])

    def set_badge(self, key: str, count: int) -> None:
        """Number shown on a page's activity-bar icon (0 = none)."""
        if self.badges.get(key, 0) != count:
            self.badges[key] = count
            self.sync_activity()

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
        for idx, cls in enumerate(PAGE_ORDER):
            spec = cls.PAGE
            b = QToolButton()
            b.setCheckable(True)
            b.setFixedSize(48, 44)
            b.setIconSize(QSize(24, 24))
            b.setToolTip(spec.title)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _c=False, i=idx: self.tabs.setCurrentIndex(i))
            group.addButton(b)
            self.activity[spec.key] = b
            if spec.key == "settings":
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
        self.sb_app = QLabel(f"{APP_TITLE} {APP_VERSION}")
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
        cur = self.current_page()
        for cls in PAGE_ORDER:
            spec = cls.PAGE
            b = self.activity[spec.key]
            n = self.badges.get(spec.key, 0)
            badge = str(n) if n else None
            ico = QIcon()
            ico.addPixmap(theme.glyph_pixmap(spec.icon, theme.T["activity_fg"], 24, badge), QIcon.Normal, QIcon.Off)
            ico.addPixmap(theme.glyph_pixmap(spec.icon, theme.T["activity_active"], 24, badge), QIcon.Normal, QIcon.On)
            ico.addPixmap(theme.glyph_pixmap(spec.icon, theme.T["activity_active"], 24, badge), QIcon.Active, QIcon.Off)
            b.setIcon(ico)
            b.setChecked(spec.key == cur)
            b.setToolTip(spec.title + (f" ({n})" if n else ""))
        spec = self.current_spec()
        if spec:
            self.page_title.setText(spec.title.upper())
            self.page_subtitle.setText(spec.subtitle)

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
        self.tray.setToolTip(f"{APP_TITLE} — {state}{txt}")
        self.tray_version_action.setText(f"Version status: {txt}")
        self.setWindowTitle(f"{APP_TITLE} — {self.repo.machine}{' (remote)' if self.remote else ''} — {txt}")
        glyph = "warning" if (ver and mm) else ("check" if ver else "tag")
        self.update_statusbar(f"{self._glyph(glyph)} {html.escape(txt)}")
        prev = getattr(self, "_last_mismatch_count", None)
        self._last_mismatch_count = len(mm)
        if ver and prev == 0 and mm and self.repo.get("notify_version_drift") != "0":
            self.tray.showMessage(f"{APP_TITLE}: files differ from v{ver.number}",
                                  ", ".join(m.file.display for m in mm[:5]), QSystemTrayIcon.Warning, 8000)

    def open_versions(self):
        self.show_window()
        self.go("versions")

    def run_bg(self, fn, done=None, err=None, write: bool = False):
        """Run fn(store) on a background worker; callbacks run on the UI thread.
        Reads use their own read-only store connection; ``write=True`` jobs (sessions, watchdog, health …) run on
        a second worker with a writable connection, so a slow scan never blocks the UI or the reads."""
        import concurrent.futures
        if not hasattr(self, "_bg_pools"):
            self._bg_pools = {k: concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"scm-{k}")
                              for k in ("read", "write")}
            self._bg_local = threading.local()
        data_dir = self.repo.data_dir
        remote_repo = self.repo if self.remote else None

        def job():
            if remote_repo is not None:
                return fn(remote_repo)
            attr = "repo_w" if write else "repo_r"
            r = getattr(self._bg_local, attr, None)
            if r is None:
                r = Repo(data_dir, read_only=not write)
                setattr(self._bg_local, attr, r)
                cache = getattr(self.repo, "_probe_cache", None)
                if cache is not None:
                    r._probe_cache = cache          # share the system-information readings
                    r.background_probes = True
            return fn(r)
        fut = self._bg_pools["write" if write else "read"].submit(job)

        def poll():
            if not fut.done():
                QTimer.singleShot(150, poll)
                return
            e = fut.exception()
            if e is not None:
                (err or (lambda x: self.statusBar().showMessage(f"Background task failed: {x}", 8000)))(e)
            elif done:
                done(fut.result())
        QTimer.singleShot(150, poll)

    def pref(self, key: str) -> str:
        if not self.remote:
            return self.repo.get(key)
        from ..remote import client_config_dir
        try:
            return str(json.loads((client_config_dir() / "settings.json").read_text(encoding="utf-8")).get(key, ""))
        except (OSError, ValueError):
            return ""

    def set_pref(self, key: str, value) -> None:
        if not self.remote:
            self.repo.set(key, value)
            return
        from ..remote import client_config_dir
        p = client_config_dir() / "settings.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[key] = str(value)
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")
