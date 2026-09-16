"""The macOS build must never sign ad-hoc: no identity means no build.

An ad-hoc signature changes on every build, so macOS resets the app's
Accessibility / Input Monitoring grants on every deploy. These tests pin the
hard-fail contract of ``scripts/build_and_install.py`` and its ``--dry-run``
plan output.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import build_and_install as installer

# Built at runtime: the repo-wide secrets guard rejects any literal 40-hex
# identity in tracked files, including test fixtures.
FAKE_HASH = "AB12" * 10
OTHER_HASH = "CD34" * 10
FAKE_TEAM = "TEAM" + "123456"


def _env(**values: str):
    """Isolated environment: nothing from the developer's shell or .env.local."""
    return mock.patch.dict(os.environ, values, clear=True)


def _isolated_root():
    """A ROOT without .env.local so the real machine's identity cannot leak in."""
    tmp = tempfile.TemporaryDirectory()
    return tmp, mock.patch.object(installer, "ROOT", Path(tmp.name))


class ResolveIdentityTests(unittest.TestCase):
    def _resolve_exit_code(self) -> int:
        with self.assertRaises(SystemExit) as ctx, redirect_stderr(io.StringIO()):
            installer.resolve_macos_sign_identity()
        return ctx.exception.code

    def test_dash_is_rejected_as_adhoc(self):
        with _env(MOUSER_SIGN_IDENTITY="-"):
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_dash_is_rejected_even_when_team_id_is_also_set(self):
        # `-` must not silently fall through to the TEAM_ID lookup.
        with _env(MOUSER_SIGN_IDENTITY="-", MOUSER_TEAM_ID=FAKE_TEAM), \
             mock.patch.object(installer, "_find_identity_for_team", return_value=FAKE_HASH):
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_empty_is_rejected(self):
        with _env(MOUSER_SIGN_IDENTITY=""):
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_whitespace_is_rejected(self):
        with _env(MOUSER_SIGN_IDENTITY="   "):
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_unset_is_rejected(self):
        with _env():
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_non_hash_identity_is_rejected(self):
        # Certificate common names drift across renewals; only the SHA-1 is stable.
        with _env(MOUSER_SIGN_IDENTITY="Apple Development: Someone"):
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_short_hash_is_rejected(self):
        with _env(MOUSER_SIGN_IDENTITY=FAKE_HASH[:-1]):
            self.assertEqual(self._resolve_exit_code(), installer.EXIT_NO_SIGN_IDENTITY)

    def test_valid_hash_is_returned(self):
        with _env(MOUSER_SIGN_IDENTITY=FAKE_HASH):
            self.assertEqual(installer.resolve_macos_sign_identity(), FAKE_HASH)

    def test_lowercase_hash_is_normalized(self):
        with _env(MOUSER_SIGN_IDENTITY=FAKE_HASH.lower()):
            self.assertEqual(installer.resolve_macos_sign_identity(), FAKE_HASH)

    def test_explicit_identity_wins_over_team_id_without_keychain_lookup(self):
        with _env(MOUSER_SIGN_IDENTITY=FAKE_HASH, MOUSER_TEAM_ID=FAKE_TEAM), \
             mock.patch.object(installer, "_find_identity_for_team") as lookup:
            self.assertEqual(installer.resolve_macos_sign_identity(), FAKE_HASH)
            lookup.assert_not_called()


class TeamIdResolutionTests(unittest.TestCase):
    """MOUSER_TEAM_ID stays as a deprecated convenience; it must yield a hash."""

    def _security_output(self, *lines: str) -> str:
        return "\n".join(lines) + "\n"

    def test_team_id_resolves_to_matching_keychain_hash(self):
        listing = self._security_output(
            f'  1) {OTHER_HASH} "Apple Development: Other (ZZ' + '9999999Z)"',
            f'  2) {FAKE_HASH} "Apple Development: Someone ({FAKE_TEAM})"',
            "     2 valid identities found",
        )
        with _env(MOUSER_TEAM_ID=FAKE_TEAM), \
             mock.patch.object(installer.subprocess, "check_output", return_value=listing), \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(installer.resolve_macos_sign_identity(), FAKE_HASH)
        self.assertIn("deprecated", err.getvalue())

    def test_team_id_with_no_matching_identity_fails(self):
        listing = self._security_output(
            f'  1) {OTHER_HASH} "Apple Development: Other (ZZ' + '9999999Z)"',
        )
        with _env(MOUSER_TEAM_ID=FAKE_TEAM), \
             mock.patch.object(installer.subprocess, "check_output", return_value=listing), \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                installer.resolve_macos_sign_identity()
        self.assertEqual(ctx.exception.code, installer.EXIT_NO_SIGN_IDENTITY)

    def test_team_id_when_security_is_unavailable_fails(self):
        with _env(MOUSER_TEAM_ID=FAKE_TEAM), \
             mock.patch.object(installer.subprocess, "check_output", side_effect=FileNotFoundError), \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                installer.resolve_macos_sign_identity()
        self.assertEqual(ctx.exception.code, installer.EXIT_NO_SIGN_IDENTITY)


