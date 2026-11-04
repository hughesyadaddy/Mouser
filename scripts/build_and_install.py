#!/usr/bin/env python3
"""Build Mouser and install it to the platform default location."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.install_lifecycle import (
    launch_installed_application,
    restart_enabled,
    stop_running_instances,
    sync_login_startup_after_install,
    windows_sign_script,
)

MACOS_APP_NAME = "Mouser.app"
WINDOWS_APP_DIR = "Mouser"
DEFAULT_MACOS_INSTALL_DIR = Path("/Applications")

#: Exit status for a missing, empty, ad-hoc, or malformed signing identity.
#: Shared with build_macos_app.sh so callers can tell "no identity" apart from
#: a build failure.
EXIT_NO_SIGN_IDENTITY = 2

#: A codesigning identity as ``security find-identity`` prints it: the SHA-1
#: of the certificate, 40 uppercase hex digits. This is the only form the
#: build accepts. Names ("Apple Development: ...") are ambiguous across
#: renewals and ``-`` is ad-hoc, which resets TCC grants on every deploy.
_IDENTITY_HASH = re.compile(r"^[A-F0-9]{40}$")

_IDENTITY_HELP = (
    "Set MOUSER_SIGN_IDENTITY in .env.local to the SHA-1 of your codesigning "
    "identity (list with: security find-identity -v -p codesigning)."
)


def fail(message: str, *, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def load_env_local() -> None:
    env_file = ROOT / ".env.local"
    if not env_file.is_file():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def resolve_command(candidate: str) -> Path | None:
    path = Path(candidate)
    if any(sep in candidate for sep in ("/", "\\", os.sep)) or path.suffix:
        if not path.is_file():
            return None
        if sys.platform == "win32":
            return path
        return path if os.access(path, os.X_OK) else None
    resolved = shutil.which(candidate)
    return Path(resolved) if resolved else None


def python_from_env_dir(env_dir: Path) -> Path | None:
    for name in ("python3", "python"):
        candidate = env_dir / "Scripts" / f"{name}.exe"
        if candidate.is_file():
            return candidate
        candidate = env_dir / "bin" / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_python() -> tuple[Path, str]:
    override = os.environ.get("MOUSER_PYTHON")
    if override:
        resolved = resolve_command(override)
        if resolved is None:
            fail(f"MOUSER_PYTHON is set but is not executable: {override}")
        return resolved, "MOUSER_PYTHON"

    virtual_env = os.environ.get("VIRTUAL_ENV")
    if virtual_env:
        resolved = python_from_env_dir(Path(virtual_env))
        if resolved is None:
            fail(f"VIRTUAL_ENV is set but no executable Python was found in {virtual_env}")
        return resolved, "VIRTUAL_ENV"

    repo_venv = ROOT / ".venv"
    if repo_venv.is_dir():
        resolved = python_from_env_dir(repo_venv)
        if resolved is None:
            fail(
                "Repository .venv exists but no executable Python was found in "
                f"{repo_venv / ('Scripts' if sys.platform == 'win32' else 'bin')}"
            )
        return resolved, "repo .venv"

    for name in ("python3", "python"):
        resolved = resolve_command(name)
        if resolved is not None:
            return resolved, f"PATH {name}"

    fail("No Python interpreter found. Create .venv or set MOUSER_PYTHON.")


def run_command(
    args: list[str | Path],
    *,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    display = " ".join(str(arg) for arg in args)
    print(f"+ {display}")
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=ROOT,
        env=env,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        fail(f"Command failed ({result.returncode}): {display}")
    return result


def require_pyinstaller(python: Path, source: str) -> None:
    probe = subprocess.run(
        [str(python), "-c", "import PyInstaller"],
        cwd=ROOT,
        text=True,
        check=False,
    )
    if probe.returncode == 0:
        return
    fail(
        f"PyInstaller not installed in {python} (source: {source}). "
        f"Install it with: {python} -m pip install -r {ROOT / 'requirements.txt'}"
    )


#: Packages whose installed version must match requirements.lock exactly.
#: pyobjc-core carries the CGEventTap trampoline whose leak the hook's
#: deferred-release guard is calibrated against (refcount == 3); PySide6 is
#: the other large native ABI the packaged app depends on.
PROVENANCE_PACKAGES_ALL = ("PySide6",)
PROVENANCE_PACKAGES_DARWIN = ("pyobjc-core",)

_PROVENANCE_PROBE = """\
import json, platform, sys
from importlib import metadata
info = {
    "python_version": platform.python_version(),
    "minor": list(sys.version_info[:2]),
    "machine": platform.machine() or "unknown",
    "packages": {},
}
try:
    import PyInstaller
    info["pyinstaller"] = PyInstaller.__version__
