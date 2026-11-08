---
feature: 1
title: Supabase alternative to Aurora RDS + private VPC (dev cost-reduction path)
status: plan-draft   # plan-draft → ready → in-progress → done
ready: false         # user flips to true when ready to dispatch
owner: syedemz
created: 2026-11-08
default_agent: backenddeveloper
feature_branch_hint: "subagent derives name per gitbranching.md — one branch for the whole feature"
prd: features/feature-1-supabase-alternative.md
prd_decisions: features/QA.txt
---

# Feature 1 Implementation Plan — Supabase alternative to Aurora RDS + private VPC

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **Project convention (per workspace CLAUDE.md):** every story in this plan is dispatched to the `backenddeveloper` subagent. The subagent derives the feature branch name from `gitbranching.md` at dispatch (one branch for the whole feature, `feat/feature-1-*`). **The main agent MUST NOT dictate the branch name** — reference the rule in the brief, never the name. All stories commit to that same feature branch; the final PR into `development` happens after the last story.

---

**Goal:** Introduce a `use_supabase` Terraform flag that toggles the dev environment between Aurora + private VPC (current path) and Supabase hosted Postgres (new path). Mutually exclusive; idempotent in both directions; zero Lambda handler code changes.

**Architecture:** The database access layer `infrastructure/src/layers/db/knotify_db/` is the single integration point. A new sibling file `_supabase.py` mirrors the public surface of `_db.py` (same `get_connection` signature, same `set_rls_context` / `rls_context` semantics via a shared `_rls.py` module). `knotify_db/__init__.py` picks the implementation at import time based on `USE_SUPABASE` env var. Terraform: a new `supabase` module (Secrets Manager secret only — Supabase itself is managed outside AWS); `networking` and `aurora` modules gated by `count = var.use_supabase ? 0 : 1`; Lambda env vars and vpc_config computed via locals in `environments/dev/main.tf` so every function module picks up the right values without per-module edits.

**Tech Stack:** Terraform ≥1.9, AWS provider ~>6.20, Python 3.14 Lambda runtime (arm64), psycopg2-binary 2.9.12, pgvector 0.3.6, Supabase hosted Postgres (eu-central-1 Frankfurt pooler, transaction mode, port 6543).

---

## File map

### New files

| Path | Responsibility |
|---|---|
| `scripts/supabase_pooler_proof.py` | Standalone one-shot script proving `SET LOCAL` GUCs work on the Supabase pooler inside a transaction. Run manually by the user before f1.1 starts. If it fails, STOP — the whole approach needs rethinking. |
| `infrastructure/modules/supabase/main.tf` | New Terraform module. Creates one AWS Secrets Manager secret (`knotify/<env>/supabase_credential`) with a placeholder JSON payload. Does NOT provision Supabase itself (Supabase is managed outside AWS). |
| `infrastructure/modules/supabase/variables.tf` | Module inputs: `environment` (string), `host` (string), `port` (number, default 6543), `dbname` (string, default `postgres`), `username` (string, default `postgres`). |
| `infrastructure/modules/supabase/outputs.tf` | Module outputs: `secret_arn`, `secret_name`, `host`, `port`, `dbname`. |
| `infrastructure/modules/supabase/tests/supabase.tftest.hcl` | Terraform test: `terraform plan` on fresh workspace produces exactly one `aws_secretsmanager_secret` resource with the expected name. |
| `infrastructure/src/layers/db/knotify_db/_rls.py` | Shared RLS GUC helpers extracted from `_db.py`. Owns `set_rls_context` and `rls_context`. One implementation used by both the Aurora path and the Supabase path. |
| `infrastructure/src/layers/db/knotify_db/_supabase.py` | Supabase connection builder. Mirrors `_db.get_connection` signature: accepts a dict (local dev) or a Secrets Manager ARN/name (Lambda runtime). Reads `SUPABASE_HOST` / `SUPABASE_PORT` / `SUPABASE_DBNAME` env vars when fetching from Secrets Manager. Uses `sslmode=require`. |
| `infrastructure/src/layers/db/tests/test_supabase_unit.py` | Unit tests for `_supabase.get_connection` dict-path (parameter mapping, SSL enforcement, port default). |
| `features/supabase-runbook.md` | Operator runbook: how to flip the flag, how to populate the secret, how to invoke `db_migrator` against Supabase, rollback procedure. |

### Modified files

| Path | Change |
|---|---|
| `infrastructure/src/layers/db/knotify_db/_db.py` | Remove inline RLS code (`set_rls_context`, `rls_context`, the two `_GUC_*` constants). Import from `_rls` and re-export, so the public symbol surface is unchanged. |
| `infrastructure/src/layers/db/knotify_db/__init__.py` | Conditional dispatch: `if os.environ.get("USE_SUPABASE") == "true": from ._supabase import get_connection` else `from ._db import get_connection`. `set_rls_context` and `rls_context` always come from `_rls`. |
| `infrastructure/environments/dev/variables.tf` | Add `variable "use_supabase"` (bool, default false). Add `variable "supabase_host"` (string, default empty). Add `variable "supabase_port"` (number, default 6543). Add `variable "supabase_dbname"` (string, default `"postgres"`). Add `variable "supabase_username"` (string, default `"postgres"`). |
| `infrastructure/environments/dev/dev.tfvars` | Set `use_supabase = true`. Set `supabase_host = "<pooler host user provides>"`. |
| `infrastructure/environments/dev/main.tf` | (1) Add `count = var.use_supabase ? 0 : 1` to `module "networking"` and `module "aurora"`. (2) Add new `module "supabase"` with `count = var.use_supabase ? 1 : 0`. (3) Introduce a `locals` block that computes `db_env_vars` and `db_vpc_config` once based on `var.use_supabase`; every Aurora-using Lambda module reads from those locals so per-module edits are mechanical. (4) Pass conditional `aurora_master_user_secret_arn` and new `supabase_master_user_secret_arn` into `module "iam_roles"`. (5) Adjust every cross-module reference from `module.aurora.*` to `one(module.aurora[*].*)` and `module.networking.*` to `one(module.networking[*].*)` to tolerate count=0. |
| `infrastructure/modules/iam_roles/variables.tf` | Add `variable "supabase_master_user_secret_arn"` (string, default `""`). Keep `aurora_master_user_secret_arn` as-is (already optional-friendly since count-guarded). |
| `infrastructure/modules/iam_roles/main.tf` | For every policy that grants `secretsmanager:GetSecretValue` on `var.aurora_master_user_secret_arn`, change the Resource list to `compact([var.aurora_master_user_secret_arn, var.supabase_master_user_secret_arn])` so whichever secret exists is the resource scoped. |
| `infrastructure/environments/prod/*.tfvars` | Explicitly add `use_supabase = false` to pin production. No behavioural change; this is a safety pin against future accidental propagation. |

---

## Decomposition notes

- **Why one branch, many stories:** per workspace CLAUDE.md, `/add-feature` dispatches stories serially onto one feature branch. All stories in this plan commit to that same branch. The final PR merges the full feature into `development`.
- **Why extract `_rls.py` before writing `_supabase.py`:** the RLS logic is sensitive (wrong GUC names silently zero all query results — see the migration 0007 docstring). Extracting first makes it a pure refactor with existing tests as the safety net; then both DB implementations can import the same tested code, so there is literally one implementation of the RLS bit.
- **Why locals in dev/main.tf for env vars:** without this, every Lambda module invocation in main.tf would need a ternary. With locals, the conditional is expressed once, and each module simply reads `local.db_env_vars`.
- **Why `one(module.aurora[*].*)`:** Terraform's `one()` function returns the single element of a 0-or-1 list or null, which is the canonical idiom for cross-module references when `count` is conditional. This replaces direct `module.aurora.*` references which fail at plan time when `count = 0`.

---

## Stories

### Story f1.0 — Pre-flight: pooler transaction-mode GUC proof

**Agent:** backenddeveloper (but the user is the one who runs the script — subagent writes it, user executes with the real password)

**Rationale:** The entire plan depends on `SET LOCAL app.requesting_user_id` surviving the Supabase pooler's transaction-mode semantics. If GUCs reset mid-statement under pooler, the design fails. Prove it on a 10-line script before investing in modules and plumbing.

**Files:**
- Create: `scripts/supabase_pooler_proof.py`

- [ ] **Step 1: Write the proof script**

