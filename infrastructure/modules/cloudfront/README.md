# cloudfront module

Creates the CloudFront distribution that sits in front of the HTTP API Gateway, injects an origin secret header, and optionally attaches a custom domain certificate.

## What this module does

- Creates one `aws_cloudfront_distribution` with the HTTP API execute-api endpoint as the origin.
- Injects `x-knotify-edge-secret` as a custom origin header so Lambda handlers can verify traffic arrived via CloudFront (not directly via the execute-api URL). The secret is a 64-character alphanumeric `random_password`.
- Uses AWS-managed `Managed-CachingDisabled` + `Managed-AllViewer` policies so CloudFront behaves as a transparent proxy with no caching.
- Branches on `var.domain_name`:
  - **dev path** (`domain_name = ""`): uses the free `cloudfront_default_certificate` and sets no aliases.
  - **prod path** (`domain_name != ""`): attaches the provided ACM certificate (us-east-1, from the ACM module), uses SNI-only and TLSv1.2_2021, and sets `aliases = [var.domain_name]`.

## Usage

```hcl
module "cloudfront" {
  source = "../../modules/cloudfront"

  api_gateway_domain_name = replace(module.api_gateway.execute_api_endpoint, "https://", "")
  domain_name             = var.domain_name            # "" in dev
  acm_certificate_arn     = module.acm.certificate_arn # "" in dev (ACM module returns "" when domain_name="")
}
```

The `replace()` call strips the `https://` scheme from the execute-api URL; CloudFront's `domain_name` field in an origin block must be a hostname only.

## Outputs

| Name | Description |
|------|-------------|
| `distribution_id` | CloudFront distribution ID. Used by WAF association (story 5.4) and cache invalidation in CI. |
| `distribution_domain_name` | The `d*.cloudfront.net` hostname. Used as the base URL for integration tests on the dev path. |
| `distribution_arn` | ARN of the distribution. Required by `aws_wafv2_web_acl_association`. |
| `edge_secret` | **Sensitive.** The 64-character secret injected as `x-knotify-edge-secret`. Consumed by story 5.6 (Lambda helper) and story 5.7 (smoke test). |

## Lambda env injection pattern

Every Lambda that enforces the edge secret (story 5.6 and all phase-6 Lambdas) must:

1. Set `EDGE_SECRET = module.cloudfront.edge_secret` in the Lambda module's `environment_variables`.
2. Apply the `@with_edge_secret` decorator from the observability layer to the handler function.

The decorator handles the `EdgeSecretRequired` exception → 403 response translation centrally.

## Secret rotation

The `random_password.edge_secret` resource is intentionally stable — it does not rotate on every apply. To rotate the secret:

```bash
terraform taint module.cloudfront.random_password.edge_secret
terraform apply
```

After rotation, every Lambda that reads `EDGE_SECRET` from its environment variable will be updated on the next apply that touches the Lambda resource (the `environment_variables` map triggers a Lambda update).

## No `forwarded_values`

The `forwarded_values` block in `default_cache_behavior` is deprecated and removed in AWS provider 6.x. This module uses the AWS-managed policy data sources instead. Do not add `forwarded_values` back.
