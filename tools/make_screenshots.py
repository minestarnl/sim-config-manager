"""Regenerate the README screenshots with realistic demo data.

    QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py

Builds a throw-away store with P3D / ProSim-style files, simulates a day of changes, and saves PNGs to
docs/screenshots/. Real temp paths are shown as the Windows paths they stand for.
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = Path(os.environ.get("SCM_SHOTS_OUT") or ROOT / "docs" / "screenshots")
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QTableWidget  # noqa: E402

app = QApplication(sys.argv)
from simconfigmanager import theme  # noqa: E402
theme.init(app, os.environ.get("SCM_THEME", "dark"))
from simconfigmanager import core, gui  # noqa: E402
from simconfigmanager.ui import connect as gui_connect  # noqa: E402
from simconfigmanager.core import Repo, Rule  # noqa: E402

os.environ["USERNAME"] = "Sim Tech"
tmp = Path(tempfile.mkdtemp())
APP = r"C:\Users\Sim1\AppData\Roaming\Lockheed Martin\Prepar3D v5"
PD = r"C:\ProgramData\Lockheed Martin\Prepar3D v5"
PS = r"C:\ProSim737"

PREPAR3D_CFG = """[Main]
User Objects=Airplane, Helicopter
SimObjectPaths.0=SimObjects\\Airplanes
SimObjectPaths.1=SimObjects\\Rotorcraft
; Display settings tuned for the 3-projector visual system
[GRAPHICS]
TEXTURE_MAX_LOAD=4096
TEXTURE_BANDWIDTH_MULT=40
HDR_BRIGHTNESS=1.000000
HDR_BLOOM_MAGNITUDE=0.150000
FULLSCREEN=True
SHADER_CACHE_PRIMED=1
[DISPLAY.Device.NVIDIA GeForce RTX 4080.0]
Mode=3840x2160x32
TriLinear=1
[TERRAIN]
LOD_RADIUS=6.500000
MESH_COMPLEXITY=100
TEXTURE_RESOLUTION=29
[WEATHER]
WeatherServerAddress=localhost
DynamicWeather=0
"""
DLL_XML = """<?xml version="1.0" encoding="Windows-1252"?>
<SimBase.Document Type="Launch" version="1,0">
  <Descr>Launch</Descr>
  <Filename>dll.xml</Filename>
  <Disabled>False</Disabled>
  <Launch.ManualLoad>False</Launch.ManualLoad>
  <!-- FSUIPC is required by the ProSim737 interface -->
  <Launch.Addon>
    <Name>FSUIPC 6</Name>
    <Disabled>False</Disabled>
    <Path>C:\\FSUIPC6\\FSUIPC6.dll</Path>
  </Launch.Addon>
  <Launch.Addon>
    <Name>ProSim737 Audio Bridge</Name>
    <Disabled>False</Disabled>
    <Path>C:\\ProSim737\\Audio\\PSAudio.dll</Path>
  </Launch.Addon>
