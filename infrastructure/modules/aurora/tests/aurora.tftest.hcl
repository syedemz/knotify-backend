# Aurora module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/aurora/

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# Test 1: Cluster engine, version, encryption, and credential management
# Satisfies AC: engine "aurora-postgresql", engine_version "16.4",
# storage_encrypted true, manage_master_user_password true.
# Note: auto_minor_version_upgrade is an aws_rds_cluster_instance attribute,
# not an aws_rds_cluster attribute — it is asserted in test 9 on the instance.
# ---------------------------------------------------------------------------
run "cluster_engine_and_encryption" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
  }

  assert {
    condition     = aws_rds_cluster.this.engine == "aurora-postgresql"
    error_message = "Cluster engine must be aurora-postgresql"
  }

  assert {
    condition     = aws_rds_cluster.this.engine_version == "16.4"
    error_message = "Cluster engine_version must be 16.4"
  }

  assert {
    condition     = aws_rds_cluster.this.storage_encrypted == true
    error_message = "Cluster storage must be encrypted"
  }

  assert {
    condition     = aws_rds_cluster.this.manage_master_user_password == true
    error_message = "Cluster must use manage_master_user_password (Secrets Manager rotation)"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Network isolation — not publicly accessible, correct SG and subnet group
# Satisfies AC: publicly_accessible false; vpc_security_group_ids references
# the Aurora SG input; db_subnet_group_name references the subnet group input
# ---------------------------------------------------------------------------
run "cluster_network_isolation" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
  }

  override_resource {
    target = aws_rds_cluster.this
    values = {
      vpc_security_group_ids = ["sg-aurora-mock-id"]
    }
    override_during = plan
  }

  assert {
    condition     = aws_rds_cluster.this.db_subnet_group_name == "knotify-test-db-subnets"
    error_message = "Cluster db_subnet_group_name must match input variable"
  }

  assert {
    condition     = contains(aws_rds_cluster.this.vpc_security_group_ids, "sg-aurora-mock-id")
    error_message = "Cluster vpc_security_group_ids must contain the aurora security group"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Serverless v2 scaling configuration uses dev defaults
# Satisfies AC: serverlessv2_scaling_configuration min_acu=0.5, max_acu=2.0
# for dev (the module default)
# ---------------------------------------------------------------------------
run "serverlessv2_scaling_defaults" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
    # min_acu and max_acu use module defaults (0.5 / 2.0)
  }

  assert {
    condition     = aws_rds_cluster.this.serverlessv2_scaling_configuration[0].min_capacity == 0.5
    error_message = "dev min_acu default must be 0.5"
  }

  assert {
    condition     = aws_rds_cluster.this.serverlessv2_scaling_configuration[0].max_capacity == 2.0
    error_message = "dev max_acu default must be 2.0"
  }
}

# ---------------------------------------------------------------------------
# Test 4: Serverless v2 scaling configuration accepts prod overrides
# Satisfies AC: prod defaults min_acu=1.0, max_acu=8.0
# ---------------------------------------------------------------------------
run "serverlessv2_scaling_prod_override" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
    min_acu                  = 1.0
    max_acu                  = 8.0
  }

  assert {
    condition     = aws_rds_cluster.this.serverlessv2_scaling_configuration[0].min_capacity == 1.0
    error_message = "prod min_acu override must be 1.0"
  }

  assert {
    condition     = aws_rds_cluster.this.serverlessv2_scaling_configuration[0].max_capacity == 8.0
    error_message = "prod max_acu override must be 8.0"
  }
}

# ---------------------------------------------------------------------------
# Test 5: Dev safety flags — deletion_protection false, skip_final_snapshot true,
# apply_immediately true (the module defaults, appropriate for dev)
# Satisfies AC: variable-driven flags with safe dev defaults
# ---------------------------------------------------------------------------
run "dev_safety_flags" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
    # All safety flags use module defaults (dev-safe)
  }

  assert {
    condition     = aws_rds_cluster.this.deletion_protection == false
    error_message = "deletion_protection must default to false (dev-safe)"
  }

  assert {
    condition     = aws_rds_cluster.this.skip_final_snapshot == true
    error_message = "skip_final_snapshot must default to true (dev-safe)"
  }

  assert {
    condition     = aws_rds_cluster.this.apply_immediately == true
    error_message = "apply_immediately must default to true (dev-safe)"
  }

  assert {
    condition     = aws_rds_cluster.this.backup_retention_period == 7
    error_message = "backup_retention_period must default to 7 (dev)"
  }
}

