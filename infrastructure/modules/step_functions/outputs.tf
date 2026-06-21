output "state_machine_arn" {
  description = "ARN of the account-deletion Step Functions state machine. Sourced by story 9.9 (deletion_initiator Lambda) to call states:StartExecution."
  value       = aws_sfn_state_machine.account_deletion.arn
}

output "log_group_arn" {
  description = "ARN of the CloudWatch log group for the state machine (with :* suffix required by Step Functions logging integration). Sourced by module.iam_roles as var.deletion_sfn_log_group_arn to scope the stepfn_deletion_exec logs:* policy."
  value       = "${aws_cloudwatch_log_group.account_deletion_sfn.arn}:*"
}
