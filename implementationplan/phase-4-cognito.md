phase: 4
title: Cognito
last_updated: 2026-05-24  # phase-3 brainstorm updates

context_summary: |
  Provisions the Cognito User Pool and app client per §4.1 of architecture.md, wires the post-confirmation trigger Lambda built in phase 3 so signup flows are complete end-to-end on the day this phase ships, and adds the PreTokenGeneration trigger Lambda that embeds the `custom:profile_complete` claim in issued JWTs (layer 2 of the §13a enforcement model). Email is the only sign-in alias — `preferred_username` is NOT used for auth, NOT a signup attribute, and is set later via the profile-completion endpoint (phase 6). Advanced Security Features and MFA enforcement are intentionally deferred to the pre-launch hardening phase (§13 #1 resolution in v1.6). Subsequent phases consume the Cognito User Pool ID for the HTTP API Cognito JWT authorizer (phase 5) and the AppSync Cognito auth mode (phase 8).

stories:
  - id: 4.1
    title: Cognito User Pool Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/cognito/main.tf creates aws_cognito_user_pool with username_attributes=["email"], auto_verified_attributes=["email"], password_policy minimum_length=12 require_uppercase=true require_lowercase=true require_numbers=true require_symbols=true, mfa_configuration="OPTIONAL", account_recovery_setting with email as the only mechanism (no SMS)
      - schema attributes include given_name (mutable=true) and family_name (mutable=true) — both optional, populated when the signup path supplies them; gender (string, mutable=true) and birthdate (string, mutable=true) — both optional, populated by social-identity providers when available
      - preferred_username is NOT in the schema, NOT a sign-in alias, and NOT auto-verified — see architecture.md §13 item 27. The user-supplied display handle lands in users.username at profile completion (phase 6), with no Cognito coupling
      - token validity is configured with id_token=1h, access_token=1h, refresh_token=30d
      - Module outputs user_pool_id and user_pool_arn
    notes: ""

  - id: 4.2
    title: Cognito app client for React Native
    agent: backenddeveloper
    done: false
    depends_on: [4.1]
    acceptance_criteria:
      - aws_cognito_user_pool_client created with generate_secret=false (public client), explicit_auth_flows includes ALLOW_USER_SRP_AUTH and ALLOW_REFRESH_TOKEN_AUTH, prevent_user_existence_errors="ENABLED"
      - refresh_token_validity 30, access_token_validity 60, id_token_validity 60 with token_validity_units configured for minutes/days as appropriate
      - Module outputs app_client_id
    notes: ""

  - id: 4.3
    title: Wire post-confirmation trigger to the User Pool
    agent: backenddeveloper
    done: false
    depends_on: [4.1]
    acceptance_criteria:
      - aws_cognito_user_pool resource (or aws_cognito_user_pool_lambda_config separation) references the post_confirmation Lambda ARN from phase 3's knotify-cognito-post-confirmation function
      - aws_lambda_permission grants cognito-idp.amazonaws.com permission to invoke the Lambda from the User Pool ARN (this resource is intentionally deferred from phase 3 story 3.6 to here — phase 3 builds the function with no permission since the source service is not yet known)
      - terraform apply in dev shows the trigger attached when describing the user pool via aws cognito-idp describe-user-pool
    notes: ""

  - id: 4.4
    title: PreTokenGeneration Lambda (profile_complete claim embedding)
    agent: backenddeveloper
    done: false
    depends_on: [4.1]
    acceptance_criteria:
      - Source directory src/functions/cognito_pre_token_generation/ contains a Lambda handler that receives a Cognito PreTokenGeneration_Authentication event (and PreTokenGeneration_RefreshTokens), reads the `sub` claim from the event's request.userAttributes, queries Aurora for users.profile_complete_verified WHERE user_id = sub, and returns the event with response.claimsAndScopeOverrideDetails.idTokenGeneration.claimsToAddOrOverride.custom:profile_complete = "true"|"false"
      - The Lambda is deployed via the lambda module (phase 3 story 3.1) with the observability + db layers (phase 3 stories 3.2, 3.3) attached, IAM role cognito_trigger (phase 3 story 3.4), VPC config wired to phase 1 private subnets, environment variable DB_SECRET_ARN pointing at the Aurora app_user credential secret (knotify-<env>-app-user-credential, written by the phase 3 migrator)
      - If the users row is missing (post-confirmation Lambda was not invoked yet, race condition) the handler returns custom:profile_complete = "false" and logs a structured warning — does NOT raise an exception, since failing PreTokenGeneration blocks login entirely
      - The function's aws_cognito_user_pool resource wires the lambda_config.pre_token_generation_config reference; aws_lambda_permission grants cognito-idp.amazonaws.com invoke rights from the User Pool ARN
      - Integration test against the dev Aurora cluster simulates two cases: (a) profile_complete_verified=false → claim is "false", (b) profile_complete_verified=true → claim is "true". Both assertions inspect the returned event payload
      - Documents the contract in src/functions/cognito_pre_token_generation/README.md including the latency budget (one DB hit per token issue, runs inside warm Lambda container after first invocation)
    notes: "Layer 2 of the §13a profile-completion enforcement model. Layer 1 is the DB CHECK constraint (already in §5.1 / migration 0002 as of phase 3 brainstorm). Layer 3 is the per-route 403 gate (phase 5). All three must ship for the contract to be airtight, but each layer is independently safe — a failure in any layer is caught by the next."

  - id: 4.5
    title: Per-environment wiring with deferred hardening flags
    agent: backenddeveloper
    done: false
    depends_on: [4.1, 4.2, 4.3, 4.4]
    acceptance_criteria:
      - Both dev and prod environments instantiate module.cognito
      - Variable advanced_security_mode defaults to "OFF" in both environments for v1; the variable exists and is wired so the hardening phase only flips the value
      - A comment in prod/main.tf marks "Cognito Advanced Security and MFA enforcement deferred to phase 11" with a reference to architecture.md §13 #1
    notes: ""

  - id: 4.6
    title: End-to-end signup test
    agent: backenddeveloper
    done: false
    depends_on: [4.1, 4.2, 4.3, 4.4, 4.5]
    acceptance_criteria:
      - An integration test (under tests/integration/cognito_signup_test.py or equivalent) drives a real signup against the dev User Pool using boto3.cognito-idp.sign_up with synthetic credentials and a one-time test email
      - The test confirms the user via admin_confirm_sign_up to fire the post-confirmation trigger
      - The test then queries the dev Aurora cluster and asserts a users row exists with user_id matching the Cognito sub. Other columns (first_name, last_name, sex, birthday, username) are NULL by design — the post-confirmation Lambda inserts a minimal bootstrap row and the user completes the rest later via the profile-completion endpoint (phase 6). If Cognito supplied given_name/family_name/gender/birthdate, those fields are populated; if not, they remain NULL. The test must NOT fail because these are NULL — that is the expected bootstrap state
      - A second assertion drives a token issuance via initiate_auth and inspects the returned id_token; the custom:profile_complete claim is "false" (since the bootstrap row has profile_complete_verified=false). This exercises the PreTokenGeneration Lambda from story 4.4
      - The test admin_delete_user cleans up the Cognito user; a manual cleanup of the orphaned Aurora row is documented but soft-delete is not yet implemented (that arrives in phase 9)
    notes: ""
