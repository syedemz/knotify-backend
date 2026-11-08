environment = "dev"

# ACM certificate — story 5.2
# Empty strings → module produces zero resources (dev path).
# The us_east_1 provider alias is still exercised end-to-end.
domain_name    = ""
hosted_zone_id = ""

# Aurora — dev values per story 2.14 AC
aurora_min_acu                 = 0
aurora_max_acu                 = 4.0
aurora_deletion_protection     = false
aurora_skip_final_snapshot     = true
aurora_apply_immediately       = true
aurora_backup_retention_period = 7

# Aurora postgres log group retention — short in dev to keep CloudWatch cost low
aurora_postgresql_log_retention_days = 1

# DynamoDB — dev values per story 2.14 AC
dynamodb_point_in_time_recovery = false
dynamodb_deletion_protection    = false

# Supabase toggle — phase 14 (dev cost reduction)
# use_supabase=true routes all DB traffic through Supabase hosted Postgres,
# suppressing Aurora + VPC + NAT + VPC-endpoint resources (count=0 in 14.4).
# Flip to false to restore the full Aurora + private VPC stack.
use_supabase = true

# FILL BEFORE APPLY (story 14.12): replace the placeholder strings below with
# the real pooler hostname and pooler username from your Supabase project.
# Example host:     aws-0-eu-central-1.pooler.supabase.com
# Example username: postgres.<project-ref>
supabase_host     = "" # TODO(operator): set to Supabase pooler host before story 14.12 apply
supabase_username = "" # TODO(operator): set to Supabase pooler username before story 14.12 apply

# supabase_port defaults to 6543 (transaction mode — required for Lambda).
# supabase_dbname defaults to "postgres".
# Override only if your Supabase project uses non-standard values.
