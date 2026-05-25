"""
pytest configuration for the db layer tests.

Adds the layer root to sys.path so `import knotify_db` resolves from the
source tree without needing a built layer zip.

Integration tests require a running docker-compose postgres container with all
migrations applied and local_init.sql executed.  Mark them with
@pytest.mark.integration so `pytest -m "not integration"` skips them cleanly
in any CI path that lacks docker.
"""

import sys
import os

import pytest

# The knotify_db package lives at:
#   infrastructure/src/layers/db/knotify_db/
# which is one level above this conftest.py.
_layer_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _layer_root not in sys.path:
    sys.path.insert(0, _layer_root)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "integration: mark test as requiring a running docker-compose postgres container",
    )


# ---------------------------------------------------------------------------
# Integration fixture: apply migrations, set app_user password, rollback after
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def db_with_migrations():
    """
    Module-scoped fixture for integration tests.

    setUp:
      1. `yoyo apply` — applies all pending migrations against the local
         docker-compose postgres container.
      2. Executes infrastructure/db/local_init.sql via psycopg2 to set the
         local app_user password to 'app_user' (mirrors `psql -f local_init.sql`
         per story 3.3 AC, using psycopg2 directly so no psql binary is
         required in the test environment).

    tearDown:
      3. `yoyo rollback --all` — reverts every migration, leaving the schema
         empty so subsequent test runs start clean.

    The fixture is marked integration via the test module using
    @pytest.mark.integration — any test requesting this fixture will be
    skipped when running `pytest -m "not integration"`.
    """
    import subprocess
    import sys as _sys
    import psycopg2

    # Resolve paths relative to repo root.
    # conftest.py is at: infrastructure/src/layers/db/tests/conftest.py
    _here = os.path.dirname(os.path.abspath(__file__))
    _repo_root = os.path.normpath(
        os.path.join(_here, "..", "..", "..", "..", "..")
    )

    yoyo_ini = os.path.join(_repo_root, "infrastructure", "db", "yoyo.ini")
    local_init_sql = os.path.join(_repo_root, "infrastructure", "db", "local_init.sql")

    # 1. Apply all migrations.
    # Use `python -m yoyo` so the invocation works regardless of whether the
    # yoyo entry-point script is on PATH (on Windows it often is not, but the
    # module is always importable via the active interpreter).
    subprocess.run(
        [_sys.executable, "-m", "yoyo", "apply",
         "--config", yoyo_ini, "--batch"],
        check=True,
    )

    # 2. Execute local_init.sql to set app_user password.
    #    Reads the SQL file and executes it via psycopg2 as the knotify master
    #    user — equivalent to: psql -h localhost -U knotify -d knotify
    #                                -f infrastructure/db/local_init.sql
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

    yield  # tests run here

    # 3. Rollback all migrations
    subprocess.run(
        [_sys.executable, "-m", "yoyo", "rollback", "--all",
         "--config", yoyo_ini, "--batch"],
        check=True,
    )
