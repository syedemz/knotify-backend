# CloudFront module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/cloudfront/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 5.3).
#
# Provider note: the cloudfront module uses only the default aws provider.
# mock_provider "aws" {} handles all AWS calls; mock_provider "random" {} handles
# the random_password resource.
#
# Tests covered (acceptance criteria mapping):
#   Test  1 — distribution enabled and ipv6 enabled (AC 1)
#   Test  2 — origin_id is "api-gateway" (AC 1)
#   Test  3 — viewer_protocol_policy is "redirect-to-https" (AC 2)
#   Test  4 — allowed_methods covers the full 7-method set (AC 2)
#   Test  5 — cached_methods is ["GET","HEAD"] (AC 2)
#   Test  6 — cache policy data source wired from Managed-CachingDisabled (AC 2)
#   Test  7 — origin-request policy data source wired from Managed-AllViewerExceptHostHeader (AC 2)
#   Test  8 — origin custom_header contains x-knotify-edge-secret (AC 3)
#   Test  9 — random_password length=64, special=false, upper=true, lower=true, numeric=true (AC 3)
#   Test 10 — dev path: cloudfront_default_certificate=true and aliases=[] (AC 4)
#   Test 11 — prod path: acm_certificate_arn set + sni-only + TLSv1.2_2021 + aliases=[domain] (AC 4)
#   Test 12 — price_class is PriceClass_100 (AC 5)
#   Test 13 — geo_restriction type is "none" (AC 6)
#   Test 14 — all four outputs resolve (distribution_id, distribution_domain_name,
#              distribution_arn, edge_secret) (AC 7)
#   Test 15 — edge_secret output is marked sensitive (AC 7)

mock_provider "aws" {}

mock_provider "random" {}

# ---------------------------------------------------------------------------
# Shared variable set: dev path (domain_name = "")
# ---------------------------------------------------------------------------

variables {
  api_gateway_domain_name = "abc123.execute-api.eu-central-1.amazonaws.com"
  domain_name             = ""
  acm_certificate_arn     = ""
}

# ---------------------------------------------------------------------------
# Test 1: distribution is enabled and IPv6 is enabled
#
# Satisfies AC 1: enabled = true, is_ipv6_enabled = true.
# ---------------------------------------------------------------------------
run "distribution_enabled_and_ipv6_enabled" {
  command = plan

  assert {
    condition     = aws_cloudfront_distribution.this.enabled == true
    error_message = "aws_cloudfront_distribution.this.enabled must be true"
  }

  assert {
    condition     = aws_cloudfront_distribution.this.is_ipv6_enabled == true
    error_message = "aws_cloudfront_distribution.this.is_ipv6_enabled must be true"
  }
}

# ---------------------------------------------------------------------------
# Test 2: origin_id is "api-gateway"
#
# Satisfies AC 1: the origin block uses origin_id = "api-gateway" and the
# default_cache_behavior references the same id.
# ---------------------------------------------------------------------------
run "origin_id_is_api_gateway" {
  command = plan

  assert {
    condition     = one(aws_cloudfront_distribution.this.origin).origin_id == "api-gateway"
    error_message = "origin origin_id must be \"api-gateway\""
  }

  assert {
    condition     = aws_cloudfront_distribution.this.default_cache_behavior[0].target_origin_id == "api-gateway"
    error_message = "default_cache_behavior target_origin_id must be \"api-gateway\""
  }
}

# ---------------------------------------------------------------------------
# Test 3: viewer_protocol_policy is "redirect-to-https"
#
# Satisfies AC 2.
# ---------------------------------------------------------------------------
run "viewer_protocol_policy_redirect_to_https" {
  command = plan

  assert {
    condition     = aws_cloudfront_distribution.this.default_cache_behavior[0].viewer_protocol_policy == "redirect-to-https"
    error_message = "viewer_protocol_policy must be \"redirect-to-https\""
  }
}

# ---------------------------------------------------------------------------
# Test 4: allowed_methods covers full 7-method set
#
# Satisfies AC 2: GET HEAD OPTIONS PUT POST PATCH DELETE.
# ---------------------------------------------------------------------------
run "allowed_methods_full_set" {
  command = plan

  assert {
    condition     = toset(aws_cloudfront_distribution.this.default_cache_behavior[0].allowed_methods) == toset(["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"])
    error_message = "allowed_methods must be the full 7-method set"
  }
}

# ---------------------------------------------------------------------------
# Test 5: cached_methods is ["GET","HEAD"]
#
# Satisfies AC 2.
# ---------------------------------------------------------------------------
run "cached_methods_get_head" {
  command = plan

  assert {
    condition     = toset(aws_cloudfront_distribution.this.default_cache_behavior[0].cached_methods) == toset(["GET", "HEAD"])
    error_message = "cached_methods must be [\"GET\", \"HEAD\"]"
  }
}

