variable "name" {
  description = "Name for the Cognito User Pool (e.g. knotify-dev-user-pool)"
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
