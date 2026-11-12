"""1.12 features: sessions + performance + benchmarks, live-data bridge, who changed it, cockpit hardware,
install recordings + rollback, integrity manifests, shared templates with staged rollout, health (PJLink) and
maintenance, configuration manual, Hub report sections and commands. Windows-only calls are faked via
core.winsys.FAKES, so this runs everywhere."""
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simconfigmanager import core, mcp_server, remote  # noqa: E402
from simconfigmanager.core import Repo, winsys  # noqa: E402


def make_repo(tmp: Path, name: str = "Sim 1") -> tuple:
    (tmp / "sim").mkdir(parents=True, exist_ok=True)
    cfg = tmp / "sim" / "Prepar3D.cfg"
    cfg.write_text("[GRAPHICS]\nTEXTURE_MAX_LOAD=4096\n[DISPLAY]\nWidth=1920\n")
    repo = Repo(tmp / "data")
    repo.set("machine_name", name)
    f = repo.add_file(str(cfg), "Prepar3D.cfg", "P3D")
    repo.ensure_baseline()
    return repo, cfg, f


def test_sessions_benchmarks_and_performance():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, f = make_repo(tmp)
    repo.create_version("Baseline", "", "Tech")
    state = {"running": True, "fps": 40.0}
    winsys.FAKES.update(
        processes=lambda: ["Prepar3D.exe"] if state["running"] else [],
        simlink=lambda: {"ts": time.time(), "fps": state["fps"], "aircraft": "PMDG 737-800", "lat": 52.3,
                         "lon": 4.76, "alt_ft": 0, "paused": 0},
        gpu={"name": "RTX 4080", "temp": 71.0, "util": 80.0, "mem_used": 9000},
        app_crashes=lambda since, proc: [{"ts": time.time(), "app": "Prepar3D.exe", "module": "terrain.dll",
                                          "code": "0xc0000005"}],
        display_resets=[])
    try:
        st = repo.session_tick()
        assert st["session"] and st["session"]["version_number"] == 1 and st["session"]["version_matches"]
        for fps in (40, 42, 38, 12, 41):
            state["fps"] = fps
            repo.session_tick()
        state["running"] = False
        repo.session_tick()
        s = repo.sessions()[0]
        assert s["ended"] and s["stats"]["avg_fps"] > 30 and s["stats"]["stutters"] == 1
        assert s["aircraft"] == "PMDG 737-800" and s["crash"]["crashes"][0]["module"] == "terrain.dll"
        assert any("crashed" in e["message"] and e["level"] == "alert" for e in repo.events(10))
        perf = repo.performance_by_version()
        assert perf[0]["version"] == 1 and perf[0]["crashes"] == 1 and perf[0]["sessions"] == 1

        # benchmark: needs live data, records for its planned time, ends by itself
        state["running"] = True
        b = repo.start_benchmark("A", minutes=0.0001)
        repo.session_tick()
        time.sleep(0.05)
        repo.session_tick()
        assert repo.session(b["id"])["ended"]
        b2 = repo.start_benchmark("B", minutes=10)
        state["fps"] = 60
        repo.session_tick()
        repo.stop_benchmark()
        cmp_ = repo.compare_benchmarks([b["id"], b2["id"]])
        assert [c["label"] for c in cmp_] == ["A", "B"]
        assert "benchmark 'B'" in mcp_server.t_performance(repo, {})
        assert repo.sim_hours() >= 0
    finally:
        winsys.FAKES.clear()


