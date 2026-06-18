output "role_arns" {
  description = "Map of IAM role ARNs keyed by role name. Consuming phases reference entries as module.iam_roles.role_arns[\"db_migrator\"] etc."
  value = {
    db_migrator                   = aws_iam_role.db_migrator.arn
    cognito_trigger               = aws_iam_role.cognito_trigger.arn
    aurora_reader                 = aws_iam_role.aurora_reader.arn
    aurora_writer                 = aws_iam_role.aurora_writer.arn
    blocks_writer                 = aws_iam_role.blocks_writer.arn
    friends_writer                = aws_iam_role.friends_writer.arn
    dynamodb_chat_writer          = aws_iam_role.dynamodb_chat_writer.arn
    dynamodb_notifications_writer = aws_iam_role.dynamodb_notifications_writer.arn
    stepfn_task                   = aws_iam_role.stepfn_task.arn
    aurora_reader_match           = aws_iam_role.aurora_reader_match.arn
    aurora_refresh_lambda         = aws_iam_role.aurora_refresh_lambda.arn
    chat_resolver                 = aws_iam_role.chat_resolver.arn
    # Story 8.1 — AppSync IAM roles
    appsync_logs                  = aws_iam_role.appsync_logs.arn
    appsync_chat_resolver_invoke  = aws_iam_role.appsync_chat_resolver_invoke.arn
    # Story 8.9a — room_state_publisher IAM role
    room_state_publisher          = aws_iam_role.room_state_publisher.arn
  }
}
