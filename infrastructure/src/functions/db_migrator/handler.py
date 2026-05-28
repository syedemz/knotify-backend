"""
db_migrator — Lambda handler for running yoyo migrations against Aurora
and rotating the app_user password.

Invoked once per `terraform apply` via a null_resource local-exec when
migration files or the function code change (via null_resource triggers).

What this handler does (story 3.7):
  1. Reads AURORA_MASTER_SECRET_ARN and APP_USER_SECRET_NAME from env.
  2. Fetches the master Aurora credential from Secrets Manager.
  3. Constructs a PostgreSQL connection URL and applies all pending yoyo
     migrations (using the Python API — no subprocess shelling).
  4. Generates a cryptographically-random 32-char password.
  5. Writes the password to a Secrets Manager secret named
     APP_USER_SECRET_NAME:
       - CreateSecret on the first run (action = "created")
       - PutSecretValue on subsequent runs (action = "updated")
  6. Runs ALTER ROLE app_user WITH PASSWORD '<random>' against the cluster.
  7. Returns a structured JSON dict with:
       applied_migration_ids   — list of migration IDs applied in this run
       pending_migrations      — count of pending migrations (0 on success)
       app_user_secret_action  — "created" or "updated"
       elapsed_time_seconds    — float, wall-clock time for this invocation

Dependencies (via the shared db layer):
  - psycopg2-binary (aarch64 manylinux wheel)
  - yoyo-migrations (Python API)

The Secrets Manager calls are routed through the Interface VPC Endpoint
provisioned by story 3.0 (private_dns_enabled=true, standard hostname).
No special endpoint URL configuration is needed in the handler.
"""

from __future__ import annotations

import json
import os
import secrets
import string
import time
from pathlib import Path

# boto3 and psycopg2 are deferred to function-call time so that unit tests
# can patch them at the module boundary without requiring the packages to be
# installed in the local test environment.  At Lambda runtime they are always
# available (boto3 via the Lambda runtime itself; psycopg2 and yoyo via the
# shared db layer).
try:
    import boto3
except ImportError:  # local test environment without boto3
    boto3 = None  # type: ignore[assignment]

try:
    import psycopg2
except ImportError:  # local test environment without psycopg2
    psycopg2 = None  # type: ignore[assignment]

try:
    from yoyo import get_backend, read_migrations
except ImportError:  # local test environment without yoyo
    get_backend = None  # type: ignore[assignment]
    read_migrations = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------

_MASTER_SECRET_ARN: str = os.environ.get("AURORA_MASTER_SECRET_ARN", "")
_APP_USER_SECRET_NAME: str = os.environ.get("APP_USER_SECRET_NAME", "")

# Alphabet for the random password — URL-safe characters only so the value
# can be embedded in a connection string without escaping.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits
_PASSWORD_LENGTH = 32


# ---------------------------------------------------------------------------
# Public helpers (exposed for unit testing)
# ---------------------------------------------------------------------------

def _get_migrations_path() -> Path:
    """
    Return the absolute path to the 'migrations' directory that was bundled
    into the deployment zip by build_package.py --include-dir.

    The migrations directory sits alongside handler.py at:
        <zip-root>/migrations/
    """
    return Path(os.path.dirname(os.path.abspath(__file__))) / "migrations"


