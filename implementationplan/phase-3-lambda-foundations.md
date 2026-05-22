phase: 3
title: Lambda foundations
last_updated: 2026-05-23

context_summary: |
  Builds the shared Lambda substrate every later phase reuses. Establishes the lambda Terraform module pattern (function definition, IAM role template, log group with 7-day retention per the owner's observability directive), two shared Lambda layers (one for observability/JWT helpers, one for Aurora access with psycopg2-binary and pgvector and the RLS session-GUC setter), least-privilege IAM role templates per access pattern, local dev tooling (packaging, integration testing against a containerized Postgres), the DB migrator Lambda that runs yoyo against the dev Aurora cluster from inside the VPC (the cluster-side migration work that was deferred from phase 2 per the 2026-05-23 phase-2 brainstorm), and the knotify-cognito-post-confirmation Lambda. The Cognito trigger Lambda is built and unit-tested here but not wired to a Cognito User Pool — that happens in phase 4, ensuring signup works end-to-end the moment Cognito ships. By the end of this phase, the dev Aurora cluster has the full §5.1 schema applied (users, siblings, friendships, friend_requests, bookmarks, blocks, deck_view, triggers, RLS policy, app_user role).

stories:
  - id: 3.1
    title: Lambda Terraform module skeleton
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/lambda/main.tf accepts inputs function_name, handler, runtime, architectures, layers, environment_variables, vpc_config, role_arn, and creates aws_lambda_function, aws_cloudwatch_log_group with retention_in_days=7 and name "/aws/lambda/${var.function_name}", and an alias "live"
      - Default runtime is "python3.14", default architectures is ["arm64"], default timeout 10s, default memory_size 512
      - terraform validate passes; a smoke instantiation with a dummy zip file plans cleanly
    notes: ""

  - id: 3.2
    title: Shared observability layer (Powertools, logger, JWT)
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Directory src/layers/observability/ contains a build script that produces a python/ directory with aws-lambda-powertools (latest compatible with Python 3.14), python-jose or PyJWT, and a thin wrapper module knotify_obs with helpers init_logger(service), correlation_id_middleware, verify_cognito_jwt(token, user_pool_id, region)
      - Building the layer produces a .zip artifact and Terraform packages it as aws_lambda_layer_version with compatible_runtimes=["python3.14"] and compatible_architectures=["arm64"]
      - Unit test verify_cognito_jwt rejects a token with wrong issuer, wrong audience, expired exp, and accepts a known-good signed token
    notes: ""

  - id: 3.3
    title: Shared Aurora-access layer (psycopg2, pgvector, RLS GUC helper)
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Directory src/layers/db/ packages psycopg2-binary (manylinux2014_aarch64 wheel), pgvector Python client, and module knotify_db exposing get_connection(secret_or_env), set_rls_context(conn, user_id, user_sex), and a context manager that resets the GUCs on exit
      - Integration test against the local Postgres container from phase 2 calls set_rls_context with a Male user and asserts that a follow-up SELECT FROM users returns only female rows plus the male's own row
      - Built as aws_lambda_layer_version with compatible_runtimes=["python3.14"], compatible_architectures=["arm64"]
    notes: ""

  - id: 3.4
    title: IAM role templates per access pattern
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - Module infrastructure/modules/iam_roles/ exposes named role builders aurora_reader, aurora_writer, dynamodb_chat_writer, dynamodb_notifications_writer, stepfn_task, cognito_trigger, and db_migrator; each attaches the minimal set of managed and inline policies
      - The db_migrator role attaches AWSLambdaVPCAccessExecutionRole, allows secretsmanager:GetSecretValue scoped to the Aurora master_user_secret_arn from phase 2's aurora module output, and grants kms:Decrypt on the KMS key that encrypts that secret (the AWS-managed `aws/secretsmanager` key unless overridden). It does NOT need Aurora IAM-database-auth permissions because the migrator connects as the Aurora master via Secrets Manager credentials.
      - Each role includes the AWSLambdaVPCAccessExecutionRole managed policy so the Lambda can attach an ENI
      - terraform validate passes; a unit test (.tftest.hcl) instantiates each role and asserts the attached policy ARNs match expectations
    notes: ""

  - id: 3.5
    title: Local development tooling
    agent: backenddeveloper
    done: false
    depends_on: [3.2, 3.3]
    acceptance_criteria:
      - A Makefile target "make package FUNC=<name>" produces a Lambda deployment .zip under build/ excluding layer-provided dependencies
      - A pytest configuration runs unit tests for any src/functions/<name>/ folder and integration tests against the Postgres container from phase 2
      - "make test" exits zero with at least the verify_cognito_jwt and set_rls_context tests from stories 3.2 and 3.3 included
      - README at src/ documents the workflow
    notes: ""

  - id: 3.6
    title: knotify-cognito-post-confirmation Lambda (built, not wired)
    agent: backenddeveloper
    done: false
    depends_on: [3.1, 3.2, 3.3, 3.4, 3.5]
    acceptance_criteria:
      - Source directory src/functions/cognito_post_confirmation/ contains a Lambda handler that receives a Cognito PostConfirmation_ConfirmSignUp event, extracts user_id (sub), email, phone_number, preferred_username, given_name, family_name, gender (mapped to sex), birthday from user attributes, and inserts a users row using ON CONFLICT (user_id) DO NOTHING for idempotency
      - The handler does NOT return an error when the row already exists (re-runs are no-ops)
      - Integration test against the Postgres container simulates two successive invocations with the same event payload and asserts exactly one users row exists
      - Integration test simulates an event missing a required attribute and asserts the handler returns the event unmodified after logging a structured error (so Cognito does not roll back the signup)
      - The Lambda is deployed via the lambda module with the observability and db layers attached, IAM role "cognito_trigger", VPC config wired to phase 1 private subnets, environment variable DB_SECRET_ARN pointing at the Aurora master_user_secret_arn from phase 2 (the cognito_trigger role grants secretsmanager:GetSecretValue on that ARN)
      - terraform plan in dev shows the function created but no aws_cognito_user_pool_lambda_config wiring (intentional — wiring is in phase 4)
    notes: ""

  - id: 3.7
    title: DB migrator Lambda and initial cluster-side migration run
    agent: backenddeveloper
    done: false
    depends_on: [3.1, 3.4]
    acceptance_criteria:
      - Source directory src/functions/db_migrator/ contains a Lambda handler that (a) reads env var DB_SECRET_ARN, (b) fetches the secret via boto3 secretsmanager:GetSecretValue, (c) parses host/port/username/password/dbname from the Aurora-managed JSON, (d) constructs a Postgres connection URL, (e) shells out to or imports yoyo-migrations to run `yoyo apply` against the cluster using the migrations directory baked into the deploy package, (f) returns a structured JSON response listing applied migration ids, pending migrations (should be zero on success), and total elapsed time
      - The function packages the migrations directory from infrastructure/db/migrations/ into the Lambda deployment .zip (or mounts it via a dedicated layer) so a single deploy artifact contains both the runner and the SQL files
      - The function bundles yoyo-migrations and psycopg2-binary (manylinux2014_aarch64 wheel) in its package or via the shared db layer from story 3.3 — whichever the implementer judges cleaner. The acceptance test is that `yoyo apply` runs inside the Lambda without ModuleNotFoundError.
      - The function is deployed via the lambda module with IAM role "db_migrator" (from story 3.4), VPC config wired to phase 1 private subnets and the aurora_security_group_id (so the ENI can reach Aurora on port 5432), memory_size 1024, timeout 300s (migrations are bounded but allow headroom for the largest migration plus the HNSW index build on the users table)
      - The function is invoked once against the dev Aurora cluster as part of phase 3's apply flow — either by a `null_resource` with `local-exec` (`aws lambda invoke --function-name knotify-db-migrator-dev --payload '{}' out.json && cat out.json`) wired to depend on the migrator Lambda + the Aurora cluster, OR by a dedicated step in `.github/workflows/deploy.yml` after the apply-dev job. The story's PR description records which path was chosen.
      - After the first invocation succeeds, `psql -h <dev-aurora-endpoint> -U app_user -c "\d users"` (or an equivalent SELECT against information_schema.tables) returns the §5.1 users table definition. Verification can be performed by a second null_resource that runs a one-shot SQL query via the migrator Lambda itself (e.g., payload `{"command": "verify"}`) — no human bastion access required.
      - A second invocation of the migrator on an unchanged cluster exits successfully and reports zero pending migrations (idempotency check).
      - prod migrator Lambda is also authored in infrastructure/environments/prod/main.tf, but is not deployed (apply gated per PROD_CUTOVER.md, consistent with phase 2 story 2.14). The prod invocation step in deploy.yml is similarly gated.
      - Integration test (run against the local Postgres container) validates the Lambda's handler logic by mocking boto3 SecretsManager and asserting that yoyo applies migrations correctly. This test runs as part of `make test` per story 3.5.
    notes: ""
