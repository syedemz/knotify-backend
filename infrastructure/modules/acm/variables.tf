variable "domain_name" {
  description = "Primary domain name for the ACM certificate. When empty the module produces zero resources (dev path)."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID in which to create the DNS validation records. Required when domain_name is non-empty."
  type        = string
  default     = ""
}

variable "subject_alternative_names" {
  description = "Additional domain names (SANs) to include on the certificate. Ignored when domain_name is empty."
  type        = list(string)
  default     = []
}