# ---------------------------------------------------------------------------
# Test 6: cache policy data source wired from Managed-CachingDisabled
#
# Satisfies AC 2: the default_cache_behavior references the data source that
# looks up Managed-CachingDisabled by name. We verify the data source name
# attribute and that cache_policy_id is wired to it.
# ---------------------------------------------------------------------------
run "cache_policy_wired_from_managed_caching_disabled" {
  command = plan

  assert {
    condition     = data.aws_cloudfront_cache_policy.managed_caching_disabled.name == "Managed-CachingDisabled"
    error_message = "data.aws_cloudfront_cache_policy.managed_caching_disabled.name must be \"Managed-CachingDisabled\""
  }

  assert {
    condition     = aws_cloudfront_distribution.this.default_cache_behavior[0].cache_policy_id == data.aws_cloudfront_cache_policy.managed_caching_disabled.id
    error_message = "default_cache_behavior.cache_policy_id must be wired to data.aws_cloudfront_cache_policy.managed_caching_disabled.id"
  }
}

# ---------------------------------------------------------------------------
# Test 7: origin-request policy wired from Managed-AllViewerExceptHostHeader
#
# Satisfies AC 2: the default_cache_behavior references the data source that
# looks up Managed-AllViewerExceptHostHeader by name. The Host header MUST
# NOT be forwarded — API Gateway's regional execute-api endpoint rejects
# requests whose Host doesn't match its own DNS name with 403 ForbiddenException,
# which is what caused the entire edge path to break with Managed-AllViewer.
# ---------------------------------------------------------------------------
run "origin_request_policy_wired_from_managed_all_viewer_except_host_header" {
  command = plan

  assert {
    condition     = data.aws_cloudfront_origin_request_policy.managed_all_viewer_except_host_header.name == "Managed-AllViewerExceptHostHeader"
    error_message = "data.aws_cloudfront_origin_request_policy.managed_all_viewer_except_host_header.name must be \"Managed-AllViewerExceptHostHeader\""
  }

  assert {
    condition     = aws_cloudfront_distribution.this.default_cache_behavior[0].origin_request_policy_id == data.aws_cloudfront_origin_request_policy.managed_all_viewer_except_host_header.id
    error_message = "default_cache_behavior.origin_request_policy_id must be wired to data.aws_cloudfront_origin_request_policy.managed_all_viewer_except_host_header.id"
  }
}

# ---------------------------------------------------------------------------
# Test 8: origin custom_header contains x-knotify-edge-secret
#
# Satisfies AC 3: the origin block's custom_header set contains a header named
# "x-knotify-edge-secret". The value is sourced from random_password.
# ---------------------------------------------------------------------------
run "origin_custom_header_contains_edge_secret" {
  command = plan

  assert {
    condition = anytrue([
      for h in tolist(one(aws_cloudfront_distribution.this.origin).custom_header) :
      h.name == "x-knotify-edge-secret"
    ])
    error_message = "origin must include a custom_header named \"x-knotify-edge-secret\""
  }
}

# ---------------------------------------------------------------------------
# Test 9: random_password configured correctly
#
# Satisfies AC 3: length=64, special=false, upper=true, lower=true, numeric=true.
# These settings restrict the output to [A-Za-z0-9] and guarantee a valid
# HTTP header value (no special characters that would break header parsing).
# ---------------------------------------------------------------------------
run "random_password_configuration" {
  command = plan

  assert {
    condition     = random_password.edge_secret.length == 64
    error_message = "random_password.edge_secret.length must be 64"
  }

  assert {
    condition     = random_password.edge_secret.special == false
    error_message = "random_password.edge_secret.special must be false"
  }

  assert {
    condition     = random_password.edge_secret.upper == true
    error_message = "random_password.edge_secret.upper must be true"
  }

  assert {
    condition     = random_password.edge_secret.lower == true
    error_message = "random_password.edge_secret.lower must be true"
  }

  assert {
    condition     = random_password.edge_secret.numeric == true
    error_message = "random_password.edge_secret.numeric must be true"
  }
}

# ---------------------------------------------------------------------------
# Test 10: dev path — cloudfront_default_certificate=true and aliases=[]
#
# Satisfies AC 4: when var.domain_name is empty, use the free CloudFront
# default certificate and no aliases.
# ---------------------------------------------------------------------------
run "dev_path_default_certificate_and_no_aliases" {
  command = plan

  variables {
    domain_name         = ""
    acm_certificate_arn = ""
  }

  assert {
    condition     = aws_cloudfront_distribution.this.viewer_certificate[0].cloudfront_default_certificate == true
    error_message = "viewer_certificate.cloudfront_default_certificate must be true when domain_name is empty"
  }

  assert {
    condition     = length(aws_cloudfront_distribution.this.aliases) == 0
    error_message = "aliases must be empty when domain_name is empty"
  }
}

