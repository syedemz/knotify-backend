environment = "dev"

# Aurora — dev values per story 2.14 AC
aurora_min_acu                 = 0
aurora_max_acu                 = 2.0
aurora_deletion_protection     = false
aurora_skip_final_snapshot     = true
aurora_apply_immediately       = true
aurora_backup_retention_period = 7

# Aurora postgres log group retention — short in dev to keep CloudWatch cost low
aurora_postgresql_log_retention_days = 1

# DynamoDB — dev values per story 2.14 AC
dynamodb_point_in_time_recovery = false
dynamodb_deletion_protection    = false
