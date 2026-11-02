"""Sessions: every flight session with the version it ran on, performance per version, benchmarks and A/B tests."""
from __future__ import annotations

import html
import json

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QInputDialog, QLineEdit, QSpinBox,
    QSplitter, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core, theme
from ...core import fmt_ts
from ...theme import TLabel as QLabel
from ..common import (BAD, MUTED, OK, WARN, banner, button, button_row, colored, confirm, fail, heading, hint,
                      inform, item, selected_data, setup_table)
from . import Page


def _dur(minutes) -> str:
    if minutes is None:
        return "–"
    m = int(round(minutes))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


class FpsChart(QWidget):
    """Frame rate (and GPU temperature) over a session."""

    def __init__(self, samples: list[dict], parent=None):
        super().__init__(parent)
        self.samples = samples
        self.setMinimumHeight(160)

    def paintEvent(self, _ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(theme.T["panel"]))
        pts = [(s["ts"], s["fps"]) for s in self.samples if s.get("fps")]
        if len(pts) < 2:
            p.setPen(QColor(theme.T["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, "No frame-rate data (live data bridge not running)")
            return
        t0, t1 = pts[0][0], pts[-1][0]
        top = max(v for _t, v in pts) * 1.1 or 1
        x = lambda t: 40 + (t - t0) / max(1, t1 - t0) * (w - 50)   # noqa: E731
        y = lambda v: h - 20 - v / top * (h - 30)                    # noqa: E731
        p.setPen(QPen(QColor(theme.T["border"])))
        for frac in (0.25, 0.5, 0.75, 1.0):
            yy = y(top * frac / 1.1)
            p.drawLine(40, int(yy), w - 10, int(yy))
            p.setPen(QColor(theme.T["muted"]))
            p.drawText(2, int(yy) + 4, f"{top * frac / 1.1:.0f}")
            p.setPen(QPen(QColor(theme.T["border"])))
        p.setPen(QPen(QColor(theme.T["blue"]), 1.6))
        for (ta, va), (tb, vb) in zip(pts, pts[1:]):
            p.drawLine(QPointF(x(ta), y(va)), QPointF(x(tb), y(vb)))
        p.setPen(QColor(theme.T["muted"]))
        p.drawText(42, h - 4, fmt_ts(t0)[11:16])
        p.drawText(w - 50, h - 4, fmt_ts(t1)[11:16])
        p.drawText(44, 14, "fps")


class SessionsPage:
    """Mixin for MainWindow — Sessions page."""

    PAGE = Page("sessions", "Sessions", "pulse", "Every session with its version, performance, crashes and benchmarks",
                "build_sessions_page", "refresh_sessions")

    def build_sessions_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        self.live_banner = QLabel()
        self.live_banner.setTextFormat(Qt.RichText)
        self.live_banner.setWordWrap(True)
        self.bridge_btn = button("Install live-data bridge…", self.install_bridge_dialog)
        lay.addLayout(button_row(self.live_banner, stretch=False, trailing=[self.bridge_btn]))

        split = QSplitter(Qt.Vertical)
        top = QWidget()
        tl = QVBoxLayout(top)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(heading("Performance per version"))
        tl.addWidget(hint("Sessions in which every file matched a version, grouped by that version. Use it to see "
                          "whether a new version made things smoother or less stable."))
        self.perf_table = QTableWidget()
        setup_table(self.perf_table, ["Version", "Sessions", "Hours", "Average fps", "1% low fps", "Stutters / h",
                                      "Crashes", "Display resets", "Problems / 10 h"], 0)
        tl.addWidget(self.perf_table)
        split.addWidget(top)

        low = QWidget()
        ll = QVBoxLayout(low)
        ll.setContentsMargins(0, 0, 0, 0)
        self.sess_kind = QComboBox()
        for label, val in (("All", ""), ("Sessions", "session"), ("Benchmarks", "benchmark")):
            self.sess_kind.addItem(label, val)
        self.sess_kind.currentIndexChanged.connect(lambda _i: self.refresh_sessions())
        ll.addLayout(button_row(heading("Sessions and benchmarks"), self.sess_kind,
                                trailing=[("Start benchmark…", self.start_benchmark_dialog, {"icon": "play"}),
                                          ("A/B test…", self.ab_test_dialog),
                                          ("Compare selected", self.compare_selected_benchmarks)]))
        self.sess_table = QTableWidget()
        setup_table(self.sess_table, ["When", "Kind", "Length", "Version", "Aircraft", "Average fps", "1% low",
                                      "Max GPU °C", "Result"], 4)
        self.sess_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.sess_table.doubleClicked.connect(lambda _i: self.show_session())
        ll.addWidget(self.sess_table)
        ll.addLayout(button_row(("Details…", self.show_session), ("Add note…", self.note_session),
                                trailing=[("Delete…", self.delete_session_dialog)]))
        split.addWidget(low)
        split.setSizes([230, 420])
        lay.addWidget(split, 1)
        lay.addWidget(hint("Ask Claude or ChatGPT to tune the sim with the <b>autotune</b> prompt: it reads these "
                           "benchmarks, proposes one change at a time for you to approve, and you run the benchmark "
                           "again."))
        return w

    # ---------------------------------------------------------------- refresh
    def update_live_banner(self):
        st = self.live if not self.remote else getattr(self, "_remote_live", {})
        s, b, live, gpu = st.get("session"), st.get("benchmark"), st.get("live"), st.get("gpu")
        if b:
            left = max(0, (b.get("planned_end") or 0) - core.now_ts())
            txt = colored(f"● Benchmark running — {b['label']}", WARN, True) + f" · {int(left // 60)}:{int(left % 60):02d} left"
        elif s:
            ver = f"v{s['version_number']}" if s.get("version_number") else "no version"
            match = "all files match" if s.get("version_matches") else colored("files differ from the version", WARN)
            txt = colored("● Session running", OK, True) + f" since {fmt_ts(s['started'])[11:16]} on {ver} ({match})"
        elif st.get("running") is False:
            txt = colored("○ The sim is not running.", MUTED)
        else:
            txt = "Sessions are recorded on the sim PC while Prepar3D runs."
        if live:
            txt += f" · {live.get('fps', 0):.0f} fps · {html.escape(live.get('aircraft') or '')}"
        if gpu:
            txt += f" · GPU {gpu.get('temp', 0):.0f} °C"
        self.live_banner.setText(txt)

    def refresh_sessions(self):
        if self.remote:
            def got(state):
                self._remote_live = state
                self.update_live_banner()
            def state(r):
                open_ = [x for x in r.sessions(10) if not x["ended"]]
                return {"live": r.live_data(), "running": None,
                        "session": next((x for x in open_ if x["kind"] == "session"), None),
                        "benchmark": next((x for x in open_ if x["kind"] == "benchmark"), None)}
            self.run_bg(state, got, lambda e: None)
        self.update_live_banner()
        folders = self.repo.fsuipc_folders()
        installed = any(self.repo.bridge_installed(f) for f in folders)
        self.bridge_btn.setVisible(not installed)
        self.bridge_btn.setToolTip("Lets the app read frame rate, aircraft and position from Prepar3D (FSUIPC Lua)"
                                   if folders else "FSUIPC was not found on this sim")
        perf = self.repo.performance_by_version()
        self.perf_table.setRowCount(len(perf))
        best = max((p["avg_fps"] or 0 for p in perf), default=0)
        for r, p in enumerate(perf):
            self.perf_table.setItem(r, 0, item(p["label"], p["version"], bold=True))
            self.perf_table.setItem(r, 1, item(p["sessions"]))
            self.perf_table.setItem(r, 2, item(p["hours"]))
            self.perf_table.setItem(r, 3, item(p["avg_fps"] if p["avg_fps"] is not None else "–",
                                              color=OK if best and p["avg_fps"] == best else None))
            self.perf_table.setItem(r, 4, item(p["low1_fps"] if p["low1_fps"] is not None else "–"))
            self.perf_table.setItem(r, 5, item(p["stutters_per_hour"] if p["stutters_per_hour"] is not None else "–"))
            self.perf_table.setItem(r, 6, item(p["crashes"], color=BAD if p["crashes"] else None))
            self.perf_table.setItem(r, 7, item(p["display_resets"], color=BAD if p["display_resets"] else None))
            self.perf_table.setItem(r, 8, item(p["crashes_per_10h"] if p["crashes_per_10h"] is not None else "–"))
        ss = self.repo.sessions(300, self.sess_kind.currentData() or None)
        self.sess_table.setRowCount(len(ss))
        for r, s in enumerate(ss):
            st = s["stats"] or {}
            crash = s["crash"] or {}
            if not s["ended"]:
                res, col = "Running", WARN
            elif crash.get("crashes"):
                res, col = f"Crashed ({crash['crashes'][0].get('module', '?')})", BAD
            elif crash.get("display_resets"):
                res, col = f"{len(crash['display_resets'])} display reset(s)", BAD
            else:
                res, col = "OK", OK
            ver = f"v{s['version_number']}" if s["version_number"] else "–"
            if s["version_number"] and not s["version_matches"]:
                ver += " (files differed)"
            self.sess_table.setItem(r, 0, item(fmt_ts(s["started"]), s["id"]))
            self.sess_table.setItem(r, 1, item("Benchmark: " + s["label"] if s["kind"] == "benchmark" else "Session",
                                              color="#6a1b9a" if s["kind"] == "benchmark" else None))
            self.sess_table.setItem(r, 2, item(_dur(st.get("minutes")) if s["ended"] else "…"))
            self.sess_table.setItem(r, 3, item(ver))
            self.sess_table.setItem(r, 4, item(s["aircraft"] or "–"))
            self.sess_table.setItem(r, 5, item(st.get("avg_fps", "–")))
            self.sess_table.setItem(r, 6, item(st.get("low1_fps", "–")))
            self.sess_table.setItem(r, 7, item(st.get("max_gpu_temp", "–")))
            self.sess_table.setItem(r, 8, item(res + (f" · {s['note']}" if s.get("note") else ""), color=col))

    # ---------------------------------------------------------------- actions
    def selected_session(self):
        ids = selected_data(self.sess_table)
        return self.repo.session(ids[0]) if ids else None

    def show_session(self):
        s = self.selected_session()
        if not s:
            return
        samples = self.repo.session_samples(s["id"])
        st, crash = s["stats"] or {}, s["crash"] or {}
        dlg = QDialog(self)
        dlg.setWindowTitle(("Benchmark: " + s["label"]) if s["kind"] == "benchmark" else f"Session {fmt_ts(s['started'])}")
        dlg.resize(820, 620)
        lay = QVBoxLayout(dlg)
        ver = f"v{s['version_number']}" if s["version_number"] else "no version"
        rows = [("When", f"{fmt_ts(s['started'])} → {fmt_ts(s['ended']) if s['ended'] else 'running'}"),
                ("Length", _dur(st.get("minutes"))),
                ("Version", ver + ("" if s["version_matches"] else " — these files differed: " +
                                   ", ".join(s["differing"] or []))),
                ("Aircraft", s["aircraft"] or "–"), ("Route", f"{s['start_pos'] or '?'} → {s['end_pos'] or '?'}"),
                ("Frame rate", f"average {st.get('avg_fps', '–')}, median {st.get('median_fps', '–')}, "
                               f"1% low {st.get('low1_fps', '–')}, lowest {st.get('min_fps', '–')}, "
                               f"{st.get('stutters', 0)} stutter(s)"),
                ("GPU", f"max {st.get('max_gpu_temp', '–')} °C, average {st.get('avg_gpu_temp', '–')} °C, "
                        f"{st.get('avg_gpu_util', '–')}% busy")]
        lay.addWidget(QLabel("<table cellpadding=3>" + "".join(
            f"<tr><td style='color:{MUTED}'>{html.escape(k)}</td><td>{html.escape(str(v))}</td></tr>"
            for k, v in rows) + "</table>"))
        for c in crash.get("crashes", []):
            lay.addWidget(banner(f"<b>{html.escape(str(c.get('app')))} crashed</b> at {fmt_ts(c.get('ts') or 0)} — "
                                 f"faulting module <b>{html.escape(str(c.get('module')))}</b>, exception "
                                 f"{html.escape(str(c.get('code')))}", "bad"))
        for d in crash.get("display_resets", []):
            lay.addWidget(banner(f"Display driver reset at {fmt_ts(d.get('ts') or 0)}: "
                                 f"{html.escape(str(d.get('message', ''))[:200])}", "bad"))
        lay.addWidget(FpsChart(samples), 1)
        if s.get("snapshot_id"):
            lay.addLayout(button_row(("Compare those files with the files on disk now",
                                      lambda: self.compare_snapshot_with_current(s["snapshot_id"],
                                                                                 f"session {fmt_ts(s['started'])}"))))
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def note_session(self):
        s = self.selected_session()
        if not s:
            return
        note, ok = QInputDialog.getText(self, "Session note", "Note (e.g. 'Checkride B737, smooth'):",
                                        text=s.get("note") or "")
        if ok:
            self.repo.set_session_note(s["id"], note.strip())
            self.refresh_sessions()

    def delete_session_dialog(self):
        ids = selected_data(self.sess_table)
        if ids and confirm(self, f"Delete {len(ids)} session record(s)?", "Delete", danger=True,
                           details="Only the recording is removed; files and history are not affected."):
            for i in ids:
                self.repo.delete_session(i)
            self.refresh_sessions()

    def install_bridge_dialog(self):
        folders = self.repo.fsuipc_folders()
        if not folders:
            inform(self, "FSUIPC was not found on this sim. Track the FSUIPC6.ini (Files → Add) so the app knows "
                         "where it is, then try again.")
            return
        folder, ok = QInputDialog.getItem(self, "Live-data bridge", "FSUIPC folder:", folders, 0, False)
        if not ok:
            return
        if not confirm(self, "Install the live-data bridge?", "Install",
                       details=f"Adds SimConfigManager.lua to {folder} and starts it from the [Auto] section of the "
                               "FSUIPC ini (recorded in History, so it can be undone). It only reads data: frame rate, "
                               "aircraft, position. Restart Prepar3D afterwards. Lua plugins need a registered "
                               "FSUIPC."):
            return
        try:
            ini = self.repo.install_bridge(folder)
        except Exception as e:  # noqa: BLE001
            fail(self, "install the bridge", e)
            return
        self.notify(f"Bridge installed ({ini}). Restart Prepar3D to start it.")
        self.refresh_all()

    # ---------------------------------------------------------------- benchmarks and A/B tests
    def start_benchmark_dialog(self, label: str = ""):
        dlg = QDialog(self)
        dlg.setWindowTitle("Start benchmark")
        form = QFormLayout(dlg)
        form.addRow(hint("Load the benchmark flight in Prepar3D first (the same flight every time), then start. "
                         "The frame rate is recorded for the chosen time."))
        name = QLineEdit(label or self.repo.get("benchmark_label") or "Benchmark")
        minutes = QSpinBox()
        minutes.setRange(1, 60)
        minutes.setSuffix(" min")
        minutes.setValue(int(self.repo.get("benchmark_minutes") or 5))
        form.addRow("Name:", name)
        form.addRow("Record for:", minutes)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Start")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted:
            return None
        self.repo.set("benchmark_minutes", minutes.value())
        try:
            b = self.repo.start_benchmark(name.text().strip(), minutes.value())
        except Exception as e:  # noqa: BLE001
            fail(self, "start the benchmark", e)
            return None
        self.notify(f"Benchmark '{b['label']}' started — fly the benchmark flight; it stops by itself after "
                    f"{minutes.value()} min")
        self.refresh_sessions()
        return b

    def compare_selected_benchmarks(self):
        ids = selected_data(self.sess_table)
        if len(ids) < 2:
            inform(self, "Select two or more sessions or benchmarks (Ctrl-click) to compare them.")
            return
        rows = self.repo.compare_benchmarks(ids)
        self.show_comparison(rows)

    def show_comparison(self, rows: list[dict]):
        dlg = QDialog(self)
        dlg.setWindowTitle("Comparison")
        dlg.resize(760, 360)
        lay = QVBoxLayout(dlg)
        t = QTableWidget()
        keys = [("label", "Name"), ("version", "Version"), ("avg_fps", "Average fps"), ("low1_fps", "1% low fps"),
                ("stutters", "Stutters"), ("max_gpu_temp", "Max GPU °C"), ("minutes", "Minutes")]
        setup_table(t, ["", *[r.get("label") or f"#{r['id']}" for r in rows]], -1)
        t.setRowCount(len(keys))
        for i, (k, label) in enumerate(keys):
            t.setItem(i, 0, item(label, bold=True))
            vals = [r.get(k) for r in rows]
            nums = [v for v in vals if isinstance(v, (int, float))]
            best = (max(nums) if k in ("avg_fps", "low1_fps") else min(nums)) if nums and k not in (
                "minutes", "version") else None
            for j, v in enumerate(vals):
                t.setItem(i, j + 1, item("–" if v is None else (f"v{v}" if k == "version" and v else v),
                                         color=OK if best is not None and v == best and len(nums) > 1 else None))
        lay.addWidget(t)
        if len(rows) == 2 and rows[0].get("avg_fps") and rows[1].get("avg_fps"):
            a, b = rows
            diff = (b["avg_fps"] - a["avg_fps"]) / a["avg_fps"] * 100
            lay.addWidget(banner(f"<b>{html.escape(b.get('label') or '')}</b> is {abs(diff):.0f}% "
                                 f"{'faster' if diff > 0 else 'slower'} than <b>{html.escape(a.get('label') or '')}"
                                 f"</b> on average" + (f", 1% lows {b.get('low1_fps')} vs {a.get('low1_fps')}"
                                                         if a.get("low1_fps") else "") + ".", "info"))
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()

    def ab_test_dialog(self):
        """Guided A/B test: restore setup A, benchmark, restore setup B, benchmark, compare."""
        try:
            state = json.loads(self.repo.get("ab_test") or "{}")
        except ValueError:
            state = {}
        dlg = QDialog(self)
        dlg.setWindowTitle("A/B test")
        dlg.resize(620, 420)
        lay = QVBoxLayout(dlg)
        lay.addWidget(hint("Compare two setups on the same benchmark flight. Pick a restore point or version for "
                           "each, then follow the steps. Each restore takes a safety snapshot first."))
        choices = [("rp", rp.name, rp.snapshot_id) for rp in self.repo.restore_points()] + \
                  [("v", v.label, v.snapshot_id) for v in self.repo.versions()]
        form = QFormLayout()
        combos = {}
        for side in ("a", "b"):
            c = QComboBox()
            c.addItem("Files as they are now", None)
            for kind, label, sid in choices:
                c.addItem(("Restore point: " if kind == "rp" else "Version: ") + label, [label, sid])
            prev = state.get(side, {}).get("label")
            if prev:
                c.setCurrentIndex(max(0, c.findText(prev, Qt.MatchEndsWith)))
            combos[side] = c
            form.addRow(f"Setup {side.upper()}:", c)
        lay.addLayout(form)
        status = QLabel()
        status.setTextFormat(Qt.RichText)
        lay.addWidget(status)

        def save():
            for side in ("a", "b"):
                d = combos[side].currentData()
                state.setdefault(side, {})
                state[side]["label"] = d[0] if d else "now"
                state[side]["snapshot_id"] = d[1] if d else None
            self.repo.set("ab_test", json.dumps(state))

        def show():
            parts = []
            for side in ("a", "b"):
                sid = state.get(side, {}).get("benchmark")
                b = self.repo.session(sid) if sid else None
                res = "not run yet" if not b else ("running…" if not b["ended"] else
                                                   f"{(b['stats'] or {}).get('avg_fps', '–')} fps average")
                parts.append(f"<b>{side.upper()}</b>: {html.escape(combos[side].currentText())} — {res}")
            status.setText("<br>".join(parts))

        def run(side):
            save()
            sid = state[side]["snapshot_id"]
            if sid:
                self.do_restore(sid, state[side]["label"])
            b = self.start_benchmark_dialog(f"A/B {side.upper()}: {state[side]['label']}")
            if b:
                state[side]["benchmark"] = b["id"]
                self.repo.set("ab_test", json.dumps(state))
            show()

        def compare():
            ids = [state.get(s, {}).get("benchmark") for s in ("a", "b")]
            if not all(ids):
                inform(dlg, "Run both benchmarks first.")
                return
            self.show_comparison(self.repo.compare_benchmarks(ids))
        lay.addLayout(button_row(("1. Restore A and benchmark…", lambda: run("a")),
                                 ("2. Restore B and benchmark…", lambda: run("b")),
                                 ("3. Compare", compare, {"primary": True})))
        lay.addStretch(1)
        show()
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        dlg.exec()
        save()


__all__ = ["SessionsPage", "FpsChart"]