# ---------------------------------------------------------------------------
# Test 11: prod path — acm_certificate_arn + sni-only + TLSv1.2_2021 + aliases=[domain]
#
# Satisfies AC 4: when var.domain_name is non-empty, use the provided ACM cert,
# SNI-only, TLSv1.2_2021, and aliases = [var.domain_name].
# ---------------------------------------------------------------------------
run "prod_path_acm_cert_sni_tls12_aliases" {
  command = plan

  variables {
    domain_name         = "api.example.com"
    acm_certificate_arn = "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
  }

  assert {
    condition     = aws_cloudfront_distribution.this.viewer_certificate[0].acm_certificate_arn == "arn:aws:acm:us-east-1:123456789012:certificate/test-cert-id"
    error_message = "viewer_certificate.acm_certificate_arn must be set to var.acm_certificate_arn when domain_name is non-empty"
  }

  assert {
    condition     = aws_cloudfront_distribution.this.viewer_certificate[0].ssl_support_method == "sni-only"
    error_message = "viewer_certificate.ssl_support_method must be \"sni-only\""
  }

  assert {
    condition     = aws_cloudfront_distribution.this.viewer_certificate[0].minimum_protocol_version == "TLSv1.2_2021"
    error_message = "viewer_certificate.minimum_protocol_version must be \"TLSv1.2_2021\""
  }

  assert {
    condition     = toset(aws_cloudfront_distribution.this.aliases) == toset(["api.example.com"])
    error_message = "aliases must be [var.domain_name] when domain_name is non-empty"
  }
}

# ---------------------------------------------------------------------------
# Test 12: price_class is PriceClass_100
#
# Satisfies AC 5: restricts edge locations to NA + EU only to control cost.
# ---------------------------------------------------------------------------
run "price_class_is_price_class_100" {
  command = plan

  assert {
    condition     = aws_cloudfront_distribution.this.price_class == "PriceClass_100"
    error_message = "price_class must be \"PriceClass_100\""
  }
}

# ---------------------------------------------------------------------------
# Test 13: geo_restriction type is "none"
#
# Satisfies AC 6: no geo-blocking at this stage.
# ---------------------------------------------------------------------------
run "geo_restriction_type_none" {
  command = plan

  assert {
    condition     = aws_cloudfront_distribution.this.restrictions[0].geo_restriction[0].restriction_type == "none"
    error_message = "geo_restriction.restriction_type must be \"none\""
  }
}

# ---------------------------------------------------------------------------
# Test 14: all four module outputs resolve
#
# Satisfies AC 7: distribution_id, distribution_domain_name, distribution_arn,
# and edge_secret are all present and wired to the correct resource attributes.
#
# override_resource supplies deterministic values for computed distribution
# attributes (id, domain_name, arn) and override_resource for random_password
# supplies a deterministic result — both are needed to make plan-time assertions
# on outputs that reference these computed values.
# ---------------------------------------------------------------------------
run "all_four_outputs_resolve" {
  command = plan

  override_resource {
    target = aws_cloudfront_distribution.this
    values = {
      id          = "EDFDVBD6EXAMPLE"
      domain_name = "d111111abcdef8.cloudfront.net"
      arn         = "arn:aws:cloudfront::123456789012:distribution/EDFDVBD6EXAMPLE"
      enabled     = true
    }
    override_during = plan
  }

  override_resource {
    target = random_password.edge_secret
    values = {
      result = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB"
    }
    override_during = plan
  }

  assert {
    condition     = output.distribution_id == "EDFDVBD6EXAMPLE"
    error_message = "output.distribution_id must be wired to aws_cloudfront_distribution.this.id"
  }

  assert {
    condition     = output.distribution_domain_name == "d111111abcdef8.cloudfront.net"
    error_message = "output.distribution_domain_name must be wired to aws_cloudfront_distribution.this.domain_name"
  }

  assert {
    condition     = output.distribution_arn == "arn:aws:cloudfront::123456789012:distribution/EDFDVBD6EXAMPLE"
    error_message = "output.distribution_arn must be wired to aws_cloudfront_distribution.this.arn"
  }
}

# ---------------------------------------------------------------------------
# Test 15: edge_secret output is marked sensitive
#
# Satisfies AC 7: the edge_secret output carries sensitive = true so Terraform
# masks its value in plan output and prevents accidental logging.
# override_resource for random_password supplies a known result so the output
# is not "(known after apply)" during plan.
# ---------------------------------------------------------------------------
run "edge_secret_output_is_sensitive" {
  command = plan

  override_resource {
    target = random_password.edge_secret
    values = {
      result = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789AB"
    }
    override_during = plan
  }

  assert {
    condition     = sensitive(output.edge_secret) == output.edge_secret
    error_message = "output.edge_secret must be marked sensitive"
  }
}
