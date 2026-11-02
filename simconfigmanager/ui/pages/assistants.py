"""AI & network: Claude / ChatGPT, proposed changes and network access."""
from __future__ import annotations


import html
import json
import os
import sys

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QMenu,
    QApplication, QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QListWidget,
    QListWidgetItem, QSpinBox, QSystemTrayIcon, QTableWidget, QVBoxLayout, QWidget,
)

from ... import core, net, theme
from ...theme import TLabel as QLabel, TTextEdit as QTextEdit  # theme-aware versions
from ...core import fmt_ts


from ..dialogs import NetSetupDialog, ProposalDialog
from ..common import (BAD, MUTED, OK, WARN, button, button_row, colored, confirm, fail, heading, hint, inform,
                      item, section, selected_data, setup_table, warn)

from . import Page


class AssistantsPage:
    """Mixin for MainWindow — AI & network page."""

    PAGE = Page("assistants", "AI & network", "claude", "Claude, ChatGPT, proposed changes and network access", "build_assistants_page", "refresh_assistants")

    def build_assistants_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addWidget(hint(
            "Claude (and ChatGPT) can read every tracked file, its history and versions, the rulebook and the other "
            "sims' files — to troubleshoot problems and help dial in settings. They can also <b>propose</b> changes, "
            "but nothing is written until you approve it here, and every applied change is recorded in History. "
            "Try: <i>“Why do we get stutters on Sim 2 but not Sim 1?”</i> · <i>“What changed since v3?”</i>"))

        box, bl = section("Claude Desktop on this PC", QHBoxLayout)
        self.claude_setup_box = box
        self.claude_status = QLabel()
        self.claude_status.setTextFormat(Qt.RichText)
        self.claude_status.setWordWrap(True)
        bl.addWidget(self.claude_status, 1)
        self.claude_connect_btn = button("Connect", self.toggle_claude_connection, primary=True)
        bl.addWidget(self.claude_connect_btn)
        bl.addWidget(button("Manual setup…", self.show_claude_manual))
        lay.addWidget(box)

        nbox, nl = section("Network access — Claude, ChatGPT or the remote app on other computers (e.g. MacBooks)")
        row = QHBoxLayout()
        self.net_enable = QCheckBox("Allow connections from the local network")
        self.net_enable.setChecked(self.repo.get("net_enabled") == "1")
        self.net_enable.toggled.connect(self.toggle_network)
        row.addWidget(self.net_enable)
        row.addWidget(QLabel("Port:"))
        self.net_port = QSpinBox()
        self.net_port.setRange(1024, 65535)
        self.net_port.setValue(int(self.repo.get("net_port") or net.DEFAULT_PORT))
        self.net_port.editingFinished.connect(self.change_net_port)
        row.addWidget(self.net_port)
        row.addStretch()
        nl.addLayout(row)
        self.net_status = QLabel()
        self.net_status.setTextFormat(Qt.RichText)
        self.net_status.setWordWrap(True)
        self.net_status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        nl.addWidget(self.net_status)
        self.key_table = QTableWidget()
        setup_table(self.key_table, ["Person / computer", "Access", "Key created", "Last connected"], 0)
        self.key_table.setMaximumHeight(140)
        nl.addWidget(self.key_table)
        setup = button("Setup instructions")
        m = QMenu(setup)
        m.addAction("Claude Desktop on a Mac…", lambda: self.show_net_setup("mac"))
        m.addAction("Claude Desktop on a Windows laptop…", lambda: self.show_net_setup("windows"))
        m.addAction("ChatGPT (through a Mac)…", lambda: self.show_net_setup("chatgpt"))
        m.addAction("Remote app (this app on another computer)…", lambda: self.show_net_setup("app"))
        setup.setMenu(m)
        nl.addLayout(button_row(("Add person…", self.add_net_key), setup, ("Remove access…", self.remove_net_key)))
        lay.addWidget(nbox)
        self.net_server = None
        self._net_activity = []
        if self.remote:
            # Claude Desktop hookup and the network server belong to the sim PC itself
            self.claude_setup_box.hide()
            self.net_enable.setEnabled(False)
            self.net_port.setEnabled(False)
        elif self.net_enable.isChecked():
            QTimer.singleShot(500, lambda: self.start_network(quiet=True))

        self.claude_allow = QCheckBox("Allow AI assistants to propose changes (you always approve before anything "
                                      "is written)")
        self.claude_allow.setChecked(self.repo.get("claude_proposals") != "0")
        self.claude_allow.toggled.connect(lambda v: self.repo.set("claude_proposals", "1" if v else "0"))
        lay.addLayout(button_row(heading("Proposed changes"), self.claude_allow,
                                 trailing=[("Review…", self.review_selected_proposal),
                                           ("Copy files for an AI chat…", self.copy_for_claude)]))
        self.prop_table = QTableWidget()
        setup_table(self.prop_table, ["Status", "When", "From", "File", "Reason"], 4)
        self.prop_table.doubleClicked.connect(lambda _i: self.review_selected_proposal())
        lay.addWidget(self.prop_table, 1)
        self._seen_proposals = {p["id"] for p in core.list_proposals(self.repo, "pending")}
        QTimer.singleShot(1500, lambda: self.check_proposals(startup=True))
        return w

    def start_network(self, quiet: bool = False) -> bool:
        self.stop_network()
        port = self.net_port.value()
        srv = net.NetworkServer(self.repo.data_dir, port=port, on_activity=self._net_activity.append)
        srv.keys = net.load_keys(self.repo)
        try:
            srv.start()
        except OSError as e:
            if not quiet:
                fail(self, f"open port {port}", e, "Is another program using it? Pick another port.")
            self.net_status.setText(colored(f"✖ Could not open port {port}: {e}", BAD))
            return False
        self.net_server = srv
        ok, msg = net.set_firewall_rule(port, True)
        if not ok and not quiet:
            fail(self, "add the Windows Firewall rule", msg, "Other computers may be blocked. Run the app as "
                 "administrator.")
        self.repo.log(f"Network access switched on (port {port})")
        self.refresh_network()
        return True

    def stop_network(self):
        if self.net_server:
            self.net_server.stop()
            self.net_server = None

    def toggle_network(self, on: bool):
        self.repo.set("net_enabled", "1" if on else "0")
        if on:
            if not net.load_keys(self.repo):
                inform(self, "Network access is on. Now click Add person… to create an access key for each "
                             "laptop.")
            if not self.start_network():
                self.net_enable.setChecked(False)
        else:
            self.stop_network()
            net.set_firewall_rule(self.net_port.value(), False)
            self.repo.log("Network access switched off")
        self.refresh_network()

    def change_net_port(self):
        old = int(self.repo.get("net_port") or net.DEFAULT_PORT)
        new = self.net_port.value()
        if new == old:
            return
        self.repo.set("net_port", new)
        if self.net_server:
            net.set_firewall_rule(old, False)
            self.start_network()
            inform(self, "Port changed. Every laptop needs the new setup (Setup instructions).")

    def net_urls(self) -> list[str]:
        port = self.net_port.value()
        if self.remote:
            return [f"{self.repo.base}/mcp"]
        hosts = net.lan_addresses() + [f"{net.hostname().lower()}.local"]
        return [f"http://{h}:{port}/mcp" for h in hosts]

    def refresh_network(self):
        self.update_statusbar()
        keys = net.load_keys(self.repo)
        seen = self.net_server.last_seen if self.net_server else {}
        if self.net_server:
            self.net_server.keys = keys
            urls = self.net_urls()
            self.net_status.setText(
                colored("✔ Listening.", OK, True) + " Laptops connect to "
                + " or ".join(f"<b>{html.escape(u)}</b>" for u in urls[:2])
                + ". Only computers on this local network are let through the firewall, and each needs its own key. "
                  "Tip: give this PC a fixed IP address (DHCP reservation in the router) so the setup keeps working.")
        elif self.remote:
            self.net_status.setText("Network access is switched on and off at the sim PC itself; people and keys "
                                    "can be managed here.")
        elif self.net_enable.isChecked():
            pass
        else:
            self.net_status.setText("Off. Switch on to let Claude, ChatGPT or the remote app on another computer "
                                    "work with this sim.")
        self.key_table.setRowCount(len(keys))
        for r, k in enumerate(keys):
            ls = seen.get(k["name"])
            self.key_table.setItem(r, 0, item(k["name"], k["key"], bold=True))
            lvl = net.key_level(k)
            self.key_table.setItem(r, 1, item(net.ACCESS_SHORT[lvl], color="#6a1b9a" if lvl == "full" else None,
                                              bold=lvl == "full"))
            self.key_table.setItem(r, 2, item(fmt_ts(k["created"])))
            self.key_table.setItem(r, 3, item(f"{fmt_ts(ls[0])} from {ls[1]}" if ls else "–"))

    def add_net_key(self):
        name, ok = QInputDialog.getText(self, "Add person", "Name of the person or laptop (e.g. 'Jan – MacBook Pro'):")
        if not ok or not name.strip():
            return
        if any(k["name"].lower() == name.strip().lower() for k in net.load_keys(self.repo)):
            warn(self, "That name already has a key. Use another name, or remove the old key first.")
            return
        levels = list(net.ACCESS_LEVELS.items())
        choice, ok = QInputDialog.getItem(self, "Add person", f"What may {name} do?", [v for _k, v in levels], 1,
                                          False)
        if not ok:
            return
        lvl = levels[[v for _k, v in levels].index(choice)][0]
        if lvl == "full" and not confirm(self, f"Give {name} full control?", "Give full control",
                                         details=f"{name} can change, restore and create versions of every tracked "
                                                 "file on this sim from another computer. Everything is logged with "
                                                 "their name."):
            return
        k = net.add_key(self.repo, name, lvl != "read", full_control=lvl == "full")
        self.repo.log(f"Network access key created for {k['name']} ({net.ACCESS_LEVELS[lvl]})")
        self.refresh_network()
        for r in range(self.key_table.rowCount()):
            if self.key_table.item(r, 0).text() == k["name"]:
                self.key_table.selectRow(r)
        if not self.net_enable.isChecked() and not self.remote:
            self.net_enable.setChecked(True)
        self.show_net_setup("app" if lvl == "full" else "mac")

    def remove_net_key(self):
        ids = selected_data(self.key_table)
        if not ids:
            return
        keys = net.load_keys(self.repo)
        k = next(k for k in keys if k["key"] == ids[0])
        if confirm(self, f"Remove access for {k['name']}?", "Remove access", danger=True,
                   details="Their key stops working immediately."):
            net.save_keys(self.repo, [x for x in keys if x["key"] != k["key"]])
            self.repo.log(f"Network access removed for {k['name']}")
            self.refresh_network()

    def show_net_setup(self, kind: str):
        keys = net.load_keys(self.repo)
        if not keys:
            inform(self, "First click Add person… to create a key.")
            return
        ids = selected_data(self.key_table)
        key = next((k for k in keys if ids and k["key"] == ids[0]), keys[-1])
        dlg = NetSetupDialog(self.repo.machine, key, self.net_urls(), kind, self)
        dlg.exec()

    def drain_net_activity(self):
        acts, self._net_activity[:] = list(self._net_activity), []
        for a in acts:
            self.repo.log(a)
        if acts and self.is_current("assistants"):
            self.refresh_network()

    def refresh_assistants(self):
        self.refresh_network()
        if self.remote:
            self.refresh_proposals_table()
            return
        connected = core.claude_desktop_connected()
        if connected:
            self.claude_status.setText(colored("✔ Connected.", OK, True) + " Restart Claude Desktop once after "
                                       "connecting, then look for <b>sim-config-manager</b> in a chat's tools menu.")
            self.claude_connect_btn.setText("Disconnect")
            self.claude_connect_btn.setProperty("primary", False)
        else:
            self.claude_status.setText("Not connected. Install Claude Desktop (claude.ai/download) on this PC, then "
                                       "click <b>Connect</b>.")
            self.claude_connect_btn.setText("Connect")
            self.claude_connect_btn.setProperty("primary", True)
        self.claude_connect_btn.style().unpolish(self.claude_connect_btn)
        self.claude_connect_btn.style().polish(self.claude_connect_btn)
        self.refresh_proposals_table()

    def refresh_proposals_table(self):
        props = list(reversed(core.list_proposals(self.repo)))
        style = {"pending": ("Waiting for approval", WARN), "applied": ("Applied", OK), "rejected": ("Rejected", MUTED)}
        self.prop_table.setRowCount(len(props))
        for r, p in enumerate(props):
            text, col = style.get(p["status"], (p["status"], None))
            self.prop_table.setItem(r, 0, item(text, p["id"], col, bold=p["status"] == "pending"))
            self.prop_table.setItem(r, 1, item(fmt_ts(p["created"])))
            self.prop_table.setItem(r, 2, item(p.get("author", "Claude")))
            self.prop_table.setItem(r, 3, item(p["label"]))
            self.prop_table.setItem(r, 4, item(p["reason"].replace("\n", " ")[:200]))

    def toggle_claude_connection(self):
        enable = not core.claude_desktop_connected()
        try:
            paths = core.connect_claude_desktop(self.repo.data_dir, enable)
        except Exception as e:  # noqa: BLE001
            fail(self, "update Claude Desktop's settings", e)
            return
        cmd, _args = core.connector_command(self.repo.data_dir)
        if enable and getattr(sys, "frozen", False) and not os.path.exists(cmd):
            warn(self, f"The connector is missing. Put SimConfigManager-MCP.exe next to SimConfigManager.exe:\n{cmd}")
        inform(self, ("Connected" if enable else "Disconnected") + ". Updated:\n" + "\n".join(paths) +
               "\n\nQuit Claude Desktop completely (also from its tray icon) and start it again.")
        self.refresh_assistants()

    def show_claude_manual(self):
        cmd, args = core.connector_command(self.repo.data_dir)
        snippet = json.dumps({"mcpServers": {core.MCP_SERVER_KEY: {"command": cmd, "args": args}}}, indent=2)
        dlg = QDialog(self)
        dlg.setWindowTitle("Claude Desktop — manual setup")
        dlg.resize(720, 360)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("In Claude Desktop: Settings → Developer → Edit Config, and add this to "
                             "claude_desktop_config.json (merge with what is there), then restart Claude Desktop:"))
        t = QTextEdit()
        t.setPlainText(snippet)
        t.setFont(theme.mono_font(10))
        lay.addWidget(t)
        lay.addLayout(button_row(("Copy", lambda: QApplication.clipboard().setText(snippet))))
        dlg.exec()

    def check_proposals(self, startup: bool = False):
        try:
            pend = core.list_proposals(self.repo, "pending")
        except Exception:  # noqa: BLE001
            return
        seen = getattr(self, "_seen_proposals", set())
        new = [p for p in pend if p["id"] not in seen]
        self._seen_proposals = seen | {p["id"] for p in pend}
        if startup and pend:
            new = pend
        if new:
            p = new[-1]
            self.tray.showMessage(f"{p.get('author', 'Claude')} proposes a change to {p['label']}",
                                  p["reason"][:180] + "\n\nOpen SimConfigManager → AI & network to review.",
                                  QSystemTrayIcon.Information, 15000)
            if self.is_current("assistants"):
                self.refresh_assistants()
        self.set_badge("assistants", len(pend))

    def review_selected_proposal(self):
        ids = selected_data(self.prop_table)
        pend = core.list_proposals(self.repo)
        prop = next((p for p in pend if ids and p["id"] == ids[0]), None)
        if prop is None:
            prop = next((p for p in reversed(pend) if p["status"] == "pending"), None)
        if prop is None:
            inform(self, "There are no proposed changes to review.")
            return
        ProposalDialog(self.repo, prop, self).exec()
        self.sync_dirty = True
        self.check_proposals()
        self.refresh_all()

    def copy_for_claude(self):
        files = []
        if self.current_page() == "files":
            files = [self.repo.file(i) for i in selected_data(self.files_table)]
        if not files:
            names = [f"{f.display}  [{f.group}]" for f in self.repo.files()]
            dlg = QDialog(self)
            dlg.setWindowTitle("Copy files for an AI chat")
            dlg.resize(560, 480)
            lay = QVBoxLayout(dlg)
            lay.addWidget(hint("Tick the files to copy, then paste into any Claude or ChatGPT chat (also on your "
                               "phone)."))
            lst = QListWidget()
            for f, n in zip(self.repo.files(), names):
                it = QListWidgetItem(n)
                it.setData(Qt.UserRole, f.id)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Unchecked)
                lst.addItem(it)
            lay.addWidget(lst)
            bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            bb.accepted.connect(dlg.accept)
            bb.rejected.connect(dlg.reject)
            lay.addWidget(bb)
            if dlg.exec() != QDialog.Accepted:
                return
            files = [self.repo.file(lst.item(i).data(Qt.UserRole)) for i in range(lst.count())
                     if lst.item(i).checkState() == Qt.Checked]
        files = [f for f in files if f]
        if not files:
            return
        md = core.files_as_markdown(self.repo, files)
        QApplication.clipboard().setText(md)
        self.notify(f"Copied {len(files)} file(s) ({len(md):,} characters) — paste them into an AI chat")
