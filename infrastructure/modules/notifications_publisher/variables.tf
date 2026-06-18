variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names."
  type        = string
}

variable "function_name" {
  description = "Name of the notifications_publisher Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the notifications_publisher deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (notifications_publisher) to assign to the Lambda function."
  type        = string
}

variable "notifications_stream_arn" {
  description = "DynamoDB stream ARN for the Notifications table. Wired as the event source for the Lambda."
  type        = string
}

variable "appsync_graphql_url" {
  description = "HTTPS URL of the AppSync GraphQL endpoint. Injected as APPSYNC_GRAPHQL_URL env var so the Lambda can POST the publish mutation."
  type        = string
}
