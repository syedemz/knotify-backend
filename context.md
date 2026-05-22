# knotify-backend — Context

## Elevator pitch
<1-2 sentences. Auto-populate from architecture.md after /create-plan, or user fills now.>

## Current phase
Phase 0 — Pipeline smoke test (complete, pending PR #7 merge + phase tag). Next: phase 1 (Networking foundations) once user flips `ready: true` in the index.

## Active blockers
- Production deploys are PAUSED. The prod AWS account has not been provisioned. All phases plan-against-prod but only apply-against-dev. See `docs/PROD_CUTOVER.md` for the full pause mechanism and the flip-on checklist.

## Critical design decisions
None yet.

## Recent changes
- 2026-05-21: project created via /start-project
- 2026-05-21: GitHub repo bootstrapped via /setup-repo (visibility: public, url: https://github.com/syedemz/knotify-backend)
- 2026-05-22: Production deploys paused at the CI/CD layer (DEPLOY_PROD=false + empty prod GitHub Environment + required-reviewer protection). Phase 0 prod stories deferred. Phase 11 hardening re-scoped to apply fully to dev with prod-side plans only. New docs/PROD_CUTOVER.md is the source of truth for flip-on.
- 2026-05-22: Renamed Terraform state buckets from knotify-tfstate-{env} to knotify-{env}-tfstate (i.e. knotify-dev-tfstate, knotify-prod-tfstate). DynamoDB lock table name knotify-tfstate-lock unchanged. Updated in phase-0 PRD, architecture.md §10.6, and docs/PROD_CUTOVER.md.
- 2026-05-22: Phase 0 story 0.1 complete — smoke S3 Terraform module authored (infrastructure/smoke/{main.tf,variables.tf,outputs.tf}); aws ~>5.70 + random ~>3.6 providers pinned; terraform fmt -check and terraform validate both pass; branch feat/phase-0-smoke-test created; PR opened into development.
- 2026-05-22: Phase 0 story 0.2 complete — per-environment backend configs and tfvars added (infrastructure/smoke/{backend-dev.hcl,backend-prod.hcl,dev.tfvars,prod.tfvars,README.md}); partial backend pattern documented; terraform fmt -check passes (exit 0).
- 2026-05-22: Phase 0 story 0.3 complete — .github/workflows/smoke-test.yml authored; smoke-dev job guarded by refs/heads/smoke-test/dev ref check; smoke-prod job doubly guarded by ref + vars.DEPLOY_PROD=='true'; DEPLOY_PROD repo variable set to "false"; actionlint passes with exit 0; Terraform pinned to 1.9.8.
- 2026-05-22: Phase 0 story 0.4 complete — smoke-test/dev branch pushed; GitHub Actions run 26279114489 completed conclusion=success; bucket knotify-smoke-dev-ce43afa2 deployed with all four public-access-block flags true; Terraform state at s3://knotify-dev-tfstate/smoke/terraform.tfstate (5447 bytes). Workflow run: https://github.com/syedemz/knotify-backend/actions/runs/26279114489
- 2026-05-22: Phase 0 story 0.6 complete — terraform destroy removed knotify-smoke-dev-ce43afa2 (4 resources, dev account); zero knotify-smoke-dev-* buckets confirmed; PIPELINE_VALIDATED.md committed at repo root; smoke-test/dev branch deleted from remote. Phase 0 dev-side work is done; PR #7 open for review.
- 2026-05-22: Phase 0 complete (dev-side). Stories 0.1, 0.2, 0.3, 0.4, 0.6 all `done: true`; 0.5 remains deferred-as-done (prod pause). Phase flipped `done: true` in implementationplan.md index. Awaiting PR #7 merge into development and phase-0-complete tag.
