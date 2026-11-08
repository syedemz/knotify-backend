variable "environment" {
  description = "Deployment environment name (dev or prod)"
  type        = string
  default     = "prod"
}

# ---------------------------------------------------------------------------
# Aurora module variables — wired from this env root into modules/aurora.
# Prod-safe defaults; dev/dev.tfvars supplies more permissive values.
# ---------------------------------------------------------------------------

variable "aurora_min_acu" {
  description = "Minimum Aurora Capacity Units for Serverless v2 (dev: 0.5, prod: 1.0)"
  type        = number
  default     = 1.0
}

variable "aurora_max_acu" {
  description = "Maximum Aurora Capacity Units for Serverless v2 (dev: 2.0, prod: 8.0)"
  type        = number
  default     = 8.0
}

variable "aurora_deletion_protection" {
  description = "Enable deletion protection on the Aurora cluster (false in dev, true in prod)"
  type        = bool
  default     = true
}

variable "aurora_skip_final_snapshot" {
  description = "Skip the final snapshot on cluster deletion (true in dev, false in prod)"
  type        = bool
  default     = false
}

variable "aurora_apply_immediately" {
  description = "Apply Aurora changes immediately (true in dev, false in prod)"
  type        = bool
  default     = false
}

variable "aurora_backup_retention_period" {
  description = "Number of days to retain Aurora automated backups (7 in dev, 30 in prod)"
  type        = number
  default     = 30
}

variable "aurora_postgresql_log_retention_days" {
  description = "Retention in days for the Aurora /aws/rds/cluster/<id>/postgresql CloudWatch log group (1 in dev, 7 in prod)"
  type        = number
  default     = 7
}

# ---------------------------------------------------------------------------
# DynamoDB module variables — wired from this env root into modules/dynamodb.
# Prod-safe defaults; dev/dev.tfvars supplies more permissive values.
# ---------------------------------------------------------------------------

variable "dynamodb_point_in_time_recovery" {
  description = "Enable PITR on DynamoDB tables (false in dev, true in prod)"
  type        = bool
  default     = true
}

variable "dynamodb_deletion_protection" {
  description = "Enable DynamoDB native deletion protection (false in dev, true in prod)"
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Cognito module variables — story 4.5
# advanced_security_mode is set to AUDIT (the minimum required by the V2
# PreTokenGeneration trigger — brainstorm B2). Flip to ENFORCED in the
# phase 11 hardening pass (architecture.md §13 #1).
# ---------------------------------------------------------------------------

variable "advanced_security_mode" {
  description = "Cognito Advanced Security Mode. Valid values: OFF | AUDIT | ENFORCED. Default AUDIT is the minimum required by the V2 PreTokenGeneration trigger (story 4.4 / brainstorm B2). Upgrade to ENFORCED in phase 11."
  type        = string
  default     = "AUDIT"
}

# ---------------------------------------------------------------------------
# ACM certificate module variables — story 5.2
# Default "" → module produces zero resources (pre-cutover prod plan-only mode).
# Set domain_name + hosted_zone_id in prod.tfvars per docs/PROD_CUTOVER.md §4b
# when ready to create the prod certificate.
# ---------------------------------------------------------------------------

variable "domain_name" {
  description = "Primary domain name for the ACM certificate. Default empty string (zero resources until prod cutover). Set in prod.tfvars per PROD_CUTOVER.md §4b."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID for DNS validation records. Default empty string. Set in prod.tfvars per PROD_CUTOVER.md §4b."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# API Gateway throttling — story 5.3 (env-level wiring, deferred from 5.1)
# Prod uses production-capacity values (brainstorm Mn4).
# ---------------------------------------------------------------------------

variable "api_gateway_throttling_burst_limit" {
  description = "Maximum concurrent requests allowed by the API Gateway default stage. Dev: 10. Prod: 500."
  type        = number
  default     = 500
}

variable "api_gateway_throttling_rate_limit" {
  description = "Maximum steady-state request rate (req/s) for the API Gateway default stage. Dev: 25. Prod: 1000."
  type        = number
  default     = 1000
}

# ---------------------------------------------------------------------------
# Supabase toggle — phase 14 (symmetric declaration with dev; prod pinned false).
# A dedicated prod-cutover PRD governs any future prod=true transition.
# supabase_host / port / dbname / username are declared here so prod/main.tf
# can reference them symmetrically without variable-not-found errors; they are
# only meaningful when use_supabase=true, which prod.tfvars never sets.
# ---------------------------------------------------------------------------

variable "use_supabase" {
  description = "When true, routes database traffic through Supabase hosted Postgres instead of Aurora Serverless v2. Prod is pinned false in prod.tfvars (phase 14 safety). A dedicated prod-cutover PRD is required before flipping this in prod."
  type        = bool
  default     = false
}

variable "supabase_host" {
  description = "Supabase pooler hostname. Empty string in prod (use_supabase pinned false). Set via prod.tfvars only after a dedicated prod-cutover PRD approves the transition."
  type        = string
  default     = ""
}

variable "supabase_port" {
  description = "Supabase pooler port. 6543 = transaction mode. Only meaningful when use_supabase=true; prod is pinned false."
  type        = number
  default     = 6543
}

variable "supabase_dbname" {
  description = "Supabase database name. Only meaningful when use_supabase=true; prod is pinned false."
  type        = string
  default     = "postgres"
}

variable "supabase_username" {
  description = "Supabase pooler username. Empty string in prod (use_supabase pinned false)."
  type        = string
  default     = ""
}
