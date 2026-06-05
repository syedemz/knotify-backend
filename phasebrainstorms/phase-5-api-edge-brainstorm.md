# Phase 5 brainstorm — API edge (HTTP API + CloudFront + WAF)

## 2026-06-05 18:30 brainstorm

Context: phase 4 (Cognito) shipped — PR #53 squash-merged into development as 9af5ae0 with follow-up hotfixes #54–#57. User Pool deployed in dev with: production SRP app client (`knotify-dev-app`), dev-only integration-test app client (`knotify-dev-integration-test`, `ALLOW_ADMIN_USER_PASSWORD_AUTH`), post-confirmation trigger, V2 PreTokenGeneration trigger writing `custom:profile_complete` to BOTH id and access tokens, `advanced_security_mode = "AUDIT"`. Cognito module outputs `user_pool_endpoint` (the issuer URL) and `app_client_id` / `integration_test_app_client_id`. Lambda module exposes `function_arn` / `function_version` / `alias_arn` (live alias). Layers (observability, db) deployed. Aurora reachable from VPC. `dev/main.tf` has a single `provider "aws"` in `eu-central-1` — **no `us_east_1` alias declared yet**. Prod deploys are paused (`docs/PROD_CUTOVER.md`).

Findings categorized BLOCKER (must resolve before dispatch) / MAJOR (resolve before story dispatch) / MEDIUM (worth tightening) / MINOR (cosmetic / forward-looking).

---

### BLOCKERS

**B1 — Story 5.6: HTTP API does not natively reject by header — the enforcement mechanism is unspecified, and the choice cascades into story 5.1.**
AC #1 says "A request authorizer Lambda **or** an API Gateway integration policy rejects any request whose `x-knotify-edge-secret` header does not match…". Two problems:

- HTTP API (`apigatewayv2`) does **not** support resource policies the way REST API does. There is no `aws_apigatewayv2_api` policy field; the REST-API-only `aws_api_gateway_rest_api_policy` cannot be reused. "API Gateway integration policy" is not a real option for HTTP API.
- HTTP API allows **exactly one** authorizer per route — JWT **or** Lambda, never both. If the answer is "Lambda authorizer that does the header check", then story 5.1's JWT authorizer disappears and the authorizer must validate both the Cognito JWT (against JWKS) AND the edge secret. That is a different module, different IAM, different test surface.

Realistic options the PRD must pick between:

- (a) Replace the built-in JWT authorizer with a **Lambda authorizer** that does both checks. Story 5.1's AC #2 must be rewritten; story 5.6 owns the Lambda authorizer; story 5.7's hello function reads claims from `event.requestContext.authorizer.lambda` instead of `.jwt`.
- (b) Keep the built-in JWT authorizer; **every business Lambda** enforces the edge-secret check itself (probably via a shared helper in the observability layer). 403 happens in-function. Story 5.6 then ships the shared helper + a unit test, not an authorizer.
- (c) Use a **CloudFront Function** (viewer-request) to inject the secret AND a separate `aws_apigatewayv2_route_response`/Lambda integration that rejects in-function. Effectively (b) with a thinner skin.

Architecture §4.2 line 241 hand-waves this as "HTTP API rejects requests without it" but doesn't pick a mechanism. The PRD must pin one before dispatch — otherwise the implementer of 5.6 will design one and the implementer of 5.7 will assume another.

**Recommendation:** pick (b). Keep story 5.1's built-in JWT authorizer (it's free, native JWKS validation, exposes claims via `.jwt.claims` for layer-3 enforcement in §13a). Story 5.6 ships a `knotify_edge.require_edge_secret(event)` helper in the observability layer + the smoke hello in 5.7 wires it. AC #1 of 5.6 becomes: "shared helper raises 403 when `event['headers'].get('x-knotify-edge-secret') != $SECRET`; secret name pulled from env var injected by Terraform from `module.cloudfront.edge_secret` (sensitive)." Per-route enforcement is the same pattern §13a layer 3 already uses for `custom:profile_complete`.

