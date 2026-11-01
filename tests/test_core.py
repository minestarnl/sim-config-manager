import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from simconfigmanager.core import Repo, Rule, FolderSync, evaluate_rule, Change, TrackedFile  # noqa: E402

CFG = """[GRAPHICS]
TEXTURE_MAX_LOAD=4096
FULLSCREEN=1
[DISPLAY]
WideViewAspect=True
"""
DLL = """<?xml version="1.0" encoding="Windows-1252"?>
<SimBase.Document Type="Launch" version="1,0">
  <Launch.Addon><Name>FSUIPC</Name><Disabled>False</Disabled><Path>Modules\\FSUIPC5.dll</Path></Launch.Addon>
</SimBase.Document>
"""


def touch(p, text):
    Path(p).write_text(text, encoding="utf-8")
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # force mtime change


def test_all():
    tmp = Path(tempfile.mkdtemp())
    sim = tmp / "sim"
    sim.mkdir()
    cfg, dll = sim / "Prepar3D.cfg", sim / "dll.xml"
    touch(cfg, CFG)
    touch(dll, DLL)

    repo = Repo(tmp / "data")
    fc = repo.add_file(str(cfg), group="P3D v5")
    fd = repo.add_file(str(dll), group="P3D v5")
    assert repo.ensure_baseline() is not None
    assert repo.scan() == []
    assert repo.file_status(fc) == "OK"

    rp = repo.create_restore_point("Customer default")
    assert rp.name == "Customer default"

    repo.save_rule(Rule(0, "Texture", fc.id, "ini_value", "GRAPHICS/TEXTURE_MAX_LOAD", "4096"))
    repo.save_rule(Rule(0, "FSUIPC gone", fd.id, "line_removed", "FSUIPC"))
    repo.save_rule(Rule(0, "Lock DLL", fd.id, "differs_from_rp", "Customer default", action="revert"))

    # change cfg
    touch(cfg, CFG.replace("4096", "1024"))
    ch = repo.scan()
    assert len(ch) == 1 and ch[0].file.id == fc.id
    alerts = repo.process_changes(ch)
    assert any("TEXTURE_MAX_LOAD" in a.message for a in alerts), alerts
    assert repo.scan() == []  # auto-snapshot recorded it
    hist = repo.file_history(fc.id)
    assert len(hist) == 2

    # same version shouldn't alert twice
    assert repo.process_changes(repo.scan()) == []

    # change dll -> two alerts, auto revert
    touch(dll, DLL.replace("FSUIPC", "Other"))
    alerts = repo.process_changes(repo.scan())
    msgs = [a.message for a in alerts]
    assert any("removed" in m for m in msgs), msgs
    assert any(a.reverted for a in alerts), msgs
    assert "FSUIPC" in dll.read_text()

    # restore all to restore point
    report = repo.restore(rp.snapshot_id, label="Customer default")
    assert ("restored" in [r for _, r in report])
    assert "4096" in cfg.read_text()

    # deleted file
    os.remove(cfg)
    ch = repo.scan()
    assert ch and ch[0].new_hash is None
    repo.process_changes(ch)
    assert repo.file_status(fc) == "Missing"
    repo.restore(rp.snapshot_id)
    assert cfg.exists()

    # xml rule
    r = Rule(0, "x", None, "xml_value", ".//Launch.Addon/Name")
    c = Change(TrackedFile(1, "dll.xml", "dll", "", 0), "a", "b", DLL.encode(), DLL.replace("FSUIPC", "X").encode())
    assert "changed" in evaluate_rule(r, c)

    # changelog + zip export
    repo.export_changelog(str(tmp / "log.md"))
    repo.export_changelog(str(tmp / "log.csv"), "csv")
    repo.export_snapshot_zip(rp.snapshot_id, str(tmp / "rp.zip"))
    assert (tmp / "log.md").read_text().count("|") > 10

    # sync
    cloud = tmp / "drive"
    cloud.mkdir()
    s = FolderSync(repo, str(cloud))
    res = s.push()
    assert res["objects_copied"] > 0
    ms = s.machines()
    assert ms and ms[0]["machine"] == repo.machine
    latest = s.remote_latest(repo.machine)
    assert any(x["path"] == str(cfg) for x in latest)
    assert b"4096" in s.fetch([x for x in latest if x["path"] == str(cfg)][0]["hash"])

    # disaster recovery into empty store
    repo2 = Repo(tmp / "data2")
    info = FolderSync(repo2, str(cloud)).import_machine(repo.machine)
    assert info["snapshots"] == len(repo.snapshots())
    assert repo2.restore_point("Customer default") is not None
    assert len(repo2.rules()) == 3
    print("ALL CORE TESTS PASSED")



