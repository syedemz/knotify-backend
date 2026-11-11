# Supabase ↔ Aurora Operator Runbook

**Audience:** Operators who need to flip the database backend, rotate credentials, or troubleshoot connectivity. This is a post-implementation operational reference — not a design or story walkthrough. For the full architectural rationale and implementation details, see `features/feature-1-supabase-alternative.md` and `features/feature-1-supabase-alternative-plan.md`.

**Environment:** AWS account `776217504626`, region `eu-central-1`. Terraform state backend: S3 `knotify-dev-tfstate`. All `terraform` commands run from `infrastructure/environments/dev/`.

---

## 1. Flipping the flag: Aurora → Supabase (`use_supabase=false` → `true`)

Use this when you want to tear down Aurora + private VPC and route dev traffic through Supabase.

**Pre-condition:** `dev.tfvars` must contain valid `supabase_host` and `supabase_username` values, and the secret `knotify/dev/supabase_credential` must already be populated with the real Supabase password (see step 5 if starting from scratch).

### Step 1 — Confirm current state

```bash
cd infrastructure/environments/dev

# Verify Aurora path is currently live
grep 'use_supabase' dev.tfvars
# Expected output:  use_supabase = false
```

### Step 2 — Set the flag

```bash
# Edit dev.tfvars: change  use_supabase = false  to  use_supabase = true
sed -i 's/^use_supabase = false/use_supabase = true/' dev.tfvars

# Confirm
grep 'use_supabase' dev.tfvars
# Expected output:  use_supabase = true
```

### Step 3 — Verify supabase_host and supabase_username are set

```bash
grep -E 'supabase_host|supabase_username' dev.tfvars
# Expected (example values for the knotify dev project):
#   supabase_host     = "aws-0-eu-west-2.pooler.supabase.com"
#   supabase_username = "postgres.waulcjgqytiqkwdrkchg"
```

If either value is empty, fill it now. The pooler host and username come from Supabase Dashboard → Project Settings → Database → Connection Pooling (port 6543, transaction mode).

### Step 4 — Preview the plan

```bash
terraform plan -var-file=dev.tfvars -out=flip-to-supabase.tfplan
# Expected headline:  Plan: N to add, N to change, N to destroy.
# Must see:
#   module.supabase[0].aws_secretsmanager_secret.credential will be created
#   module.aurora[0].* will be destroyed  (if Aurora is currently deployed)
#   Every Aurora-using Lambda env_vars updated: SUPABASE_HOST/PORT/DBNAME replaces AURORA_HOST/PORT/DBNAME
#   USE_SUPABASE = "true"  on every DB Lambda
# Must NOT see any errors.
```

### Step 5 — Apply

```bash
# User approval gate — read the plan output before running this
terraform apply flip-to-supabase.tfplan
```

### Step 6 — Populate the Supabase credential secret (first-time only)

Terraform seeds the secret with an empty password via `lifecycle.ignore_changes`. You must inject the real password once. The password never enters Terraform state.

```bash
# Replace <password> with the actual Supabase database password
aws secretsmanager put-secret-value \
  --secret-id knotify/dev/supabase_credential \
  --secret-string '{"username":"postgres.waulcjgqytiqkwdrkchg","password":"<password>"}'

# Verify (password is not printed — only confirm it is non-empty)
aws secretsmanager get-secret-value \
  --secret-id knotify/dev/supabase_credential \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; s=json.load(sys.stdin); print('username:', s['username'], '| password_set:', bool(s['password']))"
# Expected:  username: postgres.waulcjgqytiqkwdrkchg | password_set: True
```

### Step 7 — Run the db_migrator Lambda

The `null_resource.db_migrator_invoke` Terraform resource fires automatically on apply if the layer hash changed. If you need to trigger it manually (e.g., the Supabase schema is empty):

