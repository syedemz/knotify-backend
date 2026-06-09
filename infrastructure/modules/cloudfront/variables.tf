variable "api_gateway_domain_name" {
  description = "Hostname-only endpoint of the HTTP API Gateway origin (no https:// scheme, no trailing path). Pass replace(module.api_gateway.execute_api_endpoint, \"https://\", \"\") at the env level."
  type        = string
}

variable "domain_name" {
  description = "Custom domain name for the CloudFront distribution. Empty string (default) uses the free cloudfront_default_certificate and no aliases. Set in prod.tfvars for the prod cutover."
  type        = string
  default     = ""
}

variable "acm_certificate_arn" {
  description = "ARN of the ACM certificate in us-east-1. Required when domain_name is non-empty. Leave empty on the dev path."
  type        = string
  default     = ""
}

variable "web_acl_id" {
  description = "ARN of the WAFv2 web ACL to associate with this distribution. Empty string disables the association. The CloudFront distribution's web_acl_id field accepts the WAFv2 ARN directly (do not use aws_wafv2_web_acl_association — WAFv2 does not support CloudFront resources via that API)."
  type        = string
  default     = ""
}

variable "price_class" {
  description = "CloudFront price class. PriceClass_100 restricts edge locations to NA + EU. Widen in phase 11 if user geography demands it."
  type        = string
  default     = "PriceClass_100"
}

variable "tags" {
  description = "Additional resource tags merged onto the distribution."
  type        = map(string)
  default     = {}
}
