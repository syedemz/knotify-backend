variable "environment" {
  type        = string
  description = "Deployment environment name (e.g. dev, prod)."
}

variable "region" {
  type        = string
  description = "AWS region for all resources in this module."
  default     = "eu-central-1"
}
