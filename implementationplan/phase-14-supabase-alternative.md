phase: 14
title: Supabase alternative to Aurora RDS and private VPC (dev cost reduction; prod-capable)
last_updated: 2026-11-08 (phase 14 OPEN — stories 14.1-14.5a done; early plan verification run against dev caught 1 null-arn bug and expanded 14.6 scope; 14.6 is next; plan digest in features/feature-1-terraform-plan-output.md)

context_summary: |
  Introduces a single Terraform flag `use_supabase` that toggles the dev environment between
  (A) Aurora Serverless v2 + private VPC + NAT + VPC endpoints (current) and
  (B) Supabase hosted Postgres over the public internet (new).
  The two stacks are mutually exclusive: when the flag is on, Aurora + networking modules get
  count=0 and a `supabase` module provisions a single AWS Secrets Manager secret for the
  Supabase credential pair; when off, the current Aurora+VPC stack is deployed. Flipping in
  either direction is a single `terraform apply` with no manual cleanup — proven by story 14.15.

  Motivation: the October 2026 AWS bill was $207, of which Aurora (~$131) + VPC NAT + VPC
  endpoints (~$36) totalled ~$167 (81%) for a pre-launch environment. Aurora Serverless v2
  accrues storage, I/O, backups, and log ingestion even at min_acu=0. With use_supabase=true
  and the AWS stack torn down (already done manually by the user on 2026-11-08), the dev bill
  drops to ~$40/month on consumption-only services.

  Architectural anchor: every Aurora-using Lambda reaches the database through a single shared
  layer at infrastructure/src/layers/db/knotify_db/ (get_connection + set_rls_context + rls_context).
  Supabase hosts stock PostgreSQL that accepts direct psycopg2 connections and supports the same
  `SET LOCAL app.requesting_user_id` RLS GUC pattern from migration 0007 (verified on the
  Supabase pooler by story 14.1 before any implementation work begins). This means:
    - A new sibling file _supabase.py mirrors _db.get_connection with Supabase-specific env vars
      and sslmode=require.
    - RLS helpers are extracted into _rls.py as a pure refactor; both _db.py and _supabase.py
      import them so there is ONE implementation of the sensitive RLS code (wrong GUC names
      silently zero all query results — the sole-source-of-truth property matters).
    - knotify_db/__init__.py picks _db vs _supabase at import time based on the USE_SUPABASE
      env var. No Lambda handler code changes — handlers keep importing knotify_db exactly as
      they do today.

  Production posture: prod.tfvars is pinned use_supabase=false as a safety. The Terraform code
  is symmetric across dev and prod — a future "feature 2: production Supabase cutover" PRD
  would cover the prod-specific concerns (data migration via pg_dump, SLA/DPA, GDPR, latency,
  cost-crossover analysis, observability) without needing any code changes.

  Phase-wide acceptance:
    - `terraform plan` on dev with use_supabase=false produces zero diff vs. the current
      Aurora-torn-down state. Flag is off → no behaviour change.
    - `terraform plan` on dev with use_supabase=true produces no Aurora resources, no VPC
      resources, no NAT, no VPC endpoints; produces exactly one Supabase credential secret;
      every Aurora-using Lambda has USE_SUPABASE=true + SUPABASE_HOST/PORT/DBNAME env vars.
    - Every Lambda IAM role that reads a DB secret is scoped to whichever secret ARN is live
      for the current flag state (no over-broad wildcard, no orphaned ARN references).
    - Running db_migrator against an empty Supabase project applies all existing migration
      files (0000 through 0018) and exits 0. The RLS policy from migration 0007 behaves
      identically on Supabase as on Aurora.
    - The existing phase 6, 7, 8, 9 smoke tests pass against the Supabase-backed stack with
      zero test-code changes (test harness env vars re-pointed by Terraform outputs; test
      logic unchanged).
    - A reverse-direction proof (use_supabase=true → false → applied) in an ephemeral
      Terraform workspace completes without manual cleanup.
    - No change to prod plan output (prod.tfvars pinned false).

  Branch convention per gitbranching.md: one branch for the whole phase,
  feat/phase-14-supabase (naming derived by the backenddeveloper subagent at dispatch). The
  user reviews and merges the final PR into development.

