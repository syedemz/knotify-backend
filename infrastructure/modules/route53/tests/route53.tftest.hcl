# Route 53 module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/route53/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 5.5).
#
# The module uses only the default AWS provider (no alias) — Route 53 is a global
# service and requires no us-east-1 alias. mock_provider "aws" {} covers all resources.
#
# Tests covered (acceptance criteria mapping):
#   Test 1 — dev path: both empty → zero resources (AC 2)
#   Test 2 — half-configured: domain_name set, hosted_zone_id empty → zero resources (AC 2)
#   Test 3 — half-configured: hosted_zone_id set, domain_name empty → zero resources (AC 2)
#   Test 4 — prod path: both set → one aws_route53_record (AC 3)
#   Test 5 — prod path: record type is A (AC 3)
#   Test 6 — prod path: alias block targets cloudfront_distribution_domain_name (AC 3)
#   Test 7 — prod path: alias block uses cloudfront_hosted_zone_id Z2FDTNDATAQYW2 (AC 3)
#   Test 8 — prod path: alias evaluate_target_health = false (AC 3)
#   Test 9 — dev path: record_fqdn output is empty string (AC 5)
#   Test 10 — prod path: record_fqdn output is non-empty (AC 5)

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# Test 1: dev path — both empty → zero resources
#
# Satisfies AC 2: when domain_name and hosted_zone_id are both empty, the
# module creates NO AWS resources. This is the canonical dev path: no Route 53
# API calls are made, apply completes instantly.
# ---------------------------------------------------------------------------
run "dev_path_zero_resources_when_both_empty" {
  command = plan

  variables {
    domain_name                         = ""
    hosted_zone_id                      = ""
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = length(aws_route53_record.this) == 0
    error_message = "aws_route53_record must NOT be created when both domain_name and hosted_zone_id are empty (dev path)"
  }
}

# ---------------------------------------------------------------------------
# Test 2: half-configured — domain_name set, hosted_zone_id empty → zero resources
#
# Satisfies AC 2: the count idiom uses OR so a half-configured call (one
# variable set, the other empty) must also produce zero resources. Guards
# against partial Terraform variable injection mistakes during prod cutover.
# ---------------------------------------------------------------------------
run "half_configured_domain_set_zone_empty_zero_resources" {
  command = plan

  variables {
    domain_name                         = "api.example.com"
    hosted_zone_id                      = ""
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = length(aws_route53_record.this) == 0
    error_message = "aws_route53_record must NOT be created when hosted_zone_id is empty even if domain_name is set"
  }
}

# ---------------------------------------------------------------------------
# Test 3: half-configured — hosted_zone_id set, domain_name empty → zero resources
#
# Satisfies AC 2: the other half-configured direction. If only hosted_zone_id
# is present without a domain_name, no record must be created.
# ---------------------------------------------------------------------------
run "half_configured_zone_set_domain_empty_zero_resources" {
  command = plan

  variables {
    domain_name                         = ""
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = length(aws_route53_record.this) == 0
    error_message = "aws_route53_record must NOT be created when domain_name is empty even if hosted_zone_id is set"
  }
}

# ---------------------------------------------------------------------------
# Test 4: prod path — both set → one aws_route53_record
#
# Satisfies AC 2/3: when both domain_name and hosted_zone_id are non-empty,
# exactly one aws_route53_record resource must be created (count = 1).
# ---------------------------------------------------------------------------
run "prod_path_one_record_created_when_both_set" {
  command = plan

  variables {
    domain_name                         = "api.knotify.app"
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = length(aws_route53_record.this) == 1
    error_message = "exactly one aws_route53_record must be created when domain_name and hosted_zone_id are both non-empty"
  }
}

# ---------------------------------------------------------------------------
# Test 5: prod path — record type is A
#
# Satisfies AC 3: the Route 53 record must be type A (the alias record type
# for CloudFront). AAAA is handled automatically by CloudFront's own IPv6
# support — only A is required here.
# ---------------------------------------------------------------------------
run "prod_path_record_type_is_a" {
  command = plan

  variables {
    domain_name                         = "api.knotify.app"
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = aws_route53_record.this[0].type == "A"
    error_message = "aws_route53_record type must be A for a CloudFront alias record"
  }
}