def test_live_data_bridge_install():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, f = make_repo(tmp)
    repo.set("simlink_file", str(tmp / "simlink.json"))
    fs = tmp / "FSUIPC6"
    fs.mkdir()
    (fs / "FSUIPC6.ini").write_bytes(b"[General]\r\nUpdatedByVersion=7000\r\n\r\n[Auto]\r\n1=Lua Display Vals\r\n\r\n"
                                     b"[Buttons]\r\n")
    ini = repo.install_bridge(str(fs))
    text = Path(ini).read_bytes().decode()
    assert "[Auto]\r\n1=Lua Display Vals\r\n2=Lua SimConfigManager\r\n" in text
    assert repo.bridge_installed(str(fs)) and "event.timer" in (fs / "SimConfigManager.lua").read_text()
    assert str(tmp / "simlink.json") in (fs / "SimConfigManager.lua").read_text()
    repo.install_bridge(str(fs))          # idempotent
    assert Path(ini).read_text().count("Lua SimConfigManager") == 1
    tracked = repo.find_file_by_path(ini)
    assert tracked and len(repo.file_history(tracked.id)) >= 2         # the change is in History
    fs2 = tmp / "F2"
    fs2.mkdir()
    (fs2 / "FSUIPC5.ini").write_bytes(b"[General]\nX=1\n")
    assert "[Auto]\n1=Lua SimConfigManager\n" in Path(repo.install_bridge(str(fs2))).read_bytes().decode()
    (tmp / "simlink.json").write_text(json.dumps({"ts": time.time(), "fps": 33.0, "aircraft": "x"}))
    assert repo.live_data()["fps"] == 33.0
    (tmp / "simlink.json").write_text(json.dumps({"ts": time.time() - 60, "fps": 33.0}))
    assert repo.live_data() is None


def test_who_changed_it():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, f = make_repo(tmp)
    winsys.FAKES["enable_audit"] = lambda paths: [{"ok": len(paths), "failed": []}]
    winsys.FAKES["writers"] = lambda path, since, until: [
        {"ts": time.time(), "process": r"C:\Program Files\Lockheed Martin\Prepar3D v5\Prepar3D.exe",
         "user": "SIM1\\pilot", "mask": "0x2"},
        {"ts": time.time(), "process": r"C:\Windows\explorer.exe", "user": "SIM1\\pilot", "mask": "0x80"}]
    try:
        assert repo.enable_file_auditing()["files"] == 1
        cfg.write_text("[GRAPHICS]\nTEXTURE_MAX_LOAD=2048\n")
        ch = repo.scan()
        repo.process_changes(ch)
        who = repo.attribute_change(f.id, ch[0].new_hash, time.time() - 60)
        assert who["process"] == "Prepar3D.exe" and who["user"] == "SIM1\\pilot"
        assert repo.change_actors(f.id)[ch[0].new_hash] == "Prepar3D.exe (SIM1\\pilot)"
        assert any("changed by Prepar3D.exe" in e["message"] for e in repo.events(5))
    finally:
        winsys.FAKES.clear()


def test_cockpit_hardware_watchdog():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, f = make_repo(tmp)
    yoke = {"name": "PFC Yoke", "cls": "HIDClass", "id": r"HID\VID_0EB0&PID_0100\7&1A2B", "status": "OK"}
    mcp = {"name": "MCP Panel", "cls": "Ports", "id": r"USB\VID_16C0&PID_05BA\1234", "status": "OK"}
    devices = [yoke, mcp]
    winsys.FAKES["devices"] = lambda: devices
    try:
        assert core.device_key(mcp["id"]) == r"USB\VID_16C0&PID_05BA*"
        repo.add_required_device("Captain yoke", r"HID\VID_0EB0&PID_0100*")
        repo.add_required_device("MCP", core.device_key(mcp["id"]))
        assert repo.hardware_check()["missing"] == []
        devices.remove(mcp)
        st = repo.hardware_check()
        assert st["missing"] == ["MCP"]
        assert any("MCP is not connected" in e["message"] and e["level"] == "alert" for e in repo.events(5))
        assert repo.hardware_status()["missing"] == ["MCP"]
        assert core.build_status(repo)["hardware_missing"] == ["MCP"]
        devices.append(mcp)
        repo.hardware_check()
        assert any("MCP is connected again" in e["message"] for e in repo.events(5))
    finally:
        winsys.FAKES.clear()


