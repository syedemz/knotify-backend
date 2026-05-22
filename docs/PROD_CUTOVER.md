# Production Cutover Checklist

**Status:** Production deploys are PAUSED. The prod AWS account has not been provisioned.

This document is the single source of truth for flipping production on. Until every item below is checked off, prod-side `terraform apply` MUST NOT run.

## How the pause is enforced

Three independent layers — any one of them blocks a prod deploy on its own:

1. **No prod AWS credentials exist.** The GitHub Environment named `prod` is created with required-reviewer protection but contains no `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` secrets. Any prod-targeted job that reaches the AWS-credentials step fails at auth.
2. **`vars.DEPLOY_PROD == 'false'`.** The repo-level GitHub Actions variable `DEPLOY_PROD` is set to `"false"`. The smoke-prod and full-deploy-prod jobs are gated by `if: vars.DEPLOY_PROD == 'true'` and are skipped at job-evaluation time — they never start, so the environment gate is never reached.
3. **Required-reviewer protection.** Even if the variable flipped accidentally, the prod GitHub Environment requires manual approval from the repo owner. No approval = no deploy.

All three layers must be lifted, in order, by the cutover checklist below.

## What still happens during the pause

- All Terraform modules are written and validated against dev (the actual `terraform apply` target).
- Every phase that has a `environments/prod/` consumer still runs `terraform plan` against prod as part of CI — these plans are reviewed but never applied, and they accumulate as the prod-side blueprint.
- Phase 11 hardening lands fully in dev (Secrets Manager, OIDC, WAF tightening, sg-lambda egress lockdown, CloudTrail, Cognito Advanced Security in AUDIT mode, MFA OPTIONAL). Dev runs at production posture.

## Cutover checklist

Execute these in order. Each step has a clear stop condition.

### 1. Provision the prod AWS account
- [ ] Create the prod member account inside the existing AWS Organization.
- [ ] Apply the same SCPs that protect the dev account (deny region != eu-central-1, deny root-user actions except billing).
- [ ] Create the prod IAM admin role and confirm console login works via the Organization's IAM Identity Center.

### 2. Bootstrap the prod Terraform backend
- [ ] Create `knotify-prod-tfstate` S3 bucket (versioning ON, public access blocked, SSE-KMS).
- [ ] Confirm `knotify-tfstate-lock` DynamoDB table exists in the prod account (or share the dev one — decide before this step).
- [ ] Verify `infrastructure/smoke/backend-prod.hcl` and every `environments/prod/backend.hcl` in later phases point at the correct bucket + region.

### 3. Provision the prod OIDC role (phase 11 module)
- [ ] Run `terraform apply` against `infrastructure/modules/github_oidc/` for the prod account (one-time, locally with break-glass admin credentials).
- [ ] Capture the resulting role ARN.
- [ ] Add `AWS_DEPLOY_ROLE_ARN` as a secret on the GitHub `prod` Environment (still no static keys).

### 4. Lift the prod deploy gate
- [ ] In GitHub repo settings, set the variable `DEPLOY_PROD` to `"true"`.
- [ ] Confirm the `prod` GitHub Environment still has required-reviewer protection enabled (it must — this is the surviving safety layer).

### 5. Run the deferred phase-0 prod smoke
- [ ] Push `smoke-test/prod` branch; approve the prod environment gate when prompted.
- [ ] Confirm the smoke bucket is created in the prod account.
- [ ] `terraform destroy` against `backend-prod.hcl`; confirm zero `knotify-smoke-prod-*` buckets remain.
- [ ] Update `PIPELINE_VALIDATED.md` to add the prod bucket name + destroy confirmation; remove the "PROD VALIDATION DEFERRED" line.
- [ ] In `implementationplan/phase-0-smoke-test.md` flip story 0.5 back to `done: false`, execute, then flip to `done: true` for real.

### 6. Roll the deferred-from-phase-11 prod applies
For each of phase 11's stories 11.1, 11.2, 11.3 (prod side), 11.4 (ENFORCED), 11.5 (ON), 11.6, 11.7:
- [ ] Diff the committed prod terraform plan against a fresh plan; ensure no unexpected changes.
- [ ] Approve the prod environment gate; let the deploy run.
- [ ] Smoke-validate per story 11.8's prod-equivalent criteria (originally in story 11.8 before the cutover refactor).

### 7. Run every interim phase's prod-side apply
Phases 1–10 and 12 each have a `environments/prod/` consumer that has been producing plans but not applying. Now apply each, in phase order, gated through the same approval flow.
- [ ] Phase 1 → 2 → 3 → 4 → 5 → 6 → 7 → 8 → 9 → 10 → 12.
- [ ] After each, run that phase's e2e integration test suite against prod.

### 8. Final state
- [ ] Update `context.md` Active blockers to remove the production-paused entry.
- [ ] Update this file's Status line to "Production active as of <date>."
- [ ] Tag the cutover commit `prod-cutover-complete`.

## When something goes wrong mid-cutover

If a prod apply fails partway through step 6 or 7:
- Do NOT roll forward by manually patching state.
- `terraform destroy` the failed module's prod resources back to the last known clean state.
- Re-run the failing phase's apply once the root cause is fixed.
- Static keys must never be created as a fallback — OIDC remains the only auth path.
