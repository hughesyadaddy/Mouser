import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_macos_gui_session as gui


class ConsoleUserTests(unittest.TestCase):
    def test_returns_owner_of_dev_console(self):
        with mock.patch.object(gui.subprocess, "check_output", return_value="alex\n"):
            self.assertEqual(gui.console_user(), "alex")

    def test_returns_none_when_stat_unavailable(self):
        with mock.patch.object(gui.subprocess, "check_output", side_effect=FileNotFoundError):
            self.assertIsNone(gui.console_user())

    def test_returns_none_when_console_has_no_owner(self):
        with mock.patch.object(gui.subprocess, "check_output", return_value="\n"):
            self.assertIsNone(gui.console_user())


class KeychainReachableTests(unittest.TestCase):
    def _result(self, code):
        return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr="")

    def test_true_when_security_succeeds(self):
        with mock.patch.object(gui.subprocess, "run", return_value=self._result(0)):
            self.assertTrue(gui.keychain_reachable())

    def test_false_when_interaction_not_allowed(self):
        # The exact failure seen over SSH: the keychain is not locked, the
        # session simply cannot reach it.
        with mock.patch.object(gui.subprocess, "run", return_value=self._result(36)):
            self.assertFalse(gui.keychain_reachable())


class RoutingDecisionTests(unittest.TestCase):
    def _decide(self, *, platform="darwin", reachable, owner, user):
        with mock.patch.object(gui.sys, "platform", platform), \
             mock.patch.object(gui, "keychain_reachable", return_value=reachable), \
             mock.patch.object(gui, "console_user", return_value=owner), \
             mock.patch.dict(os.environ, {"USER": user}, clear=False):
            return gui.should_route_through_gui_session()

    def test_routes_when_keychain_unreachable_and_user_owns_console(self):
        self.assertTrue(self._decide(reachable=False, owner="alex", user="alex"))

    def test_does_not_route_when_keychain_already_reachable(self):
        # Building locally must stay a plain in-place build.
        self.assertFalse(self._decide(reachable=True, owner="alex", user="alex"))

    def test_does_not_route_when_someone_else_owns_the_console(self):
        # Driving another user's GUI session is neither permitted nor wanted.
        self.assertFalse(self._decide(reachable=False, owner="root", user="alex"))

    def test_does_not_route_when_nobody_is_logged_in(self):
        self.assertFalse(self._decide(reachable=False, owner=None, user="alex"))

    def test_does_not_route_off_darwin(self):
        self.assertFalse(
            self._decide(platform="linux", reachable=False, owner="alex", user="alex")
        )


class BuildCommandTests(unittest.TestCase):
    def test_appends_exit_sentinel_so_completion_is_detectable(self):
        cmd = gui.build_command([], "/tmp/x.log")
        self.assertIn("scripts/build_and_install.py", cmd)
        self.assertTrue(cmd.rstrip().endswith(f"echo {gui.SENTINEL}=$? >> /tmp/x.log"))

    def test_never_forces_adhoc_signing(self):
        # The whole point of this helper: ad-hoc signing resets TCC grants on
        # every build, so the routed command must not reintroduce it.
        self.assertNotIn("MOUSER_SIGN_IDENTITY=-", gui.build_command([], "/tmp/x.log"))

    def test_quotes_arguments(self):
        cmd = gui.build_command(["--flag", "a b"], "/tmp/x.log")
        self.assertIn("'a b'", cmd)


