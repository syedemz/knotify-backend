variable "domain_name" {
  description = "Public domain name to point at CloudFront via an A-alias record. When empty the module produces zero resources (dev path)."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID for the public domain. Required when domain_name is non-empty. When empty the module produces zero resources (dev path)."
  type        = string
  default     = ""
}

variable "cloudfront_distribution_domain_name" {
  description = "The d*.cloudfront.net hostname of the CloudFront distribution. Used as the alias target in the A record. Required — this module exists solely to point the custom domain at CloudFront."
  type        = string
}

variable "cloudfront_hosted_zone_id" {
  description = "The Route 53 hosted zone ID for the CloudFront global endpoint. Always Z2FDTNDATAQYW2 in every AWS account — this is the well-known constant for CloudFront alias targets."
  type        = string
}
