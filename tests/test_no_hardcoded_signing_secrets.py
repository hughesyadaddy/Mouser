"""Repo-wide guard: code-signing identities must only come from the environment.

These tests scan every git-tracked text file so that a hardcoded Apple Team
ID, a codesigning identity hash, or a baked-in fallback default can never be
reintroduced. Signing configuration belongs in .env.local (gitignored) or the
caller's environment.
"""

import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Built at runtime so this file never contains the literal itself.
_PREVIOUSLY_LEAKED_TEAM_ID = "MVDT65" + "NPA4"

# Shell parameter expansion with a non-empty default, e.g.
# ${MOUSER_TEAM_ID:-ABC123}, ${MOUSER_TEAM_ID-ABC123}, or ${MOUSER_TEAM_ID:=ABC123}.
_SHELL_DEFAULT = re.compile(
    r"\$\{MOUSER_(?:TEAM_ID|SIGN_IDENTITY)(?::?[-=])[^}]+\}"
)

# Python env lookup with a non-empty default, e.g. os.environ.get("MOUSER_TEAM_ID", "ABC"),
# os.getenv("MOUSER_TEAM_ID", "ABC"), or env.get("MOUSER_TEAM_ID", "ABC").
_PYTHON_DEFAULT = re.compile(
    r"""(?:os\.getenv|(?:[A-Za-z_][A-Za-z0-9_]*\.)?environ\.get|[A-Za-z_][A-Za-z0-9_\.]*\.get)\(\s*["']MOUSER_(?:TEAM_ID|SIGN_IDENTITY)["']\s*,\s*["'][^"']+["']"""
)

# A literal SHA-1 codesigning identity (uppercase 40 hex chars).
_IDENTITY_HASH = re.compile(r"\b[A-F0-9]{40}\b")

# An ad-hoc codesign invocation: `--sign -` / `-s -` (a lone dash identity), or
# an explicit MOUSER_SIGN_IDENTITY=- assignment. Ad-hoc signatures change on
# every build and reset macOS TCC grants on every deploy, so no build path may
# fall back to one.
_ADHOC_CODESIGN = re.compile(
    r"""(?:--sign|-s)\s+(?:-|["']-["'])(?:\s|$)|MOUSER_SIGN_IDENTITY["']?\]?\s*=\s*["']?-["']?(?:\s|$)"""
)

# Files that build or install the app; the only places an ad-hoc fallback
# could live.
_BUILD_FILES = (
    "build_macos_app.sh",
    "scripts/build_and_install.py",
    "scripts/build_and_install_macos_app.sh",
    "scripts/build_macos_gui_session.py",
    "scripts/install_from_dist.py",
    "scripts/install_lifecycle.py",
    "scripts/windows_install.py",
)


def _tracked_text_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT, text=True
    )
    files = []
    for rel in output.split("\0"):
        if not rel:
            continue
        path = ROOT / rel
        if path.is_file():
            files.append(path)
    return files


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None  # binary or unreadable: not a place secrets hide as text


class NoHardcodedSigningSecretsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.files = _tracked_text_files()
        except (FileNotFoundError, subprocess.CalledProcessError):
            raise unittest.SkipTest("git not available; cannot enumerate tracked files")

    def _scan(self, predicate, description: str) -> None:
        offenders = []
        for path in self.files:
            if path == Path(__file__).resolve():
                continue
            text = _read_text(path)
            if text is None:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if predicate(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            f"{description} found in tracked files:\n" + "\n".join(offenders),
        )

    def test_no_leaked_team_id_literal(self):
        self._scan(
            lambda line: _PREVIOUSLY_LEAKED_TEAM_ID in line,
            "Hardcoded Apple Team ID",
        )

    def test_no_shell_fallback_defaults_for_signing_vars(self):
        self._scan(
            lambda line: _SHELL_DEFAULT.search(line),
            "Shell fallback default for MOUSER_TEAM_ID/MOUSER_SIGN_IDENTITY",
        )

    def test_no_python_fallback_defaults_for_signing_vars(self):
        self._scan(
            lambda line: _PYTHON_DEFAULT.search(line),
            "Python fallback default for MOUSER_TEAM_ID/MOUSER_SIGN_IDENTITY",
        )

    def test_shell_guard_covers_common_default_variants(self):
        examples = (
            "${MOUSER_TEAM_ID:-ABC123}",
            "${MOUSER_TEAM_ID-ABC123}",
            "${MOUSER_SIGN_IDENTITY:=ABC123}",
        )
        for example in examples:
            with self.subTest(example=example):
                self.assertIsNotNone(_SHELL_DEFAULT.search(example))

    def test_python_guard_covers_common_default_variants(self):
        examples = (
            'os.environ.get("MOUSER_TEAM_ID", "ABC123")',
            'os.getenv("MOUSER_SIGN_IDENTITY", "ABC123")',
            'env.get("MOUSER_TEAM_ID", "ABC123")',
        )
        for example in examples:
            with self.subTest(example=example):
                self.assertIsNotNone(_PYTHON_DEFAULT.search(example))

    def test_no_literal_codesigning_identity_hashes(self):
        self._scan(
            lambda line: _IDENTITY_HASH.search(line),
            "Literal codesigning identity hash (uppercase 40-hex)",
        )

    def test_no_adhoc_codesign_in_build_files(self):
        build_files = {ROOT / rel for rel in _BUILD_FILES}
        offenders = []
        for path in self.files:
            if path not in build_files:
                continue
            text = _read_text(path) or ""
            for lineno, line in enumerate(text.splitlines(), start=1):
                if _ADHOC_CODESIGN.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
        self.assertEqual(offenders, [], "Ad-hoc codesign fallback found:\n" + "\n".join(offenders))

    def test_adhoc_guard_covers_common_variants(self):
        positives = (
            'codesign --force --deep --sign - "$APP"',
            "codesign -s - dist/Mouser.app",
            "MOUSER_SIGN_IDENTITY=- ./build_macos_app.sh",
            'env["MOUSER_SIGN_IDENTITY"] = "-"',
        )
        negatives = (
            'codesign --force --sign "$SIGN_IDENTITY" "$APP"',
            "codesign --verify --deep --strict",
            'MOUSER_SIGN_IDENTITY="${MOUSER_SIGN_IDENTITY:-}"',
            "if configured == '-':",
        )
        for example in positives:
            with self.subTest(example=example):
                self.assertIsNotNone(_ADHOC_CODESIGN.search(example))
        for example in negatives:
            with self.subTest(example=example):
                self.assertIsNone(_ADHOC_CODESIGN.search(example))

    def test_build_script_has_no_sign_dash_literal(self):
        # The fleet gate is literally `! grep -q 'sign -' build_macos_app.sh`.
        text = _read_text(ROOT / "build_macos_app.sh") or ""
        self.assertNotIn("sign -", text)

    def test_env_example_documents_sign_identity_and_deprecates_team_id(self):
        lines = (_read_text(ROOT / ".env.local.example") or "").splitlines()
        active = [line for line in lines if line.startswith("MOUSER_")]
        self.assertEqual(
            active,
            ["MOUSER_SIGN_IDENTITY="],
            "MOUSER_SIGN_IDENTITY must be the only uncommented MOUSER_* key",
        )
        team_lines = [line for line in lines if "MOUSER_TEAM_ID" in line]
        self.assertTrue(team_lines, "MOUSER_TEAM_ID should be documented as a deprecated alias")
        for line in team_lines:
            self.assertTrue(line.startswith("#"), f"MOUSER_TEAM_ID must stay commented out: {line}")
        self.assertTrue(
            any("DEPRECATED" in line.upper() for line in lines),
            ".env.local.example must mark MOUSER_TEAM_ID as deprecated",
        )

    def test_env_local_is_gitignored_and_example_is_not(self):
        for name in (".env.local", ".env"):
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", name],
                cwd=ROOT,
            )
            self.assertEqual(ignored.returncode, 0, f"{name} must be gitignored")

        example = subprocess.run(
            ["git", "check-ignore", "-q", ".env.local.example"],
            cwd=ROOT,
        )
        self.assertNotEqual(
            example.returncode, 0, ".env.local.example must be tracked, not ignored"
        )


if __name__ == "__main__":
    sys.exit(unittest.main())
