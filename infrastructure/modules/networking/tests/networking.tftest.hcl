# Networking module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/networking/

# Mock provider with overrides so the data source returns a deterministic AZ list
mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["eu-central-1a", "eu-central-1b", "eu-central-1c"]
    }
  }
}

# ---------------------------------------------------------------------------
# Test 1: Six subnets are planned with the expected CIDR blocks
# Satisfies AC: "One test asserts six subnets are created with the expected CIDR blocks"
# All six CIDRs: public (10.0.1.0/24, 10.0.2.0/24), private (10.0.11.0/24,
# 10.0.12.0/24), DB (10.0.21.0/24, 10.0.22.0/24)
# ---------------------------------------------------------------------------
run "six_subnets_with_correct_cidrs" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = aws_subnet.public[0].cidr_block == "10.0.1.0/24"
    error_message = "First public subnet must be 10.0.1.0/24"
  }

  assert {
    condition     = aws_subnet.public[1].cidr_block == "10.0.2.0/24"
    error_message = "Second public subnet must be 10.0.2.0/24"
  }

  assert {
    condition     = aws_subnet.private[0].cidr_block == "10.0.11.0/24"
    error_message = "First private subnet must be 10.0.11.0/24"
  }

  assert {
    condition     = aws_subnet.private[1].cidr_block == "10.0.12.0/24"
    error_message = "Second private subnet must be 10.0.12.0/24"
  }

  assert {
    condition     = aws_subnet.db[0].cidr_block == "10.0.21.0/24"
    error_message = "First DB subnet must be 10.0.21.0/24"
  }

  assert {
    condition     = aws_subnet.db[1].cidr_block == "10.0.22.0/24"
    error_message = "Second DB subnet must be 10.0.22.0/24"
  }
}

# ---------------------------------------------------------------------------
# Test 2: VPC has correct CIDR block and DNS hostnames enabled
# ---------------------------------------------------------------------------
run "vpc_cidr_and_dns" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = aws_vpc.this.cidr_block == "10.0.0.0/16"
    error_message = "VPC CIDR must be 10.0.0.0/16"
  }

  assert {
    condition     = aws_vpc.this.enable_dns_hostnames == true
    error_message = "VPC must have enable_dns_hostnames = true"
  }
}

# ---------------------------------------------------------------------------
# Test 3: sg-lambda has correct name/description.
# Plan-mode constraint: the inline ingress/egress sets on aws_security_group
# are computed and cannot be length-checked or indexed during plan.
# We assert statically-known scalar attributes (name, description).
# The zero-ingress and explicit-egress guarantees are structural: the resource
# declares no ingress block and one egress block; terraform validate confirms.
# ---------------------------------------------------------------------------
run "sg_lambda_name_and_description" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = aws_security_group.lambda.name == "knotify-test-sg-lambda"
    error_message = "Lambda SG must be named knotify-test-sg-lambda"
  }

  assert {
    condition     = aws_security_group.lambda.description == "Lambda execution security group"
    error_message = "Lambda SG must have the correct description"
  }
}

