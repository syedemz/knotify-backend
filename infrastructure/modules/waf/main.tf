terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"

      # CLOUDFRONT-scoped WAF resources must reside in us-east-1 regardless of
      # the environment's primary region. Callers pass the alias via
      # `providers = { aws.us_east_1 = aws.us_east_1 }`.
      configuration_aliases = [aws.us_east_1]
    }
  }
}

locals {
  # Use the caller-supplied name when provided; fall back to the convention.
  acl_name = var.name != "" ? var.name : "knotify-${var.environment}-edge-waf"
}

# ---------------------------------------------------------------------------
# WAF Web ACL — CLOUDFRONT scope
#
# Must reside in us-east-1 (CloudFront control plane is global/us-east-1).
# All four rule groups are evaluated in priority order; the first matching
# rule whose action is "block" terminates evaluation. Rules in "count" mode
# never block — they record a metric and let the request continue.
#
# Rule posture (brainstorm M3):
#   Priority 1  — AWSManagedRulesCommonRuleSet   : count  (false-positives on JSON)
#   Priority 2  — AWSManagedRulesKnownBadInputsRuleSet : none (enforce; low FP rate)
#   Priority 3  — AWSManagedRulesSQLiRuleSet       : count  (observe before enforce)
#   Priority 10 — Rate-based per-IP               : block  (2000 req/5 min)
#
# Phase 11 hardening will review CloudWatch sampled-request data and flip
# Common + SQLi from count to none once false-positive baselines are known.
# ---------------------------------------------------------------------------

resource "aws_wafv2_web_acl" "this" {
  provider = aws.us_east_1

  name  = local.acl_name
  scope = "CLOUDFRONT"

  # Allow all traffic not matched by a blocking rule.
  default_action {
    allow {}
  }

  # -------------------------------------------------------------------------
  # Rule 1: AWSManagedRulesCommonRuleSet — count mode
  #
  # Ships in count mode because the CRS contains body-inspection rules that
  # false-positive on legitimate JSON payloads (signup, profile patches in
  # phase 6, photo uploads in phase 12). CloudWatch sampled-request data
  # collected here will be reviewed in phase 11 before flipping to none.
  # -------------------------------------------------------------------------

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 1

    override_action {
      count {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.acl_name}-common"
      sampled_requests_enabled   = true
    }
  }

  # -------------------------------------------------------------------------
  # Rule 2: AWSManagedRulesKnownBadInputsRuleSet — enforce from day one
  #
  # Low false-positive rate. Blocks known-malicious input patterns (log4j
  # exploits, SSRF probes, path traversal). Safe to enforce on day one.
  # -------------------------------------------------------------------------

  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.acl_name}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  # -------------------------------------------------------------------------
  # Rule 3: AWSManagedRulesSQLiRuleSet — count mode
  #
  # Ships in count mode to observe traffic before enforcing. The Knotify app
  # sends parameterized SQL only via the Aurora writer Lambda (never raw SQL
  # from clients), but the rule can still flag URL-encoded query-string values
  # that contain SQL keywords. Review sampled data in phase 11.
  # -------------------------------------------------------------------------

  rule {
    name     = "AWSManagedRulesSQLiRuleSet"
    priority = 3

    override_action {
      count {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesSQLiRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.acl_name}-sqli"
      sampled_requests_enabled   = true
    }
  }

  # -------------------------------------------------------------------------
  # Rule 10: Rate-based per-IP — block
  #
  # A single IP that exceeds 2000 requests in any rolling 5-minute window is
  # blocked. 2000/5 min is a generous ceiling for a pre-launch app; tighten
  # in phase 11 once traffic baselines are established.
  # -------------------------------------------------------------------------

  rule {
    name     = "RateBasedPerIP"
    priority = 10

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.acl_name}-rate-based"
      sampled_requests_enabled   = true
    }
  }

  # ACL-level visibility config — required by the provider even when every
  # rule has its own visibility_config.
  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.acl_name
    sampled_requests_enabled   = true
  }

  tags = var.tags
}

# ---------------------------------------------------------------------------
# WAF Web ACL Association — attach to CloudFront distribution
#
# For CLOUDFRONT-scoped ACLs the association resource must also use the
# us-east-1 provider alias. The resource_arn is the CloudFront distribution
# ARN produced by module.cloudfront.distribution_arn (story 5.3).
# ---------------------------------------------------------------------------

resource "aws_wafv2_web_acl_association" "cloudfront" {
  provider = aws.us_east_1

  resource_arn = var.cloudfront_distribution_arn
  web_acl_arn  = aws_wafv2_web_acl.this.arn
}
