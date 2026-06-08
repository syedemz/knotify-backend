output "web_acl_id" {
  description = "ID of the WAF web ACL. Used for CloudFront association and for the web_acl_id field on the CloudFront distribution resource if needed."
  value       = aws_wafv2_web_acl.this.id
}

output "web_acl_arn" {
  description = "ARN of the WAF web ACL. Used when attaching the ACL to the CloudFront distribution via aws_wafv2_web_acl_association."
  value       = aws_wafv2_web_acl.this.arn
}
