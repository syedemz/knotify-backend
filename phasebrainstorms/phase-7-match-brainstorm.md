# Phase 7 brainstorm — Match and deck

## 2026-06-15 10:15 brainstorm

Findings against `implementationplan/phase-7-match.md`, with cross-checks against `architecture.md §5.1/§5.2/§5.5/§5.7`, migrations 0001/0002/0007/0009, the phase-6 profile handler, and post-merge hotfixes #84–#87.

### Critical gaps (would break the phase if not addressed)

1. **Story 7.4 is broken as written: `app_user` cannot execute `refresh_deck_view()`.**
   Migration 0009 explicitly comments: *"EXECUTE on refresh_deck_view() is NOT granted to app_user — refresh is an admin/scheduler operation. Only the master (knotify) can invoke it."* The story's second AC ("PATCH /v1/profile/me ... is updated to enqueue an immediate refresh ... call `refresh_deck_view()` inline") will get a permission-denied. Decide one of:
   - grant EXECUTE to `app_user` (cheapest, but widens RLS-protected role's surface)
   - have profile Lambda assume a separate `aurora_refresh` role / different secret
   - PATCH publishes to SNS/SQS; scheduled refresh Lambda is the only invoker
   Pick a path and rewrite AC accordingly.

2. **Story 7.2 assumes RLS on `deck_view`. RLS doesn't propagate through materialized views.**
   Migration 0007 enables `FORCE ROW LEVEL SECURITY` on `users`. `deck_view` is a separate object built by the privileged migrator role; reads from it are NOT filtered by the `users_opposite_sex_only` policy. Either (a) add `sex != current_setting('app.requesting_user_sex', true)` directly to the deck SQL, or (b) wrap a security_barrier view on top of `deck_view`. AC currently says "applies the opposite-sex filter via RLS" — false premise, must be revised.

3. **Story 7.6 conflates search and deck ordering, but `deck_view` has no `preference_vector` column.**
   Migration 0009 projects 14 columns (excludes `preference_vector`). Story 7.6 AC: *"GET /v1/match/deck returns the same set"* as search (which is ordered by `preference_vector <=> requester.preference_vector`). The deck handler cannot produce that ordering from `deck_view` as currently shaped. Either:
   - add `preference_vector` (and `religion`, `sex`, `age`, `current_residence_country` if used for filtering) to `deck_view` via a new migration 0011, OR
   - 7.6's "same set" means *unordered membership equality*, not order equality — rephrase the AC.

4. **`preference_vector` is never populated. Phase 6 profile PATCH writes `preferences` (JSONB) but doesn't derive `preference_vector`.**
   Confirmed by reading `profile/handler.py:_MUTABLE_FIELDS`: `preferences` is accepted, `preference_vector` is not even in the table mutation path. Story 7.3 only creates the encoder utility. Add an explicit AC to 7.3 (or a new story 7.3a): *"PATCH /v1/profile/me — when `preferences` is in the patch body, the handler recomputes `preference_vector = encode_prefs(preferences)` and writes it in the same UPDATE."* Without this, every user's vector stays NULL and ranking is meaningless.

### Significant gaps

5. **No story for the knotify-match Lambda scaffold + IAM role + Terraform module.**
   Phase 6 had `module "profile"`, `module "friends"`, etc. each with route wiring as a separate step. Phase 7 implicitly bundles "create the Lambda + role + module + JWT integration + permission" into 7.1 alongside the SQL work. That worked for phase 0 with a single-route smoke test, but it's brittle when a story spans Python + Terraform + IAM. Suggest splitting: 7.0 (Lambda scaffold + IAM `aurora_reader` role + TF module shell), 7.1 (search route logic), 7.2 (deck route logic), 7.5 (route wiring — keep).

6. **Blocks-exclusion not aligned with hotfix #87 shared helper.**
   Story 7.1 AC says *"NOT EXISTS subquery against blocks (both directions)"* — verbatim ad-hoc SQL. Phase 6 hotfix #87 standardized this in `knotify_db.block_filter()` with alias-qualified column whitelist (`bk.bookmarked_user_id`, etc.). Stories 7.1 and 7.2 should reuse `block_filter("u.user_id")` (and add `u.user_id` to the whitelist) — not roll their own. Otherwise we repeat the alias-bug that triggered hotfix #87.

7. **Cursor pagination on deck (story 7.2) is non-testable as written.**
   "Pagination by cursor" — what's the cursor? `user_id`? `(preference_distance, user_id)`? What's the stable ordering? What happens when `deck_view` refreshes mid-pagination and a page-boundary user vanishes? Either define a concrete ordering + cursor format (e.g., `user_id` lexicographic, returning `next_cursor: <last_user_id>`) or drop cursoring for v1 and return a deterministic top-N with offset.

8. **Hard-filter columns: `current_residence_country` vs `resident_country_code`.**
   Schema (0002) has both. Architecture §5.5 SQL filters on `current_residence_country = ANY($4)`. Story 7.1's body accepts `countries (list)` — but is the client sending ISO-2 codes (CHAR(2)) or free-text names? Pick one column and one format; reject the other in the input schema. Otherwise the filter silently mismatches.

9. **`subsect` is matching-critical (§5.7) but not in story 7.1 filters.**
   §5.7 lists `religion` AND `subsect` as immutable matching-critical fields. Story 7.1 only filters on `religion`. Decide intentionally: include `subsect` in the filter body, or document in the story notes that v1 ships religion-only and subsect is deferred.

### Drift since plan was written

10. **PATCH handler is committed and stable; story 7.4 retroactively edits a phase-6 file.**
    `infrastructure/src/functions/profile/handler.py` is committed and passing tests. Story 7.4's second AC mutates that file — which is fine, but the story should explicitly list the test file (`tests/unit/test_profile.py`) and the integration test (`tests/integration/test_profile.py`) as artifacts to update, and call out that re-packaging `profile.zip` + redeploying via Terraform is part of the story.

11. **Phase 2/6 schema reality is intact:** `users.preference_vector vector(20)` exists, HNSW index `idx_users_vector` exists, `deck_view` + `refresh_deck_view()` exist, `block_filter()` helper exists (alias-qualified post-#87). The §5.5 SQL is implementable verbatim. No drift here — these are all confirmed against migrations and the db layer.

### Minor / clarifications

12. **`u.sex = $2` in §5.5 SQL is redundant with RLS.** RLS already enforces opposite-sex visibility on `users`. The story-7.1 SQL can drop the explicit `sex` predicate (RLS does it). Worth a one-line note in the story to avoid the reviewer asking "why two filters?"

13. **Refresh-concurrency:** `REFRESH MATERIALIZED VIEW CONCURRENTLY` requires the unique index (we have `idx_deck_user`, good) and serializes refreshes — two concurrent calls will block. With both a 15-min scheduler and inline PATCH-triggered refreshes, contention is possible but bounded (refresh of 10k rows is fast). Worth a `pg_try_advisory_lock` gate or scheduler skip-if-running, but not blocking for v1 if depend (4) on a queue.

14. **Story 7.6 test data builder:** the e2e test seeds 10 candidates "with deterministic preference vectors (each candidate's vector is the requester's vector plus a known perturbation, producing a strict ranking)". Useful to add a `conftest.py` fixture `seeded_match_candidates(requester_id, n=10)` so 7.6 doesn't reimplement seeding logic. Mirror phase-6's `completed_profile_user(sex)` pattern.

15. **`tracking_issue`-style references in PRD:** PRD has no `tracking_issue:` fields yet — that's expected; the implement-phase command creates them in Step 1. Just noting it's a clean slate.

### `depends_on` review

- 7.1, 7.2, 7.3, 7.4 listed as `depends_on: []`. Mostly correct, but **7.4 should depend on a "knotify-match Lambda exists" anchor** if you split 7.0 out (see finding 5). And if findings 2 + 3 are addressed by adding columns to `deck_view`, then 7.2 effectively depends on that migration — make it a new story 7.0 (or 7.2a) and link `depends_on: [7.0]` to 7.2.
- 7.5 depending on [7.1, 7.2] — good.
- 7.6 depending on all — good.

### External assumptions to validate before code

- `psycopg2` recognizes `vector(20)` parameter binding without an extension adapter? (Phase 2 brainstorm should have addressed this; check `knotify_db` layer for any pgvector type registration. If missing, the writer for `preference_vector` must use `cur.execute("UPDATE ... SET preference_vector = %s::vector", [str(vec_as_list)])` casting.)
- HNSW index gives consistent ranking under cosine distance with vectors of all zeros? `encode_prefs({})` returns `[0.0]*20` — cosine distance from any other vector to the zero vector is undefined (divide by zero norm). Add an AC to 7.3: empty-preferences requesters return an explicit error OR fall back to no-vector ranking (filter-only). Otherwise the search SQL produces NaN/undefined ordering for users who haven't set any preferences.

### Summary

There are **four blocking issues** (1, 2, 3, 4) that require PRD edits before implementation. The rest are tightenings. Recommended order of fixes:

1. Resolve refresh-permission (finding 1) and pick the inline-vs-async refresh strategy.
2. Drop the "RLS handles it" assumption on `deck_view` (finding 2) — either add columns + a security_barrier wrapper, or push the sex predicate into the handler SQL.
3. Decide whether deck is ordered by vector distance (which forces a new migration to add columns to `deck_view`) or by something else (finding 3).
4. Add an explicit AC to 7.3 (or a new story) wiring `encode_prefs()` into the profile PATCH handler (finding 4).

Once those are in the PRD, the remaining stories are clean and dependencies are correct.

## 2026-06-16 08:20 brainstorm

Re-brainstorm against the revised PRD (`implementationplan/phase-7-match.md`, `last_updated: 2026-06-15`). The PRD now incorporates the 2026-06-15 round's blocking issues and ships nine stories: 7.0, 7.0a, 7.0b, 7.1, 7.2, 7.3, 7.4, 7.5, 7.6. Cross-checks done against the live repo (post-hotfix-#87 layer code, migration set 0000–0010, `module/api_gateway/outputs.tf`, `profile/handler.py`).

### Blocking — must fix before dispatch

1. **Wrong module path for `block_filter()` (Stories 7.1 + 7.2).**
   PRD says: *"Block filtering uses knotify_db.block_filter(...)"* and *"the shared helper's whitelist is extended to include 'u.user_id'"*.
   Reality: `block_filter` lives in `knotify_obs` (`src/layers/observability/knotify_obs/_blocks.py`), not `knotify_db`. The phase-6 callers (`friends/handler.py`, `bookmarks/handler.py`) import from `knotify_obs`. A subagent following the PRD literally will write a failing import. Replace both AC mentions with `knotify_obs.block_filter`.

2. **Whitelist needs two entries, not one (Stories 7.1 + 7.2).**
   Hotfix #87 made the whitelist alias-qualified. Story 7.1 queries `users` aliased as `u`, so it needs `"u.user_id"` added. Story 7.2 queries `deck_view` *without* an alias and calls `block_filter("user_id")` — so it also needs the **bare** `"user_id"` whitelisted. The current PRD only mentions the alias-qualified entry. Either (a) alias deck_view (`FROM deck_view dv`) and use `dv.user_id`, or (b) add both `"u.user_id"` and `"user_id"` (or `"dv.user_id"`) to the whitelist in story 7.1. Pick one and make it explicit.

3. **Empty-vector fallback wording bug (Story 7.1 AC, third bullet).**
   AC reads: *"when the requester's preference_vector is NULL or all zeros (any of u.preference_vector being NaN)"*. The parenthetical mixes up requester and candidate vectors — `u` is the *candidate* table alias in §5.5 SQL. The fallback triggers on the *requester's* vector state alone (NULL or all zeros). Candidates' vectors being zero is normal data, not a fallback trigger. Rewrite to: *"when the requester's preference_vector is NULL or all zeros, omit the cosine ORDER BY"*.

### Significant — should fix before dispatch

4. **Async refresh-invoke transaction boundary (Story 7.4 AC, fifth bullet).**
   AC says the invoke "happens AFTER the DB commit succeeds". Today `profile/handler.py` flips `profile_complete_verified` inside `with conn:` (txn commits when the `with` block exits). The boto3 invoke must be issued **outside** that `with` block. The current AC wording could be read as "after the UPDATE statement" — which would dispatch the invoke from inside an uncommitted txn (if the txn rolls back, the refresh fires against stale state). Tighten the AC to specify "outside the `with conn:` block".

5. **Advisory-lock integration test side-channel unspecified (Story 7.4 AC, last bullet).**
   AC: *"invoke the refresh Lambda twice in parallel; both succeed; only one actually performed the refresh (assert via log inspection or DB-side instrumentation)"*. Neither channel exists today. Log inspection from a parallel pytest is flaky; DB-side instrumentation requires a new `refresh_log(refreshed_at)` table or similar. Either define the table (and add it to migration 0013), or drop "log inspection" and rely on a CloudWatch-Logs query in the test fixture. Otherwise the AC is "not testable as written".

6. **`cognito_pre_token_generation` Aurora roundtrip on every token (Story 7.0b AC, third bullet).**
   AC says the pre-token-gen Lambda is "extended to inject custom:profile_complete claim ... sourced from Aurora users.profile_complete_verified". This means **every** access-token issuance (and every refresh) opens a VPC connection to Aurora and runs `SELECT profile_complete_verified FROM users WHERE user_id=$1`. With Cognito's default 1-hour access-token TTL and a refresh cycle, that's one Aurora roundtrip per user per hour on the auth path — bounded but non-trivial latency. Alternative: maintain the flag as a Cognito custom attribute updated via `admin_update_user_attributes` after the PATCH flip (phase-6 profile Lambda already has admin scope; one write per onboarding flip, zero auth-path queries). Decide intentionally. If Aurora-on-every-token is the choice, ensure pre-token-gen gets `aurora_reader_match`'s VPC config + secret access (the existing pre-token-gen Lambda may not be VPC-attached today — verify).

7. **Token-refresh trigger in 7.0b integration test (AC, sixth bullet).**
   AC: *"subsequent token refresh issues a JWT with custom:profile_complete=true"*. The test needs to **call** the refresh explicitly (`cognitoUser.refreshSession()` or `boto3 cognito-idp initiate_auth(REFRESH_TOKEN_AUTH)`). Without that explicit call, Cognito returns the cached access token until the access-token TTL expires (default 1h), and the test will sit there 60 minutes. Add the explicit refresh step to the AC.

### Minor — worth noting

8. **Migration numbering convention (Stories 7.0a, 7.0b, 7.4).**
   Stories 7.0a, 7.0b have `depends_on: []` (topologically parallel-eligible) and each writes a migration: 0011 and 0012 respectively. Story 7.4 writes 0013. yoyo runs migrations in filename order, so the assignment is safe — but the PRD doesn't state "no other story will claim 0011/0012/0013". Worth one line: *"if another story or hotfix lands a migration between now and dispatch, renumber this one and update the references."* Belt-and-braces.

9. **Migrator-Lambda secret-generation pattern reuse (Story 7.4 AC, first bullet).**
   AC: *"Migrator Lambda generates a random password post-yoyo and stores it in a new secret knotify-<env>-aurora-refresh-credential"*. The existing migrator handler creates `app_user_credential` similarly (phase 2). Story should explicitly say "follow the existing app_user_credential pattern in `db_migrator/handler.py`" so the subagent doesn't invent a new pattern.

10. **Negative-scope in 7.0b decorator coverage.**
    AC lists `friends/`, `bookmarks/`, `blocks/` handlers as the files to decorate, plus the "EXCEPT profile/me" carve-out. The negative list (cognito_post_confirmation, cognito_pre_token_generation, db_migrator, _smoke, hello) is implicit. A subagent might over-decorate. Add an explicit "do NOT decorate the Cognito-trigger Lambdas or db_migrator" line.

11. **knotify_obs decorator file placement (Story 7.0b).**
    AC says the decorator "lives in the knotify_obs layer" without naming the file. The existing pattern is one file per decorator (`_edge_secret.py`, `_chat_room_id.py`, `_blocks.py`). Subagent should create `_profile_complete.py`. Not a blocker — pattern is obvious — just noting.

12. **`depends_on` review.**
    - 7.5 depends on [7.0, 7.0b, 7.1, 7.2] — correct (7.5 wires routes that require both the handler stories and the decorator from 7.0b).
    - 7.6 depends on all eight prior stories — correct (it exercises the full path end-to-end including refresh and onboarding gate).
    - 7.0, 7.0a, 7.0b, 7.3 all `[]` — correct (all four are leaves until the handlers consume them).
    - 7.4 `[7.0a]` — correct (refresh needs the extended deck_view to be meaningful).
    - 7.1 `[7.0]` and 7.2 `[7.0, 7.0a]` — correct.
    No dependency edits needed.

### External assumptions still unvalidated

- **psycopg2 vector parameter binding.** Story 7.3 notes the `%s::vector` cast path. The pgvector Python package (`pgvector.psycopg2.register_vector(conn)`) is NOT installed in knotify_db's layer manifest today (verified via the layer dir listing). The cast-string path is the only viable option without a layer change. AC should make this explicit so a subagent doesn't try to `register_vector` and silently fail at runtime.
- **HNSW index with NaN/zero vectors.** The fallback path in 7.1 explicitly bypasses the cosine ORDER BY when the requester's vector is empty — good. But candidates with all-zero vectors will still appear in the cosine path (their cosine distance to the requester is undefined). Postgres returns NaN for cos-distance to a zero vector; ORDER BY NaN's behavior is implementation-defined. Either filter candidates with `preference_vector IS NOT NULL AND preference_vector != '[0,0,...]'::vector` in the SQL, or accept that some candidates land at the end with undefined order. Decide.

### Drift since 2026-06-15

None new. Hotfixes #84–#87 were already in place when the PRD was finalized. The 2026-06-15 e2e probe (all four phase-6 domains 200-green via CloudFront) confirms phase-6 ground truth — no rework needed.

### Summary

- **Three blocking findings** (1, 2, 3): trivial PRD wording fixes (`knotify_db` → `knotify_obs`, whitelist entry list, requester-vs-candidate clarification).
- **Four significant findings** (4, 5, 6, 7): warrant PRD updates to make the AC testable / decide auth-path strategy.
- **Five minor findings** (8–12): notes for the subagents; PRD edits optional.
- **Two external assumptions** (pgvector binding + zero-vector ordering): document inline in 7.1 / 7.3.
- **No dependency edits** needed.
- **No drift** since the PRD was finalized.

Once these are addressed (or explicitly waived), the PRD is implementation-ready.

### Resolution — 2026-06-16

Owner answered all findings in `questions/answers7.txt`. PRD updated in the same session; `last_updated: 2026-06-16`. Summary of dispositions:

- **Findings 1, 2, 3, 4, 7, 8, 9, 10, 11**: accepted as recommended. Edits applied to 7.1 (block_filter path + fallback wording + NaN candidate acceptance), 7.2 (alias `deck_view` as `dv`, block_filter path), 7.0b (refresh step, negative scope, decorator file name), 7.4 (outside-`with conn:` invoke, migrator-pattern reuse).
- **Finding 5**: option (a) chosen — migration 0013 also creates `refresh_log(refreshed_at)` table; advisory-lock integration test counts rows.
- **Finding 6**: option (b) chosen — `custom:profile_complete` is a real Cognito custom attribute updated via `admin_update_user_attributes` from the profile PATCH handler. Pre-token-gen Lambda copies the attribute into the access-token claim. No per-token Aurora roundtrip. Eventual consistency footnote added to 7.0b notes. Profile Lambda IAM extended with `cognito-idp:AdminUpdateUserAttributes`.
- **External assumption A (pgvector)**: accepted. Story 7.3 AC explicitly states `%s::vector` cast path; `register_vector()` NOT used; pgvector NOT added to layer manifest.
- **External assumption B (zero-vector candidates)**: accepted — arbitrary ordering for zero-vector candidates is acceptable for v1; no explicit filter added.

**WIDENED_CHECK_FIELDS finalized** (35 fields total):
- Added to REQUIRED vs prior proposal: `marriage_time` (conditional CHECK: `marital_status = 'single' OR marriage_time IS NOT NULL`), `relation` (self-vs-on-behalf-of indicator).
- Removed from REQUIRED vs prior proposal: `graduation_year`, `high_school_passing_year`, `higher_secondary_passing_year`, `partners_religious_level`.
- Reconfirmed NOT required: `phone_number`, `photo_url`, `chosen_profile_avatar`, `preferences`, `preference_vector`, siblings.

PRD is implementation-ready. Step 1 (tracking-issue sweep) is next.

## 2026-06-16 09:00 brainstorm (post-edit re-check)

Re-read the PRD after the 2026-06-16 edits to catch gaps introduced by the new ACs themselves. Three findings — one blocking, two significant.

### Blocking

**B1. Cognito custom attribute declaration collides with hotfix #85's `ignore_changes = [schema]` lifecycle.**

`infrastructure/modules/cognito/main.tf:175` carries `lifecycle { ignore_changes = [schema] }` on `aws_cognito_user_pool.this` (hotfix #85). The block was added because the AWS provider 6.x oscillates adding/removing standard-attribute schema blocks between applies.

Story 7.0b's third AC asks for a new `custom:profile_complete` attribute "declared on the user pool via Terraform". A naive add of `schema { name = "profile_complete" attribute_data_type = "String" mutable = true ... }` to `aws_cognito_user_pool.this` will be silently ignored — `ignore_changes = [schema]` covers ALL schema blocks, not just standard ones. The attribute would never get added to the pool, the profile Lambda's `admin_update_user_attributes` call would fail with `InvalidParameterException: attribute not found`, and pre-token-gen would always default to "false".

Options (need to pick before dispatch):
- **(a)** Temporarily lift `ignore_changes = [schema]` for one apply, land the new attribute, restore the lifecycle. Documented sequence in PR body.
- **(b)** Add the attribute out-of-band via `aws cognito-idp add-custom-attributes` invoked from the migrator Lambda or a one-shot null_resource provisioner. Hacky but bypasses the lifecycle. State drift afterwards because Terraform doesn't track it.
- **(c)** Verify in dev whether the oscillation observed in #85 affects CUSTOM attributes the same way standard ones were affected. If custom attributes are stable, narrow the lifecycle to `ignore_changes = [schema[0..3]]` or restructure to ignore only standard blocks. Cleanest.

Recommend (c) — investigate first, fall back to (a) if confirmed.

### Significant

**B2. `_REQUIRED_FOR_COMPLETION = frozenset(...)` cannot express the conditional `marriage_time` predicate.**

The current `profile/handler.py:82` constant is a flat `frozenset` of column names. Story 7.0b second AC says "widened to match the new CHECK fields exactly." But the new CHECK has 34 flat `IS NOT NULL` clauses AND one conditional `(marital_status = 'single' OR marriage_time IS NOT NULL)` clause. A frozenset can't carry that conditional.

The handler must change structurally — either to a list of `(field_name, predicate_fn)` tuples, a dict `{field_name: predicate_fn}`, or a single `is_profile_complete(row: dict) -> bool` function. Otherwise the in-Lambda flip-eligibility check (line 391-392) will:
- Reject single users because marriage_time is in the frozenset and is NULL → the Lambda decides not to flip the flag → the user can never complete onboarding via this code path even though the DB CHECK would accept them.

PRD doesn't call this out. Subagent might add `"marriage_time"` to the frozenset and ship the bug. Fix is small but needs to be explicit in the AC.

**B3. Profile Lambda needs `USER_POOL_ID` env var and the user pool ARN as IAM policy resource.**

The profile Lambda module currently has env vars for Aurora connectivity only (AURORA_HOST, PORT, DBNAME, DB_SECRET_NAME, EDGE_SECRET). It does NOT have `USER_POOL_ID`. The 7.0b AC says "calls `cognito-idp admin_update_user_attributes` to set ... on the Cognito user" — that boto3 call needs `UserPoolId` as a parameter. The Lambda needs `USER_POOL_ID` in its environment, sourced from the cognito module output.

Same for IAM: 7.0b notes say "aurora_writer IAM policy gets `cognito-idp:AdminUpdateUserAttributes`" — but on which Resource? The user pool ARN. The profile module needs an input variable for the user pool ARN (or a data source). Currently it has none — the cognito module just hands its ARN out as an output and no downstream module references it.

PRD note covers the IAM permission verb but not the wiring. Add a sentence to 7.0b notes: "Profile module gains two new inputs: `cognito_user_pool_id` and `cognito_user_pool_arn`, sourced from `module.cognito.user_pool_id` / `.user_pool_arn`. Env var `USER_POOL_ID` exposed to the Lambda; aurora_writer IAM policy scopes `cognito-idp:AdminUpdateUserAttributes` to the `cognito_user_pool_arn` input."

### Minor (no AC change needed)

- **M1.** Migration 0013's `refresh_log` table grows unbounded (one row per refresh × every 15 minutes × forever). Phase 10/11 should add a TTL or scheduled cleanup. Not a v1 blocker — 96 rows/day × 365 days = ~35k rows/year. Negligible. Just noting.

- **M2.** `engineeringprinciples.md` may not have been re-read this session. Subagents read it at dispatch per the protocol; no main-agent action needed.

### Verdict

- **B1 is blocking** — needs PRD direction before dispatch (which of options a/b/c).
- **B2 and B3 are tighten-the-AC fixes** — small edits to 7.0b.
- No `depends_on` changes.
- No drift from the codebase since the 2026-06-15 edits.

Address these three (especially B1) and re-run, or proceed and let the 7.0b implementer surface B1 mid-flight.

### Resolution — 2026-06-16 (post-edit re-check)

Owner direction (chat):
- **B1**: option (c) chosen — investigate custom-attribute stability first; fall back to (a) two-step apply if oscillation observed. Lessons-learned capture either way.
- **B2**: `marriage_time` moved OUT of REQUIRED. Migration 0012 sets `DEFAULT 'Not Provided'` on the column (TEXT) and backfills existing NULLs to the same sentinel. `_REQUIRED_FOR_COMPLETION` stays a flat `frozenset` (no conditional predicate, no structural change). Field count drops from 35 to 34.
- **B3**: profile module gains `cognito_user_pool_id` + `cognito_user_pool_arn` inputs; `USER_POOL_ID` env var on Lambda; `cognito-idp:AdminUpdateUserAttributes` scoped to the user pool ARN (not `*`).

PRD edited in place. Context_summary updated. Appendix CHECK clause regenerated (34 fields, no conditional). PRD is implementation-ready for the second time.

## 2026-06-16 09:17 brainstorm (pre-dispatch sanity check)

Final pass before tracking-issue creation and dispatch. Cross-checked PRD against the live repo (HEAD = `f154c03 phase 6: closeout #88`). Only two notes; nothing blocking.

### Drift since 2026-06-15

None. Last commit is the phase-6 closeout #88. No code or schema changed under the 7.x footprint since the PRD was finalized.

### Significant — worth a one-line PRD tweak

**S1. `u.user_id` is already in the `_ALLOWED_COLUMNS` whitelist (hotfix #87).**

Story 7.1's AC bullet on block filtering reads: *"the shared helper's `_ALLOWED_COLUMNS` whitelist is extended to include both `\"u.user_id\"` (this story) and `\"dv.user_id\"` (story 7.2) in a single edit"*.

Reality: `infrastructure/src/layers/observability/knotify_obs/_blocks.py:42-51` already lists `"u.user_id"` in the frozenset (added when hotfix #87 ran, alongside `bk.bookmarked_user_id` and the friendship/friend-requests entries). The unit test `test_blocks.py:80` exercises it.

The only entry stories 7.1/7.2 need to add is `"dv.user_id"`. A subagent reading the AC literally will either add a duplicate (harmless — frozenset dedup) or pause to investigate. Belt-and-braces: the subagent brief should call this out explicitly.

### Minor

**M1. Migration directory path is implicit.**

PRD references migrations `0011_deck_view_extended.sql`, `0012_profile_complete_widen_check.sql`, `0013_aurora_refresh_role.sql` by filename only. They live at `infrastructure/db/migrations/` (where 0000–0010 already sit). Subagent will discover this by listing prior files. Not worth a PRD edit.

**M2. Three stories touch `profile/handler.py`.**

7.0b (Cognito attribute write + decorator), 7.3 (preference_vector encoding), and 7.4 (async refresh invoke) all modify the same file plus its unit and integration tests. Strict-serial dispatch on a single phase branch (`feat/phase-7-match`, per `gitbranching.md`) means no concurrent edits — each story builds on the previous commit. Note for awareness, not action.

### `depends_on` recheck

Unchanged from the 2026-06-16 08:20 review. Graph is consistent: 7.0/7.0a/7.0b/7.3 leaves; 7.1 [7.0]; 7.2 [7.0, 7.0a]; 7.4 [7.0a]; 7.5 [7.0, 7.0b, 7.1, 7.2]; 7.6 all eight. Topologically sound.

### Verdict

PRD is implementation-ready. The S1 wording bug is a single-sentence subagent brief addendum; doesn't require a PRD edit. Recommend proceeding to Step 1 (tracking-issue sweep).
