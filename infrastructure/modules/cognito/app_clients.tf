# ---------------------------------------------------------------------------
# Cognito App Clients — story 4.2
#
# Two app clients are managed here:
#
#   1. aws_cognito_user_pool_client.app — the production SRP-only client used
#      by the React Native app via aws-amplify, which implements the SRP
#      handshake natively in JS. ADMIN/USER_PASSWORD flows are intentionally
#      absent so the backend never participates in credential transport.
#
#   2. aws_cognito_user_pool_client.integration_test — a dev-only testing
#      affordance for boto3, which lacks an SRP implementation. Created only
#      when var.environment == "dev" (count = 0 in prod). Phase 4.6
#      integration tests use this client to drive sign-in without SRP.
#      Production's app client surface is unchanged by this resource.
#
# Brainstorm M4 resolution: see phase-4 PRD context_summary.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Production app client
#
# Public client (generate_secret=false) — React Native apps must not embed
# a client secret (the APK/IPA is reversible). SRP-only auth flows enforce
# that credentials are never transmitted in plaintext to the backend.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool_client" "app" {
  name         = "knotify-${var.environment}-app"
  user_pool_id = aws_cognito_user_pool.this.id

  # Public client — no client secret.
  generate_secret = false

  # SRP-only: the React Native app drives the SRP handshake via aws-amplify.
  # ADMIN_USER_PASSWORD_AUTH and USER_PASSWORD_AUTH are intentionally excluded.
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Hides whether a username/email exists during sign-in to prevent
  # account-enumeration attacks.
  prevent_user_existence_errors = "ENABLED"

  # Token validity — 60 minutes for access and id tokens, 30 days for refresh.
  # Values are in the units declared in token_validity_units below.
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }
}

# ---------------------------------------------------------------------------
# Dev-only integration-test app client
#
# Created only in the dev environment (count = 0 in prod). boto3 does not
# implement the SRP client-side handshake, so integration tests must use
# ADMIN_USER_PASSWORD_AUTH instead. This client is the sole entry point for
# that flow; production users never interact with it.
#
# Brainstorm N2: list-resource output idiom — outputs.tf uses
# try(aws_cognito_user_pool_client.integration_test[0].id, "") so that prod
# receives an empty string without a conditional output expression.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool_client" "integration_test" {
  count = var.environment == "dev" ? 1 : 0

  name         = "knotify-${var.environment}-integration-test"
  user_pool_id = aws_cognito_user_pool.this.id

  # Public client — no secret needed for test tooling.
  generate_secret = false

  # ADMIN_USER_PASSWORD_AUTH allows boto3-driven sign-in without SRP.
  # SRP is intentionally absent from this client.
  explicit_auth_flows = [
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]
}