```python
# scripts/supabase_pooler_proof.py
"""
One-shot proof: SET LOCAL app.requesting_user_id survives inside a transaction
on the Supabase pooler (transaction mode, port 6543).

Run manually before starting feature-1 implementation:

    SUPABASE_HOST='aws-0-eu-central-1.pooler.supabase.com' \
    SUPABASE_PORT=6543 \
    SUPABASE_DBNAME=postgres \
    SUPABASE_USERNAME='postgres.<project-ref>' \
    SUPABASE_PASSWORD='<password>' \
    python3 scripts/supabase_pooler_proof.py

Expected output:
    GUC observed in query: some-test-uuid
    PASS

Exit 0 on success; exit 1 on failure.
"""

from __future__ import annotations

import os
import sys

import psycopg2


def main() -> int:
    conn = psycopg2.connect(
        host=os.environ["SUPABASE_HOST"],
        port=int(os.environ["SUPABASE_PORT"]),
        dbname=os.environ["SUPABASE_DBNAME"],
        user=os.environ["SUPABASE_USERNAME"],
        password=os.environ["SUPABASE_PASSWORD"],
        sslmode="require",
    )
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute("BEGIN")
            cur.execute("SET LOCAL app.requesting_user_id = 'some-test-uuid'")
            cur.execute("SELECT current_setting('app.requesting_user_id', true)")
            value = cur.fetchone()[0]
            cur.execute("COMMIT")

        print(f"GUC observed in query: {value}")
        if value == "some-test-uuid":
            print("PASS")
            return 0
        print("FAIL — GUC did not survive to the SELECT")
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Hand to user with exact invocation**

Tell the user: "Create the Supabase secret in AWS Secrets Manager OR export the five env vars above and run `python3 scripts/supabase_pooler_proof.py`. If it prints `PASS`, continue with f1.1. If it prints `FAIL`, stop and surface the output."

- [ ] **Step 3: User confirms PASS before f1.1 proceeds**

If PASS: mark story complete, proceed to f1.1.
If FAIL: halt. Open a question in `questions/` describing the failure mode. Do NOT guess; the design assumption is wrong and a different pooler mode or direct-connection path needs to be evaluated.

- [ ] **Step 4: Commit**

```bash
git add scripts/supabase_pooler_proof.py
git commit -m "feat(feature-1): add Supabase pooler GUC proof script (story f1.0)"
```

**Acceptance:** Script committed; user has run it; `PASS` confirmed.

---

### Story f1.1 — Terraform variable plumbing in dev environment

**Agent:** backenddeveloper

**Files:**
- Modify: `infrastructure/environments/dev/variables.tf`
- Modify: `infrastructure/environments/dev/dev.tfvars`
- Modify: `infrastructure/environments/prod/prod.tfvars` (safety-pin only)

- [ ] **Step 1: Add variables to dev/variables.tf**

Append to the existing `infrastructure/environments/dev/variables.tf`:

```hcl
# ---------------------------------------------------------------------------
# Supabase alternative — feature 1
#
# When use_supabase = true, the dev environment deploys Supabase credential
# plumbing instead of Aurora + private VPC. See features/feature-1-*.md.
# ---------------------------------------------------------------------------

variable "use_supabase" {
  description = "When true, dev deploys Supabase credential plumbing; networking + aurora modules get count=0. Mutually exclusive with Aurora. Prod always false."
  type        = bool
  default     = false
}

variable "supabase_host" {
  description = "Supabase pooler host (e.g. aws-0-eu-central-1.pooler.supabase.com). Required when use_supabase = true; ignored otherwise."
  type        = string
  default     = ""
}

variable "supabase_port" {
  description = "Supabase pooler port. 6543 = transaction mode (Lambda-safe); 5432 = session mode."
  type        = number
  default     = 6543
}

variable "supabase_dbname" {
  description = "Supabase database name."
  type        = string
  default     = "postgres"
}

variable "supabase_username" {
  description = "Supabase pooler username. For the pooler endpoint this is 'postgres.<project-ref>'. Required when use_supabase = true."
  type        = string
  default     = ""
}
```

- [ ] **Step 2: Set dev.tfvars for Supabase-mode default**

Append to `infrastructure/environments/dev/dev.tfvars`:

```hcl
# ---------------------------------------------------------------------------
# Supabase mode — feature 1
#
# Aurora + VPC were manually torn down on 2026-11-08 to cut the AWS bill.
# use_supabase = true is the real dev state; flipping back to false
# re-provisions Aurora + VPC in a single terraform apply.
# ---------------------------------------------------------------------------
use_supabase       = true
supabase_host      = ""  # USER FILLS IN: aws-0-eu-central-1.pooler.supabase.com-style host
supabase_username  = ""  # USER FILLS IN: postgres.<project-ref>
```

- [ ] **Step 3: Pin prod to false**

Append to `infrastructure/environments/prod/prod.tfvars`:

```hcl
# ---------------------------------------------------------------------------
# Production is Aurora-only. use_supabase is pinned false to prevent
# accidental propagation of the dev-only Supabase path. See feature 1.
# ---------------------------------------------------------------------------
use_supabase = false
```

- [ ] **Step 4: Verify both environments parse**

Run from `infrastructure/environments/dev/`:
```bash
terraform fmt -check && terraform validate
```
Expected: `Success! The configuration is valid.`

Run from `infrastructure/environments/prod/`:
```bash
terraform fmt -check && terraform validate
```
Expected: `Success! The configuration is valid.`

- [ ] **Step 5: Commit**

```bash
git add infrastructure/environments/dev/variables.tf infrastructure/environments/dev/dev.tfvars infrastructure/environments/prod/prod.tfvars
git commit -m "feat(feature-1): add use_supabase flag + supabase_* variables (story f1.1)"
```

**Acceptance:** Both environments validate. Dev has `use_supabase = true` with placeholders; prod pinned false.

---

### Story f1.2 — New `supabase` Terraform module

**Agent:** backenddeveloper

**Rationale:** A tiny module that owns exactly one AWS Secrets Manager secret. Supabase is managed outside AWS; this module only provides the glue so Lambdas can fetch the credential through the same path they use for Aurora today.

**Files:**
- Create: `infrastructure/modules/supabase/main.tf`
- Create: `infrastructure/modules/supabase/variables.tf`
- Create: `infrastructure/modules/supabase/outputs.tf`
- Create: `infrastructure/modules/supabase/tests/supabase.tftest.hcl`

- [ ] **Step 1: Write the module variables**

Create `infrastructure/modules/supabase/variables.tf`:

```hcl
variable "environment" {
  description = "Environment name (dev or prod). Used in the Secrets Manager secret name."
  type        = string
}

variable "host" {
  description = "Supabase pooler host (e.g. aws-0-eu-central-1.pooler.supabase.com)."
  type        = string
}

variable "port" {
  description = "Supabase pooler port. 6543 (transaction mode) is the Lambda-safe default."
  type        = number
  default     = 6543
}

variable "dbname" {
  description = "Supabase database name."
  type        = string
  default     = "postgres"
}

variable "username" {
  description = "Supabase pooler username. For the pooler endpoint, this is 'postgres.<project-ref>'."
  type        = string
}
```

- [ ] **Step 2: Write the module main.tf**

Create `infrastructure/modules/supabase/main.tf`:

```hcl
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
# Supabase credential secret
#
# Mirrors the Aurora master-user-secret contract: the secret JSON contains
# only {"username": ..., "password": ...}. Host / port / dbname live in the
# Lambda env vars (injected by the dev environment root). This split keeps
# the Lambda-side contract uniform across Aurora and Supabase paths.
#
# The password field is intentionally left empty on first create — the user
# or operator populates it via:
#
#   aws secretsmanager put-secret-value \
#     --secret-id knotify/<env>/supabase_credential \
#     --secret-string '{"username":"postgres.<project-ref>","password":"<pw>"}'
#
# so the password never enters Terraform state.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "credential" {
  name = "knotify/${var.environment}/supabase_credential"

  description = "Supabase database credential pair ({username, password}). Host/port/dbname come from Lambda env vars."
}

resource "aws_secretsmanager_secret_version" "credential_placeholder" {
  secret_id = aws_secretsmanager_secret.credential.id

  secret_string = jsonencode({
    username = var.username
    password = ""
  })

  lifecycle {
    # Operator populates the real password via put-secret-value. Ignore future
    # drift on secret_string so terraform apply does not clobber it.
    ignore_changes = [secret_string]
  }
}
```

- [ ] **Step 3: Write outputs**

Create `infrastructure/modules/supabase/outputs.tf`:

```hcl
output "secret_arn" {
  description = "ARN of the Supabase credential secret."
  value       = aws_secretsmanager_secret.credential.arn
}

output "secret_name" {
  description = "Name of the Supabase credential secret."
  value       = aws_secretsmanager_secret.credential.name
}

output "host" {
  description = "Supabase pooler host — re-emitted for env-var injection."
  value       = var.host
}

output "port" {
  description = "Supabase pooler port — re-emitted for env-var injection."
  value       = var.port
}

output "dbname" {
  description = "Supabase database name — re-emitted for env-var injection."
  value       = var.dbname
}
```

- [ ] **Step 4: Write the Terraform test**

Create `infrastructure/modules/supabase/tests/supabase.tftest.hcl`:

```hcl
# Terraform test — proves the module plans to exactly one secret + one version
# with the correct name shape, and that the placeholder password is empty.

variables {
  environment = "dev"
  host        = "aws-0-eu-central-1.pooler.supabase.com"
  port        = 6543
  dbname      = "postgres"
  username    = "postgres.testref"
}