**B2 — Story 5.2 + 5.5: ACM/Route 53 chicken-and-egg with provider-alias gap.**
Story 5.2 creates ACM in `us-east-1` with `validation_method = "DNS"` and outputs validation records. Story 5.5 creates Route 53 records (including the validation records). Story 5.3 needs the ACM cert in `ISSUED` state for CloudFront's `viewer_certificate`. The PRD `depends_on` graph reads:

- 5.2: `depends_on: []`
- 5.5: `depends_on: [5.2, 5.3]`  ← forces 5.3 first
- 5.3: `depends_on: [5.1, 5.2]`  ← needs ACM ISSUED, which needs 5.5 records first

This is a circular dependency in the **build order**. Terraform's actual resource-graph handles the cycle (validation records can be created in parallel with `aws_acm_certificate_validation` waiting), but the **story execution order** is broken — story 5.3 cannot complete until 5.5's DNS validation records exist.

Additionally: `dev/main.tf` and `prod/main.tf` currently declare **only** `provider "aws" { region = "eu-central-1" }`. ACM-for-CloudFront and WAF-scope-CLOUDFRONT both require `us-east-1`. No story owns adding the `provider "aws" { alias = "us_east_1", region = "us-east-1" }` block to either environment.

**Fix:** restructure stories so:

- 5.2 owns ACM creation **and** `aws_acm_certificate_validation` (the wait); the Route 53 *validation* records live in 5.2 too OR 5.2 declares a separate `dns_records` module call. Pick one — splitting validation across two stories deadlocks.
- 5.5 owns only the **public-facing A-alias** record (CloudFront alias). `depends_on: [5.3]` only.
- A new tiny story 5.0 (or fold into 5.1) adds the `us_east_1` provider alias to both env `main.tf` files. Without it, 5.2 and 5.4 will fail `terraform validate`.

---

### MAJOR

**M1 — Story 5.1: JWT authorizer `audience` is singular but phase 4 shipped two app clients.**
AC #2 says `audience equal to the app client id`. The `aws_apigatewayv2_authorizer.jwt_configuration` block's `audience` field is a **list**, and Cognito issues JWTs with `aud = <app_client_id>` set to whichever client the token was minted for. Phase 4 shipped:

- `module.cognito.app_client_id` — production SRP-only client
- `module.cognito.integration_test_app_client_id` — dev-only `ALLOW_ADMIN_USER_PASSWORD_AUTH` client, empty string in prod

If story 5.1 wires only the production client id, story 5.7's E2E test (which mints tokens via `admin_initiate_auth` against the integration-test client) will fail JWT validation with `aud` mismatch.

**Fix:** AC #2 must require `audience` to include **both** app clients in dev (`[module.cognito.app_client_id]` plus `module.cognito.integration_test_app_client_id` when non-empty), and just `[module.cognito.app_client_id]` in prod. Pattern: `audience = compact([module.cognito.app_client_id, module.cognito.integration_test_app_client_id])`.

**M2 — Story 5.3: CloudFront `forwarded_values` is deprecated in provider 6.x; dev `viewer_certificate` branch unspecified.**
Two sub-issues:

- Phase 3 bumped the provider to `~> 6.20`. In provider 6.x, the `default_cache_behavior.forwarded_values` block is deprecated and triggers a warning; the modern pattern is `cache_policy_id` + `origin_request_policy_id` (using AWS-managed `Managed-CachingDisabled` and `Managed-AllViewer`). AC #4 says "forward all headers, query strings, cookies; min_ttl=0, default_ttl=0, max_ttl=0" which reads like the legacy `forwarded_values` shape.
- AC #3 says `viewer_certificate` uses the ACM cert. When `var.domain_name` is empty in dev (per 5.2 AC #3), there is no ACM cert — the implementer needs to fall back to `cloudfront_default_certificate = true`. PRD doesn't acknowledge this branch.

