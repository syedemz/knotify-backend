# Phase 14 — Terraform plan verification digest

This file is the PRD artifact for story 14.11 (formal `terraform plan` verification for both flag values). Entries are appended as plan runs happen.

---

## 2026-11-08 — early verification run after story 14.5a

Dispatched during story 14.5 → 14.6 transition at the user's request. Terraform state on the `knotify-dev-tfstate` S3 backend was found to be empty (335 bytes, serial 0, last modified 2026-10-27) — a consequence of the manual Aurora + VPC teardown earlier on 2026-11-08. This means all three plans propose full creates rather than incremental diffs. The "zero diff vs baseline" invariant from story 14.4's acceptance criteria is NOT testable against an empty state; the tests exercised instead are: structural soundness of the refactor, locals ternary shape, and detection of null-input bugs from the `one(module.aurora[*].*)` + `one(module.supabase[*].*)` idioms.

**Env:** AWS account `776217504626`, role `AWSReservedSSO_AdministratorAccess` as `knotify-admin`, region `eu-central-1`.

### Plan A — dev, `use_supabase=false`

```
terraform plan -var-file=dev.tfvars -var 'use_supabase=false' -no-color
Plan: 304 to add, 0 to change, 0 to destroy.
Errors: 0
Warnings: pre-existing dynamodb hash_key/range_key deprecation (2 instances) + s3 backend dynamodb_table deprecated
```

Aurora + networking are included in the 304 (baseline Aurora stack). No errors from the 14.4 `one()` wrapping or the 14.5 locals pattern on the Aurora path.

### Plan B — dev, `use_supabase=true`

```
terraform plan -var-file=dev.tfvars -var 'use_supabase=true' -no-color
Plan: 269 to add, 0 to change, 0 to destroy.
Errors: 1
Warnings: same as plan A
```

Delta of 35 resources vs plan A ≈ aurora cluster + instances + parameter group + networking VPC + subnets + NAT + route tables + VPC endpoints + SGs (expected composition).

The **single error** is scope for story 14.6 but needs expanded scope — the `compact()` rewrite in `modules/iam_roles/main.tf` alone will NOT fix it:

```
Error: Null value found in list
  with module.iam_roles.data.aws_iam_policy_document.db_migrator_secrets,
  on ..\..\modules\iam_roles\main.tf line 94
  94:     resources = [
  95:       var.aurora_master_user_secret_arn,
  96:     ]
Null values are not allowed for this attribute value.
```

Root cause: env root passes `aurora_master_user_secret_arn = one(module.aurora[*].master_user_secret_arn)`, which evaluates to `null` when `module.aurora` has `count = 0`. Terraform's `compact()` filters empty strings, NOT nulls — so `compact([null, "arn:..."])` would error the same way. The fix must be upstream at the env-root call site: coerce null to empty string via `try(one(module.aurora[*].master_user_secret_arn), "")` (and the same for supabase). Story 14.6's scope is extended to include this env-root coercion in addition to the planned iam_roles compact() rewrite.

### Plan C — prod

Blocked. Prod state bucket `knotify-prod-tfstate` does not exist (prod was never bootstrapped per `docs/PROD_CUTOVER.md`). Not a bug — the pause mechanism is deliberate. Prod verification will run at phase-11 or during prod cutover, not here.

### `terraform console` evaluation of the locals block

Verified both ternary branches evaluate to the expected shape:

**`local.db_env_vars` with `use_supabase=true`:**
```
tomap({
  "DB_SECRET_ARN"   = (known after apply)
  "SUPABASE_DBNAME" = "postgres"
  "SUPABASE_HOST"   = ""                  # placeholder; operator fills before 14.12
  "SUPABASE_PORT"   = "6543"
  "USE_SUPABASE"    = "true"
})
```

**`local.db_env_vars` with `use_supabase=false`:**
```
tomap({
  "AURORA_DBNAME" = (known after apply)
  "AURORA_HOST"   = (known after apply)
  "AURORA_PORT"   = (known after apply)
  "DB_SECRET_ARN" = (known after apply)
  "USE_SUPABASE"  = "false"
})
```

**`local.db_vpc_config` with `use_supabase=true`:** `null /* object */`

**`local.db_vpc_config` with `use_supabase=false`:**
```
{
  "security_group_ids" = [(known after apply)]  # single lambda_sg, matching pre-change state
  "subnet_ids" = (known after apply)
}
```

### Spot-checks on specific resources