run "plan_shape" {
  command = plan

  assert {
    condition     = aws_secretsmanager_secret.credential.name == "knotify/dev/supabase_credential"
    error_message = "Secret name must be knotify/<env>/supabase_credential"
  }

  assert {
    condition     = jsondecode(aws_secretsmanager_secret_version.credential_placeholder.secret_string).password == ""
    error_message = "Initial password must be empty — operator populates via put-secret-value"
  }

  assert {
    condition     = jsondecode(aws_secretsmanager_secret_version.credential_placeholder.secret_string).username == "postgres.testref"
    error_message = "Initial username must match var.username"
  }

  assert {
    condition     = output.secret_arn != ""
    error_message = "secret_arn output must not be empty"
  }
}
```

- [ ] **Step 5: Run the Terraform test**

From `infrastructure/modules/supabase/`:
```bash
terraform init -backend=false && terraform test
```
Expected: `1 passed, 0 failed.`

- [ ] **Step 6: Commit**

```bash
git add infrastructure/modules/supabase/
git commit -m "feat(feature-1): new supabase module (credential secret only) (story f1.2)"
```

**Acceptance:** `terraform test` passes with exactly one plan_shape run.

---

### Story f1.3 — Make networking + aurora modules conditional

**Agent:** backenddeveloper

**Rationale:** The two big cost drivers go dormant when the flag is on. The subagent needs to find every cross-module reference to `module.networking.*` and `module.aurora.*` and wrap them in `one(module.<x>[*].*)` so the references survive `count = 0`.

**Files:**
- Modify: `infrastructure/environments/dev/main.tf`

- [ ] **Step 1: Add count guard to networking + aurora module blocks**

In `infrastructure/environments/dev/main.tf`, modify the `module "networking"` block to prepend `count = var.use_supabase ? 0 : 1` as the first attribute inside the block. Same for `module "aurora"`.

Example for networking:
```hcl
module "networking" {
  count = var.use_supabase ? 0 : 1

  source      = "../../modules/networking"
  environment = var.environment
}
```

Example for aurora (keep all existing inputs as-is; just prepend `count`):
```hcl
module "aurora" {
  count = var.use_supabase ? 0 : 1

  source = "../../modules/aurora"
  # ... all existing inputs unchanged ...
}
```

- [ ] **Step 2: Rewrite every reference to those modules**

Run this grep to find every reference that needs updating:
```bash
grep -nE 'module\.(networking|aurora)\.' infrastructure/environments/dev/main.tf
```

For each hit, replace the reference with the `one(...)` wrapped form. Examples (REPEATED here because the subagent may read this story out of order — do not paraphrase):

- `module.aurora.master_user_secret_arn` → `one(module.aurora[*].master_user_secret_arn)`
- `module.aurora.cluster_endpoint` → `one(module.aurora[*].cluster_endpoint)`
- `module.aurora.port` → `one(module.aurora[*].port)`
- `module.aurora.database_name` → `one(module.aurora[*].database_name)`
- `module.networking.aurora_security_group_id` → `one(module.networking[*].aurora_security_group_id)`
- `module.networking.db_subnet_group_name` → `one(module.networking[*].db_subnet_group_name)`
- `module.networking.lambda_security_group_id` → `one(module.networking[*].lambda_security_group_id)`
- `module.networking.private_subnet_ids` → `one(module.networking[*].private_subnet_ids)`

Apply the same transform to EVERY reference. When `count = 1` the `one(...)` call returns the single element; when `count = 0` it returns `null`, which is exactly what the Lambda module's `vpc_config` variable expects (default is `null`).

- [ ] **Step 3: terraform validate — expected to still parse, references to module.aurora.* in the IAM block will fail**

This step is expected to surface compilation errors that the next step resolves. Run:
```bash
cd infrastructure/environments/dev && terraform validate
```
Expected: validation passes IF every reference was transformed. If any `module.aurora.foo` or `module.networking.foo` (without the `[*]` splat) remains, validate prints the file:line and error. Fix each until validate passes.

- [ ] **Step 4: terraform plan with use_supabase=false — assert no diff vs. baseline**

```bash
cd infrastructure/environments/dev && terraform plan -var 'use_supabase=false' -out /tmp/plan.aurora.binary
terraform show -no-color /tmp/plan.aurora.binary > /tmp/plan.aurora.txt
grep -E '^\s*(#|~|-|\+)\s+(module|resource)' /tmp/plan.aurora.txt | head -40
```
Expected: `Plan: 0 to add, 0 to change, 0 to destroy.` This proves the count-guard rewrite is a pure no-op when the flag is off.

If the plan shows diffs, inspect them — they are bugs in the rewrite. Fix before committing.

- [ ] **Step 5: terraform plan with use_supabase=true — assert Aurora + networking are destroyed**

```bash
cd infrastructure/environments/dev && terraform plan -var 'use_supabase=true' -var 'supabase_host=placeholder' -var 'supabase_username=postgres.placeholder' -out /tmp/plan.supabase.binary
terraform show -no-color /tmp/plan.supabase.binary | grep -cE '^\s*# (module\.aurora|module\.networking)'
```
Expected: non-zero count of `# module.aurora.*` and `# module.networking.*` lines, all marked for destroy (shown as `will be destroyed`).

NOTE: this plan will still have errors referencing unresolved Supabase outputs because f1.4 hasn't landed yet — those are expected. Capture only the Aurora/networking destroy plan lines and move on.

- [ ] **Step 6: Commit**

```bash
git add infrastructure/environments/dev/main.tf
git commit -m "feat(feature-1): count-guard networking + aurora modules on use_supabase (story f1.3)"
```

**Acceptance:** `terraform plan -var 'use_supabase=false'` produces zero diff. `terraform plan -var 'use_supabase=true'` schedules destroy of every aurora + networking resource.

---

### Story f1.4 — Wire new `supabase` module in dev main.tf; add locals for env vars + vpc_config

**Agent:** backenddeveloper

**Rationale:** The locals block is the trick that keeps per-Lambda-module edits minimal. Defining `local.db_env_vars` and `local.db_vpc_config` once means each existing Lambda module invocation can swap in `local.db_env_vars` for its current `environment_variables` block, and `local.db_vpc_config` for its current `vpc_config` — no ternary per Lambda.

**Files:**
- Modify: `infrastructure/environments/dev/main.tf`

- [ ] **Step 1: Add the `supabase` module block**

Insert into `infrastructure/environments/dev/main.tf` AFTER the existing `module "aurora"` block (so declaration order matches use order):

```hcl
# ---------------------------------------------------------------------------
# Supabase credential secret — feature 1.
# Deployed only when use_supabase = true. Mutually exclusive with Aurora.
# ---------------------------------------------------------------------------

module "supabase" {
  count = var.use_supabase ? 1 : 0

  source = "../../modules/supabase"

  environment = var.environment
  host        = var.supabase_host
  port        = var.supabase_port
  dbname      = var.supabase_dbname
  username    = var.supabase_username
}
```

- [ ] **Step 2: Add the `locals` block for DB-side env vars + vpc_config**

Insert into `infrastructure/environments/dev/main.tf` immediately after the two `provider "aws"` blocks and BEFORE the `module "networking"` block:

```hcl
# ---------------------------------------------------------------------------
# Database-side Lambda inputs — feature 1.
#
# These locals are the single source of truth for every Aurora-using Lambda
# module's env vars and vpc_config. The use_supabase flag picks which branch
# resolves; every Lambda module below reads from these locals instead of
# carrying its own ternary.
#
# INVARIANT: when use_supabase = true, db_vpc_config is null (Lambdas run
# outside the VPC and reach Supabase over the public internet). When false,
# vpc_config is populated from the networking module's outputs.
# ---------------------------------------------------------------------------

locals {
  db_env_vars = var.use_supabase ? {
    USE_SUPABASE      = "true"
    SUPABASE_HOST     = one(module.supabase[*].host)
    SUPABASE_PORT     = tostring(one(module.supabase[*].port))
    SUPABASE_DBNAME   = one(module.supabase[*].dbname)
    DB_SECRET_ARN     = one(module.supabase[*].secret_arn)
    } : {
    USE_SUPABASE      = "false"
    AURORA_HOST       = one(module.aurora[*].cluster_endpoint)
    AURORA_PORT       = tostring(one(module.aurora[*].port))
    AURORA_DBNAME     = one(module.aurora[*].database_name)
    DB_SECRET_ARN     = one(module.aurora[*].master_user_secret_arn)
  }

  db_vpc_config = var.use_supabase ? null : {
    subnet_ids         = one(module.networking[*].private_subnet_ids)
    security_group_ids = [one(module.networking[*].lambda_security_group_id)]
  }
}
```

- [ ] **Step 3: Rewrite every Lambda module invocation to read from locals**

Run this grep to find every Lambda module invocation in `dev/main.tf`:
```bash
grep -nE 'source\s*=\s*"\.\./\.\./modules/(anonymize_chat_messages|blocks|bookmarks|chat_resolver|cognito_post_confirmation|cognito_pre_token_generation|cognito_user_state|db_migrator|deactivate_chat_rooms|delete_dynamodb_personal_data|deletion_initiator|friends|hard_delete_user_chat_messages|hard_purge|match|notifications_publisher|profile|push_fanout|push_tokens|refresh_deck_view|room_state_publisher|soft_delete_aurora|stale_token_cleanup|validate_deletion_request|write_audit_log)"' infrastructure/environments/dev/main.tf
```

For each Lambda module invocation that currently declares `environment_variables = { AURORA_HOST = ..., AURORA_PORT = ..., ... }`, replace that block with:
```hcl
  environment_variables = local.db_env_vars
```

For each Lambda module invocation that currently declares a `vpc_config = { subnet_ids = ..., security_group_ids = ... }`, replace that block with:
```hcl
  vpc_config = local.db_vpc_config
```

**Skip** the Lambdas that do NOT touch Aurora: `delete_dynamodb_personal_data`, `notifications_publisher`, `push_fanout`, `push_tokens`, `room_state_publisher`, `stale_token_cleanup`. If any of these are in a VPC, they likely use the lambda_security_group only for AWS-service VPC-endpoint access; coordinate with the subagent's reading of those modules before touching them.

