phase: 1
title: Networking foundations
last_updated: 2026-05-22 (story 1.2)
context_summary: |
  Establishes the per-environment VPC, subnets, route tables, and security groups that all subsequent phases consume. Implements §6 of architecture.md verbatim: VPC 10.0.0.0/16 with public (10.0.1.0/24, 10.0.2.0/24), private (10.0.11.0/24, 10.0.12.0/24), and DB (10.0.21.0/24, 10.0.22.0/24) subnets across two AZs, security groups sg-lambda and sg-aurora with Lambda→Aurora 5432 the only allowed flow, and no NAT Gateway (Lambdas have no internet egress in v1). Subsequent phases (Aurora, Lambdas, AppSync) attach to these networking primitives.

stories:
  - id: 1.1
    title: Networking Terraform module
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 8
    acceptance_criteria:
      - File infrastructure/modules/networking/main.tf declares an aws_vpc with cidr_block=10.0.0.0/16 and enable_dns_hostnames=true
      - Two public subnets (10.0.1.0/24, 10.0.2.0/24), two private subnets (10.0.11.0/24, 10.0.12.0/24), and two DB subnets (10.0.21.0/24, 10.0.22.0/24) are created across the first two AZs from data.aws_availability_zones.available.names (sliced deterministically — no hardcoded AZ names)
      - An aws_db_subnet_group spans both DB subnets and is exposed as an output named "db_subnet_group_name"
      - No aws_nat_gateway, no aws_internet_gateway resource at all, no eip — public subnets are allocated but unused in v1 per architecture §6.1/§6.4; IGW lands in a later phase when ALB/CloudFront ships
      - Module outputs vpc_id, private_subnet_ids (list), db_subnet_group_name
    notes: ""

  - id: 1.2
    title: Security groups for Lambda and Aurora
    agent: backenddeveloper
    done: true
    depends_on: [1.1]
    tracking_issue: 9
    acceptance_criteria:
      - Module creates aws_security_group "sg-lambda" inside the VPC with zero inbound rules and an outbound rule allowing all traffic (placeholder; tightened later)
      - Module creates aws_security_group "sg-aurora" with a single inbound rule allowing TCP 5432 from sg-lambda's security_group_id only, and zero outbound rules
      - Module outputs lambda_security_group_id and aurora_security_group_id
      - terraform validate passes
    notes: ""

  - id: 1.3
    title: Per-environment instantiation
    agent: backenddeveloper
    done: false
    depends_on: [1.1, 1.2]
    tracking_issue: 10
    acceptance_criteria:
      - File infrastructure/environments/dev/main.tf instantiates module.networking with environment="dev" and region="eu-central-1"
      - File infrastructure/environments/prod/main.tf instantiates module.networking with environment="prod" and region="eu-central-1"
      - File infrastructure/environments/dev/backend.tf uses inline S3 backend with bucket="knotify-dev-tfstate", key="dev/terraform.tfstate", region="eu-central-1", dynamodb_table="knotify-tfstate-lock", encrypt=true — this single state file will hold ALL infra for the dev environment across phases 1–11
      - File infrastructure/environments/prod/backend.tf uses inline S3 backend with bucket="knotify-prod-tfstate", key="prod/terraform.tfstate", region="eu-central-1", dynamodb_table="knotify-tfstate-lock", encrypt=true
      - Files infrastructure/environments/dev/dev.tfvars and infrastructure/environments/prod/prod.tfvars set environment to the matching value
      - terraform init && terraform plan succeeds in infrastructure/environments/dev/ against the real dev backend; the plan shows only additive resources (VPC, subnets, route tables, SGs, db subnet group); no destructive diff against any existing object in s3://knotify-dev-tfstate/dev/terraform.tfstate (which is the empty starting state)
      - Prod-side terraform init/plan is DEFERRED per docs/PROD_CUTOVER.md — prod/main.tf, prod/backend.tf, and prod/prod.tfvars are authored for layout symmetry but NOT exercised. Re-open when the prod AWS account and state bucket are provisioned.
    notes: "Prod init+plan deferred-as-tested; the files are authored so the directory layout matches architecture §10.1 and so the future cutover is one credentials change, not a structural one. Backend key 'dev/terraform.tfstate' is the SINGLE state file for the entire dev environment across all phases — phases 2–11 add modules to environments/dev/main.tf and write to this same key."

  - id: 1.4
    title: Terraform tests for networking module
    agent: backenddeveloper
    done: false
    depends_on: [1.1, 1.2]
    tracking_issue: 11
    acceptance_criteria:
      - File infrastructure/modules/networking/tests/networking.tftest.hcl exists with at least three test cases
      - All test runs use `command = plan` so tests are hermetic — no AWS credentials required, no apply executed, runs cleanly in CI
      - One test asserts six subnets are created with the expected CIDR blocks
      - One test asserts sg-aurora has exactly one ingress rule and that the source security group id equals the sg-lambda id
      - One test asserts no aws_nat_gateway, no aws_internet_gateway, and no public route from private subnets
      - "terraform test" exits zero from infrastructure/modules/networking/
    notes: "Run 1.4 before 1.3 — passing tests prove the module is sound before instantiating it in a real environment. depends_on permits either order."

  - id: 1.5
    title: deploy.yml auto-deploy workflow on push to development
    agent: backenddeveloper
    done: false
    depends_on: [1.3, 1.4]
    tracking_issue: 12
    acceptance_criteria:
      - File .github/workflows/deploy.yml exists, structured per architecture §10.2 with jobs validate, plan (matrix over [dev, prod]), test, apply-dev, apply-prod
      - Triggers are `on.push.branches: [main, development]` and `on.pull_request.branches: [main, development]`
      - Terraform version pinned to 1.9.8 (same as smoke-test.yml from phase 0)
      - validate job runs `terraform fmt -check -recursive infrastructure/`, then for each env runs `terraform -chdir=infrastructure/environments/<env> init -backend=false && terraform -chdir=infrastructure/environments/<env> validate`, then tflint --recursive, then tfsec
      - plan job uses `strategy.matrix.environment: [dev, prod]` with `environment: ${{ matrix.environment }}` for env-scoped AWS secrets; runs `terraform init` (against the real backend) then `terraform plan -out=tfplan`; uploads the tfplan as an artifact named `tfplan-<env>`
      - plan-prod (i.e. the matrix instance for environment=prod) is additionally gated by `if: vars.DEPLOY_PROD == 'true'` so it skips at evaluation time while the prod pause is active — mirroring the smoke-test.yml gating pattern
      - test job runs after plan and exercises all .tftest.hcl files in the repo. Concretely, it iterates over `infrastructure/modules/*/tests` directories and runs `terraform -chdir=<module-dir> test` for each, plus `terraform -chdir=infrastructure test` if any top-level tests exist. The command set must succeed when only the networking module has tests
      - apply-dev runs only when `github.event_name == 'push' && github.ref == 'refs/heads/development'`; downloads the dev tfplan artifact; runs `terraform init` then `terraform apply tfplan` against the dev backend
      - apply-prod runs only when `github.event_name == 'push' && github.ref == 'refs/heads/main' && vars.DEPLOY_PROD == 'true'`; same shape as apply-dev but for prod
      - Both apply jobs source AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY from environment-scoped secrets (`environment: dev` / `environment: prod`); no aws-actions/configure-aws-credentials
      - `permissions: contents: read, id-token: write` at the workflow level so future OIDC adoption needs no permission change
      - actionlint exits 0 against the workflow file
      - When the phase-1 PR is open against development, the workflow runs trigger as expected and complete on the PR check with: validate=success, plan(dev)=success, plan(prod)=skipped (DEPLOY_PROD gate), test=success, apply-dev=skipped (PR event, not push), apply-prod=skipped — capture the run URL in the story's bookkeeping commit
    notes: "apply-dev's actual deploy of the networking module to dev happens when the user squash-merges the phase-1 PR into development at phase handoff. That post-merge run is the end-of-phase verification, not part of this story's AC. The story ships the workflow file and proves the PR-time validation path is green."
