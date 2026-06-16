output "function_arn" {
  description = "ARN of the refresh_deck_view Lambda function (unqualified). Used by the iam_roles module to scope aurora_writer's lambda:InvokeFunction permission."
  value       = module.lambda.function_arn
}

output "function_name" {
  description = "Name of the refresh_deck_view Lambda function."
  value       = module.lambda.function_name
}

output "alias_arn" {
  description = "ARN of the live alias for the refresh_deck_view Lambda."
  value       = module.lambda.alias_arn
}