class FleetGuiExecDelegationTests(unittest.TestCase):
    """Over SSH the build is handed to deskflow's shared fleet-gui-exec when present."""

    def test_no_delegation_when_deskflow_tool_is_absent(self):
        with mock.patch.object(gui, "fleet_gui_exec_script", return_value=None):
            self.assertIsNone(gui.fleet_gui_exec_command(["--dry-run"]))

    def test_delegates_with_double_dash_and_build_script(self):
        tool = Path("/opt/deskflow/tools/fleet-gui-exec.py")
        with mock.patch.object(gui, "fleet_gui_exec_script", return_value=tool):
            cmd = gui.fleet_gui_exec_command(["--dry-run"])
        self.assertEqual(cmd[1], str(tool))
        self.assertEqual(cmd[2], "--")
        self.assertEqual(cmd[3], "python3")
        self.assertEqual(cmd[4], str(gui.ROOT / "scripts" / "build_and_install.py"))
        self.assertEqual(cmd[-1], "--dry-run")

    def test_delegated_command_never_forces_adhoc_signing(self):
        tool = Path("/opt/deskflow/tools/fleet-gui-exec.py")
        with mock.patch.object(gui, "fleet_gui_exec_script", return_value=tool):
            cmd = gui.fleet_gui_exec_command([])
        self.assertNotIn("MOUSER_SIGN_IDENTITY=-", " ".join(cmd))
        self.assertNotIn("-", [part for part in cmd if part != "--"])

    def test_deskflow_root_env_override_locates_the_tool(self):
        from scripts import install_lifecycle
        with tempfile.TemporaryDirectory() as tmp:
            tool = Path(tmp) / "tools" / "fleet-gui-exec.py"
            tool.parent.mkdir(parents=True)
            tool.write_text("#!/usr/bin/env python3\n")
            with mock.patch.dict(os.environ, {"DESKFLOW_ROOT": tmp}, clear=False):
                self.assertEqual(install_lifecycle.fleet_gui_exec_script(), tool)
            with mock.patch.dict(os.environ, {"DESKFLOW_ROOT": str(Path(tmp) / "nope")}, clear=False):
                self.assertIsNone(install_lifecycle.fleet_gui_exec_script())


class MainRoutingTests(unittest.TestCase):
    def _completed(self, code):
        return subprocess.CompletedProcess(args=[], returncode=code, stdout="", stderr="")

    def test_routed_build_uses_fleet_gui_exec_when_present(self):
        tool = Path("/opt/deskflow/tools/fleet-gui-exec.py")
        with mock.patch.object(gui, "should_route_through_gui_session", return_value=True), \
             mock.patch.object(gui, "fleet_gui_exec_script", return_value=tool), \
             mock.patch.object(gui, "_osascript_run") as osascript, \
             mock.patch.object(gui.subprocess, "run", return_value=self._completed(7)) as run:
            self.assertEqual(gui.main(["--dry-run"]), 7)
        osascript.assert_not_called()
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], str(tool))
        self.assertIn("--dry-run", argv)

    def test_routed_build_falls_back_to_terminal_without_deskflow(self):
        with mock.patch.object(gui, "should_route_through_gui_session", return_value=True), \
             mock.patch.object(gui, "fleet_gui_exec_script", return_value=None), \
             mock.patch.object(gui, "console_user", return_value="alex"), \
             mock.patch.object(gui, "_osascript_run") as osascript, \
             mock.patch.object(gui, "_wait_for_exit", return_value=3):
            self.assertEqual(gui.main([]), 3)
        osascript.assert_called_once()
        self.assertNotIn("MOUSER_SIGN_IDENTITY=-", osascript.call_args.args[0])

    def test_unrouted_build_runs_in_place(self):
        with mock.patch.object(gui, "should_route_through_gui_session", return_value=False), \
             mock.patch.object(gui, "fleet_gui_exec_script", return_value=Path("/x")), \
             mock.patch.object(gui.subprocess, "run", return_value=self._completed(0)) as run:
            self.assertEqual(gui.main(["--dry-run"]), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[1], str(gui.ROOT / "scripts" / "build_and_install.py"))


class WaitForExitTests(unittest.TestCase):
    def test_returns_exit_code_from_sentinel(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write(f"building...\n{gui.SENTINEL}=0\n")
            path = fh.name
        try:
            self.assertEqual(gui._wait_for_exit(path, timeout=5), 0)
        finally:
            os.unlink(path)

    def test_propagates_nonzero_exit(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as fh:
            fh.write(f"boom\n{gui.SENTINEL}=2\n")
            path = fh.name
        try:
            self.assertEqual(gui._wait_for_exit(path, timeout=5), 2)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
