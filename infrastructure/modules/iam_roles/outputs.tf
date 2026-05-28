output "role_arns" {
  description = "Map of IAM role ARNs keyed by role name. Consuming phases reference entries as module.iam_roles.role_arns[\"db_migrator\"] etc."
  value = {
    db_migrator                   = aws_iam_role.db_migrator.arn
    cognito_trigger               = aws_iam_role.cognito_trigger.arn
    aurora_reader                 = aws_iam_role.aurora_reader.arn
    aurora_writer                 = aws_iam_role.aurora_writer.arn
    dynamodb_chat_writer          = aws_iam_role.dynamodb_chat_writer.arn
    dynamodb_notifications_writer = aws_iam_role.dynamodb_notifications_writer.arn
    stepfn_task                   = aws_iam_role.stepfn_task.arn
  }
}
