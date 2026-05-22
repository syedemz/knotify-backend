# ---------------------------------------------------------------------------
# Security group — Lambda execution
# No inbound rules. Explicit allow-all egress (Terraform strips the implicit
# default when any egress block is managed, so we declare it explicitly).
# ---------------------------------------------------------------------------

resource "aws_security_group" "lambda" {
  name        = "knotify-${var.environment}-sg-lambda"
  description = "Lambda execution security group"
  vpc_id      = aws_vpc.this.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name        = "knotify-${var.environment}-sg-lambda"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Security group — Aurora
# No egress rules. Ingress from Lambda only — managed via a separate
# aws_security_group_rule to avoid circular references and to allow
# plan-mode tests to assert the rule attributes cleanly.
# ---------------------------------------------------------------------------

resource "aws_security_group" "aurora" {
  name        = "knotify-${var.environment}-sg-aurora"
  description = "Aurora ingress from Lambda only"
  vpc_id      = aws_vpc.this.id

  tags = {
    Name        = "knotify-${var.environment}-sg-aurora"
    Environment = var.environment
  }
}

resource "aws_security_group_rule" "aurora_ingress_from_lambda" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = aws_security_group.lambda.id
}
