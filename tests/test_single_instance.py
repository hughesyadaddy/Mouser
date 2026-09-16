import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import single_instance as si


# ─────────────────────────────────────────────────────────────────────────────
# Lock
# ─────────────────────────────────────────────────────────────────────────────


@unittest.skipIf(sys.platform == "win32", "flock lock is POSIX-only")
class PosixLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "nested", "mouser.lock")

    def test_acquire_writes_pid_and_release_frees(self):
        lock = si.acquire(self.path)
        self.assertIsNotNone(lock)
        self.assertTrue(lock.held)
        with open(self.path) as fh:
            self.assertEqual(fh.read().strip(), str(os.getpid()))
        lock.release()
        self.assertFalse(lock.held)
        again = si.acquire(self.path)
        self.assertIsNotNone(again)
        again.release()

    def test_second_process_cannot_acquire_while_held(self):
        lock = si.acquire(self.path)
        self.addCleanup(lock.release)
        probe = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from core import single_instance as si
            lock = si.acquire({self.path!r})
            print("held" if lock is not None else "blocked")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "blocked")

    def test_second_process_acquires_after_release(self):
        lock = si.acquire(self.path)
        lock.release()
        probe = textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {str(ROOT)!r})
            from core import single_instance as si
            print("held" if si.acquire({self.path!r}) is not None else "blocked")
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, check=True
        )
        self.assertEqual(result.stdout.strip(), "held")


class AcquireOrExitTests(unittest.TestCase):
    def test_loser_pings_and_exits_zero(self):
        exits: list[int] = []
        with (
            patch.object(si, "is_interactive_console_session", return_value=True),
            patch.object(si, "acquire", return_value=None),
            patch.object(si, "notify_running_instance", return_value=True) as ping,
        ):
            result = si.acquire_or_exit(exit_fn=exits.append)
        self.assertIsNone(result)
        self.assertEqual(exits, [0])
        ping.assert_called_once_with(timeout=2.0)

    def test_winner_returns_lock_without_ping(self):
        fake = MagicMock(held=True)
        with (
            patch.object(si, "is_interactive_console_session", return_value=True),
            patch.object(si, "acquire", return_value=fake),
            patch.object(si, "notify_running_instance") as ping,
        ):
            result = si.acquire_or_exit(exit_fn=lambda code: None)
        self.assertIs(result, fake)
        ping.assert_not_called()

    def test_non_console_session_refuses_before_locking(self):
        exits: list[int] = []
        with (
            patch.object(si, "is_interactive_console_session", return_value=False),
            patch.object(si, "acquire") as acquire,
        ):
            si.acquire_or_exit(exit_fn=exits.append)
        self.assertEqual(exits, [0])
        acquire.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Windows session gate (mocked ctypes)
# ─────────────────────────────────────────────────────────────────────────────


class _FakeKernel32:
    def __init__(self, process_session: int, console_session: int):
        self._session = process_session
        self._console = console_session

    def ProcessIdToSessionId(self, pid, out_ptr):
        out_ptr._obj.value = self._session
        return 1

    def WTSGetActiveConsoleSessionId(self):
        return self._console


class WindowsSessionCheckTests(unittest.TestCase):
    def test_non_windows_is_always_interactive(self):
        with patch.object(si.sys, "platform", "darwin"):
            self.assertTrue(si.is_interactive_console_session())

    def test_matching_session_is_interactive(self):
        with patch.object(si.sys, "platform", "win32"):
            self.assertTrue(si.is_interactive_console_session(_FakeKernel32(2, 2)))

    def test_session_zero_is_refused(self):
        with patch.object(si.sys, "platform", "win32"):
            self.assertFalse(si.is_interactive_console_session(_FakeKernel32(0, 2)))

    def test_no_console_session_is_refused(self):
        with patch.object(si.sys, "platform", "win32"):
            self.assertFalse(
                si.is_interactive_console_session(_FakeKernel32(1, 0xFFFFFFFF))
            )

    def test_session_ids_read_through_ctypes(self):
        session, console = si.windows_session_ids(_FakeKernel32(3, 3), pid=1234)
        self.assertEqual((session, console), (3, 3))


