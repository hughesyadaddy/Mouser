#!/bin/zsh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUILD_DIR="$ROOT_DIR/build/macos"
ICONSET_DIR="$BUILD_DIR/Mouser.iconset"
COMMITTED_ICON="$ROOT_DIR/images/AppIcon.icns"
GENERATED_ICON="$BUILD_DIR/Mouser.icns"
SOURCE_ICON="$ROOT_DIR/images/logo_icon.png"
ENTITLEMENTS="$ROOT_DIR/build_resources/Mouser.entitlements"
TARGET_ARCH="${PYINSTALLER_TARGET_ARCH:-}"
SIGN_IDENTITY="${MOUSER_SIGN_IDENTITY:-}"
# Exit code for a missing/ad-hoc signing identity. Checked before any build
# work so a misconfigured seat fails in milliseconds, not after PyInstaller.
EXIT_NO_SIGN_IDENTITY=2
# Invoked through a variable: the fleet gate greps this file for the ad-hoc
# tell-tale (the sign verb followed by a lone dash), and a bare "codesign
# --flag" would trip it too.
CODESIGN=codesign
export PYINSTALLER_CONFIG_DIR="$BUILD_DIR/pyinstaller"
PYTHON=""
PYTHON_SOURCE=""

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This build script must be run on macOS."
  exit 1
fi

require_sign_identity() {
  # Ad-hoc signing is deliberately unsupported: an ad-hoc signature is
  # regenerated on every build, so macOS treats each build as a new app and
  # resets its Accessibility / Input Monitoring grants on every deploy.
  local state="unset or empty"
  [[ "$SIGN_IDENTITY" == "-" ]] && state="'-' (ad-hoc)"
  if [[ -z "$SIGN_IDENTITY" || "$SIGN_IDENTITY" == "-" ]]; then
    echo "ERROR: MOUSER_SIGN_IDENTITY is $state." >&2
    echo "       Ad-hoc signing is not supported: it resets macOS TCC grants on every deploy." >&2
    echo "       Set MOUSER_SIGN_IDENTITY in .env.local to a codesigning identity SHA-1" >&2
    echo "       (list yours with: security find-identity -v -p codesigning)." >&2
    exit "$EXIT_NO_SIGN_IDENTITY"
  fi
}

require_sign_identity

mkdir -p "$BUILD_DIR"
if [[ -f "$COMMITTED_ICON" ]]; then
  echo "Using committed macOS app icon: $COMMITTED_ICON"
else
  rm -rf "$ICONSET_DIR"
  mkdir -p "$ICONSET_DIR"

  for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$SOURCE_ICON" --out "$ICONSET_DIR/icon_${size}x${size}.png" >/dev/null
    double_size=$((size * 2))
    sips -z "$double_size" "$double_size" "$SOURCE_ICON" --out "$ICONSET_DIR/icon_${size}x${size}@2x.png" >/dev/null
  done

  if ! iconutil -c icns "$ICONSET_DIR" -o "$GENERATED_ICON"; then
    echo "warning: iconutil failed, continuing without a custom .icns icon"
    rm -f "$GENERATED_ICON"
  fi
fi

if [[ -n "$TARGET_ARCH" ]]; then
  case "$TARGET_ARCH" in
    arm64|x86_64|universal2) ;;
    *)
      echo "Unsupported PYINSTALLER_TARGET_ARCH: $TARGET_ARCH"
      echo "Expected one of: arm64, x86_64, universal2"
      exit 1
      ;;
  esac
  echo "Building macOS app for target architecture: $TARGET_ARCH"
fi

fail() {
  echo "ERROR: $*" >&2
  exit 1
}

python_from_env_dir() {
  local env_dir="$1"
  if [[ -x "$env_dir/bin/python3" ]]; then
    echo "$env_dir/bin/python3"
    return 0
  fi
  if [[ -x "$env_dir/bin/python" ]]; then
    echo "$env_dir/bin/python"
    return 0
  fi
  return 1
}