# ---------------------------------------------------------------------------
# Test 6: prod path — alias block targets cloudfront_distribution_domain_name
#
# Satisfies AC 3: the alias.name must equal var.cloudfront_distribution_domain_name
# so DNS for the custom domain resolves to the correct CloudFront hostname.
# ---------------------------------------------------------------------------
run "prod_path_alias_name_targets_cloudfront_hostname" {
  command = plan

  variables {
    domain_name                         = "api.knotify.app"
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = aws_route53_record.this[0].alias[0].name == "d111111abcdef8.cloudfront.net"
    error_message = "aws_route53_record alias.name must equal var.cloudfront_distribution_domain_name"
  }
}

# ---------------------------------------------------------------------------
# Test 7: prod path — alias zone_id is the CloudFront global hosted zone
#
# Satisfies AC 1/3: cloudfront_hosted_zone_id is always Z2FDTNDATAQYW2 (the
# CloudFront global hosted zone ID — same in every AWS account). The module
# must wire this into alias.zone_id, not the environment's Route 53 zone ID.
# ---------------------------------------------------------------------------
run "prod_path_alias_zone_id_is_cloudfront_global_zone" {
  command = plan

  variables {
    domain_name                         = "api.knotify.app"
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = aws_route53_record.this[0].alias[0].zone_id == "Z2FDTNDATAQYW2"
    error_message = "aws_route53_record alias.zone_id must be the CloudFront global zone Z2FDTNDATAQYW2, not the environment's hosted zone"
  }
}

# ---------------------------------------------------------------------------
# Test 8: prod path — evaluate_target_health is false
#
# Satisfies AC 3: evaluate_target_health must be false. CloudFront does not
# support Route 53 health checks on alias targets; setting true would cause
# a Terraform error at apply time.
# ---------------------------------------------------------------------------
run "prod_path_evaluate_target_health_is_false" {
  command = plan

  variables {
    domain_name                         = "api.knotify.app"
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = aws_route53_record.this[0].alias[0].evaluate_target_health == false
    error_message = "aws_route53_record alias.evaluate_target_health must be false (CloudFront does not support Route 53 health check probing)"
  }
}

# ---------------------------------------------------------------------------
# Test 9: dev path — record_fqdn output is empty string
#
# Satisfies AC 5: when no record exists (dev path, both inputs empty), the
# record_fqdn output must be an empty string, not an error or null.
# Downstream callers (e.g., CI pipelines) must tolerate the empty string.
# ---------------------------------------------------------------------------
run "dev_path_record_fqdn_output_is_empty_string" {
  command = plan

  variables {
    domain_name                         = ""
    hosted_zone_id                      = ""
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  assert {
    condition     = output.record_fqdn == ""
    error_message = "record_fqdn output must be empty string when no record is created (dev path)"
  }
}

# ---------------------------------------------------------------------------
# Test 10: prod path — record_fqdn output is non-empty
#
# Satisfies AC 5: when the record is created (prod path), record_fqdn must
# resolve to a non-empty string. The exact value is provider-computed;
# non-empty is sufficient to verify the output is wired correctly.
# ---------------------------------------------------------------------------
run "prod_path_record_fqdn_output_is_non_empty" {
  command = plan

  variables {
    domain_name                         = "api.knotify.app"
    hosted_zone_id                      = "Z123456ABCDEF"
    cloudfront_distribution_domain_name = "d111111abcdef8.cloudfront.net"
    cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
  }

  override_resource {
    target = aws_route53_record.this[0]
    values = {
      fqdn = "api.knotify.app"
    }
    override_during = plan
  }

  assert {
    condition     = output.record_fqdn != ""
    error_message = "record_fqdn output must be non-empty when the Route 53 record exists (prod path)"
  }
}
