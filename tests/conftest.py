"""Structural safety net for the whole test suite.

2026-09-16/17 incidents: a deskflow bats test overwrote the real, installed
/Applications/Deskflow.app because a sandboxed path override got silently
clobbered by a real .env file being sourced, and
test_build_and_install_macos_stops_before_install (this repo) would have
shutil.rmtree()'d the real /Applications/Mouser.app outright, since it never
set MOUSER_INSTALL_DIR at all -- the default in scripts/build_and_install.py
IS /Applications.

Individual tests should still mock/override explicitly (that's the primary,
readable defense), but a forgotten override must not be able to reach a real
system path. This conftest makes that structural rather than a matter of
each test remembering correctly:

1. Every test gets a safe MOUSER_INSTALL_DIR default pointed at its own
   tmp_path, so any test that never sets it explicitly still can't resolve
   to /Applications.
2. A session-scoped check confirms the real /Applications/Mouser.app (if
   present on this machine) was not modified by the test run, as a second,
   independent line of defense that doesn't rely on every destructive call
   in the codebase going through MOUSER_INSTALL_DIR.
"""

import os
from pathlib import Path

import pytest

_REAL_MOUSER_APP = Path("/Applications/Mouser.app")


@pytest.fixture(autouse=True)
def _default_install_dir_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("MOUSER_INSTALL_DIR", str(tmp_path / "Applications"))


def _real_app_fingerprint():
    if not _REAL_MOUSER_APP.exists():
        return None
    exe = _REAL_MOUSER_APP / "Contents" / "MacOS" / "Mouser"
    try:
        st = (exe if exe.exists() else _REAL_MOUSER_APP).stat()
        return (st.st_mtime_ns, st.st_size if exe.exists() else None)
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _real_mouser_app_must_survive_the_suite():
    before = _real_app_fingerprint()
    yield
    after = _real_app_fingerprint()
    assert before == after, (
        f"The real {_REAL_MOUSER_APP} changed during the test run "
        f"({before!r} -> {after!r}). A test just wrote to or deleted the "
        "real installed app -- this is exactly the incident class this "
        "conftest exists to catch. Find the test that ran without a "
        "MOUSER_INSTALL_DIR/ROOT override reaching real filesystem calls."
    )
