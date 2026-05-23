variable "environment" {
  description = "Deployment environment name (dev or prod)"
  type        = string
}

variable "project_name" {
  description = "Project name prefix used in table names and tags"
  type        = string
  default     = "knotify"
}

# ---------------------------------------------------------------------------
# Environment-specific safety flags.
# Defaults are dev-safe (no deletion protection, no PITR).
# Prod caller sets both to true via tfvars (story 2.14).
#
# Note: Terraform's lifecycle.prevent_destroy cannot reference variables —
# it only accepts literal booleans. The native DynamoDB deletion_protection_enabled
# attribute (set here) is therefore the mechanism for prod table safety per
# brainstorm finding #9. This keeps dev tear-down simple while blocking
# accidental destroy of prod user data.
# ---------------------------------------------------------------------------

variable "point_in_time_recovery_enabled" {
  description = "Whether to enable point-in-time recovery on DynamoDB tables (true in prod, false in dev)"
  type        = bool
  default     = false
}

variable "deletion_protection_enabled" {
  description = "Whether to enable DynamoDB native deletion protection (true in prod, false in dev)"
  type        = bool
  default     = false
}
