terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

# ---------------------------------------------------------------------------
# Edge secret — random alphanumeric password
#
# Restricted to [A-Za-z0-9] via special=false so the value is always a valid
# HTTP header value (the default special-character set includes ":" which is
# the header field delimiter and would break CloudFront header injection).
# The secret rotates only when this resource is explicitly tainted:
#   terraform taint module.cloudfront.random_password.edge_secret
# ---------------------------------------------------------------------------

resource "random_password" "edge_secret" {
  length  = 64
  special = false
  upper   = true
  lower   = true
  numeric = true
}

# ---------------------------------------------------------------------------
# Data sources — AWS-managed cache and origin-request policies
#
# Managed-CachingDisabled: TTL=0 on all objects; CloudFront acts as a pure
# passthrough. Required for an API backend where every response must be fresh.
#
# Managed-AllViewer: forwards all viewer request headers, cookies, and query
# strings to the origin so the HTTP API receives the full client request.
# The deprecated `forwarded_values` block (removed in AWS provider 6.x) is
# not used; these data sources are the modern replacement.
# ---------------------------------------------------------------------------

data "aws_cloudfront_cache_policy" "managed_caching_disabled" {
  name = "Managed-CachingDisabled"
}

data "aws_cloudfront_origin_request_policy" "managed_all_viewer" {
  name = "Managed-AllViewer"
}

# ---------------------------------------------------------------------------
# Local: viewer_certificate block
#
# Branches on var.domain_name:
#   dev  (empty)    → cloudfront_default_certificate = true, no aliases
#   prod (non-empty) → ACM cert in us-east-1, SNI-only, TLSv1.2_2021
#
# The dynamic block produces exactly one viewer_certificate regardless of path.
# ---------------------------------------------------------------------------

locals {
  use_custom_domain = var.domain_name != ""

  # aliases list — empty on dev path, [var.domain_name] on prod path
  aliases = local.use_custom_domain ? [var.domain_name] : []
}

# ---------------------------------------------------------------------------
# CloudFront distribution
# ---------------------------------------------------------------------------

resource "aws_cloudfront_distribution" "this" {
  enabled         = true
  is_ipv6_enabled = true
  price_class     = var.price_class
  aliases         = local.aliases
  web_acl_id      = var.web_acl_id != "" ? var.web_acl_id : null

  # Origin — the HTTP API Gateway execute-api endpoint
  #
  # api_gateway_domain_name is the hostname-only form
  # (e.g., abc123.execute-api.eu-central-1.amazonaws.com).
  # CloudFront does not accept a scheme-prefixed URL here.
  origin {
    origin_id   = "api-gateway"
    domain_name = var.api_gateway_domain_name

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "https-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }

    # Inject the shared edge secret so the Lambda can verify that traffic
    # reached it via CloudFront (not directly via the execute-api endpoint).
    custom_header {
      name  = "x-knotify-edge-secret"
      value = random_password.edge_secret.result
    }
  }

  # Default cache behavior — no-cache passthrough
  #
  # Managed-CachingDisabled + Managed-AllViewer together make CloudFront a
  # transparent proxy: nothing is cached and all viewer headers/cookies/QS
  # are forwarded verbatim. No `forwarded_values` block (deprecated in
  # AWS provider 6.x; these managed policies are the replacement).
  default_cache_behavior {
    target_origin_id       = "api-gateway"
    viewer_protocol_policy = "redirect-to-https"

    allowed_methods = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods  = ["GET", "HEAD"]

    cache_policy_id          = data.aws_cloudfront_cache_policy.managed_caching_disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.managed_all_viewer.id
  }

  # Viewer certificate — branches on whether a custom domain is configured
  viewer_certificate {
    # dev path: use the free CloudFront default certificate
    cloudfront_default_certificate = local.use_custom_domain ? false : true

    # prod path: custom ACM cert + SNI + TLS 1.2
    acm_certificate_arn      = local.use_custom_domain ? var.acm_certificate_arn : null
    ssl_support_method       = local.use_custom_domain ? "sni-only" : null
    minimum_protocol_version = local.use_custom_domain ? "TLSv1.2_2021" : null
  }

  # No geo-blocking in this phase. Geo-restriction is a phase-11 concern.
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  tags = var.tags
}
