variable "name" {
  description = "Name for the Cognito User Pool (e.g. knotify-dev-user-pool)"
  type        = string
}

variable "post_confirmation_lambda_arn" {
  description = <<-EOT
    ARN of the cognito_post_confirmation Lambda function. Wired into the
    lambda_config.post_confirmation field on the User Pool. Pass in the
    unqualified function ARN (not the alias ARN) — Cognito invokes the
    function directly. In dev/prod environments this is
    module.cognito_post_confirmation.function_arn.
  EOT
  type        = string
}

variable "environment" {
  description = "Deployment environment (dev | prod). Used in resource names and tags."
  type        = string
}

variable "advanced_security_mode" {
  description = <<-EOT
    Cognito Advanced Security Mode. Valid values: OFF | AUDIT | ENFORCED.
    Default is AUDIT — the minimum required by the V2 PreTokenGeneration trigger
    (brainstorm B2). Upgrade to ENFORCED in the phase 11 hardening pass.
  EOT
  type        = string
  default     = "AUDIT"
}

# ---------------------------------------------------------------------------
# Token validity defaults
#
# These defaults are consumed by the app client resources added in story 4.2.
# Declared here so the module's token validity contract is established in one
# place and story 4.2 does not need to re-declare defaults.
# ---------------------------------------------------------------------------

variable "access_token_validity" {
  description = "Access token validity in hours. Default 1 hour."
  type        = number
  default     = 1
}

variable "id_token_validity" {
  description = "ID token validity in hours. Default 1 hour."
  type        = number
  default     = 1
}

variable "refresh_token_validity" {
  description = "Refresh token validity in days. Default 30 days."
  type        = number
  default     = 30
}

variable "pre_token_generation_lambda_arn" {
  description = <<-EOT
    ARN of the cognito_pre_token_generation Lambda function. Wired into the
    lambda_config pre_token_generation_config block on the User Pool using
    lambda_version = "V2_0" (brainstorm B2). The V1 pre_token_generation field
    is explicitly forbidden — V2 is required for claimsAndScopeOverrideDetails.
    Pass in the unqualified function ARN (not the alias ARN) — Cognito invokes
    the function directly. In dev/prod environments this is
    module.cognito_pre_token_generation.function_arn.
  EOT
  type        = string
}