# ─────────────────────────────────────────────────────────────────────────────
# Raise channel
# ─────────────────────────────────────────────────────────────────────────────


class RaiseMessageTests(unittest.TestCase):
    def test_quit_payload(self):
        self.assertEqual(si.parse_raise_message(si.RAISE_MSG_QUIT), "quit")
        self.assertEqual(si.parse_raise_message(b' {"cmd": "quit"} \n'), "quit")

    def test_show_and_garbage_default_to_show(self):
        self.assertEqual(si.parse_raise_message(si.RAISE_MSG_SHOW), "show")
        self.assertEqual(si.parse_raise_message(b""), "show")
        self.assertEqual(si.parse_raise_message(None), "show")
        self.assertEqual(si.parse_raise_message(b'{"cmd": "dance"}'), "show")
        self.assertEqual(si.parse_raise_message(b"[1,2]"), "show")

    @unittest.skipIf(sys.platform == "win32", "AF_UNIX test")
    def test_unix_socket_roundtrip(self):
        import socket
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "s.sock")
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(path)
            srv.listen(1)
            received: list[bytes] = []

            def accept():
                conn, _ = srv.accept()
                received.append(conn.recv(1024))
                conn.close()

            t = threading.Thread(target=accept)
            t.start()
            self.assertTrue(si.send_raise_message(si.RAISE_MSG_QUIT, timeout=2.0, address=path))
            t.join(2)
            srv.close()
        self.assertEqual(si.parse_raise_message(received[0]), "quit")

    @unittest.skipIf(sys.platform == "win32", "AF_UNIX test")
    def test_unix_socket_missing_returns_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(
                si.send_raise_message(b"show", timeout=0.2, address=os.path.join(tmp, "nope"))
            )

    def test_server_address_is_stable_and_absolute_on_unix(self):
        with patch.object(si.sys, "platform", "darwin"):
            a = si.server_address()
            b = si.server_address()
        self.assertEqual(a, b)
        self.assertTrue(os.path.isabs(a))
        self.assertTrue(a.endswith(si.SOCKET_FILENAME) or a.startswith("/tmp/"))

    def test_server_address_is_pipe_name_on_windows(self):
        with patch.object(si.sys, "platform", "win32"):
            name = si.server_address()
        self.assertTrue(name.startswith("mouser_instance_"))
        self.assertEqual(len(name), len("mouser_instance_") + 16)


# ─────────────────────────────────────────────────────────────────────────────
# ctl status parsing
# ─────────────────────────────────────────────────────────────────────────────


PS_OUTPUT = """\
    1 2-03:00:00 /sbin/launchd
  411 01:00:00 /Applications/Mouser.app/Contents/MacOS/Mouser
  512    00:42 /Applications/Mouser.app/Contents/MacOS/Mouser --start-hidden
  600    00:10 /Applications/Mouser.app/Contents/MacOS/MouserHelper
  777    00:05 /Users/x/Desktop/Mouser/dist/Mouser.app/Contents/MacOS/Mouser
  999    00:01 grep Mouser
"""


