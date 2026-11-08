---
feature: 1
title: Supabase alternative to Aurora RDS + private VPC (dev cost-reduction path)
status: confirmed    # prd-draft → brainstorm → confirmed → plan-draft → ready → in-progress → done
owner: syedemz
created: 2026-11-08
---

# Feature 1 — Supabase alternative to Aurora RDS + private VPC

## 1. Problem

The October AWS bill came in at **$207**, driven almost entirely by two line items:

| Service | Monthly cost | Why it costs this much |
|---|---|---|
| Relational Database Service (Aurora Serverless v2) | **+$131.02** | Cluster has `min_acu = 0` in dev, but storage, I/O, backups (`backup_retention_period = 7`), CloudWatch log ingestion, and secondary/standby capacity accrue even when scaled to zero. Aurora Serverless v2 is expensive to leave idle. |
| VPC | **+$36.50** | NAT Gateway hourly + per-GB; interface VPC endpoints (Secrets Manager, SSM, CloudWatch Logs, etc.) at ~$7.30/endpoint/month; the private VPC exists primarily to keep Lambdas able to reach Aurora. |

Together these are **~$167/month (~81% of the bill)** for a project in pre-launch testing.

The remainder — API Gateway, Lambda, Step Functions, DynamoDB, Cognito, AppSync, CloudWatch — is consumption-priced, low in dev, and stays.

## 2. Goal

Introduce **Supabase** as a swappable alternative to Aurora + private VPC, gated by a single Terraform flag `use_supabase`. The two stacks are **mutually exclusive**:

- `use_supabase = true` → terraform deploys Supabase credential secret + Lambdas (no VPC config); Aurora + VPC are **not deployed**; if they currently exist, `terraform apply` **destroys them**.
- `use_supabase = false` → terraform deploys Aurora + VPC + Lambdas (with VPC config); Supabase credential secret is **not deployed**; if it currently exists, `terraform apply` **destroys it**.

The current state of the real dev environment, confirmed on 2026-11-08, is that the Aurora cluster and private VPC have already been **manually torn down by the user**. This means `use_supabase = true` is the starting state for implementation. The flag was designed so that switching back to Aurora + VPC later — `use_supabase = false` then `terraform apply` — produces a fresh, clean Aurora + VPC deployment in a single step, with no orphaned state. Flipping in either direction must be idempotent.

## 3. Non-goals

- Replace DynamoDB. DynamoDB stays. It is cheap, it is regional-managed, it is not the cost driver.
- Replace Cognito, AppSync, Step Functions, SNS, SQS, S3. All stay.
- Change the production environment. Prod stays Aurora + VPC. This is a dev-only switch.
- Migrate existing Aurora data to Supabase. Dev data is disposable; `db_migrator` will reapply migrations against Supabase from scratch.
- Rewrite Lambda handlers. They must remain byte-identical.
- Introduce Supabase Auth, Realtime, Storage, or Edge Functions. Only Supabase's hosted Postgres is in scope.

## 4. Why this is a small change (architectural anchor)

Every Lambda that touches the database reaches it through one shared layer:
`infrastructure/src/layers/db/knotify_db/` — specifically `get_connection`, `set_rls_context`, `rls_context`.

Supabase hosts stock PostgreSQL. It accepts direct `psycopg2` connections. The RLS pattern we use (`SET LOCAL app.requesting_user_id`, `SET LOCAL app.requesting_user_sex`) is standard Postgres GUC behaviour and works identically on Supabase. `pgvector` (used by the match / deck pipeline) is supported on Supabase.

**Consequence:** substituting the connection builder in `knotify_db._db.get_connection` is sufficient to make every downstream Lambda target Supabase. No handler changes. No migration-script changes. No SQL changes.

## 5. Design (high level — brainstorm will refine)

### 5.1 The flag

A new root variable `use_supabase` (bool, default `false`) is added to `infrastructure/environments/dev/variables.tf` and surfaced in `dev.tfvars`. Prod ignores it (hardcoded `false`).

### 5.2 Terraform conditionality

Inside `infrastructure/environments/dev/main.tf`:

- `module "networking"` → `count = var.use_supabase ? 0 : 1`
- `module "aurora"` → `count = var.use_supabase ? 0 : 1`
- `module "iam_roles"` → inputs related to Aurora (`aurora_master_user_secret_arn`) become optional / conditional
- Every Lambda module that currently receives `vpc_config` from networking needs that `vpc_config` to become optional — when `use_supabase = true`, Lambdas deploy with no `vpc_config` block (public-internet egress)
- A new `module "supabase"` runs when `use_supabase = true`. It does *not* provision Supabase (Supabase is managed outside AWS). It only creates an AWS Secrets Manager secret holding the Supabase connection params (host, port, dbname, username, password, project_ref) so the existing Lambda-env-var contract (`AURORA_HOST`, `AURORA_PORT`, `AURORA_DBNAME` + secret ARN) is preserved by analogy.

