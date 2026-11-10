"""
db_migrator — Lambda handler for running yoyo migrations against Aurora
or Supabase, and rotating the app_user and aurora_refresh passwords.

Invoked once per `terraform apply` via a null_resource local-exec when
migration files or the function code change (via null_resource triggers).

Backend dispatch (story 14.13):
  At module load, USE_SUPABASE is read from the environment. If its value
  is exactly the string "true" (lowercase, case-sensitive — matches what
  Terraform emits from var.use_supabase), the handler dispatches to the
  Supabase path. Any other value (unset, "false", "True", ...) dispatches
  to the Aurora path. This case-sensitive guard matches the guardrail in
  knotify_db/__init__.py and prevents a hand-typed "True" from silently
  routing to the wrong backend.

What the Aurora path does (story 3.7 + story 7.4):
  1. Reads AURORA_MASTER_SECRET_ARN, APP_USER_SECRET_NAME,
     AURORA_REFRESH_SECRET_NAME, AURORA_HOST, AURORA_PORT, and AURORA_DBNAME
     from env.
  2. Fetches the master Aurora credential from Secrets Manager. The
     Aurora-managed secret contains ONLY `username` and `password`; the
     host/port/dbname come from the cluster endpoint outputs (passed via
     env vars from Terraform).
  3. Constructs a PostgreSQL connection URL and applies all pending yoyo
     migrations (using the Python API — no subprocess shelling).
  4. Generates a cryptographically-random 32-char password for app_user.
  5. Writes the app_user password to a Secrets Manager secret named
     APP_USER_SECRET_NAME:
       - CreateSecret on the first run (action = "created")
       - PutSecretValue on subsequent runs (action = "updated")
  6. Runs ALTER ROLE app_user WITH PASSWORD '<random>' against the cluster.
  7. Generates a cryptographically-random 32-char password for aurora_refresh
     (story 7.4). Writes it to AURORA_REFRESH_SECRET_NAME using the same
     create-or-update pattern. Runs ALTER ROLE aurora_refresh WITH PASSWORD.
     If the aurora_refresh role does not yet exist (migration 0013 not yet
     applied on this run), the step is silently skipped — migration order
     guarantees the role exists by the time this code runs in normal operation;
     the skip guard only protects against a cold-start where the schema
     pre-dates migration 0013.
  8. Returns a structured JSON dict with:
       applied_migration_ids          — list of migration IDs applied in this run
       pending_migrations             — count of pending migrations (0 on success)
       app_user_secret_action         — "created" or "updated"
       aurora_refresh_secret_action   — "created", "updated", or "skipped"
       elapsed_time_seconds           — float, wall-clock time for this invocation

What the Supabase path does (story 14.13):
  1. Reads DB_SECRET_ARN (the knotify/dev/supabase_credential secret ARN),
     SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DBNAME, APP_USER_SECRET_NAME, and
     AURORA_REFRESH_SECRET_NAME from env (all wired by local.db_env_vars in
     Terraform when use_supabase=true).
  2. Fetches the Supabase superuser credential from Secrets Manager.
     Payload: {"username": "postgres.<project-ref>", "password": "..."}.
  3. Extracts the project ref by splitting the username on '.'.
     Asserts exactly two segments — fails loudly on a malformed payload.
  4. Connects to Supabase via psycopg2 with sslmode="require".
  5. Creates the app_user and aurora_refresh roles idempotently if they do
     not exist (CREATE ROLE ... LOGIN PASSWORD ...).
  6. Applies yoyo migrations using the superuser credential (DDL and GRANTs
     run as postgres; the connection URL includes sslmode=require as a query
     param so yoyo passes it through to psycopg2 internally).
  7. Generates new random passwords and runs ALTER ROLE for both roles.
  8. Writes credentials to Secrets Manager with pooler-qualified usernames:
       app_user       → "app_user.<project-ref>"
       aurora_refresh → "aurora_refresh.<project-ref>"
     Pooler-qualified usernames are required by Supabase Transaction Mode
     pooler (port 6543) to route connections to the right role.
  9. Returns the same five-key response shape as the Aurora path.

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
from urllib.parse import quote

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
# Backend dispatch flag — read once at import time
# ---------------------------------------------------------------------------

# Case-sensitive match on lowercase "true" — matches the string Terraform
# emits from var.use_supabase (tostring(true) → "true"). Any other value
# (unset, "false", "True", "TRUE") routes to the Aurora path, which is the
# safe default.
_USE_SUPABASE: bool = os.environ.get("USE_SUPABASE") == "true"


# ---------------------------------------------------------------------------
# Environment variables — Aurora path
# ---------------------------------------------------------------------------

_MASTER_SECRET_ARN: str = os.environ.get("AURORA_MASTER_SECRET_ARN", "")
_APP_USER_SECRET_NAME: str = os.environ.get("APP_USER_SECRET_NAME", "")
_AURORA_REFRESH_SECRET_NAME: str = os.environ.get("AURORA_REFRESH_SECRET_NAME", "")
_AURORA_HOST: str = os.environ.get("AURORA_HOST", "")
_AURORA_PORT: str = os.environ.get("AURORA_PORT", "5432")
_AURORA_DBNAME: str = os.environ.get("AURORA_DBNAME", "")


# ---------------------------------------------------------------------------
# Environment variables — Supabase path
# ---------------------------------------------------------------------------

_DB_SECRET_ARN: str = os.environ.get("DB_SECRET_ARN", "")
_SUPABASE_HOST: str = os.environ.get("SUPABASE_HOST", "")
_SUPABASE_PORT: str = os.environ.get("SUPABASE_PORT", "6543")
_SUPABASE_DBNAME: str = os.environ.get("SUPABASE_DBNAME", "")


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

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


def _extract_project_ref(username: str) -> str:
    """
    Extract the Supabase project ref from the superuser username.

    Supabase's Transaction Mode pooler requires usernames in the format
    "<role>.<project-ref>" so the pooler can route the connection to the
    right project. The superuser username stored in the credential secret is
    "postgres.<project-ref>". Splitting on '.' gives exactly two segments;
    the second segment is the project ref used to build role usernames.

    Args:
        username: The superuser username from the Supabase credential secret,
                  e.g. "postgres.waulcjgqytiqkwdrkchg".

    Returns:
        The project ref string, e.g. "waulcjgqytiqkwdrkchg".

    Raises:
        ValueError: If the username does not contain exactly one '.' separator.
                    A malformed payload should fail loudly rather than silently
                    producing a wrong project ref.
    """
    segments = username.split(".")
    if len(segments) != 2:
        raise ValueError(
            f"db_migrator._extract_project_ref: expected username in the format "
            f"'<role>.<project-ref>' (exactly one dot), got {username!r} "
            f"({len(segments)} segments). The Supabase credential secret payload "
            f"may be malformed."
        )
    return segments[1]


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


def _write_app_user_secret_supabase(
    sm_client,
    secret_name: str,
    password: str,
    project_ref: str,
) -> str:
    """
    Write the app_user credential to Secrets Manager for the Supabase backend.

    Identical to _write_app_user_secret except the username is pooler-qualified
    as "app_user.<project_ref>" — required by Supabase Transaction Mode pooler
    to route the connection to the correct role.

    Args:
        sm_client:   boto3 SecretsManager client.
        secret_name: The friendly name for the secret.
        password:    The new password to store.
        project_ref: The Supabase project ref extracted from the superuser
                     username (e.g. "waulcjgqytiqkwdrkchg").

    Returns:
        "created" or "updated".
    """
    secret_value = json.dumps(
        {"password": password, "username": f"app_user.{project_ref}"}
    )
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


def _write_aurora_refresh_secret(
    sm_client,
    secret_name: str,
    password: str,
) -> str:
    """
    Write *password* to a Secrets Manager secret named *secret_name* for
    the aurora_refresh role.

    Mirrors _write_app_user_secret exactly — same create-or-update pattern,
    same exception-based idempotency — but stores username="aurora_refresh"
    and uses a description specific to the refresh credential.

    Args:
        sm_client:   boto3 SecretsManager client.
        secret_name: The friendly name for the secret
                     (e.g. "knotify-dev-aurora-refresh-credential").
        password:    The new password to store.

    Returns:
        "created" or "updated" — reflects which API path was taken.
    """
    secret_value = json.dumps({"password": password, "username": "aurora_refresh"})
    try:
        sm_client.create_secret(
            Name=secret_name,
            SecretString=secret_value,
            Description="aurora_refresh role credential — rotated on every migrator invocation",
        )
        return "created"
    except sm_client.exceptions.ResourceExistsException:
        sm_client.put_secret_value(
            SecretId=secret_name,
            SecretString=secret_value,
        )
        return "updated"


def _write_aurora_refresh_secret_supabase(
    sm_client,
    secret_name: str,
    password: str,
    project_ref: str,
) -> str:
    """
    Write the aurora_refresh credential to Secrets Manager for the Supabase
    backend with a pooler-qualified username.

    Args:
        sm_client:   boto3 SecretsManager client.
        secret_name: The friendly name for the secret.
        password:    The new password to store.
        project_ref: The Supabase project ref.

    Returns:
        "created" or "updated".
    """
    secret_value = json.dumps(
        {"password": password, "username": f"aurora_refresh.{project_ref}"}
    )
    try:
        sm_client.create_secret(
            Name=secret_name,
            SecretString=secret_value,
            Description="aurora_refresh role credential — rotated on every migrator invocation",
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


def _write_aurora_refresh_password(
    conn: psycopg2.extensions.connection, password: str
) -> bool:
    """
    Run ALTER ROLE aurora_refresh WITH PASSWORD '<password>' against the cluster.

    Returns True if the role exists and the password was updated.
    Returns False (skips silently) if the aurora_refresh role does not yet
    exist — this guard protects a cold-start where migration 0013 has not
    yet been applied in this run. In normal operation migration order ensures
    the role exists before this function is called.

    Args:
        conn:     An open psycopg2 connection (master credential).
        password: The new password for the aurora_refresh role.

    Returns:
        True if the ALTER was executed; False if the role was absent.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = 'aurora_refresh'"
        )
        role_exists = cur.fetchone() is not None

    if not role_exists:
        return False

    with conn.cursor() as cur:
        cur.execute("ALTER ROLE aurora_refresh WITH PASSWORD %s", (password,))
    conn.commit()
    return True