If a Lambda module currently passes bespoke env vars beyond the AURORA_* set (e.g. a table ARN, a Cognito pool ID), KEEP those entries. Use `merge(local.db_env_vars, { EXTRA_KEY = ... })`.

- [ ] **Step 4: Update iam_roles module invocation to pass both secret ARNs**

In the `module "iam_roles"` block in `dev/main.tf`, change the `aurora_master_user_secret_arn` input from `module.aurora.master_user_secret_arn` (which fails when count=0) to `one(module.aurora[*].master_user_secret_arn)`, and add a new input `supabase_master_user_secret_arn = one(module.supabase[*].secret_arn)`. The actual IAM policy edits land in f1.5.

```hcl
module "iam_roles" {
  source = "../../modules/iam_roles"

  environment                     = var.environment
  aurora_master_user_secret_arn   = one(module.aurora[*].master_user_secret_arn)
  supabase_master_user_secret_arn = one(module.supabase[*].secret_arn)

  # ... all other existing inputs unchanged ...
}
```

- [ ] **Step 5: Validate**

```bash
cd infrastructure/environments/dev && terraform validate
```
Expected: `Success!`

If validate reports "The argument 'supabase_master_user_secret_arn' is not expected here", that's because f1.5 has not landed yet. Add a temporary stub in `modules/iam_roles/variables.tf`:
```hcl
variable "supabase_master_user_secret_arn" {
  type    = string
  default = ""
}
```
Move the real IAM wiring of this variable into f1.5 but let the plumbing compile here.

- [ ] **Step 6: Plan with use_supabase=true, assert new Supabase secret + updated env vars**

```bash
cd infrastructure/environments/dev && terraform plan -var 'use_supabase=true' -var 'supabase_host=aws-0-eu-central-1.pooler.supabase.com' -var 'supabase_username=postgres.placeholder' -out /tmp/plan.supabase.full.binary
terraform show -no-color /tmp/plan.supabase.full.binary | grep -E 'aws_secretsmanager_secret\.credential|SUPABASE_HOST'
```
Expected: one `aws_secretsmanager_secret.credential` to be created, every Aurora-using Lambda's env block includes `SUPABASE_HOST = "aws-0-eu-central-1.pooler.supabase.com"`.

- [ ] **Step 7: Commit**

```bash
git add infrastructure/environments/dev/main.tf infrastructure/modules/iam_roles/variables.tf
git commit -m "feat(feature-1): wire supabase module + db-side locals in dev main.tf (story f1.4)"
```

**Acceptance:** Plan with `use_supabase=true` includes the Supabase secret and SUPABASE_* env vars on every Aurora-using Lambda; plan with `use_supabase=false` is a no-op vs. baseline.

---

### Story f1.5 — IAM roles: conditional secret ARN

**Agent:** backenddeveloper

**Rationale:** Every Lambda role that reads the DB credential today has `secretsmanager:GetSecretValue` scoped to the Aurora master secret ARN. When Aurora is count-guarded to zero, that ARN is empty. The policy needs to scope to whichever secret exists.

**Files:**
- Modify: `infrastructure/modules/iam_roles/variables.tf`
- Modify: `infrastructure/modules/iam_roles/main.tf`

- [ ] **Step 1: Promote the supabase variable from f1.4 stub to a documented first-class variable**

Replace the stub added in f1.4 with:
```hcl
variable "supabase_master_user_secret_arn" {
  description = "ARN of the Supabase credential secret. Empty string when use_supabase = false. Added to secretsmanager:GetSecretValue policies alongside the Aurora secret ARN via compact() so whichever secret is live for the current flag state is granted."
  type        = string
  default     = ""
}
```

- [ ] **Step 2: Find every policy Resource list that references the Aurora secret**

Run:
```bash
grep -nE '"Resource".*aurora_master_user_secret_arn' infrastructure/modules/iam_roles/main.tf
```

For every hit, the current line looks like one of:
```hcl
Resource = [var.aurora_master_user_secret_arn]
# or
Resource = var.aurora_master_user_secret_arn
```

Rewrite each to:
```hcl
Resource = compact([var.aurora_master_user_secret_arn, var.supabase_master_user_secret_arn])
```

`compact()` strips empty strings, so when Aurora is live the list has one ARN; when Supabase is live it has one ARN; both can never be live simultaneously because of the mutually-exclusive flag.

**Edge case:** if any IAM statement lists `secretsmanager:*` on a wildcard resource like `"*"` with a Condition, do NOT rewrite — that's over-broad. Leave untouched and surface in a questions/ file.

- [ ] **Step 3: Run the iam_roles module's existing tests**

```bash
cd infrastructure/modules/iam_roles && terraform init -backend=false && terraform test
```
Expected: all existing tests pass.

If no tests exist for this module, skip to the dev-env-level validation in step 4.

- [ ] **Step 4: Validate dev environment**

```bash
cd infrastructure/environments/dev && terraform validate && terraform plan -var 'use_supabase=true' -var 'supabase_host=test.pooler.supabase.com' -var 'supabase_username=postgres.test' | grep -A2 'secretsmanager:GetSecretValue'
```
Expected: the plan shows Supabase secret ARN appearing in `GetSecretValue` resource lists; no Aurora secret ARN appears (because Aurora is count=0).

Repeat with `use_supabase=false`:
```bash
terraform plan -var 'use_supabase=false' | grep -A2 'secretsmanager:GetSecretValue'
```
Expected: Aurora secret ARN appears; no Supabase secret ARN appears (Supabase module is count=0).

- [ ] **Step 5: Commit**

```bash
git add infrastructure/modules/iam_roles/
git commit -m "feat(feature-1): iam_roles conditional secret ARN via compact() (story f1.5)"
```

**Acceptance:** With flag true, Lambda roles can read Supabase secret only. With flag false, Aurora secret only. No other IAM scope widening.

---

### Story f1.6 — Extract RLS logic to `_rls.py`

**Agent:** backenddeveloper

**Rationale:** The RLS logic is shared across Aurora and Supabase paths. Extracting it first — as a pure refactor validated by the existing test suite — means both paths import one implementation, so there is literally one RLS code path to audit.

**Files:**
- Create: `infrastructure/src/layers/db/knotify_db/_rls.py`
- Modify: `infrastructure/src/layers/db/knotify_db/_db.py`

- [ ] **Step 1: Write the failing test (new test file)**

Create `infrastructure/src/layers/db/tests/test_rls_shared.py`:

```python
"""Prove _rls.py owns the GUC constants and context manager, importable
standalone without any psycopg2 / boto3 side effects from _db.py."""

import pytest


def test_rls_module_exposes_guc_constants():
    from knotify_db import _rls

    assert _rls._GUC_USER_ID == "app.requesting_user_id"
    assert _rls._GUC_USER_SEX == "app.requesting_user_sex"


def test_rls_module_exposes_set_rls_context_signature():
    import inspect

    from knotify_db import _rls

    sig = inspect.signature(_rls.set_rls_context)
    assert list(sig.parameters) == ["conn", "user_id", "user_sex"]


def test_rls_module_exposes_rls_context_signature():
    import inspect

    from knotify_db import _rls

    sig = inspect.signature(_rls.rls_context)
    assert list(sig.parameters) == ["conn", "user_id", "user_sex"]
```

- [ ] **Step 2: Run the test; expect import error**

```bash
cd infrastructure/src/layers/db && pytest tests/test_rls_shared.py -v
```
Expected: `ModuleNotFoundError: No module named 'knotify_db._rls'` — three test failures.

- [ ] **Step 3: Create `_rls.py` by lifting from `_db.py`**

Create `infrastructure/src/layers/db/knotify_db/_rls.py`. Copy VERBATIM from `_db.py` these items (DO NOT paraphrase — one character off on the GUC names silently zeros all query results via the RLS policy in migration 0007):

```python
"""
knotify_db._rls — shared RLS GUC helpers used by both the Aurora path
(_db.py) and the Supabase path (_supabase.py). One implementation, so the
sensitive piece (GUC names must match migration 0007 verbatim) has exactly
one place to audit.

Public surface (re-exported by knotify_db/__init__.py):
  set_rls_context(conn, user_id, user_sex)
  rls_context(conn, user_id, user_sex)  [context manager]

GUC names match migration 0007 lines 115-116 verbatim — any deviation
causes the policy to read NULL and fail-closed (zero rows, no error).
"""

from __future__ import annotations

from contextlib import contextmanager

# GUC names from migration 0007 lines 115-116 — must not be changed.
_GUC_USER_ID = "app.requesting_user_id"
_GUC_USER_SEX = "app.requesting_user_sex"


def set_rls_context(conn, user_id, user_sex) -> None:
    # VERBATIM: lift the body currently in _db.set_rls_context.
    # The subagent reads _db.py's current implementation and copies it here
    # with zero behavioural changes. Signatures and SQL strings are preserved.
    ...


@contextmanager
def rls_context(conn, user_id, user_sex):
    # VERBATIM: lift the body currently in _db.rls_context.
    ...
```

**Note to subagent:** the `...` placeholders above are intentional — this plan does not inline the full bodies because they are long and must be lifted character-identical from `_db.py`. Read `knotify_db/_db.py` on the current branch, locate `set_rls_context` and `rls_context`, copy the bodies verbatim into `_rls.py`. Do NOT retype from memory.

- [ ] **Step 4: Rewrite `_db.py` to re-export from `_rls.py`**