### 5.3 Lambda code path

A new sibling file in the DB layer: `infrastructure/src/layers/db/knotify_db/_supabase.py`.

It mirrors the public surface of `_db.py` — same function names, same signatures, same return types (`psycopg2.extensions.connection`). The difference is:

- Reads env vars `SUPABASE_HOST`, `SUPABASE_PORT`, `SUPABASE_DBNAME` instead of `AURORA_HOST`...
- Connects to Supabase's **connection-pooler endpoint** (port `6543`, transaction mode) — this matters because Lambda cold starts open fresh connections and Supabase's direct-Postgres endpoint has a hard connection cap
- Uses `sslmode=require`
- The Secrets Manager secret payload format is still `{"username": ..., "password": ...}` to keep the contract uniform

The public `knotify_db/__init__.py` picks one implementation at import time based on the `USE_SUPABASE` env var:

```python
# Pseudo-code — will be TDD'd during implementation
if os.environ.get("USE_SUPABASE") == "true":
    from ._supabase import get_connection, set_rls_context, rls_context
else:
    from ._db import get_connection, set_rls_context, rls_context
```

RLS GUC setting logic is identical on both paths — can live in a shared `_rls.py` and be imported by both, so there is literally one implementation of the sensitive bit.

### 5.4 Migrations

`db_migrator` is a Lambda. In Supabase mode it points at Supabase's pooler endpoint using the password from the new `supabase_credential` secret. It runs the exact same migration files in `infrastructure/db/migrations/`. The user triggers it manually once after first Supabase deploy.

### 5.5 Lambdas affected (require redeploy with new env vars; **no handler code changes**)

From the current function tree, these touch Aurora and will inherit the `USE_SUPABASE=true` env var + Supabase secret ARN:

- `anonymize_chat_messages`
- `blocks`, `bookmarks`, `friends`, `profile` (phase 6)
- `match`, `refresh_deck_view` (phase 7)
- `chat_resolver` (phase 8 — also touches DynamoDB; DynamoDB path is unchanged)
- `cognito_post_confirmation`, `cognito_user_state`, `cognito_pre_token_generation` (phase 4)
- `db_migrator` (phase 3)
- `deactivate_chat_rooms`, `deletion_initiator`, `hard_delete_user_chat_messages`, `hard_purge`, `soft_delete_aurora`, `validate_deletion_request`, `write_audit_log` (phase 9)

Lambdas that do **not** touch Aurora (`delete_dynamodb_personal_data`, `notifications_publisher`, `push_fanout`, `push_tokens`, `room_state_publisher`, `stale_token_cleanup`) are untouched by this feature.

### 5.6 What gets saved when flag = true (expected)

| Cost item | Expected delta |
|---|---|
| Aurora Serverless v2 cluster | **-$131/mo** (not deployed) |
| NAT Gateway + VPC endpoints | **-$36/mo** (VPC not deployed) |
| Supabase (free tier) | **$0/mo** — free tier covers 500 MB DB, 2 GB egress |
| Net saving | **~$167/mo** |

Supabase paid tier (Pro) is $25/mo if we outgrow free; still a **~$140/mo saving**.

## 6. Decisions (resolved 2026-11-08 via `features/QA.txt`)