**Fix:** AC #4 must pin "use AWS-managed `Managed-CachingDisabled` cache policy and `Managed-AllViewer` origin request policy (no `forwarded_values`)". AC #3 must add the dev branch: "when `var.domain_name == ""`, set `viewer_certificate.cloudfront_default_certificate = true` and skip `acm_certificate_arn`; `aliases` becomes empty."

**M3 — Story 5.4: WAF managed-rule override action and provider alias.**
- The PRD doesn't specify `override_action = "count"` vs `"none"` for the managed rule groups. Default is to leave the managed rules enforcing as-is, but `AWSManagedRulesCommonRuleSet` famously false-positives on JSON payloads with quotes (signup, profile patches in phase 6). Starting in `count` mode for the first deploy and flipping to enforce after monitoring is the conventional rollout.
- The WAF web ACL with `scope = "CLOUDFRONT"` requires the `aws.us_east_1` provider alias — see B2. PRD says `(provider alias us_east_1)` in AC #1 but no story creates the alias declaration.

**Fix:** AC #1 picks a starting posture (recommend `override_action = "count"` for SQLi + Common, `none` for KnownBadInputs + rate-based, with a notes-block plan to flip to `none` in phase 11 hardening). Cross-reference B2 for the provider alias.

**M4 — Story 5.6: integration-test path needs the execute-api URL exposed; AC#2 assumes direct API access bypassing CloudFront.**
AC #2 says "An integration test that calls the HTTP API directly (bypassing CloudFront) with a valid Cognito JWT but no edge-secret header receives HTTP 403". For this to work, the test needs the raw `execute-api` URL (the HTTP API's default endpoint), which story 5.1 outputs as `execute_api_endpoint`. The story chain must thread this output through to a test fixture or `.env` file the integration tests can read. Without it the test cannot run.

**Fix:** add an AC to story 5.6 (or to a small extension of 5.7): "Terraform writes `module.api_gateway.execute_api_endpoint` and `module.cloudfront.distribution_domain_name` to a test-config file (e.g., `infrastructure/src/tests/integration/.env.test` or via an SSM parameter the test harness reads); test fixtures pick them up." This mirrors how phase 4.6 picked up `module.cognito.user_pool_id` and `integration_test_app_client_id`.

**M5 — Story 5.7: drift on the Cognito helper reference.**
AC #2 says "reusing the helper from phase 4 story 4.5". Story 4.5 was `advanced_security_mode` wiring, not a Cognito helper. The actual sign-in helper is the inline boto3 dance in `infrastructure/src/tests/integration/test_cognito_signup.py` (phase 4 story 4.6). If the helper should be extracted/shared, the phase 5 E2E test should either (a) refactor that test to expose a fixture, or (b) duplicate the dance.

**Fix:** rewrite AC #2 to point at the right artifact: "reuse the boto3 sign-up + admin_confirm + admin_initiate_auth pattern from `infrastructure/src/tests/integration/test_cognito_signup.py` (phase 4 story 4.6) — extracting it into `infrastructure/src/tests/integration/conftest.py` as a `signed_in_user` fixture is acceptable." Pick one.

**M6 — Story 5.5: `aws_route53_record` count/conditional with empty `var.domain_name` needs explicit shape.**
AC #3 says "In dev, when var.domain_name is empty, no Route 53 resources are created". The standard Terraform idiom for this is `count = var.domain_name == "" ? 0 : 1` on each `aws_route53_record`. PRD should pin the pattern (matching phase 4.2's count idiom on `integration_test_app_client_id`) so the implementer doesn't reach for a `dynamic` block or `for_each` workaround.

**Fix:** AC #3 specifies the count idiom and adds: "module accepts `hosted_zone_id` as an optional `string` with default `""`; resources count to zero when either `domain_name` or `hosted_zone_id` is empty (no half-configured zone errors)."