Edit `infrastructure/src/layers/db/knotify_db/_db.py`:
1. Delete the `_GUC_USER_ID` and `_GUC_USER_SEX` module-level constants
2. Delete the `set_rls_context` function body
3. Delete the `rls_context` function body (including its `@contextmanager` decorator and import)
4. At the top of the file, add: `from knotify_db._rls import set_rls_context, rls_context  # noqa: F401 — re-exported`

The `# noqa: F401` suppresses the "imported but unused" lint — these are re-exported through the module's public surface.

- [ ] **Step 5: Run the new RLS tests + the existing db tests**

```bash
cd infrastructure/src/layers/db && pytest tests/ -v
```
Expected: all tests in `test_rls_shared.py` pass, all tests in `test_knotify_db_unit.py` continue to pass, `test_knotify_db_integration.py` continues to pass (requires local Postgres fixture per conftest.py).

- [ ] **Step 6: Commit**

```bash
git add infrastructure/src/layers/db/knotify_db/_rls.py infrastructure/src/layers/db/knotify_db/_db.py infrastructure/src/layers/db/tests/test_rls_shared.py
git commit -m "refactor(feature-1): extract RLS GUC helpers to knotify_db._rls (story f1.6)"
```

**Acceptance:** All existing tests still pass (zero behaviour change). `_rls.py` exists and is imported by `_db.py`.

---

### Story f1.7 — Create `_supabase.py`

**Agent:** backenddeveloper

**Rationale:** Same public surface as `_db.get_connection`, different connection builder. Imports RLS helpers from `_rls.py`.

**Files:**
- Create: `infrastructure/src/layers/db/knotify_db/_supabase.py`
- Create: `infrastructure/src/layers/db/tests/test_supabase_unit.py`

- [ ] **Step 1: Write the failing unit tests**

Create `infrastructure/src/layers/db/tests/test_supabase_unit.py`:

```python
"""Unit tests for knotify_db._supabase.get_connection — dict-path only
(no AWS calls). The Secrets Manager path is exercised in integration tests
run manually against the real Supabase project post-deploy."""

from __future__ import annotations

from unittest import mock

import pytest


class _FakeConn:
    def close(self):
        pass


def test_get_connection_dict_path_forwards_all_five_fields():
    with mock.patch("knotify_db._supabase.psycopg2.connect", return_value=_FakeConn()) as fake:
        from knotify_db._supabase import get_connection

        params = {
            "host": "aws-0-eu-central-1.pooler.supabase.com",
            "port": 6543,
            "dbname": "postgres",
            "username": "postgres.testref",
            "password": "pw",
        }
        conn = get_connection(params)
        assert conn is not None

        fake.assert_called_once()
        kwargs = fake.call_args.kwargs
        assert kwargs["host"] == "aws-0-eu-central-1.pooler.supabase.com"
        assert kwargs["port"] == 6543
        assert kwargs["dbname"] == "postgres"
        assert kwargs["user"] == "postgres.testref"
        assert kwargs["password"] == "pw"


def test_get_connection_dict_path_enforces_ssl_require():
    with mock.patch("knotify_db._supabase.psycopg2.connect", return_value=_FakeConn()) as fake:
        from knotify_db._supabase import get_connection

        get_connection({
            "host": "x",
            "port": 6543,
            "dbname": "postgres",
            "username": "postgres.testref",
            "password": "pw",
        })

        assert fake.call_args.kwargs["sslmode"] == "require"


def test_get_connection_secrets_manager_path_reads_env_vars():
    import os

    with mock.patch("knotify_db._supabase.psycopg2.connect", return_value=_FakeConn()) as fake, \
         mock.patch("knotify_db._supabase.boto3.client") as fake_boto:
        fake_boto.return_value.get_secret_value.return_value = {
            "SecretString": '{"username":"postgres.ref","password":"pw"}'
        }

        os.environ["SUPABASE_HOST"] = "aws-0-eu-central-1.pooler.supabase.com"
        os.environ["SUPABASE_PORT"] = "6543"
        os.environ["SUPABASE_DBNAME"] = "postgres"
        try:
            from knotify_db._supabase import get_connection

            get_connection("arn:aws:secretsmanager:eu-central-1:111:secret:knotify/dev/supabase_credential")
        finally:
            del os.environ["SUPABASE_HOST"]
            del os.environ["SUPABASE_PORT"]
            del os.environ["SUPABASE_DBNAME"]

        assert fake.call_args.kwargs["host"] == "aws-0-eu-central-1.pooler.supabase.com"
        assert fake.call_args.kwargs["port"] == 6543
        assert fake.call_args.kwargs["dbname"] == "postgres"
        assert fake.call_args.kwargs["user"] == "postgres.ref"
        assert fake.call_args.kwargs["password"] == "pw"
        assert fake.call_args.kwargs["sslmode"] == "require"


def test_get_connection_secrets_manager_path_missing_env_raises():
    import os

    with mock.patch("knotify_db._supabase.boto3.client") as fake_boto:
        fake_boto.return_value.get_secret_value.return_value = {
            "SecretString": '{"username":"postgres.ref","password":"pw"}'
        }
        # Ensure SUPABASE_HOST is not set
        os.environ.pop("SUPABASE_HOST", None)

        from knotify_db._supabase import get_connection

        with pytest.raises(EnvironmentError) as excinfo:
            get_connection("arn:aws:secretsmanager:eu-central-1:111:secret:x")

        assert "SUPABASE_HOST" in str(excinfo.value)


def test_rls_helpers_are_re_exported_from_rls_module():
    from knotify_db import _supabase, _rls

    assert _supabase.set_rls_context is _rls.set_rls_context
    assert _supabase.rls_context is _rls.rls_context
```

- [ ] **Step 2: Run tests; expect import error**

```bash
cd infrastructure/src/layers/db && pytest tests/test_supabase_unit.py -v
```
Expected: `ModuleNotFoundError: No module named 'knotify_db._supabase'`.

- [ ] **Step 3: Write `_supabase.py`**

Create `infrastructure/src/layers/db/knotify_db/_supabase.py`:

```python
"""
knotify_db._supabase — Supabase hosted Postgres connection builder.

Public surface (identical to _db, re-exported by knotify_db/__init__.py):
  get_connection(secret_or_env) -> psycopg2.extensions.connection
  set_rls_context — re-exported from _rls
  rls_context     — re-exported from _rls

Design:
  * Same dual-path contract as _db.get_connection: dict for local/test,
    Secrets Manager ARN/name for the Lambda runtime.
  * Secret payload is {"username": ..., "password": ...} — identical to
    the Aurora master secret contract, so the Lambda-side auth path does
    not branch on which DB is live.
  * Connection endpoint (host, port, dbname) comes from env vars
    SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DBNAME injected by Terraform.
    Mirroring the Aurora contract (AURORA_HOST / PORT / DBNAME) keeps the
    design symmetric.
  * Uses sslmode=require. Supabase enforces TLS; attempting plaintext
    fails at the pooler.
  * Targets the Supabase connection pooler (port 6543 transaction mode).
    Lambda concurrency bursts can exhaust direct-connection limits;
    transaction mode is the Lambda-safe default. The SET LOCAL RLS pattern
    wraps inside BEGIN...COMMIT (proven by scripts/supabase_pooler_proof.py
    in story f1.0), so transaction mode is compatible.

Re-exports set_rls_context and rls_context from _rls so callers get a
single consistent surface regardless of which DB backend is live.
"""

from __future__ import annotations

import json
import os
from typing import Union

import psycopg2
import psycopg2.extras

# Register UUID adapter once at import time — matches _db.py behaviour so
# callers can pass uuid.UUID without casting to str.
psycopg2.extras.register_uuid()

# Re-export RLS helpers (identical semantics on both backends).
from knotify_db._rls import set_rls_context, rls_context  # noqa: F401

# boto3 is deferred because local test environments may not have it on PATH;
# importing at module top would break the dict-path tests.


def get_connection(secret_or_env: Union[dict, str]) -> psycopg2.extensions.connection:
    """
    Return a psycopg2 connection to Supabase Postgres.

    Args:
        secret_or_env: Either a dict with the five connection fields
            (host, port, dbname, username, password) — used directly for
            local dev / unit tests — or a string Secrets Manager ARN/name
            fetched at Lambda runtime.

    Lambda runtime path:
        - Secret JSON: {"username": ..., "password": ...}
        - Env vars: SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DBNAME.
        - Missing env var raises EnvironmentError with a precise message
          so misconfigured Lambdas fail loudly at startup.
    """
    if isinstance(secret_or_env, dict):
        host = secret_or_env["host"]
        port = int(secret_or_env["port"])
        dbname = secret_or_env["dbname"]
        username = secret_or_env["username"]
        password = secret_or_env["password"]
    else:
        import boto3  # deferred — see module docstring

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_or_env)
        creds = json.loads(response["SecretString"])

        try:
            host = os.environ["SUPABASE_HOST"]
            port = int(os.environ["SUPABASE_PORT"])
            dbname = os.environ["SUPABASE_DBNAME"]
        except KeyError as missing:
            raise EnvironmentError(
                f"Supabase connection env var missing: {missing.args[0]}. "
                "Required: SUPABASE_HOST, SUPABASE_PORT, SUPABASE_DBNAME."
            ) from missing

        username = creds["username"]
        password = creds["password"]

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
        sslmode="require",
    )
```

- [ ] **Step 4: Run tests; expect all pass**

