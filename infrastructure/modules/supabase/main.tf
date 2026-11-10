terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.20"
    }
  }
}

# ---------------------------------------------------------------------------
# Supabase credential secret
#
# This module owns the AWS-side credential plumbing only. Supabase is a managed
# external service — no Supabase-side resources are provisioned here.
#
# The secret is seeded with a placeholder initial version (empty password).
# The real password is injected directly via:
#   aws secretsmanager put-secret-value --secret-id <name> --secret-string '...'
# lifecycle.ignore_changes on secret_string ensures that operator-set passwords
# survive subsequent `terraform apply` runs without being overwritten.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "credential" {
  name        = "knotify/${var.environment}/supabase_credential"
  description = "Knotify ${var.environment} Supabase pooler credential (username + password)"

  tags = {
    Name        = "knotify/${var.environment}/supabase_credential"
    Environment = var.environment
  }
}

resource "aws_secretsmanager_secret_version" "initial" {
  secret_id = aws_secretsmanager_secret.credential.id

  # Placeholder payload — empty password. The real password is injected by the
  # operator via aws-cli after apply (story 14.12); lifecycle.ignore_changes
  # below ensures subsequent applies do not overwrite the real value.
  secret_string = jsonencode({
    username = var.username
    password = ""
  })

  lifecycle {
    ignore_changes = [secret_string]
  }
}
