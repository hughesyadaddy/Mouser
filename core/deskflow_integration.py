"""Deskflow auto-detection for transparent KVM integration (Tier 1.5).

Reads Deskflow's ``mouser-sink.json`` manifest (preferred) or ``deskflow.conf``
so Mouser can start the loopback sink without manual ``remote_device`` /
``remote_forward`` configuration.
"""

from __future__ import annotations

import configparser
import json
import os
import sys
from typing import Any

DEFAULT_CLIENT_PORT = 19795
DEFAULT_BRIDGE_PORT = 19796


def _deskflow_config_dir() -> str:
    if sys.platform == "darwin":
        home = os.path.expanduser("~")
        candidates = (
            os.path.join(home, "Library", "Deskflow"),
            os.path.join(home, "Library", "Application Support", "Deskflow"),
        )
        for candidate in candidates:
            if os.path.isdir(candidate):
                return candidate
        return candidates[0]
    if sys.platform.startswith("linux"):
        return os.path.join(
            os.environ.get(
                "XDG_CONFIG_HOME",
                os.path.join(os.path.expanduser("~"), ".config"),
            ),
            "Deskflow",
        )
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Deskflow")


def _read_manifest() -> dict[str, Any] | None:
    path = os.path.join(_deskflow_config_dir(), "mouser-sink.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _read_deskflow_conf() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    config_dir = _deskflow_config_dir()
    for name in ("Deskflow.conf", "deskflow.conf"):
        path = os.path.join(config_dir, name)
        if os.path.isfile(path):
            parser.read(path, encoding="utf-8")
            break
    return parser


def _conf_bool(parser: configparser.ConfigParser, section: str, key: str) -> bool:
    slash_key = f"{section}/{key}"
    for candidate in ((section, key), ("General", slash_key), ("General", key)):
        sec, opt = candidate
        if parser.has_option(sec, opt):
            return parser.get(sec, opt).strip().lower() in ("1", "true", "yes", "on")
    return False


def _conf_str(parser: configparser.ConfigParser, section: str, key: str) -> str:
    slash_key = f"{section}/{key}"
    for candidate in ((section, key), ("General", slash_key), ("General", key)):
        sec, opt = candidate
        if parser.has_option(sec, opt):
            return parser.get(sec, opt).strip()
    return ""


def _conf_int(parser: configparser.ConfigParser, section: str, key: str, default: int) -> int:
    raw = _conf_str(parser, section, key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def resolve_integration(user_cfg: dict | None = None) -> dict[str, Any] | None:
    """Return Deskflow integration hints for this machine, or None."""
    settings = (user_cfg or {}).get("settings", {}) or {}
    deskflow_cfg = settings.get("deskflow", {}) or {}
    if deskflow_cfg.get("auto") is False:
        return None

    manifest = _read_manifest()
    conf = _read_deskflow_conf()

    out: dict[str, Any] = {"source": "none"}

    # Client sink (remote / focused machine)
    token = ""
    port = DEFAULT_CLIENT_PORT
    if manifest:
        token = str(manifest.get("token") or "")
        try:
            port = int(manifest.get("port", DEFAULT_CLIENT_PORT))
        except (TypeError, ValueError):
            port = DEFAULT_CLIENT_PORT
        out["source"] = "manifest"
        out["client_sink"] = True
    else:
        for section in ("client", "General"):
            if _conf_bool(conf, section, "mouserEnabled"):
                token = _conf_str(conf, section, "mouserToken") or _conf_str(
                    conf, "client", "mouserToken"
                )
                port = _conf_int(conf, section, "mouserPort", DEFAULT_CLIENT_PORT)
                if token:
                    out["source"] = "deskflow.conf"
                    out["client_sink"] = True
                break

    if token:
        out["token"] = token
        out["port"] = port

    # Host bridge (machine with physical mouse)
    bridge_token = ""
    bridge_port = DEFAULT_BRIDGE_PORT
    for section in ("server", "General"):
        if _conf_bool(conf, section, "mouserBridgeEnabled"):
            bridge_token = _conf_str(conf, section, "mouserBridgeToken")
            bridge_port = _conf_int(conf, section, "mouserBridgePort", DEFAULT_BRIDGE_PORT)
            if bridge_token:
                out["host_bridge"] = True
                out["bridge_token"] = bridge_token
                out["bridge_port"] = bridge_port
                if out["source"] == "none":
                    out["source"] = "deskflow.conf"
            break

    if out.get("client_sink") or out.get("host_bridge"):
        return out
    return None


def use_transparent_transport(user_cfg: dict | None = None) -> bool:
    """True when virtual devices should present as local (Tier 1.5 UI)."""
    settings = (user_cfg or {}).get("settings", {}) or {}
    deskflow_cfg = settings.get("deskflow", {}) or {}
    if deskflow_cfg.get("transparent_transport") is False:
        return False
    return resolve_integration(user_cfg) is not None
