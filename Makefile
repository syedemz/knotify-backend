# Makefile — knotify-backend local development tooling
#
# Targets:
#   make package FUNC=<name>  Build a Lambda deployment zip for src/functions/<name>/
#   make package-all           Build both layer zips and all function zips (CI + local)
#   make package-test          Smoke-test the package target end-to-end then clean up
#   make db-up                 Start local Postgres, apply migrations, run local_init.sql
#   make test                  Run the full pytest suite under infrastructure/src/
#
# All path variables are relative to the Makefile's location (the repo root).
# Targets are PHONY so Make never mistakes an output file for an up-to-date target.

.PHONY: package package-all package-test db-up test

# ---------------------------------------------------------------------------
# Internal path constants
# ---------------------------------------------------------------------------

# Repo root (where this Makefile lives)
ROOT := $(CURDIR)

# Python source root.  PRD shorthand "src/" maps to this directory.
SRC_ROOT := $(ROOT)/infrastructure/src

# Helper scripts (all pure Python — no system zip/shell extensions needed)
BUILD_PKG_SCRIPT      := $(SRC_ROOT)/scripts/build_package.py
SMOKE_TEST_SCRIPT     := $(SRC_ROOT)/scripts/smoke_test_package.py
WAIT_FOR_DB_SCRIPT    := $(SRC_ROOT)/scripts/wait_for_db.py

# Build output root — all zip artifacts land here
BUILD_DIR := $(ROOT)/build

# Docker-compose file for the local Postgres container
COMPOSE_FILE := $(ROOT)/infrastructure/db/docker-compose.yml

# yoyo configuration (sources = infrastructure/db/migrations/)
YOYO_INI := $(ROOT)/infrastructure/db/yoyo.ini

# post-yoyo local initialisation script (sets app_user password)
LOCAL_INIT_SQL := $(ROOT)/infrastructure/db/local_init.sql

# Migrations directory — bundled into the db_migrator function zip
MIGRATIONS_DIR := $(ROOT)/infrastructure/db/migrations

# Layer build scripts
OBS_BUILD_SH  := $(SRC_ROOT)/layers/observability/build.sh
DB_BUILD_SH   := $(SRC_ROOT)/layers/db/build.sh

# ---------------------------------------------------------------------------
# make package FUNC=<name>
#
# Produces build/<name>.zip from infrastructure/src/functions/<name>/.
#
# Delegates entirely to build_package.py which:
#   1. Validates the function directory exists (fails loudly if not).
#   2. Reads the union of layer manifests from:
#        src/layers/observability/layer_manifest.txt
#        src/layers/db/layer_manifest.txt
#      to determine which distribution names are already present at Lambda
#      runtime via the attached layers.
#   3. If a requirements.txt exists, strips layer-provided packages and
#      pip-installs the remainder into build/<name>/.
#   4. Copies all *.py source files, excluding tests/ and __pycache__.
#   5. Creates build/<name>.zip (pure Python zipfile — cross-platform).
# ---------------------------------------------------------------------------

package:
ifndef FUNC
	$(error FUNC is not set. Usage: make package FUNC=<function_name>)
endif
ifeq ($(FUNC),db_migrator)
	python3 "$(BUILD_PKG_SCRIPT)" \
	    --func "$(FUNC)" \
	    --src-root "$(SRC_ROOT)" \
	    --build-dir "$(BUILD_DIR)" \
	    --include-dir "$(MIGRATIONS_DIR):migrations:^\d{4}_.+\.(rollback\.)?sql$$"
else
	python3 "$(BUILD_PKG_SCRIPT)" \
	    --func "$(FUNC)" \
	    --src-root "$(SRC_ROOT)" \
	    --build-dir "$(BUILD_DIR)"
endif

# ---------------------------------------------------------------------------
# make package-all
#
# Builds both shared Lambda layer zips and all function deployment zips.
# Run this before `terraform plan` or `terraform apply` to ensure the
# artifacts referenced by Terraform's `filename` arguments exist.
#
# This is what CI calls (M-new-2): both plan-dev/plan-prod and
# apply-dev/apply-prod run this target before invoking Terraform.
# ---------------------------------------------------------------------------

package-all:
	@echo "[package-all] Building observability layer ..."
	bash "$(OBS_BUILD_SH)"
	@echo "[package-all] Building db layer ..."
	bash "$(DB_BUILD_SH)"
	@echo "[package-all] Copying layer zips to build/ ..."
	mkdir -p "$(BUILD_DIR)"
	cp "$(SRC_ROOT)/layers/observability/knotify-observability-layer.zip" "$(BUILD_DIR)/"
	cp "$(SRC_ROOT)/layers/db/knotify-db-layer.zip" "$(BUILD_DIR)/"
	@echo "[package-all] Building cognito_post_confirmation function ..."
	$(MAKE) package FUNC=cognito_post_confirmation
	@echo "[package-all] Building cognito_pre_token_generation function ..."
	$(MAKE) package FUNC=cognito_pre_token_generation
	@echo "[package-all] Building db_migrator function ..."
	$(MAKE) package FUNC=db_migrator
	@echo "[package-all] Building profile function ..."
	$(MAKE) package FUNC=profile
	@echo "[package-all] Building blocks function ..."
	$(MAKE) package FUNC=blocks
	@echo "[package-all] Building friends function ..."
	$(MAKE) package FUNC=friends
	@echo "[package-all] Done — all artifacts in $(BUILD_DIR)/"

# ---------------------------------------------------------------------------
# make package-test
#
# Smoke-tests the package target end-to-end:
#   1. Runs `make package FUNC=_smoke` against the minimal _smoke function.
#   2. Verifies the zip is non-empty and contains handler.py.
#   3. Cleans up the build/_smoke staging area and zip.
#
# Fails loudly if the zip does not exist, is empty, or lacks handler.py.
# ---------------------------------------------------------------------------

package-test:
	@echo "[package-test] Running smoke build ..."
	$(MAKE) package FUNC=_smoke
	python3 "$(SMOKE_TEST_SCRIPT)" "$(BUILD_DIR)"

# ---------------------------------------------------------------------------
# make db-up
#
# Brings up the local development database in a single command:
#   1. docker compose up -d          — start the container in the background
#   2. wait for healthcheck          — polls until Postgres accepts connections
#   3. python -m yoyo apply --batch  — apply all pending migrations
#   4. psql -f local_init.sql        — set the app_user password (mirrors the
#                                      cluster-side post-yoyo state)
#
# Prerequisites: docker (compose v2), Python (with yoyo-migrations installed),
#                psql
# ---------------------------------------------------------------------------

db-up:
	@echo "[db-up] Starting local Postgres container ..."
	docker compose -f "$(COMPOSE_FILE)" up -d
	@echo "[db-up] Waiting for Postgres healthcheck ..."
	python3 "$(WAIT_FOR_DB_SCRIPT)" "$(COMPOSE_FILE)"
	@echo "[db-up] Applying migrations ..."
	python3 -m yoyo apply --config "$(YOYO_INI)" --batch
	@echo "[db-up] Running local_init.sql ..."
	psql -h localhost -U knotify -d knotify -f "$(LOCAL_INIT_SQL)"
	@echo "[db-up] Done — local environment ready"

# ---------------------------------------------------------------------------
# make test
#
# Runs the full pytest suite under infrastructure/src/.
# Integration tests (marked @pytest.mark.integration) require the local
# Postgres container to be running — run `make db-up` first.
#
# To skip docker-dependent tests:
#   pytest infrastructure/src/ -m "not integration"
# ---------------------------------------------------------------------------

test:
	pytest "$(SRC_ROOT)/"
