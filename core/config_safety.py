"""Config-file safety net (ported from the hughesyadaddy fork).

* A config that fails to load is never silently replaced: the unreadable
  file is copied aside (``config.json.unreadable-<stamp>``) and the last
  known-good ``config.json.bak`` is tried before falling back to defaults.
* Every successful save rolls ``config.json.bak`` forward.
* Temp files are created with ``O_EXCL`` instead of ``tempfile.mkstemp``:
  on Windows mkstemp retries PermissionError up to ``os.TMP_MAX`` times, so
  a config dir with a broken ACL spins a core instead of raising.
"""

from __future__ import annotations

import json
import os
import shutil
import time


def create_tmp(target_dir: str, prefix: str = ".config"):
    """Return ``(fd, path)`` for a fresh 0600 temp file in *target_dir*."""
    path = os.path.join(target_dir, f"{prefix}-{os.getpid()}-{os.urandom(4).hex()}.tmp")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    return fd, path


def preserve_unreadable(config_file: str) -> str | None:
    """Copy an unreadable config aside; return the copy's path."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    salvage = f"{config_file}.unreadable-{stamp}"
    try:
        shutil.copy2(config_file, salvage)
    except Exception as exc:
        print(f"[Config] could not preserve unreadable config: {exc}")
        return None
    print(f"[Config] PRESERVED unreadable config at {salvage}")
    return salvage


def backup_path(config_file: str) -> str:
    return f"{config_file}.bak"


def load_backup(config_file: str, finish):
    """Load and ``finish()`` the .bak copy; None when absent or unusable."""
    backup = backup_path(config_file)
    if not os.path.exists(backup):
        print("[Config] no backup available; falling back to DEFAULTS "
              "-- previous settings are in the preserved file above")
        return None
    try:
        with open(backup, "r", encoding="utf-8") as f:
            cfg = finish(json.load(f))
    except Exception as exc:
        print(f"[Config] backup unusable ({exc}); falling back to defaults")
        return None
    print(f"[Config] RECOVERED settings from {backup}")
    return cfg


def roll_backup(config_file: str) -> None:
    try:
        shutil.copy2(config_file, backup_path(config_file))
    except Exception:
        pass
