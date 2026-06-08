terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# ---------------------------------------------------------------------------
# Route 53 public-facing A-alias record — story 5.5
#
# Creates a single A-alias record pointing the custom domain at CloudFront.
# Route 53 is a global service — no us-east-1 provider alias is needed.
#
# Dev path (var.domain_name == "" || var.hosted_zone_id == ""):
#   count = 0 → zero resources, zero AWS API calls, apply is a no-op.
#   This is the default for both dev and prod until the prod cutover
#   documented in docs/PROD_CUTOVER.md §4d.
#
# Prod path (both non-empty):
#   count = 1 → one A-alias record pointing the custom domain at
#   var.cloudfront_distribution_domain_name with evaluate_target_health=false.
#   CloudFront does not support Route 53 health check probing on alias targets;
#   setting evaluate_target_health=true would fail at apply time.
#
# The count idiom handles the half-configured case cleanly: if only one of
# the two required inputs is set (e.g., a tfvars partially updated during
# prod cutover), the module remains a no-op rather than creating a broken
# record pointing at an unknown zone.
# ---------------------------------------------------------------------------

resource "aws_route53_record" "this" {
  count = (var.domain_name == "" || var.hosted_zone_id == "") ? 0 : 1

  zone_id = var.hosted_zone_id
  name    = var.domain_name
  type    = "A"

  alias {
    name                   = var.cloudfront_distribution_domain_name
    zone_id                = var.cloudfront_hosted_zone_id
    evaluate_target_health = false
  }
}
