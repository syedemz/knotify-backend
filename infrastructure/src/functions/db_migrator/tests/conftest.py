"""
pytest configuration for db_migrator function tests.

Handler import strategy:
  The db_migrator handler lives at infrastructure/src/functions/db_migrator/handler.py.
  The cognito_post_confirmation handler ALSO uses the name "handler.py".  Both exist
  on sys.path when pytest runs the full test suite (infrastructure/src/).  To avoid
  one conftest shadowing the other, this conftest does NOT add the db_migrator function
  root to sys.path.  Test files load the handler by absolute path using importlib.util
  (see _load_handler() in each test file).

  The db layer root IS added to sys.path so `import knotify_db` works in integration
  tests (the layer root is distinct from any function dir and safe to put on sys.path).

Integration tests require:
  - A running docker-compose Postgres container (make db-up)
  - The migrations directory at infrastructure/db/migrations/

Mark integration tests with @pytest.mark.integration so they can be
skipped with `pytest -m "not integration"`.
"""

import os
import sys

import pytest

# db layer root: infrastructure/src/layers/db/
# Needed so `import knotify_db` works in integration tests.
_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.normpath(os.path.join(_here, "..", "..", "..", "..", ".."))
_db_layer_root = os.path.join(_repo_root, "infrastructure", "src", "layers", "db")
if _db_layer_root not in sys.path:
    sys.path.insert(0, _db_layer_root)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring a running docker-compose postgres container",
    )
