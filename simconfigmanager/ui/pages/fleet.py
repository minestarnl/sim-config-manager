"""Fleet: every sim at a glance."""
from __future__ import annotations


import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QSplitter, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...theme import TLabel as QLabel, TTextEdit as QTextEdit  # theme-aware versions
from ...core import fmt_ts


from ..common import item, selected_data, setup_table

class FleetPage:
    """Mixin for MainWindow — Fleet page."""

    def build_fleet_tab(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        top = QHBoxLayout()
        self.fleet_banner = QLabel()
        self.fleet_banner.setTextFormat(Qt.RichText)
        self.fleet_banner.setWordWrap(True)
        top.addWidget(self.fleet_banner, 1)
        b = QPushButton("Refresh")
        b.clicked.connect(self.refresh_fleet)
        top.addWidget(b)
        lay.addLayout(top)
        split = QSplitter(Qt.Vertical)
        self.fleet_table = QTableWidget()
        setup_table(self.fleet_table, ["Sim", "Online", "Config version", "Files differ", "Rulebook",
                                       "Alerts (24 h)", "Waiting for approval", "Last change", "Graphics driver",
                                       "App"], 8)
        self.fleet_table.itemSelectionChanged.connect(self.show_fleet_detail)
        split.addWidget(self.fleet_table)
        self.fleet_detail = QTextEdit()
        self.fleet_detail.setReadOnly(True)
        split.addWidget(self.fleet_detail)
        split.setSizes([360, 260])
        lay.addWidget(split, 1)
        self._fleet = []
        return w

    def refresh_fleet(self):
        local = core.build_status(self.repo)
        self._fleet = [local]
        self.render_fleet()
        if self.sync() is None:
            self.fleet_banner.setText("<b>Fleet overview</b><br>Only this PC is shown. Set up sharing (Cloud tab: a "
                                      "shared folder or Google Drive) on every sim to see them all here.")
            return
        self.fleet_banner.setText("<b>Fleet overview</b> — loading the other sims…")

        def done(statuses):
            others = [x for x in statuses if x.get("machine") != self.repo.machine]
            self._fleet = [local] + sorted(others, key=lambda x: x.get("machine", ""))
            self.render_fleet()

        def fail(e):
            self.fleet_banner.setText(f"<b>Fleet overview</b> — could not read the other sims: {html.escape(str(e))}")
        self.run_bg(lambda r: core.make_sync(r).statuses(), done, fail)

    def render_fleet(self):
        now = core.now_ts()
        rows = self._fleet
        problems = 0
        self.fleet_table.setRowCount(len(rows))
        for i, st in enumerate(rows):
            age = now - st.get("heartbeat", 0)
            me = st.get("machine") == self.repo.machine
            online = me or age < 900
            mism = st.get("mismatches", [])
            viol = st.get("rulebook_violations", [])
            ok = online and not mism and not viol
            problems += 0 if ok else 1
            ver = st.get("version")
            gpu = (st.get("system", {}).get("gpu", "") or "").split("\n")
            drv = next((ln.split(":", 1)[1].strip() for ln in gpu if "Driver version" in ln), "–")
            self.fleet_table.setItem(i, 0, item(("● " if ok else "▲ ") + st.get("machine", "?") +
                                               ("  (this PC)" if me else ""), st.get("machine"),
                                               "#2e7d32" if ok else "#e65100", bold=True))
            self.fleet_table.setItem(i, 1, item("yes" if online else f"no — last seen {fmt_ts(st.get('heartbeat', 0))}",
                                               color=None if online else "#c62828"))
            self.fleet_table.setItem(i, 2, item(f"v{ver['number']} – {ver['title']}" if ver else "no version yet",
                                               color=None if ver else "#777"))
            self.fleet_table.setItem(i, 3, item(len(mism) if mism else "✔ none", color="#e65100" if mism else
                                               "#2e7d32"))
            self.fleet_table.setItem(i, 4, item(f"{len(viol)} broken" if viol else "✔", color="#c62828" if viol else
                                               "#2e7d32"))
            self.fleet_table.setItem(i, 5, item(st.get("alerts_24h", 0)))
            self.fleet_table.setItem(i, 6, item(st.get("pending_proposals", 0) or ""))
            self.fleet_table.setItem(i, 7, item(fmt_ts(st["last_change"]) if st.get("last_change") else "–"))
            self.fleet_table.setItem(i, 8, item(drv))
            self.fleet_table.setItem(i, 9, item(st.get("app_version", "")))
        n = len(rows)
        if not self.fleet_banner.text().endswith("loading the other sims…") or n > 1:
            self.fleet_banner.setText(
                f"<b>Fleet overview</b> — {n} sim(s): " +
                ("<span style='color:#2e7d32'>all OK</span>" if not problems else
                 f"<span style='color:#e65100'>{problems} need attention</span>"))

    def show_fleet_detail(self):
        ids = selected_data(self.fleet_table)
        st = next((x for x in self._fleet if ids and x.get("machine") == ids[0]), None)
        if not st:
            self.fleet_detail.clear()
            return
        parts = [f"<h3 style='margin:0'>{html.escape(st.get('machine', ''))}</h3>",
                 f"<p style='color:#555'>Last heartbeat {fmt_ts(st.get('heartbeat', 0))} · app "
                 f"{html.escape(st.get('app_version', ''))} · {st.get('files', 0)} files tracked</p>"]
        if st.get("mismatches"):
            parts.append("<b>Files that differ from the version:</b><ul>" + "".join(
                f"<li>{html.escape(m['file'])} [{html.escape(m['group'])}] — "
                f"{html.escape(core.STATUS_TEXT.get(m['status'], m['status']))}</li>" for m in st["mismatches"]) + "</ul>")
        if st.get("rulebook_violations"):
            parts.append("<b style='color:#c62828'>Rulebook broken:</b><ul>" + "".join(
                f"<li>{html.escape(v)}</li>" for v in st["rulebook_violations"]) + "</ul>")
        if st.get("recent_alerts"):
            parts.append("<b>Recent alerts:</b><ul>" + "".join(
                f"<li>{fmt_ts(a['ts'])} — {html.escape(a['message'])}</li>" for a in st["recent_alerts"]) + "</ul>")
        for k, v in (st.get("system") or {}).items():
            parts.append(f"<b>{html.escape(core.BUILTIN_PROBES.get(k, (k,))[0])}</b><pre>{html.escape(v)}</pre>")
        self.fleet_detail.setHtml("".join(parts))
