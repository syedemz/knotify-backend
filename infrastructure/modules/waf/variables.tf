variable "environment" {
  description = "Deployment environment name (e.g. dev, prod). Used to build the ACL name when var.name is not overridden."
  type        = string
}

variable "name" {
  description = "Name for the WAF web ACL. Defaults to knotify-<environment>-edge-waf."
  type        = string
  default     = ""
}

variable "rate_limit" {
  description = "Maximum number of requests from a single IP in any 5-minute window before the rate-based rule blocks the source IP. Default 2000 is a generous ceiling for a pre-launch app."
  type        = number
  default     = 2000
}

variable "tags" {
  description = "Additional resource tags merged on top of the provider default_tags. Pass {} to add no extra tags."
  type        = map(string)
  default     = {}
}
