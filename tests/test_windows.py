"""The real Windows integrations (run on the windows-latest CI runner, which has administrator rights).
Elsewhere these tests are skipped; tests/test_operations.py covers the same features with fakes."""
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from simconfigmanager import core  # noqa: E402
from simconfigmanager.core import Repo, winsys  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def test_device_list_and_disk_health():
    winsys.FAKES.clear()
    devs = core.list_devices()
    assert isinstance(devs, list)
    for d in devs:
        assert d["id"] and d["name"] and d["cls"] in core.DEVICE_CLASSES
    disks = core.disk_health()
    assert isinstance(disks, list) and disks, "Get-PhysicalDisk returned nothing"
    assert all("health" in d for d in disks)


def test_event_log_queries():
    winsys.FAKES.clear()
    since = time.time() - 30 * 86400
    assert isinstance(winsys.app_crashes(since), list)
    assert isinstance(winsys.display_resets(since), list)
    st = winsys.gpu_stats()
    assert st is None or "temp" in st


def test_who_changed_it_with_real_auditing():
    winsys.FAKES.clear()
    tmp = Path(tempfile.mkdtemp())
    f = tmp / "Prepar3D.cfg"
    f.write_text("[GRAPHICS]\nTEXTURE_MAX_LOAD=4096\n")
    repo = Repo(tmp / "data")
    tf = repo.add_file(str(f), "Prepar3D.cfg", "P3D")
    repo.ensure_baseline()
    res = repo.enable_file_auditing()
    if not res.get("ok") or res.get("failed"):
        pytest.skip(f"auditing not available on this runner: {res}")
    since = time.time()
    time.sleep(1.5)
    with open(f, "a", encoding="utf-8") as fh:
        fh.write("[DISPLAY]\nWidth=1920\n")
    ch = repo.scan()
    repo.process_changes(ch)
    who = None
    for _ in range(10):                      # the Security log can lag a little
        who = repo.attribute_change(tf.id, ch[0].new_hash, since)
        if who:
            break
        time.sleep(1)
    assert who, "no 4663 event found for the write"
    assert who["process"].lower().startswith("python"), who


def test_running_processes_and_probe():
    winsys.FAKES.clear()
    me = os.path.basename(sys.executable)
    tmp = Path(tempfile.mkdtemp())
    repo = Repo(tmp / "data")
    assert repo.running_processes_named([me]) == [me]
    out = core.run_probe(core.probe_command(repo, "system://windows"))
    assert out and b"Windows" in out