```bash
aws lambda invoke \
  --function-name knotify-dev-db-migrator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/db-migrator-out.json

cat /tmp/db-migrator-out.json
# Expected:  {"statusCode": 200, "body": "...", "migrations_applied": N, ...}
# Check CloudWatch for the migration log stream if statusCode != 200
```

### Step 8 — Commit the tfvars change

```bash
git add infrastructure/environments/dev/dev.tfvars
git commit -m "chore: flip use_supabase=true in dev.tfvars"
```

---

## 2. Flipping the flag: Supabase → Aurora (`use_supabase=true` → `false`)

Use this to restore the full Aurora + private VPC stack. Expected plan numbers based on the story 14.15 reverse-direction proof: `Plan: 40 to add, 65 to change, 5 to destroy`. Any material divergence from those numbers is a signal to investigate before applying.

### Step 1 — Confirm current state

```bash
cd infrastructure/environments/dev

grep 'use_supabase' dev.tfvars
# Expected output:  use_supabase = true
```

### Step 2 — Set the flag

```bash
sed -i 's/^use_supabase = true/use_supabase = false/' dev.tfvars

grep 'use_supabase' dev.tfvars
# Expected output:  use_supabase = false
```

### Step 3 — Preview the plan

```bash
terraform plan -var-file=dev.tfvars -out=flip-to-aurora.tfplan
# Expected headline:  Plan: 40 to add, 65 to change, 5 to destroy.
# Must see:
#   module.aurora[0].* will be created
#   module.networking[0].* will be created  (VPC + subnets + NAT + VPC endpoints)
#   module.supabase[0].aws_secretsmanager_secret.credential will be destroyed
#   Every Aurora-using Lambda env_vars updated: AURORA_HOST/PORT/DBNAME replaces SUPABASE_HOST/PORT/DBNAME
#   USE_SUPABASE = "false"  on every DB Lambda
# Must NOT see any errors.
```

### Step 4 — Apply

```bash
# User approval gate — read the plan output before running this.
# Apply provisions Aurora + VPC (~35-45 minutes for first Aurora cluster creation).
terraform apply flip-to-aurora.tfplan
```

After apply completes, Terraform's `null_resource.db_migrator_invoke` fires automatically and runs `db_migrator` against the new Aurora cluster. The migrator applies all migrations and creates the `app_user` / `aurora_refresh` roles.

### Step 5 — Confirm Aurora secret is populated

Aurora managed-rotation auto-populates the master secret during cluster creation. For the app-user secret, the db_migrator handler writes it after migrations complete. Confirm both are present:

```bash
aws secretsmanager get-secret-value \
  --secret-id knotify/dev/aurora-master-user-secret \
  --query 'SecretString' --output text | python3 -c "import sys,json; s=json.load(sys.stdin); print('host:', s.get('host','?'), '| user:', s.get('username','?'))"

aws secretsmanager get-secret-value \
  --secret-id knotify-dev-app-user-credential \
  --query 'SecretString' --output text | python3 -c "import sys,json; s=json.load(sys.stdin); print('username:', s['username'], '| password_set:', bool(s['password']))"
```

### Step 6 — Commit the tfvars change

```bash
git add infrastructure/environments/dev/dev.tfvars
git commit -m "chore: flip use_supabase=false in dev.tfvars (restore Aurora)"
```

---

## 3. Rotating the Supabase password