class StatusParsingTests(unittest.TestCase):
    EXE = "/Applications/Mouser.app/Contents/MacOS/Mouser"

    def test_parse_ps_filters_on_image_path_newest_first(self):
        rows = si.parse_ps_output(PS_OUTPUT, self.EXE)
        self.assertEqual(rows, [(512, 42), (411, 3600)])

    def test_parse_ps_excludes_own_pid(self):
        rows = si.parse_ps_output(PS_OUTPUT, self.EXE, own_pid=512)
        self.assertEqual(rows, [(411, 3600)])

    def test_parse_ps_other_root_matches_only_its_own_path(self):
        rows = si.parse_ps_output(
            PS_OUTPUT, "/Users/x/Desktop/Mouser/dist/Mouser.app/Contents/MacOS/Mouser"
        )
        self.assertEqual(rows, [(777, 5)])

    def test_parse_cim_json_single_and_list(self):
        exe = r"C:\Program Files\Mouser\Mouser.exe"
        one = json.dumps({"ProcessId": 10, "ExecutablePath": exe, "Elapsed": 5})
        self.assertEqual(si.parse_cim_output(one, exe), [(10, 5)])
        many = json.dumps(
            [
                {"ProcessId": 10, "ExecutablePath": exe, "Elapsed": 500},
                {"ProcessId": 11, "ExecutablePath": exe.lower(), "Elapsed": 3},
                {"ProcessId": 12, "ExecutablePath": r"C:\Other\Mouser.exe", "Elapsed": 1},
                {"ProcessId": 13, "ExecutablePath": None, "Elapsed": 1},
            ]
        )
        self.assertEqual(si.parse_cim_output(many, exe), [(11, 3), (10, 500)])
        self.assertEqual(si.parse_cim_output("", exe), [])
        self.assertEqual(si.parse_cim_output("not json", exe), [])

    def test_ctl_status_exit_codes(self):
        with patch.object(si, "list_instances", return_value=[]):
            self.assertEqual(si.ctl_status(self.EXE), si.EXIT_NONE)
        with patch.object(si, "list_instances", return_value=[(1, 1)]):
            self.assertEqual(si.ctl_status(self.EXE), si.EXIT_ONE)
        with patch.object(si, "list_instances", return_value=[(1, 1), (2, 2)]):
            self.assertEqual(si.ctl_status(self.EXE), si.EXIT_MANY)

    def test_parse_etime_forms(self):
        self.assertEqual(si.parse_etime("42"), 42)
        self.assertEqual(si.parse_etime("00:42"), 42)
        self.assertEqual(si.parse_etime("01:00:00"), 3600)
        self.assertEqual(si.parse_etime("2-03:00:00"), 2 * 86400 + 3 * 3600)

    def test_list_instances_runs_ps_with_etime(self):
        with patch.object(si.sys, "platform", "darwin"):
            with patch.object(si.subprocess, "run") as run:
                run.return_value = MagicMock(stdout=PS_OUTPUT)
                rows = si.list_instances(self.EXE)
        self.assertEqual(run.call_args[0][0], ["ps", "-axo", "pid=,etime=,command="])
        self.assertEqual([pid for pid, _ in rows], [512, 411])


