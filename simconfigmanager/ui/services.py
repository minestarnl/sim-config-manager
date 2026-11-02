"""Background work on the sim PC itself: sessions, hardware watchdog, health, 'who changed it', templates.

Kept as light as possible so it never costs the sim frame rate:
* every job runs on a background worker; helper programs (PowerShell …) run at below-normal priority;
* while the sim program runs ("quiet while flying", on by default) only the cheap things happen: file checks
  (a stat per file), a session sample every 10 s (one small file read, GPU via the driver library), and the
  hardware check — which is only triggered when Windows reports a device was plugged in or out.
  System information, health and template checks wait until the sim has closed;
* the hardware watchdog doesn't poll: it reacts to Windows device-change messages, with a check every 10 min
  as a fallback.
In remote mode none of this runs here — the sim does it.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer

from .. import core

SESSION_SECONDS = 10
HARDWARE_SECONDS = 600       # fallback; device changes trigger a check right away
HEALTH_MINUTES = 15
TEMPLATE_MINUTES = 10


class SimServices:
    """Mixin for MainWindow."""

    def start_services(self):
        self.live = {}            # last session_tick result: running, live data, gpu, open session/benchmark
        self._busy = set()
        if self.remote:
            return
        for name, secs, fn in (("session", SESSION_SECONDS, self.service_sessions),
                               ("hardware", HARDWARE_SECONDS, self.service_hardware),
                               ("health", HEALTH_MINUTES * 60, self.service_health),
                               ("templates", TEMPLATE_MINUTES * 60, self.service_templates),
                               ("audit", 3600, self.service_audit_refresh)):
            t = QTimer(self)
            t.timeout.connect(fn)
            t.start(secs * 1000)
            setattr(self, f"_{name}_timer", t)
        self._hw_debounce = QTimer(self)
        self._hw_debounce.setSingleShot(True)
        self._hw_debounce.timeout.connect(self.service_hardware)
        install_device_change_filter(self)
        QTimer.singleShot(4000, self.service_sessions)
        QTimer.singleShot(6000, self.service_hardware)
        QTimer.singleShot(45_000, self.service_health)
        QTimer.singleShot(60_000, self.service_templates)

    def flying(self) -> bool:
        """The sim program is running and 'quiet while flying' is on: postpone everything that isn't needed."""
        return bool(self.live.get("running")) and self.repo.get("quiet_while_flying") != "0"

    def device_changed(self):
        """Windows reported a device change (USB plugged in/out): check the cockpit hardware in 3 s."""
        self._hw_debounce.start(3000)

    def _job(self, name: str, fn, done=None):
        """One run at a time per service."""
        if name in self._busy:
            return
        self._busy.add(name)

        def ok(res):
            self._busy.discard(name)
            if done:
                done(res)

        def failed(e):
            self._busy.discard(name)
            self.statusBar().showMessage(f"{name.capitalize()} check failed: {e}", 8000)
        self.run_bg(fn, ok, failed, write=True)

    # ---------------------------------------------------------------- sessions (#1, #2, #5)
    def service_sessions(self):
        def done(state):
            before = (self.live.get("session") or {}).get("id"), (self.live.get("benchmark") or {}).get("id")
            was_flying = self.flying()
            self.live = state
            if was_flying and not self.flying():
                QTimer.singleShot(20_000, self.catch_up)   # run what was postponed during the session
            after = (state.get("session") or {}).get("id"), (state.get("benchmark") or {}).get("id")
            if before[1] and not after[1]:
                self.notify("Benchmark finished — see the Sessions page")
            if before != after and self.is_current("sessions"):
                self.refresh_sessions()
            elif self.is_current("sessions"):
                self.update_live_banner()
        self._job("session", lambda r: r.session_tick(), done)

    # ---------------------------------------------------------------- hardware watchdog (#4)
    def service_hardware(self):
        def done(st):
            prev = getattr(self, "_hw_missing", None)
            self._hw_missing = st.get("missing", [])
            if prev is not None and set(self._hw_missing) - set(prev):
                self.notify("Cockpit hardware missing: " + ", ".join(sorted(set(self._hw_missing) - set(prev))),
                            warning=True, ms=15000)
            self.set_badge("hardware", len(self._hw_missing))
            if self.is_current("hardware"):
                self.refresh_hardware_devices(st)
        self._job("hardware", lambda r: r.hardware_check(), done)

    def catch_up(self):
        self.service_health()
        self.service_templates()
        self.refresh_probes_bg()

    # ---------------------------------------------------------------- health (#14)
    def service_health(self, force: bool = False):
        if self.flying() and not force:
            return
        def done(res):
            new = set(res.get("warnings", [])) - set(getattr(self, "_health_warnings", []))
            self._health_warnings = res.get("warnings", [])
            if new:
                self.notify("Health: " + "; ".join(sorted(new)[:3]), warning=True, ms=12000)
            if self.is_current("hardware"):
                self.refresh_hardware()
        self._job("health", lambda r: r.health_check(), done)

    # ---------------------------------------------------------------- templates (#9)
    def service_templates(self):
        if self.flying():
            return
        def done(waiting):
            names = sorted(t["name"] for t in waiting)
            if names and names != getattr(self, "_tpl_waiting", []):
                self.notify("Template update ready for this sim: " + ", ".join(names) + " (Templates page)", ms=12000)
            self._tpl_waiting = names
            self.set_badge("templates", len(names))
        self._job("templates", lambda r: r.templates_waiting(), done)

    # ---------------------------------------------------------------- who changed it (#3)
    def service_attribute(self, changes):
        if self.repo.get("audit_enabled") != "1":
            return
        since = core.now_ts() - max(120, int(self.repo.get("poll_seconds") or 5) * 3)
        todo = [(c.file.id, c.new_hash) for c in changes]

        def work(r):
            return [r.attribute_change(fid, h, since) for fid, h in todo]

        def done(res):
            if any(res) and self.is_current("files"):
                self.refresh_file_history()
        self.run_bg(work, done, lambda e: None, write=True)

    def service_audit_refresh(self):
        """New tracked files need their own audit entry: re-apply when the number of files changed."""
        if self.repo.get("audit_enabled") != "1":
            return
        n = sum(1 for f in self.repo.files() if not core.is_probe(f))
        if str(n) != self.repo.get("audit_files"):
            self._job("audit", lambda r: r.enable_file_auditing())


def install_device_change_filter(win) -> None:
    """Call win.device_changed() when Windows broadcasts WM_DEVICECHANGE (no polling)."""
    import sys
    if sys.platform != "win32":
        return
    from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication
    import ctypes.wintypes

    class _Filter(QAbstractNativeEventFilter):
        def nativeEventFilter(self, event_type, message):  # noqa: N802
            try:
                et = event_type.data() if hasattr(event_type, "data") else event_type
                if et in (b"windows_generic_MSG", "windows_generic_MSG"):
                    msg = ctypes.wintypes.MSG.from_address(int(message))
                    if msg.message == 0x0219:        # WM_DEVICECHANGE (DBT_DEVNODES_CHANGED, arrival, removal)
                        win.device_changed()
            except Exception:  # noqa: BLE001
                pass
            return False, 0

    win._device_filter = _Filter()
    QCoreApplication.instance().installNativeEventFilter(win._device_filter)
    win.winId()   # the (hidden) main window needs a native window to receive the broadcast
