variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names."
  type        = string
}

variable "user_pool_id" {
  description = "Cognito User Pool ID for the primary AMAZON_COGNITO_USER_POOLS auth mode. Pass module.cognito.user_pool_id from each environment root module."
  type        = string
}

variable "appsync_logs_role_arn" {
  description = "ARN of the appsync_logs IAM role (trust: appsync.amazonaws.com, policy: AWSAppSyncPushToCloudWatchLogs). Sourced from module.iam_roles.role_arns[\"appsync_logs\"]."
  type        = string
}

variable "appsync_invoke_role_arn" {
  description = "ARN of the appsync_chat_resolver_invoke IAM role. Used as the service_role_arn on the chat_resolver_ds Lambda datasource. Sourced from module.iam_roles.role_arns[\"appsync_chat_resolver_invoke\"]."
  type        = string
}

variable "chat_resolver_lambda_arn" {
  description = "ARN of the chat_resolver Lambda live alias. Used to register the chat_resolver_ds AWS_LAMBDA datasource. Sourced from module.chat_resolver.lambda_arn."
  type        = string
}

# ---------------------------------------------------------------------------
# DynamoDB table names and ARNs — five chat domain tables
# Table ARNs are used in the datasource service_role_arn policy (via the
# appsync_chat_resolver_invoke role); table names are used as the DynamoDB
# datasource table_name attributes.
# ---------------------------------------------------------------------------

variable "chat_rooms_table_name" {
  description = "Name of the ChatRooms DynamoDB table. Sourced from module.dynamodb.chat_rooms_table_name."
  type        = string
}

variable "chat_rooms_table_arn" {
  description = "ARN of the ChatRooms DynamoDB table. Sourced from module.dynamodb.chat_rooms_arn."
  type        = string
}

variable "chat_room_membership_table_name" {
  description = "Name of the ChatRoomMembership DynamoDB table. Sourced from module.dynamodb.chat_room_membership_table_name."
  type        = string
}

variable "chat_room_membership_table_arn" {
  description = "ARN of the ChatRoomMembership DynamoDB table. Sourced from module.dynamodb.chat_room_membership_arn."
  type        = string
}

variable "chat_messages_table_name" {
  description = "Name of the ChatMessages DynamoDB table. Sourced from module.dynamodb.chat_messages_table_name."
  type        = string
}

variable "chat_messages_table_arn" {
  description = "ARN of the ChatMessages DynamoDB table. Sourced from module.dynamodb.chat_messages_arn."
  type        = string
}

variable "message_reads_table_name" {
  description = "Name of the MessageReads DynamoDB table. Sourced from module.dynamodb.message_reads_table_name."
  type        = string
}

variable "message_reads_table_arn" {
  description = "ARN of the MessageReads DynamoDB table. Sourced from module.dynamodb.message_reads_arn."
  type        = string
}

variable "notifications_table_name" {
  description = "Name of the Notifications DynamoDB table. Sourced from module.dynamodb.notifications_table_name."
  type        = string
}

variable "notifications_table_arn" {
  description = "ARN of the Notifications DynamoDB table. Sourced from module.dynamodb.notifications_arn."
  type        = string
}

variable "dynamodb_role_arn" {
  description = "ARN of the IAM role that AppSync assumes to access DynamoDB. Used as service_role_arn on every AMAZON_DYNAMODB datasource. This role must grant dynamodb:GetItem / PutItem / Query etc. on the five chat tables. Sourced from module.iam_roles.role_arns[\"chat_resolver\"] (the chat_resolver Lambda role is also the DynamoDB-access role for the AppSync DDB datasources)."
  type        = string
}
