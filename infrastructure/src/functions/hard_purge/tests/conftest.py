"""
pytest configuration for the knotify-hard-purge function tests.

Adds the shared Lambda layer source directories to sys.path so that
`import knotify_db` resolves from the source tree without requiring the
layers to be packaged into zips first.

Also provides the db_with_migrations module-scoped fixture for integration
tests by importing it from the db layer conftest.
"""

import os
import sys

# Functions live at: infrastructure/src/functions/hard_purge/tests/  (this file)
# Layers live at:    infrastructure/src/layers/{db,observability}/
# Path traversal: tests/ -> hard_purge/ -> functions/ -> src/
_src_root = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

_layer_paths = [
    os.path.join(_src_root, "layers", "db"),
    os.path.join(_src_root, "layers", "observability"),
]

for _p in _layer_paths:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Re-export the db_with_migrations fixture from the db layer conftest so
# integration tests in this package can request it without a duplicate
# implementation.
_db_conftest_dir = os.path.join(_src_root, "layers", "db", "tests")
if _db_conftest_dir not in sys.path:
    sys.path.insert(0, _db_conftest_dir)

from conftest import db_with_migrations  # noqa: F401  (re-export for pytest)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring a running docker-compose postgres container",
    )
