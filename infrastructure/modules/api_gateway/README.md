# api_gateway Terraform module

Creates an HTTP API Gateway (v2) with:

- A `$default` auto-deploy stage with configurable throttling limits
- A Cognito JWT authorizer that validates bearer tokens against the User Pool
- A CloudWatch access-log group (retention 7 days by default)

## Usage

```hcl
module "api_gateway" {
  source = "../../modules/api_gateway"

  name = "knotify-${var.environment}-api"

  cognito_user_pool_endpoint  = module.cognito.user_pool_endpoint
  cognito_audience_client_ids = compact([
    module.cognito.app_client_id,
    module.cognito.integration_test_app_client_id,
  ])

  # Dev throttling is conservative (pre-launch); prod overrides these.
  # throttling_burst_limit = 10   # default
  # throttling_rate_limit  = 25   # default
}
```

For prod, pass explicit throttling values:

```hcl
module "api_gateway" {
  source = "../../modules/api_gateway"

  name = "knotify-prod-api"

  cognito_user_pool_endpoint  = module.cognito.user_pool_endpoint
  cognito_audience_client_ids = compact([
    module.cognito.app_client_id,
    module.cognito.integration_test_app_client_id,
  ])

  throttling_burst_limit = 500
  throttling_rate_limit  = 1000
}
```

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `name` | `string` | required | Name prefix for all resources |
| `cognito_user_pool_endpoint` | `string` | required | JWT issuer URL (`module.cognito.user_pool_endpoint`) |
| `cognito_audience_client_ids` | `list(string)` | required | List of accepted Cognito app client IDs |
| `throttling_burst_limit` | `number` | `10` | Max concurrent requests (dev default) |
| `throttling_rate_limit` | `number` | `25` | Max requests/sec (dev default) |
| `access_log_retention_days` | `number` | `7` | CloudWatch log retention in days |

### `cognito_audience_client_ids` — the `compact()` pattern

The JWT authorizer's `audience` must include every Cognito app client ID that is permitted to issue valid tokens. In dev, integration tests mint tokens via the `ADMIN_USER_PASSWORD_AUTH` flow using a dedicated dev-only app client. Both client IDs must appear in `audience` so those tokens pass authorizer validation.

In prod, the integration-test client does not exist (`module.cognito.integration_test_app_client_id` returns `""`). `compact()` drops empty strings, so the audience list automatically collapses to the production client only.

Build the list at the call site:

```hcl
cognito_audience_client_ids = compact([
  module.cognito.app_client_id,
  module.cognito.integration_test_app_client_id,  # "" in prod — compact drops it
])
```

## Outputs

| Name | Description |
|------|-------------|
| `api_id` | HTTP API ID |
| `api_arn` | HTTP API ARN |
| `execute_api_endpoint` | Raw `execute-api` HTTPS URL (before CloudFront) |
| `authorizer_id` | Cognito JWT authorizer ID — use in `aws_apigatewayv2_route.authorizer_id` |
| `default_stage_arn` | `$default` stage ARN |
| `access_log_group_name` | CloudWatch log group name for access logs |

## Wiring routes

After this module is created, attach routes with `aws_apigatewayv2_integration` and `aws_apigatewayv2_route`. Story 5.7 wires the first route (`GET /v1/_internal/hello`):

```hcl
resource "aws_apigatewayv2_route" "hello" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/_internal/hello"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
  target             = "integrations/${aws_apigatewayv2_integration.hello.id}"
}
```

## CORS

CORS is **intentionally not configured** in this module.

The v1 Knotify client is a React Native mobile application that uses native HTTP libraries (not a browser `fetch`/`XHR`). Native HTTP clients do not send CORS preflight (`OPTIONS`) requests. Configuring CORS in API Gateway would add unnecessary infrastructure complexity with no benefit to the current client.

**Future work:** if Knotify ships an Expo Web target, the web browser will send CORS preflights. CORS configuration should be revisited in Phase 11 hardening (or when Expo Web launches, whichever comes first). At that point, add `cors_configuration` to `aws_apigatewayv2_api.this` with explicit `allow_origins`, `allow_methods`, and `allow_headers` matching the web app's domain.

## Access log format

Logs are written to CloudWatch in JSON using the following schema:

```json
{
  "requestId":          "$context.requestId",
  "status":             "$context.status",
  "routeKey":           "$context.routeKey",
  "integrationLatency": "$context.integrationLatency",
  "authLatency":        "$context.authorizer.latency",
  "sourceIp":           "$context.identity.sourceIp",
  "userAgent":          "$context.identity.userAgent"
}
```

The `$context.*` placeholders are template variables evaluated by API Gateway at request time, not by Terraform. The format is valid JSON in CloudWatch Logs Insights from day one.