---

### MEDIUM

**Md1 — Story 5.1: HTTP API access logging unset.**
No AC says anything about CloudWatch access logs. Architecture §10.6 mandates 7-day retention on all log groups; without explicit access-log configuration, HTTP API does not emit any. For phase-5 E2E debugging (the 403/401/200 assertions of 5.6/5.7 will require log inspection if they fail), access logs are valuable. Cost is negligible at pre-launch RPS.

**Fix:** add AC: "default stage configures `access_log_settings.destination_arn` to a `aws_cloudwatch_log_group` (7-day retention) and a JSON `format` field capturing request id, status, route key, integration latency, auth latency". Use the same retention pattern as other log groups in the project.

**Md2 — Story 5.3: `random_password` special characters can produce invalid HTTP header values.**
AC #2 says "value sourced from a `random_password` resource (length 64)". `random_password` defaults to a charset that includes `~!@#$%^&*()-_=+[]{}<>:?` — some of which are not valid in HTTP header values (CRLF-adjacent characters, `:` is the header separator). CloudFront's origin custom header value is restricted to a subset of ASCII; values containing `:` or special control characters will silently fail.

**Fix:** AC #2 adds `special = false` (or `override_special = "-_"`) to keep the secret in `[A-Za-z0-9-_]`. 64 alphanumerics is ~380 bits of entropy — still way more than enough.

**Md3 — Story 5.7: hello Lambda packaging via the same path as phase 3 functions.**
The PRD calls it "a throwaway `src/functions/hello/` Lambda" but doesn't specify packaging. Phase 3 story 3.5 set up `build_package.py` + `Makefile package-all` + `layer_manifest.txt`. The hello function should use the same pipeline — otherwise CI will not know how to build it.

**Fix:** AC #1 adds: "function is registered in `Makefile package-all`; `layer_manifest.txt` is not needed (no shared layer dependencies — handler imports only `json`)."

**Md4 — Story 5.7: AC #4 (WAF SQLi test) sensitivity to managed-rule mode.**
AC #4 asserts the SQLi pattern returns 403. If M3 lands `AWSManagedRulesSQLiRuleSet` in `count` mode initially, this AC fails by construction. The two ACs are coupled: pick the rule-set posture in 5.4, then 5.7 either tests for 403 (enforce mode) or for a CloudWatch metric increment + 200 pass-through (count mode).

**Fix:** if M3 picks `count`, AC #4 changes to "request triggers `AWSManagedRulesSQLiRuleSet` rule's CloudWatch sample (CountedRequests metric > 0) and the request still receives 401 or 200 from origin"; if `none`, leave as-is.

**Md5 — Phase coupling: 5.7 AC #5 "removed before phase 6 begins" leaks scope into phase 6.**
AC #5: "The `/v1/_internal/hello` route is removed from the codebase before phase 6 begins (a tracking note is added to phase 6's notes field)". This is a cross-phase task; if phase 5 ships and phase 6 forgets, the route lingers in prod indefinitely. The "tracking note in phase 6" approach is fragile.