1. **~~In-place Aurora/VPC fate~~ → RESOLVED.** Aurora + private VPC were **manually torn down** by the user before implementation. `use_supabase = true` is the starting state. The flag is a true mutually-exclusive toggle (see §2). Switching back must be a one-command `terraform apply` with no manual cleanup.
2. **Supabase region → eu-central-1 (Frankfurt)**, matching the AWS region.
3. **Connection pooling mode → transaction mode (port 6543).** Our `SET LOCAL` RLS pattern wraps inside `BEGIN...COMMIT` so transaction mode is safe. A step-0 proof script will validate this before the main implementation starts.
4. **Secret storage → AWS Secrets Manager**, secret name `knotify/dev/supabase_credential`, payload `{"username": ..., "password": ...}`. Existing Lambda IAM + layer auth path reused.
5. **~~Schema drift~~ → RESOLVED. Fresh schema.** `db_migrator` Lambda reapplies all migrations against the empty Supabase project. No pg_dump / pg_restore. Aurora dev data is already gone.
6. **Testing strategy → existing unit tests untouched** (they use the `dict`-path in `get_connection` which doesn't care about Aurora vs. Supabase). **Existing integration tests run against Supabase manually** post-deploy; the integration harness env-vars are re-pointed by Terraform output and the test code itself does not change. If the Supabase connection builder needs its own unit coverage, those tests live in `infrastructure/src/layers/db/tests/test_supabase_unit.py`.
7. **Rollback → acceptable for dev.** If Supabase is down, flipping `use_supabase = false` and re-applying rebuilds Aurora + VPC in a single terraform run. Target: ≤15 minutes to restored dev DB.
8. **Branching → confirmed.** Triage uncommitted files on `ci/outputs-dev-on-main` → commit important ones → merge into `development` → cut `feat/feature-1-supabase-alternative` from `development`. PRD + implementation plan both land on that feature branch.

**New constraint the user added:** switching back to Aurora + VPC with a fresh deployment must be easy — i.e. every conditional in Terraform must be reversible, no orphaned IAM policies / Lambda env vars / module outputs when the flag flips either direction.

## 7. What I need from you to actually run Supabase

Confirmed on 2026-11-08: Supabase project exists. You still need to give me (or store yourself in AWS Secrets Manager under `knotify/dev/supabase_credential`):

1. **Project ref** — the `<project-ref>` subdomain of your Supabase project URL.
2. **Pooler host** — the `aws-0-eu-central-1.pooler.supabase.com`-style host from Project Settings → Database → Connection Pooling (port **6543**, transaction mode).
3. **Database password** — the one you set at project creation.

Clarifying the `anon` and `service_role` keys you asked about: these are PostgREST/Realtime API keys for Supabase's HTTP layer. **This feature does not use them.** Our Lambdas bypass PostgREST entirely and hit Postgres directly via `psycopg2`, so only the database password matters. Keep the keys safe but don't hand them over — they're out of scope.

Everything else — the Terraform flag plumbing, the `_supabase.py` connection builder, the Lambda env-var wiring, the IAM conditional, the smoke tests, the db_migrator run against the fresh Supabase schema — is on my side.

## 8. Acceptance criteria (draft — brainstorm will tighten)

- `use_supabase = false` → `terraform plan` on dev produces **zero diff** vs. today's state.
- `use_supabase = true` → `terraform plan` on a fresh workspace produces no Aurora resources, no VPC resources, no NAT, no VPC endpoints; produces one Supabase credential secret; every Aurora-using Lambda has `USE_SUPABASE=true` and `SUPABASE_*` env vars set.
- Running `db_migrator` with `use_supabase = true` against an empty Supabase project applies all existing migration files and exits 0.
- The existing phase-6, phase-7, phase-8, phase-9 smoke tests pass against the Supabase-backed stack with no test-code changes (test harness env vars re-pointed; no logic changes).
- No change in production environment plan output.
- Monthly AWS bill at next cycle, with flag = true and Aurora/VPC torn down, is below $50 (consumption-only tail).

## 9. Risks

- **Connection exhaustion.** Supabase free tier pooler caps are modest. Lambda concurrency bursts could exhaust them. Mitigation: pooler transaction mode + short connection TTL + alerting.
- **Latency.** Public-internet Postgres adds ~10–30ms vs. in-VPC Aurora. Acceptable for dev testing; a non-issue for prod because prod stays Aurora.
- **Feature drift.** Supabase Postgres may lag the exact Aurora Postgres major version. Our migrations target vanilla Postgres idioms, but `pgvector` version pinning should be verified against Supabase's offering during the brainstorm.
- **IAM scope.** Current Lambda IAM roles grant `secretsmanager:GetSecretValue` on the Aurora master secret ARN. With flag = true we need to add the new Supabase secret ARN to those policies (conditional).
- **RLS GUC under pooler transaction mode.** 95% certain this works (our code wraps `SET LOCAL` in a transaction); the brainstorm's step-0 proof is a 10-line script that opens a pooler connection, starts a tx, sets the GUC, queries, commits, and asserts the GUC was visible during the query.

## 10. Status / next actions

- [x] PRD drafted (this file)
- [x] User answered open questions §6 (see `features/QA.txt`) and confirmed Supabase project in eu-central-1
- [x] Aurora + VPC manually torn down by user — real dev state is now bare
- [ ] Implementation plan → produces `features/feature-1-supabase-alternative-plan.md` with `ready: false`
- [ ] User flips `ready: true` on the plan
- [ ] Triage uncommitted files on `ci/outputs-dev-on-main` → commit important ones → merge into `development`
- [ ] Cut `feat/feature-1-supabase-alternative` from `development`; move PRD + plan there
- [ ] User provides pooler host + password (or stores in AWS Secrets Manager directly)
- [ ] Implementation dispatched story-by-story on the feature branch