# Keep the old name as an alias so existing tests that call
# _alter_aurora_refresh_password continue to work.
_alter_aurora_refresh_password = _write_aurora_refresh_password



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
# Supabase entrypoint (called by handler() when _USE_SUPABASE is True)
# ---------------------------------------------------------------------------

def _handler_supabase(sm) -> dict:
    """
    DB migrator logic for the Supabase backend.

    Connects as the Supabase postgres superuser, creates the app_user and
    aurora_refresh roles idempotently, applies yoyo migrations, rotates both
    role passwords, writes pooler-qualified credentials to Secrets Manager,
    and returns the same five-key response shape as the Aurora path.

    Args:
        sm: boto3 SecretsManager client (injected by handler() for testability).

    Returns:
        dict with keys: applied_migration_ids, pending_migrations,
                        app_user_secret_action, aurora_refresh_secret_action,
                        elapsed_time_seconds.
    """
    # 1. Fetch Supabase superuser credential
    secret_response = sm.get_secret_value(SecretId=_DB_SECRET_ARN)
    creds = json.loads(secret_response["SecretString"])
    superuser_username = creds["username"]
    superuser_password = creds["password"]

    host = _SUPABASE_HOST
    port = int(_SUPABASE_PORT)
    dbname = _SUPABASE_DBNAME

    # 2. Derive project ref — asserts exactly two segments (fail loudly on
    #    malformed secret payload).
    project_ref = _extract_project_ref(superuser_username)

    # 3. Connect as the Supabase superuser.
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=superuser_username,
        password=superuser_password,
        sslmode="require",
    )

    try:
        # 4. Apply yoyo migrations as the Supabase superuser.
        #    Migrations are backend-agnostic PostgreSQL and own role creation:
        #    0007 creates app_user, 0013 creates aurora_refresh. A previous
        #    revision of this handler pre-created those roles here before
        #    running migrations, which caused 0007/0013 to fail with
        #    DuplicateObject on the CREATE ROLE statement. The handler now
        #    trusts migrations to create the roles, then ALTERs their
        #    passwords afterwards — same contract as the Aurora path.
        db_url = (
            f"postgresql://{quote(superuser_username, safe='')}:{quote(superuser_password, safe='')}"
            f"@{host}:{port}/{dbname}?sslmode=require"
        )
        migrations_path = _get_migrations_path()
        migration_result = _apply_migrations(db_url, migrations_path)
        applied_ids = migration_result["applied_ids"]
        pending_count = migration_result["pending_count"]

        # 5. Rotate app_user password
        new_app_password = _generate_password()
        with conn.cursor() as cur:
            cur.execute("ALTER ROLE app_user WITH PASSWORD %s", (new_app_password,))
        conn.commit()
        app_user_secret_action = _write_app_user_secret_supabase(
            sm, _APP_USER_SECRET_NAME, new_app_password, project_ref
        )

        # 6. Rotate aurora_refresh password
        aurora_refresh_secret_action: str
        if _AURORA_REFRESH_SECRET_NAME:
            new_refresh_password = _generate_password()
            with conn.cursor() as cur:
                cur.execute("ALTER ROLE aurora_refresh WITH PASSWORD %s", (new_refresh_password,))
            conn.commit()
            aurora_refresh_secret_action = _write_aurora_refresh_secret_supabase(
                sm, _AURORA_REFRESH_SECRET_NAME, new_refresh_password, project_ref
            )
        else:
            aurora_refresh_secret_action = "skipped"

    finally:
        conn.close()

    return {
        "applied_migration_ids": applied_ids,
        "pending_migrations": pending_count,
        "app_user_secret_action": app_user_secret_action,
        "aurora_refresh_secret_action": aurora_refresh_secret_action,
    }


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

