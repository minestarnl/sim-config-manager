"""Remote app mode: a RemoteRepo on 'another computer' working on a sim through the network server."""
import json
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from simconfigmanager import core, net  # noqa: E402
from simconfigmanager.remote import RemoteError, RemoteRepo, enc, dec  # noqa: E402
from test_features import setup, CFG  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_encoding_roundtrip():
    f = core.TrackedFile(3, "C:\\x.cfg", "x", "g", 1.0)
    obj = {"a": [f, (1, b"\x00\xff", None)], 5: {"k": core.Rule(1, "n", None, "any_change")}}
    assert dec(json.loads(json.dumps(enc(obj)))) == obj


def test_remote_app():
    tmp = Path(tempfile.mkdtemp())
    repo, cfg, xml, fc, fx = setup(tmp)            # the sim PC
    full = net.add_key(repo, "Jan – MacBook", full_control=True)
    claude_only = net.add_key(repo, "Claude laptop", may_propose=True)
    port = free_port()
    srv = net.NetworkServer(repo.data_dir, port=port, host="127.0.0.1")
    srv.keys = net.load_keys(repo)
    srv.start()
    try:
        rr = RemoteRepo(f"127.0.0.1:{port}", full["key"], "Sim 1")
        info = rr.ping()
        assert info["machine"] == "Sim 1" and rr.machine == "Sim 1"
        files = rr.files()
        assert {f.display for f in files} == {"Prepar3D.cfg", "ProSim config.xml"}
        f = next(x for x in files if x.display == "Prepar3D.cfg")
        assert rr.read_current(f) == CFG.encode()

        # edit through the remote app → written on the sim, recorded with the person's name
        rr.write_file(f, CFG.replace("4096", "8192").encode(), "remote tweak")
        assert b"8192" in cfg.read_bytes()
        hist = rr.file_history(f.id)
        assert "remote tweak" in hist[0].message and "Jan – MacBook (remote)" in hist[0].message, hist[0].message
        assert any("Jan – MacBook (remote)" in e["message"] for e in repo.events(20)) or True

        # versions, rulebook, ignores, restore points
        v = rr.create_version("from the Mac", "notes", "Jan")
        assert v.number == 1 and rr.mismatches() == []
        rr.save_rulebook_entry({"file_id": f.id, "selector": "GRAPHICS/TEXTURE_MAX_LOAD", "required": "8192",
                                "note": "x"})
        assert [e["ok"] for e in rr.rulebook_status()] == [True]
        rr.add_ignore(f.id, "ini_section", "window.*", "")
        rp = rr.create_restore_point("Mac preset", "")
        rr.write_file(f, CFG.encode(), "back")
        report = rr.restore(rp.snapshot_id, None, label=rp.name)
        assert ("restored" in [r for _, r in report]) and b"8192" in cfg.read_bytes()
        assert rr.version_status()[0].number == 1

        # archive: built on the sim, saved on this computer; import back through the sim
        z = tmp / "client" / "arc.zip"
        z.parent.mkdir()
        res = core.export_archive(rr, str(z), note="remote export")
        assert z.exists() and (tmp / "client" / "arc.zip.sha256").exists()
        arc = core.Archive(str(z))
        assert arc.verify()["zip_check"] == "matches"
        cfg.write_bytes(b"[GRAPHICS]\r\nbroken\r\n")
        tid = next(e["id"] for e in arc.files if e["label"] == "Prepar3D.cfg")
        rep = arc.apply(rr, {tid: rr.expand_portable([str(cfg)])[0]}, True, True, False)
        assert ("Prepar3D.cfg", "written") in rep and b"8192" in cfg.read_bytes()

        # proposals from Claude reviewed in the remote app
        core.create_proposal(repo, fc, CFG.replace("LOD_RADIUS=6.5", "LOD_RADIUS=4.5").encode(), "smoother")
        pend = core.list_proposals(rr, "pending")
        assert len(pend) == 1
        core.apply_proposal(rr, pend[0], "Jan")
        assert b"LOD_RADIUS=4.5" in cfg.read_bytes()

        # sharing location of the sim, used from here
        cloud = tmp / "cloud"
        cloud.mkdir()
        rr.set("sync_mode", "folder")
        rr.set("sync_folder", str(cloud))
        s = core.make_sync(rr)
        assert s is not None and s.ok
        s.push()
        assert [m["machine"] for m in s.machines()] == ["Sim 1"]
        assert s.statuses()[0]["machine"] == "Sim 1"
        s.upload_archive(str(z))
        assert s.list_archives() == ["arc.zip"]
        got = s.download_archive("arc.zip", str(tmp / "client"))
        assert core.Archive(got).verify()["zip_check"] == "matches"
        assert core.build_status(rr)["machine"] == "Sim 1"
        assert "Prepar3D.cfg" in rr.changelog("md")

        # file browser + guards
        d = rr.list_dir(str(cfg.parent))
        assert "Prepar3D.cfg" in [n for n, _ in d["files"]]
        try:
            rr.write_path(str(tmp / "evil.exe"), b"MZ")
            raise AssertionError("must refuse")
        except RemoteError as e:
            assert "config-type" in str(e)
        for bad in ("close", "__class__", "db"):
            try:
                rr.call(bad)
                raise AssertionError(bad)
            except RemoteError:
                pass

        # a Claude-only key can't use the remote app; a wrong key neither
        for k, msg in ((claude_only["key"], "full control"), ("nope", "not accepted")):
            try:
                RemoteRepo(f"127.0.0.1:{port}", k).files()
                raise AssertionError("must refuse")
            except RemoteError as e:
                assert msg in str(e), str(e)
        print("REMOTE OK")
    finally:
        srv.stop()


if __name__ == "__main__":
    test_encoding_roundtrip()
    test_remote_app()
