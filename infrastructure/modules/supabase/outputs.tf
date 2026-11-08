output "secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Supabase credential (consumed by iam_roles and Lambda env-var wiring in story 14.6)"
  value       = aws_secretsmanager_secret.credential.arn
  sensitive   = true
}

output "secret_name" {
  description = "Name of the Secrets Manager secret (knotify/<env>/supabase_credential)"
  value       = aws_secretsmanager_secret.credential.name
}
