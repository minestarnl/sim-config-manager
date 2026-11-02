"""Regenerate the README screenshots with realistic demo data.

    QT_QPA_PLATFORM=offscreen python tools/make_screenshots.py

Builds a throw-away store with P3D / ProSim-style files, simulates a day of changes, and saves PNGs to
docs/screenshots/. Real temp paths are shown as the Windows paths they stand for.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "docs" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication, QDialog, QLabel, QMessageBox, QTableWidget  # noqa: E402

app = QApplication(sys.argv)
from simconfigmanager import core, editor, gui  # noqa: E402
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


w = gui.MainWindow(repo, start_hidden=True)
w.resize(1280, 760)
w.show()
w.update_version_indicator()

# 1. versions overview
w.tabs.setCurrentWidget(w.tab_versions)
w.refresh_all()
w.ver_list.selectRow(0)
shot(w, "versions.png")

# 2. quick restore
w.tabs.setCurrentWidget(w.tab_quick)
w.refresh_all()
shot(w, "quick-restore.png")

# 3. files + history
w.tabs.setCurrentWidget(w.tab_files)
w.refresh_all()
for r in range(w.files_table.rowCount()):
    if w.files_table.item(r, 1).text() == "Prepar3D.cfg":
        w.files_table.selectRow(r)
shot(w, "files.png")

# 4. alerts
w.tabs.setCurrentWidget(w.tab_alerts)
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
w.tabs.setCurrentWidget(w.tab_claude)
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
print("done →", OUT)
