terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# ---------------------------------------------------------------------------
# Aurora cluster parameter group
# Family aurora-postgresql16 is required for Aurora PostgreSQL 16.x.
# No shared_preload_libraries entries are needed: vector, pg_trgm, and pgcrypto
# are CREATE EXTENSION extensions loaded by schema migrations (story 2.3),
# not preloaded server libraries.
# ---------------------------------------------------------------------------

resource "aws_rds_cluster_parameter_group" "this" {
  name        = "${var.cluster_identifier}-pg16"
  family      = "aurora-postgresql16"
  description = "Knotify ${var.environment} Aurora PostgreSQL 16 cluster parameter group"

  tags = {
    Name        = "${var.cluster_identifier}-pg16"
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Aurora Serverless v2 cluster
#
# engine_mode is left at the provider default ("provisioned") — Serverless v2
# runs under the provisioned engine_mode with serverlessv2_scaling_configuration,
# unlike the legacy Serverless v1 which uses engine_mode = "serverless".
#
# manage_master_user_password = true instructs Aurora to generate the master
# credential and store/rotate it in AWS Secrets Manager. The password never
# appears in Terraform state; the module outputs the secret ARN so downstream
# callers (Lambda, migrator) can retrieve the credential at runtime.
#
# iam_database_authentication_enabled is deferred to phase 3 (Lambda foundations)
# when the IAM execution roles are defined.
# ---------------------------------------------------------------------------

resource "aws_rds_cluster" "this" {
  cluster_identifier = var.cluster_identifier
  engine             = "aurora-postgresql"
  engine_version     = "16.4"

  # Serverless v2 scaling configuration — ACU bounds are caller-configurable
  serverlessv2_scaling_configuration {
    min_capacity = var.min_acu
    max_capacity = var.max_acu
  }

  database_name   = var.database_name
  master_username = var.master_username

  # Aurora manages and rotates the master credential in Secrets Manager;
  # the password is never stored in Terraform state.
  manage_master_user_password = true

  # Network — cluster is private; security group and subnet group come from
  # the phase-1 networking module outputs passed in as variables.
  vpc_security_group_ids = [var.aurora_security_group_id]
  db_subnet_group_name   = var.db_subnet_group_name

  # Encryption at rest is always on
  storage_encrypted = true

  # Parameter group authored above
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name

  # Observability — send PostgreSQL logs to CloudWatch Logs
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # Environment-specific safety flags — all variable-driven so dev and prod
  # callers supply the appropriate values via tfvars (story 2.14).
  deletion_protection     = var.deletion_protection
  skip_final_snapshot     = var.skip_final_snapshot
  apply_immediately       = var.apply_immediately
  backup_retention_period = var.backup_retention_period

  tags = {
    Name        = var.cluster_identifier
    Environment = var.environment
  }
}

# ---------------------------------------------------------------------------
# Aurora Serverless v2 cluster instance
#
# instance_class = "db.serverless" is the Serverless v2 instance class.
# At least one instance is required for Aurora to accept connections.
# ---------------------------------------------------------------------------

resource "aws_rds_cluster_instance" "this" {
  identifier         = "${var.cluster_identifier}-instance-1"
  cluster_identifier = aws_rds_cluster.this.cluster_identifier
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.this.engine
  engine_version     = aws_rds_cluster.this.engine_version

  # Minor version upgrades are applied automatically by AWS within the
  # configured maintenance window. Combined with engine_version "16.4" this
  # means Terraform controls the major/minor floor but AWS keeps the patch
  # level current. Use lifecycle { ignore_changes = [engine_version] } if
  # state drift from AWS-applied minor upgrades becomes noisy (phase 11).
  auto_minor_version_upgrade = true

  # Instances in a private subnet group — no public endpoint
  publicly_accessible = false

  tags = {
    Name        = "${var.cluster_identifier}-instance-1"
    Environment = var.environment
  }
}
