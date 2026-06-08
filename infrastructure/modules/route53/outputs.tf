output "record_fqdn" {
  description = "Fully-qualified domain name of the Route 53 A-alias record. Empty string when no record was created (dev path or half-configured state)."
  value       = try(aws_route53_record.this[0].fqdn, "")
}