def test_install_recording_and_rollback_and_integrity():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, f = make_repo(tmp)
    p3d = tmp / "P3D"
    (p3d / "SimObjects").mkdir(parents=True)
    (p3d / "dll.xml").write_text("<SimBase.Document><Launch.Addon><Name>FSUIPC</Name></Launch.Addon></SimBase.Document>")
    (p3d / "scenery.cfg").write_text("[Area.001]\nTitle=Default\n")
    (p3d / "big.bgl").write_bytes(os.urandom(3 * 1024 * 1024))       # too big to copy beforehand
    roots = [str(p3d)]

    man = repo.create_manifest("P3D", roots)
    assert man["files"] == 3 and repo.verify_manifest(man["id"])["ok"]

    rec = repo.start_install("Orbx EHAM", roots)
    assert rec["status"] == "recording"
    (p3d / "dll.xml").write_text("<SimBase.Document><Launch.Addon><Name>FSUIPC</Name></Launch.Addon>"
                                 "<Launch.Addon><Name>Orbx</Name></Launch.Addon></SimBase.Document>")
    (p3d / "scenery.cfg").unlink()
    (p3d / "big.bgl").write_bytes(os.urandom(3 * 1024 * 1024))
    (p3d / "Orbx").mkdir()
    (p3d / "Orbx" / "EHAM.bgl").write_bytes(b"x" * 100)
    (p3d / "Orbx" / "EHAM.cfg").write_text("[x]\n")
    done = repo.finish_install(rec["id"], create_version=True, author="Tech")
    c = done["summary"]["counts"]
    assert (c["added"], c["changed"], c["removed"]) == (2, 2, 1)
    assert done["version_number"] == 1 and repo.latest_version().title.startswith("Installed Orbx")
    assert done["summary"]["new_config_files"] == [str(p3d / "Orbx" / "EHAM.cfg")]

    chk = repo.verify_manifest(man["id"])
    assert not chk["ok"] and chk["counts"] == {"added": 2, "removed": 1, "changed": 2}
    assert core.build_status(repo)["integrity_problems"] == ["P3D"]

    prev = repo.rollback_preview(rec["id"])
    assert sorted(os.path.basename(p) for p in prev["restore"]) == ["dll.xml", "scenery.cfg"]
    assert [os.path.basename(p) for p in prev["cannot_restore"]] == ["big.bgl"]
    rep = repo.rollback_install(rec["id"])
    assert len(rep["removed"]) == 2 and len(rep["restored"]) == 2 and len(rep["not_restorable"]) == 1
    assert not (p3d / "Orbx").exists() and (p3d / "scenery.cfg").read_text().startswith("[Area.001]")
    assert "Orbx" not in (p3d / "dll.xml").read_text()
    assert repo.install_record(rec["id"])["status"] == "rolled_back"
    assert "Installed add-ons" in repo.build_manual()


def test_templates_with_staged_rollout():
    tmp = Path(tempfile.mkdtemp())
    shared = tmp / "shared"
    shared.mkdir()
    r1, cfg1, f1 = make_repo(tmp / "a", "Sim 1")
    r2, cfg2, f2 = make_repo(tmp / "b", "Sim 2")
    for r in (r1, r2):
        r.set("sync_mode", "folder")
        r.set("sync_folder", str(shared))
    t = r1.save_template({"name": "Display", "content": "[GRAPHICS]\nTEXTURE_MAX_LOAD=4096\n[DISPLAY]\n"
                                                       "Width={{width}}\n", "stages": [["Sim 1"], ["Sim 2"]]})
    assert t["rev"] == 1 and t["variables"] == ["width"]
    r1.set_template_binding(t["id"], f1.id, {"width": "5760"})
    r2.set_template_binding(t["id"], f2.id, {"width": "3840"})
    l1 = r1.list_templates()[0]
    l2 = r2.list_templates()[0]
    assert l1["update_ready"] and l1["eligible"] and not l2["eligible"]
    assert [x["name"] for x in r2.templates_waiting()] == []
    r1.apply_template(t["id"])
    assert "Width=5760" in cfg1.read_text()
    assert r1.list_templates()[0]["status"]["Sim 1"]["rev"] == 1 and not r1.list_templates()[0]["update_ready"]
    try:
        r2.apply_template(t["id"])
        raise AssertionError("stage 2 is not open yet")
    except RuntimeError:
        pass
    r1.advance_rollout(t["id"])
    assert [x["name"] for x in r2.templates_waiting()] == ["Display"]
    r2.apply_template(t["id"])
    assert "Width=3840" in cfg2.read_text()
    # a new revision starts at stage 1 again
    t2 = dict(r1.list_templates()[0], content=t["content"] + "; new\n")
    assert r1.save_template(t2)["rev"] == 2 and r1.list_templates()[0]["open_stage"] == 0
    assert not r2.list_templates()[0]["eligible"]
    try:
        core.render_template("x={{a}}", {})
        raise AssertionError
    except ValueError as e:
        assert "a" in str(e)


