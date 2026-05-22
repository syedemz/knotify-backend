# Phase 1 brainstorm — Networking foundations

## 2026-05-22 14:26 brainstorm

PRD reviewed: `implementationplan/phase-1-networking.md`. Four stories (1.1, 1.2, 1.3, 1.4). All `backenddeveloper`. Findings below — several are real and may require PRD edits before dispatch.

### Missing or non-testable acceptance criteria

- **1.1 — IGW presence is implicit, not explicit.** The AC says "No aws_nat_gateway, no aws_internet_gateway *route from private subnets*, no eip." It does NOT say whether the VPC itself has an internet gateway. Architecture §6.1 calls public subnets "for future NAT/ALB" — in v1 they have nothing in them. Architecture §6.4 says "no NAT Gateway; no internet routing from private subnets." Provisioning an IGW that nothing uses is a no-op and clutters the diff. Recommendation: subagent does NOT create an `aws_internet_gateway`. Public subnets exist as empty allocations until a later phase introduces an ALB or NAT.
- **1.1 — AZ selection is non-deterministic.** AC says "across distinct AZs (data.aws_availability_zones.available)." That data source returns all AZs in the region — currently 3 for eu-central-1. The module must slice deterministically to the first two (e.g., `data.aws_availability_zones.available.names[0..1]`) so applies are stable across runs. Worth making explicit.
- **1.3 — "without surfacing destructive diffs against the smoke-test state" is unclear.** The smoke module used a different state key (`smoke/terraform.tfstate`) and was destroyed in phase 0.6. The new env state key will be different (proposed below). They are independent state files — there is no possibility of a destructive diff between them. The AC is probably trying to say "don't accidentally collide with the smoke state's key." Recommend rewording to "uses a distinct state key from the smoke-test deploy" so it's verifiable.
- **1.4 — `terraform test` runner mode is unspecified.** Module-level tests can run with `command = plan` (no AWS calls, fast) or `command = apply` (real AWS). For a hermetic test suite suitable for CI without credentials, all three required cases should use `command = plan` plus assertions on the planned values. Recommendation: subagent uses `command = plan` exclusively in this story.

### Scope smuggling

- None detected. Stories stay within networking primitives. No Aurora, no Lambda, no IGW for "future use."

### Dependency issues

- 1.3 depends on 1.1 + 1.2 — correct.
- 1.4 depends on 1.1 + 1.2 — correct. (Tests can be written against the module independently of any environment instantiation.)
- 1.3 and 1.4 are siblings (no dependency between them). Per the strict-serial rule, they still run one at a time — but order between 1.3 and 1.4 is a judgment call. Recommend running 1.4 BEFORE 1.3: passing tests before instantiating in real environments catches module bugs without burning a `terraform apply` cycle. The PRD's depends_on graph allows either order.

### Drift since the plan was written

- **State bucket names** are now `knotify-dev-tfstate` / `knotify-prod-tfstate` (renamed during phase 0 prep, recorded in context.md). The PRD references "from phase 0" which is still accurate, but a subagent reading only the PRD won't know the exact bucket names. The subagent must read `infrastructure/smoke/backend-dev.hcl` / `backend-prod.hcl` from the merged phase-0 work to get the right values.
- **Terraform pinned to 1.9.8** (per phase 0 story 0.3). Phase 1 must use the same version. `aws ~>5.70` and `random ~>3.6` already proven from phase 0; should be reused.

### External dependencies / assumptions not yet validated — these are blockers

1. **PROD STATE BUCKET DOES NOT EXIST.** Story 1.3 AC #4 says "`terraform init then terraform plan succeeds in both environments`". The prod AWS account has not been provisioned (per context.md active blockers + docs/PROD_CUTOVER.md). The bucket `knotify-prod-tfstate` does not exist; the lock table `knotify-tfstate-lock` does not exist in any prod account. `terraform init -backend-config=backend-prod.hcl` will fail with `NoSuchBucket`. **This AC is currently unsatisfiable as written.**

   Resolution options:
   - **(a)** Re-scope AC #4 to: "`terraform init` and `terraform plan` succeed in dev. Prod-side init/plan is deferred per docs/PROD_CUTOVER.md until the prod state bucket is provisioned." Match the dev-only-validation pattern from phase 0.5.
   - **(b)** Bootstrap the prod state bucket now as a prerequisite for phase 1. (Doesn't match the project's pause posture; not recommended.)
   - **(c)** Skip the prod-side environment file entirely until cutover. (Diverges from architecture §10.1 which calls for symmetric `dev/` + `prod/` dirs.)

   Recommendation: **(a)**. Author both `dev/main.tf` and `prod/main.tf` so the layout is correct, but only `terraform init -backend-config=backend-dev.hcl && terraform plan -var-file=dev.tfvars` is required to succeed. Prod init is deferred-as-tested with a note in the PRD pointing at docs/PROD_CUTOVER.md.

