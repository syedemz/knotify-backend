# Phase 4 brainstorm — Cognito

## 2026-05-28 brainstorm

Context: phase 3 shipped (PR #38 merged + follow-ups #43–#46). `cognito_post_confirmation` Lambda is deployed in dev but unwired; `cognito_trigger` IAM role exists; `knotify-dev-app-user-credential` Secrets Manager secret exists; `app.requesting_user_id` / `app.requesting_user_sex` GUCs and the `users.profile_complete_verified` column are live in dev Aurora.

Findings categorized BLOCKER (must resolve before dispatch) / MAJOR (resolve before story dispatch) / MEDIUM (worth tightening) / MINOR (cosmetic / forward-looking).

---

### BLOCKERS

**B1 — Story 4.4: id_token vs access_token claim — wrong target token.**
AC #1 says the handler writes
`response.claimsAndScopeOverrideDetails.idTokenGeneration.claimsToAddOrOverride.custom:profile_complete`.
This puts the claim **only in the ID token**.
Architecture §13a layer 2 (line 2108) and the phase 5 enforcement path both expect the claim on the **access token** — HTTP API's built-in Cognito JWT authorizer reads the access token by default, and §13a layer 3 says "exposes claims to business Lambdas via `event.requestContext.authorizer.jwt.claims`". If the claim lives only in the ID token, phase 5's per-route 403 gate has nothing to enforce on.

Fix: PRD must explicitly require BOTH `idTokenGeneration.claimsToAddOrOverride` AND `accessTokenGeneration.claimsToAddOrOverride` to set `custom:profile_complete`. Alternative: write to access token only, and tell the mobile app to read its own bootstrap state from `/v1/me` rather than the ID token. Pick one and pin in the AC.

**B2 — Story 4.4: PreTokenGeneration V2 trigger configuration is under-specified.**
AC #4 says "wires the lambda_config.pre_token_generation_config reference". `pre_token_generation_config` is the V2 block — it requires `lambda_version = "V2_0"` AND a separate `lambda_arn`. The V1 `pre_token_generation` field is a single ARN string and CANNOT override access token claims. The AC must explicitly require V2 (`lambda_version = "V2_0"`); otherwise an implementor will reach for the V1 field, the access-token claim never lands, and B1 silently regresses.

Fix: AC #4 pins `lambda_config.pre_token_generation_config { lambda_version = "V2_0", lambda_arn = … }` AND explicitly forbids the V1 `pre_token_generation` field. Also: V2 PreTokenGeneration requires the User Pool's `user_pool_add_ons.advanced_security_mode` to be at least `AUDIT` per AWS docs — verify against the provider's behavior; if so, this conflicts with story 4.5 defaulting `advanced_security_mode = "OFF"`. Resolve by either (a) bumping default to `AUDIT` in dev (free) and `OFF` documented as a hardening flip, or (b) confirming the provider lets `OFF` + V2 coexist (some sources say it does for trigger purposes).

---

### MAJOR

**M1 — Story 4.4: env var name drift from phase 3.**
AC #2 says `DB_SECRET_ARN pointing at the Aurora app_user credential secret`.
Phase 3 story 3.6 (already shipped) standardized on `DB_SECRET_NAME` and the **friendly secret name** (`knotify-${var.environment}-app-user-credential`), not the ARN. See `infrastructure/environments/dev/main.tf` line 279 and the inline comment: "boto3 resolves by name (wildcard ARN strings are not accepted by GetSecretValue)".

Fix:

**M2 — Story 4.1: schema attributes for `gender` and `birthdate` are dead code in v1.**
AC #2 lists `gender` and `birthdate` (with `given_name` / `family_name`) as schema attributes "populated by social-identity providers when available". But architecture §4.1 explicitly says "Out of scope for v1: social login (Google/Apple)". With no IDP federation in v1, these attributes are guaranteed to be NULL on every signup. The `cognito_post_confirmation` Lambda (already shipped in phase 3) reads them — and getting NULLs is the expected branch — so leaving them in the schema is harmless but misleading.

Worse: Cognito **standard schema attributes are immutable after pool creation**. Declaring `given_name`/`family_name`/`gender`/`birthdate` now and later wanting to change a property (e.g. `mutable`, `required`, length constraints) means rebuilding the User Pool — which means re-signing-up every existing user. For four attributes that are not used at signup in v1, this is locking in a decision that might bite at the federation cutover.

Fix options (pick one in AC #2):

- (a) Drop `gender` and `birthdate` from the schema for v1; add them at the federation cutover (which will be a planned breaking change anyway).
- (b) Keep all four with explicit comments that they exist for forward-compat with social IDPs, and require both phase 4 PRD AC and architecture §4.1 to be updated so future-Claude doesn't trip on "but social login is out of scope".

**M3 — Story 4.3: wiring resource ambiguity.**
AC #1 says "aws_cognito_user_pool resource (or aws_cognito_user_pool_lambda_config separation)". There is no standalone `aws_cognito_user_pool_lambda_config` resource in the AWS provider — the lambda triggers are configured via the `lambda_config` nested block on `aws_cognito_user_pool` itself. The ambiguity will cost the implementor a search.

Fix: pin to "the `lambda_config` block on `aws_cognito_user_pool` references both `post_confirmation` (V1, single ARN string) and `pre_token_generation_config` (V2 nested block)". Drop the "or separation" wording.

**M4 — Story 4.6: SRP auth flow vs. test client.**
AC #4 says the test calls `initiate_auth` and inspects `id_token`. The app client (story 4.2) has `explicit_auth_flows = [ALLOW_USER_SRP_AUTH, ALLOW_REFRESH_TOKEN_AUTH]` — SRP is a multi-step proof-of-knowledge dance that boto3 supports only via `pycognito` or a hand-rolled SRP client. Without `ALLOW_ADMIN_USER_PASSWORD_AUTH` (or `ALLOW_USER_PASSWORD_AUTH`), `admin_initiate_auth` with USER_PASSWORD_AUTH won't work and SRP must be implemented in the test.

Fix options:

- (a) Add `ALLOW_ADMIN_USER_PASSWORD_AUTH` to a **second app client** dedicated to integration testing (keeps the production app client SRP-only). Acceptance: app client `knotify-${env}-integration-test` exists in dev only, not in prod.
- (b) Pull in `pycognito` (or implement SRP) and keep one app client. Heavier code path.
- Pick (a) — minimal blast radius and avoids leaking ADMIN auth into the React Native client config. AC updated accordingly.

**M5 — Story 4.6: claim-target mismatch repeats B1.**
AC #4 inspects `id_token` for `custom:profile_complete`. If B1 is resolved by writing to the access token (with or without id token), this AC must change accordingly. Cannot be fixed independently of B1.

---

### MEDIUM

**Md1 — Story 4.4: cold-start latency on PreTokenGeneration is on the critical sign-in path.**
PreTokenGeneration runs on **every** `InitiateAuth` and **every** refresh. The Lambda is VPC-attached (db layer + Secrets Manager). On cold start with ENI attachment, that's 200ms–1s added to sign-in. PostConfirmation runs once per signup so latency hides; PreTokenGeneration runs forever. Refresh interval is 1h access token TTL, so a moderately active user hits this 24x/day per device.

The notes block already calls this out as "runs inside warm Lambda container after first invocation" — but that's only true at non-trivial sustained traffic. Knotify pre-launch traffic is ~0 RPS; every PreTokenGeneration invocation will likely be a cold start.

Fix: notes block should explicitly acknowledge "first sign-in / first refresh per cold container will pay 200ms–1s ENI penalty; acceptable for pre-launch volumes; provisioned concurrency considered in phase 11 hardening if observed". Adds no AC change — just sets expectations.

**Md2 — Story 4.4: race between PostConfirmation and PreTokenGeneration.**
PostConfirmation fires on `confirm_signup`. The user's next action is `initiate_auth`, which fires PreTokenGeneration. In normal flow PostConfirmation completes well before the next sign-in attempt — but if PostConfirmation **failed** (or has been called but is still flying), PreTokenGeneration's "users row missing → return `false`" path is the correct fallback.

The AC already covers this. But: if PostConfirmation later succeeds (retry) and writes the row with `profile_complete_verified = false`, the next refresh's PreTokenGeneration reads correctly. Good. The edge case to flag: if PostConfirmation **silently dropped** the row (e.g., the missing-email path from phase 3 AC #3), PreTokenGeneration will return `false` forever — the user is stuck. There is no current recovery path. Document as a known operational gap; do not add code in phase 4.

Fix: add a note to story 4.4 acknowledging this. No AC change.

**Md3 — Story 4.4: IAM role reuse — single `cognito_trigger` role for two different Lambdas.**
Phase 3 story 3.6 attached the `cognito_trigger` role to `cognito_post_confirmation`. Story 4.4 now attaches it to `cognito_pre_token_generation`. Both need VPC + `GetSecretValue` on the same app_user credential, so permissions overlap exactly. But:

- The IAM role's trust policy (lambda.amazonaws.com) is fine for both.
- The role NAME (`cognito_trigger`, singular) is now misleading.
- If either Lambda later needs a permission the other doesn't (e.g., PreTokenGeneration needs to write metrics; PostConfirmation needs SES to send a welcome email), they'd diverge and require a split.

Fix: keep the shared role for now (KISS), but add a notes-block flag: "Shared with `cognito_post_confirmation`; split if their permission sets diverge". No AC change.

**Md4 — Story 4.6: test user cleanup leaves Aurora row.**
AC #5 says `admin_delete_user` cleans up the Cognito user; the Aurora row is orphaned ("manual cleanup documented"). Phase 9 (account deletion) will eventually handle this. But the test will likely be re-run dozens of times during phase 5+ debugging — each run accumulates a dead Aurora row. With a one-time `test+<random>@example.com` email pattern, each gets a new Cognito sub, so the Aurora rows have distinct PKs. No correctness bug. Just gross.

Fix: AC #5 should explicitly require the test to also `DELETE FROM users WHERE user_id = '<sub>'` in its teardown — uses the master DB credential (test-only context, not the app_user RLS-scoped path). Avoids accumulation; doesn't pre-empt phase 9's actual soft-delete.

**Md5 — Story 4.1: schema attribute mutability is a one-shot lock-in.**
Quick note: every schema attribute declared in the user pool resource is **set at creation and cannot be changed afterward**. Terraform will tell you to destroy + recreate the pool if you change any of: `name`, `attribute_data_type`, `required`, `mutable`, length/numeric constraints. This is industry-standard but worth surfacing once because the implementor might be tempted to "tighten constraints later".

Fix: notes block on story 4.1: "User Pool schema attributes are immutable after creation. Get this right the first time — changing later means rebuilding the pool and re-signing-up every user". No AC change.

---

### MINOR

**Mn1 — Story 4.1: MFA = "OPTIONAL" semantics.**
"OPTIONAL" means the MFA setup UI is **available** to users; it does not enforce. That's compatible with the "deferred to phase 11" intent. But for v1 launch with no marketing of MFA, "OFF" might be cleaner (no MFA UI). Confirm intent.

**Mn2 — Story 4.5: `advanced_security_mode = "OFF"` interacts with V2 PreTokenGeneration (see B2).**
If B2's resolution requires `AUDIT` minimum, story 4.5's default must change too. Cross-link in the AC.

**Mn3 — Cross-phase: Phase 5 story 5.1 needs the issuer URL.**
Phase 5's JWT authorizer needs `https://cognito-idp.<region>.amazonaws.com/<user_pool_id>`. Phase 4's `module.cognito` outputs `user_pool_id` and `user_pool_arn` — phase 5 can reconstruct, or add `user_pool_endpoint` (or `issuer`) to cognito module outputs for ergonomic. Trivial; flagging only.

**Mn4 — Story 4.3: verification command in dev.**
AC #3 says "describing the user pool via aws cognito-idp describe-user-pool" — fine for an integration test. Suggest the AC reword to: "After dev apply, `aws cognito-idp describe-user-pool --user-pool-id <id> | jq .UserPool.LambdaConfig.PostConfirmation` returns the post-confirmation Lambda ARN, AND `.LambdaConfig.PreTokenGenerationConfig.LambdaArn` returns the PreTokenGeneration Lambda ARN." (Adds the second check; ties 4.3 and 4.4 verification together.)

**Mn5 — Story 4.4: README documentation timing.**
AC #6 says the function's README documents the latency budget. Trivial to forget. Keep — but flag it explicitly because phase 3 had multiple README acceptance criteria across stories and they were the most-likely-skipped item per the brainstorm pattern.

---

### External-dependency / assumption gaps

- **AWS provider 6.x feature gating**: `pre_token_generation_config` V2 block requires `aws` provider >= 5.50 or thereabouts. Phase 3 bumped to `~> 6.20`. Confirm 6.20 supports V2 `lambda_version="V2_0"` in `pre_token_generation_config`. (Spot check the provider changelog; almost certainly fine, but pin once.)
- **Cognito service quotas**: User pool count per account is 1000 (default). Not a concern.
- **dev IAM Identity Center / GitHub OIDC**: phase 3 used static IAM keys for CI. Phase 4 introduces no new auth surface for CI; existing keys are sufficient.

---

### Summary

Two BLOCKERS (B1 wrong-token claim, B2 V2 trigger config under-spec'd), three MAJORs (env-var drift M1, dead schema attrs M2, wiring ambiguity M3, SRP auth flow M4 — actually 4 MAJORs), five MEDIUMs and five MINORs. All resolvable by PRD edit; no architecture rewrite required.

The two blockers compound: if 4.4 ships claiming the ID token only, and 4.5/4.6 verify against the ID token, phase 5's per-route gate has no enforcement surface and §13a layer 2 is effectively absent. This is silent — every test passes — until the first phase-5 business endpoint tries to read the claim from the access-token authorizer context and finds nothing.

Recommendation: address B1, B2, M1, M2, M3, M4 in the PRD before dispatching any story. The Md/Mn items can ride forward as notes-block updates without re-running the brainstorm.

---

## 2026-05-29 brainstorm (re-run after PRD updates)

PRD was updated 2026-05-29 with the resolutions confirmed in `questions/phase4brainstormanswers.txt`. This second pass validates the resolutions and surfaces any new gaps introduced by the edits.

### Resolution audit — all clean

| ID | Resolution path | Verified in PRD |
|---|---|---|
| **B1** | Claim written to BOTH `idTokenGeneration` AND `accessTokenGeneration` | 4.4 AC #1, 4.4 AC #5 (test), 4.6 AC #4 (e2e test) — all three call out the two-token contract explicitly |
| **B2** | Pin V2 + bump advanced_security_mode to AUDIT | 4.4 AC #4 forbids V1, 4.5 AC #1 defaults AUDIT, 4.1 AC #4 wires the variable into `user_pool_add_ons` |
| **M1** | `DB_SECRET_NAME` + friendly name | 4.4 AC #2 |
| **M2** | Keep all four schema attrs as forward-compat | 4.1 AC #2 + architecture.md §4.1 forward-compat note |
| **M3** | Pin to `lambda_config` block | 4.3 AC #1 |
| **M4** | Dev-only second app client with ADMIN_USER_PASSWORD_AUTH | 4.2 AC #3/#4/#5 + 4.6 AC #4 |
| **Md1** | Cold-start latency documented in 4.4 README | 4.4 AC #6 |
| **Md4** | Aurora teardown via master credential | 4.6 AC #5 |

### New issues introduced by the edits (none are blockers)

**N1 — Story 4.1 ↔ 4.4 Terraform dependency arc.**
4.4 AC #4 says the pool's `lambda_config.pre_token_generation_config.lambda_arn = module.cognito_pre_token_generation.lambda_arn`. The aws_cognito_user_pool (story 4.1) and the PreTokenGeneration Lambda module (story 4.4) now reference each other through Terraform's DAG: User Pool → Lambda ARN (for lambda_config), and `aws_lambda_permission` → User Pool ARN (for source_arn). Terraform handles this fine because the Lambda function ARN is known at plan time and the lambda_permission is a separate node — no cycle. But operationally, **Cognito does not validate the trigger Lambda at User Pool creation time for V2 pre_token_generation_config** (unlike some V1 triggers), so the apply order is: Lambda → User Pool (with trigger set) → Lambda Permission. First sign-in attempt is the first true validation.

Severity: NONE — Terraform's DAG resolves this correctly. Flagging for the implementor so they know to expect the standard ordering and not chase a phantom cycle.

**N2 — `count` + output expression for the dev-only app client.**
4.2 AC #5 outputs `integration_test_app_client_id` as a string — empty in prod, the dev-only id in dev. With `count = var.environment == "dev" ? 1 : 0`, the resource becomes a list and a direct `aws_cognito_user_pool_client.integration_test.id` would fail at plan time in prod (no zero-th element). The expected idiom is:

```hcl
output "integration_test_app_client_id" {
  value = try(aws_cognito_user_pool_client.integration_test[0].id, "")
}
```

Severity: MINOR — implementor will figure it out, but pinning the idiom in the AC would save the search. Optional clarification; not blocking.

**N3 — IAM permissions for the 4.6 teardown to read the Aurora master secret.**
4.6 AC #5 has the test connect to Aurora as master to issue `DELETE FROM users …`. The master credential lives in `rds!cluster-<resource_id>` (managed by RDS). Who runs the test, and does their IAM principal have `secretsmanager:GetSecretValue` on that ARN?

Two execution contexts:
  - Local dev (developer runs `pytest -m integration` against dev cloud): the developer's AWS profile needs `secretsmanager:GetSecretValue` on `rds!cluster-…`. This is already true for any developer admin-shaped role in the dev account.
  - CI (if the test eventually runs in deploy.yml as a post-apply gate): the CI role needs the same permission added to its policy.

For phase 4 we're not adding CI integration-test execution (that's hardening territory), so this only matters for the local-dev path. Implementor should document the required IAM in the test's README. Severity: MINOR.

**N4 — Story 4.5 cost flag.**
The 4.5 notes block now documents the Cognito Plus-plan cost activation at advanced_security_mode = AUDIT. The "Confirm cost is acceptable, or fall back to investigating whether the AWS provider lets OFF + V2 coexist" instruction effectively asks the implementor to do an empirical spike: try apply with OFF first, if it fails fall back to AUDIT. That's the pragmatic path. Flagging that the implementor's first-apply path may iterate — and that the dev cost is the only signal until prod traffic exists. Severity: NONE — already in the notes.

**N5 — MFA = "OPTIONAL" left unaddressed.**
Mn1 from the first brainstorm raised whether `mfa_configuration="OPTIONAL"` vs `"OFF"` was intended. The PRD still says OPTIONAL. User did not provide guidance. Going with "OPTIONAL" is harmless (users can opt in via the React Native UI if Amplify exposes it; otherwise it just sits dormant) but if the team's intent is "no MFA UI surface in v1, hardened in phase 11", "OFF" is cleaner. Severity: COSMETIC. Flagging once more so the implementor is aware; not blocking.

### Verdict

PRD is dispatch-ready. The four findings above are minor/informational — none rise to BLOCKER or MAJOR. N1 (Terraform arc) and N4 (cost spike) are operational notes for the implementor; N2 (Terraform output idiom) and N3 (IAM for teardown) are micro-clarifications. N5 (MFA) is a cosmetic intent question the user may revisit later.

Recommendation: PROCEED.
