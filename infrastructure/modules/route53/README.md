# Route 53 Module

Creates a public-facing Route 53 A-alias record pointing a custom domain at a CloudFront distribution.

## Purpose

This module owns only the A-alias record for the custom domain. Certificate provisioning
and DNS validation records live in the `acm` module (story 5.2). This separation avoids
the chicken-and-egg cycle between cert validation records and the public alias record.

## Dev / Prod branching

The module uses a `count` idiom so both inputs must be non-empty to create any resource:

```
count = (var.domain_name == "" || var.hosted_zone_id == "") ? 0 : 1
```

| domain_name | hosted_zone_id | Result |
|-------------|----------------|--------|
| `""`        | `""`           | Zero resources — dev path |
| `"api.example.com"` | `""` | Zero resources — half-configured, safe no-op |
| `""`        | `"Z123456"`    | Zero resources — half-configured, safe no-op |
| `"api.example.com"` | `"Z123456"` | One A-alias record created |

## Usage

### Dev environment (dev/main.tf)

```hcl
module "route53" {
  source = "../../modules/route53"

  domain_name                         = var.domain_name      # "" in dev
  hosted_zone_id                      = var.hosted_zone_id   # "" in dev
  cloudfront_distribution_domain_name = module.cloudfront.distribution_domain_name
  cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
}
```

### Prod environment (prod/main.tf)

```hcl
module "route53" {
  source = "../../modules/route53"

  domain_name                         = var.domain_name      # set in prod.tfvars at cutover
  hosted_zone_id                      = var.hosted_zone_id   # set in prod.tfvars at cutover
  cloudfront_distribution_domain_name = module.cloudfront.distribution_domain_name
  cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
}
```

`Z2FDTNDATAQYW2` is the CloudFront global hosted zone ID. It is the same in every AWS
account and must be passed as a literal — it is not a resource output.

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `domain_name` | `string` | `""` | Public domain to point at CloudFront. Empty = dev path (no resources). |
| `hosted_zone_id` | `string` | `""` | Route 53 hosted zone ID for the domain. Empty = dev path (no resources). |
| `cloudfront_distribution_domain_name` | `string` | required | The `d*.cloudfront.net` hostname from `module.cloudfront.distribution_domain_name`. |
| `cloudfront_hosted_zone_id` | `string` | required | Always `Z2FDTNDATAQYW2` — the CloudFront global zone ID. |

## Outputs

| Name | Type | Description |
|------|------|-------------|
| `record_fqdn` | `string` | FQDN of the Route 53 record. Empty string on dev path. |

## Provider notes

Route 53 is a global service. This module uses the default `aws` provider — no `us-east-1`
alias is required (unlike `acm` and `waf` which must deploy resources in us-east-1).

## Tests

```bash
cd infrastructure/modules/route53
terraform test
```

10 plan-mode tests covering dev path, both half-configured states, and prod path.
No AWS credentials are required — all tests use `mock_provider`.
