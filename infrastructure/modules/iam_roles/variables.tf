variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names and ARN patterns."
  type        = string
}

variable "aurora_master_user_secret_arn" {
  description = "Exact ARN of the Aurora-managed master user secret in Secrets Manager (i.e. aws_rds_cluster.master_user_secret[0].secret_arn). Used to scope db_migrator's GetSecretValue permission to that specific secret. The internal cluster id RDS embeds in this ARN is NOT the Terraform-visible cluster_resource_id, so the ARN must be plumbed through from the aurora module rather than reconstructed."
  type        = string
}

variable "cognito_user_pool_arn" {
  description = "ARN of the Cognito User Pool. Used to scope aurora_writer's cognito-idp:AdminUpdateUserAttributes permission (story 7.0b). Pass module.cognito.user_pool_arn from each environment root module."
  type        = string
  default     = ""
}

variable "refresh_lambda_arn" {
  description = "ARN of the refresh_deck_view Lambda function. Used to scope aurora_writer's lambda:InvokeFunction permission (story 7.4) and to scope aurora_refresh_lambda's db_migrator credential access. Pass module.refresh_deck_view.function_arn from each environment root module."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Story 8.0 — chat_resolver IAM role DynamoDB scoping
# ---------------------------------------------------------------------------

variable "chat_rooms_table_arn" {
  description = "ARN of the ChatRooms DynamoDB table. Used to scope chat_resolver IAM policy (story 8.0). Pass module.dynamodb.chat_rooms_arn from each environment root module."
  type        = string
  default     = ""
}

variable "chat_room_membership_table_arn" {
  description = "ARN of the ChatRoomMembership DynamoDB table. Used to scope chat_resolver IAM policy (story 8.0). Pass module.dynamodb.chat_room_membership_arn from each environment root module."
  type        = string
  default     = ""
}

variable "chat_messages_table_arn" {
  description = "ARN of the ChatMessages DynamoDB table. Used to scope chat_resolver IAM policy (story 8.0). Pass module.dynamodb.chat_messages_arn from each environment root module."
  type        = string
  default     = ""
}

variable "message_reads_table_arn" {
  description = "ARN of the MessageReads DynamoDB table. Used to scope chat_resolver IAM policy (story 8.0). Pass module.dynamodb.message_reads_arn from each environment root module."
  type        = string
  default     = ""
}

variable "notifications_table_arn" {
  description = "ARN of the Notifications DynamoDB table. Used to scope chat_resolver IAM policy (story 8.0). Pass module.dynamodb.notifications_arn from each environment root module."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Story 8.1 — AppSync IAM roles
# ---------------------------------------------------------------------------

variable "chat_resolver_lambda_arn" {
  description = "ARN of the chat_resolver Lambda live alias. Used to scope appsync_chat_resolver_invoke's lambda:InvokeFunction permission (story 8.1). Pass module.chat_resolver.lambda_arn from each environment root module."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Story 8.9a — room_state_publisher IAM role scoping
# ---------------------------------------------------------------------------

variable "chat_rooms_stream_arn" {
  description = "DynamoDB stream ARN for the ChatRooms table. Used to scope room_state_publisher_role's dynamodb stream read actions (story 8.9a). Pass module.dynamodb.chat_rooms_stream_arn from each environment root module."
  type        = string
  default     = ""
}

variable "appsync_api_arn" {
  description = "ARN of the AppSync GraphQL API. Used to scope room_state_publisher_role's appsync:GraphQL permission to the exact _publishRoomDeactivated and _publishRoomReactivated field ARNs (story 8.9a), and notifications_publisher_role's appsync:GraphQL permission to publishNotification and _publishFriendRequestUpdated field ARNs (story 8.9c). Pass module.appsync.api_arn from each environment root module."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# Story 8.9c — notifications_publisher IAM role scoping
# ---------------------------------------------------------------------------

variable "notifications_stream_arn" {
  description = "DynamoDB stream ARN for the Notifications table. Used to scope notifications_publisher_role's dynamodb stream read actions (story 8.9c). Pass module.dynamodb.notifications_stream_arn from each environment root module."
  type        = string
  default     = ""
}