**Posture: set-once (story 14.14b).** The Supabase path uses a set-once rotation strategy rather than automatic periodic rotation. The reason: Supavisor (Supabase's transaction-mode pooler) caches role passwords with a 30–60 second propagation lag after `ALTER ROLE`. Automatic rotation on every CI apply created auth-failure windows during that lag for cold-boot Lambdas. Instead, passwords are written once at role-creation time and only updated when an operator explicitly decides to rotate.

**When to rotate:** a compromised credential is suspected, a departing team member held the password, or a Supabase project is being migrated.

### Step 1 — Generate a new password

```bash
NEW_PW=$(python3 -c "import secrets,string; print(secrets.token_urlsafe(32))")
echo "New password (save this): $NEW_PW"
```

### Step 2 — Update the Postgres role password on Supabase

Connect to Supabase as the superuser (`postgres`) and update the role. The easiest path without installing psql locally:

```bash
# Using the Supabase CLI (must be logged in: supabase login)
supabase db execute --project-ref waulcjgqytiqkwdrkchg \
  --sql "ALTER ROLE app_user PASSWORD '$NEW_PW';"

# Verify (expect no error output — the role exists)
supabase db execute --project-ref waulcjgqytiqkwdrkchg \
  --sql "SELECT rolname FROM pg_roles WHERE rolname = 'app_user';"
```

Alternatively via psql:

```bash
PGPASSWORD="<superuser-password>" psql \
  "postgresql://postgres.waulcjgqytiqkwdrkchg@aws-0-eu-west-2.pooler.supabase.com:6543/postgres?sslmode=require" \
  -c "ALTER ROLE app_user PASSWORD '$NEW_PW';"
```

### Step 3 — Wait for Supavisor cache to flush

```bash
# Supavisor caches passwords for 30-60 seconds. Wait before updating the secret
# so Lambdas that read the secret during the propagation window still match the
# cached Supavisor state.
sleep 90
```

### Step 4 — Update the AWS Secrets Manager secret

```bash
# Replace <project-ref> with waulcjgqytiqkwdrkchg
aws secretsmanager put-secret-value \
  --secret-id knotify-dev-app-user-credential \
  --secret-string "{\"username\":\"app_user.waulcjgqytiqkwdrkchg\",\"password\":\"$NEW_PW\"}"

# Confirm
aws secretsmanager get-secret-value \
  --secret-id knotify-dev-app-user-credential \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; s=json.load(sys.stdin); print('username:', s['username'], '| password_set:', bool(s['password']))"
# Expected:  username: app_user.waulcjgqytiqkwdrkchg | password_set: True
```

### Step 5 — Test a Lambda that uses the DB

```bash
aws lambda invoke \
  --function-name knotify-dev-profile \
  --payload '{"httpMethod":"GET","path":"/v1/profile/me","headers":{},"queryStringParameters":null,"body":null}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/profile-out.json

cat /tmp/profile-out.json
# A 401 (Unauthorized — missing auth token) is a healthy response.
# A 502 or FunctionError with "password authentication failed" means
# the pool cache has not flushed; wait another 60 s and retry.
```

**Note on the Supabase master (superuser) credential:** The superuser password (`knotify/dev/supabase_credential` secret) is used only by `db_migrator`. Rotate it by repeating steps 2–4 targeting `knotify/dev/supabase_credential` and using `postgres` as the role name in `ALTER ROLE`.

---

## 4. Rotating the Aurora password

Aurora uses AWS-managed secret rotation. The master user secret is managed by Secrets Manager automatic rotation tied to the cluster.

### Option A — Trigger immediate managed rotation

```bash
# Get the secret ARN from Terraform outputs
AURORA_SECRET_ARN=$(cd infrastructure/environments/dev && terraform output -raw aurora_master_user_secret_arn 2>/dev/null)

# Trigger immediate rotation
aws secretsmanager rotate-secret --secret-id "$AURORA_SECRET_ARN"

# Poll for rotation completion (repeats until RotationEnabled is true and no pending version)
aws secretsmanager describe-secret \
  --secret-id "$AURORA_SECRET_ARN" \
  --query '{LastRotatedDate:LastRotatedDate,RotationEnabled:RotationEnabled}'
```

### Option B — Rotate the app_user credential manually

The `app_user` and `aurora_refresh` credentials on Aurora are managed by `db_migrator`. Re-invoke the migrator to trigger a password re-rotation (the migrator's `_handler_aurora` path re-generates and writes new passwords for both roles):

```bash
aws lambda invoke \
  --function-name knotify-dev-db-migrator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/db-migrator-out.json

cat /tmp/db-migrator-out.json
# Expected:  {"statusCode": 200, ...}
```

---

## 5. Troubleshooting

### Scenario A — Lambda cannot connect to the database

**Symptom:** Lambda returns a 502 error or a `FunctionError` body containing `psycopg2.OperationalError`, `password authentication failed`, or `could not connect to server`.

**Diagnostic steps:**

```bash
# 1. Check which backend is active
grep 'use_supabase' infrastructure/environments/dev/dev.tfvars

# 2. Check the Lambda's env vars to confirm USE_SUPABASE matches dev.tfvars
aws lambda get-function-configuration \
  --function-name knotify-dev-profile \
  --query 'Environment.Variables.{USE_SUPABASE:USE_SUPABASE,DB_SECRET_ARN:DB_SECRET_ARN}'
# USE_SUPABASE must be exactly "true" or "false" (lowercase) — any other value
# (e.g. "True", "TRUE", "") routes to the Aurora path regardless of intent.

# 3. Confirm the secret exists and has a non-empty password
# For Supabase:
aws secretsmanager get-secret-value \
  --secret-id knotify/dev/supabase_credential \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; s=json.load(sys.stdin); print('password_set:', bool(s['password']))"

# For Aurora:
aws secretsmanager get-secret-value \
  --secret-id knotify-dev-app-user-credential \
  --query 'SecretString' --output text \
  | python3 -c "import sys,json; s=json.load(sys.stdin); print('password_set:', bool(s['password']))"

# 4. Test TCP reachability from outside Lambda (Supabase path)
python3 -c "
import socket, ssl
ctx = ssl.create_default_context()
with socket.create_connection(('aws-0-eu-west-2.pooler.supabase.com', 6543), timeout=5) as s:
    with ctx.wrap_socket(s, server_hostname='aws-0-eu-west-2.pooler.supabase.com'):
        print('TCP + TLS OK')
"

# 5. Check CloudWatch logs for the failing Lambda
aws logs tail /aws/lambda/knotify-dev-profile --since 10m --filter-pattern '"ERROR"'
```

**Common causes and fixes:**

| Symptom | Likely cause | Fix |
|---|---|---|
| `password authentication failed` on Supabase | Supavisor cache lag after a password rotation | Wait 60–90 s and retry; see §3 |
| `password authentication failed` on Aurora | App-user secret not updated after migrator run | Re-invoke db_migrator (§4 Option B) |
| `USE_SUPABASE` is not `"true"` but flag is `true` in tfvars | Terraform apply pending — Lambda env var stale | Run `terraform apply` to push the env var update |
| `could not connect to server` on Supabase | Supabase project paused (free-tier auto-pause after 7 days inactivity) | Go to Supabase Dashboard → Project → Restore; or run any SQL query via MCP |
| `could not connect to server` on Aurora | Lambda has no VPC config (`use_supabase` was `true` during the last apply) | Flip to `use_supabase=false` and re-apply to restore VPC config (§2) |

---

### Scenario B — RLS returns zero rows

**Symptom:** A query that should return data returns an empty result set with no error. Affects the `users` table (which has `ENABLE + FORCE ROW LEVEL SECURITY` from migration 0007).

**How RLS silently fails:** The `USE_SUPABASE` dispatch in `knotify_db/__init__.py` is case-sensitive: only the exact lowercase string `"true"` activates the Supabase path. The RLS helpers (`set_rls_context`, `rls_context`) are always sourced from `knotify_db._rls` regardless of backend — they are never redefined per-backend. A mismatch in GUC names (e.g., `app.user_id` instead of `app.requesting_user_id`) causes the policy to fail-close to zero rows with no error raised.

**Diagnostic steps:**

```bash
# 1. Confirm the GUC names in the layer source match migration 0007
grep -n 'requesting_user_id\|requesting_user_sex' \
  infrastructure/src/layers/db/knotify_db/_rls.py
# Must see EXACTLY:
#   _GUC_USER_ID = "app.requesting_user_id"
#   _GUC_USER_SEX = "app.requesting_user_sex"

# 2. Confirm the same GUC names are in migration 0007
grep -n 'requesting_user_id\|requesting_user_sex' \
  infrastructure/db/migrations/0007_users_rls.sql
# Must match the values above character-for-character.

# 3. Confirm migration 0007 was applied (check via Supabase MCP or psycopg2):
# supabase> SELECT id FROM _yoyo_migration WHERE id LIKE '%0007%';
# Must return exactly one row.

# 4. Check if RLS is enabled on the users table
# supabase> SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'users';
# Must return: relrowsecurity=t, relforcerowsecurity=t

# 5. For Supabase: check that migration 0020 has been applied
# (migration 0020 revokes anon/authenticated/service_role grants from all tables — an open PR
# at time of writing; apply it manually if the hotfix branch has not yet merged into development)
# supabase> SELECT id FROM _yoyo_migration WHERE id LIKE '%0020%';
# If this returns zero rows and you are seeing unexpected empty results via PostgREST/Supabase
# client APIs, apply migration 0020 manually (it is idempotent — re-running is safe).

# 6. Verify the Lambda is passing a user_id claim through the JWT → RLS GUC chain
aws logs tail /aws/lambda/knotify-dev-profile --since 5m --filter-pattern '"set_rls_context"'
```

**Migration 0020 note:** Migration 0020 (`0020_revoke_postgrest_public_grants.sql`) is a Supabase-specific security hardening that revokes the default PostgREST grants (`anon`, `authenticated`, `service_role`) on all application tables. Without it, any holder of the publishable anon API key can read the full contents of `siblings`, `friendships`, `friend_requests`, `bookmarks`, `blocks`, and `refresh_log` via PostgREST — bypassing the Lambda authorization layer. The migration is backend-agnostic (wraps every statement in a `DO $$ ... $$` block that no-ops when the roles don't exist, so it is safe to apply on Aurora too). If you observe unexpected data exposure via the Supabase REST API, check whether this migration has been applied.

---

## 6. Cost sanity check

Monthly estimates based on the October 2026 AWS bill ($207 actual) and Supabase free-tier pricing.

| Line item | `use_supabase = false` (Aurora stack) | `use_supabase = true` (Supabase stack) |
|---|---|---|
| Aurora Serverless v2 (storage, I/O, backups, log ingestion — min_acu=0 dev) | ~$131 | $0 (not deployed) |
| VPC NAT Gateway + VPC endpoints (Secrets Manager, SSM, CloudWatch Logs, etc.) | ~$36 | $0 (no VPC — Lambdas run with public egress) |
| Supabase (hosted Postgres, free tier — covers 500 MB DB, 2 GB egress) | $0 (not used) | $0 (free tier sufficient for dev workload) |
| Supabase Pro tier (if free tier outgrown) | — | $25/mo |
| API Gateway, Lambda, Step Functions, DynamoDB, Cognito, AppSync, CloudWatch (consumption-priced) | ~$40 | ~$40 |
| **Total (approximate)** | **~$207/mo** | **~$40/mo** |
| **Saving vs Aurora stack** | — | **~$167/mo (~81%)** |

**Key notes:**
- The October 2026 Aurora + VPC cost of ~$167 is not a one-time charge. It accrues even at `min_acu=0` because Aurora Serverless v2 charges for storage, I/O, backups, and CloudWatch log ingestion continuously.
- The ~$40 consumption tail (Lambda, API Gateway, DynamoDB, etc.) is unaffected by the flag and unchanged between both states.
- If the Supabase free tier is outgrown ($25/mo Pro tier), the saving vs Aurora drops from ~$167 to ~$142/mo — still substantial.
- Production is pinned `use_supabase = false` in `infrastructure/environments/prod/prod.tfvars`. A dedicated prod-cutover PRD is required before Supabase is considered for production. Do not change that value without following that PRD.