</SimBase.Document>
"""
FILES = [  # label, group, windows path, content
    ("Prepar3D.cfg", "Sim 1 – P3D v5", APP + r"\Prepar3D.cfg", PREPAR3D_CFG),
    ("dll.xml", "Sim 1 – P3D v5", APP + r"\dll.xml", DLL_XML),
    ("exe.xml", "Sim 1 – P3D v5", APP + r"\exe.xml",
     '<?xml version="1.0" encoding="Windows-1252"?>\n<SimBase.Document Type="Launch" version="1,0">\n'
     '  <Launch.Addon>\n    <Name>ProSim737 Server</Name>\n    <Path>C:\\ProSim737\\ProSim737.exe</Path>\n'
     '  </Launch.Addon>\n</SimBase.Document>\n'),
    ("Cameras.cfg", "Sim 1 – P3D v5", APP + r"\Cameras.cfg", "[CameraDefinition.001]\nTitle=Cockpit\nGuid={B1386D92}\n"),
    ("add-ons.cfg", "Sim 1 – P3D v5", PD + r"\add-ons.cfg",
     "[Package.0]\nPATH=C:\\Users\\Sim1\\Documents\\Prepar3D v5 Add-ons\\ORBX\nTITLE=ORBX Global\nACTIVE=true\n"),
    ("scenery.cfg", "Sim 1 – P3D v5", PD + r"\scenery.cfg", "[General]\nTitle=Prepar3D Scenery\nClean_on_Exit=TRUE\n"),
    ("Standard.xml (controls)", "Sim 1 – P3D v5", APP + r"\Controls\Standard.xml",
     '<?xml version="1.0" encoding="UTF-8"?>\n<SimBase.Document Type="Input" version="1,0">\n</SimBase.Document>\n'),
    ("ProSim config.xml", "Sim 1 – ProSim", PS + r"\ProSim737\config.xml",
     '<?xml version="1.0" encoding="utf-8"?>\n<Config>\n  <Server>192.168.10.20</Server>\n'
     '  <Model>B737-800</Model>\n  <Units>metric</Units>\n</Config>\n'),
    ("ProSim MCP config.xml", "Sim 1 – ProSim", PS + r"\MCP\config.xml",
     '<?xml version="1.0" encoding="utf-8"?>\n<Config>\n  <Server>192.168.10.20</Server>\n</Config>\n'),
    ("ProSim CDU config.xml", "Sim 1 – ProSim", PS + r"\CDU\config.xml",
     '<?xml version="1.0" encoding="utf-8"?>\n<Config>\n  <Server>192.168.10.20</Server>\n</Config>\n'),
]

sim = tmp / "sim"
real_to_win = {}
repo = Repo(tmp / "data")
repo.set("machine_name", "Sim 1")
for i, (label, group, win, text) in enumerate(FILES):
    p = sim / f"f{i}" / win.split("\\")[-1]
    p.parent.mkdir(parents=True)
    p.write_bytes(text.replace("\n", "\r\n").encode("cp1252"))
    real_to_win[str(p)] = win
    real_to_win[str(p.parent)] = win.rsplit("\\", 1)[0]
    repo.add_file(str(p), label, group)
fid = {f.label: f for f in repo.files()}
path = {f.label: Path(f.path) for f in repo.files()}

# demo system information (real values come from PowerShell on Windows)
DEMO_PROBES = {
    "gpu": "NVIDIA GeForce RTX 4080\n  Driver version : 32.0.15.6603\n  Driver date    : 2024-10-04\n"
           "  Video processor: NVIDIA GeForce RTX 4080\n\nnvidia-smi:\nNVIDIA GeForce RTX 4080, 566.03, 95.03.1C.00.95, 4\n",
    "windows": "Microsoft Windows 11 Pro\nVersion 23H2  build 22631.4317\nEdition Professional\nArchitecture 64-bit\n",
    "displays": "\\\\.\\DISPLAY1  3840x2160 at 0,0  (primary)\n\\\\.\\DISPLAY2  1920x1080 at 3840,0\n"
                "\\\\.\\DISPLAY3  1920x1080 at 5760,0\nMonitor: SAM Odyssey G7\nMonitor: EPS Projector\n",
}
repo.background_probes = True
repo._probe_cache = {}
for key, text in DEMO_PROBES.items():
    repo._probe_cache[core.PROBE_PREFIX + key] = (time.time() + 10 ** 6, text.encode())
    repo.add_file(core.PROBE_PREFIX + key, core.BUILTIN_PROBES[key][0], "Sim 1 – System")
repo.ensure_baseline()

QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.Yes)
QMessageBox.information = staticmethod(lambda *a, **k: None)


def bump(p):
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def edit(label, old, new):
    p = path[label]
    p.write_bytes(p.read_bytes().replace(old.encode(), new.encode()))
    bump(p)


# rules, restore points, versions
repo.save_rule(Rule(0, "Texture resolution", fid["Prepar3D.cfg"].id, "ini_value", "GRAPHICS/TEXTURE_MAX_LOAD", "4096"))
repo.save_rule(Rule(0, "FSUIPC must stay loaded", fid["dll.xml"].id, "line_removed", "FSUIPC"))
repo.save_rule(Rule(0, "ProSim server address", fid["ProSim config.xml"].id, "xml_value", ".//Server", "192.168.10.20",
                    action="revert"))
repo.ensure_baseline()
repo.create_restore_point("Customer default", "Normal sessions")
repo.create_version("Initial certified setup", "Baseline after installation and acceptance test.", "Sim Tech")
edit("Prepar3D.cfg", "LOD_RADIUS=6.500000", "LOD_RADIUS=4.500000")
repo.process_changes(repo.scan())
repo.create_restore_point("Checkride", "Clear weather, no traffic", color="#2e7d32")
repo.create_version("Lower LOD radius for smoother frame rates",
                    "- Prepar3D.cfg [Sim 1 – P3D v5] changed (+1/-1 lines)", "Sim Tech")
repo.create_restore_point("Maintenance", "Windowed mode for service work", color="#6d4c41")
edit("ProSim config.xml", "<Units>metric</Units>", "<Units>imperial</Units>")
repo.process_changes(repo.scan())
repo.create_version("ProSim switched to imperial units", "- ProSim config.xml [Sim 1 – ProSim] changed (+1/-1 lines)",
                    "Sim Tech")
# a day of new changes → mismatches against v3
edit("Prepar3D.cfg", "TEXTURE_MAX_LOAD=4096", "TEXTURE_MAX_LOAD=2048")
repo.process_changes(repo.scan())
edit("dll.xml", "<Name>ProSim737 Audio Bridge</Name>\r\n    <Disabled>False", "<Name>ProSim737 Audio Bridge</Name>\r\n    <Disabled>True")
repo.process_changes(repo.scan())
edit("ProSim config.xml", "192.168.10.20", "192.168.10.99")  # auto-undone by the rule
repo.process_changes(repo.scan())
repo.commit("Edited Cameras.cfg: added tower view", kind="manual")


def winify(widget):
    """Replace real temp paths with the Windows paths they represent, in every table cell and label."""
    def fix(t):
        for real in sorted(real_to_win, key=len, reverse=True):
            t = t.replace(real, real_to_win[real])
        return t.replace(str(tmp / "data"), r"C:\ProgramData\SimConfigManager")
    for t in widget.findChildren(QTableWidget):
        for r in range(t.rowCount()):
            for c in range(t.columnCount()):
                it = t.item(r, c)
                if it and it.text():
                    it.setText(fix(it.text()))
    for lab in widget.findChildren(QLabel):
        if lab.text():
            lab.setText(fix(lab.text()))


def shot(widget, name):
    app.processEvents()
    winify(widget)
    app.processEvents()
    widget.grab().save(str(OUT / name))
    print("saved", name)


repo.add_ignore(fid["Prepar3D.cfg"].id, "ini_section", "window.*", "P3D rewrites window positions on exit")
repo.save_rulebook_entry({"file_id": fid["Prepar3D.cfg"].id, "selector": "GRAPHICS/TEXTURE_MAX_LOAD",
                          "required": "4096", "note": "Certified value — lower causes blurry ground textures",
                          "author": "Sim Tech"})
repo.save_rulebook_entry({"file_id": fid["ProSim config.xml"].id, "selector": ".//Server", "required": "192.168.10.20",
                          "note": "ProSim server PC (fixed IP)", "author": "Sim Tech"})
repo.save_rulebook_entry({"file_id": fid["dll.xml"].id, "selector": ".//Launch.Addon[1]/Disabled", "required": "False",
                          "note": "FSUIPC must load — ProSim needs it", "author": "Sim Tech"})
repo.save_rulebook_entry({"file_id": fid["Prepar3D.cfg"].id, "selector": "TERRAIN/LOD_RADIUS", "required": "",
                          "note": "4.5 = smooth on the projectors, 6.5 = sharper but stutters on approach",
                          "author": "Sim Tech"})

w = gui.MainWindow(repo, start_hidden=True)
w.resize(1280, 760)
w.show()
w.update_version_indicator()

# 1. versions overview
w.go("versions")
w.refresh_all()
w.ver_list.selectRow(0)
shot(w, "versions.png")

# 2. quick restore
w.go("restore")
w.refresh_all()
shot(w, "quick-restore.png")

# 3. files + history
w.go("files")
w.refresh_all()
for r in range(w.files_table.rowCount()):
    if w.files_table.item(r, 1).text() == "Prepar3D.cfg":
        w.files_table.selectRow(r)
shot(w, "files.png")

# 4. alerts
w.go("alerts")
w.refresh_all()
shot(w, "alerts.png")

# 5. editor (cfg) with find bar, and XML with a syntax error
e = w.open_editor(fid["Prepar3D.cfg"])
e.resize(1100, 560)
e.editor.goto_line(8)
e.findbar.open_bar()
e.findbar.find.setText("TEXTURE")
shot(e, "editor-cfg.png")
e.findbar.close_bar()
e.close()
x = w.open_editor(fid["dll.xml"])
x.resize(1100, 560)
x.editor.setPlainText(x.editor.toPlainText().replace("</Name>\n    <Disabled>True", "</Nme>\n    <Disabled>True"))
x.validate()
x.editor.goto_line(14)
shot(x, "editor-xml-check.png")
x.editor.document().setModified(False)
x.close()

# 6. create version dialog
d = gui.CreateVersionDialog(repo, w)
d.title.setText("New texture limit, audio bridge disabled")
d.resize(760, 560)
shot(d, "create-version.png")

# 7. diff of a version mismatch
diff = core.unified_diff(repo.get_object(repo.entries(repo.latest_version().snapshot_id)[fid["Prepar3D.cfg"].id]),
                         path["Prepar3D.cfg"].read_bytes(), "v3", "on disk now")
dd = gui.DiffDialog("Prepar3D.cfg: v3 → now", diff, w)
dd.resize(900, 420)
shot(dd, "diff.png")

# 8. Claude tab + a proposal waiting for approval
cfgf = fid["Prepar3D.cfg"]
cur = path["Prepar3D.cfg"].read_bytes()
core.create_proposal(repo, cfgf, cur.replace(b"TEXTURE_MAX_LOAD=2048", b"TEXTURE_MAX_LOAD=4096")
                     .replace(b"LOD_RADIUS=4.500000", b"LOD_RADIUS=5.500000"),
                     "TEXTURE_MAX_LOAD was lowered to 2048 today, which explains the blurry ground textures the "
                     "instructor reported. Sim 2 runs 4096 on the same RTX 4080 without stutters, so restoring 4096 "
                     "matches the certified v3 value. LOD_RADIUS 5.5 is a middle ground between v1 (6.5) and v2 (4.5).")
w.go("assistants")
w.check_proposals()
w.refresh_all()
shot(w, "claude.png")
pd = gui.ProposalDialog(repo, core.list_proposals(repo, "pending")[0], w)
pd.resize(980, 600)
shot(pd, "claude-proposal.png")

# 9. network setup for a mechanic's MacBook (example key, example addresses)
demo_key = {"name": "Jan – MacBook Pro", "key": "scm_EXAMPLE-not-a-real-key-0000", "created": 0, "may_propose": True}
nd = gui.NetSetupDialog("Sim 1", demo_key, ["http://192.168.10.11:8765/mcp", "http://sim1.local:8765/mcp"], "mac", w)
nd.resize(820, 600)
shot(nd, "claude-network.png")
nd = gui.NetSetupDialog("Sim 1", demo_key, ["http://192.168.10.11:8765/mcp", "http://sim1.local:8765/mcp"],
                        "chatgpt", w)
nd.resize(860, 680)
shot(nd, "chatgpt-setup.png")

# 10. setting-level diff with "ignore" button
old = repo.get_object(repo.entries(repo.latest_version().snapshot_id)[fid["Prepar3D.cfg"].id])
new = path["Prepar3D.cfg"].read_bytes()
sd = gui.DiffDialog("Prepar3D.cfg: v3 → now", core.unified_diff(old, new, "v3", "on disk now"), w,
                    summary=core.setting_changes(old, new, str(path["Prepar3D.cfg"])), ignore_cb=lambda c: None)
sd.resize(900, 560)
shot(sd, "diff-settings.png")

# 11. rulebook
w.go("rulebook")
w.refresh_all()
shot(w, "rulebook.png")

# 12. operations: sessions, hardware & health, installs & integrity, templates (Windows parts faked)
from simconfigmanager.core import winsys  # noqa: E402
import random  # noqa: E402
random.seed(7)


def demo_session(days_ago, minutes, version, fps, aircraft, kind="session", label="", crash=None, matches=True,
                 samples=False):
    start = time.time() - days_ago * 86400
    stats = {"minutes": minutes, "avg_fps": fps, "median_fps": fps + 1, "low1_fps": round(fps * 0.62, 1),
             "min_fps": round(fps * 0.4, 1), "stutters": int(max(0, 45 - fps) / 3), "max_gpu_temp": 70 + fps % 9,
             "avg_gpu_temp": 66, "avg_gpu_util": 88, "aircraft": [aircraft]}
    cur = repo.db.execute("INSERT INTO sessions(kind, label, started, ended, machine, version_number, version_matches, "
                          "differing, aircraft, stats, crash, snapshot_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                          (kind, label, start, start + minutes * 60, repo.machine, version, 1 if matches else 0,
                           "[]" if matches else '["Prepar3D.cfg"]', aircraft, json.dumps(stats),
                           json.dumps(crash) if crash else "", repo.latest_snapshot().id))
    if samples:
        for i in range(int(minutes * 6)):
            v = fps + random.uniform(-6, 6) - (14 if 40 < i < 44 else 0)
            repo.db.execute("INSERT INTO session_samples(session_id, ts, fps, gpu_temp) VALUES(?,?,?,?)",
                            (cur.lastrowid, start + i * 10, max(5, v), 68 + random.uniform(-2, 3)))
    repo.db.commit()


for i, (v, fps) in enumerate([(1, 34.0), (1, 33.1), (2, 41.5), (2, 42.8), (2, 40.2), (3, 41.9), (3, 40.7)]):
    demo_session(12 - i * 1.4, 95 + i * 7, v, fps, "PMDG 737-800 KLM" if i % 2 else "PMDG 737-800 Transavia",
                 crash={"crashes": [{"ts": time.time(), "app": "Prepar3D.exe", "module": "terrain.dll",
                                     "code": "0xc0000005"}], "display_resets": []} if i == 1 else None)
demo_session(0.9, 5, 3, 36.4, "PMDG 737-800 KLM", "benchmark", "A/B A: Checkride")
demo_session(0.85, 5, 3, 42.1, "PMDG 737-800 KLM", "benchmark", "A/B B: LOD 4.5")
demo_session(0.2, 104, 3, 38.6, "PMDG 737-800 KLM", matches=False, samples=True,
             crash={"crashes": [], "display_resets": [{"ts": time.time() - 3600, "message": "Display driver "
                                                       "nvlddmkm stopped responding and has successfully recovered."}]})
w.live = {"running": True, "session": None, "benchmark": None,
          "live": {"fps": 41.0, "aircraft": "PMDG 737-800 KLM"}, "gpu": {"temp": 69.0}}
repo.db.execute("INSERT INTO sessions(kind, started, machine, version_number, version_matches, differing, aircraft) "
                "VALUES('session', ?, ?, 3, 0, '[\"Prepar3D.cfg\", \"dll.xml\"]', 'PMDG 737-800 KLM')",
                (time.time() - 2400, repo.machine))
repo.db.commit()
w.live["session"] = core.sessions.open_session(repo) if hasattr(core.sessions, "open_session") else None
w.go("sessions")
w.refresh_all()
w.sess_table.selectRow(1)
shot(w, "sessions.png")
sd = None

winsys.FAKES["devices"] = [
    {"name": "PFC Cirrus II Yoke (Captain)", "cls": "HIDClass", "id": r"HID\VID_0EB0&PID_0100&MI_00\7&1A2B3C&0&0000", "status": "OK"},
    {"name": "PFC Yoke (First Officer)", "cls": "HIDClass", "id": r"HID\VID_0EB0&PID_0101&MI_00\7&2B3C4D&0&0000", "status": "OK"},
    {"name": "CPflight MCP737 (COM3)", "cls": "Ports", "id": r"USB\VID_0403&PID_6001\A10K3Z9", "status": "OK"},
    {"name": "Leo Bodnar BU0836X", "cls": "HIDClass", "id": r"HID\VID_1DD2&PID_1001\8&3C4D5E&0&0000", "status": "OK"},
    {"name": "USB Composite Device", "cls": "USB", "id": r"USB\VID_046D&PID_C52B\5&2F1&0&3", "status": "OK"},
    {"name": "Logitech Throttle Quadrant", "cls": "HIDClass", "id": r"HID\VID_06A3&PID_0C2D\6&9F&0&0000", "status": "OK"}]
for label, pat, note in (("Captain yoke", r"HID\VID_0EB0&PID_0100*", "PFC, left seat"),
                         ("First officer yoke", r"HID\VID_0EB0&PID_0101*", "PFC, right seat"),
                         ("MCP panel", r"USB\VID_0403&PID_6001*", "CPflight, COM3"),
                         ("Overhead interface", r"HID\VID_1DD2&PID_1001*", "Leo Bodnar card behind the overhead"),
                         ("Rudder pedals", r"HID\VID_06A3&PID_0763*", "Saitek combat pedals")):
    repo.add_required_device(label, pat, note)
repo.hardware_check()
winsys.FAKES.update(disks=[{"name": "Samsung SSD 990 PRO 2TB", "media": "SSD", "health": "Healthy", "temp": 41, "wear": 3,
                            "hours": 4210, "read_errors": 0},
                           {"name": "Samsung SSD 870 EVO 4TB (scenery)", "media": "SSD", "health": "Healthy", "temp": 38,
                            "wear": 9, "hours": 11800, "read_errors": 0}],
                    gpu={"name": "NVIDIA GeForce RTX 4080", "temp": 66.0, "util": 91.0, "mem_used": 11200},
                    pjlink=lambda host: {"POWR": "1", "LAMP": {"10.0.0.41": "2950 1", "10.0.0.42": "1210 1",
                                                                 "10.0.0.43": "1180 1"}[host],
                                         "ERST": "000000" if host != "10.0.0.41" else "000010", "NAME": "x"})
for n, h in (("Left", "10.0.0.41"), ("Centre", "10.0.0.42"), ("Right", "10.0.0.43")):
    repo.save_projector({"name": n, "host": h, "lamp_life": 3000})
repo.health_check()
for name, days, hours, ago_d, note in (("Clean projector filters", 90, None, 97, "All three projectors"),
                                        ("Replace projector lamps", None, 3000, 30, "Order lamps 2 weeks ahead"),
                                        ("Recalibrate yokes", None, 200, 10, "PFC calibration utility"),
                                        ("Check seat belts and harness", 180, None, 20, ""),
                                        ("Clean cockpit and screens", 7, None, 5, "")):
    repo.save_maintenance_task({"name": name, "every_days": days, "every_hours": hours, "note": note})
    repo.db.execute("UPDATE maint_tasks SET created=?, last_done=? WHERE name=?",
                    (time.time() - ago_d * 86400, time.time() - ago_d * 86400, name))
repo.db.commit()
w.go("hardware")
w.refresh_all()
shot(w, "hardware.png")

inst = tmp / "P3D-install"
(inst / "Scenery").mkdir(parents=True)
for n in ("dll.xml", "exe.xml", "scenery.cfg", "Prepar3D.exe", "terrain.dll"):
    (inst / n).write_text(n * 20)
rec = repo.start_install("Orbx EHAM Amsterdam", [str(inst)])
(inst / "dll.xml").write_text("dll.xml + Orbx")
for k in range(14):
    (inst / "Scenery" / f"EHAM_{k}.bgl").write_bytes(b"x" * (2000 + k))
repo.finish_install(rec["id"], create_version=False)
rec = repo.start_install("ProSim 3.30 update", [str(inst)])
(inst / "exe.xml").write_text("exe.xml v3.30")
repo.finish_install(rec["id"], create_version=False)
m1 = repo.create_manifest("Prepar3D v5 installation", [str(inst)])
repo.create_manifest("ProSim737", [str(inst / "Scenery")])
(inst / "terrain.dll").write_text("patched")
repo.verify_manifest(m1["id"])
w.go("installs")
w.refresh_all()
shot(w, "installs.png")

shared = tmp / "shared-templates"
shared.mkdir()
repo.set("sync_mode", "folder")
repo.set("sync_folder", str(shared))
t = repo.save_template({"name": "Prepar3D.cfg — display and LOD", "notes": "LOD 4.5 for all sims",
                        "content": "[DISPLAY]\nWidth={{width}}\nHeight={{height}}\n[TERRAIN]\nLOD_RADIUS=4.500000\n",
                        "stages": [["Sim 1"], ["Sim 2", "Sim 3"]]})
repo.set_template_binding(t["id"], fid["Prepar3D.cfg"].id, {"width": "5760", "height": "1080"})
(shared / "templates" / f"{t['id']}@Sim_2.json").write_text(json.dumps({"machine": "Sim 2", "rev": 0}))
t2 = repo.save_template({"name": "ProSim config.xml — IOS and server", "content": "<Config><Server>{{server}}</Server></Config>",
                         "stages": [["Sim 2"], ["Sim 1", "Sim 3"]]})
repo.advance_rollout(t2["id"])
(shared / "templates" / f"{t2['id']}@Sim_2.json").write_text(json.dumps({"machine": "Sim 2", "rev": 1}))
w.go("templates")
w.refresh_all()
end = time.time() + 5
while time.time() < end and w.tpl_table.rowCount() < 2:
    app.processEvents()
    time.sleep(0.05)
w.tpl_table.selectRow(0)
shot(w, "templates.png")
repo.set("sync_mode", "")

# 13. fleet overview: Sim 2 and Sim 3 share their status through a stand-in Google Drive
sys.path.insert(0, str(ROOT / "tests"))
from fake_drive import FakeDrive  # noqa: E402
from simconfigmanager import storage  # noqa: E402
drive = FakeDrive().start()
os.environ["SCM_DRIVE_API"] = drive.url
storage.service_account_token_provider = lambda f: (lambda: "test-token")
core.drive_files(repo)["service_account"].write_text(json.dumps({"type": "service_account",
                                                                 "client_email": "sim-config@simcenter.iam.gserviceaccount.com"}))
repo.set("sync_mode", "gdrive")
repo.set("gdrive_auth", "service_account")
repo.set("gdrive_folder_id", "shared123")
be = storage.DriveBackend(lambda: "test-token", "shared123", api_base=drive.url)
now = time.time()
for name, extra in (("Sim 2", {"mismatches": [], "alerts_24h": 0, "rulebook_violations": []}),
                    ("Sim 3", {"mismatches": [{"file": "ProSim config.xml", "group": "Sim 3 – ProSim", "status": "modified"}],
                               "alerts_24h": 2, "rulebook_violations": ["ProSim config.xml: .//Server"],
                               "pending_proposals": 1, "heartbeat": now - 120, "hardware_missing": ["MCP panel"],
                               "maintenance_overdue": ["Clean projector filters"],
                               "recent_alerts": [{"ts": now - 3600, "message": "[Rulebook] ProSim config.xml: .//Server = "
                                                  "'192.168.10.99', rulebook requires '192.168.10.20'"}]})):
    st = {"machine": name, "app_version": core.APP_VERSION, "heartbeat": now - 60, "files": 13,
          "version": {"number": 3, "title": "ProSim switched to imperial units", "created_at": now - 86400},
          "pending_proposals": 0, "recent_alerts": [], "last_change": now - 7200,
          "last_session": {"started": now - 5 * 3600, "avg_fps": 41.2 if name == "Sim 2" else 36.0,
                           "crashed": name == "Sim 3"},
          "system": {"gpu": DEMO_PROBES["gpu"].replace("566.03", "566.03" if name == "Sim 2" else "560.94")
                     .replace("32.0.15.6603", "32.0.15.6603" if name == "Sim 2" else "32.0.15.6094")}}
    st.update(extra)
    be.write(f"status/{name.replace(' ', '_')}.json", json.dumps(st).encode())
w.go("fleet")
w.refresh_fleet()
end = time.time() + 10
while time.time() < end and w.fleet_table.rowCount() < 3:
    app.processEvents()
    time.sleep(0.05)
w.fleet_table.selectRow(2)
shot(w, "fleet.png")

# 14. sharing page set to Google Drive
w.go("sharing")
w.sync_mode.setCurrentIndex(w.sync_mode.findData("gdrive"))
w.drive_folder.setText("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOpQrStUvWxYz")
w.update_sync_widgets()
repo.set("hub_enabled", "1")
repo.set("hub_url", "https://hub.simcenter.example")
repo.set("hub_key", "hub_EXAMPLE")
repo.set("hub_last_ok", time.time() - 40)
w.hub_enable.setChecked(True)
w.hub_url.setText(repo.get("hub_url"))
w.hub_key.setText("hub_EXAMPLE")
w.update_hub_status()
w.sync_status.setText("<span style='color:#2e7d32'>✔ Connected to Google Drive folder <b>Sim configs</b>.</span> "
                      "Last upload: 2026-11-02 09:12:40")
w.machine_combo.clear()
for m in ("Sim 1 (this PC) — 13 files", "Sim 2 — 13 files", "Sim 3 — 13 files"):
    w.machine_combo.addItem(m)
shot(w, "cloud-drive.png")
drive.stop()

# 15. remote app: connect window with saved sims, and the setup for a person with full control
os.environ["XDG_CONFIG_HOME"] = str(tmp / "client-config")
from simconfigmanager import remote  # noqa: E402
remote.save_connections([{"name": "Sim 1", "url": "192.168.10.11", "key": "x"},
                         {"name": "Sim 2", "url": "192.168.10.12", "key": "x"},
                         {"name": "Sim 3", "url": "sim3.local", "key": "x"}])
cd = gui_connect.ConnectDialog(w)
cd.resize(620, 360)
shot(cd, "remote-connect.png")
full_key = {"name": "Jan – MacBook Pro", "key": "scm_EXAMPLE-not-a-real-key-0000", "created": 0, "may_propose": True,
            "full_control": True}
nd = gui.NetSetupDialog("Sim 1", full_key, ["http://192.168.10.11:8765/mcp"], "app", w)
nd.resize(820, 560)
shot(nd, "remote-setup.png")

# 16. first-run setup: the config files found on the sim
from simconfigmanager.ui.onboarding import OnboardingWizard  # noqa: E402
APPD = r"C:\Users\Sim1\AppData\Roaming\Lockheed Martin\Prepar3D v5"
PROGD = r"C:\ProgramData\Lockheed Martin\Prepar3D v5"
demo_found = [(f"{n} (AppData)", "P3D v5", APPD + "\\" + n) for n in
              ("Prepar3D.cfg", "dll.xml", "exe.xml", "Cameras.cfg", "terrain.cfg")] + \
             [("Controls/Standard.xml (AppData)", "P3D v5", APPD + r"\Controls\Standard.xml"),
              ("add-ons.cfg (ProgramData)", "P3D v5", PROGD + r"\add-ons.cfg"),
              ("scenery.cfg (ProgramData)", "P3D v5", PROGD + r"\scenery.cfg"),
              ("FSUIPC6.ini", "FSUIPC", r"C:\FSUIPC6\FSUIPC6.ini"),
              ("ProSim config.xml", "ProSim", r"C:\ProSim737\ProSim737\config.xml"),
              ("ProSim MCP/config.xml", "ProSim", r"C:\ProSim737\MCP\config.xml"),
              ("ProSim ProSimAudio.ini", "ProSim", r"C:\ProSim737\ProSimAudio.ini")]
fresh = Repo(tmp / "fresh")
fresh.set("machine_name", "Sim 2")
fresh.discover = lambda: demo_found
wf = gui.MainWindow(fresh, start_hidden=True)
wz = OnboardingWizard(wf)
wz.resize(820, 620)
wz.show()
wz.next()
app.processEvents()
wz._scan()
shot(wz, "onboarding.png")
wz.reject()
print("done →", OUT)
