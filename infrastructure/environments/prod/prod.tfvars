environment = "prod"

# Aurora — prod values per story 2.14 AC
aurora_min_acu                 = 1.0
aurora_max_acu                 = 8.0
aurora_deletion_protection     = true
aurora_skip_final_snapshot     = false
aurora_apply_immediately       = false
aurora_backup_retention_period = 30

# Aurora postgres log group retention — longer in prod for incident forensics
aurora_postgresql_log_retention_days = 7

# DynamoDB — prod values per story 2.14 AC
dynamodb_point_in_time_recovery = true
dynamodb_deletion_protection    = true
