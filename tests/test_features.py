"""1.8 features: Google Drive sync (against a fake Drive), ignore rules, setting-level diffs, system info
probes, rulebook, fleet status."""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from simconfigmanager import core, storage  # noqa: E402
from simconfigmanager.core import Repo, Sync  # noqa: E402
from fake_drive import FakeDrive  # noqa: E402

CFG = "[GRAPHICS]\r\nTEXTURE_MAX_LOAD=4096\r\n[Window.Main]\r\nposX=10\r\nposY=20\r\n[TERRAIN]\r\nLOD_RADIUS=6.5\r\n"
XML = ('<?xml version="1.0" encoding="utf-8"?>\n<Config>\n  <Server ip="10.0.0.1">main</Server>\n'
       '  <Window><X>1</X></Window>\n</Config>\n')


def bump(p, text):
    p.write_bytes(text.encode())
    st = os.stat(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))


def setup(tmp, name="Sim 1"):
    (tmp / "sim").mkdir()
    cfg, xml = tmp / "sim" / "Prepar3D.cfg", tmp / "sim" / "config.xml"
    bump(cfg, CFG)
    bump(xml, XML)
    repo = Repo(tmp / "data")
    repo.set("machine_name", name)
    fc = repo.add_file(str(cfg), group="P3D")
    fx = repo.add_file(str(xml), "ProSim config.xml", group="ProSim")
    repo.ensure_baseline()
    return repo, cfg, xml, fc, fx


def test_ignore_rules_and_setting_diffs():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, xml, fc, fx = setup(tmp)
    repo.create_version("v1")
    # window position changes → noise
    repo.add_ignore(fc.id, "ini_section", "window.*", "P3D writes window positions on exit")
    repo.add_ignore(fx.id, "xml", ".//Window")
    bump(cfg, CFG.replace("posX=10", "posX=99"))
    bump(xml, XML.replace("<X>1</X>", "<X>5</X>"))
    assert repo.scan() == [], "ignored-only changes must not count"
    assert repo.mismatches() == []
    assert repo.file_status(fc) == "OK"
    # a real change still counts
    bump(cfg, CFG.replace("posX=10", "posX=99").replace("4096", "2048"))
    ch = repo.scan()
    assert [c.file.id for c in ch] == [fc.id]
    sc = core.setting_changes(ch[0].old_data, ch[0].new_data, str(cfg))
    texts = [core.describe_change(c) for c in sc]
    assert "[GRAPHICS] TEXTURE_MAX_LOAD: 4096 → 2048" in texts, texts
    assert any("posX" in t for t in texts)  # diff still shows it; ignoring only affects "did it change"
    xs = core.setting_changes(XML.encode(), XML.replace('ip="10.0.0.1"', 'ip="10.0.0.2"').encode(), "c.xml")
    assert [core.describe_change(c) for c in xs] == ["Config/Server@ip: 10.0.0.1 → 10.0.0.2"], xs
    repo.process_changes(ch)
    assert [m.file.id for m in repo.mismatches()] == [fc.id]
    print("IGNORE/DIFF OK")


def test_probes_and_rulebook():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, xml, fc, fx = setup(tmp)
    state = tmp / "driver.txt"
    state.write_text("Driver 560.10\n")
    pf = core.add_custom_probe(repo, "GPU driver", f"cat '{state}'", group="Sim 1 – System")
    assert core.is_probe(pf) and repo.read_current(pf) == b"Driver 560.10\n"
    repo.ensure_baseline()
    repo.create_version("v1")
    # driver update → change detected (after the probe interval / forced refresh)
    state.write_text("Driver 566.03\n")
    assert repo.scan() == []            # cached for probe_minutes
    repo.refresh_probes()
    ch = repo.scan()
    assert [c.file.id for c in ch] == [pf.id]
    repo.process_changes(ch)
    assert {m.file.id for m in repo.mismatches()} == {pf.id}
    # probes are never written by restore
    v1 = repo.version(1)
    rep = repo.restore(v1.snapshot_id)
    assert all(f.id != pf.id for f, _ in rep)
    try:
        repo.restore_version(pf.id, repo.entries(v1.snapshot_id)[pf.id])
        raise AssertionError("must refuse")
    except ValueError:
        pass

    # rulebook
    repo.save_rulebook_entry({"file_id": fc.id, "selector": "GRAPHICS/TEXTURE_MAX_LOAD", "required": "4096",
                              "note": "Certified value"})
    repo.save_rulebook_entry({"file_id": fx.id, "selector": ".//Server@ip", "required": "10.0.0.1",
                              "note": "ProSim server"})
    repo.save_rulebook_entry({"file_id": fc.id, "selector": "TERRAIN/LOD_RADIUS", "required": "",
                              "note": "Lower = smoother on projectors"})
    st = repo.rulebook_status()
    assert [e["ok"] for e in st] == [True, None, True] or sorted(map(str, [e["ok"] for e in st])) == \
        sorted(["True", "True", "None"]), st
    bump(cfg, CFG.replace("4096", "1024"))
    alerts = repo.process_changes(repo.scan())
    assert any("rulebook requires '4096'" in a.message for a in alerts), [a.message for a in alerts]
    viol = core.rulebook_violations_for(repo, fx, XML.replace("10.0.0.1", "10.9.9.9").encode())
    assert viol and viol[0]["selector"] == ".//Server@ip"
    status = core.build_status(repo)
    assert status["rulebook_violations"] and status["machine"] == "Sim 1"
    print("PROBES/RULEBOOK OK")