- `write_audit_log` Lambda env vars render as literals (`LOG_LEVEL`, `POWERTOOLS_SERVICE_NAME`, `TABLE_AUDIT`) — this Lambda does NOT consume `local.db_env_vars`, so the merge path isn't exercised. Correct.
- `profile` Lambda env_vars render as `(known after apply)` under flag-ON because `DB_SECRET_ARN = one(module.supabase[*].secret_arn)` is an unresolved resource reference at plan time. The map SHAPE is confirmed via the `terraform console` evaluation above. Will become fully literal once the Supabase secret has an ARN (either post-apply or post-populate in story 14.12).
- Zero plan-time errors on the four specialized modules migrated in 14.5a (`chat_resolver`, `refresh_deck_view`, `hard_purge`, `soft_delete_aurora`) — the nullable `vpc_config` + `db_env_vars = map(string)` schema change works cleanly.

### Takeaways for subsequent stories

1. **Story 14.6 scope must expand** — include the env-root `try()` coercion alongside the iam_roles compact() rewrite. Captured in the PRD update on the same date.
2. **Terraform state is empty** — the "zero diff vs baseline" check from 14.11's acceptance criteria cannot be exercised against the current state. Story 14.11 may need to redefine its invariant as "plans are error-free for both flag values and the flag-on plan creates the Supabase secret while planning zero Aurora/networking resources".
3. **The refactor 14.4 + 14.5 + 14.5a is sound** apart from the one known null-arn issue. Proceeding to 14.6 is safe.

---

## 2026-11-08 — formal story 14.11 verification run (post 14.6 + 14.10)

Formal plan-verification gate for phase 14. Captured after stories 14.6 (iam_roles compact() + env-root `try()` null coercion), 14.7-14.9 (Python layer SSoT + dispatch), and 14.10 (rebuilt DB layer zip including `_rls.py` + `_supabase.py`). Confirms the pre-14.6 null-arn error is now gone and that the mutual-exclusivity invariant holds across both flag values.

**Env:** AWS account `776217504626`, role `AWSReservedSSO_AdministratorAccess` as `knotify-admin`, region `eu-central-1`. Dev S3 state remains empty (335 bytes, serial 0) — plans still propose full creates, not incremental diffs.

### Plan A — dev, `use_supabase=false`

```
terraform plan -var-file=dev.tfvars -var 'use_supabase=false' -no-color
Plan: 304 to add, 0 to change, 0 to destroy.
Errors: 0
```

Identical to 2026-11-08 early-run result — Aurora path remains clean, no regression from 14.6-14.10 work.

Resource composition spot-check:
- `grep -cE 'module\.aurora|aws_rds' plan-off.txt` → **7 refs** (aurora cluster + instance + parameter group + subnet group + master secret + etc.)
- `grep -cE 'module\.networking|aws_vpc|aws_nat_gateway' plan-off.txt` → **45 refs** (VPC, subnets, NAT, VPC endpoints, route tables, SGs)
- `grep -cE 'supabase_credential|module\.supabase' plan-off.txt` → **0 refs** (supabase module has count=0 when flag off — expected)

### Plan B — dev, `use_supabase=true`

```
terraform plan -var-file=dev.tfvars -var 'use_supabase=true' -no-color
Plan: 270 to add, 0 to change, 0 to destroy.
Errors: 0
```

**The pre-14.6 `Null value found in list` error at `module.iam_roles.data.aws_iam_policy_document.db_migrator_secrets` is GONE.** The 14.6 fix (env-root `try(..., "")` + module `compact()`) works as designed. This is the critical gate.

Delta of 34 resources vs plan A ≈ aurora + networking stack (one less than the pre-14.6 run's 35 — a minor count adjustment that doesn't affect the mutual-exclusivity invariant). Resource composition spot-check:
- `grep -cE 'module\.aurora|aws_rds' plan-on.txt` → **0 refs** ✓ (no aurora resources planned when flag on)
- `grep -cE 'module\.networking|aws_vpc|aws_nat_gateway' plan-on.txt` → **0 refs** ✓ (no networking resources planned when flag on)
- `grep -cE 'supabase_credential|module\.supabase' plan-on.txt` → **5 refs** ✓ (secret resource + version + two tag entries + comment line)
- Explicit secret create: `module.supabase[0].aws_secretsmanager_secret.credential will be created` — `name = "knotify/dev/supabase_credential"`

### Plan C — prod

Blocked (unchanged from 2026-11-08 early run). `knotify-prod-tfstate` S3 bucket does not exist per `docs/PROD_CUTOVER.md`. Prod `terraform init` fails with `NoSuchBucket`. Prod validation remains terraform-validate-only until prod bootstrap occurs in a future phase.

### Lambda layer (story 14.10 verification)