# ─────────────────────────────────────────────────────────────────────────────
# ctl stop escalation (fake clock)
# ─────────────────────────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class StopEscalationTests(unittest.TestCase):
    EXE = "/Applications/Mouser.app/Contents/MacOS/Mouser"

    def _run(self, *, alive_until_quit_s, quit_ok=True, dies_on_kill=True, agent_loaded=False):
        clock = FakeClock()
        quit_at = None
        killed: list[int] = []
        bootouts: list[int] = []

        def list_pids(path):
            if path != self.EXE:
                return []
            if killed and dies_on_kill:
                return []
            if quit_at is not None and alive_until_quit_s is not None:
                if clock.now - quit_at >= alive_until_quit_s:
                    return []
            return [4242]

        def request_quit():
            nonlocal quit_at
            quit_at = clock.now
            return quit_ok

        code = si.ctl_stop(
            [self.EXE],
            request_quit_fn=request_quit,
            list_pids=list_pids,
            kill=killed.append,
            agent_loaded=lambda: agent_loaded,
            bootout=lambda: bootouts.append(1),
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        return code, clock, killed, bootouts

    def test_graceful_quit_needs_no_kill(self):
        code, clock, killed, bootouts = self._run(alive_until_quit_s=2.0)
        self.assertEqual(code, 0)
        self.assertEqual(killed, [])
        self.assertEqual(bootouts, [])
        self.assertGreaterEqual(clock.now - 1000.0, 2.0)
        self.assertLess(clock.now - 1000.0, 3.0)

    def test_escalates_to_kill_after_fifteen_seconds(self):
        code, clock, killed, _ = self._run(alive_until_quit_s=None)
        self.assertEqual(code, 0)
        self.assertEqual(killed, [4242])
        self.assertGreaterEqual(clock.now - 1000.0, si.GRACEFUL_STOP_TIMEOUT_S)
        self.assertLess(clock.now - 1000.0, si.GRACEFUL_STOP_TIMEOUT_S + 1.0)

    def test_unreachable_channel_kills_immediately(self):
        code, clock, killed, _ = self._run(alive_until_quit_s=None, quit_ok=False)
        self.assertEqual(code, 0)
        self.assertEqual(killed, [4242])
        self.assertLess(clock.now - 1000.0, 1.0)

    def test_boots_out_loaded_agent_before_kill(self):
        _, _, killed, bootouts = self._run(alive_until_quit_s=None, agent_loaded=True)
        self.assertEqual(bootouts, [1])
        self.assertEqual(killed, [4242])

    def test_survivor_after_kill_returns_nonzero(self):
        code, clock, killed, _ = self._run(alive_until_quit_s=None, dies_on_kill=False)
        self.assertEqual(code, 1)
        self.assertEqual(killed, [4242])
        total = clock.now - 1000.0
        self.assertGreaterEqual(total, si.GRACEFUL_STOP_TIMEOUT_S + si.KILL_WAIT_TIMEOUT_S)

    def test_nothing_running_is_a_noop(self):
        code = si.ctl_stop(
            [self.EXE],
            request_quit_fn=lambda: self.fail("must not ping"),
            list_pids=lambda _p: [],
            kill=lambda _pid: self.fail("must not kill"),
            agent_loaded=lambda: False,
            bootout=lambda: None,
            sleep=lambda _s: None,
            monotonic=lambda: 0.0,
        )
        self.assertEqual(code, 0)


class AssertSingleTests(unittest.TestCase):
    EXE = "/Applications/Mouser.app/Contents/MacOS/Mouser"

    def test_keeps_newest_kills_rest(self):
        clock = FakeClock()
        state = {"rows": [(512, 42), (411, 3600), (300, 9000)]}
        killed: list[int] = []

        def kill(pid):
            killed.append(pid)
            state["rows"] = [r for r in state["rows"] if r[0] != pid]

        code = si.ctl_assert_single(
            self.EXE,
            list_instances_fn=lambda _p: list(state["rows"]),
            kill=kill,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(code, 0)
        self.assertEqual(killed, [411, 300])
        self.assertEqual(state["rows"], [(512, 42)])

    def test_nonzero_when_extras_survive_five_seconds(self):
        clock = FakeClock()
        code = si.ctl_assert_single(
            self.EXE,
            list_instances_fn=lambda _p: [(2, 1), (1, 5)],
            kill=lambda _pid: None,
            sleep=clock.sleep,
            monotonic=clock.monotonic,
        )
        self.assertEqual(code, 1)
        self.assertGreaterEqual(clock.now - 1000.0, si.ASSERT_SINGLE_TIMEOUT_S)


# ─────────────────────────────────────────────────────────────────────────────
# ctl start
# ─────────────────────────────────────────────────────────────────────────────


class StartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.exe = os.path.join(self.tmp.name, "Mouser")
        Path(self.exe).write_text("#!/bin/sh\n")

    def test_macos_kickstart_when_agent_loaded(self):
        calls: list[list[str]] = []

        def launchctl(args):
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(si.sys, "platform", "darwin"), patch("os.getuid", return_value=501, create=True):
            code = si.ctl_start(
                self.exe,
                launchctl=launchctl,
                agent_loaded=lambda: True,
                plist_exists=lambda: True,
                spawn=lambda _p: self.fail("must not spawn"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [["kickstart", "-k", f"gui/501/{si.APP_BUNDLE_ID}"]])

    def test_macos_bootstrap_when_plist_only(self):
        calls: list[list[str]] = []

        def launchctl(args):
            calls.append(args)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(si.sys, "platform", "darwin"), patch("os.getuid", return_value=501, create=True):
            code = si.ctl_start(
                self.exe,
                launchctl=launchctl,
                agent_loaded=lambda: False,
                plist_exists=lambda: True,
                spawn=lambda _p: self.fail("must not spawn"),
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][:2], ["bootstrap", "gui/501"])
        self.assertTrue(calls[0][2].endswith(f"{si.APP_BUNDLE_ID}.plist"))

    def test_macos_direct_spawn_without_agent_never_uses_open(self):
        spawned: list[str] = []
        with patch.object(si.sys, "platform", "darwin"), patch.object(si.subprocess, "run") as run:
            code = si.ctl_start(
                self.exe,
                launchctl=lambda args: self.fail("no launchctl"),
                agent_loaded=lambda: False,
                plist_exists=lambda: False,
                spawn=spawned.append,
            )
        self.assertEqual(code, 0)
        self.assertEqual(spawned, [self.exe])
        run.assert_not_called()

    def test_missing_executable_fails(self):
        self.assertEqual(si.ctl_start(os.path.join(self.tmp.name, "nope")), 1)

    def test_windows_uses_one_shot_interactive_task(self):
        scripts: list[str] = []

        def run_ps(script):
            scripts.append(script)
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(si.sys, "platform", "win32"):
            code = si.ctl_start(
                self.exe,
                run_powershell=run_ps,
                console_user=lambda: "TINY11\\alex",
                spawn=lambda _p: self.fail("must not Start-Process"),
            )
        self.assertEqual(code, 0)
        script = scripts[0]
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("-UserId 'TINY11\\alex'", script)
        self.assertIn("Start-ScheduledTask", script)
        self.assertIn("Unregister-ScheduledTask", script)
        self.assertNotIn("Start-Process", script)

    def test_windows_refuses_without_console_user(self):
        with patch.object(si.sys, "platform", "win32"):
            code = si.ctl_start(
                self.exe,
                run_powershell=lambda s: self.fail("must not run"),
                console_user=lambda: "",
            )
        self.assertEqual(code, 1)


class CtlMainTests(unittest.TestCase):
    def test_usage_error(self):
        self.assertEqual(si.ctl_main([]), 64)
        self.assertEqual(si.ctl_main(["dance"]), 64)
        self.assertEqual(si.ctl_main(["status", "--exe"]), 64)

    def test_dispatch_with_exe_override(self):
        with patch.object(si, "ctl_status", return_value=2) as status:
            self.assertEqual(si.ctl_main(["status", "--exe", "/x/Mouser"]), 2)
        status.assert_called_once_with("/x/Mouser")
        with patch.object(si, "ctl_stop", return_value=0) as stop, patch.object(
            si, "ctl_start", return_value=0
        ) as start:
            self.assertEqual(si.ctl_main(["restart", "--exe=/x/Mouser"]), 0)
        stop.assert_called_once_with(["/x/Mouser"])
        start.assert_called_once_with("/x/Mouser")

    def test_restart_stops_short_when_stop_fails(self):
        with patch.object(si, "ctl_stop", return_value=1), patch.object(si, "ctl_start") as start:
            self.assertEqual(si.ctl_main(["restart", "--exe", "/x"]), 1)
        start.assert_not_called()


class MainQmlDispatchTests(unittest.TestCase):
    def test_ctl_dispatches_before_qt_import(self):
        probe = textwrap.dedent(
            f"""
            import runpy, sys
            sys.argv = ["main_qml.py", "--ctl", "status", "--exe", "/definitely/not/here/Mouser"]
            try:
                runpy.run_path({str(ROOT / "main_qml.py")!r}, run_name="__main__")
            except SystemExit as exc:
                print("exit", exc.code)
            print("qt", "PySide6.QtWidgets" in sys.modules)
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, cwd=ROOT
        )
        self.assertIn("exit 0", result.stdout, result.stderr)
        self.assertIn("qt False", result.stdout)


if __name__ == "__main__":
    unittest.main()
