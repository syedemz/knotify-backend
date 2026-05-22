phase: 4
title: Cognito
last_updated: 2026-05-21

context_summary: |
  Provisions the Cognito User Pool and app client per §4.1 of architecture.md and wires the post-confirmation trigger Lambda built in phase 3 so signup flows are complete end-to-end on the day this phase ships. Advanced Security Features and MFA enforcement are intentionally deferred to the pre-launch hardening phase (§13 #1 resolution in v1.6). Subsequent phases consume the Cognito User Pool ID for the HTTP API Cognito JWT authorizer (phase 5) and the AppSync Cognito auth mode (phase 8).

stories:
  - id: 4.1
    title: Cognito User Pool Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/cognito/main.tf creates aws_cognito_user_pool with username_attributes=["email"], auto_verified_attributes=["email"], password_policy minimum_length=12 require_uppercase=true require_lowercase=true require_numbers=true require_symbols=true, mfa_configuration="OPTIONAL", account_recovery_setting with email as the only mechanism (no SMS)
      - schema attributes include given_name, family_name, gender (string, mutable=false), birthdate (string, mutable=false), preferred_username
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
      - aws_lambda_permission grants cognito-idp.amazonaws.com permission to invoke the Lambda from the User Pool ARN
      - terraform apply in dev shows the trigger attached when describing the user pool via aws cognito-idp describe-user-pool
    notes: ""

  - id: 4.4
    title: Per-environment wiring with deferred hardening flags
    agent: backenddeveloper
    done: false
    depends_on: [4.1, 4.2, 4.3]
    acceptance_criteria:
      - Both dev and prod environments instantiate module.cognito
      - Variable advanced_security_mode defaults to "OFF" in both environments for v1; the variable exists and is wired so the hardening phase only flips the value
      - A comment in prod/main.tf marks "Cognito Advanced Security and MFA enforcement deferred to phase 11" with a reference to architecture.md §13 #1
    notes: ""

  - id: 4.5
    title: End-to-end signup test
    agent: backenddeveloper
    done: false
    depends_on: [4.1, 4.2, 4.3, 4.4]
    acceptance_criteria:
      - An integration test (under tests/integration/cognito_signup_test.py or equivalent) drives a real signup against the dev User Pool using boto3.cognito-idp.sign_up with synthetic credentials and a one-time test email
      - The test confirms the user via admin_confirm_sign_up to fire the post-confirmation trigger
      - The test then queries the dev Aurora cluster and asserts a users row exists with user_id matching the Cognito sub, sex derived from the gender attribute, and birthday parsed from the birthdate attribute
      - The test admin_delete_user cleans up the Cognito user; a manual cleanup of the orphaned Aurora row is documented but soft-delete is not yet implemented (that arrives in phase 9)
    notes: ""