def test_archive():
    import zipfile
    import xml.etree.ElementTree as ET
    from simconfigmanager import core
    tmp = Path(tempfile.mkdtemp())
    home1 = tmp / "home1" / "AppData" / "Roaming"
    (home1 / "P3D").mkdir(parents=True)
    os.environ["APPDATA"] = str(home1)
    cfg = home1 / "P3D" / "Prepar3D.cfg"
    touch(cfg, CFG)
    other = tmp / "other.xml"
    touch(other, DLL)
    repo = Repo(tmp / "d1")
    fc = repo.add_file(str(cfg), group="Sim 1")
    repo.add_file(str(other), "dll.xml", group="Sim 1")
    repo.ensure_baseline()
    repo.create_restore_point("Default")
    repo.save_rule(Rule(0, "tex", fc.id, "ini_value", "GRAPHICS/TEXTURE_MAX_LOAD", "4096"))
    assert core.to_portable(str(cfg)).startswith("%APPDATA%")
    z = tmp / "out" / "arc.zip"
    z.parent.mkdir()
    res = core.export_archive(repo, str(z), note="cert test")
    assert res["files"] == 2 and (tmp / "out" / "arc.zip.sha256").exists()
    names = zipfile.ZipFile(z).namelist()
    assert {"archive.json", "REPORT.txt", "CHECKSUMS.sha256", "history/manifest.json"} <= set(names), names
    rep_txt = zipfile.ZipFile(z).read("REPORT.txt").decode()
    assert "cert test" in rep_txt and fc.label in rep_txt

    # new PC with a different user folder
    home2 = tmp / "home2" / "AppData" / "Roaming"
    home2.mkdir(parents=True)
    os.environ["APPDATA"] = str(home2)
    arc = core.Archive(str(z))
    v = arc.verify()
    assert v["ok"] == 2 and not v["bad"] and v["zip_check"] == "matches", v
    targets = {e["id"]: arc.suggested_target(e) for e in arc.files}
    assert any(t.startswith(str(home2)) for t in targets.values()), targets
    repo2 = Repo(tmp / "d2")
    report = arc.apply(repo2, targets, write_files=True, track=True, import_history=True)
    assert sum(1 for _, r in report if r == "written") == 1, report  # other.xml already identical (same path)
    assert (home2 / "P3D" / "Prepar3D.cfg").read_text() == CFG
    assert repo2.restore_point("Default") and len(repo2.rules()) == 1
    f2 = repo2.find_file_by_path(str(home2 / "P3D" / "Prepar3D.cfg"))
    assert f2 and repo2.file_status(f2) == "OK" and len(repo2.file_history(f2.id)) >= 1
    arc.close()

    # tamper detection
    with open(z, "ab") as fh:
        fh.write(b"x")
    assert "NOT" in core.Archive(str(z)).verify()["zip_check"]

    # scheduled-task XML is valid
    ET.fromstring(core._task_xml().encode("utf-16"))
    print("ARCHIVE TESTS PASSED")



def test_versions():
    from simconfigmanager import core
    tmp = Path(tempfile.mkdtemp())
    a, b = tmp / "a.cfg", tmp / "b.xml"
    touch(a, CFG)
    touch(b, DLL)
    repo = Repo(tmp / "d")
    fa = repo.add_file(str(a))
    repo.add_file(str(b))
    v1 = repo.create_version("First", "initial")
    assert v1.number == 1
    assert repo.mismatches() == []
    touch(a, CFG.replace("4096", "2048"))
    mm = repo.mismatches()
    assert [m.file.id for m in mm] == [fa.id] and mm[0].status == "modified"
    notes = repo.draft_release_notes()
    assert "a.cfg" in notes and "changed" in notes, notes
    c = tmp / "c.ini"
    touch(c, "[x]\ny=1\n")
    repo.add_file(str(c))
    repo.ensure_baseline()
    st = {m.file.display: m.status for m in repo.mismatches()}
    assert st == {"a.cfg": "modified", "c.ini": "new"}, st
    v2 = repo.create_version("Second")
    assert v2.number == 2 and repo.mismatches() == []
    # compare against old version
    assert {m.file.display for m in repo.mismatches(1)} == {"a.cfg", "c.ini"}
    ch = repo.changes_between(v1.snapshot_id, v2.snapshot_id)
    assert {f.display for f, _, _ in ch} == {"a.cfg", "c.ini"}
    # put back to v1 for one file
    repo.restore(v1.snapshot_id, [fa.id], label="v1")
    assert "4096" in a.read_text()
    assert {m.file.display for m in repo.mismatches()} == {"a.cfg"}
    os.remove(b)
    assert {m.file.display: m.status for m in repo.mismatches()}["b.xml"] == "missing"
    # archive mentions version
    res = core.export_archive(repo, str(tmp / "x.zip"))
    import zipfile
    rep_txt = zipfile.ZipFile(tmp / "x.zip").read("REPORT.txt").decode()
    assert "v2 – Second" in rep_txt and "DIFFER" in rep_txt, rep_txt[:800]
    # sync/import carries versions
    cloud = tmp / "cloud"
    cloud.mkdir()
    FolderSync(repo, str(cloud)).push()
    r2 = Repo(tmp / "d2")
    FolderSync(r2, str(cloud)).import_machine(repo.machine)
    assert [v.number for v in r2.versions()] == [2, 1]
    print("VERSION TESTS PASSED")


if __name__ == "__main__":
    test_all()
    test_archive()
    test_versions()