def handler(event: dict, context: object) -> dict:
    """
    DB migrator Lambda entrypoint.

    Dispatches to the Supabase path when USE_SUPABASE="true" (import-time
    flag), otherwise runs the Aurora path.

    Both paths return a dict with keys: applied_migration_ids,
    pending_migrations, app_user_secret_action, aurora_refresh_secret_action,
    elapsed_time_seconds.

    Args:
        event:   Lambda event dict (unused — migrator is invoked without a
                 meaningful payload by the null_resource local-exec).
        context: Lambda context object (unused).

    Returns:
        dict with keys: applied_migration_ids, pending_migrations,
                        app_user_secret_action, aurora_refresh_secret_action,
                        elapsed_time_seconds.
    """
    start = time.time()

    sm = boto3.client("secretsmanager")

    if _USE_SUPABASE:
        result = _handler_supabase(sm)
    else:
        result = _handler_aurora(sm)

    result["elapsed_time_seconds"] = round(time.time() - start, 3)
    return result


# ---------------------------------------------------------------------------
# Aurora entrypoint (extracted from the original handler body)
# ---------------------------------------------------------------------------

def _handler_aurora(sm) -> dict:
    """
    DB migrator logic for the Aurora backend.

    Preserves the original handler body byte-for-byte in its behaviour.
    Reads the Aurora master credential, applies pending yoyo migrations,
    rotates the app_user and aurora_refresh passwords in Secrets Manager
    and on the cluster.

    Args:
        sm: boto3 SecretsManager client.

    Returns:
        dict with keys: applied_migration_ids, pending_migrations,
                        app_user_secret_action, aurora_refresh_secret_action.
        (elapsed_time_seconds is added by handler() after the call.)
    """
    # 1. Fetch Aurora master credential. The Aurora-managed secret only holds
    #    username + password — connection endpoint values come from env vars
    #    set by Terraform from the Aurora module's cluster_endpoint/port/db
    #    outputs.
    master_secret_response = sm.get_secret_value(SecretId=_MASTER_SECRET_ARN)
    master_params = json.loads(master_secret_response["SecretString"])

    username = master_params["username"]
    password = master_params["password"]
    host = _AURORA_HOST
    port = int(_AURORA_PORT)
    dbname = _AURORA_DBNAME

    # The Aurora-managed master password is random and may contain
    # characters that are reserved in URLs (':', '@', '/', '<', '#', ...).
    # urllib parses such characters greedily and a single '<' is enough
    # to make the URL parser interpret the password as the port. Percent-
    # encode username and password before composing the connection URL.
    db_url = (
        f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{dbname}"
    )

    # 2. Apply yoyo migrations
    migrations_path = _get_migrations_path()
    migration_result = _apply_migrations(db_url, migrations_path)
    applied_ids = migration_result["applied_ids"]
    pending_count = migration_result["pending_count"]

    # Open one connection for all ALTER ROLE operations (master credential).
    conn = psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
    )
    try:
        # 3. Generate a new random password for app_user
        new_app_password = _generate_password()

        # 4. Write it to Secrets Manager (create or update)
        app_user_secret_action = _write_app_user_secret(
            sm, _APP_USER_SECRET_NAME, new_app_password
        )

        # 5. Apply the new password to the app_user cluster role
        _alter_role_password(conn, new_app_password)

        # 6. Generate a new random password for aurora_refresh (story 7.4)
        new_refresh_password = _generate_password()

        # 7. Write it to Secrets Manager (create or update) — same pattern as
        #    app_user_credential; no new abstraction (story 7.4 AC).
        aurora_refresh_secret_action: str
        if _AURORA_REFRESH_SECRET_NAME:
            aurora_refresh_secret_action = _write_aurora_refresh_secret(
                sm, _AURORA_REFRESH_SECRET_NAME, new_refresh_password
            )
            # 8. Apply the new password to the aurora_refresh cluster role.
            #    Skips silently if the role does not yet exist (migration 0013
            #    not yet applied — only possible on a partial rollback scenario).
            role_updated = _alter_aurora_refresh_password(conn, new_refresh_password)
            if not role_updated:
                aurora_refresh_secret_action = "skipped"
        else:
            aurora_refresh_secret_action = "skipped"
    finally:
        conn.close()

    return {
        "applied_migration_ids": applied_ids,
        "pending_migrations": pending_count,
        "app_user_secret_action": app_user_secret_action,
        "aurora_refresh_secret_action": aurora_refresh_secret_action,
    }