stories:
  - id: 14.1
    title: Pre-flight proof — SET LOCAL app.requesting_user_id survives Supabase pooler transaction mode
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: null
    acceptance_criteria:
      - scripts/supabase_pooler_proof.py is created as a standalone Python script that opens a psycopg2 connection to the Supabase pooler (port 6543, transaction mode, sslmode=require), issues BEGIN, SET LOCAL app.requesting_user_id, SELECT current_setting(...), and COMMIT, asserting the GUC value survived to the SELECT
      - The script reads SUPABASE_HOST / PORT / DBNAME / USERNAME / PASSWORD from the environment — nothing is hard-coded, no secret enters the repo
      - User runs the script manually with real Supabase credentials and confirms PASS in the dispatch feedback before story 14.2 begins. A FAIL halts the phase; a different pooler mode (session) or direct-connection path must be evaluated in a separate design round
      - Script is committed to the phase branch
    notes: |
      PASS on 2026-11-08 via Supabase MCP execute_sql (user-directed alternative to running the committed script locally). The equivalent SQL batch
      `BEGIN; SET LOCAL app.requesting_user_id = 'some-test-uuid'; SELECT current_setting('app.requesting_user_id', true); COMMIT;`
      returned `guc_value = 'some-test-uuid'` inside the transaction. A follow-up `SELECT current_setting(...)` in a fresh statement returned `null`,
      confirming SET LOCAL (not SET SESSION) scoping — the value does not leak across transactions, which is the pooler-safety invariant.
      The committed script `scripts/supabase_pooler_proof.py` remains the reproducible artifact for anyone wanting to re-prove this against the pooler directly.

  - id: 14.2
    title: Add use_supabase + supabase_* variables to dev and pin prod false
    agent: backenddeveloper
    done: true
    depends_on: [14.1]
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/environments/dev/variables.tf declares use_supabase (bool, default false), supabase_host (string, default ""), supabase_port (number, default 6543), supabase_dbname (string, default "postgres"), supabase_username (string, default "")
      - infrastructure/environments/dev/dev.tfvars sets use_supabase=true, supabase_host and supabase_username placeholders are documented inline for the user to fill in before apply
      - infrastructure/environments/prod/prod.tfvars declares use_supabase=false explicitly with a safety-pin comment referencing this phase
      - Both environments pass `terraform fmt -check` and `terraform validate`
    notes: ""

  - id: 14.3
    title: New Terraform module — supabase (Secrets Manager secret only)
    agent: backenddeveloper
    done: true
    depends_on: [14.2]
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/modules/supabase/ contains main.tf, variables.tf, outputs.tf, tests/supabase.tftest.hcl
      - Module creates exactly one aws_secretsmanager_secret named "knotify/${var.environment}/supabase_credential" with a placeholder secret version (empty password) and lifecycle.ignore_changes=[secret_string] so operator-set passwords survive subsequent applies
      - Module does NOT provision anything Supabase-side (Supabase is a managed external service); it only owns the AWS-side credential plumbing so the Lambda layer's Secrets-Manager auth path is reused
      - Terraform test asserts: exactly one secret created, name shape matches knotify/<env>/supabase_credential, initial password field is empty, username field matches var.username, secret_arn output is non-empty
      - `terraform test` in the module directory passes 1 run, 0 failures
    notes: ""

  - id: 14.4
    title: Count-guard networking and aurora modules in dev/main.tf
    agent: backenddeveloper
    done: true
    depends_on: [14.2]
    tracking_issue: null
    acceptance_criteria:
      - module "networking" and module "aurora" blocks in infrastructure/environments/dev/main.tf get count = var.use_supabase ? 0 : 1 as their first attribute
      - Every reference to module.aurora.* and module.networking.* elsewhere in dev/main.tf is rewritten to one(module.aurora[*].*) / one(module.networking[*].*) so cross-module references survive count=0
      - `terraform plan` with use_supabase=false produces zero diff vs. the pre-change state (pure refactor when flag off)
      - `terraform plan` with use_supabase=true plans every aurora and networking resource for destroy (expected if those modules are currently deployed) or to no-op (if the AWS state is already torn down, which is the current reality on 2026-11-08)
      - prod/main.tf is unchanged unless prod also referenced module.aurora.* in ways that would break; if so, the same one(...) wrapping is applied in prod with prod's pinned-false flag making it a no-op
    notes: The detailed TDD step list for the reference-rewrite is in features/feature-1-supabase-alternative-plan.md story f1.3.

  - id: 14.5
    title: Wire supabase module + db-side locals in dev/main.tf
    agent: backenddeveloper
    done: true
    depends_on: [14.3, 14.4]
    tracking_issue: null
    acceptance_criteria:
      - module "supabase" is declared in infrastructure/environments/dev/main.tf after module "aurora", with count = var.use_supabase ? 1 : 0 and inputs sourced from var.supabase_host / port / dbname / username
      - A top-level locals block defines db_env_vars (map: USE_SUPABASE, plus either SUPABASE_HOST/PORT/DBNAME/DB_SECRET_ARN or AURORA_HOST/PORT/DBNAME/DB_SECRET_ARN depending on the flag) and db_vpc_config (null when flag is on; populated from networking outputs otherwise)
      - Every Lambda module invocation in dev/main.tf that currently sets environment_variables with AURORA_* keys is rewritten to environment_variables = local.db_env_vars (or merge(local.db_env_vars, {...}) if the Lambda carries extra bespoke env vars)
      - Every Lambda module invocation currently setting vpc_config is rewritten to vpc_config = local.db_vpc_config
      - Lambdas that do NOT touch the DB (delete_dynamodb_personal_data, notifications_publisher, push_fanout, push_tokens, room_state_publisher, stale_token_cleanup) are inspected individually; if they're currently in VPC solely for VPC-endpoint access, they also get local.db_vpc_config; otherwise left untouched
      - iam_roles module invocation receives aurora_master_user_secret_arn = one(module.aurora[*].master_user_secret_arn) and new input supabase_master_user_secret_arn = one(module.supabase[*].secret_arn)
      - `terraform plan` with use_supabase=true produces a Supabase secret create + SUPABASE_HOST env var on every Aurora-using Lambda; `terraform plan` with use_supabase=false produces zero diff vs. baseline
    notes: The locals pattern is the single-source-of-truth design choice that keeps per-Lambda edits mechanical and auditable. See features/feature-1-supabase-alternative-plan.md story f1.4 for the exact locals block body.

  - id: 14.5a
    title: Unify specialized Lambda modules (chat_resolver, refresh_deck_view, hard_purge, soft_delete_aurora) with the local.db_env_vars + local.db_vpc_config pattern
    agent: backenddeveloper
    done: true
    depends_on: [14.5]
    tracking_issue: null
    acceptance_criteria:
      - Each of the four modules — `infrastructure/modules/chat_resolver/`, `infrastructure/modules/refresh_deck_view/`, `infrastructure/modules/hard_purge/`, `infrastructure/modules/soft_delete_aurora/` — has its `variables.tf` rewritten to drop the typed `aurora_host` / `aurora_port` / `aurora_dbname` inputs and instead accept a single generic `db_env_vars` map input (same shape as `local.db_env_vars` in the env main.tf). The `vpc_config` input is changed from a non-nullable object to a nullable object so `null` is a legal value when `use_supabase=true`.
      - Each of the four modules has its `main.tf` rewritten so the inner `aws_lambda_function` (or the inner `module "lambda"` call, depending on the module's pattern) passes `environment_variables = merge(var.db_env_vars, { bespoke non-DB keys })` instead of constructing `AURORA_HOST = var.aurora_host` etc., and `vpc_config = var.vpc_config` (passed through unchanged, now possibly null).
      - Lambda handler code (`infrastructure/src/functions/<name>/handler.py`) is NOT modified. The `knotify_db` layer (stories 14.7-14.9) will route to Aurora or Supabase based on the `USE_SUPABASE` env var — handlers simply call `knotify_db.get_connection()` as they do today. If a handler reads `AURORA_HOST` directly instead of going through the layer, surface the pre-existing bug in the writeup and do NOT fix it in this story.
      - `environments/dev/main.tf` and `environments/prod/main.tf` call-sites for the four modules are rewritten to pass `db_env_vars = local.db_env_vars` and `vpc_config = local.db_vpc_config` instead of `aurora_host = one(module.aurora[*].cluster_endpoint)` + inline vpc_config blocks.
      - Each module's existing `.tftest.hcl` tests (where present) are updated to reflect the new input schema and continue to pass.
      - `terraform fmt -check` and `terraform validate` pass clean on both dev and prod envs.
      - `grep -nE 'aurora_host *=|aurora_port *=|aurora_dbname *=' infrastructure/environments/{dev,prod}/main.tf` returns zero hits — these four module call-sites were the last places those inputs were set.
    notes: |
      Surfaced during 14.5 as a known follow-up. 14.5 wired the locals pattern into seven standard Lambda modules (modules/lambda-pattern) but left these four specialized modules (each with its own hand-written `aws_lambda_function` resource and typed `aurora_*` inputs) untouched because the rewrite requires changing module-level variable schemas. Without 14.5a, `terraform plan` with `use_supabase=true` would fail at these four call-sites because `one(module.aurora[*].cluster_endpoint)` evaluates to `null` and Terraform rejects null for the modules' non-nullable string inputs. Must be resolved before 14.11's formal plan-verification story.

  - id: 14.6
    title: iam_roles conditional secret ARN via compact() + env-root null→"" coercion
    agent: backenddeveloper
    done: false
    depends_on: [14.5]
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/modules/iam_roles/variables.tf already declares supabase_master_user_secret_arn (string, default "") from the story 14.5 stub — verify the docstring references phase 14 and update if needed (the stub docstring is already phase-14-aware, so this may be a no-op).
      - infrastructure/environments/dev/main.tf and infrastructure/environments/prod/main.tf iam_roles module invocations rewrite the Aurora pass from `aurora_master_user_secret_arn = one(module.aurora[*].master_user_secret_arn)` to `aurora_master_user_secret_arn = try(one(module.aurora[*].master_user_secret_arn), "")` (and same pattern for supabase: `supabase_master_user_secret_arn = try(one(module.supabase[*].secret_arn), "")`). WHY: `one()` returns `null` when count=0 and Terraform rejects `null` in a `resources = [...]` list even if wrapped in `compact()` (compact filters empty strings, NOT nulls). Confirmed by the 2026-11-08 plan run captured in features/feature-1-terraform-plan-output.md.
      - Every statement in infrastructure/modules/iam_roles/main.tf that grants secretsmanager:GetSecretValue on var.aurora_master_user_secret_arn has its Resource list rewritten to compact([var.aurora_master_user_secret_arn, var.supabase_master_user_secret_arn]) — exactly one ARN is non-empty for any given flag state, so compact() yields a single-element list.
      - No statement has its scope widened to Resource="*" — if any pre-existing statement was already wildcarded, it is NOT re-scoped here and the discrepancy is surfaced in a questions/ file for a follow-up.
      - `terraform plan -var-file=dev.tfvars -var 'use_supabase=true'` on dev produces ZERO errors (the single pre-14.6 error `module.iam_roles.data.aws_iam_policy_document.db_migrator_secrets: Null value found in list` is now gone). Verified via the user's temp credentials or deferred to story 14.11 if credentials are unavailable.
      - `terraform plan -var-file=dev.tfvars -var 'use_supabase=false'` on dev remains clean (304 resources, 0 errors — no regression on the Aurora path).
      - prod plan output is structurally unchanged (prod state bucket does not exist per docs/PROD_CUTOVER.md; prod verification is validate-only until prod bootstrap).
    notes: Scope was expanded on 2026-11-08 after the early plan verification run surfaced the null-value-in-list bug that compact() alone cannot fix. The env-root try() coercion is the upstream fix; the compact() module rewrite is the downstream consumer. Both must land together.

  - id: 14.7
    title: Extract RLS GUC helpers from _db.py into shared _rls.py (pure refactor)
    agent: backenddeveloper
    done: false
    depends_on: []
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/src/layers/db/knotify_db/_rls.py is created containing the _GUC_USER_ID and _GUC_USER_SEX constants (verbatim from migration 0007 lines 115-116) and the set_rls_context + rls_context implementations
      - infrastructure/src/layers/db/knotify_db/_db.py no longer defines those four symbols directly; instead imports them from knotify_db._rls and re-exports (to preserve the existing public surface of _db.py)
      - The lift is byte-identical for the GUC constants and the SQL strings — a one-character deviation silently fails-closes the RLS policy to zero rows (migration 0007 docstring is the authority; do NOT retype from memory)
      - A new tests/test_rls_shared.py asserts _rls exposes _GUC_USER_ID == "app.requesting_user_id", _GUC_USER_SEX == "app.requesting_user_sex", and the signatures of set_rls_context and rls_context
      - All existing tests in infrastructure/src/layers/db/tests/ continue to pass (both test_knotify_db_unit.py and test_knotify_db_integration.py — the latter requires the local Postgres fixture from conftest.py)
      - This story's diff must contain zero behavioural change — it's a refactor safety-netted by the existing test suite
    notes: Parallelizable with Terraform work (14.2-14.6). Safe to dispatch in parallel if the subagent-driven-development workflow supports it.

  - id: 14.8
    title: Create _supabase.py connection builder
    agent: backenddeveloper
    done: false
    depends_on: [14.7]
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/src/layers/db/knotify_db/_supabase.py is created with a single public function get_connection(secret_or_env) that mirrors _db.get_connection's signature: accepts a dict (local/test) or a Secrets Manager ARN/name (Lambda runtime)
      - Lambda runtime path reads SUPABASE_HOST / SUPABASE_PORT / SUPABASE_DBNAME from the environment; a missing env var raises EnvironmentError with the var name in the message
      - Secret payload format is {"username": ..., "password": ...} — identical to the Aurora master secret contract so the Lambda-side auth path does not need a backend-specific branch
      - psycopg2.connect is called with sslmode="require" (Supabase enforces TLS; plaintext fails at the pooler)
      - set_rls_context and rls_context are re-exported from _rls (NOT redefined) so the same RLS implementation serves both backends
      - tests/test_supabase_unit.py covers the dict path (all five fields forwarded correctly, sslmode=require enforced), the Secrets Manager path (env vars read, creds parsed, psycopg2 called with correct kwargs, EnvironmentError raised on missing env var), and asserts set_rls_context / rls_context are the same objects as the _rls versions
      - All unit tests pass; psycopg2 and boto3 are mocked — no real Supabase or AWS call in unit tests
    notes: Secret payload symmetry with Aurora is deliberate — keeps the authentication fetch logic in the Lambda runtime free of backend branching.

  - id: 14.9
    title: Conditional backend dispatch in knotify_db/__init__.py
    agent: backenddeveloper
    done: false
    depends_on: [14.7, 14.8]
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/src/layers/db/knotify_db/__init__.py imports get_connection from _supabase if os.environ.get("USE_SUPABASE") == "true", else from _db
      - set_rls_context and rls_context are always imported from _rls — no backend branching for the RLS helpers
      - PREFERENCE_KEYS and encode_prefs imports from .prefs are unchanged
      - __all__ lists all five public names exactly as before
      - A new tests/test_dispatch_unit.py covers: env unset → aurora path, env="false" → aurora path, env="true" → supabase path, RLS helpers always from _rls regardless of env value
      - All existing knotify_db tests continue to pass
    notes: Case-sensitive comparison on "true" is deliberate — matches the Terraform-emitted string. Document in the runbook.

  - id: 14.10
    title: Rebuild DB Lambda layer zip to include _rls.py and _supabase.py
    agent: backenddeveloper
    done: false
    depends_on: [14.7, 14.8, 14.9]
    tracking_issue: null
    acceptance_criteria:
      - infrastructure/src/layers/db/build.sh produces build/knotify-db-layer.zip that includes knotify_db/_rls.py and knotify_db/_supabase.py (verified with `unzip -l`)
      - If build.sh packages by directory (cp -r), no manifest edit is needed; if it packages by explicit file list (layer_manifest.txt), the two new files are added and the manifest is committed
      - The rebuilt zip's size is within 10% of the previous zip (sanity check — no accidental bloat from vendored deps)
      - A `terraform plan` after the rebuild shows the knotify-db layer resource with a changed source_code_hash and the aws_lambda_function resources attached to it marked for update (expected: new layer version will be published)
    notes: ""

  - id: 14.11
    title: Terraform plan verification for both flag values
    agent: backenddeveloper
    done: false
    depends_on: [14.5, 14.6, 14.10]
    tracking_issue: null
    acceptance_criteria:
      - `terraform plan -var 'use_supabase=true'` on dev produces (a) exactly one aws_secretsmanager_secret.credential to CREATE, (b) zero aurora and networking resources to create, (c) SUPABASE_HOST env var on every Aurora-using Lambda
      - `terraform plan -var 'use_supabase=false'` on dev produces (a) aurora and networking resources to CREATE (because the current state has them torn down), (b) zero supabase module resources, (c) AURORA_HOST env var on every Aurora-using Lambda
      - Plan digests (resource counts + env-var spot-checks) are captured in features/feature-1-terraform-plan-output.md and committed to the phase branch as evidence
      - `terraform plan` on prod produces zero diff (prod pinned false; symmetric code should be a no-op)
    notes: This is the proof-of-readiness story before any real apply. A diff on prod here is a bug — surface and fix before proceeding.

  - id: 14.12
    title: Apply use_supabase=true on dev and populate the Supabase secret
    agent: backenddeveloper (with user approval gate before apply)
    done: false
    depends_on: [14.11]
    tracking_issue: null
    acceptance_criteria:
      - User fills real pooler host (e.g. aws-0-eu-central-1.pooler.supabase.com) and pooler username (e.g. postgres.<project-ref>) into dev.tfvars and commits that edit as a separate commit on the phase branch
      - Subagent requests explicit user approval before `terraform apply`; do not auto-approve
      - `terraform apply` on dev completes with zero errors; outputs include the new Supabase credential secret ARN
      - User (or subagent, under user direction) runs `aws secretsmanager put-secret-value --secret-id knotify/dev/supabase_credential --secret-string '{"username":"postgres.<ref>","password":"<pw>"}'` to populate the real password — the password never enters Terraform state (lifecycle.ignore_changes guarantees it)
      - Verification query (aws secretsmanager get-secret-value, parse JSON, print {username, password_set: bool}) confirms the secret is populated without leaking the password to logs
    notes: First real AWS state change in this phase. Treat with care; require user go-ahead.

  - id: 14.13
    title: Invoke db_migrator against fresh Supabase project
    agent: backenddeveloper
    done: false
    depends_on: [14.12]
    tracking_issue: null
    acceptance_criteria:
      - `aws lambda invoke --function-name knotify-dev-db-migrator ...` returns statusCode 200
      - CloudWatch logs for the invocation show each migration file 0000 through the latest applied in order with no error lines
      - A read-only query against Supabase (via the pooler using the proof-script pattern) confirms the canonical knotify table set is present: users, siblings, friendships, friend_requests, bookmarks, blocks, deck_view, ChatRooms-related tables, audit_log, yoyo migrations metadata
      - pgvector extension is enabled on Supabase and the vector columns used by match / deck exist
      - RLS policy from migration 0007 is in place — a smoke-level query as a non-privileged role with the GUC set to a specific user_id returns only that user's rows
      - Table list + migration count digest is appended to features/feature-1-terraform-plan-output.md and committed
    notes: The pgvector check is explicit because Supabase's pgvector version might lag Aurora's; a version mismatch would surface as a migration error here.

  - id: 14.14
    title: Phase 6, 7, 8, 9 smoke tests against the Supabase-backed stack
    agent: backenddeveloper
    done: false
    depends_on: [14.13]
    tracking_issue: null
    acceptance_criteria:
      - probe_phase6.py (profile, friends, bookmarks, blocks), probe_phase7.py (match + deck + pgvector), probe_phase8.py (chat — Supabase + DynamoDB mix), and probe_phase9.py (account deletion — the broadest test) all exit 0 against the dev stack running on Supabase
      - Smoke-test code is not modified — the test harness picks up the new endpoint IDs from Terraform outputs; test logic runs unchanged
      - Pass/fail per phase is recorded as a table in features/feature-1-terraform-plan-output.md and committed
      - If ANY probe fails: halt the phase here, do not advance to 14.15, surface the failure, and invoke systematic-debugging per the superpowers skill
    notes: probe_phase9.py is the most comprehensive — it exercises Step Functions, multiple Lambdas, Supabase soft-delete + hard-purge paths, DynamoDB membership cleanup, Cognito, and the audit log. Green here is high-confidence Supabase integration.

  - id: 14.15
    title: Reverse-direction proof — use_supabase=true → false in an ephemeral workspace
    agent: backenddeveloper
    done: false
    depends_on: [14.14]
    tracking_issue: null
    acceptance_criteria:
      - A throwaway Terraform workspace (e.g. `terraform workspace new feature1-reverse-proof`) is used so the real dev state is not touched
      - `terraform apply -var 'use_supabase=false'` from a Supabase-live state completes without manual cleanup: Aurora + networking are provisioned fresh, the Supabase credential secret is destroyed in the same apply, IAM policy Resource lists swap from the Supabase ARN to the Aurora ARN
      - Post-apply `terraform state list` shows non-zero aurora + networking resources and zero supabase resources
      - The ephemeral workspace is destroyed and deleted after the proof (`terraform destroy -auto-approve && terraform workspace delete feature1-reverse-proof`) so no stray AWS resources or workspace state remain
      - The reverse-direction proof result is captured in features/feature-1-terraform-plan-output.md with timestamps and resource counts, and committed
    notes: This story directly satisfies the user's explicit constraint "switching back to Aurora and private VPC with a fresh deployment should be easy". The proof must run in an isolated workspace so a mistake does not clobber the real dev state.

  - id: 14.16
    title: Operator runbook
    agent: backenddeveloper
    done: false
    depends_on: []
    tracking_issue: null
    acceptance_criteria:
      - features/supabase-runbook.md is created with sections: Flipping the flag Aurora → Supabase (step-by-step), Flipping the flag Supabase → Aurora (step-by-step), Rotating the Supabase password, Rotating the Aurora password, Troubleshooting (Lambda-cannot-connect, RLS-returns-zero-rows), and a Cost-sanity-check table with the expected monthly bill for each flag state
      - Each step in the "Flipping the flag" procedures is executable copy-paste text, not prose instructions — a new operator who has never seen the project can follow along
      - The runbook does NOT duplicate the detailed story-by-story content of features/feature-1-supabase-alternative-plan.md; it's a short operational manual for post-implementation life
      - The runbook is committed on the phase branch
    notes: Writing the runbook is independent of the Terraform + Python work and can be dispatched in parallel with 14.7-14.10 if the workflow allows.
