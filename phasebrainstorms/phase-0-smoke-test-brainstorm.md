# Phase 0 brainstorm — Pipeline smoke test

## 2026-05-22 10:37 brainstorm

PRD reviewed: `implementationplan/phase-0-smoke-test.md`. Six stories; 0.5 is pre-marked `done: true` (deferred per docs/PROD_CUTOVER.md). Findings below — none are blockers; all are clarifications a subagent can resolve, but worth surfacing.

### Missing or non-testable acceptance criteria

- **0.1** — The bucket-name pattern `knotify-smoke-${var.environment}-${random_id.suffix.hex}` implies a `resource "random_id" "suffix"` declaration, but the AC never names it. Implicit but worth making explicit so the subagent doesn't omit the resource and produce a non-unique bucket name.
- **0.3** — AC mentions `actionlint` must pass on the workflow file, but doesn't specify how it's run (local binary, Docker image, `rhysd/actionlint@v1` GHA, etc.). Subagent will pick; flagging so the choice is conscious. Recommend running it locally before push (Docker image `rhysd/actionlint:latest`) to avoid burning a CI cycle.
- **0.3** — `on.push.branches=[smoke-test/*]` — confirm whether the GitHub Actions glob `smoke-test/*` actually matches the single-segment branches `smoke-test/dev` / `smoke-test/prod`. It does (single `*` matches one path segment), so this is correct as written. No change needed; logging it because it's a common gotcha.

### Scope smuggling

None detected. Stories stay within the dev-only smoke scope. The deferred prod work (0.5) and the prod-cleanup half of 0.6 are explicitly held back.

### Dependency issues

- **0.6 depends_on: [0.4]** is correct — cleanup runs after the dev smoke deploy succeeds. It does **not** depend on 0.5 (prod), so the deferred 0.5 does not block 0.6.
- **0.3 depends_on: []** is correct — the workflow file is authored independently of the Terraform module, though the workflow references files produced by 0.1 and 0.2. Dispatch order ends up the same because 0.4 won't dispatch until 0.1, 0.2, and 0.3 are all done.

### Drift since the plan was written

- `context.md` confirms the renamed state-bucket convention (`knotify-dev-tfstate`, `knotify-prod-tfstate`) is reflected in the PRD. ✓
- The prod-pause is reflected (0.5 deferred, DEPLOY_PROD=false, empty prod env, required-reviewer). ✓
- No other drift detected.

### External dependencies / assumptions not yet validated

1. **AWS dev bootstrap.** Phase exit assumes: AWS Organization + dev member account exist, dev IAM user with static access key exists, `knotify-dev-tfstate` bucket exists with versioning, `knotify-tfstate-lock` DynamoDB table exists. The PRD's `context_summary` lists these as prerequisites — verify they were actually completed before dispatching 0.4. If any are missing, 0.4 will fail at `terraform init`.
2. **GitHub Environment "dev"** must already contain `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` as environment-scoped secrets. Verify with `gh api repos/:owner/:repo/environments/dev/secrets --jq '.secrets[].name'` before 0.4.
3. **GitHub Environment "prod"** must exist with required-reviewer protection but **no** AWS secrets. Verify with the same `gh api` call — secrets array should be empty.
4. **Repo variable DEPLOY_PROD=false** must be set (story 0.3 will set it if missing). Verify with `gh variable list`.
5. **Branch protection.** Workspace `gitbranching.md` protects `main` and `development`. The `smoke-test/*` branches must be free to push directly. Confirmed by inspection — no protection rule applies.
6. **Phase branch strategy.** Per workspace rules, all phase work lives on one branch `feat/phase-0-smoke-test` (or similar). Story 0.4 also pushes `smoke-test/dev` to trigger the workflow — this is a separate, ephemeral branch, deleted in 0.6. The PR opened against `development` is from the `feat/phase-0-*` branch and contains the module + workflow + `PIPELINE_VALIDATED.md`.
7. **Cost safety.** 0.6 must verify destroy success (zero remaining `knotify-smoke-dev-*` buckets) before flipping `done: true`. A failed destroy that goes undetected leaves an orphan S3 bucket. Subagent should run `aws s3api list-buckets --query "Buckets[?starts_with(Name, 'knotify-smoke-dev-')].Name"` post-destroy.

### Summary

PRD is implementable as-written. The seven assumption checks above should be verified by the subagent at the start of each affected story. No PRD edits required.
