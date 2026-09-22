"""Shared test helpers."""

# Any test that pulls in tests.support gets offscreen Qt for free (see offscreen.py).
from tests.support import offscreen as _offscreen  # noqa: E402,F401

# Keep test logging out of the real seat log (~/Library/Logs/Mouser/mouser.log):
# importing main_qml calls setup_logging(), which redirects stdout there for
# the rest of the process, and the tap-line prints of later tests then land
# in the log that deskflow's native-tap deploy gate reads. A caller that
# sets MOUSER_LOG_DIR explicitly still wins.
import os as _os
import tempfile as _tempfile

if not _os.environ.get("MOUSER_LOG_DIR"):
    _os.environ["MOUSER_LOG_DIR"] = _tempfile.mkdtemp(prefix="mouser-tests-logs-")

# Tests must never reach the real sudo (this Mac has passwordless sudo). A shim
# on PATH fails loudly instead; tests that need sudo behaviour mock it explicitly.
_os.environ["PATH"] = _os.path.join(_os.path.dirname(__file__), "fakebin") + _os.pathsep + _os.environ.get("PATH", "")