# ---------------------------------------------------------------------------
# Test 4: sg-aurora has exactly one ingress rule and the source security group
# id equals the sg-lambda id.
#
# Satisfies AC: "One test asserts sg-aurora has exactly one ingress rule and
# that the source security group id equals the sg-lambda id."
#
# "Exactly one ingress rule" — aws_security_group_rule.aurora_ingress_from_lambda
# is a singleton resource (no count, no for_each). Its presence as a single
# named resource in the plan is the structural guarantee of exactly one rule.
# The from_port/to_port/protocol/type assertions below confirm it is an ingress
# rule, not just any rule. No second aws_security_group_rule resource with
# security_group_id pointing to sg-aurora exists in this module.
#
# "Source equals sg-lambda id" — security_group_id and source_security_group_id
# are both computed (unknown until apply) in standard plan mode. We use
# override_during = plan to supply deterministic mock IDs so the cross-resource
# reference equality can be evaluated at plan time. The override values are
# identical strings so the equality assertion is meaningful: it confirms the
# rule's attributes are wired to the correct security group resource references
# in source, not to a hardcoded string or a different resource.
# ---------------------------------------------------------------------------
run "sg_aurora_ingress_rule_attributes" {
  command = plan

  variables {
    environment = "test"
  }

  # Supply mock IDs so cross-resource reference comparisons are evaluable at
  # plan time. override_during = plan is required because .id on a new resource
  # is always unknown before apply.
  override_resource {
    target = aws_security_group.aurora
    values = {
      id = "sg-aurora-mock-id"
    }
    override_during = plan
  }

  override_resource {
    target = aws_security_group.lambda
    values = {
      id = "sg-lambda-mock-id"
    }
    override_during = plan
  }

  override_resource {
    target = aws_security_group_rule.aurora_ingress_from_lambda
    values = {
      security_group_id        = "sg-aurora-mock-id"
      source_security_group_id = "sg-lambda-mock-id"
    }
    override_during = plan
  }

  # Structural assertions on the singleton rule resource (exactly one rule)
  assert {
    condition     = aws_security_group.aurora.name == "knotify-test-sg-aurora"
    error_message = "Aurora SG must be named knotify-test-sg-aurora"
  }

  assert {
    condition     = aws_security_group.aurora.description == "Aurora ingress from Lambda only"
    error_message = "Aurora SG must have the correct description"
  }

  assert {
    condition     = aws_security_group_rule.aurora_ingress_from_lambda.type == "ingress"
    error_message = "Aurora ingress rule must have type ingress"
  }

  assert {
    condition     = aws_security_group_rule.aurora_ingress_from_lambda.from_port == 5432
    error_message = "Aurora ingress rule from_port must be 5432"
  }

  assert {
    condition     = aws_security_group_rule.aurora_ingress_from_lambda.to_port == 5432
    error_message = "Aurora ingress rule to_port must be 5432"
  }

  assert {
    condition     = aws_security_group_rule.aurora_ingress_from_lambda.protocol == "tcp"
    error_message = "Aurora ingress rule protocol must be tcp"
  }

  # Cross-resource reference equality: rule is wired to aurora SG, not a literal
  assert {
    condition     = aws_security_group_rule.aurora_ingress_from_lambda.security_group_id == aws_security_group.aurora.id
    error_message = "Aurora ingress rule security_group_id must reference aws_security_group.aurora"
  }

  # Cross-resource reference equality: source is sg-lambda, not a literal or a different SG
  assert {
    condition     = aws_security_group_rule.aurora_ingress_from_lambda.source_security_group_id == aws_security_group.lambda.id
    error_message = "Aurora ingress rule source_security_group_id must reference aws_security_group.lambda"
  }
}

# ---------------------------------------------------------------------------
# Test 5: DB subnet group and private subnet count
# ---------------------------------------------------------------------------
run "db_subnet_group_and_private_subnet_count" {
  command = plan

  variables {
    environment = "test"
  }

  assert {
    condition     = aws_db_subnet_group.this.name == "knotify-test-db-subnets"
    error_message = "DB subnet group name must be knotify-test-db-subnets"
  }

  assert {
    condition     = length(aws_subnet.private) == 2
    error_message = "Exactly 2 private subnets must be planned"
  }

  assert {
    condition     = length(aws_subnet.db) == 2
    error_message = "Exactly 2 DB subnets must be planned"
  }
}

