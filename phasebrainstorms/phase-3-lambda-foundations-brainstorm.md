# Phase 3 brainstorm — Lambda foundations

## 2026-05-24 20:54 brainstorm

Critical review of `implementationplan/phase-3-lambda-foundations.md` (7 stories, all `backenddeveloper`) against `architecture.md`, phase 1/2 deliverables on disk, and the deferred items from prior phase brainstorms. Findings grouped by severity.

---

### BLOCKERS — phase will fail to deploy as written

**B1. No outbound path from VPC Lambdas to AWS service APIs (Secrets Manager, KMS, optionally STS/CloudWatch).**

Phase 1 deliberately built the VPC with **no NAT Gateway and no Internet Gateway** (story 1.1 AC verbatim: "No aws_nat_gateway, no aws_internet_gateway resource at all"). Phase 3 places two Lambdas (`db_migrator`, `cognito_post_confirmation`) in private subnets and expects each to call `secretsmanager:GetSecretValue` on the Aurora master secret. That call originates from the Lambda's ENI in a private subnet — without NAT or a Secrets Manager VPC Interface Endpoint, the request has no route to the AWS service endpoint and will time out.

Architecture §6.2 was about Lambda→Aurora (in-VPC, no endpoint needed). It does NOT cover Lambda→AWS-service-APIs. The PRD inherits this confusion and assumes Secrets Manager is reachable. It is not.

CloudWatch Logs is the one exception — Lambda's log forwarder delivers via the Lambda service's own infrastructure, not through the function's ENI, so logging works without an endpoint.

Three viable fixes, in order of architectural consistency:

  a. **Add a Secrets Manager Interface VPC Endpoint** (and a KMS endpoint if KMS calls are needed for decrypting the secret payload, which Aurora-managed secrets do not require since they use the AWS-managed key on the SecretsManager service path). Cost: ~$7/mo per endpoint per AZ in eu-central-1, so two AZs × one endpoint ≈ $14/mo for dev. Architecturally clean; consistent with "no NAT" decision.
  b. **Add a single NAT Gateway in one AZ.** ~$32/mo + data transfer. Cheaper in absolute terms if you also add DynamoDB/S3 endpoints later (DynamoDB and S3 are free Gateway endpoints), but architecture §6.4 explicitly rejected NAT for v1.
  c. **Move both Lambdas OUT of the VPC and use the RDS Data API for migrations.** Rejected by architecture §6.2 because it changes the data-access pattern for all later Lambdas.

  **Recommendation: option (a).** Add a new story `3.0` (or extend story 3.1) that provisions a Secrets Manager Interface VPC Endpoint in the networking module before any Lambda is dispatched. The endpoint resource is a single `aws_vpc_endpoint` per environment; the security group on the endpoint allows TCP 443 ingress from `sg-lambda`. Cost in dev is negligible; cost in prod can be revisited at hardening time.

  Without this, `db_migrator`'s first invocation (story 3.7 ACs 5–7) WILL fail in dev, blocking phase completion.

**B2. Migration 0007 creates `app_user` with a hardcoded password 'app_user' — applying this to the dev Aurora cluster is a security regression.**

Migration `0007_rls_app_user_and_policy.sql` line 59 contains `PASSWORD 'app_user'`. The migration's header comment acknowledges this is "local dev only" and says "in dev/prod Aurora the credential is stored in Secrets Manager … this static password is never committed to the Aurora cluster — this migration is applied to the local docker-compose container only in this phase."

Story 3.7 instructs the migrator Lambda to run `yoyo apply` against the dev Aurora cluster — which means migration 0007 WILL run against dev Aurora, planting `app_user / app_user` as a real LOGIN role on the cluster. Anything in the VPC that can reach 5432 (or anyone with the Aurora password reset in a future incident) can connect as `app_user`.

The PRD does not address this. Two viable fixes:

  a. **Split 0007.** Move the `CREATE ROLE app_user … PASSWORD …` line into a separate migration that the cluster-side migrator OVERRIDES with a Secrets-Manager-sourced random password. Concretely: migration 0007 becomes `CREATE ROLE app_user WITH LOGIN NOSUPERUSER NOBYPASSRLS;` (no password). The migrator Lambda, after yoyo apply, does `ALTER ROLE app_user WITH PASSWORD '<random>'` and stores the random value in a new Secrets Manager secret (`knotify-dev-app-user-credential`). Phase 6+ business Lambdas read THAT secret to connect as `app_user`.
  b. **Make 0007 password conditional.** Use a `DO $$ BEGIN ... END $$` block in 0007 that only sets the password when the existing role is missing, and ship a separate idempotent ALTER ROLE step that the migrator runs post-yoyo.

  Recommendation: (a). Two new tracking items: (i) edit migration 0007 (move password out of CREATE ROLE), (ii) extend story 3.7 ACs to include the ALTER ROLE + Secrets Manager secret creation step.

  This is a real production-bound flaw — the migration as written is in the dev cluster path, and the dev cluster is shared infra.

---

### MAJOR — story acceptance criteria gaps that will cause subagent failure or rework

**M1. Story 3.4 (IAM roles) — `secretsmanager:GetSecretValue` scoping uses an output that is `null` at plan time.**

Aurora's `master_user_secret_arn` output (verified in `infrastructure/modules/aurora/outputs.tf:16-22`) uses `try(aws_rds_cluster.this.master_user_secret[0].secret_arn, null)` to be safe during `terraform plan` and inside `tftest.hcl` mock runs. An IAM policy with `Resource = null` is invalid. The story must specify how to handle this: either use a `*` ARN with a `Condition` filter on `aws:ResourceTag/Project=knotify`, or build the role with a `data.aws_secretsmanager_secret` lookup that exists only after the secret is materialized, or scope to a constructed-name pattern (`arn:aws:secretsmanager:<region>:<account>:secret:rds!cluster-*`). The PRD names the dependency but does not name the resolution.

**M2. Story 3.4 — `aurora_reader`, `aurora_writer`, `dynamodb_*_writer`, `stepfn_task` roles have no consumer in this phase and no scoping rationale.**

Only `db_migrator` and `cognito_trigger` are actually consumed by stories in phase 3. The other five roles are forward-looking templates with no concrete code to test against. Without consumers, the role definitions can drift from what later phases actually need, and the `.tftest.hcl` test (AC bullet 4) ends up asserting that an arbitrary list of managed ARNs is attached — testing the test, not the design.

Options:
  a. Defer the unused roles to their consuming phases (cognito_trigger here, db_migrator here, the rest in 6/7/8/9). Story 3.4 ships only the two roles phase 3 actually uses.
  b. Keep all roles here but mark them explicitly "scaffold only — actions and resource ARNs finalized in the consuming phase". Tests assert only the trust policy and the `AWSLambdaVPCAccessExecutionRole` attachment, not the action set.

  Recommendation: (a). It removes ~70% of story 3.4's surface area and avoids speculative IAM that nobody runs.