class FakeProjector(threading.Thread):
    def __init__(self, password=""):
        super().__init__(daemon=True)
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.password = password

    def run(self):
        c, _ = self.sock.accept()

        def readline():
            buf = b""
            while not buf.endswith(b"\r"):
                ch = c.recv(1)
                if not ch:
                    break
                buf += ch
            return buf
        if self.password:
            c.sendall(b"PJLINK 1 498e4a67\r")
            want = hashlib.md5(("498e4a67" + self.password).encode()).hexdigest()
        else:
            c.sendall(b"PJLINK 0\r")
            want = ""
        answers = {"POWR": "1", "LAMP": "2950 1", "ERST": "000010", "NAME": "Left", "INF1": "EPSON", "INF2": "EB-L"}
        first = True
        while True:
            line = readline().decode()
            if not line:
                break
            if first and want:
                if not line.startswith(want):
                    c.sendall(b"PJLINK ERRA\r")
                    break
                line = line[len(want):]
            first = False
            cmd = line[2:6]
            c.sendall(f"%1{cmd}={answers.get(cmd, 'ERR1')}\r".encode())
        c.close()


def test_health_projectors_and_maintenance():
    proj = FakeProjector("secret")
    proj.start()
    raw = core.pjlink_query("127.0.0.1", "secret", port=proj.port)
    assert raw["LAMP"] == "2950 1" and raw["POWR"] == "1"
    d = core.describe_projector(raw, 3000)
    assert d["lamp_hours"] == 2950 and d["lamp_pct"] == 98 and d["errors"] == ["Filter warning"]

    tmp = Path(tempfile.mkdtemp())
    repo, cfg, f = make_repo(tmp)
    repo.save_projector({"name": "Left", "host": "10.0.0.41", "lamp_life": 3000})
    winsys.FAKES.update(disks=[{"name": "Samsung 980", "health": "Healthy", "temp": 64, "wear": 12},
                               {"name": "WD Blue", "health": "Warning", "temp": 35, "wear": None}],
                        gpu={"name": "RTX", "temp": 88.0, "util": 99.0}, pjlink=lambda host: raw)
    try:
        res = repo.health_check()
        w = " | ".join(res["warnings"])
        assert "Samsung 980: 64 °C" in w and "WD Blue: health Warning" in w and "GPU at 88" in w
        assert "lamp at 2950 h (98%" in w and "Filter warning" in w
        assert repo.health_status()["projectors"][0]["lamp_hours"] == 2950
    finally:
        winsys.FAKES.clear()

    repo.save_maintenance_task({"name": "Clean filters", "every_days": 90})
    repo.save_maintenance_task({"name": "Calibrate yoke", "every_hours": 1})
    t = {x["name"]: x for x in repo.maintenance_tasks()}
    assert t["Clean filters"]["state"] == "ok" and t["Calibrate yoke"]["state"] == "soon"
    repo.db.execute("UPDATE maint_tasks SET created=? WHERE name='Clean filters'", (time.time() - 100 * 86400,))
    repo.db.commit()
    t = {x["name"]: x for x in repo.maintenance_tasks()}
    assert t["Clean filters"]["state"] == "overdue"
    assert core.build_status(repo)["maintenance_overdue"] == ["Clean filters"]
    # done from the Hub (phone page) → command in the report response
    applied = core.apply_hub_commands(repo, [{"id": "c1", "type": "maintenance_done", "task": t["Clean filters"]["id"],
                                              "by": "Jan", "note": "both filters"}])
    assert applied == ["c1"]
    assert {x["name"]: x for x in repo.maintenance_tasks()}["Clean filters"]["state"] == "ok"
    assert repo.maintenance_log()[0]["by"] == "Jan"

    md = repo.build_manual()
    for section in ("## Status", "## Tracked files", "## Projectors", "## Maintenance schedule"):
        assert section in md
    html_ = core.markdown_to_html(md, "x")
    assert "<table>" in html_ and "<h2>Maintenance schedule</h2>" in html_
    rep = repo.build_hub_report()
    assert {"sessions", "performance", "hardware", "health", "maintenance", "manifests", "installs"} <= set(rep)
    assert "Clean filters" in mcp_server.t_hardware_health(repo, {})


def test_remote_allows_feature_methods():
    for m in ("session_tick", "start_benchmark", "hardware_check", "start_install", "rollback_install",
              "create_manifest", "apply_template", "health_check", "maintenance_done", "build_manual"):
        assert m in remote.ALLOWED, m
