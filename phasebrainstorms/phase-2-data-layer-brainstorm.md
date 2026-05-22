# Phase 2 brainstorm — Data layer (Aurora + DynamoDB)

## 2026-05-23 brainstorm

PRD audited: `implementationplan/phase-2-data-layer.md`. Architecture reference: `architecture.md` §5.1, §5.2, §5.4, §5.7. Phase-1 outputs verified live: `vpc_id`, `private_subnet_ids`, `db_subnet_group_name`, `lambda_security_group_id`, `aurora_security_group_id`.

Findings below are ordered roughly by severity (real-bug → significant-gap → minor).

---

### 1. Story 2.7 RLS policy has a NULL-handling bug — acceptance criterion 3 is unsatisfiable as written (REAL BUG)

The RLS policy in architecture §5.2:

```sql
USING (
  sex != current_setting('app.requesting_user_sex', true)
  OR user_id = current_setting('app.requesting_user_id', true)::uuid
)
```

When neither GUC is set, `current_setting(name, true)` returns NULL (`missing_ok = true`). The clause becomes `sex != NULL` (= NULL) OR `user_id = NULL::uuid` (= NULL) → NULL → row filtered out.

But story 2.7 acceptance criterion 3 asserts: "with no GUC set, the Male user can still see his own row (current_setting fallback to NULL is handled)". The policy as written does NOT handle this — the user sees nothing when the GUC is unset.

**Resolution required before implementation:** either
- (a) Drop the "no GUC set" criterion — Lambda will always set the GUC, so the behavior in the unset case doesn't matter, OR
- (b) Rewrite the policy to handle NULL explicitly, e.g.:
  ```sql
  USING (
    current_setting('app.requesting_user_sex', true) IS NULL
    OR sex != current_setting('app.requesting_user_sex', true)
    OR user_id::text = current_setting('app.requesting_user_id', true)
  )
  ```
  (also note the `::uuid` cast on a NULL string raises in some PG versions — comparing as text is safer).

Recommendation: (a). The Lambda layer always sets the GUC; failing closed when it's unset is desirable defense-in-depth. Architecture's intent reads as "the GUC is the gate" — silently passing on missing GUC undermines that.

### 2. Story 2.7 RLS test will pass deceptively if run as the master user (SIGNIFICANT GAP)

RLS is bypassed by superuser-equivalent roles, which includes the Aurora master user. The acceptance criteria say "integration test seeds … and asserts" without specifying which role connects. If the test runs as the master, every assertion passes regardless of policy correctness.

**Add to AC:** the integration test connects as a dedicated non-superuser, non-BYPASSRLS role created by an earlier migration (e.g., `app_user`). Without this the test is theater.

### 3. Story 2.1 parameter group / `shared_preload_libraries` is technically wrong (REAL BUG)

Acceptance criterion 3: "An aws_rds_cluster_parameter_group enables shared_preload_libraries containing the values needed for vector and pg_trgm."

- `pg_trgm` is loaded by `CREATE EXTENSION pg_trgm` — it does NOT belong in `shared_preload_libraries`.
- `pgcrypto` — same, extension only.
- `vector` (pgvector) on Aurora Postgres 16 — also extension only, not preload.

The only thing that commonly needs preloading on Aurora Postgres for this project is `pg_stat_statements` (later, for observability). Right now there is no preload requirement.

**Resolution:** drop the `shared_preload_libraries` requirement from AC. Keep the parameter group (still useful for future tuning), but the criterion should be "a parameter group exists and is wired to the cluster" — not the misleading library list.

### 4. Story 2.1 master credential storage is unspecified (SIGNIFICANT GAP)

AC says "Module outputs … master_password (sensitive)" but doesn't say how the password is generated or stored. The two options:
- (a) Generate via `random_password`, output as sensitive. Stored in Terraform state in plaintext (state is in S3 with SSE — acceptable but a sharp edge).
- (b) `manage_master_user_password = true` — AWS auto-generates and stores in Secrets Manager. State holds only the secret ARN. This is the AWS-recommended pattern, the one phase 3 (Lambda foundations) will want to consume from anyway.