**M3. Story 3.6 — `gender (mapped to sex)` is hand-wavy and unguarded.**

Cognito's standard `gender` attribute is a free-form string. The `users.sex` column has `CHECK (sex IN ('Male','Female'))`. The PRD says "gender (mapped to sex)" but does not define the mapping. If the React Native client sends `male`/`female` lowercase or `M`/`F`, the INSERT will violate the CHECK constraint and the Lambda will throw — but Cognito will have already confirmed the user (PostConfirmation runs AFTER signup is durable). The user ends up confirmed in Cognito with no row in `users`, and the next signin fails downstream.

Add an explicit mapping table in the AC (`{m: Male, male: Male, M: Male, f: Female, female: Female, F: Female}` — anything else logs structured error and exits gracefully) and an integration test that exercises at least the three common variants plus an unrecognized value.

**M4. Story 3.6 — `users` row INSERT will fail on NOT NULL columns that Cognito doesn't supply.**

`users` requires NOT NULL on `first_name`, `last_name`, `sex`, `birthday`, `username` (verified in `infrastructure/db/migrations/0002_create_users.sql:39-47`). Cognito custom attributes can be missing or empty for confirmed users (e.g., social-identity signups skip them; phone-only signups skip email). The PRD's third AC says "handler returns the event unmodified after logging a structured error … so Cognito does not roll back the signup" — good intent — but the criteria for "what counts as missing" is undefined. Define it as: any of {first_name, last_name, sex, birthday, username, email} missing → log + return event unchanged + DO NOT INSERT (no half-row). Add an explicit AC.

Also: `users.username` is `NOT NULL` but the Cognito event attribute is `preferred_username` (optional in most pools). Pin which Cognito attribute maps to `users.username` and what happens when it is absent.

**M5. Story 3.2 — `python-jose or PyJWT` — pick one. And the helper may be dead code per architecture.**

Architecture §section near line 217 / 237 / 334 commits to HTTP API's **built-in Cognito JWT authorizer**. Business Lambdas in phases 6+ do not re-verify the JWT — they read claims from `event.requestContext.authorizer.jwt.claims`. The `verify_cognito_jwt(token, user_pool_id, region)` helper has no obvious consumer in v1. (AppSync also has native Cognito auth.)

Two choices:
  a. **Drop `verify_cognito_jwt` from the layer.** Keep the layer for Powertools + logger + correlation id middleware, which IS consumed everywhere. Smaller surface, no JWKS-caching design needed.
  b. **Keep it but document the use case** — likely zero v1 consumers, but maybe a Lambda authorizer in phase 11 hardening.

  Recommendation: (a). Remove the helper from the layer scope; remove its unit test from the AC. Replace `python-jose or PyJWT` with no JWT lib at all. Smaller layer = faster cold starts.

  If kept, force `PyJWT[crypto]` (actively maintained) — drop the `or python-jose` alternative.

**M6. Story 3.7 — "shells out to or imports yoyo-migrations" — pick one.**

Lambda functions cannot reliably shell out to subprocesses bundled in the deployment package (path issues, binary dependencies, signal handling differences from a normal shell). yoyo-migrations exposes a Python API: `yoyo.read_migrations(path)` + `backend.apply_migrations(...)`. Pick the Python API path explicitly. Remove the "shells out" option from the AC.

**M7. Story 3.7 — `null_resource` + `local-exec` for verification fork creates a `command` switch in the handler that isn't documented.**

The AC for the second verification step says: "no human bastion access required" and proposes `payload {"command": "verify"}`. The handler then needs a branch:

```
if event.get("command") == "verify":
    return run_verify_query()
else:
    return run_yoyo_apply()
```

That's a fork in the handler that the prior ACs don't describe. Either:
  a. Drop the verify step; the first invocation already returns `pending_migrations: 0` on success, which proves the schema is applied.
  b. Make the verify command a first-class AC: list the SQL it runs (`SELECT count(*) FROM information_schema.tables WHERE table_name = 'users'` returns 1; same for the other 5 §5.1 tables) and the structured JSON it returns.

  Recommendation: (a). The idempotency check (AC bullet 7) already exercises the same code path twice and proves the schema is in place. The verify branch is duplicative.

**M8. Story 3.1 — `aws_lambda_alias "live"` requires `publish = true` on the function and depends on a function version.**

Aliases cannot point at `$LATEST` (technically they can, but you lose all versioning benefits). The AC should add: `aws_lambda_function.publish = true`, and the alias's `function_version` is `aws_lambda_function.this.version`. Otherwise the alias breaks on every code update or fails to create.

**M9. Story 3.1 — log group ordering.**

If the Lambda is invoked before the explicit `aws_cloudwatch_log_group` is created, AWS auto-creates the log group with `Never expire` retention, and then `terraform apply` of the explicit log group fails with "ResourceAlreadyExists". The module must declare `depends_on = [aws_cloudwatch_log_group.this]` on the function (or use `lifecycle.create_before_destroy` semantics). Add to AC.

---

### MEDIUM — testability and scope concerns

**T1. Story 3.5 — `make test` AC is under-specified.**

"At least the verify_cognito_jwt and set_rls_context tests" is the minimum, but the PRD never says where those tests live or how `pytest` discovers them. Spell out: `pytest infrastructure/db/tests/ src/functions/ src/layers/` (or equivalent), exit zero, exit code reported in `make test`. Otherwise the subagent has to invent the layout.

**T2. Story 3.3 — `set_rls_context` GUC names must match migration 0007 exactly.**

The RLS policy reads `current_setting('app.requesting_user_id', true)` and `current_setting('app.requesting_user_sex', true)` (`0007_rls_app_user_and_policy.sql:107-110`). The helper must call `SET LOCAL app.requesting_user_id = '<uuid>'; SET LOCAL app.requesting_user_sex = '<Male|Female>';`. The AC should name these GUCs explicitly so the implementer can't drift.

**T3. Story 3.3 — the integration test against the Postgres container needs RLS set up first.**

The phase-2 docker-compose Postgres container is a clean local DB. To prove `set_rls_context` works, the test must first run all yoyo migrations against the container (including 0007 which creates `app_user` and the RLS policy). Add a `pytest` fixture that calls `yoyo apply` in setUp and `yoyo rollback --all` in tearDown. Otherwise the test won't have RLS to test against.

**T4. Story 3.7 — prod migrator Lambda "authored but not deployed" — verify CI guard explicitly.**

`prod/main.tf` adds the migrator Lambda; `apply-prod` job in deploy.yml is gated by `vars.DEPLOY_PROD == 'true'`. Add an AC: "actionlint passes, plan-prod skips at evaluation, no resource is created in any AWS account labeled prod." This matches the pattern phase 2 used for the prod RDS cluster.

