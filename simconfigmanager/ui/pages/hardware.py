"""Hardware & health: required cockpit devices, disks / GPU / projectors, and the maintenance schedule."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QInputDialog, QLineEdit, QScrollArea, QSpinBox,
    QTableWidget, QVBoxLayout, QWidget,
)

from ... import core
from ...core import fmt_ts
from ...theme import TLabel as QLabel
from ..common import (BAD, MUTED, OK, WARN, button_row, colored, confirm, fail, hint, inform, item, section,
                      selected_data, setup_table)
from . import Page

STATE = {"ok": ("✔ OK", OK), "soon": ("◷ Due soon", WARN), "overdue": ("✖ Overdue", BAD)}


class HardwarePage:
    """Mixin for MainWindow — Hardware & health page."""

    PAGE = Page("hardware", "Hardware & health", "hardware",
                "Cockpit devices, disks, GPU, projectors and the maintenance schedule", "build_hardware_page",
                "refresh_hardware")

    def build_hardware_page(self):
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        w = QWidget()
        lay = QVBoxLayout(w)
        outer.setWidget(w)

        box, bl = section("Cockpit hardware — alert and NO-GO when a required device is missing")
        self.hw_req = QTableWidget()
        setup_table(self.hw_req, ["Status", "Device", "Matches", "Note"], 1)
        self.hw_req.setMinimumHeight(130)
        bl.addWidget(self.hw_req)
        bl.addLayout(button_row(("Remove…", self.remove_required_device),
                                trailing=[("Check now", self.service_hardware, {"icon": "refresh"})]))
        bl.addWidget(hint("Connected now (USB, HID and COM). Select a device and mark it as required:"))
        self.hw_devs = QTableWidget()
        setup_table(self.hw_devs, ["Device", "Kind", "Instance ID", "Status"], 2)
        self.hw_devs.setMinimumHeight(170)
        self.hw_devs.doubleClicked.connect(lambda _i: self.mark_required())
        bl.addWidget(self.hw_devs)
        bl.addLayout(button_row(("Mark as required…", self.mark_required, {"primary": True})))
        lay.addWidget(box)

        box, bl = section("Health — checked every 15 minutes")
        self.health_label = QLabel()
        self.health_label.setWordWrap(True)
        bl.addWidget(self.health_label)
        self.disk_table = QTableWidget()
        setup_table(self.disk_table, ["Disk", "Type", "Health", "Temperature", "Wear", "Power-on hours", "Read errors"], 0)
        self.disk_table.setMinimumHeight(110)
        bl.addWidget(self.disk_table)
        self.proj_table = QTableWidget()
        setup_table(self.proj_table, ["Projector", "Address", "Power", "Lamp hours", "Lamp used", "Errors"], 5)
        self.proj_table.setMinimumHeight(110)
        self.proj_table.doubleClicked.connect(lambda _i: self.edit_projector())
        bl.addWidget(self.proj_table)
        bl.addLayout(button_row(("Add projector…", lambda: self.edit_projector(new=True)),
                                ("Edit…", self.edit_projector), ("Remove…", self.remove_projector),
                                trailing=[("Check now", lambda: self.service_health(force=True), {"icon": "refresh"})]))
        lay.addWidget(box)

        box, bl = section("Maintenance schedule")
        self.maint_label = QLabel()
        bl.addWidget(self.maint_label)
        self.maint_table = QTableWidget()
        setup_table(self.maint_table, ["Status", "Task", "Every", "Last done", "Due", "Note"], 5)
        self.maint_table.setMinimumHeight(150)
        self.maint_table.doubleClicked.connect(lambda _i: self.mark_maintenance_done())
        bl.addWidget(self.maint_table)
        bl.addLayout(button_row(("Mark done…", self.mark_maintenance_done, {"primary": True}),
                                ("Add task…", lambda: self.edit_maintenance(new=True)),
                                ("Edit…", self.edit_maintenance), ("Delete…", self.delete_maintenance)))
        lay.addWidget(box)
        lay.addStretch(1)
        return outer

    # ---------------------------------------------------------------- refresh
    def refresh_hardware(self):
        self.refresh_hardware_devices(self.repo.hardware_status())
        h = self.repo.health_status()
        warn = h.get("warnings") or []
        g = h.get("gpu") or {}
        parts = [colored(f"⚠ {len(warn)} warning(s): " + "; ".join(warn), WARN, True) if warn
                 else colored("✔ No health warnings", OK)]
        if g:
            parts.append(f"GPU {g.get('name', '')}: {g.get('temp', 0):.0f} °C, {g.get('util', 0):.0f}% busy")
        parts.append(f"last check {fmt_ts(h['ts'])}" if h.get("ts") else "not checked yet")
        self.health_label.setText(" · ".join(parts))
        disks = h.get("disks") or []
        self.disk_table.setRowCount(len(disks))
        for r, d in enumerate(disks):
            healthy = d.get("health") in ("Healthy", None, "")
            self.disk_table.setItem(r, 0, item(d.get("name") or "?"))
            self.disk_table.setItem(r, 1, item(d.get("media") or ""))
            self.disk_table.setItem(r, 2, item(d.get("health") or "–", color=OK if healthy else BAD))
            self.disk_table.setItem(r, 3, item(f"{d['temp']} °C" if d.get("temp") else "–",
                                              color=BAD if (d.get("temp") or 0) >= core.LIMITS["disk_temp"] else None))
            self.disk_table.setItem(r, 4, item(f"{d['wear']}%" if d.get("wear") is not None else "–",
                                              color=BAD if (d.get("wear") or 0) >= core.LIMITS["disk_wear"] else None))
            self.disk_table.setItem(r, 5, item(f"{d['hours']:,}" if d.get("hours") else "–"))
            self.disk_table.setItem(r, 6, item(d.get("read_errors") if d.get("read_errors") is not None else "–"))
        live = {p.get("id"): p for p in h.get("projectors") or []}
        projs = self.repo.projectors()
        self.proj_table.setRowCount(len(projs))
        for r, p in enumerate(projs):
            st = live.get(p["id"]) or {}
            pct = st.get("lamp_pct")
            self.proj_table.setItem(r, 0, item(p["name"], p["id"], bold=True))
            self.proj_table.setItem(r, 1, item(p["host"]))
            self.proj_table.setItem(r, 2, item(st.get("error") and "Not reachable" or st.get("power") or "–",
                                              color=BAD if st.get("error") else None))
            self.proj_table.setItem(r, 3, item(f"{st['lamp_hours']:,}" if st.get("lamp_hours") is not None else "–"))
            self.proj_table.setItem(r, 4, item(f"{pct}% of {p['lamp_life']:,} h" if pct is not None else "–",
                                              color=BAD if pct and pct >= core.LIMITS["lamp_pct"] else None))
            self.proj_table.setItem(r, 5, item(", ".join(st.get("errors") or []) or st.get("error") or "",
                                              color=BAD if st.get("errors") or st.get("error") else None))
        tasks = self.repo.maintenance_tasks()
        self.maint_label.setText(f"{self.repo.sim_hours():,} sim hours recorded · " + (
            colored(f"{sum(t['state'] == 'overdue' for t in tasks)} overdue", BAD, True)
            if any(t["state"] == "overdue" for t in tasks) else colored("nothing overdue", OK)))
        self.maint_table.setRowCount(len(tasks))
        for r, t in enumerate(tasks):
            label, col = STATE[t["state"]]
            every = " or ".join(x for x in (f"{t['every_days']:g} days" if t["every_days"] else "",
                                            f"{t['every_hours']:g} sim hours" if t["every_hours"] else "") if x)
            due = []
            if t["due_in_days"] is not None:
                due.append(fmt_ts(t["due_date"])[:10])
            if t["due_in_hours"] is not None:
                due.append(f"in {t['due_in_hours']:g} sim h" if t["due_in_hours"] >= 0
                           else f"{-t['due_in_hours']:g} sim h ago")
            self.maint_table.setItem(r, 0, item(label, t["id"], col, bold=t["state"] != "ok"))
            self.maint_table.setItem(r, 1, item(t["name"]))
            self.maint_table.setItem(r, 2, item(every))
            self.maint_table.setItem(r, 3, item(fmt_ts(t["last_done"]) if t["last_done"] else "never"))
            self.maint_table.setItem(r, 4, item(" · ".join(due)))
            self.maint_table.setItem(r, 5, item(t["note"] or ""))
        overdue = sum(t["state"] == "overdue" for t in tasks)
        if self.remote:
            self.set_badge("hardware", len(self.repo.hardware_status().get("missing", [])) + overdue)

    def refresh_hardware_devices(self, st: dict):
        req = st.get("required") or []
        self.hw_req.setRowCount(len(req))
        for r, d in enumerate(req):
            self.hw_req.setItem(r, 0, item("✔ Connected" if d["ok"] else ("⚠ Not working" if d["present"]
                                                                          else "✖ Missing"),
                                           d["id"], OK if d["ok"] else BAD, bold=not d["ok"]))
            self.hw_req.setItem(r, 1, item(d["label"]))
            self.hw_req.setItem(r, 2, item(d["match"]))
            self.hw_req.setItem(r, 3, item(d.get("note") or ""))
        devs = st.get("devices") or []
        self.hw_devs.setRowCount(len(devs))
        for r, d in enumerate(devs):
            self.hw_devs.setItem(r, 0, item(d["name"], d))
            self.hw_devs.setItem(r, 1, item(d["cls"]))
            self.hw_devs.setItem(r, 2, item(d["id"], color=MUTED))
            self.hw_devs.setItem(r, 3, item(d["status"], color=None if d["status"] in ("OK", "") else WARN))
        if not st.get("available") and not devs:
            self.hw_devs.setRowCount(1)
            self.hw_devs.setItem(0, 0, item("The device list is read on the sim PC (Windows) every 30 seconds.",
                                            color=MUTED))

    # ---------------------------------------------------------------- devices
    def mark_required(self):
        devs = [d for d in selected_data(self.hw_devs) if isinstance(d, dict)]
        if not devs:
            inform(self, "Select a connected device in the lower list first.")
            return
        d = devs[0]
        label, ok = QInputDialog.getText(self, "Required device", "Name (e.g. 'Captain yoke', 'MCP panel'):",
                                         text=d["name"])
        if not ok or not label.strip():
            return
        match, ok = QInputDialog.getText(self, "Required device", "Matches devices whose instance ID looks like "
                                         "(* = anything; keep the serial number to require this exact unit):",
                                         text=core.device_key(d["id"]))
        if not ok or not match.strip():
            return
        self.repo.add_required_device(label.strip(), match.strip())
        self.sync_dirty = True
        self.service_hardware() if not self.remote else None
        self.refresh_hardware()

    def remove_required_device(self):
        ids = selected_data(self.hw_req)
        if ids and confirm(self, "Stop requiring this device?", "Remove", danger=True):
            for i in ids:
                self.repo.remove_required_device(i)
            self.refresh_hardware()

    # ---------------------------------------------------------------- projectors
    def edit_projector(self, new: bool = False):
        p = {"name": "", "host": "", "password": "", "lamp_life": 3000}
        if not new:
            ids = selected_data(self.proj_table)
            if not ids:
                return
            p = next(x for x in self.repo.projectors() if x["id"] == ids[0])
        dlg = QDialog(self)
        dlg.setWindowTitle("Projector")
        form = QFormLayout(dlg)
        form.addRow(hint("Read over PJLink (port 4352) — switch on 'network standby' / PJLink in the projector's "
                         "network menu. Nothing is ever sent to the projector except status questions."))
        name, host, pw = QLineEdit(p["name"]), QLineEdit(p["host"]), QLineEdit(p.get("password") or "")
        pw.setEchoMode(QLineEdit.Password)
        name.setPlaceholderText("e.g. Left, Centre, Right")
        host.setPlaceholderText("IP address, e.g. 192.168.10.41")
        pw.setPlaceholderText("Only if the projector asks for a PJLink password")
        life = QSpinBox()
        life.setRange(0, 100000)
        life.setSuffix(" h")
        life.setValue(int(p.get("lamp_life") or 0))
        for label, wdg in (("Name:", name), ("Address:", host), ("PJLink password:", pw), ("Lamp life:", life)):
            form.addRow(label, wdg)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted or not name.text().strip() or not host.text().strip():
            return
        self.repo.save_projector({"id": p.get("id"), "name": name.text().strip(), "host": host.text().strip(),
                                  "password": pw.text(), "lamp_life": life.value()})
        if not self.remote:
            self.service_health()
        self.refresh_hardware()

    def remove_projector(self):
        ids = selected_data(self.proj_table)
        if ids and confirm(self, "Remove this projector?", "Remove", danger=True):
            self.repo.delete_projector(ids[0])
            self.refresh_hardware()

    # ---------------------------------------------------------------- maintenance
    def edit_maintenance(self, new: bool = False):
        t = {"name": "", "every_days": 90, "every_hours": 0, "note": ""}
        if not new:
            ids = selected_data(self.maint_table)
            if not ids:
                return
            t = next(x for x in self.repo.maintenance_tasks() if x["id"] == ids[0])
        dlg = QDialog(self)
        dlg.setWindowTitle("Maintenance task")
        form = QFormLayout(dlg)
        name, note = QLineEdit(t["name"]), QLineEdit(t.get("note") or "")
        name.setPlaceholderText("e.g. Clean projector filters")
        days, hours = QDoubleSpinBox(), QDoubleSpinBox()
        for sp, suf, mx in ((days, " days", 3650), (hours, " sim hours", 100000)):
            sp.setRange(0, mx)
            sp.setDecimals(0)
            sp.setSuffix(suf)
            sp.setSpecialValueText("—")
        days.setValue(t.get("every_days") or 0)
        hours.setValue(t.get("every_hours") or 0)
        form.addRow("Task:", name)
        form.addRow("Every:", days)
        form.addRow("or every:", hours)
        form.addRow("Note:", note)
        form.addRow(hint("Whichever comes first. Sim hours are counted from the recorded sessions."))
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec() != QDialog.Accepted or not name.text().strip():
            return
        if not days.value() and not hours.value():
            fail(self, "save the task", "Set an interval in days, sim hours or both.")
            return
        self.repo.save_maintenance_task({"id": t.get("id"), "name": name.text().strip(), "every_days": days.value(),
                                         "every_hours": hours.value(), "note": note.text().strip()})
        self.sync_dirty = True
        self.refresh_hardware()

    def mark_maintenance_done(self):
        ids = selected_data(self.maint_table)
        if not ids:
            return
        t = next(x for x in self.repo.maintenance_tasks() if x["id"] == ids[0])
        note, ok = QInputDialog.getText(self, "Maintenance done", f"{t['name']} — note (optional):")
        if not ok:
            return
        self.repo.maintenance_done(t["id"], self.repo.get("version_author") or "", note.strip())
        self.sync_dirty = True
        self.notify(f"Recorded: {t['name']}")
        self.refresh_hardware()

    def delete_maintenance(self):
        ids = selected_data(self.maint_table)
        if ids and confirm(self, "Delete this maintenance task?", "Delete", danger=True,
                           details="Its log entries are kept."):
            self.repo.delete_maintenance_task(ids[0])
            self.refresh_hardware()


__all__ = ["HardwarePage"]
