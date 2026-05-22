# Smoke Test — Terraform Module

This module deploys a single S3 bucket to validate the end-to-end pipeline
(`git push → GitHub Actions → Terraform → AWS`). It is the phase-0 smoke test
described in `architecture.md` §10.6.

## Directory layout

```
infrastructure/smoke/
  main.tf            # S3 bucket + public-access-block + SSE config
  variables.tf       # Input variables (environment, region)
  outputs.tf         # Outputs (bucket_name)
  backend-dev.hcl    # Partial backend config for the dev environment
  backend-prod.hcl   # Partial backend config for the prod environment
  dev.tfvars         # Variable values for dev
  prod.tfvars        # Variable values for prod
```

## Why the backend is split into partial config files

Terraform requires a single backend block at init time, but the S3 bucket name
differs between environments (`knotify-dev-tfstate` vs `knotify-prod-tfstate`).
Embedding the bucket name directly in `main.tf` would force a single shared
state file, which violates the one-state-file-per-environment rule in
`codingprinciples.md`. The solution is a _partial backend configuration_: the
`terraform {}` block in `main.tf` declares `backend "s3" {}` without values;
the actual bucket, key, region, and DynamoDB table are supplied via
`-backend-config` at init time.

## Invocation pattern

### Dev

```sh
terraform init -backend-config=backend-dev.hcl
terraform plan  -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

### Prod

```sh
terraform init -backend-config=backend-prod.hcl
terraform plan  -var-file=prod.tfvars
terraform apply -var-file=prod.tfvars
```

> **Prod is currently gated.** The prod AWS account has not been provisioned.
> Do not run `terraform apply` against prod until the gate is opened.
> See `docs/PROD_CUTOVER.md` for the full checklist.

## Destroying after smoke validation

```sh
terraform destroy -var-file=dev.tfvars   # dev
terraform destroy -var-file=prod.tfvars  # prod (when prod is live)
```

## Prerequisites

- AWS credentials for the target environment exported as environment variables
  (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) or via an IAM role.
- The remote state bucket (`knotify-dev-tfstate` / `knotify-prod-tfstate`) and
  the DynamoDB lock table (`knotify-tfstate-lock`) must already exist. These
  are bootstrapped manually once as described in `architecture.md` §10.6.
