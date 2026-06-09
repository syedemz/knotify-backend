terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"

      # The ACM certificate and its CloudFront viewer certificate must live in
      # us-east-1 regardless of the environment's primary region. Callers pass
      # the alias via `providers = { aws.us_east_1 = aws.us_east_1 }`.
      configuration_aliases = [aws.us_east_1]
    }
  }
}

# ---------------------------------------------------------------------------
# Local: validation record map
#
# When domain_name is non-empty, build a map keyed by domain name so
# for_each on aws_route53_record can create one record per validation domain.
# When domain_name is empty (dev path), the map is empty so for_each creates
# zero records — combining count and for_each on the same resource is not
# permitted in Terraform, hence the local-map approach.
# ---------------------------------------------------------------------------

locals {
  validation_records_map = var.domain_name == "" ? {} : {
    for dvo in aws_acm_certificate.this[0].domain_validation_options :
    dvo.domain_name => {
      name   = dvo.resource_record_name
      type   = dvo.resource_record_type
      record = dvo.resource_record_value
    }
  }
}

# ---------------------------------------------------------------------------
# ACM Certificate — us-east-1
#
# Required by CloudFront: viewer certificates must reside in us-east-1.
# Uses provider alias so the env-level `providers` block threads the alias.
# count = 0 on the dev path (var.domain_name == "") → zero resources, zero
# AWS API calls, apply completes instantly.
# create_before_destroy prevents downtime during cert rotation.
# ---------------------------------------------------------------------------

resource "aws_acm_certificate" "this" {
  count    = var.domain_name == "" ? 0 : 1
  provider = aws.us_east_1

  domain_name               = var.domain_name
  subject_alternative_names = var.subject_alternative_names
  validation_method         = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# ---------------------------------------------------------------------------
# Route 53 validation records
#
# One record per domain in domain_validation_options (covers the primary CN
# and every SAN). Using for_each on the local map yields zero records on the
# dev path (empty map) and one-per-validation-domain on the prod path.
# TTL = 60 s — the minimum AWS accepts; validation completes faster.
# ---------------------------------------------------------------------------

resource "aws_route53_record" "cert_validation" {
  for_each = local.validation_records_map

  zone_id = var.hosted_zone_id
  name    = each.value.name
  type    = each.value.type
  ttl     = 60
  records = [each.value.record]

  allow_overwrite = true
}

# ---------------------------------------------------------------------------
# ACM Certificate Validation
#
# Waits until ACM confirms that all DNS validation records have been seen.
# count = 0 on the dev path; count = 1 on the prod path.
# fqdns feeds every validation record into the wait so ACM verifies all SANs.
# ---------------------------------------------------------------------------

resource "aws_acm_certificate_validation" "this" {
  count    = var.domain_name == "" ? 0 : 1
  provider = aws.us_east_1

  certificate_arn         = aws_acm_certificate.this[0].arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}
