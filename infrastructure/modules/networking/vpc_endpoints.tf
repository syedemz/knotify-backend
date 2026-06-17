# ---------------------------------------------------------------------------
# Data sources for service name construction
# ---------------------------------------------------------------------------

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Security group — VPC endpoint (sg-vpce)
# Allows TCP 443 ingress from sg-lambda only.
# No egress rules needed: the response path uses the established connection.
# ---------------------------------------------------------------------------

resource "aws_security_group" "vpce" {
  name        = "knotify-${var.environment}-sg-vpce"
  description = "VPC endpoint security group - HTTPS from Lambda only"
  vpc_id      = aws_vpc.this.id

  tags = {
    Name        = "knotify-${var.environment}-sg-vpce"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "vpce_ingress_from_lambda" {
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  security_group_id        = aws_security_group.vpce.id
  source_security_group_id = aws_security_group.lambda.id
}

# ---------------------------------------------------------------------------
# Secrets Manager Interface VPC Endpoint
# Lambdas in private subnets use the standard hostname
# secretsmanager.<region>.amazonaws.com — private_dns_enabled=true ensures
# the resolution short-circuits to this endpoint without NAT or IGW.
# Attached to both private subnets (one ENI per AZ for HA).
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "secretsmanager" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.region}.secretsmanager"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = {
    Name        = "knotify-${var.environment}-vpce-secretsmanager"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# DynamoDB Gateway VPC Endpoint
# Free; no security group and no DNS toggle required for Gateway endpoints.
# Explicit route-table association resources are used (rather than inline
# route_table_ids) so plan-mode tests can assert each association cleanly.
# Associated with both private route tables (Lambda subnets) and both DB
# route tables (future Aurora-adjacent consumers in phases 6+).
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "dynamodb" {
  vpc_id            = aws_vpc.this.id
  service_name      = "com.amazonaws.${data.aws_region.current.region}.dynamodb"
  vpc_endpoint_type = "Gateway"

  tags = {
    Name        = "knotify-${var.environment}-vpce-dynamodb"
    Environment = var.environment
  }
}

resource "aws_vpc_endpoint_route_table_association" "dynamodb_private" {
  count           = 2
  vpc_endpoint_id = aws_vpc_endpoint.dynamodb.id
  route_table_id  = aws_route_table.private[count.index].id
}

resource "aws_vpc_endpoint_route_table_association" "dynamodb_db" {
  count           = 2
  vpc_endpoint_id = aws_vpc_endpoint.dynamodb.id
  route_table_id  = aws_route_table.db[count.index].id
}

# ---------------------------------------------------------------------------
# Cognito IDP Interface VPC Endpoint
# Profile Lambda calls cognito-idp:AdminUpdateUserAttributes to flip
# custom:profile_complete after the PATCH UPDATE commits. Private subnets
# have no NAT, so without this endpoint the SDK hangs on DNS / TCP SYN to
# cognito-idp.<region>.amazonaws.com until the Lambda's 30s timeout.
# Surfaced by the phase-7 live probe on 2026-06-17.
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "cognito_idp" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.region}.cognito-idp"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = {
    Name        = "knotify-${var.environment}-vpce-cognito-idp"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Lambda Interface VPC Endpoint
# Profile Lambda fires lambda:Invoke(InvocationType=Event) against the
# refresh_deck_view function after PATCH commits. Same private-subnet egress
# problem as cognito-idp above. Surfaced by the phase-7 live probe on
# 2026-06-17.
# ---------------------------------------------------------------------------

resource "aws_vpc_endpoint" "lambda" {
  vpc_id              = aws_vpc.this.id
  service_name        = "com.amazonaws.${data.aws_region.current.region}.lambda"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true

  tags = {
    Name        = "knotify-${var.environment}-vpce-lambda"
    Environment = var.environment
  }
}
