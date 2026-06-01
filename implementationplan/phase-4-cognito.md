phase: 4
title: Cognito
last_updated: 2026-05-29  # story 4.6 done

context_summary: |
  Provisions the Cognito User Pool and app clients per §4.1 of architecture.md, wires the post-confirmation trigger Lambda built in phase 3 so signup flows are complete end-to-end on the day this phase ships, and adds the PreTokenGeneration V2 trigger Lambda that embeds the `custom:profile_complete` claim in BOTH the issued ID token AND access token (layer 2 of the §13a enforcement model). Email is the only sign-in alias — `preferred_username` is NOT used for auth, NOT a signup attribute, and is set later via the profile-completion endpoint (phase 6). MFA enforcement is intentionally deferred to the pre-launch hardening phase (§13 #1 resolution in v1.6); Cognito Advanced Security Mode is set to AUDIT (the minimum required by V2 PreTokenGeneration), with the ENFORCED upgrade deferred to phase 11. Subsequent phases consume the Cognito User Pool ID for the HTTP API Cognito JWT authorizer (phase 5) and the AppSync Cognito auth mode (phase 8).

  Decisions carried from the phase-4 brainstorm (2026-05-28/29): see phasebrainstorms/phase-4-cognito-brainstorm.md for the full audit trail. Key resolutions baked into the AC below — B1 PreTokenGeneration writes claim to BOTH id and access tokens (HTTP API authorizer reads access token; mobile app reads ID token); B2 V2 trigger pinned via `lambda_version = "V2_0"` AND `advanced_security_mode` default bumped from OFF to AUDIT; M1 DB env var standardized to DB_SECRET_NAME with friendly name (matches phase 3 cognito_post_confirmation pattern); M2 schema attrs given_name/family_name/gender/birthdate kept as forward-compat for social IDP federation (User Pool schema is immutable post-creation; declaring now avoids future rebuild); M3 trigger wiring pinned to the `lambda_config` block on aws_cognito_user_pool (no standalone resource exists); M4 dev-only second app client `knotify-dev-integration-test` with ADMIN_USER_PASSWORD_AUTH for boto3-driven integration tests (production app client stays SRP-only — React Native uses aws-amplify's JS SRP implementation); Md4 phase-4.6 test teardown deletes the Aurora row to prevent accumulation.

stories:
  - id: 4.1
    title: Cognito User Pool Terraform module
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 47
    acceptance_criteria:
      - File infrastructure/modules/cognito/main.tf creates aws_cognito_user_pool with username_attributes=["email"], auto_verified_attributes=["email"], password_policy minimum_length=12 require_uppercase=true require_lowercase=true require_numbers=true require_symbols=true, mfa_configuration="OPTIONAL", account_recovery_setting with email as the only mechanism (no SMS)
      - schema attributes include given_name (mutable=true, required=false) and family_name (mutable=true, required=false) — both optional, populated when the signup path supplies them; gender (string, mutable=true, required=false) and birthdate (string, mutable=true, required=false) — both optional, declared NOW for forward-compatibility with social-identity providers (Google, Apple) that are out of scope for v1 (architecture.md §4.1) but will land in a later phase. Declaring them now avoids a User Pool rebuild later, since Cognito schema attributes are immutable post-creation. In v1, email-only signup leaves all four NULL on the resulting Cognito user; the cognito_post_confirmation Lambda (phase 3 story 3.6) already handles the NULL branch
      - preferred_username is NOT in the schema, NOT a sign-in alias, and NOT auto-verified — see architecture.md §13 item 27. The user-supplied display handle lands in users.username at profile completion (phase 6), with no Cognito coupling
      - user_pool_add_ons.advanced_security_mode is wired to the variable from story 4.5 (default "AUDIT" — required by V2 PreTokenGeneration in story 4.4 per AWS docs; bumped from the original "OFF" plan during phase-4 brainstorm resolution of B2)
      - token validity is configured with id_token=1h, access_token=1h, refresh_token=30d
      - Module outputs user_pool_id, user_pool_arn, and user_pool_endpoint (the issuer URL `https://cognito-idp.<region>.amazonaws.com/<user_pool_id>`) so phase 5's JWT authorizer can reference the issuer without reconstructing it
    notes: "Cognito User Pool schema attributes are IMMUTABLE after creation. Get this right the first time — changing any attribute property (name, type, mutable, required, length) later means rebuilding the pool, which forces every existing user to re-sign-up. Brainstorm M2/Md5 resolution: keep all four schema attrs (given_name, family_name, gender, birthdate) as forward-compat for social IDP federation. Brainstorm Mn3: user_pool_endpoint output added so phase 5 can wire the JWT authorizer issuer ergonomically."

  - id: 4.2
    title: Cognito app clients (production SRP + dev-only integration-test)
    agent: backenddeveloper
    done: true
    depends_on: [4.1]
    tracking_issue: 48
    acceptance_criteria:
      - Production app client `knotify-${var.environment}-app` is created with generate_secret=false (public client), explicit_auth_flows includes ALLOW_USER_SRP_AUTH and ALLOW_REFRESH_TOKEN_AUTH (no ADMIN/USER_PASSWORD flows — SRP only), prevent_user_existence_errors="ENABLED". This is the client the React Native app uses via aws-amplify, which implements the SRP handshake natively in JS
      - refresh_token_validity 30, access_token_validity 60, id_token_validity 60 with token_validity_units configured for minutes/days as appropriate
      - A SECOND app client `knotify-${var.environment}-integration-test` is created CONDITIONALLY — only when var.environment == "dev" — with generate_secret=false, explicit_auth_flows includes ALLOW_ADMIN_USER_PASSWORD_AUTH and ALLOW_REFRESH_TOKEN_AUTH (no SRP — boto3 cannot drive SRP). This client is consumed by the phase 4.6 integration test only; production never sees it. Brainstorm M4 resolution
      - Conditional creation uses count = var.environment == "dev" ? 1 : 0 on the dev-only client resource so terraform plan in prod shows zero integration-test resources
      - Module outputs app_client_id (always populated, references the production client) AND integration_test_app_client_id (a string output — empty string in prod, the dev-only client id in dev). The output is non-sensitive — the id is not a credential
    notes: "Brainstorm M4: production app client stays SRP-only. The React Native app uses aws-amplify's JS SRP implementation; backend never participates in user sign-in. The dev-only second app client is a testing affordance for boto3 (which lacks an SRP implementation) — gated by var.environment so prod's app client surface is unchanged."

  - id: 4.3
    title: Wire post-confirmation trigger to the User Pool
    agent: backenddeveloper
    done: true
    depends_on: [4.1]
    tracking_issue: 49
    acceptance_criteria:
      - The `lambda_config` nested block on aws_cognito_user_pool (story 4.1) references the post_confirmation Lambda ARN from phase 3's knotify-cognito-post-confirmation function via the `post_confirmation` field (V1 single-ARN form). There is no standalone aws_cognito_user_pool_lambda_config resource — wiring lives in the `lambda_config` block on the pool itself. Brainstorm M3 resolution
      - aws_lambda_permission grants cognito-idp.amazonaws.com permission to invoke the Lambda from the User Pool ARN (this resource is intentionally deferred from phase 3 story 3.6 to here — phase 3 builds the function with no permission since the source service is not yet known)
      - After dev apply, `aws cognito-idp describe-user-pool --user-pool-id <id> | jq .UserPool.LambdaConfig.PostConfirmation` returns the post-confirmation Lambda ARN. Once story 4.4 lands, the same describe call's `.UserPool.LambdaConfig.PreTokenGenerationConfig.LambdaArn` returns the PreTokenGeneration Lambda ARN (verifies stories 4.3 and 4.4 wiring together). Brainstorm Mn4
    notes: ""

  - id: 4.4
    title: PreTokenGeneration Lambda (profile_complete claim embedding)
    agent: backenddeveloper
    done: true
    depends_on: [4.1]
    tracking_issue: 50
    acceptance_criteria:
      - Source directory src/functions/cognito_pre_token_generation/ contains a Lambda handler that receives a Cognito PreTokenGeneration_Authentication event AND PreTokenGeneration_RefreshTokens event (V2 trigger shape — see lambda_version pin below), reads the `sub` claim from the event's request.userAttributes, queries Aurora for users.profile_complete_verified WHERE user_id = sub, and returns the event with `response.claimsAndScopeOverrideDetails.idTokenGeneration.claimsToAddOrOverride["custom:profile_complete"]` AND `response.claimsAndScopeOverrideDetails.accessTokenGeneration.claimsToAddOrOverride["custom:profile_complete"]` BOTH set to "true"|"false". The claim MUST be written to both tokens — the HTTP API Cognito JWT authorizer (phase 5) reads the access token by default, while the mobile app reads the ID token for client-side routing. Brainstorm B1 resolution
      - The Lambda is deployed via the lambda module (phase 3 story 3.1) with the observability + db layers (phase 3 stories 3.2, 3.3) attached, IAM role cognito_trigger (phase 3 story 3.4 — shared with cognito_post_confirmation; their permission sets are identical), VPC config wired to phase 1 private subnets, environment variable DB_SECRET_NAME set to the friendly secret name pattern `knotify-${var.environment}-app-user-credential` (NOT an ARN — boto3 secretsmanager:GetSecretValue does NOT accept wildcard ARN strings; the friendly name is what's resolvable; see infrastructure/environments/dev/main.tf line 279 for the matching cognito_post_confirmation pattern). The IAM role's existing scoping `arn:...:secret:knotify-${var.environment}-app-user-credential-*` covers this Lambda. Brainstorm M1 resolution
      - If the users row is missing (post-confirmation Lambda was not invoked yet, race condition) the handler returns custom:profile_complete = "false" on both tokens and logs a structured warning — does NOT raise an exception, since failing PreTokenGeneration blocks login entirely. Known operational gap (Brainstorm Md2): if the post-confirmation Lambda silently dropped the row earlier (the missing-email path from phase 3 story 3.6 AC), the user is stuck at "false" forever with no recovery — track and revisit in phase 9 (account deletion / recovery flows) if it ever fires
      - The pool wiring lives in story 4.1's aws_cognito_user_pool `lambda_config` block as `pre_token_generation_config { lambda_version = "V2_0", lambda_arn = module.cognito_pre_token_generation.lambda_arn }`. The V1 `pre_token_generation` field is EXPLICITLY FORBIDDEN — V1 only customizes ID tokens and cannot reach the access token, which silently regresses B1. aws_lambda_permission grants cognito-idp.amazonaws.com invoke rights from the User Pool ARN. Brainstorm B2 resolution
      - Integration test against the dev Aurora cluster simulates two cases: (a) profile_complete_verified=false → claim is "false" on BOTH `response.claimsAndScopeOverrideDetails.idTokenGeneration.claimsToAddOrOverride["custom:profile_complete"]` AND `response.claimsAndScopeOverrideDetails.accessTokenGeneration.claimsToAddOrOverride["custom:profile_complete"]`, (b) profile_complete_verified=true → claim is "true" on both. The test asserts both sub-objects to lock in B1
      - Documents the contract in src/functions/cognito_pre_token_generation/README.md including: (a) one DB hit per token issue (sign-in and refresh) — not per request, (b) cold-start latency budget: 200ms–1s ENI penalty on first sign-in / first refresh per cold container, acceptable for pre-launch volumes; provisioned concurrency to be evaluated in phase 11 hardening if observed in CloudWatch (Brainstorm Md1), (c) V2 trigger shape and the two-token claim contract, (d) the missing-users-row branch and its operational implications (Md2)
    notes: "Layer 2 of the §13a profile-completion enforcement model. Layer 1 is the DB CHECK constraint (already in §5.1 / migration 0002 as of phase 3 brainstorm). Layer 3 is the per-route 403 gate (phase 5). All three must ship for the contract to be airtight, but each layer is independently safe — a failure in any layer is caught by the next. IAM role cognito_trigger is shared with cognito_post_confirmation (Brainstorm Md3) — split if their permission sets ever diverge."

  - id: 4.5
    title: Per-environment wiring with deferred hardening flags
    agent: backenddeveloper
    done: true
    depends_on: [4.1, 4.2, 4.3, 4.4]
    tracking_issue: 51
    acceptance_criteria:
      - Both dev and prod environments instantiate module.cognito
      - Variable advanced_security_mode defaults to "AUDIT" in both environments for v1 (bumped from the original "OFF" plan during phase-4 brainstorm B2 resolution — V2 PreTokenGeneration in story 4.4 requires the User Pool to have advanced_security_mode at least AUDIT per AWS docs; AUDIT is the minimum that enables the V2 trigger without enforcing risk-based blocking, and it incurs Cognito Plus-plan billing). The variable exists and is wired so the hardening phase only flips the value to "ENFORCED"
      - A comment in prod/main.tf marks "Cognito Advanced Security set to AUDIT minimum to enable V2 PreTokenGeneration (story 4.4 / brainstorm B2). ENFORCED upgrade and MFA enforcement deferred to phase 11" with a reference to architecture.md §13 #1
    notes: "Brainstorm B2/Mn2: advanced_security_mode defaults bumped from OFF to AUDIT to satisfy V2 PreTokenGeneration. Cost impact: Cognito advanced security tier (Plus plan) billing kicks in at AUDIT — non-trivial in prod once MAU > 50. Confirm cost is acceptable, or fall back to investigating whether the AWS provider lets OFF + V2 coexist (some sources claim it does for trigger purposes only); empirical apply in dev resolves this. If empirical apply succeeds with OFF, the default may be reverted."

  - id: 4.6
    title: End-to-end signup test
    agent: backenddeveloper
    done: true
    depends_on: [4.1, 4.2, 4.3, 4.4, 4.5]
    tracking_issue: 52
    acceptance_criteria:
      - An integration test (under tests/integration/cognito_signup_test.py or equivalent) drives a real signup against the dev User Pool using boto3.cognito-idp.sign_up with synthetic credentials and a unique per-run test email of the form `knotify-test+<uuid4>@example.com` (uniqueness prevents Cognito's email-uniqueness rule from blocking re-runs and prevents Aurora row accumulation from colliding on PK)
      - The test confirms the user via admin_confirm_sign_up to fire the post-confirmation trigger
      - The test then queries the dev Aurora cluster and asserts a users row exists with user_id matching the Cognito sub. Other columns (first_name, last_name, sex, birthday, username) are NULL by design — the post-confirmation Lambda inserts a minimal bootstrap row and the user completes the rest later via the profile-completion endpoint (phase 6). If Cognito supplied given_name/family_name/gender/birthdate, those fields are populated; if not, they remain NULL. The test must NOT fail because these are NULL — that is the expected bootstrap state
      - A second assertion drives a token issuance via admin_initiate_auth with AuthFlow=ADMIN_USER_PASSWORD_AUTH against the dev-only integration-test app client (story 4.2) — NOT the production SRP app client, since boto3 does not implement the SRP client-side handshake. The test inspects BOTH the returned id_token AND access_token and asserts custom:profile_complete = "false" on both (since the bootstrap row has profile_complete_verified=false). This exercises the PreTokenGeneration Lambda from story 4.4 and locks in the brainstorm B1 two-token contract. Brainstorm M4 + M5 resolution
      - Teardown: the test calls admin_delete_user to clean up the Cognito user AND issues `DELETE FROM users WHERE user_id = '<sub>'` against the dev Aurora cluster using the Aurora master credential (not the app_user RLS-scoped path — the master secret is loaded from Secrets Manager `rds!cluster-<resource_id>` via the same pattern the db_migrator uses). This prevents Aurora row accumulation across repeated test runs without pre-empting phase 9's actual soft-delete contract. Brainstorm Md4 resolution
    notes: "Test uses the dev-only integration-test app client (story 4.2) — the production SRP-only client is untouched. Test runs marked @pytest.mark.integration so they only execute against a live dev environment, not in pytest -m 'not integration'."
