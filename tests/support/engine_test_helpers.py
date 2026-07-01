"""Engine test helpers."""

from contextlib import contextmanager
from unittest.mock import patch


@contextmanager
def engine_start_without_kvm(engine):
    """Run ``engine.start()`` without binding loopback KVM ports."""
    with (
        patch.object(engine, "_start_remote_device_server"),
        patch.object(engine, "_start_remote_forwarder"),
    ):
        engine.start()
        yield