**T5. Story 3.6 — VPC config for the cognito_post_confirmation Lambda is in scope BUT will not be exercised until phase 4.**

Phase 4 wires the User Pool's PostConfirmation trigger to this Lambda. Until then, the function exists but is never invoked. That's fine — but the integration test runs the handler against the local Postgres container, NOT against the deployed Lambda. The deployed function is unverified at phase-3 boundary. Add an explicit note that "deployed Lambda is unverified until phase 4 wires Cognito" — so the user understands the deferred risk and doesn't expect a green light from phase 3 alone.

---

### MINOR — clarity and consistency

**N1. Story 3.6's "intentional: no aws_cognito_user_pool_lambda_config wiring"** — fine, but the Lambda also needs a `aws_lambda_permission` resource granting `lambda:InvokeFunction` to `cognito-idp.amazonaws.com`, which is set up when the Cognito wiring happens in phase 4. The PRD should explicitly defer that resource to phase 4 so the subagent does not create it speculatively.

**N2. Story 3.1's `aws_lambda_function` AC does not mention environment variable injection for `POWERTOOLS_SERVICE_NAME`, `LOG_LEVEL`** — the observability layer expects these. Add as defaults (`POWERTOOLS_SERVICE_NAME = var.function_name`, `LOG_LEVEL = "INFO"`).

**N3. Story 3.3's pgvector Python client — for Postgres-side vector ops the pgvector package gives ORM helpers, but the actual SELECT queries can run plain SQL.** Verify the package is needed in the layer at all; if not, drop it and slim the layer.

**N4. Story 3.2 — "aws-lambda-powertools (latest compatible with Python 3.14)"** — Powertools v3.x supports 3.13 and 3.14 since Nov 2025. Pin to a specific minor in the build script (`==3.x.y`) for reproducibility; "latest compatible" drifts.

**N5. Story 3.7 — `null_resource` invocation timing.** The `local-exec` runs at every `apply`, even on no-op applies. Either wrap in `triggers = { migrations_hash = filesha256(...) }` so it only runs when migrations change, or accept the small extra invoke cost. Spell out which.

**N6. DynamoDB access from Lambdas — when phase 6+ ships.** Lambdas in private subnets will need DynamoDB reachability. DynamoDB has a free **Gateway VPC Endpoint** (not Interface). It is one resource + a route-table association. Recommend adding this to the networking module in the same change as B1 (Secrets Manager Interface endpoint), so phases 6/7/8 have it ready. Cost: zero. Not strictly a phase-3 blocker but it's the same edit window.

---

### Cross-phase / drift items

**D1. `context.md` says "Phase 2 PR open into development"** — incorrect. Phase 2's PR was merged (commit `e1f0e2d`) AND tagged `phase-2-complete`. Update `context.md` "Current phase" before phase 3 dispatch begins. (I'll handle this as part of phase-3 startup if you proceed.)

**D2. Aurora `database_name = "knotify"`** — the same name in both environments. yoyo's tracking table (`_yoyo_log`) lives in this database. Confirm dev and prod both connect to the same DB name. (They will, since both environments hard-code `database_name = "knotify"`.) No action — just confirming this is intentional, since `phase-3` story 3.7 has no `DB_NAME` env var; the migrator gets it from the Secrets Manager JSON (`dbname` field).

---

### Suggested action plan (if user picks `address`)