2. **State key not specified.** Both envs need a `key =` line in `backend.tf`. The smoke deploy used `smoke/terraform.tfstate`. The real env state must use a different, deterministic key that all subsequent phases will share so that at end of phase 11 there is exactly **one state object per environment** (matching the owner's stated end-state goal in this session). Recommendation: `environments/dev/terraform.tfstate` (or simpler: `terraform.tfstate` at the bucket root). Then phases 2–11 add modules to the same `infrastructure/environments/<env>/main.tf` and the state file grows cumulatively. **The PRD should pin this key explicitly to prevent later phases from each picking a different one.**

3. **`deploy.yml` is NOT in phase 1's stories.** Architecture §10.2 specifies a `.github/workflows/deploy.yml` that auto-applies on push to `development`. Phase 1 doesn't include it. That means after phase 1 merges, push-to-development will NOT deploy networking — only `smoke-test/**` triggers anything. The user has explicitly asked for "push to development → deploy" as the end state.

   Options:
   - **(a)** Add a fifth story to phase 1: "Author `.github/workflows/deploy.yml` and verify push-to-development deploys the networking module to dev." This brings the deploy workflow online at the earliest possible moment.
   - **(b)** Leave deploy.yml for a later, dedicated CI/CD story (perhaps in phase 11 hardening). Until then, all deploys are manual `terraform apply` from the user's terminal.

   Recommendation: **(a)** — add the story. The smoke-test workflow proved the credentials and S3 backend. Wiring `deploy.yml` now is incremental and means the user gets the push-to-development experience from phase 1 forward.

4. **AWS dev credentials must work locally for the user.** Story 1.3 requires `terraform init` + `plan` to succeed. If this is run from a subagent on the user's Windows machine, the subagent needs `aws sts get-caller-identity` to return the dev account. The user confirmed dev bootstrap is in place during phase 0 — the same credentials apply here.

5. **`terraform test` command requires Terraform ≥1.6.** We have 1.9.8 pinned. Fine.

### Summary

PRD is mostly solid but has three real issues that should be addressed before dispatch:

| # | Story | Issue | Severity |
|---|-------|-------|----------|
| A | 1.3 | Prod-side init+plan AC is unsatisfiable (prod bucket doesn't exist) | **blocker** |
| B | 1.3 | Backend state key not specified — risks each phase picking a different key | **blocker for end-state goal** |
| C | n/a | Phase 1 doesn't ship `deploy.yml`; push-to-development won't deploy anything | **design gap** |

Minor clarifications also worth adding:
- 1.1 IGW intent (none in v1)
- 1.1 AZ slicing (first 2 deterministic)
- 1.3 wording fix on the smoke-state diff line
- 1.4 use `command = plan` for hermetic tests
- Recommend executing 1.4 before 1.3 (allowed by depends_on, catches module bugs cheaply)

If the user wants the smoothest path: edit the PRD to address A/B/C, then re-run. If they want to proceed, the subagent for 1.3 will need explicit per-dispatch instructions to cover A (defer prod) and B (use `environments/<env>/terraform.tfstate` key); and the deploy.yml gap (C) will sit unresolved until a later phase.

## 2026-05-22 14:43 brainstorm (re-run)

PRD re-read after edits. The five stories now in `implementationplan/phase-1-networking.md` are 1.1, 1.2, 1.3, 1.4, 1.5. Comparing against the prior findings:

### Prior concerns — status

| # | Story | Prior issue | Status |
|---|-------|-------------|--------|
| A | 1.3 | Prod-side init/plan unsatisfiable | **Addressed.** AC #6 now scopes the init+plan requirement to dev only against the real dev backend. AC #7 explicitly defers prod-side init+plan per `docs/PROD_CUTOVER.md`; prod files are authored for layout symmetry. Matches the dev-only-validation pattern from phase 0.5. |
| B | 1.3 | State key unspecified — risk of per-phase divergence | **Addressed.** AC #3 pins `key = "dev/terraform.tfstate"` with an inline note that this single state file holds ALL dev infra across phases 1–11. AC #4 mirrors for prod with `key = "prod/terraform.tfstate"`. The story `notes` reinforce that phases 2–11 add modules to the same `environments/<env>/main.tf` and write to this same key. End-state single-state-file goal is now locked in. |
| C | n/a | No deploy.yml story — push-to-development won't deploy anything after phase 1 | **Addressed.** New story 1.5 ships `.github/workflows/deploy.yml` per architecture §10.2 with the full job graph (validate / plan matrix / test / apply-dev / apply-prod), terraform 1.9.8 pinned to match phase 0, three-layer prod pause preserved (plan-prod + apply-prod both gated on `vars.DEPLOY_PROD=='true'`), environment-scoped AWS secrets, PR-time verification path enumerated in AC. |
| Minor | 1.1 | IGW intent not explicit | **Addressed.** AC #4 now states "No aws_nat_gateway, no aws_internet_gateway resource at all, no eip" with reference to architecture §6.1/§6.4. |
| Minor | 1.1 | AZ slicing non-deterministic | **Addressed.** AC #2 now requires deterministic slicing from `data.aws_availability_zones.available.names` with the explicit "no hardcoded AZ names" qualifier. |
| Minor | 1.3 | "destructive diff" wording unclear | **Addressed.** AC #6 now reads "no destructive diff against any existing object in s3://knotify-dev-tfstate/dev/terraform.tfstate (which is the empty starting state)" — verifiable and correct given the smoke state was destroyed in phase 0.6. |
| Minor | 1.4 | `terraform test` runner mode unspecified | **Addressed.** AC #2 now requires `command = plan` for all test runs (hermetic, no credentials, CI-friendly). |
| Minor | exec order | Recommend 1.4 before 1.3 | **Captured.** Story 1.4's `notes:` records the recommendation; `depends_on` permits either order. Dispatch order will be 1.1 → 1.2 → 1.4 → 1.3 → 1.5. |

### New observations on story 1.5

- **AC alignment with smoke-test.yml is good.** Same Terraform version (1.9.8), same env-scoped secrets pattern (`environment: dev/prod` + `${{ secrets.AWS_ACCESS_KEY_ID }}`), same prod gate idiom (`vars.DEPLOY_PROD == 'true'`). Subagent has a working reference to copy from.
- **Test job iteration scope is sound.** "Iterate `infrastructure/modules/*/tests` and run `terraform -chdir=<module-dir> test` for each" matches Terraform's module-test discovery model. With only networking having tests in phase 1, the loop must still exit 0 — that's covered by the AC explicitly.
- **PR-time verification AC is unambiguous.** Concrete pass/skip outcomes per job are listed (validate=success, plan(dev)=success, plan(prod)=skipped, test=success, apply-*=skipped). The story can be verified objectively from the PR check run.
- **Permissions block (`contents: read, id-token: write`)** is forward-looking for OIDC adoption; harmless today since we still use long-lived keys, and explicit `id-token: write` does not weaken anything.
- **Apply-dev gating: `github.event_name == 'push' && github.ref == 'refs/heads/development'`.** This means PR check runs never apply, only merges. Correct for the end-state goal.

### Residual concerns / risks

- **`tflint` and `tfsec` are introduced for the first time in 1.5's validate job.** Phase 0 only ran `terraform fmt -check`. If either tool flags an issue against the networking module that wasn't caught locally, the PR check will fail and we'll iterate. Not a blocker — that's exactly what the validate job is for. Worth flagging to the 1.5 subagent so it tests locally before pushing.
- **`infrastructure/environments/dev/backend.tf` is inline (not partial).** Phase 0 used partial backends with `-backend-config=backend-dev.hcl`. The 1.3 AC moves to inline backends. This is the right call for per-env directories (no flag juggling) but it's a deliberate divergence from the smoke pattern — the 1.3 subagent must not copy the smoke pattern.
- **5-story phase is the largest so far.** All `backenddeveloper`. Strict-serial means ~5 sequential dispatches. Expected and acceptable.
- **`terraform plan` in 1.3 requires real dev AWS credentials.** Same condition that satisfied phase 0; carries forward. The 1.3 subagent will run `aws sts get-caller-identity` first per its usual hygiene.

### Drift since prior brainstorm (none significant)

- Phase 0 is now fully merged and tagged (`phase-0-complete` on `064ff25`); branch `feat/phase-0-smoke-test` deleted local+remote; dev state bucket holds only the empty `dev/` key prefix today (smoke state was destroyed in 0.6). All assumptions used to write the PRD edits are intact.

### Summary

All three prior blockers (A, B, C) and all four minor clarifications are addressed in the current PRD. No new blockers identified. The phase is ready to dispatch.

Recommendation: **proceed**. Dispatch order 1.1 → 1.2 → 1.4 → 1.3 → 1.5.
