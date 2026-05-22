variable "environment" {
  description = "Deployment environment name (e.g. dev, prod)"
  type        = string
}

variable "region" {
  description = "AWS region in which to deploy networking resources"
  type        = string
  default     = "eu-central-1"
}
