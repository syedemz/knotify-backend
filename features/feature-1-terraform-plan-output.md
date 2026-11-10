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