except Exception as exc:
    info["pyinstaller"] = f"unavailable ({exc})"
for name in sys.argv[1:]:
    try:
        info["packages"][name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        info["packages"][name] = None
print(json.dumps(info))
"""


def required_python_minor(root: Path | None = None) -> tuple[int, int]:
    """``(major, minor)`` from ``.python-version`` (e.g. ``3.13``)."""
    text = ((root or ROOT) / ".python-version").read_text(encoding="utf-8").strip()
    match = re.match(r"^(\d+)\.(\d+)", text)
    if not match:
        fail(f".python-version is malformed: {text!r}")
    return int(match.group(1)), int(match.group(2))


def locked_versions(root: Path | None = None) -> dict[str, str]:
    """``{normalized_name: version}`` from ``requirements.lock``."""
    versions: dict[str, str] = {}
    for raw in ((root or ROOT) / "requirements.lock").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, version = line.partition("==")
        if sep:
            versions[_normalize_package_name(name)] = version.strip()
    return versions


def _normalize_package_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def provenance_skip_allowed() -> bool:
    """``MOUSER_SKIP_PROVENANCE=1`` is honoured only off macOS (CI/Linux)."""
    return os.environ.get("MOUSER_SKIP_PROVENANCE") == "1" and sys.platform != "darwin"


def verify_python_provenance(python: Path, source: str) -> None:
    """Log the interpreter and refuse to build from the wrong one.

    Fails (nonzero exit) when the interpreter's minor version differs from
    ``.python-version`` or when an installed pyobjc-core (macOS) / PySide6
    version differs from ``requirements.lock``. A seat whose .venv drifted
    (hackintosh was 3.12) must be rebuilt, not packaged.
    """
    if os.environ.get("MOUSER_SKIP_PROVENANCE") == "1" and sys.platform == "darwin":
        print("MOUSER_SKIP_PROVENANCE is ignored on macOS; verifying anyway")
    if provenance_skip_allowed():
        print(f"Using Python: {python} (source: {source}); provenance check skipped "
              "(MOUSER_SKIP_PROVENANCE=1)")
        return
    packages = list(PROVENANCE_PACKAGES_ALL)
    if sys.platform == "darwin":
        packages += list(PROVENANCE_PACKAGES_DARWIN)
    raw = subprocess.check_output(
        [str(python), "-c", _PROVENANCE_PROBE, *packages],
        cwd=ROOT,
        text=True,
    ).strip()
    info = json.loads(raw.splitlines()[-1])
    print(f"Using Python: {python} (source: {source})")
    print(f"Python version: {info['python_version']} ({info['machine']})")
    print(f"PyInstaller version: {info['pyinstaller']}")
    for name in packages:
        print(f"{name} version: {info['packages'].get(name)}")

    problems: list[str] = []
    required = required_python_minor()
    if tuple(info["minor"]) != required:
        problems.append(
            f"Python {info['python_version']} but .python-version requires "
            f"{required[0]}.{required[1]}.x"
        )
    locked = locked_versions()
    for name in packages:
        want = locked.get(_normalize_package_name(name))
        have = info["packages"].get(name)
        if want is None:
            problems.append(f"{name} is not pinned in requirements.lock")
        elif have != want:
            problems.append(f"{name} {have or 'not installed'} but requirements.lock pins {want}")
    if problems:
        want = f"{required[0]}.{required[1]}"
        fail(
            "Python provenance mismatch for "
            f"{python} (source: {source}):\n  - "
            + "\n  - ".join(problems)
            + f"\nRebuild the environment with a Python {want} interpreter, e.g.\n"
            f"  python{want} -m venv {ROOT / '.venv'} && "
            f"{ROOT / '.venv' / 'bin' / 'python'} -m pip install -r "
            f"{ROOT / 'requirements.lock'}"
        )


def resolve_install_dir(default: Path | None = None) -> Path:
    override = os.environ.get("MOUSER_INSTALL_DIR")
    if override:
        return Path(override).expanduser()
    if default is not None:
        return default
    if sys.platform == "win32":
        from scripts.windows_install import default_install_root

        return default_install_root()
    fail("No install directory configured for this platform.")


def _find_identity_for_team(team_id: str) -> str | None:
    """First codesigning identity hash in the keychain for *team_id*, if any."""
    try:
        output = subprocess.check_output(
            ["security", "find-identity", "-v", "-p", "codesigning"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None

    for line in output.splitlines():
        if f"({team_id})" not in line:
            continue
        match = re.search(r"\b([A-F0-9]{40})\b", line)
        return match.group(1) if match else None
    return None


def resolve_macos_sign_identity() -> str:
    """The codesigning identity hash the macOS build must use.

    ``MOUSER_SIGN_IDENTITY`` is the only supported source. ``MOUSER_TEAM_ID``
    is honored as a deprecated convenience: it is translated to the first
    matching keychain identity, and the caller is told to pin that hash.
    There is no ad-hoc fallback: an empty identity or ``-`` is a hard error
    (exit ``EXIT_NO_SIGN_IDENTITY``) before anything is built or stopped.
    """
    configured = os.environ.get("MOUSER_SIGN_IDENTITY", "").strip()
    if configured == "-":
        fail(
            "MOUSER_SIGN_IDENTITY is '-' (ad-hoc signing), which is not supported: "
            "an ad-hoc signature changes on every build and resets macOS "
            "Accessibility / Input Monitoring grants on every deploy. "
            + _IDENTITY_HELP,
            code=EXIT_NO_SIGN_IDENTITY,
        )

    if configured:
        identity = configured.upper()
        if not _IDENTITY_HASH.match(identity):
            fail(
                f"MOUSER_SIGN_IDENTITY={configured!r} is not a codesigning identity "
                "SHA-1 (40 hex digits). " + _IDENTITY_HELP,
                code=EXIT_NO_SIGN_IDENTITY,
            )
        return identity

    team_id = os.environ.get("MOUSER_TEAM_ID", "").strip()
    if not team_id:
        fail(
            "MOUSER_SIGN_IDENTITY is unset or empty and there is no ad-hoc "
            "fallback. " + _IDENTITY_HELP,
            code=EXIT_NO_SIGN_IDENTITY,
        )

    print(
        "warning: MOUSER_TEAM_ID is deprecated; resolving it to a keychain "
        "identity. Pin the hash as MOUSER_SIGN_IDENTITY in .env.local instead.",
        file=sys.stderr,
    )
    identity = _find_identity_for_team(team_id)
    if identity is None or not _IDENTITY_HASH.match(identity):
        fail(
            f"No codesigning identity found in the keychain for team {team_id}. "
            + _IDENTITY_HELP,
            code=EXIT_NO_SIGN_IDENTITY,
        )
    return identity


# --- macOS bundle signature walk ---------------------------------------------
# Every Mach-O in the BUILT bundle (dist/Mouser.app, never the installed copy)
# must carry a real signature from the fleet team, none may be ad-hoc, and
# the first-party binaries (Contents/MacOS/*) must run under the hardened
# runtime (build_macos_app.sh signs with --options runtime and the
# entitlements in build_resources/Mouser.entitlements, which grant PyInstaller
# the allow-unsigned-executable-memory / allow-jit it needs). The same walk
# lives in deskflow's scripts/install-macos.sh and tools/fleet-health.
DEFAULT_EXPECT_TEAM = "J5KPG8ZR5C"
_CD_FLAGS = re.compile(r"^CodeDirectory .*flags=0x[0-9a-fA-F]+\(([^)]*)\)", re.M)


def expected_team_id() -> str:
    return os.environ.get("MOUSER_EXPECT_TEAM", "").strip() or DEFAULT_EXPECT_TEAM


_MACHO_MAGICS = {
    b"\xfe\xed\xfa\xce", b"\xce\xfa\xed\xfe",  # MH_MAGIC / swapped
    b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 / swapped
    b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca",  # FAT
}


def is_macho(path: Path) -> bool:
    """True when the file starts with a Mach-O / fat magic (scripts are not)."""
    try:
        with path.open("rb") as fh:
            return fh.read(4) in _MACHO_MAGICS
    except OSError:
        return False


def bundle_machos(app: Path) -> list[Path]:
    """Every Mach-O in the bundle: Contents/MacOS/*, the dylibs/executables
    under Contents/Frameworks (framework Resources/Headers skipped), and every
    dylib or executable Mach-O under Contents/PlugIns (Qt platform, imageformats,
    tls plugins) and Contents/Resources. Same enumeration as deskflow's
    tools/fleet-health and scripts/install-macos.sh."""
    found: list[Path] = []
    macos = app / "Contents" / "MacOS"
    if macos.is_dir():
        found += [p for p in macos.rglob("*") if p.is_file()]
    frameworks = app / "Contents" / "Frameworks"
    if frameworks.is_dir():
        for p in frameworks.rglob("*"):
            if not p.is_file():
                continue
            parts = p.relative_to(frameworks).parts
            if "Resources" in parts or "Headers" in parts:
                continue
            if p.suffix == ".dylib" or os.access(p, os.X_OK):
                found.append(p)
    for tree in (app / "Contents" / "PlugIns", app / "Contents" / "Resources"):
        if not tree.is_dir():
            continue
        for p in tree.rglob("*"):
            if not p.is_file():
                continue
            if p.suffix == ".dylib" or (os.access(p, os.X_OK) and is_macho(p)):
                found.append(p)
    return sorted(set(found))


def codesign_info(path: Path) -> str:
    """``codesign -dvvv`` text (it prints to stderr); raises on failure."""
    proc = subprocess.run(
        ["codesign", "-dvvv", str(path)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"codesign -dvvv failed for {path}: {(proc.stderr or proc.stdout).strip()}")
    return proc.stderr + proc.stdout


def is_first_party(app: Path, path: Path) -> bool:
    try:
        return path.relative_to(app).parts[:2] == ("Contents", "MacOS")
    except ValueError:
        return False


def verify_macos_bundle_signatures(app: Path, *, expect_team: str | None = None) -> dict[str, int]:
    """Fail unless every Mach-O is non-adhoc, team-signed and (first-party) hardened.

    Returns the summary counts and prints ``sign: total=N apple=N adhoc=N hardened=N``.
    """
    team = expect_team or expected_team_id()
    machos = bundle_machos(app)
    counts = {"total": 0, "apple": 0, "adhoc": 0, "hardened": 0}
    problems: list[str] = []
    for bin_path in machos:
        counts["total"] += 1
        rel = bin_path.relative_to(app)
        try:
            info = codesign_info(bin_path)
        except RuntimeError as exc:
            problems.append(str(exc))
            continue
        if re.search(r"^Signature=adhoc", info, re.M):
            counts["adhoc"] += 1
            problems.append(f"{rel}: Signature=adhoc (ad-hoc signed)")
            continue
        if not re.search(r"^Authority=", info, re.M):
            problems.append(f"{rel}: no Authority= (unsigned or ad-hoc)")
            continue
        m = re.search(r"^TeamIdentifier=(.*)$", info, re.M)
        actual = m.group(1).strip() if m else ""
        if not re.fullmatch(r"[A-Z0-9]+", actual):
            problems.append(f"{rel}: no TeamIdentifier= (not a Developer certificate)")
            continue
        if actual != team:
            problems.append(f"{rel}: TeamIdentifier={actual} != expected {team} (MOUSER_EXPECT_TEAM)")
            continue
        counts["apple"] += 1
        flags = _CD_FLAGS.search(info)
        hardened = bool(flags and "runtime" in flags.group(1).split(","))
        if hardened:
            counts["hardened"] += 1
        elif is_first_party(app, bin_path):
            problems.append(f"{rel}: not signed with the hardened runtime (CodeDirectory flags lack 'runtime')")
    print("sign: total={total} apple={apple} adhoc={adhoc} hardened={hardened}".format(**counts))
    if counts["total"] == 0:
        fail(f"No Mach-O found under {app}/Contents/MacOS -- not a built bundle")
    if problems:
        fail(
            f"{len(problems)} Mach-O(s) in {app} failed the signature gate:\n  " + "\n  ".join(problems)
        )
    print(f"Signature gate OK: team {team}, {counts['hardened']} hardened")
    return counts


def app_version() -> str:
    from core.version import APP_VERSION

    return str(APP_VERSION)


def gui_session_routing_would_be_used() -> bool:
    from scripts.build_macos_gui_session import should_route_through_gui_session

    return should_route_through_gui_session()


def print_macos_plan(sign_identity: str, install_path: Path) -> None:
    """The ``--dry-run`` report: everything the build would commit to."""
    print("Dry run: nothing will be built, stopped, or installed.")
    print(f"  identity:    {sign_identity}")
    print(f"  version:     {app_version()}")
    print(f"  build:       {ROOT / 'dist' / MACOS_APP_NAME}")
    print(f"  install to:  {install_path}")
    routed = gui_session_routing_would_be_used()
    print(f"  gui session: {'yes (keychain unreachable here)' if routed else 'no'}")


def build_and_install_macos(*, dry_run: bool = False) -> None:
    build_output = ROOT / "dist" / MACOS_APP_NAME
    install_dir = resolve_install_dir(DEFAULT_MACOS_INSTALL_DIR)
    install_path = install_dir / MACOS_APP_NAME

    # Resolve the identity before touching anything: a seat without one must
    # fail without killing the running app or spending minutes in PyInstaller.
    sign_identity = resolve_macos_sign_identity()
    if dry_run:
        print_macos_plan(sign_identity, install_path)
        return

    # Same resolution order as build_macos_app.sh (MOUSER_PYTHON, VIRTUAL_ENV,
    # repo .venv, PATH), so the interpreter verified here is the one that
    # packages the app.
    python, source = resolve_python()
    verify_python_provenance(python, source)

    env = os.environ.copy()
    env["MOUSER_SIGN_IDENTITY"] = sign_identity

    print(f"Building signed macOS app (identity: {sign_identity})")
    run_command(["/bin/zsh", ROOT / "build_macos_app.sh"], env=env)

    if not build_output.is_dir():
        fail(f"Build output not found: {build_output}")

    # Gate the BUILT bundle before the running app is stopped: an ad-hoc,
    # foreign-team or unhardened Mach-O anywhere in dist/ never reaches
    # /Applications (its signature would reset the TCC grants on install).
    if shutil.which("codesign"):
        run_command(["codesign", "--verify", "--deep", "--strict", "--verbose=2", build_output])
        verify_macos_bundle_signatures(build_output)
    else:
        fail("codesign not available; refusing to install an unverified bundle")

    print(f"Installing to {install_path}")
    # Use ditto rather than shutil.copytree: copytree does not preserve the
    # extended attributes / sealed resources that macOS code signatures depend
    # on, which breaks the signature on copy and prevents the app from launching
    # from /Applications ("code signature invalid"). ditto copies the bundle
    # byte-for-byte including signing metadata.
    print("[*] ctl stop: quitting running Mouser instances...")
    stop_running_instances()
    if install_path.exists():
        shutil.rmtree(install_path)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    run_command(["ditto", build_output, install_path])

    if shutil.which("codesign"):
        run_command(
            ["codesign", "--verify", "--deep", "--strict", "--verbose=2", install_path]
        )

    print(f"Installed: {install_path}")
    sync_login_startup_after_install(install_path)
    if restart_enabled():
        launch_installed_application(install_path)


def print_windows_plan(install_path: Path, scope: str) -> None:
    sign_script = windows_sign_script()
    thumbprint_set = bool(os.environ.get("DESKFLOW_SIGN_THUMBPRINT", "").strip())
    print("Dry run: nothing will be built, stopped, or installed.")
    print(f"  version:     {app_version()}")
    print(f"  build:       {ROOT / 'dist' / WINDOWS_APP_DIR}")
    print(f"  install to:  {install_path} ({scope} scope)")
    print(f"  sign script: {sign_script or 'not found (install will FAIL on Windows)'}")
    print(f"  thumbprint:  {'set' if thumbprint_set else 'DESKFLOW_SIGN_THUMBPRINT unset'}")


def build_and_install_windows(*, dry_run: bool = False) -> None:
    from scripts.windows_install import (
        cleanup_all_windows_installs,
        default_install_root,
        finalize_windows_install,
        permission_hint,
        remove_legacy_local_install,
        replace_tree,
        resolve_install_scope,
        sign_windows_dist,
    )

    build_output = ROOT / "dist" / WINDOWS_APP_DIR
    scope = resolve_install_scope()
    install_path = (
        Path(os.environ["MOUSER_INSTALL_DIR"]).expanduser()
        if os.environ.get("MOUSER_INSTALL_DIR")
        else default_install_root(scope)
    )
    if dry_run:
        print_windows_plan(install_path, scope)
        return

    # Verify the interpreter before anything destructive (uninstalling the
    # current app, mutating its environment with pip): a wrong Python must
    # fail with the old install still in place.
    python, source = resolve_python()
    verify_python_provenance(python, source)

    print("[*] Cleaning previous Windows installs...")
    cleanup_all_windows_installs()

    print("[*] Installing requirements...")
    run_command([python, "-m", "pip", "install", "-r", ROOT / "requirements.txt"])

    require_pyinstaller(python, source)

    print("[*] Verifying hidapi import...")
    probe = subprocess.run(
        [str(python), "-c", "import hid; print('[*] hidapi:', hid.__file__)"],
        cwd=ROOT,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        fail(
            "hidapi is not importable. The packaged app would not detect Logitech devices."
        )

    if build_output.exists():
        print(f"[*] Removing previous {build_output}...")
        shutil.rmtree(build_output)

    print("[*] Building with PyInstaller...")
    env = os.environ.copy()
    env.setdefault("PYTHONHASHSEED", "0")
    run_command(
        [python, "-m", "PyInstaller", ROOT / "Mouser.spec", "--noconfirm"],
        env=env,
    )

    exe_path = build_output / "Mouser.exe"
    internal_dir = build_output / "_internal"
    if not exe_path.is_file() or not internal_dir.is_dir():
        fail(f"Build output is incomplete: {build_output}")

    print("[*] Signing build output with the fleet certificate...")
    try:
        sign_windows_dist(build_output)
    except RuntimeError as exc:
        fail(str(exc))

    print(f"Installing to {install_path} ({scope} scope)")
    try:
        replace_tree(build_output, install_path)
    except PermissionError as exc:
        fail(f"{permission_hint(scope)} ({exc})")

    installed_exe = install_path / "Mouser.exe"
    if not installed_exe.is_file():
        fail(f"Install verification failed: {installed_exe}")

    remove_legacy_local_install(active_scope=scope)
    shell = finalize_windows_install(install_path, scope=scope)

    print(f"Installed: {shell['install_root']}")
    print(f"Start Menu: {shell['start_menu_shortcut']}")
    print(f"Uninstall: {shell['uninstall_script']}")
    sync_login_startup_after_install(shell["install_root"])
    if restart_enabled():
        launch_installed_application(shell["install_root"])


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Resolve the signing identity and print the build plan "
            "(identity, version, target path, GUI-session routing) without "
            "building, stopping, or installing anything."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point. *argv* is the command line without the program name;
    ``None`` means no options (callers pass ``sys.argv[1:]`` explicitly)."""
    args = parse_args([] if argv is None else argv)
    load_env_local()

    if sys.platform == "darwin":
        build_and_install_macos(dry_run=args.dry_run)
        return
    if sys.platform == "win32":
        build_and_install_windows(dry_run=args.dry_run)
        return

    fail(
        "Unsupported platform for build-and-install. "
        "Use build_macos_app.sh, build.bat, or Mouser-linux.spec on this system."
    )


if __name__ == "__main__":
    main(sys.argv[1:])