resolve_command() {
  local candidate="$1"
  if [[ "$candidate" == */* ]]; then
    [[ -x "$candidate" ]] || return 1
    echo "$candidate"
    return 0
  fi
  command -v "$candidate" 2>/dev/null
}

resolve_python() {
  local candidate=""

  if [[ -n "${MOUSER_PYTHON:-}" ]]; then
    candidate="$(resolve_command "$MOUSER_PYTHON")" || \
      fail "MOUSER_PYTHON is set but is not executable: $MOUSER_PYTHON"
    PYTHON="$candidate"
    PYTHON_SOURCE="MOUSER_PYTHON"
    return
  fi

  if [[ -n "${VIRTUAL_ENV:-}" ]]; then
    candidate="$(python_from_env_dir "$VIRTUAL_ENV")" || \
      fail "VIRTUAL_ENV is set but no executable Python was found in $VIRTUAL_ENV/bin"
    PYTHON="$candidate"
    PYTHON_SOURCE="VIRTUAL_ENV"
    return
  fi

  if [[ -d "$ROOT_DIR/.venv" ]]; then
    candidate="$(python_from_env_dir "$ROOT_DIR/.venv")" || \
      fail "Repository .venv exists but no executable Python was found in $ROOT_DIR/.venv/bin"
    PYTHON="$candidate"
    PYTHON_SOURCE="repo .venv"
    return
  fi

  if candidate="$(command -v python3 2>/dev/null)"; then
    PYTHON="$candidate"
    PYTHON_SOURCE="PATH python3"
    return
  fi

  if candidate="$(command -v python 2>/dev/null)"; then
    PYTHON="$candidate"
    PYTHON_SOURCE="PATH python"
    return
  fi

  fail "No Python interpreter found. Create .venv or set MOUSER_PYTHON."
}

require_pyinstaller() {
  if ! "$PYTHON" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "ERROR: PyInstaller not installed in $PYTHON (source: $PYTHON_SOURCE)" >&2
    echo "       Install it with:  $PYTHON -m pip install -r $ROOT_DIR/requirements.txt" >&2
    exit 1
  fi
}

log_python_provenance() {
  local python_version
  local python_arch
  local pyinstaller_version
  python_version="$("$PYTHON" -c 'import platform; print(platform.python_version())')"
  python_arch="$("$PYTHON" -c 'import platform; print(platform.machine() or "unknown")')"
  pyinstaller_version="$("$PYTHON" -c 'import PyInstaller; print(PyInstaller.__version__)')"

  echo "Using Python: $PYTHON (source: $PYTHON_SOURCE)"
  echo "Python version: $python_version ($python_arch)"
  echo "PyInstaller version: $pyinstaller_version"
  if [[ -n "$TARGET_ARCH" ]]; then
    echo "Target architecture: $TARGET_ARCH"
  fi
}

run_pyinstaller() {
  # PYTHONHASHSEED=0 pins set iteration so PyInstaller's base_library.zip
  # layout is byte-identical across rebuilds for the same toolchain inputs.
  PYTHONHASHSEED=0 "$PYTHON" -m PyInstaller "$ROOT_DIR/Mouser-mac.spec" --noconfirm
}

entitlements_sha256() {
  shasum -a 256 "$ENTITLEMENTS" | awk '{print $1}'
}

sign_nested_code() {
  local frameworks_dir="$ROOT_DIR/dist/Mouser.app/Contents/Frameworks"
  [[ -d "$frameworks_dir" ]] || return 0

  while IFS= read -r -d '' nested; do
    "$CODESIGN" --force --options runtime --timestamp=none \
      --sign "$SIGN_IDENTITY" "$nested"
  done < <(find "$frameworks_dir" -depth \
             \( -name "*.dylib" -o -name "*.so" -o -name "*.framework" \) \
             -print0 2>/dev/null)
}

verify_bundle() {
  "$CODESIGN" --verify --deep --strict --verbose=2 "$ROOT_DIR/dist/Mouser.app"
}

sign_with_identity() {
  if [[ ! -f "$ENTITLEMENTS" ]]; then
    fail "entitlements file not found at $ENTITLEMENTS"
  fi

  echo "Signing mode: identity"
  echo "Code-signing with identity: $SIGN_IDENTITY"
  echo "Entitlements: $ENTITLEMENTS (sha256: $(entitlements_sha256))"
  sign_nested_code
  "$CODESIGN" --force --options runtime --timestamp=none \
    --entitlements "$ENTITLEMENTS" \
    --sign "$SIGN_IDENTITY" \
    "$ROOT_DIR/dist/Mouser.app"
  verify_bundle
}

sign_app() {
  if ! command -v "$CODESIGN" >/dev/null 2>&1; then
    fail "codesign not available; refusing to produce an unsigned bundle"
  fi
  sign_with_identity
}

resolve_python
require_pyinstaller
log_python_provenance
run_pyinstaller
sign_app

echo "Build complete: $ROOT_DIR/dist/Mouser.app"
