# CI/CD — knotify-backend

> Operational guide for the GitHub Actions → AWS deployment pipeline.
> Authoritative sources: `architecture.md` §10 (pipeline design + smoke test) and `codingprinciples.md` (tagging, secrets, IaC, test rules).
> This file is **not loaded at session start** — backenddeveloper reads it on demand when picking up CI/CD work.
>
> **Naming reconciliation:** `architecture.md` §10.2 uses the branch name `develop`. The workspace standard (per `codingprinciples.md` and `gitbranching.md`) is `development`. This document uses **`development`** throughout. When transcribing the architecture's YAML examples, replace `develop` → `development` and `refs/heads/develop` → `refs/heads/development`.

---

## 1. Deployment targets (environments)

Two completely isolated AWS environments, each tied to a single branch.

| GitHub branch | AWS environment | Account | Auto-deploy? | Approval gate |
| --- | --- | --- | --- | --- |
| `development` | `dev` | dev AWS account | Yes, on every push | None |
| `main` | `prod` | prod AWS account | Yes, on every push | **Required reviewer** via GitHub Environments |
| `feat/*`, `fix/*`, `chore/*` | none | n/a | No | n/a — only `validate` + `plan` + `test` run when a PR is opened |

Pull-requests against `development` or `main` run `validate` + `plan` (for **both** environments, so reviewers can see both diffs) + `test`. They never `apply`.

**Environment routing happens via the branch you merge into — there is no manual environment selector.** See architecture.md §10.3 for the day-to-day promotion walkthrough.

### GitHub Environments configuration (one-time, in repo settings)

| Environment | Required reviewers | Wait timer | Deployment branch rule |
| --- | --- | --- | --- |
| `dev` | none | 0 | restrict to `development` |
| `prod` | repo owner (and any future maintainers) | 0 | restrict to `main` |

Restricting the deployment branch on each environment is defense-in-depth: even if the `if:` guard on an apply job were misconfigured, `prod` credentials cannot be assumed from a branch that isn't `main`.

### AWS account / region

- Two member accounts under one AWS Organization (dev + prod). Bootstrapped manually per architecture.md §10.6.
- Default region: **`eu-central-1`** (owner location). Set via the `region` variable in each environment's `terraform.tfvars`.
- Cross-account access is not used in v1; each pipeline job assumes credentials scoped to a single account.

---

## 2. Secret management

### Split: who holds what

| Secret type | Where it lives | Who reads it |
| --- | --- | --- |
| AWS deploy credentials (CI-time) | **GitHub Environment secrets** (per environment: `dev`, `prod`) | GitHub Actions jobs only, while the matching `environment:` is active |
| Runtime secrets (DB passwords, third-party API tokens, signing keys) | **AWS Secrets Manager** or **SSM Parameter Store**, per environment | Lambdas at runtime via IAM-scoped access |
| Terraform backend coordinates (S3 bucket, DynamoDB lock table) | Per-environment `backend.tf` (non-sensitive) | `terraform init` |

Never put runtime secrets in GitHub Actions secrets, and never put AWS access keys in Secrets Manager. The two stores serve different threat models.

### Required GitHub Environment secrets (v1 — static keys)

Set the same secret **names** in both environments; the values differ:

| Environment | Secret | Source |
| --- | --- | --- |
| `dev` | `AWS_ACCESS_KEY_ID` | dev account IAM user |
| `dev` | `AWS_SECRET_ACCESS_KEY` | dev account IAM user |
| `prod` | `AWS_ACCESS_KEY_ID` | prod account IAM user |
| `prod` | `AWS_SECRET_ACCESS_KEY` | prod account IAM user |

`secrets.AWS_ACCESS_KEY_ID` resolves to the dev value when `environment: dev` is active in the job, and to the prod value when `environment: prod` is active. The workflow file is environment-agnostic.

### Required upgrade (planned, architecture §3.2 / §10.4)

Replace the static IAM-user keys with **GitHub OIDC federation**:

