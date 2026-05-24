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