class DryRunTests(unittest.TestCase):
    def _run_dry(self, **env: str) -> tuple[int | None, str]:
        tmp, root_patch = _isolated_root()
        out = io.StringIO()
        try:
            with root_patch, _env(**env), \
                 mock.patch.object(installer.sys, "platform", "darwin"), \
                 mock.patch.object(installer, "stop_running_instances") as stop, \
                 mock.patch.object(installer, "run_command") as run, \
                 mock.patch.object(installer, "gui_session_routing_would_be_used", return_value=True), \
                 mock.patch.object(installer, "app_version", return_value="9.9.9"), \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                code: int | None = 0
                try:
                    installer.main(["--dry-run"])
                except SystemExit as exc:
                    code = exc.code
                stop.assert_not_called()
                run.assert_not_called()
        finally:
            tmp.cleanup()
        return code, out.getvalue()

    def test_valid_hash_prints_plan_and_exits_zero(self):
        code, out = self._run_dry(MOUSER_SIGN_IDENTITY=FAKE_HASH, MOUSER_INSTALL_DIR="/tmp/apps")
        self.assertEqual(code, 0, out)
        self.assertIn(FAKE_HASH, out)
        self.assertIn("9.9.9", out)
        self.assertIn(str(Path("/tmp/apps") / installer.MACOS_APP_NAME), out)
        self.assertIn("gui session: yes", out)

    def test_dash_exits_nonzero(self):
        code, _ = self._run_dry(MOUSER_SIGN_IDENTITY="-")
        self.assertEqual(code, installer.EXIT_NO_SIGN_IDENTITY)

    def test_empty_exits_nonzero(self):
        code, _ = self._run_dry(MOUSER_SIGN_IDENTITY="")
        self.assertEqual(code, installer.EXIT_NO_SIGN_IDENTITY)

    def test_team_id_resolution_feeds_the_plan(self):
        tmp, root_patch = _isolated_root()
        out = io.StringIO()
        try:
            with root_patch, _env(MOUSER_TEAM_ID=FAKE_TEAM), \
                 mock.patch.object(installer.sys, "platform", "darwin"), \
                 mock.patch.object(installer, "_find_identity_for_team", return_value=FAKE_HASH), \
                 mock.patch.object(installer, "gui_session_routing_would_be_used", return_value=False), \
                 mock.patch.object(installer, "app_version", return_value="9.9.9"), \
                 mock.patch.object(installer, "stop_running_instances") as stop, \
                 redirect_stdout(out), redirect_stderr(io.StringIO()):
                installer.main(["--dry-run"])
                stop.assert_not_called()
        finally:
            tmp.cleanup()
        self.assertIn(FAKE_HASH, out.getvalue())
        self.assertIn("gui session: no", out.getvalue())


class BuildOrderTests(unittest.TestCase):
    def test_identity_is_resolved_before_the_running_app_is_stopped(self):
        # A seat without an identity must not lose its running Mouser.
        with _env(MOUSER_SIGN_IDENTITY="-"), \
             mock.patch.object(installer, "stop_running_instances") as stop, \
             mock.patch.object(installer, "run_command") as run, \
             redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                installer.build_and_install_macos()
        stop.assert_not_called()
        run.assert_not_called()


@unittest.skipUnless(sys.platform == "darwin", "end-to-end dry run drives the macOS path")
class SubprocessDryRunTests(unittest.TestCase):
    """The plan's acceptance command, run for real."""

    def test_dash_identity_exits_nonzero_from_the_command_line(self):
        env = os.environ.copy()
        env["MOUSER_SIGN_IDENTITY"] = "-"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_and_install.py"), "--dry-run"],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, installer.EXIT_NO_SIGN_IDENTITY, result.stderr)
        self.assertIn("ad-hoc", result.stderr)


if __name__ == "__main__":
    unittest.main()
