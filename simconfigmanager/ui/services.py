"""Background work on the sim PC itself: sessions, hardware watchdog, health, 'who changed it', templates.

Every job runs on the writable background worker (MainWindow.run_bg(write=True)), so the UI never waits for
PowerShell, nvidia-smi or the event log. In remote mode none of this runs here — the sim does it.
"""
from __future__ import annotations

from PySide6.QtCore import QTimer

from .. import core

SESSION_SECONDS = 5
HARDWARE_SECONDS = 30
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
        QTimer.singleShot(4000, self.service_sessions)
        QTimer.singleShot(6000, self.service_hardware)
        QTimer.singleShot(45_000, self.service_health)
        QTimer.singleShot(60_000, self.service_templates)

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
            self.live = state
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

    # ---------------------------------------------------------------- health (#14)
    def service_health(self):
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