```bash
cd infrastructure/src/layers/db && pytest tests/test_supabase_unit.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/src/layers/db/knotify_db/_supabase.py infrastructure/src/layers/db/tests/test_supabase_unit.py
git commit -m "feat(feature-1): add knotify_db._supabase connection builder (story f1.7)"
```

**Acceptance:** `_supabase.py` passes unit tests. Same signature as `_db.get_connection`. RLS helpers re-exported from `_rls` identically.

---

### Story f1.8 — Wire conditional dispatch in `knotify_db/__init__.py`

**Agent:** backenddeveloper

**Rationale:** This is the one-line switch that routes every Lambda to either `_db` or `_supabase` based on the `USE_SUPABASE` env var.

**Files:**
- Modify: `infrastructure/src/layers/db/knotify_db/__init__.py`
- Create: `infrastructure/src/layers/db/tests/test_dispatch_unit.py`

- [ ] **Step 1: Write the failing dispatch tests**

Create `infrastructure/src/layers/db/tests/test_dispatch_unit.py`:

```python
"""Prove knotify_db.get_connection resolves to the correct backend based on
the USE_SUPABASE env var."""

import importlib
import os

import pytest


def _reload_knotify_db():
    import knotify_db
    return importlib.reload(knotify_db)


def test_dispatch_aurora_when_env_unset():
    os.environ.pop("USE_SUPABASE", None)
    mod = _reload_knotify_db()

    from knotify_db import _db
    assert mod.get_connection is _db.get_connection


def test_dispatch_aurora_when_env_false():
    os.environ["USE_SUPABASE"] = "false"
    try:
        mod = _reload_knotify_db()
        from knotify_db import _db
        assert mod.get_connection is _db.get_connection
    finally:
        del os.environ["USE_SUPABASE"]


def test_dispatch_supabase_when_env_true():
    os.environ["USE_SUPABASE"] = "true"
    try:
        mod = _reload_knotify_db()
        from knotify_db import _supabase
        assert mod.get_connection is _supabase.get_connection
    finally:
        del os.environ["USE_SUPABASE"]


def test_rls_helpers_always_come_from_rls_module():
    # Regardless of USE_SUPABASE, set_rls_context and rls_context must point
    # at the _rls implementation — one RLS code path, no backend branching.
    for value in (None, "false", "true"):
        if value is None:
            os.environ.pop("USE_SUPABASE", None)
        else:
            os.environ["USE_SUPABASE"] = value

        mod = _reload_knotify_db()
        from knotify_db import _rls

        assert mod.set_rls_context is _rls.set_rls_context
        assert mod.rls_context is _rls.rls_context

    os.environ.pop("USE_SUPABASE", None)
```

- [ ] **Step 2: Run tests; expect fail (current __init__.py unconditionally imports from _db)**

```bash
cd infrastructure/src/layers/db && pytest tests/test_dispatch_unit.py -v
```
Expected: `test_dispatch_supabase_when_env_true` fails (get_connection is still the Aurora version even when env is set).

- [ ] **Step 3: Rewrite `__init__.py` with conditional dispatch**

Replace the contents of `infrastructure/src/layers/db/knotify_db/__init__.py` with:

```python
"""
knotify_db — shared DB-access layer for Knotify Lambda functions.

Backend selection (feature 1):
    USE_SUPABASE env var picks which connection builder is exposed as
    get_connection. When "true", _supabase.get_connection; otherwise
    _db.get_connection (Aurora). The RLS helpers set_rls_context and
    rls_context always come from _rls — one implementation shared across
    backends.

Public API (unchanged from story 3.3):
  get_connection(secret_or_env)  — picks Aurora or Supabase at import time
  set_rls_context(conn, user_id, user_sex)
  rls_context(conn, user_id, user_sex)  [context manager]

Public API (unchanged from story 7.3):
  PREFERENCE_KEYS : list[str]
  encode_prefs(prefs: dict) -> list[float]
"""

from __future__ import annotations

import os

from knotify_db._rls import set_rls_context, rls_context
from knotify_db.prefs import PREFERENCE_KEYS, encode_prefs

if os.environ.get("USE_SUPABASE") == "true":
    from knotify_db._supabase import get_connection
else:
    from knotify_db._db import get_connection

__all__ = [
    "get_connection",
    "set_rls_context",
    "rls_context",
    "PREFERENCE_KEYS",
    "encode_prefs",
]
```

- [ ] **Step 4: Run all tests; expect everything green**

```bash
cd infrastructure/src/layers/db && pytest tests/ -v
```
Expected: all unit tests pass. Integration tests require a live Postgres and are run manually later.

- [ ] **Step 5: Commit**

```bash
git add infrastructure/src/layers/db/knotify_db/__init__.py infrastructure/src/layers/db/tests/test_dispatch_unit.py
git commit -m "feat(feature-1): conditional backend dispatch in knotify_db.__init__ (story f1.8)"
```

**Acceptance:** `USE_SUPABASE=true` routes `get_connection` to `_supabase`, otherwise to `_db`. RLS helpers unaffected.

---

### Story f1.9 — Rebuild and validate the DB Lambda layer zip

**Agent:** backenddeveloper

**Rationale:** The DB layer is packaged as a zip (`build/knotify-db-layer.zip`). It includes `knotify_db/`. The new `_supabase.py` + `_rls.py` must be in the shipped layer; otherwise Lambdas see the old code.

**Files:**
- Modify: `infrastructure/src/layers/db/build.sh` (if the file-list manifest needs updating — subagent verifies)
- Modify: `infrastructure/src/layers/db/layer_manifest.txt` (if present)
- Output: `build/knotify-db-layer.zip` (not committed — gitignored artifact)

- [ ] **Step 1: Read current build.sh to understand the manifest**

```bash
cat infrastructure/src/layers/db/build.sh
cat infrastructure/src/layers/db/layer_manifest.txt
```
Expected: shows which files are packaged. Confirm `knotify_db/__init__.py`, `knotify_db/_db.py`, `knotify_db/prefs.py` are listed. **If packaging is by directory (`cp -r knotify_db/` or similar), the new `_supabase.py` + `_rls.py` are picked up automatically and no manifest edit is needed.** If packaging is by explicit file list, add the two new files to the manifest.

- [ ] **Step 2: Run the build**

```bash
bash infrastructure/src/layers/db/build.sh
```
Expected: `build/knotify-db-layer.zip` created or refreshed. Script exits 0.

- [ ] **Step 3: Verify the new files are in the zip**

```bash
unzip -l build/knotify-db-layer.zip | grep -E 'knotify_db/_(rls|supabase)\.py'
```
Expected: two lines, one per file.

- [ ] **Step 4: Commit any manifest/build.sh edits**

```bash
git add infrastructure/src/layers/db/
git commit -m "chore(feature-1): include _rls.py + _supabase.py in db layer package (story f1.9)" || echo "No edits needed"
```

**Acceptance:** The layer zip contains both new files.

---

### Story f1.10 — Terraform plan verification: both flag values

**Agent:** backenddeveloper

**Rationale:** Before applying anything, prove both flag values plan cleanly. The two acceptance invariants from the PRD land here.

**Files:** none modified; plan artifacts captured for the PR description.

- [ ] **Step 1: Capture the use_supabase=true plan**

From `infrastructure/environments/dev/`:
```bash
terraform plan -var-file dev.tfvars -var 'supabase_host=aws-0-eu-central-1.pooler.supabase.com' -var 'supabase_username=postgres.testref' -out /tmp/plan.supabase.binary
terraform show -no-color /tmp/plan.supabase.binary > /tmp/plan.supabase.txt
```

Scan the plan:
```bash
# Must contain: 1 aws_secretsmanager_secret.credential to CREATE
grep -c 'will be created' /tmp/plan.supabase.txt
# Must NOT contain: aurora or networking resources to create
grep -cE 'module\.(aurora|networking).*will be created' /tmp/plan.supabase.txt
# Must contain: Lambda functions updated with SUPABASE_* env vars
grep -c 'SUPABASE_HOST' /tmp/plan.supabase.txt
```

Expected: at least 1 `will be created`, zero aurora/networking creates, SUPABASE_HOST appears on every Aurora-using Lambda (see f1.4 inventory — ~18 Lambdas).

- [ ] **Step 2: Capture the use_supabase=false plan**

```bash
terraform plan -var-file dev.tfvars -var 'use_supabase=false' -out /tmp/plan.aurora.binary
terraform show -no-color /tmp/plan.aurora.binary > /tmp/plan.aurora.txt
```

Scan:
```bash
# Must contain: aurora cluster + networking resources to create (because current state = everything torn down)
grep -c 'module.aurora.*will be created' /tmp/plan.aurora.txt
grep -c 'module.networking.*will be created' /tmp/plan.aurora.txt
# Must NOT create: aws_secretsmanager_secret.credential
grep -c 'module.supabase.*will be created' /tmp/plan.aurora.txt
# Must contain AURORA_HOST env vars
grep -c 'AURORA_HOST' /tmp/plan.aurora.txt
```

Expected: aurora + networking resources created, zero supabase creates, AURORA_HOST on every Aurora-using Lambda.

- [ ] **Step 3: Save both plans as PR artifacts**

Copy summarised plan outputs to `features/feature-1-terraform-plan-output.md` (a short digest, not the full plan):

