phase: 0
title: Pipeline smoke test
last_updated: 2026-05-22 (story 0.6)

context_summary: |
  Validates the end-to-end deployment pipeline (GitHub Actions → Terraform → AWS) by deploying a single S3 bucket per §10.6 of architecture.md. Per the owner's production-deploy pause (see docs/PROD_CUTOVER.md), this phase ships with **dev-only validation**; the prod-side smoke (story 0.5) and the prod cleanup half of 0.6 are deferred until the prod AWS account is provisioned and the prod deploy gate is opened. Bootstrap prerequisites needed NOW (dev only): AWS Organization + dev member account, dev IAM user with static access key, knotify-dev-tfstate bucket with versioning, knotify-tfstate-lock DynamoDB table, GitHub Environment "dev" with AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY, AND a GitHub Environment "prod" created with required-reviewer protection (no AWS secrets yet — the empty gate is what enforces the pause). The workflow file written in 0.3 still declares the prod job; it simply never gets approved. Phase exits when 0.1–0.4 and the dev portion of 0.6 are green and PIPELINE_VALIDATED.md is committed. Subsequent phases assume the dev pipeline works; they will plan against prod but never apply.

stories:
  - id: 0.1
    title: Smoke S3 Terraform module
    agent: backenddeveloper
    done: true
    tracking_issue: 2
    depends_on: []
    acceptance_criteria:
      - File infrastructure/smoke/main.tf exists and declares aws + random providers pinned per architecture.md §10.6, an aws_s3_bucket named with pattern "knotify-smoke-${var.environment}-${random_id.suffix.hex}", an aws_s3_bucket_public_access_block with all four block flags true, and an aws_s3_bucket_server_side_encryption_configuration applying AES256
      - File infrastructure/smoke/variables.tf declares variables "environment" (string, required) and "region" (string, default "eu-central-1")
      - File infrastructure/smoke/outputs.tf exposes the bucket name as "bucket_name"
      - "terraform fmt -check" passes on infrastructure/smoke/
      - "terraform init -backend=false && terraform validate" passes inside infrastructure/smoke/
    notes: ""

  - id: 0.2
    title: Per-environment backend configuration for smoke scope
    agent: backenddeveloper
    done: true
    tracking_issue: 3
    depends_on: [0.1]
    acceptance_criteria:
      - File infrastructure/smoke/backend-dev.hcl exists configuring S3 backend with bucket=knotify-dev-tfstate, key=smoke/terraform.tfstate, region=eu-central-1, dynamodb_table=knotify-tfstate-lock
      - File infrastructure/smoke/backend-prod.hcl exists with the prod equivalents (bucket=knotify-prod-tfstate)
      - File infrastructure/smoke/dev.tfvars sets environment="dev"
      - File infrastructure/smoke/prod.tfvars sets environment="prod"
      - README within infrastructure/smoke/ documents the invocation pattern "terraform init -backend-config=backend-<env>.hcl"
    notes: ""

  - id: 0.3
    title: GitHub Actions smoke-test workflow
    agent: backenddeveloper
    done: true
    tracking_issue: 4
    depends_on: []
    acceptance_criteria:
      - File .github/workflows/smoke-test.yml exists with on.workflow_dispatch and on.push.branches=[smoke-test/*]
      - Job smoke-dev runs only when github.ref == "refs/heads/smoke-test/dev" and uses environment "dev"
      - Job smoke-prod runs only when github.ref == "refs/heads/smoke-test/prod" and uses environment "prod"
      - The smoke-prod job is additionally guarded by `if: vars.DEPLOY_PROD == 'true'`; the repo variable DEPLOY_PROD is created with the value "false" so the prod job is skipped at evaluation time even before the environment approval gate is reached
      - Both jobs run terraform init with backend-config from the matching backend-<env>.hcl, terraform plan, and terraform apply -auto-approve, sourcing AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY from the environment-scoped secrets
      - "actionlint" passes on the workflow file
    notes: "DEPLOY_PROD=false + an empty prod GitHub Environment (no AWS secrets) + required-reviewer protection together form the three-layer prod pause. See docs/PROD_CUTOVER.md for the flip-on checklist."

  - id: 0.4
    title: Dev smoke deploy and bucket verification
    agent: backenddeveloper
    done: true
    tracking_issue: 5
    depends_on: [0.1, 0.2, 0.3]
    acceptance_criteria:
      - A branch named smoke-test/dev is pushed to the remote and the smoke-dev job completes with conclusion=success in GitHub Actions
      - The deployed bucket appears in the dev AWS account with name matching "knotify-smoke-dev-*" and has BlockPublicAcls=true confirmed via aws s3api get-public-access-block
      - The dev Terraform state object exists at s3://knotify-dev-tfstate/smoke/terraform.tfstate
    notes: "Completed 2026-05-22. Workflow run https://github.com/syedemz/knotify-backend/actions/runs/26279114489 conclusion=success. Bucket knotify-smoke-dev-ce43afa2 deployed with all four public-access-block flags true. State at s3://knotify-dev-tfstate/smoke/terraform.tfstate (5447 bytes)."

  - id: 0.5
    title: Prod smoke deploy and bucket verification
    agent: backenddeveloper
    done: true
    depends_on: [0.1, 0.2, 0.3]
    acceptance_criteria:
      - DEFERRED — do not execute. Re-open per docs/PROD_CUTOVER.md once the prod AWS account is provisioned. Original criteria preserved below.
      - "(deferred) A branch named smoke-test/prod is pushed to the remote, the smoke-prod job blocks on the prod GitHub Environment approval gate, and after owner approval the job completes with conclusion=success"
      - "(deferred) The deployed bucket appears in the prod AWS account with name matching 'knotify-smoke-prod-*'"
      - "(deferred) The prod Terraform state object exists at s3://knotify-prod-tfstate/smoke/terraform.tfstate"
    notes: "Marked done=true to allow phase 0 to close on dev-only validation. This story is deferred, not completed. Re-flip to done=false and execute when the prod gate is opened — tracked in docs/PROD_CUTOVER.md."

  - id: 0.6
    title: Cleanup destroy and PIPELINE_VALIDATED.md
    agent: backenddeveloper
    done: true
    tracking_issue: 6
    depends_on: [0.4]
    acceptance_criteria:
      - terraform destroy executed against backend-dev.hcl leaves zero remaining knotify-smoke-dev-* buckets in the dev AWS account
      - File PIPELINE_VALIDATED.md committed at the repo root containing the date, the dev bucket name that was deployed, a one-line confirmation that dev destroy completed, and an explicit "PROD VALIDATION DEFERRED — see docs/PROD_CUTOVER.md" line
      - The smoke-test/dev branch is deleted from the remote
    notes: "Prod cleanup is part of the deferred 0.5 work; it will run when prod is brought online. Completed 2026-05-22: terraform destroy removed all 4 resources (bucket knotify-smoke-dev-ce43afa2 + public-access-block + SSE config + random_id.suffix); zero knotify-smoke-dev-* buckets confirmed; PIPELINE_VALIDATED.md committed (43c4558); smoke-test/dev branch deleted from remote."