**Recommendation: (b).** Add to AC: `manage_master_user_password = true`, module outputs `master_user_secret_arn` (instead of `master_password`).

### 5. Story 2.1 `engine_version "16.x"` is not a real value (TESTABILITY)

Terraform requires a concrete version. "16.x" will fail. Pick one of:
- Pin a specific version (e.g., `"16.4"`). Brittle — every minor bump requires plan changes.
- Use `data "aws_rds_engine_version" "this"` with `default_only = true` and reference its `version`. Self-updating but plan diff on each AWS minor release.
- Set `auto_minor_version_upgrade = true` and pin to `"16"` (Aurora accepts the major). Simplest.

**Recommendation:** AC should say "engine_version pinned to a concrete Aurora-Postgres-16 minor available in eu-central-1; `auto_minor_version_upgrade = true`". Subagent picks the minor at apply time.

### 6. Story 2.14 — migrations must run against Aurora, but Aurora is in private subnets with no public access (PRACTICAL BLOCKER)

AC 5: "A dev apply followed by running all migrations against the dev Aurora cluster completes and `\d users` matches the spec."

Aurora's `publicly_accessible = false` (correctly). There is no bastion, no VPN, no SSM Session Manager target, and no migration-runner Lambda in phase 2. How does the operator reach the cluster to run migrations?

Options:
- (a) Run migrations from a one-shot Lambda invoked from phase 2 itself. Cleanest but adds Lambda scope before phase 3.
- (b) Run migrations from the GitHub Actions runner via an SSM port-forward through a temporary EC2 bastion. Heavy.
- (c) Run migrations from a developer machine via SSM Session Manager port-forward to a small, temporary t4g.nano bastion that's spun up/torn down by Terraform with `count = var.enable_bastion ? 1 : 0`. Practical, minimal.
- (d) **Defer the cluster-side migration AC to phase 3** when a Lambda VPC executor exists. Phase 2's AC becomes "migrations succeed against local Postgres container; cluster-side run is phase 3's first task."

**Strong recommendation: (d).** It cleanly separates concerns — phase 2 is "schema exists in code, validated locally; cloud infra exists." Phase 3 wires the first Lambda and runs the migration against Aurora as its first acceptance criterion. This also avoids inventing a one-off operator path that gets ripped out next phase.

If the user prefers to keep migration-against-cluster in phase 2, option (c) is the most surgical — but it adds bastion scope.

### 7. Story 2.2 migration runner choice is left open (DECISION DRIFT RISK)

AC: "A migration runner is chosen (sqitch, Flyway, Alembic, or yoyo)." Leaving four equally-valid choices to the subagent means re-litigation and possible inconsistency with future stories.

Given the backend is Python and migrations are raw SQL (extensions, triggers, RLS policies), **sqitch** or **yoyo** are the natural fits — neither requires defining models. Alembic is heavy if there's no SQLAlchemy. Flyway requires JVM.

**Recommendation:** decide here. Suggest yoyo (Python-native, raw SQL, lightweight) OR sqitch (declarative dependency graph, slightly more ceremony, but excellent for the trigger/RLS layering this project needs). Pick one and update AC to "yoyo-migrations is the runner; migrations live in `infrastructure/db/migrations/`."

### 8. Story 2.14 — prod environment wiring under the PROD_CUTOVER.md pause (CLARITY)

AC 1: "Both infrastructure/environments/dev/main.tf and prod/main.tf instantiate the aurora and dynamodb modules wired to the networking outputs from phase 1."

Per PROD_CUTOVER.md (and phase-1 precedent), prod-side modules are authored and planned-against-prod via CI, but `apply` is gated. The criterion should explicitly say "author both; only apply against dev". Otherwise the subagent may interpret "instantiate" as "must apply" and either get blocked or skip prod entirely.

