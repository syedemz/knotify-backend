terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.20"
    }
  }
}

# ---------------------------------------------------------------------------
# soft_delete_aurora Lambda function — story 9.5
#
# Executes the §11.1 step-2 soft-delete UPDATE against Aurora:
#   UPDATE users SET deleted_at=NOW(), email=NULL, phone_number=NULL,
#     photo_url=NULL, chosen_profile_avatar=NULL, preferences='{}',
#     preference_vector=NULL, username='[deleted-user]',
#     first_name='Deleted', last_name='User'
#   WHERE user_id = :id AND deleted_at IS NULL
#
# Called from the account-deletion Step Functions state machine as the
# SoftDeleteAurora task (parallel cleanup branch, soft-delete path).
# In the purge_immediately branch it is chained sequentially to HardPurgeNow.
#
# Placement: INSIDE the VPC.
# WHY: Aurora is VPC-private — the Lambda must reach the cluster writer
# endpoint which is on a private subnet.  The Lambda must attach an ENI to
# the Lambda security group so it can reach Aurora.
# AWSLambdaVPCAccessExecutionRole is required — provided via role_arn.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — a single-row UPDATE against Aurora is fast; 30s is generous
# headroom for cold-start + Secrets Manager GetSecretValue + TCP connect.
# Memory: 128 MB — minimal psycopg2 workload; no large result sets.
# Layers: knotify_db (psycopg2 + knotify_db helpers) +
#         knotify_obs (structured logging).
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  layers        = var.layers
  architectures = ["arm64"]
  timeout       = 30
  memory_size   = 128

  # VPC config — REQUIRED: Aurora is VPC-private.
  # subnet_ids and security_group_ids are sourced from the networking module.
  vpc_config = var.vpc_config

  environment_variables = {
    # Secrets Manager secret name for the Aurora app_user credential.
    # Pattern: knotify-<env>-app-user-credential (written by db_migrator).
    DB_SECRET_NAME = var.db_secret_name

    # Aurora connection endpoint params — sourced from aurora module outputs.
    # The Secrets Manager secret contains only username + password; host/port/
    # dbname are injected separately so they can change (failover, blue/green)
    # without requiring a secret rotation.
    AURORA_HOST   = var.aurora_host
    AURORA_PORT   = var.aurora_port
    AURORA_DBNAME = var.aurora_dbname
  }
}