```markdown
# Feature 1 — Terraform plan snapshots

Captured 2026-11-08 during story f1.10 of `features/feature-1-supabase-alternative-plan.md`.

## `use_supabase = true`
- Plan: N to add, 0 to change, 0 to destroy (N is the Supabase secret + updated Lambda env vars).
- Aurora resources created: 0
- Networking resources created: 0
- Supabase secret created: 1 (knotify/dev/supabase_credential)
- Lambdas with SUPABASE_HOST env var: 18

## `use_supabase = false`
- Plan: M to add, 0 to change, 0 to destroy (M is Aurora + networking rebuilt from zero).
- Aurora resources created: <fill in actual count>
- Networking resources created: <fill in actual count>
- Supabase secret created: 0
- Lambdas with AURORA_HOST env var: 18
```

Subagent fills in the actual counts from the real plan output.

- [ ] **Step 4: Commit the plan digest**

```bash
git add features/feature-1-terraform-plan-output.md
git commit -m "docs(feature-1): capture both-flag terraform plan digests (story f1.10)"
```

**Acceptance:** Both plans execute without errors; the digest file is committed.

---

### Story f1.11 — Terraform apply: deploy Supabase path

**Agent:** backenddeveloper, with user approval gate

**Rationale:** The first time we touch real AWS state. Approval gate before apply.

**Files:** none modified in-repo; AWS state changes.

- [ ] **Step 1: User fills in real values**

The subagent prepares a message to the user:

> "Ready to apply `use_supabase=true` to the dev environment. Before I run `terraform apply`, please:
>
> 1. Open `infrastructure/environments/dev/dev.tfvars` and set `supabase_host` to your real pooler host (e.g. `aws-0-eu-central-1.pooler.supabase.com`) and `supabase_username` to your real pooler username (e.g. `postgres.<your-project-ref>`).
> 2. Commit that edit as a separate commit.
> 3. Confirm here that you want me to run `terraform apply` on the dev workspace.
>
> The apply will create one AWS Secrets Manager secret with an empty password placeholder. Right after apply, you (or I, if you prefer) populate the secret with the real password via `aws secretsmanager put-secret-value`. That password never enters Terraform state."

Wait for user's explicit "apply" confirmation before proceeding to step 2.

- [ ] **Step 2: User commits the host + username edit**

```bash
git add infrastructure/environments/dev/dev.tfvars
git commit -m "chore(feature-1): fill in real Supabase pooler host + username in dev.tfvars (story f1.11)"
```

- [ ] **Step 3: terraform apply with use_supabase=true**

```bash
cd infrastructure/environments/dev && terraform apply -auto-approve
```
Expected: apply completes 0 errors. Outputs include the new Supabase secret ARN.

- [ ] **Step 4: Populate the secret with the real password**

```bash
aws secretsmanager put-secret-value \
  --secret-id knotify/dev/supabase_credential \
  --secret-string "{\"username\":\"postgres.<project-ref>\",\"password\":\"<real-password>\"}"
```
The subagent does NOT run this command with real secrets in logs — it prepares the command and asks the user to run it locally.

- [ ] **Step 5: Verify secret contents (without printing the password)**

```bash
aws secretsmanager get-secret-value --secret-id knotify/dev/supabase_credential --query 'SecretString' --output text | python3 -c 'import sys,json; d=json.loads(sys.stdin.read()); print({"username": d["username"], "password_set": bool(d["password"])})'
```
Expected: `{'username': 'postgres.<project-ref>', 'password_set': True}`.

- [ ] **Step 6: No commit — this story is a deploy, not a code change**

**Acceptance:** `terraform apply` completes; secret ARN is in outputs; secret contains real password.

---

### Story f1.12 — Run `db_migrator` against Supabase

**Agent:** backenddeveloper

**Rationale:** First real behavioural test. db_migrator is a Lambda; it reads USE_SUPABASE=true, routes through `_supabase.get_connection`, and applies the migration SQL files one by one.

**Files:** none modified in-repo.

- [ ] **Step 1: Invoke db_migrator**

```bash
aws lambda invoke \
  --function-name knotify-dev-db-migrator \
  --payload '{}' \
  --cli-binary-format raw-in-base64-out \
  /tmp/db_migrator_response.json
cat /tmp/db_migrator_response.json
```
(Function name shape taken from the module source — subagent verifies the exact name via `terraform output` first.)

Expected: `{"statusCode": 200, ...}` or equivalent success shape per the current db_migrator contract.

- [ ] **Step 2: Tail the Lambda's CloudWatch logs for the invocation**

```bash
aws logs tail /aws/lambda/knotify-dev-db-migrator --since 5m
```
Expected: lines showing each migration file applied in order (0001, 0002, ..., 0012 or whatever the current max is). No error lines.

- [ ] **Step 3: Verify tables exist on Supabase**

Run a read-only query against Supabase via the pooler (using the proof script from f1.0 modified for a SELECT):
```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'pg_toast', 'pgbouncer', 'realtime', 'storage', 'auth', 'vault', 'graphql', 'graphql_public', 'extensions')
ORDER BY table_schema, table_name;
```
Expected: the canonical knotify table set (users, profiles, friends, blocks, bookmarks, chat_rooms, messages, deletion_requests, migrations metadata table, etc.) — matches what migration 0001-0012 produces on Aurora.

- [ ] **Step 4: Save the table list as PR artifact**

Append to `features/feature-1-terraform-plan-output.md`:
```markdown

## db_migrator invocation (story f1.12)

- Invocation status: 200
- Migrations applied: 0001 through <highest>
- Tables present after migration: <list from step 3>
```

- [ ] **Step 5: Commit the digest update**

```bash
git add features/feature-1-terraform-plan-output.md
git commit -m "docs(feature-1): db_migrator run output against Supabase (story f1.12)"
```

**Acceptance:** db_migrator exits 200 against Supabase; expected tables present; log is clean.

---

### Story f1.13 — Smoke tests for phases 6, 7, 8, 9 against Supabase

**Agent:** backenddeveloper

**Rationale:** Prove no Lambda handler regressed. The existing phase-smoke-tests are the honest regression surface.

**Files:** no code change; test-run artifact committed.

- [ ] **Step 1: Locate the existing smoke test suites**

```bash
ls infrastructure/smoke/
```
Expected: one or more phase-scoped smoke test scripts. Read each to understand how it targets env (dev endpoint, api gateway URL, etc.).

- [ ] **Step 2: Run phase 6 smoke tests (profile, friends, bookmarks, blocks)**

Follow the invocation convention established by `infrastructure/smoke/README.md` (or by convention from prior phase runs — subagent reads the smoke module). Example shape:
```bash
cd infrastructure/smoke && python3 probe_phase6.py --env dev
```
Expected: all probes PASS.

- [ ] **Step 3: Run phase 7 smoke tests (match + deck)**

```bash
cd infrastructure/smoke && python3 probe_phase7.py --env dev
```
Expected: all probes PASS. This exercises pgvector — proves Supabase's pgvector version is compatible with our migrations.

- [ ] **Step 4: Run phase 8 smoke tests (chat)**

```bash
cd infrastructure/smoke && python3 probe_phase8.py --env dev
```
Expected: all probes PASS. Chat also uses DynamoDB which is unchanged.

- [ ] **Step 5: Run phase 9 smoke tests (account deletion)**

```bash
cd infrastructure/smoke && python3 probe_phase9.py --env dev
```
Expected: all probes PASS. This is the broadest test — touches Step Functions, multiple Lambdas, Aurora/Supabase, DynamoDB, Cognito.

- [ ] **Step 6: Record pass/fail per phase**

Append to `features/feature-1-terraform-plan-output.md`:
```markdown

## Smoke tests against Supabase (story f1.13)

| Phase | Probe | Result |
|---|---|---|
| 6 | probe_phase6.py | PASS/FAIL (fill in) |
| 7 | probe_phase7.py | PASS/FAIL |
| 8 | probe_phase8.py | PASS/FAIL |
| 9 | probe_phase9.py | PASS/FAIL |
```

If any FAIL: halt the plan here, surface the failure, do NOT proceed to f1.14 or later. Treat the failure as a bug in the Supabase integration and debug per the `superpowers:systematic-debugging` skill.

- [ ] **Step 7: Commit**

```bash
git add features/feature-1-terraform-plan-output.md
git commit -m "test(feature-1): phase 6/7/8/9 smoke tests against Supabase (story f1.13)"
```

**Acceptance:** All four phase probes pass against Supabase.

---

### Story f1.14 — Reverse-direction proof: flag flip to Aurora

**Agent:** backenddeveloper

**Rationale:** The user's explicit new requirement is "switching back to Aurora with a fresh deployment should be easy." This story proves the flag toggle is idempotent in BOTH directions, without destroying production.

**Files:** none modified in-repo; ephemeral Terraform apply + destroy.

- [ ] **Step 1: Apply with use_supabase=false in a disposable branch state**

Do NOT run this against the live dev state. Instead, create a throwaway workspace:
```bash
cd infrastructure/environments/dev && terraform workspace new feature1-reverse-proof
terraform apply -var-file dev.tfvars -var 'use_supabase=false'
```
Expected: Aurora + VPC come up fresh. The Supabase secret resource is destroyed as part of the same apply. Apply completes clean.

- [ ] **Step 2: Verify post-apply state**

```bash
terraform state list | grep -E 'module\.(aurora|networking)' | wc -l
terraform state list | grep 'module.supabase' | wc -l
```
Expected: non-zero count for aurora + networking; zero count for supabase.

- [ ] **Step 3: Destroy the throwaway workspace to clean up**

