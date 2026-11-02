"""Start SimConfigManager.  Usage:  SimConfigManager.pyw [--minimized] [--data DIR] [--no-elevate]"""
import argparse
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from simconfigmanager import core
from simconfigmanager.core import APP_NAME, Repo, Rule, default_data_dir
from simconfigmanager.gui import MainWindow, make_icon


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minimized", action="store_true", help="start hidden in the tray (used at boot)")
    ap.add_argument("--data", default=None, help="data folder (default C:\\ProgramData\\SimConfigManager)")
    ap.add_argument("--no-elevate", action="store_true", help="do not ask for administrator rights")
    ap.add_argument("--uninstall-cleanup", action="store_true", help=argparse.SUPPRESS)
    args, _ = ap.parse_known_args()

    if args.uninstall_cleanup:   # called by the uninstaller (already elevated)
        try:
            core.set_autostart(False)
            from simconfigmanager import net
            data = Path(args.data) if args.data else default_data_dir()
            try:
                repo = Repo(data, read_only=True)
                net.set_firewall_rule(int(repo.get("net_port") or net.DEFAULT_PORT), False)
            except Exception:  # noqa: BLE001
                net.set_firewall_rule(net.DEFAULT_PORT, False)
            core.connect_claude_desktop(data, False)
        except Exception:  # noqa: BLE001
            pass
        return 0

    # Files under Program Files / ProgramData can only be restored with admin rights, so ask for them.
    # (The built .exe already carries an 'requireAdministrator' manifest; this covers running from source.)
    if sys.platform == "win32" and not core.is_admin() and not args.no_elevate:
        if core.relaunch_as_admin(sys.argv[1:]):
            return 0
        # user clicked "No" on the UAC prompt: carry on without admin rights

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setWindowIcon(make_icon())
    app.setQuitOnLastWindowClosed(False)

    data_dir = args.data or str(default_data_dir())
    Path(data_dir).mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(Path(data_dir) / "instance.lock"))
    if not lock.tryLock(100):
        if not args.minimized:
            QMessageBox.information(None, APP_NAME, f"{APP_NAME} is already running — look for its icon in "
                                    "the system tray (bottom-right, maybe under the ^ arrow).")
        return 0

    repo = Repo(data_dir)
    if repo.get("first_run_done") != "1":
        repo.save_rule(Rule(0, "Any change", None, "any_change"))
        repo.set("first_run_done", "1")

    # Earlier versions started at logon via the registry Run key (cannot start an elevated program) or
    # under the old app name: move either to the current scheduled task.
    if sys.platform == "win32" and core.is_admin() and repo.get("autostart_migrated_v14") != "1":
        try:
            core.migrate_legacy_autostart()
        except OSError:
            pass
        repo.set("autostart_migrated_v14", "1")

    if sys.platform == "win32" and core.is_admin() and repo.get("proposals_acl_v15") != "1":
        core.allow_users_to_propose(repo.data_dir)
        repo.set("proposals_acl_v15", "1")

    if sys.platform == "win32" and core.is_admin():
        try:
            fixed = core.repair_paths_after_move(repo.data_dir)
            if fixed:
                repo.log("Updated after moving the program: " + ", ".join(fixed))
        except Exception:  # noqa: BLE001
            pass

    start_hidden = args.minimized and repo.get("start_minimized") == "1"
    win = MainWindow(repo, start_hidden=start_hidden)
    if sys.platform == "win32" and not core.is_admin():
        win.statusBar().showMessage("Not running as administrator — restoring files in Program Files / "
                                    "ProgramData may fail.")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
