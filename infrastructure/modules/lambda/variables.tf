variable "function_name" {
  description = "Name of the Lambda function"
  type        = string
}

variable "handler" {
  description = "Lambda handler in the form <module>.<function> (e.g. handler.handler)"
  type        = string
}

variable "runtime" {
  description = "Lambda runtime identifier"
  type        = string
  default     = "python3.14"
}

variable "architectures" {
  description = "List of CPU architectures the function supports"
  type        = list(string)
  default     = ["arm64"]
}

variable "layers" {
  description = "List of Lambda layer ARNs to attach"
  type        = list(string)
  default     = []
}

variable "environment_variables" {
  description = "Map of environment variables to inject into the function. Merged with module-level defaults (POWERTOOLS_SERVICE_NAME, LOG_LEVEL); consumer values override defaults."
  type        = map(string)
  default     = {}
}

variable "vpc_config" {
  description = "VPC configuration for the Lambda function. Set to null (the default) to run outside a VPC."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

variable "role_arn" {
  description = "ARN of the IAM execution role to assign to the Lambda function"
  type        = string
}

variable "memory_size" {
  description = "Amount of memory (MB) allocated to the Lambda function"
  type        = number
  default     = 512
}

variable "timeout" {
  description = "Maximum execution time (seconds) for the Lambda function"
  type        = number
  default     = 10
}

variable "filename" {
  description = "Path to the deployment package .zip file"
  type        = string
}
