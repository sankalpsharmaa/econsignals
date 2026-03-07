"""Pytest configuration and fixtures for EconSignals tests.

Sets ECONSIGNALS_DB to a temp file before any econsignals import so tests
use an isolated database.
"""

import atexit
import os
import tempfile

# Set test DB path before any econsignals import
_test_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_test_db_path = _test_db.name
_test_db.close()
os.environ["ECONSIGNALS_DB"] = _test_db_path


def _cleanup_test_db():
    try:
        os.unlink(_test_db_path)
    except OSError:
        pass


atexit.register(_cleanup_test_db)


def pytest_configure(config):
    """Ensure test DB is initialized before any db tests run."""
    # Import here so env is already set
    from econsignals.lib.db import init_db

    init_db()
