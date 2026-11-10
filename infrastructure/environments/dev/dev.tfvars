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

# Supabase connection — populated 2026-11-10 for story 14.12.
# Values derived from the Supabase project `waulcjgqytiqkwdrkchg`:
#   - Project ref via MCP get_project_url: https://waulcjgqytiqkwdrkchg.supabase.co
#   - Region: eu-west-2 (London). The IPv6 2a05:d01c::/32 observed via
#     inet_server_addr() is Supabase's own edge range — NOT AWS region — so
#     the region had to be confirmed out of band by the operator. An initial
#     eu-central-1 guess was rejected by the pooler with "tenant/user not
#     found"; retrying against aws-0-eu-west-2 authenticated successfully.
#   - Pooler host: aws-0-eu-west-2.pooler.supabase.com (shared pooler,
#     port 6543, transaction mode). Verified end-to-end: psycopg2.connect
#     with the operator-supplied password returned current_user='postgres'.
#   - Pooler username format is `postgres.<project-ref>` on the shared pooler.
# The Supabase database password is NOT stored here (nor anywhere in
# Terraform). It is injected directly into AWS Secrets Manager via
# `aws secretsmanager put-secret-value` after the 14.12 apply — see the
# runbook (feature-1 story 14.16).
supabase_host     = "aws-0-eu-west-2.pooler.supabase.com"
supabase_username = "postgres.waulcjgqytiqkwdrkchg"

# supabase_port defaults to 6543 (transaction mode — required for Lambda).
# supabase_dbname defaults to "postgres".
# Override only if your Supabase project uses non-standard values.
