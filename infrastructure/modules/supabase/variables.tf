variable "environment" {
  description = "Deployment environment name (e.g. dev, prod)"
  type        = string
}

variable "username" {
  description = "Supabase pooler username (e.g. postgres.<project-ref>). The caller passes var.supabase_username from the env root. Stored as a placeholder in the initial secret version; the real password is injected directly via aws secretsmanager put-secret-value per the phase 14 runbook (story 14.12) and is never stored in Terraform state."
  type        = string
}
