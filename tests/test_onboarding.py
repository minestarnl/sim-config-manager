"""First-run setup: finds the P3D / ProSim files, pre-ticks the usual ones, and sets everything up on Finish."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")


def test_onboarding_finds_and_tracks_files(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    app_data, prog_data = tmp / "AppData", tmp / "ProgramData"
    p3d = app_data / "Lockheed Martin" / "Prepar3D v5"
    (p3d / "Controls").mkdir(parents=True)
    for n in ("Prepar3D.cfg", "dll.xml", "Cameras.cfg", "terrain.cfg"):
        (p3d / n).write_text("[x]\ny=1\n")
    (p3d / "Controls" / "Standard.xml").write_text("<x/>")
    prosim = tmp / "drive" / "ProSim737" / "ProSimB738"          # ProSim System folder
    prosim.mkdir(parents=True)
    for n in ("config.xml", "cockpitsetup.xml", "debug.ini"):
        (prosim / n).write_text("<x/>")
    from simconfigmanager.core import system
    monkeypatch.setattr(system, "prosim_roots", lambda: [str(tmp / "drive")])   # not this PC's real drives
    monkeypatch.setenv("APPDATA", str(app_data))
    monkeypatch.setenv("PROGRAMDATA", str(prog_data))
    monkeypatch.setenv("USERNAME", "Tech")

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    from simconfigmanager import theme
    theme.init(app, "dark")
    from simconfigmanager.core import Repo
    from simconfigmanager.ui import MainWindow
    from simconfigmanager.ui.onboarding import OnboardingWizard, maybe_run_onboarding, recommended

    assert recommended("P3D v5", "x/Prepar3D.cfg") and not recommended("P3D v5", "x/terrain.cfg")
    assert recommended("ProSim", "c/ProSim737/config.xml") and not recommended("ProSim", "c/a.ini")
    assert recommended("ProSim", r"C:\prosim\ProSimB738\CockpitSetup.xml")
    repo = Repo(tmp / "data")
    win = MainWindow(repo, start_hidden=True)
    maybe_run_onboarding(win)
    wiz = win._onboarding
    assert isinstance(wiz, OnboardingWizard)
    wiz.name.setText("Sim 1")
    wiz.sysinfo.setChecked(False)
    wiz.audit.setChecked(False)
    wiz.autostart.setChecked(False)
    wiz._scan()
    chosen = {os.path.basename(c[2]) for c in wiz.chosen_files()}
    assert chosen == {"Prepar3D.cfg", "dll.xml", "Cameras.cfg", "Standard.xml",        # terrain.cfg unticked
                      "config.xml", "cockpitsetup.xml"}, chosen                         # debug.ini unticked
    wiz.share.setCurrentIndex(1)
    wiz.share_folder.setText(str(tmp / "shared"))
    wiz.accept()
    assert repo.machine == "Sim 1" and len(repo.files()) == 6
    assert repo.latest_version().number == 1 and repo.get("sync_folder") == str(tmp / "shared")
    assert {f.group for f in repo.files()} == {"Sim 1 – P3D v5", "Sim 1 – ProSim"}
    assert repo.get("onboarding_done") == "1"
    win._onboarding = None
    maybe_run_onboarding(win)          # not again
    assert win._onboarding is None