def _generate_password() -> str:
    """Generate a cryptographically-random 32-character alphanumeric password."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(_PASSWORD_LENGTH))


def _write_app_user_secret(
    sm_client,
    secret_name: str,
    password: str,
) -> str:
    """
    Write *password* to a Secrets Manager secret named *secret_name*.

    Uses exception-based idempotency (M-new-3):
      - On first run: CreateSecret → returns "created"
      - On subsequent runs: CreateSecret raises ResourceExistsException
        → PutSecretValue → returns "updated"

    Args:
        sm_client:   boto3 SecretsManager client.
        secret_name: The friendly name for the secret
                     (e.g. "knotify-dev-app-user-credential").
        password:    The new password to store.

    Returns:
        "created" or "updated" — reflects which API path was taken.
    """
    secret_value = json.dumps({"password": password, "username": "app_user"})
    try:
        sm_client.create_secret(
            Name=secret_name,
            SecretString=secret_value,
            Description="app_user role credential — rotated on every migrator invocation",
        )
        return "created"
    except sm_client.exceptions.ResourceExistsException:
        sm_client.put_secret_value(
            SecretId=secret_name,
            SecretString=secret_value,
        )
        return "updated"


def _alter_role_password(conn: psycopg2.extensions.connection, password: str) -> None:
    """
    Run ALTER ROLE app_user WITH PASSWORD '<password>' against the cluster.

    Uses %s parameterisation via psycopg2 to pass the password safely
    rather than f-string interpolation.

    Args:
        conn:     An open psycopg2 connection (master credential).
        password: The new password for the app_user role.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER ROLE app_user WITH PASSWORD %s", (password,))
    conn.commit()


def _apply_migrations(db_url: str, migrations_path: Path) -> dict:
    """
    Apply all pending yoyo migrations against the database at *db_url*.

    Args:
        db_url:          Full PostgreSQL connection URL (postgres://...).
        migrations_path: Absolute path to the directory containing .sql files.

    Returns:
        dict with keys:
          applied_ids   — list of migration step IDs applied in this run
          pending_count — number of remaining pending migrations (0 on success)
    """
    backend = get_backend(db_url)
    migrations = read_migrations(str(migrations_path))

    applied_ids: list[str] = []
    with backend.lock():
        pending = backend.to_apply(migrations)
        for migration in pending:
            backend.apply_one(migration)
            applied_ids.append(migration.id)

        remaining = backend.to_apply(migrations)
        pending_count = len(list(remaining))

    return {"applied_ids": applied_ids, "pending_count": pending_count}


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

def handler(event: dict, context: object) -> dict:
    """
    DB migrator Lambda entrypoint.

    Reads the master Aurora credential, applies pending yoyo migrations,
    rotates the app_user password in Secrets Manager and on the cluster,
    then returns a structured JSON response.

    Args:
        event:   Lambda event dict (unused — migrator is invoked without a
                 meaningful payload by the null_resource local-exec).
        context: Lambda context object (unused).

    Returns:
        dict with keys: applied_migration_ids, pending_migrations,
                        app_user_secret_action, elapsed_time_seconds.
    """
    start = time.time()

    sm = boto3.client("secretsmanager")

    # 1. Fetch Aurora master credential
    master_secret_response = sm.get_secret_value(SecretId=_MASTER_SECRET_ARN)
    master_params = json.loads(master_secret_response["SecretString"])

    host = master_params["host"]
    port = int(master_params["port"])
    dbname = master_params["dbname"]
    username = master_params["username"]
    password = master_params["password"]

    db_url = f"postgresql://{username}:{password}@{host}:{port}/{dbname}"

    # 2. Apply yoyo migrations
    migrations_path = _get_migrations_path()
    migration_result = _apply_migrations(db_url, migrations_path)
    applied_ids = migration_result["applied_ids"]
    pending_count = migration_result["pending_count"]

    # 3. Generate a new random password for app_user
    new_password = _generate_password()

    # 4. Write it to Secrets Manager (create or update)
    secret_action = _write_app_user_secret(sm, _APP_USER_SECRET_NAME, new_password)

    # 5. Apply the new password to the cluster role
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
    )
    try:
        _alter_role_password(conn, new_password)
    finally:
        conn.close()

    elapsed = time.time() - start

    return {
        "applied_migration_ids": applied_ids,
        "pending_migrations": pending_count,
        "app_user_secret_action": secret_action,
        "elapsed_time_seconds": round(elapsed, 3),
    }
