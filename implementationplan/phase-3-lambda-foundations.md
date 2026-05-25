phase: 3
title: Lambda foundations
last_updated: 2026-05-25

context_summary: |
  Builds the shared Lambda substrate every later phase reuses. Establishes (a) the VPC endpoints that let in-VPC Lambdas reach AWS service APIs without a NAT Gateway, (b) the lambda Terraform module pattern (function definition, IAM role template, log group with 7-day retention per the owner's observability directive, alias "live" on a published version), (c) two shared Lambda layers — observability/JWT helpers and Aurora access with psycopg2-binary, pgvector, and the RLS session-GUC setter, (d) least-privilege IAM role templates per access pattern (scaffolded; finalized in consuming phases), (e) local dev tooling (packaging, integration testing against a containerized Postgres), (f) the DB migrator Lambda that runs yoyo against the dev Aurora cluster from inside the VPC AND generates a random app_user password stored in Secrets Manager (the cluster-side migration work that was deferred from phase 2 per the 2026-05-23 phase-2 brainstorm), and (g) the knotify-cognito-post-confirmation Lambda. The Cognito trigger Lambda is built and unit-tested here but not wired to a Cognito User Pool — that happens in phase 4, ensuring signup works end-to-end the moment Cognito ships. By the end of this phase, the dev Aurora cluster has the full §5.1 schema applied (users with relaxed nullability + profile-completion CHECK, siblings, friendships, friend_requests, bookmarks, blocks, deck_view, the immutable-fields trigger guarded for first-set-once semantics, RLS policy, and app_user role with a Secrets-Manager-sourced random password).

  Decisions carried from the phase-3 brainstorm (2026-05-24): see phasebrainstorms/phase-3-lambda-foundations-brainstorm.md for the full audit trail. Notable cross-phase ripples that already shipped in this same change: migrations 0002 / 0007 / 0008 were edited in place (Aurora has not been migrated yet — safe), architecture.md §5.1 nullability + §13a profile-completion enforcement model documented, phase-4 PRD gained a PreTokenGeneration Lambda story and dropped preferred_username from the User Pool schema.

stories:
  - id: 3.0
    title: VPC endpoints for in-VPC Lambda → AWS service APIs
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 30
    acceptance_criteria:
      - infrastructure/modules/networking/vpc_endpoints.tf creates an aws_vpc_endpoint of type "Interface" for com.amazonaws.<region>.secretsmanager, attached to both private subnets, with security_group_ids referencing a NEW security group sg-vpce that allows TCP 443 ingress from sg-lambda only (no egress rules needed; the response path is via the established connection). private_dns_enabled=true so Lambdas use the standard secretsmanager.<region>.amazonaws.com hostname and the resolution short-circuits to the endpoint
      - The same module also creates an aws_vpc_endpoint of type "Gateway" for com.amazonaws.<region>.dynamodb, associated with the private and DB route tables. Gateway endpoints are free; no security group, no DNS toggle
      - Module outputs secretsmanager_vpc_endpoint_id and dynamodb_vpc_endpoint_id (the latter may be unused until phase 6; ship the output anyway so the consuming phases just wire it without re-touching the networking module)
      - tests/networking.tftest.hcl gains two new plan-mode runs: (a) one asserts the Secrets Manager interface endpoint exists with private_dns_enabled=true and its security group has exactly one ingress rule from sg-lambda's id, (b) one asserts the DynamoDB gateway endpoint is associated with both private route tables AND both db route tables (four associations total — Lambdas in private subnets, future consumers attaching to DB subnets). All tests pass
      - terraform fmt/validate clean; the full test suite (networking + aurora + dynamodb modules) stays green
    notes: "Resolves brainstorm BLOCKER B1. Without these endpoints, the db_migrator and cognito_post_confirmation Lambdas in this same phase have NO route to secretsmanager.<region>.amazonaws.com (no NAT, no IGW in this VPC by design — phase 1 story 1.1 AC). The DynamoDB gateway is bundled here even though it has no phase-3 consumer because it is a one-line additional resource and avoids re-touching the networking module in phase 6. Cost: ~$14/mo dev (interface endpoint, two AZs); DynamoDB gateway is free. Brainstorm N6 resolution."

  - id: 3.1
    title: Lambda Terraform module skeleton
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 31
    acceptance_criteria:
      - File infrastructure/modules/lambda/main.tf accepts inputs function_name, handler, runtime, architectures, layers, environment_variables (map), vpc_config (object with subnet_ids + security_group_ids), role_arn, memory_size, timeout, and creates aws_lambda_function, aws_cloudwatch_log_group with retention_in_days=7 and name "/aws/lambda/${var.function_name}", and an aws_lambda_alias named "live"
      - aws_lambda_function sets publish=true and the alias's function_version references aws_lambda_function.this.version. Without publish=true the alias has no concrete version to point at and apply fails (brainstorm M8)
      - aws_lambda_function declares depends_on = [aws_cloudwatch_log_group.this] so the explicit log group is created BEFORE the first invocation; otherwise AWS auto-creates a "Never expire" log group and the subsequent apply of the explicit group fails with ResourceAlreadyExists (brainstorm M9)
      - environment_variables is merged with module-level defaults POWERTOOLS_SERVICE_NAME=var.function_name and LOG_LEVEL="INFO" so every consumer Lambda picks these up without restating them (brainstorm N2). Consumer overrides win via Terraform's merge() right-side precedence
      - Default runtime is "python3.14", default architectures is ["arm64"], default timeout 10s, default memory_size 512
      - terraform validate passes; a smoke instantiation with a dummy zip file plans cleanly
    notes: "Provider constraint bumped from ~> 5.70 to ~> 6.20 across all 4 modules — python3.14 runtime requires provider >= 6.20.0. Networking deprecation data.aws_region.current.name → .region fixed as part of the same change. 10/10 lambda tests pass; full suite (aurora 10 + networking 8 + dynamodb 28 + lambda 10 = 56) clean."

  - id: 3.2
    title: Shared observability layer (Powertools, logger, optional JWT helper)
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 32
    acceptance_criteria:
      - Directory src/layers/observability/ contains a build script that produces a python/ directory with aws-lambda-powertools pinned to a specific version compatible with Python 3.14 (no "latest" — pin in the build script for reproducibility, brainstorm N4), PyJWT[crypto] pinned (replaces "python-jose or PyJWT" — PyJWT is actively maintained, brainstorm M5), and a thin wrapper module knotify_obs with helpers init_logger(service), correlation_id_middleware, verify_cognito_jwt(token, user_pool_id, region)
      - verify_cognito_jwt is documented in knotify_obs/README.md as "rarely used — the HTTP API Cognito JWT authorizer (phase 5) handles routine validation natively. This helper exists for Lambda authorizers or other niche paths that may emerge in phase 11 hardening." (brainstorm M5 — kept with documented use case)
      - Building the layer produces a .zip artifact and Terraform packages it as aws_lambda_layer_version with compatible_runtimes=["python3.14"] and compatible_architectures=["arm64"]
      - Unit test verify_cognito_jwt rejects a token with wrong issuer, wrong audience, expired exp, and accepts a known-good signed token. JWKS fetch is mocked at the requests-library boundary so the test is hermetic
    notes: ""

  - id: 3.3
    title: Shared Aurora-access layer (psycopg2, pgvector, RLS GUC helper)
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 33
    acceptance_criteria:
      - Directory src/layers/db/ packages psycopg2-binary (manylinux2014_aarch64 wheel), pgvector Python client (kept — brainstorm N3), and module knotify_db exposing get_connection(secret_or_env), set_rls_context(conn, user_id, user_sex), and a context manager that resets the GUCs on exit
      - set_rls_context calls SET LOCAL app.requesting_user_id = '<uuid>'; SET LOCAL app.requesting_user_sex = '<Male|Female>'; — the GUC names match migration 0007_rls_app_user_and_policy.sql lines 107–110 verbatim. If the helper writes any other GUC name, the RLS policy reads NULL and fail-closes to zero rows with no error (brainstorm T2 — pin the names in the AC)
      - Integration test against the local Postgres container from phase 2 calls set_rls_context with a Male user and asserts that a follow-up SELECT FROM users returns only female rows plus the male's own row
      - A pytest fixture (in src/layers/db/tests/conftest.py) runs `yoyo apply` against the local docker-compose container in setUp and `yoyo rollback --all` in tearDown, then runs `psql -f infrastructure/db/local_init.sql` to set the local app_user password (since 0007 no longer embeds it). The fixture is marked `@pytest.mark.integration` so a `pytest -m "not integration"` invocation skips it cleanly in any future CI path that lacks docker (brainstorm T3). deploy.yml's `test` job stays terraform-test-only in this phase — no pytest in CI
      - Built as aws_lambda_layer_version with compatible_runtimes=["python3.14"], compatible_architectures=["arm64"]
    notes: ""

  - id: 3.4
    title: IAM role templates per access pattern
    agent: backenddeveloper
    done: false
    depends_on: []
    tracking_issue: 34
    acceptance_criteria:
      - Module infrastructure/modules/iam_roles/ exposes named role builders aurora_reader, aurora_writer, dynamodb_chat_writer, dynamodb_notifications_writer, stepfn_task, cognito_trigger, and db_migrator; each attaches the minimal set of managed and inline policies (brainstorm M2 — keep all roles even though only cognito_trigger and db_migrator have phase-3 consumers; the others are needed in phases 6–9 and shipping them all here avoids re-touching this module repeatedly)
      - The db_migrator role attaches AWSLambdaVPCAccessExecutionRole, allows secretsmanager:GetSecretValue scoped via a constructed-name ARN pattern `arn:${data.aws_partition.current.partition}:secretsmanager:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:secret:rds!cluster-${module.aurora.cluster_resource_id}-*` (brainstorm M1 — option c, constructed-name, tightest scoping, single apply, no tag dependency). It also allows secretsmanager:CreateSecret + secretsmanager:PutSecretValue + secretsmanager:DescribeSecret scoped to `arn:...:secret:knotify-${var.environment}-app-user-credential-*` so the migrator can create/update the app_user credential post-yoyo (brainstorm B2 — split 0007 fix). It does NOT need Aurora IAM-database-auth permissions because the migrator connects as the Aurora master via Secrets Manager credentials
      - The cognito_trigger role attaches AWSLambdaVPCAccessExecutionRole and allows secretsmanager:GetSecretValue scoped to the app_user credential ARN pattern `arn:...:secret:knotify-${var.environment}-app-user-credential-*` (post-confirmation reads this to connect as app_user). Cognito-side invoke permission (aws_lambda_permission for cognito-idp.amazonaws.com) is intentionally NOT created here — it ships with the wiring in phase 4 story 4.3 (brainstorm N1)
      - Each role includes the AWSLambdaVPCAccessExecutionRole managed policy so the Lambda can attach an ENI
      - terraform validate passes; a unit test (.tftest.hcl) instantiates each role and asserts the attached policy ARNs match expectations. For roles with no phase-3 consumers (aurora_reader, aurora_writer, dynamodb_*, stepfn_task), the tests assert only the trust policy and AWSLambdaVPCAccessExecutionRole attachment; per-action assertions for those roles ship with their consuming phase
    notes: ""

  - id: 3.5
    title: Local development tooling
    agent: backenddeveloper
    done: false
    depends_on: [3.2, 3.3]
    tracking_issue: 35
    acceptance_criteria:
      - A Makefile target "make package FUNC=<name>" produces a Lambda deployment .zip under build/ from src/functions/<name>/, excluding the function's tests/ subdirectory and any dependencies provided by the observability or db layer (the layer manifest lists which packages each layer provides)
      - A make target "make db-up" runs `docker compose -f infrastructure/db/docker-compose.yml up -d`, waits for healthcheck, runs `yoyo apply`, then `psql -h localhost -U knotify -d knotify -f infrastructure/db/local_init.sql` so the local environment matches the cluster-side post-yoyo state in a single command
      - Test layout is colocated: each function and layer owns its tests under src/functions/<name>/tests/ or src/layers/<name>/tests/. A top-level infrastructure/src/pytest.ini sets testpaths=. and python_files=test_*.py. `pytest infrastructure/src/` discovers all tests; `pytest -m "not integration"` skips docker-dependent tests (brainstorm T1)
      - "make test" runs `pytest infrastructure/src/` and exits zero with at least the verify_cognito_jwt and set_rls_context tests from stories 3.2 and 3.3 included
      - README at src/ documents the workflow (make db-up, make package FUNC=, make test, the @pytest.mark.integration marker convention)
    notes: ""

  - id: 3.6
    title: knotify-cognito-post-confirmation Lambda (built, not wired)
    agent: backenddeveloper
    done: false
    depends_on: [3.0, 3.1, 3.2, 3.3, 3.4, 3.5]
    tracking_issue: 36
    acceptance_criteria:
      - Source directory src/functions/cognito_post_confirmation/ contains a Lambda handler that receives a Cognito PostConfirmation_ConfirmSignUp event, extracts user_id (sub) and email (required), and optionally extracts given_name → first_name, family_name → last_name, gender → sex via the mapping table {m, male, M → Male; f, female, F → Female; anything else → leave NULL and log a structured warning} (brainstorm M3 — gender mapping retained for robustness), birthdate → birthday (parsed as ISO date; on parse failure leave NULL and log warning), and inserts a minimal users row using INSERT … ON CONFLICT (user_id) DO NOTHING for idempotency
      - The minimal-row insert ships only the columns Cognito actually supplied; first_name, last_name, sex, birthday, username remain NULL when absent (these are nullable per migration 0002 as updated by the phase-3 brainstorm; the profile-completion endpoint in phase 6 fills them in). profile_complete_verified defaults to false from the table DEFAULT
      - If email is missing from the Cognito event, the handler logs a structured error and returns the event unmodified WITHOUT inserting — a partial signup with no email cannot be recovered downstream, and inserting an email-less row would violate users.email NOT NULL. Cognito does NOT roll back the signup (the handler always returns the event), so the user can complete signup via a different path later (brainstorm M4)
      - The handler does NOT return an error when the row already exists (re-runs are no-ops) and does NOT raise on any data validation failure other than missing email — defense against Cognito retrying confirmation events
      - Integration test against the Postgres container exercises (a) full attribute set → row with all populated columns, (b) social-login minimal (email + given_name + family_name only, no gender / no birthdate) → row with first_name + last_name populated, sex / birthday / username NULL, (c) gender variants "male", "M", "Female", "x" → mapped to Male, Male, Female, NULL respectively, (d) two successive invocations with identical event → exactly one users row, (e) event missing email → no insert, handler returns event unmodified after logging
      - The Lambda is deployed via the lambda module with the observability and db layers attached, IAM role "cognito_trigger" (story 3.4), VPC config wired to phase 1 private subnets, environment variable DB_SECRET_ARN pointing at the knotify-<env>-app-user-credential secret (the migrator creates this in story 3.7). The secretsmanager:GetSecretValue call from this Lambda goes through the Interface VPC Endpoint from story 3.0
      - terraform plan in dev shows the function created but no aws_cognito_user_pool_lambda_config wiring AND no aws_lambda_permission for cognito-idp.amazonaws.com (intentional — wiring + permission are deferred to phase 4 story 4.3, brainstorm N1)
      - Documented note: the deployed Lambda is unverified end-to-end until phase 4 wires Cognito (brainstorm T5). The integration tests in this story exercise the handler logic against a local container only — they prove the code path is sound, but the AWS-side trigger fires for the first time in phase 4
    notes: ""

  - id: 3.7
    title: DB migrator Lambda, app_user password generation, and initial cluster-side migration run
    agent: backenddeveloper
    done: false
    depends_on: [3.0, 3.1, 3.3, 3.4]
    tracking_issue: 37
    acceptance_criteria:
      - Source directory src/functions/db_migrator/ contains a Lambda handler that (a) reads env var AURORA_MASTER_SECRET_ARN, (b) fetches the secret via boto3 secretsmanager:GetSecretValue (call routed via the Interface VPC Endpoint from story 3.0), (c) parses host/port/username/password/dbname from the Aurora-managed JSON, (d) constructs a Postgres connection URL, (e) uses the yoyo-migrations Python API — `from yoyo import read_migrations, get_backend` — to apply migrations against the cluster using the migrations directory baked into the deploy package, (f) after yoyo apply, generates a cryptographically-random 32-character password, writes it to a NEW Secrets Manager secret named `knotify-${env}-app-user-credential` (created on first run, PutSecretValue on subsequent runs), then connects to the cluster and runs `ALTER ROLE app_user WITH PASSWORD '<random>'`, (g) returns a structured JSON response listing applied migration ids, pending migrations (should be zero on success), whether the app_user secret was created or updated, and total elapsed time. The yoyo Python API is the canonical embedding path (the CLI is a thin wrapper) — Lambda containers cannot reliably shell out, so the API path is the only correct choice (brainstorm M6)
      - The function packages the migrations directory from infrastructure/db/migrations/ into the Lambda deployment .zip — exactly the files matching /^\d{4}_.+\.sql$/ (excludes the local-dev-only infrastructure/db/local_init.sql which lives outside migrations/ and is therefore not picked up)
      - The function bundles yoyo-migrations and psycopg2-binary (manylinux2014_aarch64 wheel) via the shared db layer from story 3.3 — keeps the function package thin and means a yoyo upgrade ripples through one layer rebuild
      - The function is deployed via the lambda module with IAM role "db_migrator" (from story 3.4), VPC config wired to phase 1 private subnets and the aurora_security_group_id (so the ENI can reach Aurora on port 5432), memory_size 1024, timeout 300s (migrations are bounded but allow headroom for the largest migration plus the HNSW index build on the users table)
      - The function is invoked once against the dev Aurora cluster as part of phase 3's apply flow via a `null_resource` with `local-exec` (`aws lambda invoke --function-name knotify-db-migrator-dev --payload '{}' out.json && cat out.json`). The null_resource sets `triggers = { migrations_hash = sha256(jsonencode([for f in fileset(...) : filesha256(...)])), lambda_version = aws_lambda_function.db_migrator.version }` so it re-runs whenever any migration file changes OR the migrator code changes — otherwise new migrations after the first apply would require a manual `terraform taint` to re-invoke (brainstorm N5)
      - After the first invocation succeeds, a Secrets Manager secret `knotify-dev-app-user-credential` exists with a populated SecretString, and the dev Aurora cluster has the §5.1 schema applied. Verification is via the migrator Lambda's own structured response (pending_migrations: 0, applied_count > 0, app_user_secret_action in {created, updated}) — no separate verify branch / no second invocation pattern (brainstorm M7)
      - The idempotency check is a third invocation on the now-fully-migrated cluster: the response reports zero pending migrations and app_user_secret_action="updated" (the migrator rotates the password on every run for simplicity — phases 6+ business Lambdas read the current value via SecretsManager and reconnect on auth failure). If a no-rotate-on-no-migration-change behavior is preferred later, gate the ALTER ROLE on the same migrations_hash that triggers null_resource — defer to a future story
      - prod migrator Lambda is also authored in infrastructure/environments/prod/main.tf, but is not deployed (apply gated per PROD_CUTOVER.md, consistent with phase 2 story 2.14). The prod invocation step in deploy.yml is similarly gated. Add CI assertion AC: actionlint passes, plan-prod skips at evaluation time under the DEPLOY_PROD=false gate, and no resource is created in any AWS account labeled prod during this phase's apply (brainstorm T4)
      - Integration test (run against the local Postgres container, marked @pytest.mark.integration) validates the Lambda's handler logic by mocking boto3 SecretsManager (for both the master-secret fetch and the app_user-secret write) and asserting that yoyo applies migrations correctly AND the ALTER ROLE step runs with a non-empty password. This test runs as part of `make test` per story 3.5
    notes: ""