### 9. Story 2.10–2.13 — DynamoDB tables miss `deletion_protection_enabled` and `lifecycle.prevent_destroy` in prod (PROD SAFETY)

AC says "server_side_encryption enabled" and "tags". For chat messages, notifications, and push tokens — all production user data — `terraform destroy` should not be able to wipe them in prod. Phase 1's networking module sets no such guards (no destroy-protection needed for VPC), but data tables are different.

**Add to AC:** `deletion_protection_enabled = var.environment == "prod"` (DynamoDB now supports this natively as of mid-2024) and `lifecycle { prevent_destroy = true }` gated by a variable for the test environment.

### 10. Story 2.12 "sparse" GSI description is a write-pattern, not a Terraform attribute (CLARITY)

AC: "A sparse GSI named UnreadIndex projects only items where read=false". DynamoDB enforces sparsity by the presence/absence of the GSI key attribute — the application is responsible for writing the GSI key only on unread items and removing it when `read` flips true. Terraform just declares the GSI keys; it can't enforce sparsity.

Cosmetic — restate as "GSI declared with PK=user_id, SK=notification_id; sparsity is application-enforced (writers attach the GSI SK only when `read = false`)".

### 11. Aurora Serverless v2 minimum-ACU cost note (NOT A BUG, COST AWARENESS)

Aurora Serverless v2 minimum ACU dropped to 0.5 (it was 0.5 → 0 in late 2024 for zero-scaling, but zero-scaling has cold-start of ~15s — bad for dev iteration). Recommend dev `min=0.5, max=2`, prod `min=1, max=8` (or whatever architecture decided). This is the first phase that creates non-trivial standing cost (~$45–90/mo dev at 0.5 ACU 24/7).

Not blocking — just worth setting the defaults intentionally rather than copy-pasting AWS docs.

### 12. Story 2.1 missing decisions to surface explicitly (MINOR)

- `iam_database_authentication_enabled` — defer to phase 3 (Lambda → Aurora connection). Document as deferred in story notes, not a hidden gap.
- `enabled_cloudwatch_logs_exports = ["postgresql"]` — useful for phase 10 observability. Add now for free.
- `skip_final_snapshot` — must be `true` in dev (else destroy fails), `false` in prod. Variable-driven, like deletion_protection.
- `apply_immediately` — `true` in dev, `false` in prod. Same pattern.

### 13. External assumptions not yet validated

- **pgvector availability on Aurora Postgres 16.** Last confirmed available; the specific minor version may matter. The subagent should verify with `aws rds describe-db-engine-versions --engine aurora-postgresql --engine-version <chosen>` and check `SupportedFeatureNames` or just attempt `CREATE EXTENSION vector` in the integration test.
- **`pg_trgm`, `pgcrypto`** — both ship with Postgres, always available.
- **eu-central-1 supports Serverless v2** — yes, has since 2022. Safe assumption.

### 14. Scope smuggling — none found

Stories stay within data-layer concerns. No Lambda code, no API definitions, no Cognito wiring leaks in. Clean phase boundary.

### 15. depends_on correctness

- 2.3 → 2.2: needs the runner before writing migrations. ✓
- 2.4, 2.5, 2.6, 2.7, 2.8, 2.9 → 2.3: all need `users` to exist. ✓
- 2.11, 2.12, 2.13 → 2.10: arguably 2.10 isn't a hard dependency for 2.11–2.13 (they're independent DynamoDB tables), but co-locating them in one module file is reasonable, and 2.10 establishes that module file. Keep.
- 2.14 → 2.1, 2.10, 2.11, 2.12, 2.13: correct.

One missing dependency: **2.14 should also depend on 2.2** (or implicitly on all migrations) if AC 5 stays as "run migrations against the dev cluster". With recommendation #6 (defer cluster-side migrations to phase 3), this drops away. If kept, add the dep.

