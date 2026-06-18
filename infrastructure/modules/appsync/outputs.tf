output "api_id" {
  description = "AppSync GraphQL API ID — consumed by resolver attachment resources in stories 8.3–8.8."
  value       = aws_appsync_graphql_api.knotify.id
}

output "graphql_url" {
  description = "HTTPS GraphQL endpoint URL for client mutations and queries."
  value       = aws_appsync_graphql_api.knotify.uris["GRAPHQL"]
}

output "realtime_url" {
  description = "WSS real-time endpoint URL for AppSync subscriptions."
  value       = aws_appsync_graphql_api.knotify.uris["REALTIME"]
}

output "chat_resolver_ds_name" {
  description = "Name of the AWS_LAMBDA datasource for the chat_resolver Lambda. Consumed by pipeline resolver attachment resources in stories 8.3–8.8 to reference the datasource by name."
  value       = aws_appsync_datasource.chat_resolver_ds.name
}

output "api_arn" {
  description = "ARN of the AppSync GraphQL API — consumed by room_state_publisher_role and notifications_publisher_role (stories 8.9a, 8.9c) to scope appsync:GraphQL to exact publish-mutation field ARNs."
  value       = aws_appsync_graphql_api.knotify.arn
}
