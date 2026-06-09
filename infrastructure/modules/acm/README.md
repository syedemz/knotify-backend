# ACM Certificate Module

Story 5.2 — ACM certificate for custom domain in us-east-1.

Bundles three resources in a single module to avoid the chicken-and-egg cycle
that would arise from splitting them across stories:

1. `aws_acm_certificate` — DNS-validated certificate in us-east-1 (CloudFront requirement).
2. `aws_route53_record` — one record per validation domain (primary CN + every SAN).
3. `aws_acm_certificate_validation` — waits until ACM confirms all records, ensuring
   downstream CloudFront wiring never races against a pending cert.

## Dev vs Prod path

When `var.domain_name = ""` the module produces **zero** resources. This is the dev
path: the apply runs cleanly with no AWS calls, and all outputs are empty strings.

When `var.domain_name != ""` (prod path), all three resources are created and the
`certificate_arn` output is populated.

## Provider alias

This module must be called with the `aws.us_east_1` provider alias because ACM
certificates for CloudFront must reside in us-east-1. The module declares
`configuration_aliases = [aws.us_east_1]` in its `required_providers` block.

Caller pattern:

```hcl
module "acm" {
  source = "../../modules/acm"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  domain_name    = var.domain_name
  hosted_zone_id = var.hosted_zone_id
}
```

## Inputs

| Name | Type | Default | Description |
|------|------|---------|-------------|
| `domain_name` | `string` | `""` | Primary domain name. Empty = dev path (zero resources). |
| `hosted_zone_id` | `string` | `""` | Route 53 zone for DNS validation records. Required when `domain_name != ""`. |
| `subject_alternative_names` | `list(string)` | `[]` | Additional SANs to include on the certificate. |

## Outputs

| Name | Description |
|------|-------------|
| `certificate_arn` | ARN of the ACM certificate. Empty string on dev path. |
| `certificate_validated` | Marker — id of the validation resource. Empty on dev path. Use as a `depends_on` proxy in downstream modules to ensure the cert is validated before attaching to CloudFront. |

## Prod cutover

Before enabling the prod path, ensure:

- A public hosted zone exists in Route 53 for the domain.
- `prod.tfvars` has `domain_name` and `hosted_zone_id` set.
- The CloudFront distribution module (story 5.3) references `module.acm.certificate_arn`.

See `docs/PROD_CUTOVER.md` for the full prod cutover checklist.