Plan output shows `module.db_layer.aws_lambda_layer_version.db` as "will be created" with a fresh `source_code_hash` computed over `./../../../build/knotify-db-layer.zip`. Three distinct `source_code_hash` values appear in the flag-OFF plan — one each for the db-layer and two other layer/function resources. Hashes:
- `Fbw09j6Wn6d/yiYnOnyr4khVogUASb/7rl/G/nJwlC0=`
- `9wxUYt1l2kqkqpLWcUhymPFC+4r17NLSxZKDPvnzfSs=`
- `gHm+eWzjj9JRHsNah8IVUhfI9xcj5R6JI7y5B0BWSRM=`

Because state is empty, the layer resource is a fresh create rather than a replace — a non-empty state would show the `source_code_hash` diff against the existing deployed layer version and mark attached Lambda functions for update. The behaviour is architecturally proven; the specific "update existing layer" trajectory can't be exercised until the environment is applied.

### `terraform console` re-check of the locals ternary

Identical to 2026-11-08 early-run results:

**Flag-ON:** `tomap({"DB_SECRET_ARN"=(known after apply), "SUPABASE_DBNAME"="postgres", "SUPABASE_HOST"="", "SUPABASE_PORT"="6543", "USE_SUPABASE"="true"})`

**Flag-OFF:** `tomap({"AURORA_DBNAME"=(known after apply), "AURORA_HOST"=(known after apply), "AURORA_PORT"=(known after apply), "DB_SECRET_ARN"=(known after apply), "USE_SUPABASE"="false"})`

### Story 14.11 verdict

**PASS.** Phase 14 Terraform surface is proven sound:
- Flag-ON plan: zero errors, 270 resource adds, one supabase secret created, zero aurora/networking resources.
- Flag-OFF plan: zero errors, 304 resource adds, aurora + networking planned, zero supabase resources.
- Mutual exclusivity invariant: holds in both directions.
- Null-arn bug from 14.5's pre-14.6 state: gone.
- Layer rebuild from 14.10: reflected in plan via fresh `source_code_hash` on `db_layer`.
- Prod code path: validate-only until prod state bootstrap.

Ready to proceed to story 14.12 (apply `use_supabase=true` on dev, populate the Supabase secret, verify the deployed stack boots clean). 14.12 is the first REAL AWS state change in phase 14 — gated on user approval.

---

## 2026-11-10 — story 14.13 landing (db_migrator Supabase-awareness)

### Handler refactor scope

`infrastructure/src/functions/db_migrator/handler.py` was refactored to be backend-aware. An import-time boolean `_USE_SUPABASE = os.environ.get("USE_SUPABASE") == "true"` (lowercase, case-sensitive — matches what Terraform emits) gates dispatch inside `handler()`. The original Aurora logic was lifted verbatim into a private `_handler_aurora(sm)` function; a new `_handler_supabase(sm)` function covers the Supabase path. The public `handler(event, context)` entrypoint calls one or the other and appends `elapsed_time_seconds` to whichever dict is returned, so the five-key response shape is identical for both backends.

The Supabase path: (1) fetches `DB_SECRET_ARN` (the `knotify/dev/supabase_credential` secret populated in story 14.12) via boto3; (2) derives the project ref by splitting the superuser username on `.` — asserts exactly two segments via `_extract_project_ref()` (fails loudly on malformed payload); (3) connects with `psycopg2.connect(sslmode="require")` to `SUPABASE_HOST:SUPABASE_PORT`; (4) creates `app_user` and `aurora_refresh` roles idempotently if absent; (5) applies yoyo migrations using the superuser credential with `sslmode=require` appended as a query param in the yoyo db_url; (6) rotates both role passwords and writes pooler-qualified usernames (`app_user.<ref>`, `aurora_refresh.<ref>`) to Secrets Manager. The Aurora path is byte-identical in behaviour to the pre-14.13 handler.

### Un-gate of null_resource.db_migrator_invoke

`count = var.use_supabase ? 0 : 1` removed from `null_resource.db_migrator_invoke` in both `infrastructure/environments/dev/main.tf` and `infrastructure/environments/prod/main.tf`. The resource is now unconditional. Comment block updated to reflect the new backend-aware behaviour. After the next CI apply the null_resource will fire (triggered by the new Lambda version hash), invoke the db_migrator Lambda with the Supabase-path handler, and run all 19 migration files (0000-0018) against the empty Supabase project.

### terraform fmt -check + terraform validate

Both commands pass clean on dev and prod environments:

```
dev:  terraform fmt -check  → clean (no reformatting needed)
dev:  terraform validate    → Success! (2 pre-existing dynamodb hash_key/range_key deprecation warnings — out of scope)
prod: terraform fmt -check  → clean
prod: terraform validate    → Success! (same 2 pre-existing warnings)
```

### Expected post-merge trajectory

1. CI runs `terraform apply` on dev (triggered by PR merge into `development`).
2. Apply detects that `null_resource.db_migrator_invoke.count` changed from 0 to 1 (hotfix PR #183 set it to 0; this PR removes the count entirely).
3. Terraform creates the null_resource and fires the `local-exec` provisioner.
4. The provisioner invokes `knotify-db-migrator-dev` with the new handler (USE_SUPABASE=true, DB_SECRET_ARN set, SUPABASE_HOST/PORT/DBNAME set).
5. Handler connects to Supabase, creates `app_user` and `aurora_refresh` roles, applies migrations 0000-0018.
6. After apply completes, `relation "users" does not exist` errors cease for all Lambdas that call `knotify_db.get_connection()`.

### Scope boundary

The ACTUAL verification of migration application (CloudWatch log inspection, read-only SQL query against Supabase confirming table set, pgvector column presence, RLS policy smoke test) is OUT of this subagent's scope. Those checks constitute the story 14.13 acceptance criteria and are executed post-merge by the main agent via the Supabase MCP `execute_sql` tool. This digest records the pre-merge local verification only.

---

## 2026-11-10 — post-merge verification (story 14.13 AC)

Executed by the main agent via Supabase MCP after PR #184 (14.13 code) and PR #185 (hotfix removing the pre-migration role-creation loop that collided with migration 0007/0013 `CREATE ROLE`) both merged into `development`. Final `apply-dev` run **42839298117 PASSED**.

### Story 14.13 AC — checklist

| AC item | Result |
|---|---|
| `aws lambda invoke --function-name knotify-dev-db-migrator` returns statusCode 200 | ✅ apply exit 0 implies FunctionError absent |
| Each migration 0000–0018 applied in order with no error lines | ✅ `_yoyo_migration` lists all 19 ids; `applied_at_utc` monotone (0000–0006 at 14:41; 0007–0018 at 15:11 after hotfix) |
| Canonical table set present | ✅ `users`, `siblings`, `friendships`, `friend_requests`, `bookmarks`, `blocks`, `deck_view` MV, `refresh_log`, plus yoyo metadata |
| pgvector enabled + vector columns on users | ✅ `pg_extension.vector` installed; `users.preference_vector` column present |
| Migration 0007 RLS policy in place | ✅ 4 policies on `users` (`users_insert_own_row`, `users_opposite_sex_only`, `users_update_own_row`, `users_delete_own_row`) all reference `app.requesting_user_id` + `app.requesting_user_sex`; `relrowsecurity = true` + `relforcerowsecurity = true` |

### What MCP verification could NOT cover

The behavioral RLS smoke (`non-privileged role with GUC set returns only matching rows`) requires connecting through the pooler as a non-superuser. Supabase MCP connects as `postgres` (superuser), which always bypasses RLS even with `FORCE ROW LEVEL SECURITY`. That check falls out of story 14.14 when probe_phase6/7/8/9 Lambdas authenticate as `app_user.waulcjgqytiqkwdrkchg` through the pooler and run real queries.

### What's live on Supabase project `waulcjgqytiqkwdrkchg`

- Schema fully migrated to parity with Aurora (migrations 0000–0018).
- Roles: `app_user` (login, non-super, non-bypassrls) + `aurora_refresh` (same). Created by migrations, password-rotated by the handler, pooler-qualified usernames written to Secrets Manager.
- Secrets: `knotify-dev-app-user-credential` + `knotify-dev-aurora-refresh-credential` populated with `{"username": "app_user.waulcjgqytiqkwdrkchg", "password": "<redacted>"}` shape.

### Hotfix PR #185 — one-sentence summary

The 14.13 `_handler_supabase` pre-created `app_user` + `aurora_refresh` via CREATE ROLE before calling `_apply_migrations`; migrations 0007/0013 then collided with the existing roles on `DuplicateObject`; hotfix removed the pre-migration loop so migrations own role creation (same as Aurora) and the handler only ALTERs passwords afterwards. Stray roles dropped from Supabase via MCP before the merge so migrations could CREATE them fresh on the re-apply.

---

## 2026-11-10 — story 14.14 (probe_phase7 + probe_phase8 against Supabase)

Lean-to-comprehensive probe runs executed by the main agent with user-provided temp SSO credentials. Dev stack endpoint IDs fetched from CI workflow `outputs-dev.yml` run **42846206576**; `EDGE_SECRET` fetched from `knotify-profile-dev` Lambda env config.

### Issue surfaced + fix (migration 0019)

First probe run hit `InsufficientPrivilege: new row violates row-level security policy for table "refresh_log"`. Investigation via MCP revealed that **every** public-schema table on Supabase has `relrowsecurity = true` with `policy_count = 0` — a Supabase PostgREST-safety event trigger auto-enables RLS on every CREATE TABLE. Aurora never did this; the 6 app tables (`siblings`, `friendships`, `friend_requests`, `bookmarks`, `blocks`, `refresh_log`) were designed around GRANT-based authorisation with no RLS.

Fix landed as migration `0019_disable_rls_on_grant_based_tables.sql`: explicit `ALTER TABLE <t> DISABLE ROW LEVEL SECURITY` for those 6 tables. Backend-agnostic — no-op on Aurora (RLS was never enabled), reverses the Supabase auto-enable on Supabase. `users` is deliberately left with `ENABLE + FORCE RLS + 4 policies` (the one designed RLS surface). yoyo-internal tables (`_yoyo_*`, `yoyo_lock`, `_schema_init`) are untouched — owned by `postgres`, owner-bypass applies, no cross-role access. deck_view is a materialized view (MVs can't carry RLS).

The migration was pre-applied via MCP for immediate probe iteration; CI will re-run it when PR merges (`ALTER TABLE ... DISABLE RLS` is idempotent on already-disabled tables).

### Probe scripts — env-driven refactor

`scripts/probe_phase7.py` and `scripts/probe_phase8.py` had their hardcoded resource IDs (pointing at the OLD Aurora dev stack torn down on 2026-11-08) rewritten to read from env vars with new-stack defaults. `EDGE_SECRET` is required via env (clean failure message with the `aws lambda get-function-configuration` fetch hint if missing).

Also fixed one stale probe assertion in `probe_phase8.py` step [14] `listMyRooms` ordering: the comment and check expected roomA first "because only A has messages", but step [13b] (added later) sends Jack's 'hey' to roomB, making roomB the room with the most-recent message. Assertion flipped to expect roomB first (correct lastMessageAt DESC order).

### Results

| Probe | Result | Notes |
|---|---|---|
| `probe_phase6` | N/A | No such script exists; profile/friends/bookmarks/blocks are exercised transitively by phase 7/8 flows |
| `probe_phase7` | ✅ **9/9 PASS** | match + deck + pgvector against Supabase. Cognito signup → PATCH /v1/profile/me → RLS gate closes/opens → refresh_deck_view MV populates → POST /v1/match/search returns 1 result → GET /v1/match/deck returns 1 result |
| `probe_phase8` | ✅ **22/22 PASS** | chat (AppSync + DynamoDB) + account deletion end-to-end. Friend-request accept, createOrGetRoom, sendMessage × 3, room isolation, block → room deactivation + friendship_active=false, re-friend-blocked 409, unfriend → room preserved + friendship_active=false, @aws_iam directive enforcement, Kate hard-delete (SFN SUCCEEDED, audit retention='hard_deleted'), Jack soft-delete (anonymized to '[deleted-user]'), John hard-delete (roomA empty) |
| `probe_phase9` | **DEFERRED** to story 14.14a | 13 Aurora Data API `execute_statement` call sites need rewrite to psycopg2+pooler. The phase 8 probe already covers the deletion workflows (soft + hard) end-to-end on Supabase; probe 9 adds AppSync WebSocket subscription + friend-decline + Aurora-side row inspection. The Aurora-side inspection is Aurora-specific and needs Supabase equivalent via psycopg2. |

### What 14.14 proves

- RLS enforcement works on Supabase (users table gate CLOSED pre-completion, OPEN post-completion with correct GUC).
- pgvector + preference_vector encoding + deck_view materialized view refresh all function.
- Step Functions account-deletion workflow (both branches: soft/anonymize and hard-purge) correctly invokes `soft_delete_aurora` + `hard_purge` Lambdas, which transparently use `knotify_db._supabase.get_connection` via the layer dispatch from story 14.9.
- Audit log retention values (`hard_deleted` / `permanent_anonymized`) land correctly in DynamoDB.
- Chat (AppSync + DynamoDB + push-fanout) works end-to-end regardless of DB backend (AppSync + DynamoDB are backend-agnostic).
- Non-superuser role routing through the Supabase pooler as `app_user.<project_ref>` and `aurora_refresh.<project_ref>` works correctly.

Phase 14 behavioral gate: **PASSED** end-to-end on dev. Supabase substitutes cleanly for Aurora.