1. Edit migration `0007_rls_app_user_and_policy.sql` — remove the `PASSWORD 'app_user'` from the cluster-bound migration; keep a separate `0007a` (or extend 0007's local-only path) that sets the password for the docker-compose container only.
2. Add a Secrets Manager Interface VPC Endpoint (+ recommended: DynamoDB Gateway Endpoint) to the networking module. Either as a new story `3.0` or by widening story 3.1's scope.
3. Tighten story 3.4: drop the speculative roles, keep only `cognito_trigger` and `db_migrator`, and spell out the Secrets Manager ARN scoping for both.
4. Tighten story 3.6 ACs: gender mapping, NOT NULL guard list, `username` source attribute, deferred `aws_lambda_permission` resource.
5. Tighten story 3.7 ACs: import-not-shell yoyo, drop the verify command branch, spell out the post-apply ALTER ROLE + Secrets Manager secret for app_user (depends on item 1).
6. Drop `verify_cognito_jwt` from story 3.2 (or commit to PyJWT and justify the keep).
7. Add story 3.1 ACs: `publish=true`, alias points to `aws_lambda_function.this.version`, function `depends_on` log group.
8. Add story 3.3 AC: GUC names verbatim; fixture runs yoyo before integration test.
9. Update `context.md` "Current phase" to reflect phase-2-merged state.

If user picks `proceed`, items B1 and B2 will surface as failures during story 3.7's invocation step; expect to land back here.

---

## 2026-05-24 21:30 resolutions

User reviewed findings, picked `address`, and supplied per-item answers in
`C:\Users\syede\Claude-Master\questions\phase3brainstormanswers.txt`. Resolutions applied:

| ID | Resolution | Files touched |
|---|---|---|
| **B1** | New story 3.0: Secrets Manager Interface VPC Endpoint + DynamoDB Gateway VPC Endpoint, both added to the networking module. sg-vpce SG allows 443 from sg-lambda. private_dns_enabled=true. | phase-3 PRD (new story 3.0), networking module (deferred to subagent at dispatch) |
| **B2** | Split 0007: removed the PASSWORD clause from CREATE ROLE in `0007_rls_app_user_and_policy.sql`. Local-dev password set via new file `infrastructure/db/local_init.sql` (outside `migrations/` so yoyo never picks it up). Cluster password generated post-yoyo by the migrator Lambda in story 3.7 and stored in Secrets Manager (`knotify-<env>-app-user-credential`). README.md updated. | `migrations/0007_*.sql`, `db/local_init.sql` (new), `db/README.md`, phase-3 story 3.7 ACs |
| **M1** | Picked option (c): constructed-name ARN pattern `arn:...:secret:rds!cluster-${cluster_resource_id}-*`. Single apply, no tag dependency. | phase-3 story 3.4 ACs |
| **M2** | Kept all IAM roles in story 3.4 (forward-looking templates). Per-action tests only required for roles with phase-3 consumers (`cognito_trigger`, `db_migrator`); other roles assert only trust policy + AWSLambdaVPCAccessExecutionRole. | phase-3 story 3.4 ACs |
| **M3** | Gender mapping kept: `{m, M, male → Male; f, F, female → Female; else → NULL + log warning}`. Encoded in story 3.6 ACs + test cases. | phase-3 story 3.6 ACs |
| **M4** | **Minimal-bootstrap-row pattern** adopted. Migration 0002 edited in place: dropped NOT NULL on `first_name`, `last_name`, `sex`, `birthday`, `username`. Added CHECK constraint `profile_complete_requires_required_fields`. Migration 0008 edited to fire only when OLD is non-NULL (one-time NULL→value transition allowed). architecture.md §5.1 updated to match. `preferred_username` removed from architecture.md §13 #27 (RESOLVED: not used in v2) AND from phase-4 PRD story 4.1. Post-confirmation Lambda requires only `sub` + `email`; missing email → no insert + log. | `0002_create_users.sql`, `0008_immutable_fields_trigger.sql`, `architecture.md` (§5.1 + §13 #26/#27 + new §13a), phase-3 PRD story 3.6, phase-4 PRD story 4.1 |
| **M5** | Option (b): kept `verify_cognito_jwt` helper with documented rare-use case (Lambda authorizers in phase 11). Forced `PyJWT[crypto]`, dropped `python-jose`. | phase-3 story 3.2 ACs |
| **M6** | yoyo Python API path: `from yoyo import read_migrations, get_backend`. Rationale: stable embedding API, CLI is a thin wrapper over it, no subprocess fragility in Lambda. Encoded in story 3.7 AC. | phase-3 story 3.7 ACs |
| **M7** | Dropped the `{"command": "verify"}` handler fork. Idempotency check (third invocation) already proves the cluster state — no second verify branch needed. | phase-3 story 3.7 ACs |
| **M8** | `publish=true` + `function_version = aws_lambda_function.this.version` in the lambda module. | phase-3 story 3.1 ACs |
| **M9** | `depends_on = [aws_cloudwatch_log_group.this]` on the function. | phase-3 story 3.1 ACs |
| **T1** | Colocated tests: `src/functions/<name>/tests/`, `src/layers/<name>/tests/`. Top-level `infrastructure/src/pytest.ini`. `pytest infrastructure/src/` discovers all. `make package` excludes `tests/`. | phase-3 story 3.5 ACs |
| **T2** | GUC names pinned verbatim in story 3.3 AC: `app.requesting_user_id`, `app.requesting_user_sex`. Reason: wrong-name silently fail-closes to zero rows. | phase-3 story 3.3 ACs |
| **T3** | yoyo apply/rollback fixture in `src/layers/db/tests/conftest.py`, marked `@pytest.mark.integration`. Local-only; deploy.yml `test` job stays terraform-test-only in this phase. No CI interference. | phase-3 story 3.3 ACs |
| **T4** | Prod CI gate AC added to story 3.7: actionlint passes, plan-prod skips under DEPLOY_PROD=false, no prod resource created. | phase-3 story 3.7 ACs |
| **T5** | Note added to story 3.6: deployed Lambda is unverified end-to-end until phase 4 wires Cognito; integration tests in this story cover handler logic against a local container only. | phase-3 story 3.6 ACs |
| **N1** | `aws_lambda_permission` for cognito-idp.amazonaws.com explicitly deferred to phase 4 story 4.3 in both phase-3 story 3.6 AC and phase-4 PRD. | phase-3 story 3.6, phase-4 story 4.3 |
| **N2** | Module-level defaults `POWERTOOLS_SERVICE_NAME=var.function_name`, `LOG_LEVEL="INFO"`, merged with consumer overrides via `merge()`. | phase-3 story 3.1 ACs |
| **N3** | Kept pgvector Python client in story 3.3 layer per user direction. | phase-3 story 3.3 ACs |
| **N5** | `null_resource.triggers = { migrations_hash = sha256(...), lambda_version = ... }` so re-runs fire when migrations or code change — no manual `terraform taint`. | phase-3 story 3.7 ACs |
| **N6** | Bundled into story 3.0 alongside the Secrets Manager Interface endpoint: a DynamoDB Gateway VPC Endpoint (free) associated with private + db route tables. No phase-3 consumer, but ready for phase 6+. | phase-3 story 3.0 (new) |
| **D1** | `context.md` "Current phase" updated to reflect that phase 2's PR is merged + tagged (`phase-2-complete`). | `context.md` |

**Profile-completion enforcement model** (added during the address conversation, applies to phases 4–6):

- Layer 1 — DB CHECK constraint on `users.profile_complete_verified` (now shipped via the 0002 edit).
- Layer 2 — Cognito PreTokenGeneration trigger Lambda embeds `custom:profile_complete` claim (new phase-4 story 4.4).
- Layer 3 — HTTP API per-route 403 gate, with a small allowlist (`GET /v1/me`, `POST /v1/me/profile-completion`, `PATCH /v1/me`, `DELETE /v1/me`, photo upload routes) — phase 5.
- Architecture.md gained a new §13a "Profile-completion enforcement (defense in depth)" section.

**No item left unresolved.** All brainstorm findings either applied to files or explicitly deferred to a named downstream phase/story. Ready to re-run `/implement-phase 3` once the index `ready: true` is confirmed (already true).

---

## 2026-05-24 22:15 brainstorm (pre-dispatch confirmation pass)

Re-running brainstorming against the tightened PRD before dispatching story 3.0, per `/implement-phase` Step 0. Goal: confirm the 21:30 resolutions actually shipped on disk and surface any residual gaps. Not re-litigating items on the resolution log unless concrete evidence shows the resolution went sideways.

### Verification of 21:30 resolutions against on-disk state

| Resolution | On-disk verification | Status |
|---|---|---|
| B2 — 0007 PASSWORD removed | `0007_rls_app_user_and_policy.sql:59-66` shows `CREATE ROLE app_user WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;` with no `PASSWORD` clause | ✓ shipped |
| B2 — local_init.sql created | `infrastructure/db/local_init.sql` exists, outside `migrations/` | ✓ shipped |
| M4 — 0002 nullability relaxed + CHECK | `0002_create_users.sql:48-128` has `first_name TEXT` etc. (no NOT NULL), `sex` CHECK now allows NULL, `profile_complete_requires_required_fields` CHECK present | ✓ shipped |
| M9-counterpart — 0008 OLD IS NOT NULL guard | `0008_immutable_fields_trigger.sql:47-52` guards each field with `OLD.<field> IS NOT NULL AND NEW.<field> IS DISTINCT FROM OLD.<field>` | ✓ shipped |
| M1 — cluster_resource_id available for ARN pattern | `infrastructure/modules/aurora/outputs.tf:24-27` exposes `cluster_resource_id` | ✓ shipped |
| B1 — new story 3.0 with VPC endpoints | PRD line 11-22, full ACs for both endpoints | ✓ in PRD |
| N6 — DynamoDB gateway endpoint bundled in 3.0 | PRD story 3.0 AC bullet 2 + bullet 3 | ✓ in PRD |
| D1 — context.md "Current phase" updated | context.md line 7 reads "Phase 3 — Lambda foundations. Phase 2 PR merged into development (commit e1f0e2d) and tagged `phase-2-complete`" | ✓ shipped |

### Residual gaps found in this pass

**R1. Drift in 0007's header comment (cosmetic, not a blocker).**
`0007_rls_app_user_and_policy.sql:22-23` reads: "For local docker-compose development, a separate `0007a_local_only_*` migration (NOT applied by yoyo against any AWS cluster) sets a static password matching docker-compose.yml — see infrastructure/db/README.md." But the final 21:30 decision was `infrastructure/db/local_init.sql` (outside `migrations/`, not a yoyo migration at all). No `0007a_local_only_*` file exists. The header comment references a file that doesn't exist and an approach that wasn't taken.

- Severity: minor (cosmetic — doesn't affect runtime, only documentation accuracy).
- Risk: a future engineer (or a future subagent reading 0007 for context) will hunt for a nonexistent `0007a_local_only_*` file.
- Fix: opportunistic — story 3.7's subagent will be touching the migrator path; a one-line comment update to point at `infrastructure/db/local_init.sql` is appropriate. Not worth blocking dispatch over.

**R2. Story 3.5's `src/` path shorthand vs the project's `infrastructure/` convention.**
Story 3.5 AC bullet 3 says `infrastructure/src/pytest.ini` (full path), but other story ACs (3.2, 3.3, 3.6, 3.7) use bare `src/layers/` and `src/functions/`. The phase-2 layout has everything under `infrastructure/` (no top-level `src/`). The intended interpretation is `infrastructure/src/...`; bare `src/` is just shorthand.

- Severity: minor (clarity — a subagent reading the PRD in isolation could ambiguously create a top-level `src/` instead of `infrastructure/src/`).
- Risk: low — story 3.5 explicitly anchors at `infrastructure/src/pytest.ini`, which pins the location. Subagents for 3.2/3.3 will see story 3.5's anchor and follow it.
- Fix: not required; the anchor in 3.5 disambiguates. Subagent briefs should re-anchor the path at dispatch time.

**R3. Story 3.6's "VPC config wired to phase 1 private subnets, environment variable DB_SECRET_ARN pointing at the knotify-<env>-app-user-credential secret (the migrator creates this in story 3.7)" — apply-time ordering.**
The cognito_post_confirmation Lambda (3.6) depends on the app_user credential secret existing. The migrator (3.7) creates it. Both Lambdas are defined in the dev environment's main.tf. Terraform plan/apply: the cognito Lambda's `environment.DB_SECRET_ARN` value is a static string `arn:...:secret:knotify-dev-app-user-credential-*` (or constructed name without the random suffix). Terraform will not see a dependency between cognito Lambda and the migrator Lambda's runtime side-effect (the secret is created at INVOKE time, not at apply time of the migrator Lambda resource).

If the `null_resource` from 3.7 (which invokes the migrator) hasn't run yet, the cognito Lambda is deployed but its `DB_SECRET_ARN` points at a secret that doesn't exist. The cognito Lambda is not yet wired to Cognito (3.6 AC explicitly intentional — that's phase 4), so it will never be invoked at this point. No runtime impact in phase 3.

- Severity: none for phase 3.
- Risk for phase 4: when phase 4 wires the trigger, the migrator MUST have run first (which it will have, since 3.7's null_resource fires during 3.7's apply). The dev environment's phase-4 apply will run the migrator (via its existing null_resource triggers) before the User Pool wiring takes effect anyway, since the cognito Lambda is referenced by aws_cognito_user_pool_lambda_config in phase 4 and that resource forces ordering through the Terraform graph.
- Action: no change needed. Document in story 3.6 dispatch brief that the secret's existence is guaranteed by 3.7's null_resource which runs in the same apply pass (3.7 dispatches AFTER 3.6 per topological order — but null_resource lifecycle is `apply` time, not `create` time, so the per-resource ordering inside one `terraform apply` handles this). Actually wait — story 3.6 `depends_on` includes 3.7? Let me check.

  Re-checking PRD: story 3.6 `depends_on: [3.0, 3.1, 3.2, 3.3, 3.4, 3.5]`. Story 3.7 `depends_on: [3.0, 3.1, 3.3, 3.4]`. They are siblings — 3.6 does NOT depend on 3.7. In the topological dispatch order, 3.7 happens AFTER 3.5 (because 3.5 depends on 3.2/3.3 which 3.7 doesn't, but 3.7 depends on 3.0/3.1/3.3/3.4 — same generation as 3.5). Both 3.6 and 3.7 are eligible after their dependencies complete. Per `/implement-phase`'s topological order, 3.6 comes first in PRD order; but the strict-serial dispatch rule means one finishes before the next starts.

  If 3.6 dispatches BEFORE 3.7 (PRD order), then at the moment 3.6's `terraform apply` runs in dev, the cognito Lambda gets deployed with `DB_SECRET_ARN=arn:...:secret:knotify-dev-app-user-credential-XXXXXX` but that secret doesn't exist yet. The Lambda is not wired, never invoked. Phase 3 still completes cleanly. Phase 4 wiring later assumes 3.7 ran by then. ✓

- Conclusion: no action. The apparent ordering quirk resolves because the cognito Lambda is not invoked until phase 4, by which time 3.7's `null_resource` has run (in this phase's apply or a later one).

**R4. `cognito_trigger` IAM role's secret ARN pattern — constructed name uses `var.environment`.**
PRD story 3.4 AC bullet 3 reads: `arn:...:secret:knotify-${var.environment}-app-user-credential-*`. The dev environment passes `environment=dev` to the iam_roles module. The migrator creates `knotify-dev-app-user-credential` (without a random suffix, since the migrator uses `aws_secretsmanager_secret` named literally). AWS Secrets Manager auto-appends a random 6-char suffix to secret ARNs by default, BUT only when using `CreateSecret` without a Name parameter or with `tags` that conflict. When using a fixed Name via boto3 `create_secret(Name='knotify-dev-app-user-credential')`, the ARN suffix is still randomized — AWS always appends `-XXXXXX` for secret ARNs.

- Conclusion: the `-*` suffix in the policy IS correct (it matches AWS's random 6-char suffix). ✓
- Note: store the actual created secret ARN as a Terraform output OR reference it via `data.aws_secretsmanager_secret` from the dev env so Phase 6+ Lambdas don't have to wildcard. Out of scope for phase 3.

**R5. Story 3.7 AC item 5 (`null_resource` + `local-exec` aws lambda invoke) — assumes AWS CLI on the apply host.**
The `local-exec` runs `aws lambda invoke ...`. Phase 1 story 1.5 confirmed CI uses ubuntu-latest with `aws-actions/configure-aws-credentials@v4`. CI runners have aws-cli pre-installed. Local apply (rare in this project) requires aws-cli installed.

- Conclusion: standard assumption. No action.

### Drift since 21:30 resolutions

None found. context.md, the index, and the PRD are in sync. Phase 2 commit `e1f0e2d` matches the latest commit on `development` per `git log`. No external dependencies introduced that the PRD doesn't already validate.

### Summary

PRD is fit for dispatch. Only one cosmetic residual (R1, 0007 header comment drift) which can be cleaned up opportunistically during story 3.7. No blockers, no majors, no medium concerns. Five minor findings, four are non-actions and one is documentation.

**Recommendation: proceed with dispatch.**

---

## 2026-05-25 08:53 brainstorm (resume-pass — stories 3.0 and 3.1 shipped)

Re-running brainstorming before resuming dispatch of stories 3.2–3.7 (3.0 and 3.1 shipped in commits 72e1173 and f54e320; tracking issues #30/#31 closed; phase-3 PR #38 open). Focus: did the as-built lambda module + VPC endpoints leave any drift in the remaining stories' ACs?

### Verification of as-built artifacts against downstream ACs

| Downstream story expectation | As-built reality | Status |
|---|---|---|
| 3.6/3.7 — `vpc_config = { subnet_ids, security_group_ids }` object input | `infrastructure/modules/lambda/variables.tf:35-42` defines this exact object shape (defaults to `null` for out-of-VPC) | ✓ aligned |
| 3.6/3.7 — `layers` accepts list of layer ARNs | `variables.tf:23-27` `list(string)` default `[]` | ✓ aligned |
| 3.6/3.7 — `environment_variables` map merged with defaults | `main.tf:18-28` `merge()` with POWERTOOLS_SERVICE_NAME + LOG_LEVEL, consumer wins | ✓ aligned (N2 satisfied) |
| 3.6/3.7 — function uses arm64 + python3.14 by default | `variables.tf:14-21` defaults match | ✓ aligned |
| 3.7 — `aws_lambda_function.this.version` available for null_resource trigger | `outputs.tf` exposes `function_arn`, `alias_arn`, `invoke_arn`, `function_name`, `log_group_name` — but NOT `function_version` | ⚠ see F1 below |
| 3.6/3.7 — `DB_SECRET_ARN` env var routes through Secrets Manager Interface endpoint | `outputs.tf:26-29` exposes `secretsmanager_vpc_endpoint_id`; endpoint exists with private_dns_enabled=true so the standard hostname resolves to the endpoint automatically — no env var routing logic required | ✓ aligned |
| 3.7 — db_migrator reaches Aurora on 5432 via aurora_security_group_id | `outputs.tf:21-24` exposes `aurora_security_group_id`; phase 1 sg-aurora ingress already allows from sg-lambda | ✓ aligned |

### Residual findings

**F1. Lambda module does not output `function_version`.**
Story 3.7 AC bullet 5 requires `null_resource.triggers = { ..., lambda_version = aws_lambda_function.db_migrator.version }`. The dev environment's main.tf will reference `module.db_migrator.<something>` — but the module currently exposes `function_arn`, `alias_arn`, `invoke_arn`, `function_name`, `log_group_name` only. No `version` output.

- Severity: minor (one-line fix at consumption time).
- Resolution: story 3.7's subagent must either (a) add a `function_version` output to the lambda module as an in-passing change, or (b) reference `module.db_migrator.alias_arn` for re-trigger detection (the alias updates on every published version; using the alias ARN as a trigger string works but is opaque). Option (a) is cleaner and is a one-line additive change to `infrastructure/modules/lambda/outputs.tf`.
- Action: flag in story 3.7's dispatch brief. No PRD edit needed — the AC text says "aws_lambda_function.db_migrator.version" which is correct at the resource level; the module simply needs to expose it.

**F2. `make package` ordering vs `terraform apply` in 3.6/3.7.**
Lambda module's `filename` variable is REQUIRED (no default). Stories 3.6 and 3.7 will instantiate the module against build/cognito_post_confirmation.zip and build/db_migrator.zip respectively. Those zips do not exist until `make package FUNC=<name>` (story 3.5) runs. If a fresh clone runs `terraform apply` in dev without first running `make package`, the apply fails with "file not found" — annoying but loud and immediate, not a silent failure.

- Severity: minor (documentation / runbook concern).
- Resolution: story 3.5's README AC already documents the workflow ("make db-up, make package FUNC=, make test"). Story 3.7 should also update deploy.yml's apply-dev job to run `make package FUNC=db_migrator && make package FUNC=cognito_post_confirmation` BEFORE `terraform apply`, OR add a Terraform-native `null_resource` that runs the make target as a `local-exec` provisioner. The deploy.yml path is more transparent.
- Action: flag in story 3.7's dispatch brief. No PRD edit needed; the deploy.yml integration is implied by story 3.5's "make test" / "make package" workflow.

**F3. Story 3.4's `iam_roles` module uses data sources for partition / region / account.**
AC bullet 2 references `data.aws_partition.current.partition`, `data.aws_region.current.name`, `data.aws_caller_identity.current.account_id` inside the constructed-name ARN pattern. The networking module recently fixed an `aws_region.name → aws_region.region` deprecation under provider ~> 6.20 (per phase 3.1 commit message). Confirm: in provider 6.20, `data.aws_region.current.name` is deprecated in favor of `data.aws_region.current.region`. Story 3.4 AC text currently says `data.aws_region.current.name`.

- Severity: minor (one-character fix during 3.4 implementation).
- Resolution: subagent will see the deprecation warning at plan time and use `.region` instead. No PRD edit needed — the AC's intent is clear (it wants the region string); the attribute name is incidental.
- Action: flag in story 3.4's dispatch brief so the subagent doesn't copy-paste the deprecated form.

**F4. Cluster_resource_id construction for the master-secret ARN.**
Story 3.4 AC bullet 2 reads `arn:...:secret:rds!cluster-${module.aurora.cluster_resource_id}-*`. Verified: `infrastructure/modules/aurora/outputs.tf:24-27` exposes `cluster_resource_id`. The Aurora cluster has been applied to dev (phase 2 complete), so `module.aurora.cluster_resource_id` is a concrete string at plan/apply time — no null-during-plan trap. ✓

### Drift check (since 22:15 brainstorm)

| Item | Then (22:15) | Now (08:53) | Notes |
|---|---|---|---|
| context.md "Current phase" | "Phase 3 stories 3.0/3.1 not yet shipped" | "Stories 3.0 (VPC endpoints) and 3.1 (Lambda module skeleton) complete" | ✓ updated by subagents |
| Provider constraint | ~> 5.70 across modules | ~> 6.20 across all 4 modules (bumped by 3.1) | Cascade: story 3.4's iam_roles module must use ~> 6.20 too |
| Networking outputs | added secretsmanager_vpc_endpoint_id, dynamodb_vpc_endpoint_id | confirmed present | ✓ |
| `data.aws_region.current.name` | n/a | deprecated under 6.20 (already fixed in networking) | F3 above |

### Summary

PRD remains fit for dispatch. Four minor findings, all of which the subagents will encounter and resolve in passing — no PRD edits required. The 21:30 resolutions and 22:15 verification stand; nothing in the as-built 3.0/3.1 work invalidates the remaining stories' ACs.

**Recommendation: proceed with dispatch of stories 3.2 through 3.7.**

---

## 2026-05-25 15:34 brainstorm (story 3.7 pre-dispatch — stories 3.2/3.3/3.4/3.5/3.6 shipped)

Resume pass before dispatching the **last remaining story (3.7)** — DB migrator Lambda, app_user password generation, and the initial cluster-side migration run. Stories 3.2–3.6 shipped in commits 13d45d1, ea16a2b, 797d702, a0ed0a5 plus 3.2's earlier commit; tracking issues #32–#36 closed; PR #38 open against `development`. Focus: does anything in the as-built peer artifacts force a change in 3.7's ACs before dispatch?

### Verification of 3.7 dependencies against as-built peer artifacts

| 3.7 AC expects | As-built reality | Status |
|---|---|---|
| `db_migrator` IAM role with master-secret + app-user-credential perms (3.4) | `infrastructure/modules/iam_roles/main.tf:71-125` — both inline policies present, ARN patterns correct | ✓ aligned |
| Lambda module accepts `filename`, `vpc_config`, `layers`, `role_arn`, `memory_size`, `timeout`, `environment_variables` (3.1) | `infrastructure/modules/lambda/variables.tf` exposes all of these | ✓ aligned |
| `secretsmanager_vpc_endpoint_id` available for routing master-secret GET (3.0) | `infrastructure/modules/networking/outputs.tf` exposes it; endpoint has `private_dns_enabled=true` so standard hostname auto-resolves | ✓ aligned (no env-var routing logic needed) |
| `db_layer` includes psycopg2-binary aarch64 (3.3) | `infrastructure/src/layers/db/build.sh` pins `psycopg2-binary==2.9.12` | ✓ partial — see B3 below |
| `make package FUNC=<name>` produces a build/<name>.zip from src/functions/<name>/ (3.5) | `Makefile` lines 59-66 delegate to `infrastructure/src/scripts/build_package.py` | ✓ exists — see M-new-1 below |

### Findings

---

### BLOCKER — story 3.7 cannot ship as currently written

**B3. The db layer does NOT contain `yoyo-migrations` — story 3.7 AC 3 (`The function bundles yoyo-migrations and psycopg2-binary via the shared db layer from story 3.3`) is false as built.**

`infrastructure/src/layers/db/build.sh` (verified) pins `psycopg2-binary==2.9.12` and `pgvector==0.3.6` only. The db layer was scoped in story 3.3 around the Aurora-access wrapper (`knotify_db`); `yoyo-migrations` was never added because no peer story needed it yet.

Story 3.7 AC text explicitly says the function uses the db layer for both psycopg2 and yoyo, with the rationale "keeps the function package thin and means a yoyo upgrade ripples through one layer rebuild." With the layer as-built, only psycopg2 ripples through the layer; yoyo would have to be bundled directly in the function package or a new third layer.

**Recommended action:** Pick one of these, encode in the 3.7 dispatch brief:

  a. **Extend the db layer.** Edit `infrastructure/src/layers/db/build.sh` to also pin `yoyo-migrations==<x.y.z>` and rebuild. The layer version increments; `db_layer.layer_arn` references the new version on next apply. Cost: one line + a rebuild. Matches the AC's intent verbatim.
  b. **Bundle yoyo in the function's own requirements.txt.** Add `yoyo-migrations==<x.y.z>` to `src/functions/db_migrator/requirements.txt`. `build_package.py` will pip-install it into the function package (after stripping layer-provided distributions). Cost: function zip grows by a few hundred KB. AC text would need to be amended ("via the shared db layer" → "in the function package, with psycopg2 from the db layer").
  c. **New `migrator_tools` layer.** Heavy, no reuse — reject.

Recommend (a) — matches the AC verbatim, one-line layer edit, future yoyo upgrades touch one place. The db layer's compatible runtimes/architectures already match (python3.14/arm64). Pin yoyo to a specific 9.x release (currently `yoyo-migrations==9.0.0` or whatever the latest tested release at dispatch time is — let the subagent verify).

**This is a blocker because without it, story 3.7's first-AC handler — `from yoyo import read_migrations, get_backend` — will `ImportError` at runtime.**

---

### MAJOR

**M-new-1. `build_package.py` does not have a mechanism to package non-Python files outside `src/functions/<name>/` — story 3.7 AC 2 requires the function zip to contain `infrastructure/db/migrations/*.sql`.**

The migrations directory lives at `infrastructure/db/migrations/`. The function source is at `infrastructure/src/functions/db_migrator/`. Per as-built `build_package.py` (verified via Makefile delegation), the script copies `*.py` from the function dir and pip-installs requirements. There is no `--include-dir` or migration-bundling step. Without one, the deployed Lambda has no migrations to apply — `yoyo.read_migrations()` finds an empty directory.

**Recommended action:** Extend `build_package.py` (or add a 3.7-specific pre-build step in the Makefile) to copy `infrastructure/db/migrations/*.sql` and `*.rollback.sql` into the function package under a known subpath (e.g., `build/db_migrator/migrations/`). The handler then references `migrations_path = os.path.join(os.path.dirname(__file__), "migrations")`.

Cleanest path: add a `--include-dir <src>:<dest>` flag to `build_package.py` (generalizes for future functions that need static assets), and have the Makefile/dispatch brief pass `--include-dir infrastructure/db/migrations:migrations` when building `db_migrator`. Alternatively, a one-off shell snippet in the dispatch brief is acceptable since this is the only function with such a requirement in v1.

Encode in 3.7's dispatch brief. PRD AC 2 is correct in intent ("packages the migrations directory ... into the Lambda deployment .zip — exactly the files matching /^\d{4}_.+\.sql$/"); the gap is in the tooling that produces the zip.

**M-new-2. The `apply-dev` CI job in `.github/workflows/deploy.yml` does NOT run `make package FUNC=<name>` before `terraform apply` — so the cognito_post_confirmation.zip and db_migrator.zip referenced by `module.<>.filename` in dev/main.tf will not exist in CI.**

Verified: `deploy.yml` lines 173-202 show apply-dev steps = checkout → setup terraform → download tfplan artifact → terraform init → terraform apply tfplan. No `make package` step. No artifact upload from plan-dev that would carry the zips forward.

Story 3.6 already shipped `module.cognito_post_confirmation { filename = "${path.module}/../../../build/cognito_post_confirmation.zip" }` into dev/main.tf. If apply-dev runs today against `development`, terraform apply will fail with `"file not found: build/cognito_post_confirmation.zip"` UNLESS the package was built locally and somehow ended up on the runner — which it won't, since CI is a fresh ubuntu-latest.

This is a real gap that 3.6 introduced and 3.7 will compound (db_migrator.zip also needed). The phase-3 PR cannot merge cleanly into `development` without it.

**Recommended action:** In story 3.7, ALSO add to `deploy.yml`:

  - A `make package FUNC=cognito_post_confirmation && make package FUNC=db_migrator` step BEFORE `terraform plan` in both `plan-dev` and `plan-prod`.
  - The same in `apply-dev` and `apply-prod` BEFORE terraform apply (since the zip needs to exist at apply time too — the plan artifact won't carry the zip into apply-dev's runner).
  - Optionally use actions/upload-artifact in plan-dev to ship the zips alongside tfplan, then download in apply-dev. Cleaner; one build per pipeline run.

  Layer artifacts (`build/knotify-observability-layer.zip` and `build/knotify-db-layer.zip`) face the same issue — they're built by `infrastructure/src/layers/<name>/build.sh`. Add layer build steps too (or roll into a single `make package-all` target).

Pin in the 3.7 dispatch brief: "your scope includes the CI integration so phase 3 actually deploys end-to-end."

---

### MEDIUM

**M-new-3. Idempotency semantics of CreateSecret vs PutSecretValue not pinned in handler AC.**

Story 3.7 AC 1(f) says the migrator generates a 32-char password, then "writes it to a NEW Secrets Manager secret named `knotify-${env}-app-user-credential` (created on first run, PutSecretValue on subsequent runs)." The handler logic for deciding first-run vs subsequent-run is unspecified. Three patterns:

  a. **Exception-based**: `try CreateSecret except ResourceExistsException: PutSecretValue` — idiomatic, no extra API call when both calls succeed-path is fast.
  b. **Describe-first**: `try DescribeSecret except ResourceNotFoundException: CreateSecret else: PutSecretValue` — extra API call always, more "explicit" but slower.
  c. **PutSecretValue with conditional create**: not supported by the API for this case.

Recommended action: pin (a) in the dispatch brief and the handler unit tests should cover both code paths. AC 6 (`app_user_secret_action in {created, updated}`) already prescribes the response shape — the handler just needs the try/except to set it correctly.

**M-new-4. Story 3.7 `depends_on` is missing 3.5.**

Currently `depends_on: [3.0, 3.1, 3.3, 3.4]`. Story 3.7's AC 8 (integration test runs as part of `make test`) and the underlying build mechanism (the function zip is produced by `make package FUNC=db_migrator`) both depend on story 3.5's tooling. In a parallel-universe re-run where 3.5 had not yet shipped, 3.7 would have nothing to build with.

Risk in practice: zero — 3.5 already shipped, topological order is moot. But for correctness (and for the audit trail of future plan re-derivations), the dependency should be in the PRD.

Recommended action: opportunistic edit, add 3.5 to 3.7's `depends_on` list. Not a blocker.

---

### MINOR

**N-new-1. 0007 header comment still references nonexistent `0007a_local_only_*` migration (R1 from 22:15 pass, not yet fixed).**

`infrastructure/db/migrations/0007_rls_app_user_and_policy.sql:21-23` still reads: "For local docker-compose development, a separate `0007a_local_only_*` migration (NOT applied by yoyo against any AWS cluster) sets a static password matching docker-compose.yml — see infrastructure/db/README.md." No such file exists; the actual approach is `infrastructure/db/local_init.sql` (outside migrations/).

Story 3.7's subagent will be touching the migrator path and reading 0007 closely. Recommended: fix the comment in passing — point at `infrastructure/db/local_init.sql`. One-line edit. Not a blocker.

**N-new-2. Lambda module does not output `function_version` (F1 from 08:53 pass, not yet fixed).**

Story 3.7 AC 5 (null_resource triggers) requires `aws_lambda_function.db_migrator.version`. The dev env will reference `module.db_migrator.<output>`. As-built `infrastructure/modules/lambda/outputs.tf` exposes function_name/function_arn/alias_arn/invoke_arn/log_group_name — no `function_version`.

Recommended action: 3.7's subagent adds `function_version` as a one-line additive output to the lambda module. Alternative is to use `alias_arn` as a trigger string (less clear). Flag in the dispatch brief.

**N-new-3. `apply-time invocation` against prod Aurora is implicit at PROD_CUTOVER.**

Story 3.7 AC 8 says prod migrator + null_resource are authored but gated. When `DEPLOY_PROD=true` flips on, the first apply-prod creates the migrator Lambda AND immediately invokes it against the prod Aurora cluster — this is the desired cutover behavior. PRD AC 8 covers the "not deployed in this phase" half but doesn't explicitly note the at-flip behavior. Not strictly a 3.7 issue (correct behavior), but worth a one-line note in the dispatch brief so the subagent doesn't accidentally split the null_resource into a separate gated job.

---

### Drift summary

| Drift | Impact on 3.7 |
|---|---|
| db layer ships without yoyo-migrations | **Blocker B3** — fix during 3.7 |
| build_package.py lacks `--include-dir` for migrations | **Major M-new-1** — fix during 3.7 |
| deploy.yml has no `make package` step before terraform plan/apply | **Major M-new-2** — fix during 3.7 (also unblocks the already-merged story 3.6 from running cleanly on `development`) |
| Lambda module has no `function_version` output | **Minor N-new-2** — fix during 3.7 in passing |
| 0007 header references nonexistent file | **Minor N-new-1** — fix during 3.7 opportunistically |
| 3.7 depends_on missing 3.5 | **Medium M-new-4** — edit PRD or accept |

### Summary

Three real items need pre-dispatch agreement:
- **B3**: extend db layer with yoyo-migrations (recommended fix) OR amend 3.7 AC text to put yoyo in the function package.
- **M-new-1**: extend build_package.py with `--include-dir` (recommended) OR a Makefile-side pre-package copy for db_migrator.
- **M-new-2**: add `make package FUNC=...` (and layer builds) to deploy.yml's plan/apply jobs.

If the user picks `proceed`, the 3.7 dispatch brief MUST include all three so the subagent doesn't hit B3 mid-implementation. If the user picks `address`, edit the PRD (3.7 AC 3 — yoyo source) and ship the tooling/CI changes first. Recommend `proceed` with the dispatch brief carrying these resolutions — they're all in 3.7's reasonable scope ("DB migrator Lambda + initial cluster migration" naturally includes the CI plumbing to make it actually run).

