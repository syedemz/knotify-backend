"""
pytest configuration for cognito_pre_token_generation tests.

Adds the function root to sys.path so `import handler` resolves from the
source tree without needing a built Lambda zip.

Integration tests require a running docker-compose postgres container with all
migrations applied and local_init.sql executed.  Mark them with
@pytest.mark.integration so `pytest -m "not integration"` skips them cleanly.
"""

import sys
import os

import pytest

# The handler.py lives at:
#   infrastructure/src/functions/cognito_pre_token_generation/handler.py
# which is two levels above this conftest.py (tests/ -> cognito_pre_token_generation/)
_function_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _function_root not in sys.path:
    sys.path.insert(0, _function_root)

# The knotify_obs package lives at:
#   infrastructure/src/layers/observability/knotify_obs/
_obs_layer_root = os.path.normpath(
    os.path.join(_function_root, "..", "..", "layers", "observability")
)
if _obs_layer_root not in sys.path:
    sys.path.insert(0, _obs_layer_root)

# The knotify_db package lives at:
#   infrastructure/src/layers/db/knotify_db/
_db_layer_root = os.path.normpath(
    os.path.join(_function_root, "..", "..", "layers", "db")
)
if _db_layer_root not in sys.path:
    sys.path.insert(0, _db_layer_root)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring a running docker-compose postgres container",
    )


# ---------------------------------------------------------------------------
# Shared integration fixture: apply migrations + set app_user password
# Mirrors the same pattern used by cognito_post_confirmation conftest.py.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_with_migrations():
    """
    Module-scoped fixture for integration tests.

    setUp:
      1. `yoyo apply` — applies all pending migrations against the local
         docker-compose postgres container.
      2. Executes infrastructure/db/local_init.sql via psycopg2 to set the
         local app_user password to 'app_user'.

    tearDown:
      3. `yoyo rollback --all` — reverts every migration.
    """
    import subprocess
    import sys as _sys
    import psycopg2

    _here = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.normpath(
        os.path.join(_here, "..", "..", "..", "..", "..")
    )

    yoyo_ini = os.path.join(_repo_root, "infrastructure", "db", "yoyo.ini")
    local_init_sql = os.path.join(_repo_root, "infrastructure", "db", "local_init.sql")

    subprocess.run(
        [_sys.executable, "-m", "yoyo", "apply",
         "--config", yoyo_ini, "--batch"],
        check=True,
    )

    with open(local_init_sql) as f:
        init_sql = f.read()

    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=int(os.environ.get("PGPORT", "5432")),
        dbname=os.environ.get("PGDATABASE", "knotify"),
        user="knotify",
        password=os.environ.get("PGPASSWORD", "knotify"),
    )
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(init_sql)
    finally:
        conn.close()

    yield

    subprocess.run(
        [_sys.executable, "-m", "yoyo", "rollback", "--all",
         "--config", yoyo_ini, "--batch"],
        check=True,
    )
