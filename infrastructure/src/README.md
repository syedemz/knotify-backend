# knotify-backend — local development guide

This directory (`infrastructure/src/`) contains all Python source code:
Lambda layer packages and Lambda function handlers.

---

## Layout

```
infrastructure/src/
  layers/
    observability/          knotify-observability Lambda layer
      knotify_obs/          wrapper module (init_logger, correlation_id_middleware, verify_cognito_jwt)
      tests/                unit tests for the observability layer
      build.sh              builds knotify-observability-layer.zip
      layer_manifest.txt    distribution names provided by this layer (used by make package)
    db/                     knotify-db Lambda layer
      knotify_db/           wrapper module (get_connection, set_rls_context, rls_context)
      tests/                unit + integration tests for the db layer
      build.sh              builds knotify-db-layer.zip
      layer_manifest.txt    distribution names provided by this layer (used by make package)
  functions/
    _smoke/                 minimal smoke function used by make package-test (not deployed)
    cognito_post_confirmation/   (story 3.6)
    db_migrator/            (story 3.7)
  scripts/
    build_package.py        called by make package — handles layer exclusion, pip install, and zipping
    smoke_test_package.py   called by make package-test — verifies the smoke zip
    wait_for_db.py          called by make db-up — polls docker compose healthcheck
    tests/                  unit tests for the helper scripts
  pytest.ini                test discovery config for `pytest infrastructure/src/`
  README.md                 this file
```

---

## Prerequisites

- Python 3.12+ with `pip`
- Docker (compose v2) for integration tests and `make db-up`
- `psql` CLI for `make db-up`
- GNU Make (or any compatible `make`)

Install Python test dependencies:

```bash
pip install pytest pytest-cov aws-lambda-powertools PyJWT cryptography requests \
            psycopg2-binary yoyo-migrations
```

---

## Workflow

### 1. Start the local database

```bash
make db-up
```

This runs three steps in sequence:
1. `docker compose -f infrastructure/db/docker-compose.yml up -d` — starts the `pgvector/pgvector:0.8.2-pg16` container.
2. Polls the container's healthcheck until Postgres accepts connections.
3. `python -m yoyo apply --config infrastructure/db/yoyo.ini --batch` — applies all pending migrations.
4. `psql -h localhost -U knotify -d knotify -f infrastructure/db/local_init.sql` — sets the local `app_user` password.

After `make db-up` the local environment matches the cluster-side post-migration state.

---

### 2. Run the test suite

```bash
make test
```

This runs `pytest infrastructure/src/` and discovers all `test_*.py` files under
`infrastructure/src/` (see `pytest.ini` for the configuration).

**Unit tests only (no Docker required):**

```bash
pytest infrastructure/src/ -m "not integration"
```

Integration tests are marked with `@pytest.mark.integration`.  Any test that
requires a running Postgres container carries this marker.  Run
`pytest -m "not integration"` in any CI environment that lacks Docker.

---

### 3. Package a Lambda function for deployment

```bash
make package FUNC=<function_name>
```

For example:

```bash
make package FUNC=cognito_post_confirmation
make package FUNC=db_migrator
```

**What it does:**

1. Validates that `infrastructure/src/functions/<name>/` exists.
2. Reads `layer_manifest.txt` from both layer directories to find packages
   already present at Lambda runtime via the attached layers.
3. If `requirements.txt` exists in the function directory, strips layer-provided
   packages from it and `pip install`s the remainder into `build/<name>/`.
4. Copies all `*.py` source files, excluding `tests/` and `__pycache__/`.
5. Creates `build/<name>.zip` — ready to pass to `terraform apply` as the
   `filename` for the lambda module.

**Layer exclusion mechanism:**

Each layer ships a `layer_manifest.txt` listing the distribution names it
provides (e.g. `PyJWT`, `requests`, `psycopg2-binary`).  `make package` reads
the union of both manifests and excludes those packages from the function's
`pip install --target`.  This keeps function zips thin and avoids bundling
packages that are already on the Lambda's `PYTHONPATH` via the layers.

**Output:** `build/<name>.zip`

---

### 4. Smoke-test the package target

```bash
make package-test
```

Runs `make package FUNC=_smoke` against the minimal `_smoke` function (no
dependencies), verifies the zip is non-empty and contains `handler.py`, then
cleans up.  Run this to confirm the packaging pipeline is working before
stories 3.6 and 3.7 produce real function zips.

---

## Test markers

| Marker | Meaning |
|--------|---------|
| `@pytest.mark.integration` | Requires a running Docker + local Postgres container.  Run `make db-up` first.  Skip with `pytest -m "not integration"`. |

All other tests are unit tests (hermetic, no external services).

---

## Adding a new Lambda function

1. Create `infrastructure/src/functions/<name>/handler.py` with your handler.
2. If the function has dependencies beyond the two shared layers, create
   `infrastructure/src/functions/<name>/requirements.txt` listing only the
   function-specific packages (do NOT re-list packages in `layer_manifest.txt`).
3. Place tests under `infrastructure/src/functions/<name>/tests/test_*.py`.
   Mark any test that needs Postgres with `@pytest.mark.integration`.
4. Run `make package FUNC=<name>` and verify `build/<name>.zip` is created.
5. Wire the zip into the Terraform lambda module via `filename = "../../build/<name>.zip"`.
