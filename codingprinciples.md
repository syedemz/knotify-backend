# Coding Principles — knotify-backend

Used together with `C:\Users\syede\Claude-Master\engineeringprinciples.md`. If sections below are empty, the workspace engineeringprinciples apply by default. Fill these in only when this project needs principles that differ from or extend the workspace defaults.

## General principles

- Keep modules small and single-purpose. If a file does more than one thing, split it.

- **Two deployment environments via GitHub Actions.** The repository must be configured to deploy to two separate AWS environments — `development` and `production` — through GitHub Actions workflows. Branch-to-environment mapping follows the project's git branching strategy (`development` branch → dev AWS account, `main` branch → prod AWS account).
- **Pipeline smoke test before any real infrastructure work.** Before any cloud infrastructure is provisioned for actual features, the end-to-end pipeline must be validated per `architecture.md` §10.6. The flow under test is:

  `terminal → git (push/PR) → development/main branch → GitHub Actions → AWS environment`

  The smoke test deploys a trivial resource (per §10.6) through both the dev and prod paths to prove the pipeline is wired correctly. No phase-1 implementation begins until §10.6's exit criteria are met.

- **`knotify` naming prefix for AWS resources.** All cloud resources created (Lambda functions, APIs, S3 buckets, IAM roles, Step Functions, Cognito user pools, SNS topics, SQS queues, EventBridge rules, etc.) must be named — or, where naming is not user-controlled, labeled/tagged — with a `knotify` prefix (or a `knotify` tag) so all project-owned resources are unambiguously identifiable inside the AWS account.

  **Exception:** table names in Aurora and DynamoDB do not take the `knotify` prefix. They follow normal conventions as described in `architecture.md` (the schema and naming sections there are authoritative for tables).
- **Required AWS tags on every resource.** In addition to the `knotify` name prefix, every AWS resource carries the following tags: `Project=knotify`, `Environment=<dev|prod>`, `ManagedBy=terraform`, and `Owner=<team-or-individual>`. Enforce centrally via the AWS provider's `default_tags` block so individual `resource` blocks don't need to repeat them. Resources missing these tags are considered a CI failure once tag-enforcement linting is wired up.
- **Secrets discipline.** No secrets — AWS keys, DB passwords, API tokens, signing keys — in code, in committed Terraform files (including `.tfvars`), or in any tracked file. Runtime secrets live in **AWS Secrets Manager** or **SSM Parameter Store** and are read by Lambdas through IAM-scoped access at runtime. CI/deploy-time secrets live in **GitHub Environment secrets** (per `architecture.md` §10.4). A pre-commit hook (e.g., `gitleaks` or `detect-secrets`) must be installed and run in CI so this rule is enforced, not aspirational.
- **Least-privilege IAM.** No wildcard `Action: "*"` or `Resource: "*"` in any committed IAM policy. Every Lambda gets its own dedicated execution role scoped to exactly the AWS APIs and resource ARNs it needs. Cross-service shared "kitchen-sink" roles are not allowed. Where Terraform supports it, prefer policy documents built with `aws_iam_policy_document` data sources over inline JSON for reviewability.
- **API versioning.** All externally exposed APIs (AppSync, REST endpoints, public Lambdas behind API Gateway) carry an explicit version prefix in the path or schema (e.g., `/v1/...`) starting from day one. New breaking changes go to `/v2` rather than mutating `/v1`. This applies even during the initial dev rollout — retrofitting versioning after clients ship is painful.

## Language-specific principles

- **Python.**
  - Use type hints on all public functions and module-level callables.
  - Prefer Python's standard logging module over print statements.
  - **Structured (JSON) logging** in all Lambda code. Use `aws-lambda-powertools` (`Logger`) or equivalent so every log line is queryable in CloudWatch Logs Insights. A correlation/request ID must be propagated through the call chain (Lambda → downstream Lambda → AppSync resolver → DB call) and emitted on every log line.
  - Use `pyproject.toml` over legacy `setup.py`/`setup.cfg` where a packaging file is needed.
  - **Dependency pinning.** Use a lockfile (`poetry.lock`, `uv.lock`, or `pip-tools`-generated pinned `requirements.txt`) committed to the repo; CI installs strictly from the lock. Unpinned `requirements.txt` is not acceptable.
  - **Decorator patterns** should be used where they cleanly express cross-cutting concerns (logging, auth checks, retry/backoff, input validation, metrics) — provided the use is compatible with the rules in `engineeringprinciples.md`. Do not introduce decorators purely for novelty; they must reduce duplication or sharpen intent.
- **Terraform.**
  - Prefer **modules** wherever a resource grouping will be used more than once, or where a logical unit (e.g., "a Lambda + its IAM role + its log group") deserves a single reusable interface. Reach for a module before copy-pasting `resource` blocks across environments.
  - Module inputs and outputs must be explicit and documented at the top of the module's `variables.tf` / `outputs.tf`.
  - Pin provider versions; never use floating `>=` constraints on AWS or Terraform versions in committed code. The `.terraform.lock.hcl` file must be committed.
  - **Remote state with locking.** State lives in an **S3 backend** (one bucket, or one bucket per environment) with **DynamoDB-based state locking** to prevent concurrent `apply` corruption. One distinct state file per environment (`dev` / `prod`) — never share state between environments. Local state files (`terraform.tfstate*`) must never be committed and are listed in `.gitignore`. The S3 bucket + DynamoDB lock table themselves are bootstrapped as part of the §10.6 pipeline smoke test.

## Testing principles

- **Python code** must have test coverage. At minimum, unit tests for all Lambda handlers and shared library functions. Integration tests for any code that touches AWS services (use `moto`, `localstack`, or against an ephemeral dev-account resource — chosen per the engineering principles).
- **Terraform infrastructure** must have both:
  - **Unit tests** — `terraform validate`, `terraform fmt -check`, and module-level tests via `terraform test` (HCL test framework) or equivalent (`terratest` is acceptable for complex modules).
  - **Integration tests** — `terraform plan` against a real backend in dev, plus post-`apply` assertions that the deployed resources behave as intended (e.g., the Lambda actually invokes, the API actually responds, IAM policies actually allow what they should and deny what they shouldn't).
- All tests run in CI on every PR to `development` and `main`; merges are blocked on test failures (CI required-status-check enforcement will be added once `cicd.md` is filled out).
- **Formatting and linting are required CI checks**, not optional local hygiene:
  - **Python**: `ruff check` (lint) and `ruff format` or `black --check` (formatting). Type-check with `mypy` on shared library code at minimum.
  - **Terraform**: `terraform fmt -check -recursive` and `tflint` (with the AWS ruleset). `terraform validate` runs per root module.
  - **Secrets scanner**: `gitleaks` (or `detect-secrets`) runs on every PR; any finding blocks the merge.
  - Failures on any of the above block the merge once required-status-checks are enabled.
