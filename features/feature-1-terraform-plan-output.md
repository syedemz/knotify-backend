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
