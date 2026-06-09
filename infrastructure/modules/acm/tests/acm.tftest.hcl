# ACM module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/acm/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 5.2).
#
# Provider alias note: the module declares configuration_aliases = [aws.us_east_1].
# mock_provider "aws" {} covers the default provider; mock_provider "aws" { alias = "us_east_1" }
# covers the alias. Each run block passes both via the providers block.
#
# For prod-path tests, override_resource on aws_acm_certificate.this[0] supplies
# deterministic domain_validation_options so for_each on aws_route53_record can
# resolve at plan time (domain_validation_options is a computed attribute — unknown
# at plan time without the override).
#
# Tests covered (acceptance criteria mapping):
#   Test 1  — dev path: domain_name="" produces zero resources (AC 2)
#   Test 2  — dev path: outputs are empty strings when no cert (AC 5)
#   Test 3  — prod path: aws_acm_certificate created with DNS validation method (AC 3)
#   Test 4  — prod path: aws_acm_certificate_validation created (AC 3)
#   Test 5  — prod path: certificate_arn output wired to cert ARN (AC 5)
#   Test 6  — prod path: certificate_validated marker output non-empty (AC 5)
#   Test 7  — prod path: subject_alternative_names flows through (AC 3)
#   Test 8  — prod path: aws_route53_record created for validation domain (AC 3)

mock_provider "aws" {}

mock_provider "aws" {
  alias = "us_east_1"
}

# ---------------------------------------------------------------------------
# Test 1: dev path — domain_name="" produces zero resources
#
# Satisfies AC 2: when var.domain_name is empty, the module creates NO AWS
# resources. This is the dev path: no cert, no Route 53 records, no validation
# wait. The apply runs cleanly with no AWS calls.
# ---------------------------------------------------------------------------
run "dev_path_zero_resources_when_domain_name_empty" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = ""
    hosted_zone_id = ""
  }

  assert {
    condition     = length(aws_acm_certificate.this) == 0
    error_message = "aws_acm_certificate must NOT be created when domain_name is empty (dev path)"
  }

  assert {
    condition     = length(aws_route53_record.cert_validation) == 0
    error_message = "aws_route53_record must NOT be created when domain_name is empty (dev path)"
  }

  assert {
    condition     = length(aws_acm_certificate_validation.this) == 0
    error_message = "aws_acm_certificate_validation must NOT be created when domain_name is empty (dev path)"
  }
}

# ---------------------------------------------------------------------------
# Test 2: dev path — outputs are empty strings
#
# Satisfies AC 5: certificate_arn and certificate_validated are both ""
# when domain_name is empty. Downstream modules must tolerate empty string.
# ---------------------------------------------------------------------------
run "dev_path_outputs_are_empty_strings" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = ""
    hosted_zone_id = ""
  }

  assert {
    condition     = output.certificate_arn == ""
    error_message = "certificate_arn output must be empty string when domain_name is empty"
  }

  assert {
    condition     = output.certificate_validated == ""
    error_message = "certificate_validated output must be empty string when domain_name is empty"
  }
}

# ---------------------------------------------------------------------------
# Test 3: prod path — aws_acm_certificate created with DNS validation method
#
# Satisfies AC 3 (part 1): when domain_name is non-empty, exactly one
# aws_acm_certificate resource is created with validation_method = "DNS"
# and the correct domain_name.
#
# override_resource supplies deterministic domain_validation_options so
# subsequent for_each on aws_route53_record can resolve keys at plan time.
# ---------------------------------------------------------------------------
run "prod_path_certificate_created_with_dns_validation" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = "api.example.com"
    hosted_zone_id = "Z123456ABCDEF"
  }

  override_resource {
    target = aws_acm_certificate.this[0]
    values = {
      arn               = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
      domain_name       = "api.example.com"
      validation_method = "DNS"
      domain_validation_options = [{
        domain_name           = "api.example.com"
        resource_record_name  = "_abc123.api.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def456.acm-validations.aws."
      }]
    }
    override_during = plan
  }

  assert {
    condition     = length(aws_acm_certificate.this) == 1
    error_message = "aws_acm_certificate must be created (count=1) when domain_name is non-empty"
  }

  assert {
    condition     = aws_acm_certificate.this[0].validation_method == "DNS"
    error_message = "aws_acm_certificate.this[0].validation_method must be DNS"
  }
}

# ---------------------------------------------------------------------------
# Test 4: prod path — aws_acm_certificate_validation created
#
# Satisfies AC 3 (part 3): one aws_acm_certificate_validation resource is
# created when domain_name is non-empty.
# ---------------------------------------------------------------------------
run "prod_path_certificate_validation_resource_created" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = "api.example.com"
    hosted_zone_id = "Z123456ABCDEF"
  }

  override_resource {
    target = aws_acm_certificate.this[0]
    values = {
      arn               = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
      domain_name       = "api.example.com"
      validation_method = "DNS"
      domain_validation_options = [{
        domain_name           = "api.example.com"
        resource_record_name  = "_abc123.api.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def456.acm-validations.aws."
      }]
    }
    override_during = plan
  }

  assert {
    condition     = length(aws_acm_certificate_validation.this) == 1
    error_message = "aws_acm_certificate_validation must be created (count=1) when domain_name is non-empty"
  }
}