---

## Recommended PRD edits (summary)

| Story | Edit |
|---|---|
| 2.1 | Drop `shared_preload_libraries` requirement; add `manage_master_user_password = true` and `master_user_secret_arn` output; pin engine_version concretely; add `skip_final_snapshot`, `apply_immediately`, `enabled_cloudwatch_logs_exports` as variable-driven; default ACU min/max for dev. |
| 2.2 | Pick one migration runner (recommend yoyo). |
| 2.7 | Fix or drop the "no GUC set" criterion (recommend drop); require integration test runs as a dedicated non-superuser role. |
| 2.10–2.13 | Add `deletion_protection_enabled` and `prevent_destroy` variable-driven for prod. |
| 2.12 | Restate sparsity as app-enforced, not Terraform-enforced. |
| 2.14 | (a) Explicitly say "author both dev and prod; apply dev only" per PROD_CUTOVER. (b) Defer cluster-side migration run to phase 3 (strong recommendation) OR add bastion sub-story. |

The four items I'd flag as actually blocking (must resolve before dispatch):
1. The RLS NULL-handling bug (#1) — silent wrong behavior shipped to prod is bad.
2. The `shared_preload_libraries` confusion (#3) — first apply will surprise the operator.
3. Cluster-side migration access path (#6) — story 2.14 AC 5 currently has no working answer.
4. Master credential strategy (#4) — phase 3 will need the answer locked in.

---

## 2026-05-23 — resolution log

All four blockers and the significant gaps have been addressed in the phase-2 PRD by direct edits made the same day. Summary:

- Blocker #1 (RLS NULL bug): story 2.7 acceptance criterion 3 rewritten to assert fail-closed behavior (no GUC → zero rows) and to require the integration test connects as a dedicated non-superuser `app_user` role. Policy text unchanged in architecture.md — the fix is in the test contract, not the SQL.
- Blocker #2 (shared_preload_libraries): story 2.1 criterion 3 rewritten to drop the preload requirement and clarify that vector/pg_trgm/pgcrypto are CREATE EXTENSION extensions loaded by migrations.
- Blocker #3 (cluster-side migration access path): story 2.14 AC 5 replaced with an explicit out-of-scope statement deferring cluster-side migration to phase 3. The deferred work is now tracked as **phase 3 story 3.7 — DB migrator Lambda and initial cluster-side migration run** (depends_on [3.1, 3.4]). Phase 3's IAM role list (story 3.4) was extended to include a `db_migrator` builder. Phase 3's `context_summary` was updated to mention the cascade. Phase 3's `last_updated` is now 2026-05-23.
- Blocker #4 (master credential): story 2.1 now requires `manage_master_user_password = true` and outputs `master_user_secret_arn`. Phase 3's cognito Lambda env (story 3.6) and the new migrator Lambda (story 3.7) both consume that ARN — no plaintext credential flows through the system.
- Significant gaps #5–#9 (master-user RLS bypass, engine_version pin, runner choice, prod-author-but-not-apply wording, DynamoDB deletion_protection): all applied to the PRD directly. yoyo-migrations is locked in. Engine pinned to 16.4 with auto_minor_version_upgrade. dev/prod tfvars defaults specified in story 2.14. DynamoDB deletion_protection_enabled is variable-driven; the Terraform `lifecycle.prevent_destroy` non-static-variable caveat is documented inline.
- Minor #10–#13: sparsity wording fixed in 2.12; cost note for Aurora ACU defaults applied (dev 0.5–2.0, prod 1.0–8.0); pgvector availability flagged as something the subagent verifies at apply time via `CREATE EXTENSION vector` in migration 0001.
- Finding #15 (missing 2.14 → 2.2 depends_on): no longer relevant — with cluster-side migration deferred to phase 3, story 2.14 no longer needs the migration runner.

The PRD is now internally consistent and ready for dispatch. Proceed signal: user picks `proceed`.