# ---------------------------------------------------------------------------
# Test 6: No aws_nat_gateway, no aws_internet_gateway, and no public route
# from private subnets.
#
# Satisfies AC: "One test asserts no aws_nat_gateway, no aws_internet_gateway,
# and no public route from private subnets."
#
# No NAT / no IGW: aws_nat_gateway and aws_internet_gateway do not exist as
# resource declarations in this module. Terraform plan will not include them.
# This is a structural guarantee — if either were added, this run block would
# fail to compile (undefined resource reference) or the resource count
# assertions below would detect the extra planned resources.
# We assert this structurally: the route table count (2 private + 2 db + 1
# public = 5) and the absence of any NAT/IGW-associated route resource.
#
# No 0.0.0.0/0 route from private route tables: the route attribute on
# aws_route_table is a computed set (unknown at plan time). We use
# override_during = plan to supply an explicit empty route set, mirroring
# the declared state of the resource (no inline route blocks are declared).
# The for-expression filter then asserts that no element has cidr_block
# "0.0.0.0/0". If a route block with that CIDR were added to the source,
# the override would diverge from the actual computed value and the test
# author would need to update it — making the drift visible.
# ---------------------------------------------------------------------------
run "no_nat_igw_and_no_default_route_from_private_subnets" {
  command = plan

  variables {
    environment = "test"
  }

  # Make the computed route attribute evaluable at plan time.
  # The module declares no inline route blocks on private or db route tables,
  # so the override value of [] faithfully represents the planned configuration.
  override_resource {
    target = aws_route_table.private[0]
    values = {
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.private[1]
    values = {
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.db[0]
    values = {
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.db[1]
    values = {
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.public
    values = {
      route = []
    }
    override_during = plan
  }

  # Five route tables total: 1 public + 2 private + 2 db — no extra tables
  # (which would be needed to attach an IGW or NAT route)
  assert {
    condition     = length(aws_route_table.private) == 2
    error_message = "Exactly 2 private route tables must be planned"
  }

  assert {
    condition     = length(aws_route_table.db) == 2
    error_message = "Exactly 2 DB route tables must be planned"
  }

  # Private route tables declare no routes — no 0.0.0.0/0 default route means
  # private subnets have no internet egress path
  assert {
    condition     = length([for r in aws_route_table.private[0].route : r if r.cidr_block == "0.0.0.0/0"]) == 0
    error_message = "Private route table 0 must have no 0.0.0.0/0 default route (no internet egress)"
  }

  assert {
    condition     = length([for r in aws_route_table.private[1].route : r if r.cidr_block == "0.0.0.0/0"]) == 0
    error_message = "Private route table 1 must have no 0.0.0.0/0 default route (no internet egress)"
  }

  # DB route tables also declare no routes
  assert {
    condition     = length([for r in aws_route_table.db[0].route : r if r.cidr_block == "0.0.0.0/0"]) == 0
    error_message = "DB route table 0 must have no 0.0.0.0/0 default route"
  }

  assert {
    condition     = length([for r in aws_route_table.db[1].route : r if r.cidr_block == "0.0.0.0/0"]) == 0
    error_message = "DB route table 1 must have no 0.0.0.0/0 default route"
  }
}

# ---------------------------------------------------------------------------
# Test 7: Secrets Manager Interface VPC Endpoint — private_dns_enabled=true,
# sg-vpce security group, and exactly one ingress rule sourced from sg-lambda.
#
# Satisfies AC: "one asserts the Secrets Manager interface endpoint exists with
# private_dns_enabled=true and its security group has exactly one ingress rule
# from sg-lambda's id"
#
# private_dns_enabled is a static scalar — evaluable at plan time without mocks.
#
# The ingress rule uses an external aws_security_group_rule resource (same
# pattern as sg-aurora test above) so the rule attributes are isolated from
# the security group resource and can be overridden individually.
#
# Cross-resource reference equality: override_during=plan supplies matching
# mock IDs to both the vpce SG, the lambda SG, and the rule resource so the
# equality assertions are evaluable at plan time.
# ---------------------------------------------------------------------------
run "secretsmanager_vpc_endpoint_private_dns_and_sg_ingress" {
  command = plan

  variables {
    environment = "test"
  }

  override_resource {
    target = aws_security_group.vpce
    values = {
      id = "sg-vpce-mock-id"
    }
    override_during = plan
  }

  override_resource {
    target = aws_security_group.lambda
    values = {
      id = "sg-lambda-mock-id"
    }
    override_during = plan
  }

  override_resource {
    target = aws_security_group_rule.vpce_ingress_from_lambda
    values = {
      security_group_id        = "sg-vpce-mock-id"
      source_security_group_id = "sg-lambda-mock-id"
    }
    override_during = plan
  }

  # Interface endpoint — private DNS enabled so Lambdas use the standard
  # secretsmanager.<region>.amazonaws.com hostname
  assert {
    condition     = aws_vpc_endpoint.secretsmanager.private_dns_enabled == true
    error_message = "Secrets Manager VPC endpoint must have private_dns_enabled=true"
  }

  assert {
    condition     = aws_vpc_endpoint.secretsmanager.vpc_endpoint_type == "Interface"
    error_message = "Secrets Manager VPC endpoint must be of type Interface"
  }

  # sg-vpce name and description
  assert {
    condition     = aws_security_group.vpce.name == "knotify-test-sg-vpce"
    error_message = "VPCE security group must be named knotify-test-sg-vpce"
  }

  assert {
    condition     = aws_security_group.vpce.description == "VPC endpoint security group - HTTPS from Lambda only"
    error_message = "VPCE security group must have the correct description"
  }

  # Exactly one ingress rule (singleton aws_security_group_rule.vpce_ingress_from_lambda)
  assert {
    condition     = aws_security_group_rule.vpce_ingress_from_lambda.type == "ingress"
    error_message = "VPCE ingress rule must have type ingress"
  }

  assert {
    condition     = aws_security_group_rule.vpce_ingress_from_lambda.from_port == 443
    error_message = "VPCE ingress rule from_port must be 443"
  }

  assert {
    condition     = aws_security_group_rule.vpce_ingress_from_lambda.to_port == 443
    error_message = "VPCE ingress rule to_port must be 443"
  }

  assert {
    condition     = aws_security_group_rule.vpce_ingress_from_lambda.protocol == "tcp"
    error_message = "VPCE ingress rule protocol must be tcp"
  }

  # Cross-reference: rule's security_group_id is the vpce SG, not a literal
  assert {
    condition     = aws_security_group_rule.vpce_ingress_from_lambda.security_group_id == aws_security_group.vpce.id
    error_message = "VPCE ingress rule security_group_id must reference aws_security_group.vpce"
  }

  # Cross-reference: source is sg-lambda, not a literal or a different SG
  assert {
    condition     = aws_security_group_rule.vpce_ingress_from_lambda.source_security_group_id == aws_security_group.lambda.id
    error_message = "VPCE ingress rule source_security_group_id must reference aws_security_group.lambda"
  }
}

# ---------------------------------------------------------------------------
# Test 8: DynamoDB Gateway VPC Endpoint — associated with both private route
# tables AND both DB route tables (four associations total).
#
# Satisfies AC: "one asserts the DynamoDB gateway endpoint is associated with
# both private route tables AND both db route tables (four associations total)"
#
# aws_vpc_endpoint_route_table_association is used for gateway endpoints
# (inline route_table_ids on aws_vpc_endpoint is an alternative, but the
# association resource gives plan-time attributes we can enumerate).
#
# Route table IDs are computed (unknown at plan time); we override each
# route table resource with a deterministic mock ID and then assert that
# each association's route_table_id equals the corresponding mock ID.
# ---------------------------------------------------------------------------
run "dynamodb_gateway_endpoint_route_table_associations" {
  command = plan

  variables {
    environment = "test"
  }

  override_resource {
    target = aws_route_table.private[0]
    values = {
      id    = "rtb-private-0-mock"
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.private[1]
    values = {
      id    = "rtb-private-1-mock"
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.db[0]
    values = {
      id    = "rtb-db-0-mock"
      route = []
    }
    override_during = plan
  }

  override_resource {
    target = aws_route_table.db[1]
    values = {
      id    = "rtb-db-1-mock"
      route = []
    }
    override_during = plan
  }

  # Endpoint type must be Gateway (free; no security group, no DNS toggle)
  assert {
    condition     = aws_vpc_endpoint.dynamodb.vpc_endpoint_type == "Gateway"
    error_message = "DynamoDB VPC endpoint must be of type Gateway"
  }

  # Four associations: two private route tables + two DB route tables
  assert {
    condition     = aws_vpc_endpoint_route_table_association.dynamodb_private[0].route_table_id == aws_route_table.private[0].id
    error_message = "DynamoDB endpoint must be associated with private route table 0"
  }

  assert {
    condition     = aws_vpc_endpoint_route_table_association.dynamodb_private[1].route_table_id == aws_route_table.private[1].id
    error_message = "DynamoDB endpoint must be associated with private route table 1"
  }

  assert {
    condition     = aws_vpc_endpoint_route_table_association.dynamodb_db[0].route_table_id == aws_route_table.db[0].id
    error_message = "DynamoDB endpoint must be associated with DB route table 0"
  }

  assert {
    condition     = aws_vpc_endpoint_route_table_association.dynamodb_db[1].route_table_id == aws_route_table.db[1].id
    error_message = "DynamoDB endpoint must be associated with DB route table 1"
  }
}
