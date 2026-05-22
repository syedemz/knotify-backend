# knotify-backend — Context

## Elevator pitch
<1-2 sentences. Auto-populate from architecture.md after /create-plan, or user fills now.>

## Current phase
Not yet started. Awaiting `architecture.md` and `/create-plan`.

## Active blockers
- Production deploys are PAUSED. The prod AWS account has not been provisioned. All phases plan-against-prod but only apply-against-dev. See `docs/PROD_CUTOVER.md` for the full pause mechanism and the flip-on checklist.

## Critical design decisions
None yet.

## Recent changes
- 2026-05-21: project created via /start-project
- 2026-05-21: GitHub repo bootstrapped via /setup-repo (visibility: public, url: https://github.com/syedemz/knotify-backend)
- 2026-05-22: Production deploys paused at the CI/CD layer (DEPLOY_PROD=false + empty prod GitHub Environment + required-reviewer protection). Phase 0 prod stories deferred. Phase 11 hardening re-scoped to apply fully to dev with prod-side plans only. New docs/PROD_CUTOVER.md is the source of truth for flip-on.
- 2026-05-22: Renamed Terraform state buckets from knotify-tfstate-{env} to knotify-{env}-tfstate (i.e. knotify-dev-tfstate, knotify-prod-tfstate). DynamoDB lock table name knotify-tfstate-lock unchanged. Updated in phase-0 PRD, architecture.md §10.6, and docs/PROD_CUTOVER.md.
