output "certificate_arn" {
  description = "ARN of the ACM certificate. Empty string when domain_name is empty (dev path)."
  value       = try(aws_acm_certificate.this[0].arn, "")
}

output "certificate_validated" {
  description = "Marker output — the id of aws_acm_certificate_validation.this when the cert has been validated. Empty string on the dev path. Downstream modules can use this as a depends_on proxy to ensure the cert is fully validated before attaching it to CloudFront."
  value       = try(aws_acm_certificate_validation.this[0].id, "")
}
