phase: 5
title: API edge (HTTP API + CloudFront + WAF)
last_updated: 2026-06-05  # brainstorm-driven revision (B1, B2, M1-M6, Md1-Md5, Mn1-Mn5 + re-run Nb1-Nb5 incorporated)

context_summary: |
  Builds the inbound HTTPS surface per §4.2 of architecture.md: HTTP API Gateway behind CloudFront with WAF attached at the edge, a Cognito JWT authorizer wired to the User Pool from phase 4, and an origin-secret header that prevents bypassing CloudFront. No domain Lambdas attach yet — that begins in phase 6. A stub "hello" Lambda is wired only as a smoke endpoint to validate that JWT enforcement, WAF rules, and the origin-secret check all function end-to-end through the edge stack.

  The custom domain (ACM cert + Route 53) is a **prod-only feature**: dev runs with `var.domain_name = ""` and reaches the edge via the auto-generated `d*.cloudfront.net` hostname plus the free CloudFront default certificate. The ACM and Route 53 modules ship in this phase but produce zero dev resources (`count = 0` branch). The us_east_1 provider alias is declared in BOTH envs because story 5.4's WAF (`scope = "CLOUDFRONT"`) requires it regardless of whether a custom domain exists.

  Decisions carried from the phase-5 brainstorm (2026-06-05, two rounds): see phasebrainstorms/phase-5-api-edge-brainstorm.md for the full audit trail. Key resolutions baked into the AC below — B1 edge-secret check stays in-Lambda via a shared `require_edge_secret(event)` helper AND a `@with_edge_secret` decorator in the observability layer (NOT a Lambda authorizer — HTTP API allows only one authorizer per route and the built-in JWT bouncer is kept); the decorator centralizes exception → 403 translation so every phase-6 Lambda gets identical error shape (re-run finding Nb1); B2a ACM cert + its own validation DNS records + `aws_acm_certificate_validation` all live in story 5.2 to avoid the cycle that the original split created; B2b us_east_1 provider alias is added as a new story 5.0 ahead of any module that needs it; M1 JWT authorizer `audience` is a compact list of both phase-4 app clients so the dev-only integration-test client can mint valid tokens; M2 CloudFront uses AWS-managed `Managed-CachingDisabled` + `Managed-AllViewer` policies (no deprecated `forwarded_values`) and the dev branch uses `cloudfront_default_certificate = true`; M3 WAF Common + SQLi managed rules ship in `count` mode (flip to `none` deferred to phase 11 hardening after tuning), KnownBadInputs + rate-based ship in `none`; M5 phase-4 helper reference corrected to point at the boto3 dance in `infrastructure/src/tests/integration/test_cognito_signup.py` (phase 4 story 4.6) — and per re-run Nb5, story 5.7 explicitly refactors that phase-4 test to consume the new `signed_in_user` fixture so the dance is not duplicated; Md5 the `/v1/_internal/hello` cleanup is owned by a new phase-6 story 6.0, not a tracking note here. Re-run Nb2: `.env.test` is added to `.gitignore` in story 5.6. Re-run Nb3: `hashicorp/local` Terraform provider is declared in env backend.tf as part of story 5.6 (first usage in the project). Re-run Nb4: story 5.0's verification AC separates `terraform validate` from `terraform plan`.