```bash
terraform destroy -auto-approve -var-file dev.tfvars -var 'use_supabase=false'
terraform workspace select default
terraform workspace delete feature1-reverse-proof
```
Expected: everything destroyed; no orphans.

- [ ] **Step 4: Record the proof**

Append to `features/feature-1-terraform-plan-output.md`:
```markdown

## Reverse-direction proof (story f1.14)

- Workspace: feature1-reverse-proof (ephemeral)
- `terraform apply -var 'use_supabase=false'` from Supabase-live state: clean exit
- Aurora + networking resources created: YES
- Supabase secret destroyed in same apply: YES
- Workspace destroyed and deleted after proof.
```

- [ ] **Step 5: Commit**

```bash
git add features/feature-1-terraform-plan-output.md
git commit -m "test(feature-1): reverse-direction flag flip proof (story f1.14)"
```

**Acceptance:** The flag flips cleanly in both directions in an isolated workspace with no manual cleanup.

---

### Story f1.15 — Operator runbook

**Agent:** backenddeveloper

**Files:**
- Create: `features/supabase-runbook.md`

- [ ] **Step 1: Write the runbook**

Create `features/supabase-runbook.md`:

```markdown
# Supabase alternative — operator runbook

Scope: dev environment only. Prod is pinned to `use_supabase = false` in `infrastructure/environments/prod/prod.tfvars`.

## Flipping the flag

### Aurora → Supabase

1. Ensure `infrastructure/environments/dev/dev.tfvars` has:
   - `use_supabase = true`
   - `supabase_host = "aws-0-eu-central-1.pooler.supabase.com"`
   - `supabase_username = "postgres.<project-ref>"`
2. `cd infrastructure/environments/dev && terraform apply`
3. Populate the secret with the real password:
   ```bash
   aws secretsmanager put-secret-value \
     --secret-id knotify/dev/supabase_credential \
     --secret-string '{"username":"postgres.<project-ref>","password":"<password>"}'
   ```
4. Invoke `db_migrator` to apply migrations against the fresh Supabase schema:
   ```bash
   aws lambda invoke --function-name knotify-dev-db-migrator /tmp/out.json
   ```
5. Run phase smoke tests to confirm (phases 6, 7, 8, 9).

### Supabase → Aurora

1. Set `use_supabase = false` in `dev.tfvars`.
2. `cd infrastructure/environments/dev && terraform apply`
   - This creates Aurora + VPC from scratch and destroys the Supabase secret.
   - Apply typically takes 8–15 minutes due to the Aurora cluster create.
3. Invoke `db_migrator` to apply migrations against the fresh Aurora cluster.
4. Run phase smoke tests.

## Common operations

### Rotate the Supabase password
1. Rotate in Supabase (Project Settings → Database → Reset password).
2. `aws secretsmanager put-secret-value --secret-id knotify/dev/supabase_credential --secret-string '{"username":"postgres.<ref>","password":"<new>"}'`
3. No Lambda restart required — `get_connection` fetches the secret per-invocation at cold start.

### Rotate the Aurora password
Unchanged from current operation; the Aurora master-user-secret rotation flow still applies.

## Troubleshooting

### Lambda cannot connect to Supabase
1. Check CloudWatch log for the exact error.
2. If `SSL error`: confirm `sslmode=require` is set in `_supabase.py` (it is; this is a layer-redeploy issue).
3. If `too many connections`: the pooler is over capacity. Check Supabase dashboard.
4. If `password authentication failed`: confirm the secret contents (`aws secretsmanager get-secret-value ...`).

### RLS queries return zero rows unexpectedly
1. Confirm `USE_SUPABASE` env var is literally `"true"` (case-sensitive) on the failing Lambda.
2. Confirm the GUC names in `_rls._GUC_USER_ID` and `_GUC_USER_SEX` match migration 0007 verbatim.
3. Run `scripts/supabase_pooler_proof.py` to isolate pooler behaviour from application logic.

## Cost sanity-check (expected monthly, dev)
| Flag state | Expected AWS bill |
|---|---|
| `use_supabase = true` (current) | ~$40 (Lambda + DynamoDB + API GW tail; no Aurora, no VPC) |
| `use_supabase = false` | ~$207 (Aurora + VPC + tail) |
```

- [ ] **Step 2: Commit**

```bash
git add features/supabase-runbook.md
git commit -m "docs(feature-1): operator runbook for Supabase flag (story f1.15)"
```

**Acceptance:** The runbook exists and is self-contained enough that a new operator can flip the flag in either direction without reading this plan.

---

### Story f1.16 — Open PR to `development`

**Agent:** main agent (NOT a subagent) — the main agent owns PR creation per `/implement-phase` convention.

**Files:** none modified; git + GitHub operation only.

- [ ] **Step 1: Confirm feature branch is clean and all stories committed**

```bash
git status
git log --oneline development..HEAD
```
Expected: clean working tree; list shows the sequence of `(feature-1)` commits from f1.0 through f1.15.

- [ ] **Step 2: Push the feature branch**

```bash
git push -u origin feat/feature-1-supabase-alternative
```
Expected: push succeeds; PR URL hint from GitHub shows up.

- [ ] **Step 3: Open PR**

```bash
gh pr create --base development --head feat/feature-1-supabase-alternative --title "Feature 1: Supabase alternative to Aurora + private VPC (dev-only)" --body "$(cat <<'EOF'
## Summary
- Introduces `use_supabase` Terraform flag for dev environment. Mutually exclusive with Aurora + private VPC. Prod pinned false.
- New `supabase` module (AWS Secrets Manager secret only — Supabase itself is managed outside AWS).
- New `knotify_db._supabase` connection builder; `knotify_db._rls` extracted so one RLS implementation serves both Aurora and Supabase paths.
- `knotify_db/__init__.py` dispatches on `USE_SUPABASE` env var.
- Zero Lambda handler code changes.

## Motivation
October AWS bill: $207, of which Aurora + VPC = ~$167 (81%). With `use_supabase=true` and the flag flipped, dev bill drops to ~$40/month. Prod unaffected.

## PRD
`features/feature-1-supabase-alternative.md`

## Plan
`features/feature-1-supabase-alternative-plan.md` — 16 stories, all green.

## Verification
- `features/feature-1-terraform-plan-output.md` captures the both-flag plan digests, db_migrator output, phase 6/7/8/9 smoke-test results, and the reverse-direction proof.

## Test plan
- [ ] CI terraform validate on both env/dev and env/prod
- [ ] `pytest infrastructure/src/layers/db/tests/` — all unit tests pass
- [ ] Reviewer confirms prod.tfvars pins `use_supabase = false`
EOF
)"
```

- [ ] **Step 4: Report PR URL to user**

Output the PR URL for the user to review and merge.

**Acceptance:** PR open against `development`. No force-push. No skip-hooks. Main agent does NOT merge — user reviews and merges.

---

## Self-review checklist (completed before plan was saved)

1. **Spec coverage.** Every PRD §5 design element has a story:
   - §5.1 flag → f1.1
   - §5.2 Terraform conditionality → f1.3, f1.4, f1.5
   - §5.3 Lambda code path → f1.6, f1.7, f1.8
   - §5.4 Migrations → f1.12
   - §5.5 Lambdas affected → covered by f1.4's local-based env-var swap
   - §5.6 Cost sanity → f1.15 runbook table
   - §6 acceptance decisions → embedded in f1.1, f1.3, f1.10, f1.14
   - §8 acceptance criteria → f1.10 (plan diffs), f1.12 (migrator), f1.13 (smoke), f1.14 (reverse proof)
   - §9 risks → connection exhaustion mitigated by pooler transaction mode (f1.0 proof); latency accepted for dev; RLS proof is f1.0

2. **Placeholder scan.** No "TBD", no "implement later". The two `...` placeholders in f1.6's `_rls.py` body are explicitly annotated as "lift verbatim from `_db.py`" — the subagent reads the source for the exact bodies; this is NOT a vague TODO, it is a deliberate instruction to avoid re-typing sensitive GUC code from memory.

3. **Type consistency.** `get_connection` signature matches across `_db.py`, `_supabase.py`, and the `__init__.py` dispatch — all accept `Union[dict, str]` and return `psycopg2.extensions.connection`. Env var names are consistent: `SUPABASE_HOST`/`SUPABASE_PORT`/`SUPABASE_DBNAME` in both `_supabase.py` and `dev/main.tf` locals. Terraform variable names match between `dev/variables.tf`, `modules/supabase/variables.tf`, and the invocations.

4. **Ambiguity check.** The two `module.aurora.foo` → `one(module.aurora[*].foo)` rewrites and the `environment_variables = local.db_env_vars` substitution are described exactly once and are mechanical. The subagent cannot misinterpret which Lambdas are Aurora-using — the inventory is listed verbatim in f1.4.

---

## Status / next actions

- [ ] User reviews this plan.
- [ ] User flips `ready: true` in the front matter when ready.
- [ ] Triage uncommitted files on `ci/outputs-dev-on-main`; commit important ones; merge into `development`; cut `feat/feature-1-supabase-alternative` from `development`; move PRD + plan to that branch. (Main agent handles this step with user approval before f1.0.)
- [ ] User runs `scripts/supabase_pooler_proof.py` after f1.0 lands and before f1.1.
- [ ] User fills real `supabase_host` + `supabase_username` into `dev.tfvars` before f1.11.
- [ ] User provides pooler password via `put-secret-value` after f1.11 apply.