- Create an IAM role in each member account with a trust policy that permits `token.actions.githubusercontent.com` for this repo and the matching environment.
- Replace the `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env entries with `aws-actions/configure-aws-credentials@v4` and `role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}`.
- Short-lived credentials, no rotation, no static secret to leak.

The `permissions: id-token: write` declaration is already present in the workflow (architecture.md §10.2) so this upgrade does not require a workflow-permission change.

### Secret-leak prevention

- `gitleaks` (or `detect-secrets`) runs in CI on every PR. Any finding blocks merge. (Required check.)
- A matching pre-commit hook is installed locally so leaks fail before push.
- `.gitignore` includes `*.tfvars` containing secret values, `terraform.tfstate*`, `.env*`, and `*.pem`.
- `.tfvars` files committed to the repo (per environment) contain **only** non-sensitive variables; secrets are pulled at apply-time from Secrets Manager via `data` sources.

---

## 3. Build pipeline

There is no traditional "build" step for Terraform — `terraform plan` is the build artifact. For Python Lambda code, "build" means: install pinned deps, package the deployment artifact, and upload it (in v1 via Terraform's `archive_file` data source; in v2 via S3-uploaded zip referenced by `s3_bucket`/`s3_key`).

### Stages

| Stage | Job | Runs on | Caching |
| --- | --- | --- | --- |
| Checkout | `actions/checkout@v4` | every job | n/a |
| Toolchain setup | `hashicorp/setup-terraform@v3` (pin `1.7.5`); `actions/setup-python@v5` (pin matching `pyproject.toml`) | every job that needs it | `actions/setup-*` cache |
| Python deps | `pip install -r requirements.txt` (from lockfile — `poetry.lock` / `uv.lock` / pinned `requirements.txt` per coding principles) | tests + lambda packaging | `actions/cache@v4` keyed on lockfile hash |
| Terraform providers | `terraform init` (one per environment) | validate, plan, apply | `actions/cache@v4` keyed on `.terraform.lock.hcl` |
| Lambda packaging | `terraform plan` invokes `archive_file` (v1) | plan, apply | n/a |

### Tool versions (pinned)

- Terraform: **`1.7.5`** (matches architecture.md §10.2)
- AWS provider: `~> 5.70`
- tflint: latest via `terraform-linters/setup-tflint@v4` (pin once first install is verified)
- Python: read from `pyproject.toml` — pin major.minor (e.g., `3.12`)
- ruff, mypy, black: pinned in `pyproject.toml` dev-dependencies

Floating versions (`>=`) are not allowed anywhere — coding principles §Terraform.

---

## 4. Test pipeline

Every PR to `development` or `main` runs the full check matrix. **All must be green before merge** — these are configured as required status checks on both protected branches.

### Required checks (block merge on failure)

| Check | Command | Scope |
| --- | --- | --- |
| Terraform format | `terraform fmt -check -recursive infrastructure/` | repo-wide |
| Terraform validate | `terraform validate` per environment, run with `-backend=false` | dev + prod root modules |
| Terraform lint | `tflint --recursive` (AWS ruleset enabled) | repo-wide |
| Terraform static security | `aquasecurity/tfsec-action@v1.0.3` | repo-wide |
| Terraform unit tests | `terraform test` against `infrastructure/tests/` | module test files |
| Terraform plan (dev) | `terraform -chdir=infrastructure/environments/dev plan -out=tfplan` | uploaded as artifact |
| Terraform plan (prod) | `terraform -chdir=infrastructure/environments/prod plan -out=tfplan` | uploaded as artifact |
| Python lint | `ruff check` | repo-wide |
| Python format | `ruff format --check` (or `black --check`) | repo-wide |
| Python type-check | `mypy` on shared library code at minimum | shared libs |
| Python unit tests | `pytest` with coverage threshold (set when first lambda lands) | all Lambda handlers + shared libs |
| Python integration tests | `pytest -m integration` using `moto` / `localstack` (or ephemeral dev resources) | code that touches AWS |
| Secret scan | `gitleaks detect --redact` on the PR diff | repo-wide |

### Test job ordering

```
validate ─┬─► plan (matrix: dev, prod) ─┐
          │                              ├─► apply-dev   (only on push to development)
          └─► test (terraform + python) ─┤
                                         └─► apply-prod  (only on push to main, gated)
```

The `test` job and the per-environment `plan` jobs run in parallel after `validate`. `apply-*` depends on both `plan` (for the matching artifact) and `test`.

### Tagging-policy check (deferred but planned)

Coding principles require every AWS resource to carry `Project=knotify`, `Environment=<dev|prod>`, `ManagedBy=terraform`, `Owner=<team-or-individual>`. Enforced first via the provider's `default_tags` block, then via a tflint custom rule or OPA/conftest policy run in CI. Add this check as soon as the tagging convention is in place; until then, missing tags are a code-review item.

---

## 5. Deploy pipeline

### Triggers

```yaml
on:
  push:
    branches: [main, development]
  pull_request:
    branches: [main, development]
```

### Apply guards (the routing rule, in code)

```yaml
apply-dev:
  needs: [plan, test]
  if: github.event_name == 'push' && github.ref == 'refs/heads/development'
  environment: dev

apply-prod:
  needs: [plan, test]
  if: github.event_name == 'push' && github.ref == 'refs/heads/main'
  environment: prod   # GitHub Environment enforces manual approval
