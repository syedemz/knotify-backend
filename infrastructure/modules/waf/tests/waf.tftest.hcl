# ---------------------------------------------------------------------------
# WAF module tests — story 5.4
#
# Verifies:
#   (a) scope = CLOUDFRONT
#   (b) AWSManagedRulesCommonRuleSet has override_action.count
#   (c) AWSManagedRulesKnownBadInputsRuleSet has override_action.none
#   (d) AWSManagedRulesSQLiRuleSet has override_action.count
#   (e) rate-based rule has action.block + limit=2000 + aggregate_key_type=IP
#   (f) each rule's visibility_config has both metrics and sampled_requests enabled
#   (g) ACL-level visibility_config has cloudwatch_metrics_enabled + sampled_requests_enabled
#   (h) default_action.allow at ACL level
#   (i) outputs web_acl_id and web_acl_arn resolve
#
# The mock provider eliminates real AWS calls. WAF-to-CloudFront attachment
# is performed on the CloudFront distribution (web_acl_id argument), not via
# aws_wafv2_web_acl_association — see modules/cloudfront tests.
# ---------------------------------------------------------------------------

provider "aws" {
  alias = "us_east_1"

  region                      = "us-east-1"
  access_key                  = "mock-access-key"
  secret_key                  = "mock-secret-key"
  skip_credentials_validation = true
  skip_requesting_account_id  = true
  skip_metadata_api_check     = true

  endpoints {
    wafv2 = "http://localhost:5000"
  }
}

variables {
  environment = "test"
  rate_limit  = 2000
  tags        = {}
}

# ---------------------------------------------------------------------------
# (a) WAF ACL scope is CLOUDFRONT
# ---------------------------------------------------------------------------

run "waf_acl_scope_is_cloudfront" {
  command = plan

  assert {
    condition     = aws_wafv2_web_acl.this.scope == "CLOUDFRONT"
    error_message = "Expected scope to be CLOUDFRONT, got: ${aws_wafv2_web_acl.this.scope}"
  }
}

# ---------------------------------------------------------------------------
# (b) AWSManagedRulesCommonRuleSet — override_action is count (monitor mode)
# ---------------------------------------------------------------------------

run "common_rule_set_override_action_is_count" {
  command = plan

  assert {
    condition = anytrue([
      for rule in aws_wafv2_web_acl.this.rule :
      length(rule.override_action) > 0 && length(rule.override_action[0].count) > 0
      if rule.name == "AWSManagedRulesCommonRuleSet"
    ])
    error_message = "AWSManagedRulesCommonRuleSet must have override_action.count (monitor mode)"
  }
}

# ---------------------------------------------------------------------------
# (c) AWSManagedRulesKnownBadInputsRuleSet — override_action is none (enforce)
# ---------------------------------------------------------------------------

run "known_bad_inputs_rule_set_override_action_is_none" {
  command = plan

  assert {
    condition = anytrue([
      for rule in aws_wafv2_web_acl.this.rule :
      length(rule.override_action) > 0 && length(rule.override_action[0].none) > 0
      if rule.name == "AWSManagedRulesKnownBadInputsRuleSet"
    ])
    error_message = "AWSManagedRulesKnownBadInputsRuleSet must have override_action.none (enforce from day one)"
  }
}

# ---------------------------------------------------------------------------
# (d) AWSManagedRulesSQLiRuleSet — override_action is count (observe before enforce)
# ---------------------------------------------------------------------------

run "sqli_rule_set_override_action_is_count" {
  command = plan

  assert {
    condition = anytrue([
      for rule in aws_wafv2_web_acl.this.rule :
      length(rule.override_action) > 0 && length(rule.override_action[0].count) > 0
      if rule.name == "AWSManagedRulesSQLiRuleSet"
    ])
    error_message = "AWSManagedRulesSQLiRuleSet must have override_action.count (observe before enforcing)"
  }
}

# ---------------------------------------------------------------------------
# (e) Rate-based rule — action=block, limit=2000, aggregate_key_type=IP
# ---------------------------------------------------------------------------

run "rate_based_rule_blocks_at_2000_per_ip" {
  command = plan

  assert {
    condition = anytrue([
      for rule in aws_wafv2_web_acl.this.rule :
      length(rule.action) > 0 &&
      length(rule.action[0].block) > 0 &&
      length(rule.statement) > 0 &&
      length(rule.statement[0].rate_based_statement) > 0 &&
      rule.statement[0].rate_based_statement[0].limit == 2000 &&
      rule.statement[0].rate_based_statement[0].aggregate_key_type == "IP"
      if rule.name == "RateBasedPerIP"
    ])
    error_message = "Rate-based rule must have action=block, limit=2000, aggregate_key_type=IP"
  }
}

# ---------------------------------------------------------------------------
# (f) Each rule's visibility_config has both flags enabled
# ---------------------------------------------------------------------------

run "each_rule_visibility_config_both_flags_enabled" {
  command = plan

  assert {
    condition = alltrue([
      for rule in aws_wafv2_web_acl.this.rule :
      length(rule.visibility_config) > 0 &&
      rule.visibility_config[0].cloudwatch_metrics_enabled == true &&
      rule.visibility_config[0].sampled_requests_enabled == true
    ])
    error_message = "Every rule must have visibility_config with cloudwatch_metrics_enabled=true and sampled_requests_enabled=true"
  }
}

# ---------------------------------------------------------------------------
# (g) ACL-level visibility_config has both flags enabled
# ---------------------------------------------------------------------------

run "acl_level_visibility_config_both_flags_enabled" {
  command = plan

  assert {
    condition = (
      length(aws_wafv2_web_acl.this.visibility_config) > 0 &&
      aws_wafv2_web_acl.this.visibility_config[0].cloudwatch_metrics_enabled == true &&
      aws_wafv2_web_acl.this.visibility_config[0].sampled_requests_enabled == true
    )
    error_message = "ACL-level visibility_config must have cloudwatch_metrics_enabled=true and sampled_requests_enabled=true"
  }
}

# ---------------------------------------------------------------------------
# (h) default_action is allow at the ACL level
# ---------------------------------------------------------------------------

run "acl_default_action_is_allow" {
  command = plan

  assert {
    condition = (
      length(aws_wafv2_web_acl.this.default_action) > 0 &&
      length(aws_wafv2_web_acl.this.default_action[0].allow) > 0
    )
    error_message = "ACL default_action must be allow"
  }
}

# ---------------------------------------------------------------------------
# (i) Outputs resolve: web_acl_id and web_acl_arn surface the correct resource
#     attributes.
#
# aws_wafv2_web_acl.this.id and .arn are computed (unknown at plan time) so a
# direct != null check fails with "Unknown condition value" in plan-only runs.
# The override_resource block supplies mock computed values so both outputs
# resolve to known strings during the plan phase.
# ---------------------------------------------------------------------------

run "outputs_resolve" {
  command = plan

  override_resource {
    target          = aws_wafv2_web_acl.this
    override_during = plan
    values = {
      id  = "mock-waf-acl-id"
      arn = "arn:aws:wafv2:us-east-1:123456789012:global/webacl/knotify-test-edge-waf/mock-waf-acl-id"
    }
  }

  assert {
    condition     = output.web_acl_id == "mock-waf-acl-id"
    error_message = "Output web_acl_id must surface aws_wafv2_web_acl.this.id"
  }

  assert {
    condition     = output.web_acl_arn == "arn:aws:wafv2:us-east-1:123456789012:global/webacl/knotify-test-edge-waf/mock-waf-acl-id"
    error_message = "Output web_acl_arn must surface aws_wafv2_web_acl.this.arn"
  }
}
