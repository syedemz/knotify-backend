variable "function_name" {
  description = "Name of the deletion_initiator Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the deletion_initiator deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (deletion_initiator) to assign to the Lambda function."
  type        = string
}

variable "state_machine_arn" {
  description = "ARN of the account-deletion Step Functions state machine. Injected as STATE_MACHINE_ARN env var so the handler can call states:StartExecution at runtime."
  type        = string
}

variable "edge_secret" {
  description = "CloudFront edge secret value. Injected as EDGE_SECRET env var so the @with_edge_secret decorator can validate all traffic arrived via CloudFront."
  type        = string
  sensitive   = true
}

variable "layers" {
  description = "List of Lambda layer ARNs to attach. Must include the knotify_obs observability layer (provides init_logger and with_edge_secret)."
  type        = list(string)
  default     = []
}
