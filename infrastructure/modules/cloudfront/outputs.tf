output "distribution_id" {
  description = "ID of the CloudFront distribution. Used to attach WAF web ACLs (story 5.4) and for cache invalidation in CI."
  value       = aws_cloudfront_distribution.this.id
}

output "distribution_domain_name" {
  description = "The d*.cloudfront.net hostname assigned to this distribution. The mobile app connects via this hostname on the dev path; on prod the Route 53 A-alias record (story 5.5) points the custom domain here."
  value       = aws_cloudfront_distribution.this.domain_name
}

output "distribution_arn" {
  description = "ARN of the CloudFront distribution."
  value       = aws_cloudfront_distribution.this.arn
}

output "edge_secret" {
  description = "The 64-character alphanumeric secret injected by CloudFront as the x-knotify-edge-secret header. Lambda handlers compare this value to verify that requests arrived via CloudFront. Mark as sensitive to prevent Terraform from logging it in plan/apply output."
  value       = random_password.edge_secret.result
  sensitive   = true
}