def test_drive_sync():
    drive = FakeDrive().start()
    try:
        prov = lambda: "test-token"  # noqa: E731
        tmp = Path(tempfile.mkdtemp())
        repo, cfg, xml, fc, fx = setup(tmp)
        repo.create_version("first")
        be = storage.DriveBackend(prov, "shared123", api_base=drive.url)
        assert be.ok() and be.check() == "Sim configs"
        sync = Sync(repo, be)
        drive.fail_next = 1  # a transient 503 must be retried
        res = sync.push()
        assert res["objects_copied"] == 2, res
        assert drive.names_in("machines") == ["Sim_1.json"]
        assert drive.names_in("status") == ["Sim_1.json"]
        assert len(drive.names_in("objects")) == 2
        # second push uploads nothing new, updates manifest in place (no duplicates)
        bump(cfg, CFG.replace("4096", "8192"))
        repo.process_changes(repo.scan())
        res = sync.push()
        assert res["objects_copied"] == 1 and drive.names_in("machines") == ["Sim_1.json"]

        # another sim reads it all through its own client instance
        tmp2 = Path(tempfile.mkdtemp())
        r2, *_ = setup(tmp2, "Sim 2")
        storage.DriveBackend._cache.clear()
        s2 = Sync(r2, storage.DriveBackend(prov, "shared123", api_base=drive.url))
        s2.push()
        assert sorted(m["machine"] for m in s2.machines()) == ["Sim 1", "Sim 2"]
        latest = {f["label"]: f for f in s2.remote_latest("Sim 1")}
        assert b"8192" in s2.fetch(latest["Prepar3D.cfg"]["hash"])
        st = {x["machine"]: x for x in s2.statuses()}
        assert st["Sim 1"]["version"]["number"] == 1 and st["Sim 1"]["mismatches"]

        # archives up and down
        z = tmp / "a.zip"
        core.export_archive(repo, str(z), note="cert")
        sync.upload_archive(str(z))
        assert s2.list_archives() == ["a.zip"]
        got = s2.download_archive("a.zip", str(tmp2))
        assert core.Archive(got).verify()["zip_check"] == "matches"

        # disaster recovery from Drive into an empty store
        r3 = Repo(Path(tempfile.mkdtemp()) / "d")
        info = Sync(r3, storage.DriveBackend(prov, "shared123", api_base=drive.url)).import_machine("Sim 1")
        assert info["snapshots"] == len(repo.snapshots()) and r3.versions()

        # make_sync from settings (service-account mode) with a stubbed token provider
        repo.set("sync_mode", "gdrive")
        repo.set("gdrive_auth", "service_account")
        repo.set("gdrive_folder_id", "shared123")
        core.drive_files(repo)["service_account"].write_text(json.dumps({"client_email": "sim@x.iam"}))
        orig = storage.service_account_token_provider
        storage.service_account_token_provider = lambda f: prov
        os.environ["SCM_DRIVE_API"] = drive.url
        try:
            s = core.make_sync(repo)
            assert s is not None and s.ok and "Google Drive" in s.where
        finally:
            storage.service_account_token_provider = orig
            del os.environ["SCM_DRIVE_API"]
        # wrong token → clear error
        bad = storage.DriveBackend(lambda: "nope", "shared123", api_base=drive.url)
        try:
            bad.check()
            raise AssertionError("expected refusal")
        except storage.StorageError as e:
            assert "refused" in str(e)
        print("DRIVE OK")
    finally:
        drive.stop()


if __name__ == "__main__":
    test_ignore_rules_and_setting_diffs()
    test_probes_and_rulebook()
    test_drive_sync()
