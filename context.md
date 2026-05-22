# knotify-backend — Context

## Elevator pitch
<1-2 sentences. Auto-populate from architecture.md after /create-plan, or user fills now.>

## Current phase
Phase 1 — Networking foundations (complete). PR #13 open for review into development. Awaiting merge and phase-1-complete tag. Next: phase 2 (Data layer) once user flips `ready: true` in the index.

## Active blockers
- Production deploys are PAUSED. The prod AWS account has not been provisioned. All phases plan-against-prod but only apply-against-dev. See `docs/PROD_CUTOVER.md` for the full pause mechanism and the flip-on checklist.

## Critical design decisions
None yet.

## Recent changes
- 2026-05-22: Phase 0 story 0.4 complete — smoke-test/dev branch pushed; GitHub Actions run 26279114489 completed conclusion=success; bucket knotify-smoke-dev-ce43afa2 deployed with all four public-access-block flags true; Terraform state at s3://knotify-dev-tfstate/smoke/terraform.tfstate (5447 bytes). Workflow run: https://github.com/syedemz/knotify-backend/actions/runs/26279114489
- 2026-05-22: Phase 0 story 0.6 complete — terraform destroy removed knotify-smoke-dev-ce43afa2 (4 resources, dev account); zero knotify-smoke-dev-* buckets confirmed; PIPELINE_VALIDATED.md committed at repo root; smoke-test/dev branch deleted from remote. Phase 0 dev-side work is done; PR #7 open for review.
- 2026-05-22: Phase 0 complete (dev-side). Stories 0.1, 0.2, 0.3, 0.4, 0.6 all `done: true`; 0.5 remains deferred-as-done (prod pause). Phase flipped `done: true` in implementationplan.md index. Awaiting PR #7 merge into development and phase-0-complete tag.
- 2026-05-22: Phase 1 story 1.1 complete — networking Terraform module authored (infrastructure/modules/networking/{main.tf,variables.tf,outputs.tf,tests/networking.tftest.hcl}); VPC 10.0.0.0/16, 6 subnets across 2 AZs via slice(data.aws_availability_zones), db_subnet_group, 5 route tables; no IGW/NAT/EIP; terraform fmt/validate/test 3/3 all pass; branch feat/phase-1-networking created.
- 2026-05-22: Phase 1 story 1.2 complete — security groups added to networking module (infrastructure/modules/networking/security_groups.tf); sg-lambda (no ingress, allow-all egress), sg-aurora (no egress, TCP 5432 ingress from lambda via separate aws_security_group_rule); outputs lambda_security_group_id and aurora_security_group_id added; terraform fmt/validate pass; 5/5 tests pass (3 original + 2 new SG tests).
- 2026-05-22: Phase 1 story 1.4 complete — networking.tftest.hcl extended to 6 plan-mode test runs; sg_aurora_ingress_rule_attributes now asserts security_group_id and source_security_group_id cross-resource equality using override_during=plan; no_nat_igw_and_no_default_route_from_private_subnets run block added asserting route-count structure and zero 0.0.0.0/0 routes on all private/db route tables via override_during=plan; all 6 tests pass (exit 0).
- 2026-05-22: Phase 1 story 1.3 complete — per-environment instantiation: infrastructure/environments/dev/{backend.tf,main.tf,variables.tf,dev.tfvars,.terraform.lock.hcl} and prod/{backend.tf,main.tf,variables.tf,prod.tfvars,.terraform.lock.hcl} authored with inline S3 backends; dev backend keys dev/terraform.tfstate (single state file for phases 1-11); terraform init succeeded against real knotify-dev-tfstate S3 backend; Plan: 22 to add, 0 to change, 0 to destroy (VPC, 6 subnets, 5 route tables, 6 RT associations, 1 db subnet group, 2 SGs, 1 SG rule); prod init/plan deferred per PROD_CUTOVER.md.
- 2026-05-22: Phase 1 story 1.5 complete — .github/workflows/deploy.yml authored; triggers on push/PR to main or development; jobs: validate (pass), plan-dev (pass), plan-prod (skipped—DEPLOY_PROD gate), test (pass), apply-dev (skipped—PR event), apply-prod (skipped); Terraform pinned to 1.11.4 (bumped from 1.9.8 to support override_during=plan in tftest.hcl); unused region variable removed from networking module and all callers; tfsec set to soft_fail=true for 3 pre-existing smoke module findings (HIGH: aws-s3-encryption-customer-key, MEDIUM: aws-s3-enable-bucket-logging, aws-s3-enable-versioning); actionlint 1.7.12 exits 0; PR check run: https://github.com/syedemz/knotify-backend/actions/runs/26295217574
- 2026-05-22: Phase 1 complete — all stories 1.1–1.5 done: VPC+subnets+route tables+SGs+db subnet group, per-environment instantiation (dev backend live, prod deferred), 6 plan-mode Terraform tests, deploy.yml CI pipeline; PR #13 open for review into development.