stories:
  - id: 5.0
    title: us_east_1 provider alias in dev and prod environments
    agent: backenddeveloper
    done: false
    tracking_issue: 58
    depends_on: []
    acceptance_criteria:
      - infrastructure/environments/dev/main.tf adds a second `provider "aws"` block with `alias = "us_east_1"` and `region = "us-east-1"`, copying the same `default_tags` block as the primary provider so resources created via the alias get the same Project/Environment/ManagedBy/Owner tags
      - infrastructure/environments/prod/main.tf adds the same alias block, again mirroring the primary provider's default_tags
      - `terraform validate` is clean on BOTH environments after the change; `terraform plan` against the existing dev state shows zero resource changes (the alias is a no-op until a resource references it in stories 5.2 and 5.4)
      - Each subsequent module call that needs the alias (stories 5.2 and 5.4) will receive it via the standard `providers = { aws.us_east_1 = aws.us_east_1 }` block in the env-level `module` invocation; module-side `required_providers` block declares the `configuration_aliases` so the module accepts the alias cleanly
    notes: "Brainstorm B2b. CloudFront-scoped WAF and CloudFront viewer certificates must live in us-east-1 regardless of the env's primary region; the alias is the canonical Terraform pattern. No real AWS resources land in this story — just the provider declarations. The alias is unused until 5.2 references it; story 5.4 also references it. Brainstorm Nb4: AC #3 separates `terraform validate` (syntax) from `terraform plan` (state diff) — both must pass, but they verify different things."

  - id: 5.1
    title: HTTP API Gateway Terraform module
    agent: backenddeveloper
    done: false
    tracking_issue: 59
    depends_on: []
    acceptance_criteria:
      - infrastructure/modules/api_gateway/main.tf creates aws_apigatewayv2_api with protocol_type="HTTP" and a default stage with throttling burst_limit and rate_limit configurable via variables (defaults `dev: burst=10, rate=25`; `prod: burst=500, rate=1000`)
      - aws_apigatewayv2_authorizer of type JWT references the Cognito User Pool issuer URL from phase 4 via `module.cognito.user_pool_endpoint`; `audience` is a list built via `compact([module.cognito.app_client_id, module.cognito.integration_test_app_client_id])` so dev gets both client ids and prod gets only the production client (the integration-test client output is `""` in prod, which `compact()` drops)
      - Default stage configures access_log_settings.destination_arn pointing at an aws_cloudwatch_log_group with retention_in_days=7 (matches the project-wide log retention convention from architecture.md §10.6); access log format is JSON capturing requestId, status, routeKey, integrationLatency, authLatency, sourceIp, userAgent
      - Module outputs api_id, api_arn, execute_api_endpoint, authorizer_id, default_stage_arn, access_log_group_name
      - CORS is intentionally NOT configured — the v1 mobile React Native client uses native HTTP libraries and does not send CORS preflight; a notes block in the module README states this and flags CORS as future work if Expo Web ever ships (phase 11 hardening or later)
    notes: "Brainstorm M1: `audience` MUST be a list using `compact(...)` over both app client outputs; using only the production client breaks phase-4.6-style integration tests that mint tokens via the dev-only ADMIN_USER_PASSWORD_AUTH client. Brainstorm Md1: access logs are required from day one — they're cheap and the only way to debug 5.6/5.7's 403/401/200 assertions when they regress. Brainstorm Mn4: throttling defaults bumped down in dev (10/25) to reflect pre-launch reality; prod stays at 500/1000. Brainstorm Mn5: CORS is deferred."

  - id: 5.2
    title: ACM certificate for custom domain in us-east-1 (self-contained: cert + DNS validation records + validation wait)
    agent: backenddeveloper
    done: false
    tracking_issue: 60
    depends_on: [5.0]
    acceptance_criteria:
      - infrastructure/modules/acm/main.tf accepts variables `domain_name` (string, default `""`), `hosted_zone_id` (string, default `""`), and `subject_alternative_names` (list(string), default `[]`)
      - When `var.domain_name == ""`, the module produces ZERO resources (count branch on every resource). This is the dev path; the apply runs cleanly with no AWS calls
      - When `var.domain_name != ""`, the module creates: (1) `aws_acm_certificate` in us-east-1 (`provider = aws.us_east_1`) with `validation_method = "DNS"`; (2) one `aws_route53_record` per validation domain in `var.hosted_zone_id` populated from `aws_acm_certificate.this.domain_validation_options`; (3) one `aws_acm_certificate_validation` that waits on the records — bundling all three in one module eliminates the cross-story cycle the original PRD would have caused
      - Module declares `required_providers.aws.configuration_aliases = [aws.us_east_1]` so callers can pass the alias; the env-level call passes `providers = { aws.us_east_1 = aws.us_east_1 }`
      - Outputs: `certificate_arn` (string, empty when no cert) and `certificate_validated` (a marker output that downstream modules can use as a `depends_on` proxy)
      - dev/dev.tfvars sets `domain_name = ""` and `hosted_zone_id = ""`; prod/prod.tfvars leaves the variables to module defaults until the prod cutover documented in docs/PROD_CUTOVER.md
    notes: "Brainstorm B2a: cert + validation DNS records + validation wait are bundled here to avoid the chicken-and-egg the original PRD created (5.2 needs records from 5.5, 5.5 needs cert from 5.2). Brainstorm M6 also covered: empty `domain_name` short-circuits the entire module. Dev path: zero resources. Prod path: full cert + validation. Phase-5 ships dev only; prod stays in plan-only mode per docs/PROD_CUTOVER.md."

  - id: 5.3
    title: CloudFront distribution with origin secret header
    agent: backenddeveloper
    done: false
    tracking_issue: 61
    depends_on: [5.1, 5.2]
    acceptance_criteria:
      - infrastructure/modules/cloudfront/main.tf creates `aws_cloudfront_distribution` with the HTTP API `execute_api_endpoint` as the origin (origin_id="api-gateway"); `enabled = true`, `is_ipv6_enabled = true`
      - `default_cache_behavior` uses the AWS-managed cache policy `Managed-CachingDisabled` (`data.aws_cloudfront_cache_policy.managed_caching_disabled.id`) and the AWS-managed origin request policy `Managed-AllViewer` (`data.aws_cloudfront_origin_request_policy.managed_all_viewer.id`); `viewer_protocol_policy = "redirect-to-https"`; allowed_methods covers the full set (`GET, HEAD, OPTIONS, PUT, POST, PATCH, DELETE`); cached_methods is `["GET", "HEAD"]` — and because the cache policy disables caching, this is functionally a no-cache passthrough. No `forwarded_values` block anywhere (deprecated in provider 6.x)
      - A `custom_header` block on the origin injects `x-knotify-edge-secret` with a value sourced from a `random_password` resource (length=64, `special = false`, `upper = true`, `lower = true`, `number = true`) so the value is restricted to `[A-Za-z0-9]` and is always valid as an HTTP header value; the secret rotates only on explicit `terraform taint` of the random_password
      - `viewer_certificate` branches on `var.domain_name`: when empty, `cloudfront_default_certificate = true` and `aliases = []`; when non-empty, `acm_certificate_arn = var.acm_certificate_arn`, `ssl_support_method = "sni-only"`, `minimum_protocol_version = "TLSv1.2_2021"`, and `aliases = [var.domain_name]`
      - `price_class = "PriceClass_100"` (NA + EU only) to control cost; can be widened in phase 11 if user geography demands it
      - `restrictions { geo_restriction { restriction_type = "none" } }` for now (geo-blocking is a phase-11 concern)
      - Module outputs `distribution_id`, `distribution_domain_name` (the d*.cloudfront.net hostname), `distribution_arn`, and `edge_secret` (sensitive — the random_password.result)
    notes: "Brainstorm M2a: `forwarded_values` is deprecated in provider 6.x; pinned AWS-managed cache + origin-request policies as the modern path. Brainstorm M2b: dev branch uses `cloudfront_default_certificate = true`; this is the dev primary path because var.domain_name is empty. Brainstorm Md2: `random_password` restricted to alphanumerics via `special = false` to guarantee a valid HTTP header value (the default special chars include `:` which is the header delimiter). The edge_secret output is consumed by stories 5.6 (in-Lambda check) and 5.7 (smoke test injects it directly when calling the raw HTTP API to verify the secret-required path)."

  - id: 5.4
    title: WAF web ACL attached to CloudFront
    agent: backenddeveloper
    done: false
    tracking_issue: 62
    depends_on: [5.0, 5.3]
    acceptance_criteria:
      - infrastructure/modules/waf/main.tf creates `aws_wafv2_web_acl` with `scope = "CLOUDFRONT"` using `provider = aws.us_east_1` (alias declared in story 5.0); module declares `required_providers.aws.configuration_aliases = [aws.us_east_1]`
      - Managed rule groups, with explicit override_action posture: `AWSManagedRulesCommonRuleSet` (priority 1, `override_action = "count"` — known to false-positive on JSON payloads, ship in monitor mode and flip to none in phase 11 after tuning); `AWSManagedRulesKnownBadInputsRuleSet` (priority 2, `override_action = "none"` — low false-positive rate, enforce from day one); `AWSManagedRulesSQLiRuleSet` (priority 3, `override_action = "count"` — observe before enforcing); rate-based rule (priority 10, `action = "block"`, limit=2000 requests/5min per IP)
      - Each rule has `visibility_config` with `cloudwatch_metrics_enabled = true` and `sampled_requests_enabled = true` so CloudWatch reports counted-but-not-blocked matches for the rules in count mode
      - `aws_wafv2_web_acl_association` attaches the ACL to the CloudFront distribution from story 5.3 (`resource_arn = module.cloudfront.distribution_arn`)
      - Module outputs `web_acl_id`, `web_acl_arn`
    notes: "Brainstorm M3: Common + SQLi ship in `count` mode because they false-positive on legitimate JSON payloads (signup, profile patches in phase 6, photo uploads in phase 12). KnownBadInputs is low-FP — safe to enforce. Rate-based block at 2000/5min is a generous ceiling for a pre-launch app. Phase 11 hardening will review CloudWatch sample data and flip Common + SQLi from `count` to `none`. Brainstorm B2b cross-reference: the `aws.us_east_1` alias must already exist via story 5.0 before this story dispatches."

  - id: 5.5
    title: Route 53 public-facing A-alias record for custom domain (prod only)
    agent: backenddeveloper
    done: false
    tracking_issue: 63
    depends_on: [5.3]
    acceptance_criteria:
      - infrastructure/modules/route53/main.tf accepts variables `domain_name` (string, default `""`), `hosted_zone_id` (string, default `""`), `cloudfront_distribution_domain_name` (string, required), `cloudfront_hosted_zone_id` (string, required — always `Z2FDTNDATAQYW2`, the CloudFront global zone)
      - Every resource uses `count = (var.domain_name == "" || var.hosted_zone_id == "") ? 0 : 1` so the module produces ZERO resources when either input is empty; this is the dev path
      - When both are set, `aws_route53_record` creates an A-alias record pointing `var.domain_name` at `var.cloudfront_distribution_domain_name` with `evaluate_target_health = false`
      - Module is invoked from prod/main.tf with the real domain name; from dev/main.tf with empty strings (so it no-ops cleanly in dev)
      - Module outputs `record_fqdn` (string, empty when no record)
    notes: "Brainstorm B2a: this story now owns ONLY the public-facing alias record; the cert-validation records moved to story 5.2 to break the cycle. Brainstorm M6: count idiom is `var.domain_name == \"\" || var.hosted_zone_id == \"\"` to handle the half-configured case cleanly. Dev path: zero resources, no Route 53 zone needed."

  - id: 5.6
    title: Shared `require_edge_secret` helper + `@with_edge_secret` decorator in observability layer + integration test plumbing
    agent: backenddeveloper
    done: false
    tracking_issue: 64
    depends_on: [5.3]
    acceptance_criteria:
      - infrastructure/src/layers/observability/knotify_obs/__init__.py adds a `require_edge_secret(event)` function. The function reads `event.get("headers", {})`, lowercases the keys (HTTP API event headers can arrive in either case), pulls the `x-knotify-edge-secret` header, compares it constant-time via `hmac.compare_digest` to the value of `os.environ["EDGE_SECRET"]`. On mismatch or missing header, raises an `EdgeSecretRequired` exception (defined alongside). When `EDGE_SECRET` env var is unset, raises `EdgeSecretRequired` defensively (treated as a Lambda config error).
      - The SAME module adds a `@with_edge_secret` decorator. The decorator wraps a Lambda handler so that any raised `EdgeSecretRequired` is translated to a Lambda return of `{"statusCode": 403, "body": json.dumps({"error": "forbidden"})}` (with a `Content-Type: application/json` header). All phase-5.7 and phase-6 Lambdas that gate on the edge secret MUST use the decorator (not raw try/except) so the translation is consistent across the fleet.
      - The module exports `require_edge_secret`, `EdgeSecretRequired`, and `with_edge_secret`; the layer's README documents the canonical usage: `@with_edge_secret` on the handler function. Inline use of `require_edge_secret(event)` is permitted only for non-handler call sites (e.g., utility scripts) and is flagged as such in the README.
      - Unit tests in infrastructure/src/layers/observability/tests/test_edge_secret.py cover both APIs: `require_edge_secret` — (a) header missing → raises, (b) header present but wrong → raises, (c) header present and correct (case-insensitive lookup) → returns None, (d) EDGE_SECRET env var unset → raises; `@with_edge_secret` — (e) handler runs normally when secret matches and returns whatever the handler returned, (f) handler is bypassed and a 403 response dict is returned when the helper raises, (g) other exceptions raised inside the handler are NOT caught by the decorator (must propagate)
      - Terraform outputs from stories 5.1 and 5.3 (`execute_api_endpoint`, `distribution_domain_name`, `edge_secret`) are written to `infrastructure/src/tests/integration/.env.test` via a `local_file` resource so the phase-5.7 integration tests can read them at runtime
      - `infrastructure/src/tests/integration/.env.test` is added to `.gitignore` in the same commit that introduces the `local_file` resource; verify with `git check-ignore -v infrastructure/src/tests/integration/.env.test`
      - The `local_file` resource requires the `hashicorp/local` Terraform provider. dev/backend.tf AND prod/backend.tf are extended with `local = { source = "hashicorp/local", version = "~> 2.5" }` under `required_providers`; `terraform init` is re-run in both environments to fetch the provider
      - The Lambda env injection pattern is documented in the module README so story 5.7 (and every phase-6 Lambda) knows to set `EDGE_SECRET = module.cloudfront.edge_secret` in its Terraform module call AND apply the `@with_edge_secret` decorator to the handler
    notes: "Brainstorm B1 resolution: option (b) — keep story 5.1's built-in JWT authorizer (HTTP API allows only ONE authorizer per route, and the free JWT bouncer does native JWKS validation), enforce the edge-secret check INSIDE every Lambda via this shared helper. Matches §13a layer 3's per-route pattern. Brainstorm M4: the `.env.test` file is the canonical way to thread Terraform outputs into the integration test harness; mirrors the phase-4.6 pattern. The helper lives in the observability layer so it's already on the dependency path of every Lambda; no new layer needed. Brainstorm Nb1: `@with_edge_secret` decorator added so the exception → 403 translation is centralized — every phase-6 Lambda gets identical error shape with no per-Lambda try/except boilerplate. Brainstorm Nb2: `.env.test` added to `.gitignore` in the same commit. Brainstorm Nb3: `hashicorp/local` provider declared in env backend.tf — this is the first usage of that provider in the project."

  - id: 5.7
    title: Stub hello endpoint and end-to-end edge smoke test
    agent: backenddeveloper
    done: false
    tracking_issue: 65
    depends_on: [5.1, 5.3, 5.4, 5.6]
    acceptance_criteria:
      - infrastructure/src/functions/hello/handler.py is a minimal Lambda: imports `with_edge_secret` from the observability layer and applies it as a decorator on the handler function, then returns `{"statusCode": 200, "body": json.dumps({"ok": True, "user_id": event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]})}`. The decorator handles all 403 translation — the handler body assumes the edge secret is valid by the time it executes.
      - The hello function is registered in `Makefile package-all` so CI builds the zip; build_package.py is invoked the same way as phase 3's `_smoke` function
      - Terraform wires `module.hello` in dev/main.tf using the lambda module, references observability layer + JWT authorizer + the HTTP API route `GET /v1/_internal/hello` via `aws_apigatewayv2_route` with `authorization_type = "JWT"` and `authorizer_id = module.api_gateway.authorizer_id`; Lambda env includes `EDGE_SECRET = module.cloudfront.edge_secret`
      - The boto3 sign-up + admin_confirm + admin_initiate_auth dance currently inline in `infrastructure/src/tests/integration/test_cognito_signup.py` (phase 4 story 4.6) is extracted into a reusable `signed_in_user` pytest fixture in `infrastructure/src/tests/integration/conftest.py`. The fixture is consumed by BOTH the existing phase-4 `test_cognito_signup.py` (refactored in THIS story so the dance is not duplicated) AND the new phase-5 `test_edge_smoke.py`. The phase-4 test must continue to pass with no behavioral change after the refactor.
      - Integration test infrastructure/src/tests/integration/test_edge_smoke.py covers four assertions, loading endpoint values from `.env.test` and using the `signed_in_user` fixture: (a) GET `<distribution_domain_name>/v1/_internal/hello` with the Authorization header returns 200 with the user's sub in the body; (b) the same request to `<distribution_domain_name>/v1/_internal/hello` WITHOUT the Authorization header returns 401 (the built-in JWT authorizer rejects); (c) GET `<execute_api_endpoint>/v1/_internal/hello` (bypassing CloudFront) WITH a valid Authorization header but no `x-knotify-edge-secret` header returns 403 (the `@with_edge_secret` decorator rejects); (d) the same `<execute_api_endpoint>` request WITH the correct `x-knotify-edge-secret` header value (read from `.env.test`) returns 200 — confirms the helper accepts the right secret, not just rejects all direct-API traffic
      - Test teardown deletes the Cognito user via `admin_delete_user` and the Aurora users row via the master credential — mirrors phase 4.6 teardown
      - Phase-6 ownership of the cleanup: a phase-6 story 6.0 owns removing this Lambda + route + integration test before any domain Lambda lands; NO tracking note is added to phase 6's notes field (the cleanup is a real PRD story, not a note)
    notes: "Brainstorm M5 correction: the Cognito sign-in helper is in phase 4 story 4.6 (`test_cognito_signup.py`), NOT 4.5. The fixture extraction is the clean path. Brainstorm Md3: registered in Makefile package-all so CI builds the zip. Brainstorm Md4 removed the original AC #4 (\"WAF rejects SQLi with 403\") because Common + SQLi ship in `count` mode (M3) — the SQLi rule would not block at this stage; the count-mode CloudWatch sample is checked manually in phase 11 hardening, not in this story's automated tests. Brainstorm Md5: the `/v1/_internal/hello` cleanup is a phase-6 story 6.0 (added to phase-6 PRD), not a tracking note. Brainstorm Nb1: hello handler uses the `@with_edge_secret` decorator from story 5.6, not raw try/except — keeps phase-6 Lambdas on the same pattern. Brainstorm Nb5: the phase-4 `test_cognito_signup.py` is explicitly refactored in THIS story to consume the new `signed_in_user` fixture so the boto3 dance is not duplicated; phase-4's test must continue to pass after the refactor (no behavioral change). Additional AC (d): a positive-case test against the raw execute-api endpoint WITH the correct secret confirms the helper accepts the right secret, not just rejects all direct-API traffic."
