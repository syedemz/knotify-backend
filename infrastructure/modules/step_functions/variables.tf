variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names and the DeletionFailed metric stage dimension."
  type        = string
}

variable "execution_role_arn" {
  description = "ARN of the Step Functions execution IAM role (stepfn_deletion_exec). Sourced from module.iam_roles.role_arns[\"stepfn_deletion_exec\"] in each environment root module."
  type        = string
}

# ---------------------------------------------------------------------------
# Lambda ARN inputs — one per deletion task
#
# The root module in environments/ supplies the actual ARNs from each Lambda
# module's outputs after those Lambdas are deployed. Module-level TF tests
# pass stub ARN strings — end-to-end correctness of the wired state machine
# is validated in story 9.13.
# ---------------------------------------------------------------------------

variable "lambda_arns" {
  description = "Map of Lambda ARNs for each deletion task. Keys match the task names in the ASL definition template."
  type = object({
    validate_deletion_request  = string
    cognito_user_state         = string
    deactivate_chat_rooms      = string
    soft_delete_aurora         = string
    delete_dynamodb_personal   = string
    anonymize_chat_messages    = string
    hard_purge_now             = string
    hard_delete_user_chat_msgs = string
    write_audit_log            = string
  })
}