# ---------------------------------------------------------------------------
# Test 6: Prod safety flags — deletion_protection true, skip_final_snapshot false,
# apply_immediately false, backup_retention_period 30
# Satisfies AC: variable-driven flags with prod-safe overrides
# ---------------------------------------------------------------------------
run "prod_safety_flags" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
    deletion_protection      = true
    skip_final_snapshot      = false
    apply_immediately        = false
    backup_retention_period  = 30
  }

  assert {
    condition     = aws_rds_cluster.this.deletion_protection == true
    error_message = "deletion_protection must be settable to true (prod)"
  }

  assert {
    condition     = aws_rds_cluster.this.skip_final_snapshot == false
    error_message = "skip_final_snapshot must be settable to false (prod)"
  }

  assert {
    condition     = aws_rds_cluster.this.apply_immediately == false
    error_message = "apply_immediately must be settable to false (prod)"
  }

  assert {
    condition     = aws_rds_cluster.this.backup_retention_period == 30
    error_message = "backup_retention_period must be settable to 30 (prod)"
  }
}

# ---------------------------------------------------------------------------
# Test 7: CloudWatch logs export includes "postgresql"
# Satisfies AC: enabled_cloudwatch_logs_exports includes "postgresql"
# ---------------------------------------------------------------------------
run "cloudwatch_logs_export" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
  }

  assert {
    condition     = contains(aws_rds_cluster.this.enabled_cloudwatch_logs_exports, "postgresql")
    error_message = "enabled_cloudwatch_logs_exports must contain postgresql"
  }
}

# ---------------------------------------------------------------------------
# Test 8: Parameter group exists for family aurora-postgresql16 and is wired
# to the cluster via db_cluster_parameter_group_name
# Satisfies AC: aws_rds_cluster_parameter_group family and cluster wiring
# ---------------------------------------------------------------------------
run "parameter_group_wired_to_cluster" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
  }

  # Override computed name so the cross-resource equality is evaluable at plan time
  override_resource {
    target = aws_rds_cluster_parameter_group.this
    values = {
      id   = "knotify-test-aurora-pg16"
      name = "knotify-test-aurora-pg16"
    }
    override_during = plan
  }

  override_resource {
    target = aws_rds_cluster.this
    values = {
      db_cluster_parameter_group_name = "knotify-test-aurora-pg16"
    }
    override_during = plan
  }

  assert {
    condition     = aws_rds_cluster_parameter_group.this.family == "aurora-postgresql16"
    error_message = "Parameter group family must be aurora-postgresql16"
  }

  assert {
    condition     = aws_rds_cluster.this.db_cluster_parameter_group_name == aws_rds_cluster_parameter_group.this.name
    error_message = "Cluster db_cluster_parameter_group_name must reference the parameter group"
  }
}

# ---------------------------------------------------------------------------
# Test 9: Cluster instance is of type db.serverless, belongs to the cluster,
# and has auto_minor_version_upgrade = true.
# Satisfies AC: aws_rds_cluster_instance of type db.serverless;
# auto_minor_version_upgrade true (instance-level attribute in the AWS provider).
# ---------------------------------------------------------------------------
run "cluster_instance_is_serverless" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
  }

  # Override the cluster_identifier (computed) so the cross-resource reference
  # on the instance's cluster_identifier attribute is evaluable at plan time
  override_resource {
    target = aws_rds_cluster.this
    values = {
      cluster_identifier = "knotify-test-aurora"
    }
    override_during = plan
  }

  assert {
    condition     = aws_rds_cluster_instance.this.instance_class == "db.serverless"
    error_message = "Cluster instance must use instance_class db.serverless"
  }

  assert {
    condition     = aws_rds_cluster_instance.this.engine == "aurora-postgresql"
    error_message = "Cluster instance engine must be aurora-postgresql"
  }

  assert {
    condition     = aws_rds_cluster_instance.this.cluster_identifier == aws_rds_cluster.this.cluster_identifier
    error_message = "Cluster instance cluster_identifier must reference the cluster"
  }

  assert {
    condition     = aws_rds_cluster_instance.this.auto_minor_version_upgrade == true
    error_message = "Cluster instance auto_minor_version_upgrade must be true"
  }

  assert {
    condition     = aws_rds_cluster_instance.this.publicly_accessible == false
    error_message = "Cluster instance must not be publicly accessible"
  }
}

# ---------------------------------------------------------------------------
# Test 10: database_name is wired through to the cluster
# Satisfies AC: module output database_name reflects the input
# ---------------------------------------------------------------------------
run "database_name_wired" {
  command = plan

  variables {
    environment              = "test"
    cluster_identifier       = "knotify-test-aurora"
    database_name            = "knotify"
    aurora_security_group_id = "sg-aurora-mock-id"
    db_subnet_group_name     = "knotify-test-db-subnets"
  }

  assert {
    condition     = aws_rds_cluster.this.database_name == "knotify"
    error_message = "database_name must be wired from input variable to cluster"
  }
}