# ---------------------------------------------------------------------------
# Test 5: prod path — certificate_arn output wired to cert ARN
#
# Satisfies AC 5: certificate_arn is wired to the cert ARN when the cert
# exists. override_resource supplies a deterministic ARN for assertion.
# ---------------------------------------------------------------------------
run "prod_path_certificate_arn_output_wired_to_cert" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = "api.example.com"
    hosted_zone_id = "Z123456ABCDEF"
  }

  override_resource {
    target = aws_acm_certificate.this[0]
    values = {
      arn               = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
      domain_name       = "api.example.com"
      validation_method = "DNS"
      domain_validation_options = [{
        domain_name           = "api.example.com"
        resource_record_name  = "_abc123.api.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def456.acm-validations.aws."
      }]
    }
    override_during = plan
  }

  assert {
    condition     = output.certificate_arn == "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
    error_message = "certificate_arn output must be wired to aws_acm_certificate.this[0].arn"
  }
}

# ---------------------------------------------------------------------------
# Test 6: prod path — certificate_validated marker output non-empty
#
# Satisfies AC 5: certificate_validated is non-empty when the validation
# resource exists.
# ---------------------------------------------------------------------------
run "prod_path_certificate_validated_output_non_empty" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = "api.example.com"
    hosted_zone_id = "Z123456ABCDEF"
  }

  override_resource {
    target = aws_acm_certificate.this[0]
    values = {
      arn               = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
      domain_name       = "api.example.com"
      validation_method = "DNS"
      domain_validation_options = [{
        domain_name           = "api.example.com"
        resource_record_name  = "_abc123.api.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def456.acm-validations.aws."
      }]
    }
    override_during = plan
  }

  override_resource {
    target = aws_acm_certificate_validation.this[0]
    values = {
      id = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
    }
    override_during = plan
  }

  assert {
    condition     = output.certificate_validated != ""
    error_message = "certificate_validated output must be non-empty when validation resource exists"
  }
}

# ---------------------------------------------------------------------------
# Test 7: prod path — subject_alternative_names flows through to certificate
#
# Satisfies AC 3 (SANs): when subject_alternative_names is supplied, the
# certificate is created with those SANs.
# ---------------------------------------------------------------------------
run "prod_path_subject_alternative_names_flow_through" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name               = "api.example.com"
    hosted_zone_id            = "Z123456ABCDEF"
    subject_alternative_names = ["www.example.com", "staging.example.com"]
  }

  override_resource {
    target = aws_acm_certificate.this[0]
    values = {
      arn                       = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
      domain_name               = "api.example.com"
      validation_method         = "DNS"
      subject_alternative_names = ["api.example.com", "www.example.com", "staging.example.com"]
      domain_validation_options = [
        {
          domain_name           = "api.example.com"
          resource_record_name  = "_abc123.api.example.com."
          resource_record_type  = "CNAME"
          resource_record_value = "_def456.acm-validations.aws."
        },
        {
          domain_name           = "www.example.com"
          resource_record_name  = "_abc123.www.example.com."
          resource_record_type  = "CNAME"
          resource_record_value = "_def456.acm-validations.aws."
        },
        {
          domain_name           = "staging.example.com"
          resource_record_name  = "_abc123.staging.example.com."
          resource_record_type  = "CNAME"
          resource_record_value = "_def456.acm-validations.aws."
        }
      ]
    }
    override_during = plan
  }

  assert {
    condition     = length(aws_acm_certificate.this) == 1
    error_message = "aws_acm_certificate must be created when domain_name is non-empty (with SANs)"
  }

  assert {
    condition     = contains(tolist(aws_acm_certificate.this[0].subject_alternative_names), "www.example.com")
    error_message = "certificate must include www.example.com in subject_alternative_names"
  }

  assert {
    condition     = contains(tolist(aws_acm_certificate.this[0].subject_alternative_names), "staging.example.com")
    error_message = "certificate must include staging.example.com in subject_alternative_names"
  }
}

# ---------------------------------------------------------------------------
# Test 8: prod path — aws_route53_record created for validation domain
#
# Satisfies AC 3 (part 2): Route 53 validation records are created from the
# domain_validation_options map. With one domain and no SANs, one record
# is expected.
# ---------------------------------------------------------------------------
run "prod_path_route53_validation_record_created" {
  command = plan

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  variables {
    domain_name    = "api.example.com"
    hosted_zone_id = "Z123456ABCDEF"
  }

  override_resource {
    target = aws_acm_certificate.this[0]
    values = {
      arn               = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
      domain_name       = "api.example.com"
      validation_method = "DNS"
      domain_validation_options = [{
        domain_name           = "api.example.com"
        resource_record_name  = "_abc123.api.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def456.acm-validations.aws."
      }]
    }
    override_during = plan
  }

  assert {
    condition     = length(aws_route53_record.cert_validation) == 1
    error_message = "one aws_route53_record must be created for the single validation domain"
  }

  assert {
    condition     = aws_route53_record.cert_validation["api.example.com"].zone_id == "Z123456ABCDEF"
    error_message = "aws_route53_record zone_id must equal var.hosted_zone_id"
  }

  assert {
    condition     = aws_route53_record.cert_validation["api.example.com"].type == "CNAME"
    error_message = "aws_route53_record type must be CNAME"
  }
}
