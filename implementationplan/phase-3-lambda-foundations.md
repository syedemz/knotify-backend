phase: 3
title: Lambda foundations
last_updated: 2026-05-21

context_summary: |
  Builds the shared Lambda substrate every later phase reuses. Establishes the lambda Terraform module pattern (function definition, IAM role template, log group with 7-day retention per the owner's observability directive), two shared Lambda layers (one for observability/JWT helpers, one for Aurora access with psycopg2-binary and pgvector and the RLS session-GUC setter), least-privilege IAM role templates per access pattern, local dev tooling (packaging, integration testing against a containerized Postgres), and the knotify-cognito-post-confirmation Lambda. The Cognito trigger Lambda is built and unit-tested here but not wired to a Cognito User Pool — that happens in phase 4, ensuring signup works end-to-end the moment Cognito ships.

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
      - Module infrastructure/modules/iam_roles/ exposes named role builders aurora_reader, aurora_writer, dynamodb_chat_writer, dynamodb_notifications_writer, stepfn_task, cognito_trigger; each attaches the minimal set of managed and inline policies
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
      - The Lambda is deployed via the lambda module with the observability and db layers attached, IAM role "cognito_trigger", VPC config wired to phase 1 private subnets, environment variable DB_SECRET pointing at an env var for now (Secrets Manager migration is in phase 11)
      - terraform plan in dev shows the function created but no aws_cognito_user_pool_lambda_config wiring (intentional — wiring is in phase 4)
    notes: ""
