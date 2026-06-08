# WAF module

Creates an `aws_wafv2_web_acl` with `scope = "CLOUDFRONT"` and attaches it to a
CloudFront distribution via `aws_wafv2_web_acl_association`.

## Requirements

- AWS provider `>= 5.0`
- Provider alias `aws.us_east_1` must be declared by the caller and passed via
  `providers = { aws.us_east_1 = aws.us_east_1 }`.  CLOUDFRONT-scoped WAF resources
  must reside in `us-east-1` regardless of the environment's primary region.

## Usage

```hcl
module "waf" {
  source = "../../modules/waf"

  providers = {
    aws.us_east_1 = aws.us_east_1
  }

  environment                 = var.environment
  cloudfront_distribution_arn = module.cloudfront.distribution_arn
}
```

## Rule posture

| Priority | Rule group | Mode | Rationale |
|----------|-----------|------|-----------|
| 1 | AWSManagedRulesCommonRuleSet | `count` | False-positives on JSON payloads; review in phase 11 before flipping to `none` |
| 2 | AWSManagedRulesKnownBadInputsRuleSet | `none` (enforce) | Low false-positive rate; safe to enforce from day one |
| 3 | AWSManagedRulesSQLiRuleSet | `count` | Observe before enforcing; review sampled data in phase 11 |
| 10 | Rate-based per IP | `block` | 2000 requests per 5-minute window; tighten in phase 11 after traffic baselines |

Rules in `count` mode record a CloudWatch metric and let requests continue.
Rules in `none` mode apply the managed rule's default action (typically block).

## Phase 11 hardening

After phase 11 tuning, flip the `override_action` blocks for
`AWSManagedRulesCommonRuleSet` and `AWSManagedRulesSQLiRuleSet` from `count {}`
to `none {}` to move them from monitor mode to enforce mode.

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `environment` | string | — | Deployment environment (dev, prod). Used in the default ACL name. |
| `name` | string | `""` | Override for the ACL name. Defaults to `knotify-<environment>-edge-waf`. |
| `cloudfront_distribution_arn` | string | — | ARN of the CloudFront distribution. Required. |
| `rate_limit` | number | `2000` | Max requests per IP per 5-minute window before blocking. |
| `tags` | map(string) | `{}` | Extra tags merged on top of provider `default_tags`. |

## Outputs

| Name | Description |
|------|-------------|
| `web_acl_id` | ID of the WAF web ACL. |
| `web_acl_arn` | ARN of the WAF web ACL. |