```

Both jobs `download-artifact` the matching `tfplan-<env>` produced earlier, `terraform init` against the per-environment backend, then `terraform apply tfplan`. Applying the saved plan (not re-planning at apply time) guarantees the reviewer-approved diff is exactly what gets executed.

### Branch protection (configured manually in repo settings)

- **`main`**: require PR, require status checks `validate / plan-dev / plan-prod / test / gitleaks` to pass, no direct pushes, require linear history, require reviewer approval, dismiss stale approvals on new pushes.
- **`development`**: require PR + status checks. Direct pushes from the owner are tolerated for fast iteration; once the team grows, tighten to "PR required."

### Smoke-test workflow

A separate `.github/workflows/smoke-test.yml` exists per architecture.md §10.6 to deploy a trivial S3 bucket end-to-end before any phase-1 work begins. Triggered by:

- `workflow_dispatch` (manual), or
- push to `smoke-test/dev` (→ dev) or `smoke-test/prod` (→ prod, gated)

Exit criteria: bucket created in both accounts, manual approval gate exercised on prod, destroy runs cleanly, `PIPELINE_VALIDATED.md` checked into the repo root with date + outputs. No phase-1 implementation begins until this is green.

---

## 6. Rollback strategy

Terraform-managed infrastructure rolls back by **redeploying the previous known-good Terraform code**, not by clicking buttons in the AWS console. Manual console edits would drift from state and break the next `apply`.

### Standard rollback (preferred)

1. Identify the last good commit on the affected branch.
   ```
   git log --oneline --first-parent main      # or development
   ```
2. Open a **revert PR** that reverts the offending merge commit:
   ```
   git revert -m 1 <merge_sha>
   ```
3. Let the normal pipeline run: `validate` → `plan` (review the diff — it should be the inverse of the breaking change) → merge.
4. On merge to `main`, `apply-prod` is gated — approve in GitHub Environments UI.

This re-uses the audited pipeline. No special rollback tooling. Mean time to rollback ≈ mean time to merge + apply.

### Emergency rollback (when you cannot wait for a PR)

If prod is actively broken and the revert PR pipeline is too slow:

1. Use `workflow_dispatch` on the smoke-test or a dedicated `manual-apply` workflow (to be added when needed) to run `terraform apply` against an explicit commit SHA on the affected environment.
2. The job still gates on the `prod` GitHub Environment manual approval — that is intentional. Do not weaken this for "emergencies"; one approval click is not the bottleneck.
3. Open the matching revert PR immediately after, so the branch state and the deployed state converge.

### State-level recovery

- **Terraform state in S3 has versioning enabled** (set up in §10.6 bootstrap). If a bad `apply` corrupts state, restore the prior S3 object version and re-`plan`.
- **DynamoDB lock table** — if a job dies holding the lock, manually delete the lock row (`aws dynamodb delete-item --table-name knotify-tfstate-lock --key '{"LockID":{"S":"<id>"}}'`). The job's `terraform plan` output usually prints the LockID. Do not force-unlock during an active apply — that's how state gets corrupted.

### What v1 does *not* do

- No blue/green Lambda deploys, no traffic-shifted aliases, no canary rollout. Single-version deploys, atomic per Lambda.
- No automated post-deploy smoke tests (architecture.md §10.5 — owner preference). Verification is manual in dev before promotion to prod.
- No Aurora point-in-time-restore drill scripted in v1. PITR is enabled on the cluster (per architecture.md §4.5); the runbook is "restore via console / `aws rds restore-db-cluster-to-point-in-time`."

These are explicit v1 trade-offs. Add them when the operational pain justifies the complexity.

---

## 7. Notifications / alerts

### Pipeline outcome notifications

| Event | Channel | How |
| --- | --- | --- |
| Failed run on `development` or `main` | GitHub default email to the committer | Built-in, no config needed |
| Failed run on `main` (prod) | Same as above, plus a future Slack/Discord webhook | Add when team grows past one person |
| `apply-prod` waiting for approval | GitHub UI + email to required reviewers | Built-in via GitHub Environments |
| Smoke-test failure | GitHub default email | Built-in |

For v1 (single owner), the default GitHub email notifications are sufficient. When a second engineer joins, add a `notify` job on failure that posts to Slack/Discord via webhook stored as a repo-level secret.

### AWS runtime alerts (out of scope for cicd.md, owned by architecture.md)

CloudWatch alarms on Lambda error rates, Aurora CPU/connections, DynamoDB throttles, etc. are infrastructure concerns provisioned by Terraform — not pipeline concerns. They live in the relevant phase PRDs and the `monitoring` Terraform module (to be added).

### Pipeline health metrics (deferred)

- Average time from PR open to merge
- Average time from merge to deploy
- Rollback frequency

Track manually for v1; revisit when the team or deploy frequency justifies a DORA-style dashboard.

---

## 8. Reference

- `architecture.md` §10.1 — Terraform layout
- `architecture.md` §10.2 — full workflow YAML (canonical; substitute `development` for `develop`)
- `architecture.md` §10.3 — day-to-day promotion procedure
- `architecture.md` §10.4 — GitHub Environment secrets mechanics + OIDC upgrade path
- `architecture.md` §10.5 — Terraform test coverage scope
- `architecture.md` §10.6 — pre-implementation smoke test (mandatory exit gate before phase 1)
- `codingprinciples.md` — `knotify` prefix, required tags, secrets discipline, IAM least-privilege, lockfile / `terraform.lock.hcl`, formatting / lint / secret-scan as required checks
- `C:\Users\syede\Claude-Master\engineeringprinciples.md` — workspace-level engineering rules
- `C:\Users\syede\Claude-Master\gitbranching.md` — branch naming and protection