**Fix:** convert the cleanup into a phase 6 story (phase 6 PRD's first story: "remove `_internal/hello` route + Lambda before any domain Lambda lands"). Phase 5's AC #5 becomes "leaves a clear comment in `dev/main.tf` adjacent to the hello module call: `// REMOVE IN PHASE 6 STORY 6.0`". Or accept the fragility and keep the note in phase 6.

---

### MINOR

**Mn1 — Phase 4 hotfix work not reflected in `context.md`.**
PRs #54–#57 (terraform fmt + Cognito TOTP enable + tflint cleanup + db_migrator retry widening) merged into `development` but `context.md` "Recent changes" stops at 2026-06-01 and "Current phase" still says phase 4. Should be updated as part of the phase-4 → phase-5 handoff. Doesn't block phase 5 dispatch but creates ongoing drift.

**Mn2 — `out.json` untracked at repo root.**
Leftover scratch file from prior tooling. Either `.gitignore` it or delete before phase 5 starts to keep `git status` clean during dispatch.

**Mn3 — PRD `last_updated:` stale.**
PRD still reads `last_updated: 2026-05-21`. Per the bookkeeping convention used in phase 4 PRD (`last_updated: 2026-05-29  # story 4.6 done`), this gets bumped on every story landing. The first subagent dispatched in phase 5 will bump it.

**Mn4 — Throttling defaults (50/100 dev, 500/1000 prod) have no rationale.**
Not a blocker, but no explanation of where the numbers come from. Cognito issues ~1 request per sign-in per device; profile completion is a one-shot; the chat path is via AppSync not HTTP API. 50 rps burst dev is wildly generous. Keep as-is — pre-launch — but note this is "headroom, not a measured ceiling".

**Mn5 — No CORS AC.**
React Native HTTP clients don't preflight, so CORS isn't required for the v1 mobile app. If Expo Web ever lands, CORS becomes required. Out of scope for phase 5 — flag as future work in story 5.1 notes block.

---

### Summary

- **2 BLOCKERS** (B1 mechanism for edge-secret enforcement, B2 ACM/Route 53 cycle + missing us_east_1 provider alias) — must be resolved before any story dispatches.
- **6 MAJORS** — most are AC-pinning edits; M5 is a drift correction; M6 is a count-idiom pin.
- **5 MEDIUMS** — quality-of-life and coupling tightening; not blockers but worth tightening before dispatch.
- **5 MINORS** — bookkeeping cleanup.

The PRD as-it-stands is dispatchable IF B1 and B2 are resolved first. M-tier findings will save the subagents a round of clarifying questions; the medium tier mostly serve future-debuggability (access logs, secret charset).

---

## 2026-06-05 20:15 brainstorm (re-run)

PRD revised in commit `da0d0d8` after the 18:30 audit. This re-run verifies the prior findings are addressed and surfaces anything new that the revisions introduced. Format follows the same severity buckets.

### Prior findings — verification pass

- **B1 (edge-secret enforcement mechanism)** — RESOLVED. Story 5.6 ships a shared `require_edge_secret(event)` helper in the observability layer; story 5.1 keeps the built-in JWT authorizer; story 5.7 wires the helper as the first line of the hello handler. The 403 vs 401 split (401 from JWT authorizer, 403 from require_edge_secret) is the clean separation §13a layer 3 expects.
- **B2a (ACM/Route 53 cycle)** — RESOLVED. Story 5.2 self-contains cert + validation DNS records + `aws_acm_certificate_validation`. Story 5.5 now owns only the public-facing A-alias and `depends_on: [5.3]`. No cycle.
- **B2b (us_east_1 alias)** — RESOLVED. New story 5.0 declares the alias in dev and prod env main.tf; stories 5.2 and 5.4 reference it via standard `providers = { aws.us_east_1 = aws.us_east_1 }` passthrough.
- **M1 (audience list)**, **M2a (forwarded_values)**, **M2b (dev cert branch)**, **M3 (WAF count posture)**, **M4 (.env.test plumbing)**, **M5 (4.6 helper reference)**, **M6 (count idiom)** — ALL RESOLVED in the revised AC. Verified line by line.
- **Md1 (access logs)**, **Md2 (random_password special chars)**, **Md3 (Makefile entry)**, **Md4 (SQLi 403 AC removed)**, **Md5 (phase-6 story 6.0)** — ALL RESOLVED. Phase-6 PRD now has story 6.0; the original AC #5 of 5.7 was replaced with a phase-6 ownership note.
- **Mn1 (context.md)**, **Mn2 (out.json)**, **Mn3 (PRD last_updated)**, **Mn4 (throttling)**, **Mn5 (CORS note)** — ALL RESOLVED.

### NEW FINDINGS (introduced by the revisions)

**Nb1 — Story 5.6: `EdgeSecretRequired` exception translation pattern is not pinned.**
AC says the helper "raises a `EdgeSecretRequired` exception ... whose handler-side translation is a Lambda return of `{statusCode: 403, body: ...}`." Who actually performs the translation? Three possibilities the implementer might reach for:

- (a) The hello handler in 5.7 wraps its body in `try / except EdgeSecretRequired: return 403_dict` — every Lambda re-implements the wrap.
- (b) A decorator like `@edge_secret_required` that wraps the handler and converts the exception — cleanest.
- (c) AWS Lambda Powertools' `APIGatewayHttpResolver` has an `exception_handler` decorator that does this idiomatically; Powertools is already on the observability layer.

Without pinning, phase-6 Lambdas will each pick a different pattern. Recommendation: change the helper to expose **both** the raising form (`require_edge_secret(event)`) and a **decorator form** (`@require_edge_secret_decorator` or `@with_edge_secret` that wraps a handler). The hello Lambda in 5.7 uses the decorator. Phase-6 PRD adopts the same.

**Severity:** MEDIUM — the helper still works without this, but inconsistent error handling across the fleet is a hardening-phase smell.

**Nb2 — Story 5.6: `.env.test` is written but not added to .gitignore by any AC.**
AC says the `local_file` resource writes `infrastructure/src/tests/integration/.env.test` and "the file is git-ignored". But no AC says "add the path to .gitignore." The first `terraform apply` will create the file; the next `git status` will show it untracked unless the gitignore line is in place. The implementer should add it — but the AC doesn't compel it.

**Severity:** MINOR — quality-of-life; AC #4 should include "add the path to `.gitignore` in the same commit that introduces the local_file resource."

**Nb3 — Story 5.6: `local_file` resource requires the `hashicorp/local` provider, which is not yet declared.**
Phase-3 backend.tf added `hashicorp/null` (for null_resource) but not `hashicorp/local`. Story 5.6 introduces the first `local_file` usage — the env `required_providers` block must be extended to include `local = { source = "hashicorp/local", version = "~> 2.5" }`.

**Severity:** MINOR — `terraform init` will fetch the provider once declared; the AC should call out the provider addition explicitly so it isn't missed.

**Nb4 — Story 5.0: AC #3 wording "no plan diff against existing state" overpromises.**
AC #3 says `terraform validate` is clean AND "no resources moved, no plan diff against existing state". `terraform validate` doesn't consult state; only `terraform plan` does. The intent is right — the alias is a no-op until referenced — but the assertion mixes validate with plan. Re-word as "`terraform validate` is clean; `terraform plan` shows zero resource changes" so the verification command actually matches the claim.

**Severity:** MINOR — wording tightness.

**Nb5 — Story 5.7 AC integration test path: `signed_in_user` fixture refactored to `conftest.py`.**
AC says the fixture is extracted into `infrastructure/src/tests/integration/conftest.py` so phase-4 AND phase-5 tests share it. That means **phase-4's `test_cognito_signup.py` test must also be refactored** to consume the new fixture in this story. The AC doesn't explicitly call out that the phase-4 test gets touched — the implementer might add the fixture and only wire phase-5 to it, leaving the phase-4 test with its inline boto3 dance and creating divergence.

**Severity:** MEDIUM — either: (a) AC explicitly says "phase-4 `test_cognito_signup.py` is updated to consume the new `signed_in_user` fixture", or (b) drop the "shared" claim and accept duplication.

### Summary (re-run)

- **0 BLOCKERS** — both original blockers fully resolved.
- **0 MAJORS** — all original majors resolved.
- **2 MEDIUMS** — Nb1 exception translation pattern, Nb5 phase-4 test refactor scope.
- **3 MINORS** — Nb2 .gitignore line, Nb3 local provider declaration, Nb4 AC wording.

PRD is dispatchable. The two MEDIUMS are worth one more revision pass for cleanliness; the MINORS can be absorbed during implementation by an alert subagent. The whole second-pass tier could also be left to the subagents to handle inline — they're small enough that a sensible implementer will spot and fix them.


## 2026-06-05 13:20 brainstorm (third pass — pre-dispatch)

Re-audit of the twice-revised PRD against the current repo state (`infrastructure/environments/{dev,prod}/`, modules already shipped through phase 4). Goal: confirm dispatchability and surface any residual gaps not caught in rounds 1–2.

### BLOCKERS
None.

### MAJORS
None.

### MEDIUMS

**Tb1 — Story 5.6 AC #5: `local_file` resource path resolution.**
The PRD names the artifact path as `infrastructure/src/tests/integration/.env.test`, but a `local_file` resource is evaluated from the working directory of the env's `terraform apply` (i.e., `infrastructure/environments/dev/`). The literal string would write to `infrastructure/environments/dev/infrastructure/src/tests/integration/.env.test` — wrong location.

**How to apply:** the implementer must compute the path via `${path.root}/../../src/tests/integration/.env.test` (or use `pathexpand`/`abspath`). Mention in the dispatch brief so the file lands where the pytest harness can find it. Not a PRD revision — an implementer note.

**Tb2 — Story 5.1 AC #3: access log format requires JSON template string, not a JSON-object literal.**
HTTP API access log fields are populated via `$context.*` template variables; the value of `access_log_settings.format` must be a JSON string literal containing those variables (e.g., `"{\"requestId\":\"$context.requestId\",\"status\":\"$context.status\",...}"`). The PRD says "JSON capturing requestId, status, …" which the alert backend dev will get right, but worth pinning in the dispatch brief so the access log is parseable by CloudWatch Logs Insights from day one.

**How to apply:** brief story 5.1 with the exact field list and the reminder that `format` is a CloudFormation/HTTP-API template string, not a Terraform `jsonencode(...)`. Not a PRD revision.

### MINORS

**Tb3 — Story 5.3 AC #3: `random_password` attribute name.**
The PRD spells the booleans `upper=true, lower=true, number=true`. The `hashicorp/random` provider has used `numeric` (not `number`) since 3.4; `number` still works but emits a deprecation warning. Worth using `numeric` for green plan output.

**How to apply:** implementer detail — the subagent will catch this from the deprecation warning. No PRD revision needed.

**Tb4 — Story 5.6 AC #1: `os.environ["EDGE_SECRET"]` vs `.get`.**
AC explicitly says the helper raises `EdgeSecretRequired` when the env var is unset. The implementer must use `os.environ.get("EDGE_SECRET")` + explicit raise (not subscript, which would raise `KeyError`). PRD wording is unambiguous on the desired behavior; this is a "don't subscript" reminder for the implementer.

**How to apply:** implementer detail. No PRD revision.

**Tb5 — Story 5.7 AC (b)/(c) status codes: 401 vs 403.**
HTTP API's built-in JWT authorizer returns **401** for missing/malformed Authorization header and **403** for valid-format-but-invalid-signature. PRD (b) asserts 401 for "no Authorization header" — correct. (c) asserts 403 for "missing edge secret via the in-Lambda check" — correct (the `@with_edge_secret` decorator returns 403). The codes are right; flag is just to remind the implementer not to write `== 200` and accept whatever else comes back.

**How to apply:** implementer detail. No PRD revision.

### Summary (third pass)

- **0 BLOCKERS** — dispatchable.
- **0 MAJORS** — dispatchable.
- **2 MEDIUMS** — both are dispatch-brief notes, not PRD revisions. Adding them to the briefs for 5.6 (path resolution) and 5.1 (access log format) before dispatch.
- **3 MINORS** — implementer-catchable. No action required pre-dispatch.

PRD is **dispatchable as-is**. The two MEDIUMS will be folded into the dispatch briefs for stories 5.1 and 5.6, not into the PRD itself.
