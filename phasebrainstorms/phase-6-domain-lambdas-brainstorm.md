# Phase 6 brainstorm — Profile, friends, bookmarks, blocks domain Lambdas

## 2026-06-09 09:02 brainstorm

Review of `implementationplan/phase-6-domain-lambdas.md` against architecture.md, the as-built phase-2/3/4/5 code, and the migration set on disk. Eight stories (6.0–6.7), all `backenddeveloper`, all dependencies in-phase. Findings are grouped by severity. Each item names the story, the gap, the resolution, and whether it requires a PRD edit.

---

### Blockers (must resolve in PRD before dispatch)

**B1 — Story 6.1 conflates "first set" with "modify after set" for PATCH /v1/profile/me, and silently owns profile-completion as a side concern.**
The architecture references a "profile-completion endpoint (phase 6)" in at least three places (migration 0002 lines 30–36 and 114–127, migration 0007 lines 145, architecture §13). Migration 0002's `profile_complete_requires_required_fields` CHECK constraint and the `users_update_own_row` RLS policy from migration 0007 only make sense if PATCH /v1/profile/me can:
  1. Set first_name/last_name/sex/birthday/username/religion/subsect for the FIRST time (NULL → value) — allowed.
  2. Flip `profile_complete_verified` to true in the SAME transaction as setting the last required field.
  3. Reject any subsequent PATCH that tries to modify those fields (the trigger in 0008 enforces this DB-side, but the API needs a 400 with a clean error, not a 500 from an exception).
The current AC (story 6.1) says "PATCH validates ... rejects any of the immutable fields from §5.7" without distinguishing first-write from update-after-write. Following the AC literally would break signup completion: a freshly-bootstrapped user's PATCH including `sex` would 400 because §5.7 lists `sex` as immutable — but the column is NULL at bootstrap and MUST be settable exactly once.
**Resolution:** rewrite story 6.1's PATCH AC. Make explicit:
  - The PATCH endpoint accepts ALL profile fields. For each immutable-after-set field, it is rejected only when the column is non-NULL AND the request body changes it.
  - When all five required fields (first_name, last_name, sex, birthday, username) become non-NULL in the same transaction, the Lambda sets `profile_complete_verified=true`. The CHECK constraint already guards against premature flips; the Lambda's responsibility is to flip when the row qualifies.
  - The trigger in migration 0008 is defense-in-depth; the API layer returns 400 with the field list before the DB rejects.
  - One PATCH idempotency contract: re-PATCHing with the same value for an already-set immutable field is a no-op (200), not 400.
**PRD edit required:** yes, story 6.1.

**B2 — `username` has no UNIQUE constraint at the DB level; story 6.1 silently inherits the gap.**
Migration 0002 declares `username TEXT` (not `TEXT UNIQUE`). Architecture §5.7 says username is mutable, rate-limited to 1/30 days, and is the search key for GET /v1/profiles?username=… (story 6.1). Without a UNIQUE constraint:
  - GET /v1/profiles?username=alice can return more than one row.
  - Two users can complete profile with the same username during a race.
Story 6.1's AC doesn't mention uniqueness, doesn't mention the 30-day rate limit, and doesn't say where the search index lives (a CITEXT cast? case-insensitive? prefix index for autocomplete?). 
**Resolution:** add a story 6.0a (or fold into 6.1) that adds migration 0010 introducing `CONSTRAINT users_username_key UNIQUE (username)` and (optional) a partial unique index `WHERE username IS NOT NULL` to allow multiple NULL bootstrap rows. Decide:
  - Case sensitivity. (Recommend: store lowercased + UNIQUE on lower(username), or use CITEXT.)
  - Whether the 1/30-day rate limit is enforced at the API (track `username_last_changed_at` column) or deferred to a later phase. If deferred, say so explicitly in the PRD notes so it doesn't silently leak into phase 7.
**PRD edit required:** yes — either a new migration story or a documented deferral note.

**B3 — Story 6.4 "blocking is only allowed against current friends" is in tension with story 6.2's friend-request flow when a block targets a stranger.**
Owner decision (2026-05-24) says POST /v1/blocks returns 409 NOT_FRIENDS if no friendship row exists. But story 6.2's POST /v1/friend-requests has NO acceptance criterion that says "rejects when blocker_id is blocked by toUserId (in either direction)" — story 6.4's integration test asserts this end-to-end ("A POST /v1/friend-requests {toUserId: B} returns 409 with BLOCKED reason") but the corresponding behavior is owned by story 6.2, not story 6.4. If you build 6.2 first to its AC literally, you can send friend requests to people who have blocked you — and the integration test in 6.4 will fail.
**Resolution:** add an AC to story 6.2: POST /v1/friend-requests must reject with HTTP 409 reason BLOCKED if either side has a row in `blocks` for the pair. Also add: GET /v1/friends and GET /v1/friend-requests filter out any pair where a block exists in either direction (architecture §8 multi-layer block enforcement). DELETE /v1/friend-requests/{id} accept/decline endpoints should treat a freshly-arrived block as auto-decline (POST /v1/blocks already deletes pending friend_requests per story 6.4 AC, so this is symmetric — just make sure both stories agree on the canonical pair).
**PRD edit required:** yes, story 6.2.

---

### Major (high risk if not addressed, but not strict blockers)

**M1 — Story 6.0 vs. Story 6.5 AC overlap on hello stub removal.**
Story 6.0 AC removes everything hello-stub-related. Story 6.5 AC criterion 2 also says "The stub /v1/_internal/hello route from phase 5 story 5.7 and its Lambda source are deleted from the repo". This is a duplicated invariant; if story 6.0 runs first as designed, the assertion in 6.5 either becomes vacuously true (no diff) or becomes a "still gone — confirmed" check. Either way it's confusing. The 6.5 AC also accidentally implies the hello cleanup is part of 6.5's work, which contradicts story 6.0's `depends_on: []` (run-first ordering).
**Resolution:** delete AC criterion 2 from story 6.5. Story 6.0 owns the cleanup; 6.5 just adds routes.
**PRD edit required:** yes, story 6.5.

**M2 — `signed_in_user` fixture yields a bootstrap user with NULL profile fields; phase-6 integration tests need a "completed-profile user" fixture.**
The fixture in `infrastructure/src/tests/integration/conftest.py` does sign-up + confirm + sign-in only. The Aurora row has email + user_id; everything else (first_name, sex, birthday, username, religion) is NULL. Stories 6.1's AC "signup two opposite-sex users, GET /v1/profile/me returns full profile" cannot pass without a second fixture that completes the profile. Story 6.6 RLS tests need sex set on both users to assert opposite-sex enforcement. Story 6.7's E2E needs the full thing.
**Resolution:** add a `completed_profile_user(sex)` fixture (parametrized) in conftest.py as PART of story 6.1's work (since 6.1 is the first story to need it). All later stories reuse it. The fixture should:
  - Reuse `signed_in_user` internally.
  - PATCH /v1/profile/me with a minimal complete profile (first_name, last_name, sex, birthday, username, religion).
  - Assert profile_complete_verified flipped to true.
  - Yield the same shape as `signed_in_user` plus the completed fields.
**PRD edit required:** yes — add a note to story 6.1 covering the fixture, and remove the "signup two opposite-sex users" boilerplate from each subsequent story's AC by saying "uses the `completed_profile_user` fixture from conftest.py".

**M3 — Story 6.6 AC criterion 3 ("manual SQL query through the Lambda with the GUCs unset returns no rows") has no clean execution path.**
The `knotify_db.rls_context()` context manager sets the GUCs on entry and resets them on exit. There is no public Lambda code path that does a SELECT without first entering the context manager — by design. To test "GUCs unset returns no rows", you'd have to either:
  - Add a test-only Lambda or a debug switch in production code (rejected — security risk).
  - Bypass the context manager from a Lambda integration test (impossible from the outside).
  - Test it as a UNIT test against a local Postgres with the migration applied — call `cur.execute("SELECT * FROM users")` without setting GUCs and assert empty result.
The third option is the right one but it isn't an integration test; it belongs under `infrastructure/src/tests/integration/test_db_rls.py` as a docker-compose unit test, NOT under tests/integration/.
**Resolution:** split story 6.6 AC criterion 3 into a separate docker-compose unit test (no Lambda involved, just psycopg2 + the migration). The other two ACs in 6.6 stay as live-AWS integration tests.
**PRD edit required:** yes, story 6.6.

**M4 — Story 6.4's DynamoDB UpdateItem path is missing IAM, environment wiring, and table-name plumbing.**
The Lambda needs `dynamodb:UpdateItem` on the `ChatRooms` table ARN, plus the table name as an env var. The existing `dynamodb_chat_writer` IAM role (from phase 3 story 3.4) exists but its actual policy needs to include UpdateItem on the ChatRooms table — verify the role's policy document allows it. The `module.dynamodb` outputs `chat_rooms_table_name` already (confirmed). Story 6.4's AC doesn't say "Lambda assumes the dynamodb_chat_writer role" or "TABLE_CHAT_ROOMS env var wired from module.dynamodb.chat_rooms_table_name".
**Resolution:** add to story 6.4 AC: knotify-blocks Lambda runs under an IAM role that grants `dynamodb:UpdateItem` on `arn:aws:dynamodb:<region>:<account>:table/ChatRooms` AND the Aurora `app_user` connection rights from phase 3. Decide whether to reuse `dynamodb_chat_writer` (needs Aurora layer too — probably needs a new combined role) or add a new role `blocks_writer` that grants both. Add TABLE_CHAT_ROOMS env var to the Lambda config.
**PRD edit required:** yes, story 6.4. Also flag for IAM module: may need a new role.

**M5 — Story 6.4 owns the canonical_pair + sha256 helper, but doesn't say where it lives. Phase 8 (chat) needs the identical helper.**
If story 6.4 inlines the computation in functions/blocks/handler.py, phase 8 will either duplicate it or have to refactor in flight. The architecture defines this primitive in §5.4 — it's a shared chat-domain primitive, not a blocks-specific one.
**Resolution:** put the helper in the shared observability layer (next to other cross-Lambda helpers) OR in a new shared `knotify_chat` layer. Recommendation: put it in `knotify_obs._chat_room_id` (small, single function, no extra deps), exported from the observability layer's `__init__`. Document the contract: `room_id(user_a: str, user_b: str) -> str` returns the hex-encoded SHA-256 of "min:max".
**PRD edit required:** yes, story 6.4 — add an AC pinning the helper location and a one-line unit test.

**M6 — Stories 6.1–6.4 have NO Terraform wiring AC; story 6.5 owns it. This forces dispatching the four Python stories serially before any AWS apply can be planned, which means none of the per-story integration tests run until 6.5.**
Story 6.1's AC says "Integration test against the dev Aurora cluster: signup two opposite-sex users, GET /v1/profile/me returns full profile…" — but if the route isn't wired (route wiring is in 6.5), the integration test cannot pass during 6.1's dispatch. Either:
  - Each of 6.1–6.4 wires its own routes (and 6.5 becomes a verification-only story that asserts all routes exist), OR
  - Stories 6.1–6.4 are CODE-ONLY (unit tests inside docker-compose; no live integration tests), and 6.5 owns route wiring AND the live integration tests.
The current PRD reads as the first interpretation but actually executes as a hybrid: 6.1 has live integration tests that depend on routes that don't yet exist.
**Resolution:** pick one model and rewrite. Recommendation: each Lambda story (6.1–6.4) ALSO wires its Terraform (lambda module instantiation + apigatewayv2_integration + routes + permission + IAM). Story 6.5 reduces to "smoke-test all wired routes return 401 without JWT and 403 without edge-secret" — a regression sweep across all routes. This way each story's integration test runs against a live deployed Lambda after that story's apply. Drop story 6.5's `depends_on: [6.1, 6.2, 6.3, 6.4]` to mean "runs after them" rather than "owns their wiring".
**PRD edit required:** yes, all of 6.1, 6.2, 6.3, 6.4, 6.5. This is the largest structural change suggested.

---

### Medium (worth fixing, low risk if deferred)

**Md1 — Story 6.1 GET /v1/profiles?username=… does not specify exact-match vs. prefix vs. fuzzy.**
The old API had `GET /userdata?username=...` with unclear semantics. Architecture §4.2 calls it "username search". Decide now: exact match (CITEXT comparison) is the simplest and matches the per-user-uniqueness model in B2. Defer prefix/fuzzy search to a later phase.
**PRD edit required:** small — add a sentence to story 6.1 AC.

**Md2 — Story 6.2's "canonical (user_a < user_b)" ordering for friendships is asserted in the architecture (§5.1) but no AC says HOW the Lambda computes the canonical pair.**
Same primitive as M5 (string-min/max). If the helper from M5 is shared, this AC just consumes it. Otherwise the friends Lambda re-implements it. Resolve M5 first; then this AC trivially references the shared helper.
**PRD edit required:** yes if M5 lands — replace "canonical (user_a < user_b)" with "uses shared canonical_pair helper from M5".

**Md3 — Story 6.4 "deactivate the chat room (if one exists)" via UpdateItem with `attribute_exists(room_id)` condition — what happens to the failed-condition exception?**
The story says it should be a no-op when no room was ever created. Boto3 raises ConditionalCheckFailedException; the Lambda must catch and treat as success, not surface as a 5xx. The AC says "no-op" but doesn't say "swallow ConditionalCheckFailedException".
**PRD edit required:** small — add the exception name to the AC.

**Md4 — CloudWatch log retention.**
Architecture §13 mandates 7-day retention on all CloudWatch log groups. The `modules/lambda` module from phase 3 likely already creates the log group with retention_in_days=7 (verified by the phase-3 story 3.1 commit notes: "depends_on log group (brainstorm M8/M9)"). No PRD change needed IF the existing module owns it; flag for the subagent to confirm during 6.1's dispatch.
**PRD edit required:** no — but the subagent should explicitly verify on 6.1 dispatch.

**Md5 — Throttling on the new routes.**
Phase 5 added throttling at the stage level (burst=10, rate=25 in dev). Per-route throttling overrides exist for hot endpoints. Story 6.1's GET /v1/profile/me will be hit on every app open — may need per-route throttling differing from the global setting. Defer to phase 10 (observability) or phase 11 (hardening) unless dev numbers prove problematic.
**PRD edit required:** no — document as a phase-11 carryover candidate.

---

### Minor / nitpicks

**N1 — Story 6.0's AC "terraform plan ... shows only the removals (negative diff); no positive diff" is technically false when removing the hello Lambda also forces a `random_password.edge_secret` no-op refresh in CloudFront state.** The plan will show ZERO net resource change in CloudFront but might log a `random_password` re-read. Phrase the AC as "no positive resource diff aside from null-diff data-source refreshes".

**N2 — Story 6.7's "make test-e2e" target does not yet exist.** The current Makefile has `test` and `package-test`. Story 6.7 silently expects the subagent to add a Makefile target. Make the addition an explicit AC.

**N3 — Story 6.7 depends on [6.5, 6.6] but transitively that means all of 6.1–6.6.** Fine as written but consider listing it as `depends_on: [6.1, 6.2, 6.3, 6.4, 6.5, 6.6]` for clarity — the dependency graph reader doesn't follow transitive closures.

**N4 — `signed_in_user` fixture in conftest.py yields `_aurora_conn`** opened with MASTER credentials (autocommit=True). Phase-6 RLS tests will need a connection opened as `app_user` to validate that RLS actually fires (master bypasses RLS unless FORCE is in play — and 0007 DOES force it on `users`, so master is filtered too). Worth double-checking: open a second connection as `app_user` from a follow-up fixture, drive a SELECT, assert filtered result set. The current `_aurora_conn` (master) is correct for teardown (master can delete from any row) but wrong for the visibility tests. Note in 6.6's AC.

---

### Cross-phase drift

**X1 — Phase 7 (match) and phase 8 (chat) depend on every primitive surfaced here.** Specifically:
  - Phase 7: deck_view materialized view + block filter (story 6.4 wires the block tables; phase 7 reads them).
  - Phase 8: ChatRooms UpdateItem path (story 6.4 wires the deactivation path; phase 8 owns activation/reactivation).
  - Phase 8: canonical_pair + sha256 helper (M5).
If the shared helper from M5 lands in the observability layer, both phase-7 and phase-8 PRDs will need a one-line note pointing at it. Cheap to do now.

---

## Summary

- **3 blockers** (B1 PATCH semantics, B2 username uniqueness, B3 block-aware friend-requests) — require PRD edits.
- **6 majors** (M1 hello dup, M2 fixture gap, M3 RLS GUCs-unset test, M4 IAM/env wiring for blocks, M5 shared helper location, M6 Terraform wiring ownership) — strongly recommended to resolve.
- **5 mediums** + **4 minors** + **1 cross-phase drift** — small edits; can be batched.

The biggest structural decision is **M6**: do Lambda stories own their own Terraform wiring (recommended), or does 6.5 batch-wire at the end? Picking the first interpretation cascades through every per-Lambda story's AC.

Recommended next step: ADDRESS (edit PRD), then re-run /implement-phase 6.

---

## 2026-06-09 11:45 brainstorm (re-run after PRD revision)

The owner picked Option A for every blocker and major (B1, B2-A+E, B3-B, M1, M2, M3, M4, M5, M6) and accepted the defaults for mediums/minors. The PRD was rewritten — two new stories landed (6.0a `username` UNIQUE migration; 6.0b shared `chat_room_id` + block-aware filter helpers in the observability layer), every Lambda story now owns its own Terraform wiring, story 6.5 shrank to a verification sweep, and story 6.6 split its third criterion off as a docker-compose unit test.

This re-brainstorm verifies the originals are resolved and surfaces NEW issues introduced by the edits.

### Verification — original findings closed

| Finding | Resolution in revised PRD | Status |
|---|---|---|
| B1 PATCH semantics | Story 6.1 specifies first-set-allowed, change-after-set-rejected (400 with field list), same-value re-PATCH = 200, profile_complete_verified flipped in the same transaction when last required field lands | ✓ |
| B2 username uniqueness | New story 6.0a adds migration 0010 with partial case-insensitive UNIQUE index; 30-day rename rate limit explicitly deferred to phase 11 with a carryover note | ✓ |
| B3 block-aware friends | Story 6.0b ships shared block filter; stories 6.2/6.3/6.4 consume it; POST /v1/friend-requests returns 409 BLOCKED, GET endpoints filter block pairs | ✓ |
| M1 hello dup | Removed from story 6.5 (now a regression sweep only) | ✓ |
| M2 completed_profile_user | Built in story 6.1 conftest.py, parametrized by sex, asserts profile_complete_verified flip and token claim | ✓ |
| M3 RLS GUCs-unset test | Split off to docker-compose unit test `tests/db/test_rls_fail_closed.py` in story 6.6 | ✓ |
| M4 blocks IAM + env | New `blocks_writer` IAM role in story 6.4 (Aurora + scoped DynamoDB UpdateItem); TABLE_CHAT_ROOMS env wired | ✓ |
| M5 canonical helper | `knotify_obs.chat_room_id` lands in story 6.0b; consumed by 6.4 here and phase 8 later | ✓ |
| M6 wiring ownership | Each of 6.1–6.4 owns its own module + integration + route + permission; story 6.5 verifies | ✓ |
| Md1 username search | Story 6.1 specifies case-insensitive exact match (`WHERE lower(username) = lower($1)`) | ✓ |
| Md3 conditional update | Story 6.4 catches `ConditionalCheckFailedException` explicitly, treats as INFO-level success | ✓ |
| Md5 throttling | Deferred to phase 11 with carryover note (story 6.7) | ✓ |
| N1, N2, N3, N4 | All applied | ✓ |
| X1 cross-phase | The shared helpers in 6.0b mean phases 7 and 8 will reuse, not reinvent | ✓ |

All 17 original items addressed.

### NEW findings introduced by the revision

#### NEW-1 — Story 6.2's "test 6.2's block behavior" requires 6.4 to be deployed, but 6.2 runs first

Serial execution order is 6.0 → 6.0a → 6.0b → 6.1 → 6.2 → 6.3 → 6.4 → 6.5 → ... When story 6.2's integration test wants to assert "POST /v1/friend-requests returns 409 BLOCKED", it needs a block to exist in the `blocks` table for the test pair. Two ways to create that block at 6.2's dispatch time:
  - (a) Direct SQL INSERT into `blocks` via the master Aurora connection (bypasses 6.4's API).
  - (b) Defer the block-aware test to story 6.7's E2E.

The PRD currently says (b) as a fallback. (a) is fine too and lets 6.2 close out fully testable at dispatch. **Recommendation:** add a sentence to story 6.2's AC permitting either approach — explicit choice avoids the subagent improvising. If we pick (a), the integration test owns inserting the test fixture row directly via the master conn, then exercising the API.

#### NEW-2 — `BLOCK_FILTER_SQL` as a raw string with `<ref>` placeholders is fragile

Story 6.0b exports `BLOCK_FILTER_SQL: str` and asks the caller to substitute the `<ref>` column reference and bind `%s` parameters twice. That mixes templating (column substitution via string format) with parameterization (value binding via psycopg2). The two are different categories — column substitution by string-format is an injection vector if a caller ever wires user input into the `<ref>` field.

**Resolution:** rewrite as a Python builder function: `def block_filter(other_user_col: str) -> tuple[str, int]` that returns `(sql_fragment, num_params)` where `other_user_col` is a TRUSTED identifier (validated against a whitelist of legal column names inside the function — `friendships.user_a`, `friendships.user_b`, `bookmarks.bookmarked_user_id`, etc.). Callers pass two copies of the requesting user's id into their `cur.execute(...)` params list. This keeps templating internal and parameters external — the safe split. Worth editing story 6.0b before dispatch.

#### NEW-3 — Story 6.6's docker-compose test description contradicts itself

The criterion reads:
> "As app_user, SET the GUCs to the Male user's id/sex, re-execute the same SELECT. Assert exactly one row visible (the Male's own row) **and no Female rows** (because the Male sees Females via the != branch in the policy → **but here only the OR branch fires** because we set sex=Male and the Female has sex=Female, so the != branch **ALSO returns the Female row**; revise the test setup so it asserts the expected union — the Male's own row + every opposite-sex row)."

The phrase "no Female rows" contradicts "the != branch ALSO returns the Female row" three sentences later. The actual expected result of the policy with GUCs set to a Male requester is: own row (via OR clause) UNION all Female rows (via the `sex != 'Male'` clause). The criterion needs cleanup to remove the contradictory clauses.

**Resolution:** rewrite criterion as: "With GUCs set to the Male user, the SELECT returns exactly two rows: the Male's own row (via the OR identity clause) and the Female row (via the `sex != Male` clause). The other Male row in the table is filtered." Edit story 6.6 before dispatch.

#### NEW-4 — Story 6.4 unconditional DynamoDB SET can clobber an earlier non-block deactivation

Architecture §5.4.1 describes multiple deactivation reasons: `'user_deleted_account'` and `'blocked'`. If a chat room was previously deactivated because user B deleted their account, and later (during the 30-day soft-delete window) someone tries to block them, the current AC's `SET status='deactivated', deactivated_reason='blocked', deactivated_by=...` would overwrite the original `user_deleted_account` reason. That's probably wrong — once a user is account-deleted, the "blocked" status is meaningless.

**Resolution:** add ConditionExpression `(attribute_not_exists(deactivated_reason) OR deactivated_reason = 'active')` so the SET only fires when the room is currently active. If it's already deactivated for another reason, treat as a no-op (catch ConditionalCheckFailedException — same handler as the no-room case). Edit story 6.4.

#### NEW-5 — Reactivation on unblock should be conditional on `deactivated_reason='blocked'`

Symmetric to NEW-4. DELETE /v1/blocks should only reactivate the room if the deactivation reason was 'blocked' AND deactivated_by matches the unblocker. Otherwise, deleting a block resurrects rooms deactivated for unrelated reasons (e.g., the other party deleted their account).

**Resolution:** the reactivation UpdateItem in story 6.4 needs ConditionExpression `(deactivated_reason = 'blocked' AND deactivated_by = :unblocker_id)`. The same exception-catching contract applies. Edit story 6.4.

#### NEW-6 — `completed_profile_user` fixture must re-login after PATCH to refresh the token claim

Story 6.1's fixture description says "asserts that a fresh token issued via PreTokenGeneration carries `custom:profile_complete = "true"` on both ID and access tokens." PreTokenGeneration only fires at login (or token refresh), not at PATCH. The fixture must explicitly:
  1. Sign up + confirm + initial login → token A (carries `custom:profile_complete = "false"`).
  2. PATCH /v1/profile/me with the completion payload → 200.
  3. Call `cognito_client.admin_initiate_auth` AGAIN to mint a NEW token pair → token B.
  4. Assert token B carries `custom:profile_complete = "true"`.

The PRD already says "a freshly minted token pair" but doesn't spell out the explicit re-login step. **Resolution:** add a one-sentence clarification to story 6.1's fixture AC: "the fixture explicitly re-issues a token via `admin_initiate_auth` after the PATCH; the original token from `signed_in_user` is NOT mutated and is discarded." Edit story 6.1.

#### NEW-7 — Layer version pinning claim in story 6.0b is inaccurate

The AC says: "existing Lambdas using the layer pin to a specific version, so old Lambdas continue on the old version; new phase-6 Lambdas pick up the new version." But the `modules/lambda` consumers reference `module.observability_layer.layer_arn`, which Terraform resolves to the most-recent published `aws_lambda_layer_version` ARN at plan time. On the next apply, ALL Lambdas that reference the output get the new layer ARN — including phase-3/4/5 ones.

That's not actually a problem (the new helpers are purely additive — no existing API changes), but the AC's claim is misleading. **Resolution:** rephrase: "All Lambdas that depend on `module.observability_layer.layer_arn` will pick up the new layer version on next apply. The new exports (`chat_room_id`, `block_filter`, `is_blocked`) are additive — no existing imports change behavior. The apply diff will show every existing Lambda's `layers` attribute changing to the new layer ARN; this is expected." Edit story 6.0b.

#### NEW-8 — `infrastructure/src/tests/db/` directory and pytest config need a one-line bootstrap

Stories 6.0a and 6.6 both reference `infrastructure/src/tests/db/...` test files. That directory doesn't exist yet. `pytest.ini` from phase 3 has `testpaths=.` so any subdirectory under `infrastructure/src/` is discovered automatically — no config change needed. But the directory needs to be created, and an empty `__init__.py` may or may not be required depending on the import mode (phase 3 set `--import-mode=importlib` so it likely isn't).

**Resolution:** add an AC to story 6.0a: "create `infrastructure/src/tests/db/` with an empty `__init__.py` (consistent with existing test directories)." Subsequent stories (6.6) reuse it. Tiny edit.

#### NEW-9 — Story 6.7's `make test-e2e` target depends on `.env.test` being fresh after apply

The `.env.test` file is written by Terraform's `local_file.integration_test_env` (story 5.6). When dev is destroyed (current state per context.md), the file goes stale (or vanishes — `terraform destroy` removes the file). The next phase-6 apply will regenerate it. The `make test-e2e` target needs to either:
  - Run after a successful `terraform apply` and trust the file is fresh.
  - Add a `prereq` Makefile dependency that runs `terraform output -raw distribution_domain_name > ... ` etc., bypassing the static file entirely.

Currently the AC says "with the required env vars sourced from `.env.test`". **Resolution:** add a sentence: "`make test-e2e` exits with a clear error message if `.env.test` is missing or older than the most recent `apply.log`, prompting the developer to run `terraform apply -auto-approve` first." Minor — could also defer.

#### NEW-10 — Story 6.4 negative integration test asserts "ChatRooms row absent"

The test reads: "A POST /v1/blocks {userId: B} returns 409 NOT_FRIENDS and writes nothing to Aurora or DynamoDB (assert blocks table row count unchanged, ChatRooms row absent)."

But ChatRooms rows are created in phase 8 (chat) when users first message each other, not in phase 6. A ChatRooms row "absent" at the start of the test could become "present" mid-test only if phase 8 code runs — which it doesn't. So the assertion is correct but the rationale is a bit thin. More importantly, the AC reads as if blocks SHOULD have created a ChatRooms row in the positive path — but it doesn't; it only UPDATES an existing one (no-op otherwise).

**Resolution:** clarify in story 6.4's notes that knotify-blocks never CREATES ChatRooms rows; it only mutates existing ones via conditional UpdateItem. Phase 8 creates rooms on first message. This avoids confusion when a future reader sees the "writes nothing to ... DynamoDB" assertion and wonders why a block wouldn't create a row. Tiny edit.

### Severity summary of NEW findings

- **0 blockers** (no new blockers introduced by the revision)
- **2 majors** that need PRD edits before dispatch:
  - NEW-2 (block-filter API ergonomics — string templating is fragile)
  - NEW-3 (story 6.6 docker-compose test description is self-contradictory)
- **3 mediums** that should be addressed but won't break dispatch:
  - NEW-4 (story 6.4 ConditionExpression on initial SET)
  - NEW-5 (story 6.4 ConditionExpression on reactivation)
  - NEW-6 (clarify re-login in the completed_profile_user fixture)
- **3 minors** (NEW-1 explicit choice, NEW-7 rewording, NEW-8 directory bootstrap, NEW-9 .env.test freshness, NEW-10 notes clarification)

NEW-1, NEW-7, NEW-8, NEW-9, NEW-10 can be addressed in-flight by the subagent during dispatch with a one-sentence note in the PRD. NEW-2 and NEW-3 should be fixed in the PRD now because they affect what the subagent will actually build (API surface) and what the test will actually assert.

### Recommendation

Two NEW majors (NEW-2, NEW-3) warrant a PRD edit; the rest can be batched into a single sweep or addressed by the subagent at dispatch. Three mediums (NEW-4, NEW-5, NEW-6) raise the correctness bar around the chat-room deactivation lifecycle and the token-refresh contract — worth fixing for downstream phase 8 to inherit a clean baseline.

**Suggested next step:** edit the PRD to fix NEW-2, NEW-3, NEW-4, NEW-5, NEW-6 (five total small edits), then proceed without further re-brainstorm — the remaining minors are docs-grade only.

Or, if you want to move faster: PROCEED now and let the subagent handle NEW-2/NEW-3/NEW-4/NEW-5/NEW-6 in-flight by including them as explicit dispatch-brief notes. Slightly riskier (no audit trail in the PRD), but workable.

---

## 2026-06-09 12:00 brainstorm

Third pass — re-run at `/implement-phase 6` after the prior two passes (B1–B3 / M1–M6 then NEW-2 through NEW-7) have already been folded into the PRD. Focus is on NEW gaps only: missing/non-testable AC, scope drift, wrong `depends_on`, drift from already-merged phase-5, unvalidated external assumptions. Dev infra is currently DESTROYED (context.md last entry) — phase 6's first apply will be a full bring-up, not an incremental diff.

### Blockers

**B1 (new) — Story 6.0's plan-diff AC is impossible against the current destroyed-dev state.**
AC reads: "`terraform plan` against dev shows only the removals; no positive resource diff aside from null-diff data-source refreshes." With dev destroyed via run 27188014855, the first plan will show ~91+ resources to CREATE. There are no "removals" because nothing is deployed. Following the AC literally blocks story 6.0 indefinitely.
**Resolution:** drop the plan-shape assertion. Keep the file-deletion AC and `terraform validate` clean. Or assert `terraform plan -no-color | grep -E 'hello|_internal/hello' | grep -E '^\s*\+ ' | wc -l == 0` — i.e. zero `+ create` lines for hello resources, irrespective of the rest of the plan.

### Majors

**M1 — Story 6.2's "accept/decline a friend_request whose blocker has already blocked" branch is dead code.**
Story 6.4's POST /v1/blocks DELETEs any pending `friend_requests` rows between the two users in the same transaction. After that DELETE commits, 6.2's POST .../accept does a SELECT on `friend_requests.id` and finds nothing → 404. The "succeeds at the data layer (status flip), inserts no friendship" branch and its `{"status":"declined_auto","reason":"blocked"}` HTTP 200 shape will not be reachable through any normal flow.
**Resolution:** delete that AC bullet from 6.2 and spec the realistic behavior: POST .../accept on a friend_request that no longer exists returns HTTP 404. The block-aware integration test asserts 404. This removes the only place in the PRD that introduces a `declined_auto` enum the rest of the system doesn't carry.

**M2 — Stories 6.1–6.4 are all `depends_on: [6.0a, 6.0b]` (same tier), but 6.2's and 6.3's block-aware integration tests require 6.4 to be deployed first.**
6.2's last AC says "exercising story 6.4 which must already be deployed at the time this test runs — adjust the integration-test ordering to run AFTER 6.4's wiring lands". 6.3 has the same shape ("a third Female who has blocked A"). The dependency is informal — `depends_on` does not encode it. Within-tier dispatch order is undefined, so the main agent could pick 6.2 before 6.4 and have to invoke the PRD's "defer to 6.7" fallback, leaving 6.2's exit gate weaker than intended.
**Resolution:** add `6.4` to `6.2.depends_on` and `6.3.depends_on`. This makes the dispatch loop deterministic: 6.0 → 6.0a → 6.0b → 6.1 → 6.4 → 6.2 → 6.3 → 6.5 → 6.6 → 6.7.

**M3 — Story 6.4 returns HTTP 500 on a post-commit DynamoDB error even though the Aurora block has already committed. Client sees "block failed"; user is in fact blocked.**
AC: "Any other DynamoDB error surfaces as HTTP 500 and the Aurora transaction is NOT rolled back (the block is committed; chat deactivation is best-effort)." This is a real divergence between client-observed and server-actual state. Retrying the block is idempotent at Aurora but will retry the same DynamoDB call, returning 500 again until DynamoDB recovers.
**Resolution:** catch the non-`ConditionalCheckFailedException` DynamoDB error, log ERROR, return HTTP 200 with `{"chat_deactivation_pending": true}` in the response body. Phase 8 will re-check chat-room status on every write so the pending state is self-healing. The pin in 6.4's AC is small: replace the "HTTP 500 ... best-effort" sentence with the 200+flag contract.

### Mediums

**Md1 — Story 6.4's DELETE /v1/blocks reactivation reactivates a chat room with no backing friendship.**
On block, 6.4 deletes the friendship. On unblock, 6.4 reactivates the chat room (status=active) but does NOT recreate the friendship. Phase 8 will then see an active chat room for two non-friends.
**Resolution:** the ConditionExpression already restricts reactivation to the same blocker who placed the block, so the no-op cases are covered. The remaining issue is purely phase-8's: it should treat "active chat room without backing friendship" as read-only. Capture this in a `## Carryovers from phase 6` block appended to `implementationplan/phase-8-chat.md` at phase-6 completion handoff (no PRD edit needed in phase 6 itself).

**Md2 — Story 6.0b's `is_blocked(conn, user_a, user_b)` has no AC forbidding SQL string-formatting of the user UUIDs.**
`block_filter`'s column-name whitelist is solid, but `is_blocked` only specifies "a single SELECT against blocks for the pair in either direction". The user UUIDs flow from the JWT sub and the URL path/body — attacker-controllable surfaces.
**Resolution:** add an AC: "`is_blocked` issues the SELECT via `cur.execute(sql, (user_a, user_b, user_b, user_a))` with parameter binding; no f-string interpolation of user_a/user_b. UUID-format validation is the caller's responsibility (Lambda handlers parse the URL path through a UUID regex before calling)."

### Minors

**Mi1 — Two stories (6.0a and 6.7) each ask for a `## Carryovers from phase 6` note in `implementationplan/phase-11-hardening.md`. The pattern should land once.**
**Resolution:** drop the per-story carryover AC bullets from 6.0a and 6.7. Move the carryover write to the phase-completion handoff: append a single block listing the username rename limit (from 6.0a), per-route throttling (from 6.7), and — if Md1 above is accepted — the chat-room-without-friendship rule into phase-8's PRD too.

**Mi2 — Story 6.6's `app_user_conn` fixture depends on the secret `knotify-${env}-app-user-credential`, which db_migrator creates on first migration apply. After the dev destroy, that secret does not exist.**
**Resolution:** no PRD change needed (the same apply that ships 6.6's wiring also runs db_migrator). But add: "the fixture `pytest.skip`s if the secret is absent — no-op on a freshly-destroyed env, runs once the secret materializes."

### No action needed (verified during this pass)

- `modules/lambda` log retention is 7d (`infrastructure/modules/lambda/main.tf:42`) — story 6.1's claim is correct.
- `modules/dynamodb` exports `chat_rooms_table_name` (`infrastructure/modules/dynamodb/outputs.tf:1`) — story 6.4's env-var wiring is valid.
- `signed_in_user` fixture lives in `infrastructure/src/tests/integration/conftest.py` (phase 5.7) — story 6.1's fixture-layering plan is grounded.
- `@with_edge_secret` decorator + `EDGE_SECRET` env-var pattern are proven by the now-removed hello stub — stories 6.1–6.4 inherit the same shape.

### Recommendation

Three majors (M1, M2, M3) and one blocker (B1) are worth a PRD edit before dispatch — all are small. Md1, Mi1 are phase-completion handoff items (no in-story edit). Md2, Mi2 can land via dispatch-brief notes if the user wants to move faster.

---

## 2026-06-09 12:30 brainstorm

Fourth pass — narrow sweep after the third-pass edits (B1/M1/M2/M3/Md2/Mi1/Mi2) were applied. Only checked for edit-induced regressions: dependency cycles, AC contradictions across stories, missing-home for moved content.

### Verified clean

- M2 — `6.2.depends_on = [6.0a, 6.0b, 6.4]` and `6.3.depends_on = [6.0a, 6.0b, 6.4]`; `6.4.depends_on = [6.0a, 6.0b]`. No cycle. Topological order is deterministic: 6.0 → 6.0a → 6.0b → 6.1 → 6.4 → 6.2 → 6.3 → 6.5 → 6.6 → 6.7.
- M1 — story 6.2's HTTP 404 for stale-request accept does not collide with story 6.7's E2E flow (6.7 tests only the happy-path accept and the 409-BLOCKED-on-block path).
- M3 — story 6.4's new HTTP 200 + `chat_deactivation_pending: true` contract on DynamoDB error does not conflict with any other AC (6.5 auth sweep is status-agnostic for /v1/blocks; 6.7 E2E does not assert 500).
- Md2 — `is_blocked` parameterized-binding AC is code-review testable; no other story makes a contradictory claim.
- Mi2 — `pytest.skip` in `app_user_conn` cleanly cascades to story 6.6's two tests that consume it.

### New gap

**G1 — Mi1 removed the per-story carryover bullets from 6.0a and 6.7 in favor of "a single consolidated `## Carryovers from phase 6` block written at phase-completion handoff", but the orchestrator's "Phase completion handoff" checklist has no step for writing carryover notes to other phases' PRDs.** If followed literally, the carryover deferral note disappears.
**Resolution (applied):** added a final AC to story 6.7 owning the carryover write directly — appends `## Carryovers from phase 6` blocks to both `implementationplan/phase-11-hardening.md` (1/30-day username rename limit + per-route throttling) and `implementationplan/phase-8-chat.md` (chat-room-without-friendship is read-only, from third-pass Md1). Writes are idempotent (heading detection + bullet dedup). PRD `last_updated:` annotation bumped to record the fourth-pass edit.

### Recommendation

Edits applied. No further blockers. Proceed to Step 1 (tracking-issue creation) on the next `/implement-phase 6` run.

## 2026-06-09 14:30 brainstorm (pre-dispatch readiness check, 5th pass)

Scope: final pre-execution sweep. Surface only material NEW concerns since the 4th-pass brainstorm and the phase-5 merge + dev destroy.

### Findings

**1. MAJOR — Story 6.4 depends_on is missing 6.1, and its integration-test setup is self-contradictory.**

- Story 6.4 (`depends_on: [6.0a, 6.0b]`) consumes the `completed_profile_user(sex)` pytest fixture, which is introduced by story 6.1. At 6.4 dispatch time under the current depends_on, 6.1 has not yet been dispatched — the fixture does not exist.
- Story 6.4's integration-test AC says "A (Male) and B (Female) are friends (set up via 6.2 friend-request flow which is also deployed)." But topologically 6.2 runs AFTER 6.4 (`6.2 depends_on: [..., 6.4]`). At 6.4 dispatch time the friends Lambda is NOT yet deployed; the API path the AC names is unavailable.
- **Resolution (PRD edit required):**
  - Add `6.1` to story 6.4's `depends_on` → `depends_on: [6.0a, 6.0b, 6.1]`.
  - Replace the integration-test setup clause: instead of "set up via 6.2 friend-request flow", the test seeds the friendship row via direct master-credential INSERT against `friendships` (canonical lex-min/max ordering matching `chat_room_id`). The block + chat-room deactivation behaviors under test do not require the friends Lambda to be deployed.
  - The follow-up assertion "POST /v1/friend-requests {toUserId: B} returns 409 BLOCKED" must be deleted from 6.4 (the friends Lambda doesn't exist yet); that scenario is already covered by story 6.2's own block-aware integration test.

**2. MINOR — Story 6.0's hello-removal AC over-states the prod cleanup.**

- The AC says "Remove the `module \"hello\"` block from infrastructure/environments/dev/main.tf and infrastructure/environments/prod/main.tf." On disk, hello references exist ONLY in `dev/main.tf` (phase-5 story 5.7 was dev-only — confirmed via grep: 12 hits in dev/main.tf, 0 in prod/main.tf).
- **Resolution (no PRD edit needed):** the subagent will grep prod, find nothing, and treat it as a no-op. The AC is over-broad but not incorrect.

**3. DRIFT NOTE — dev infrastructure is currently destroyed.**

- Per context.md line 7 and 47, dev was destroyed via `deploy.yml workflow_dispatch action=destroy environment=dev` (run 27188014855) on 2026-06-09 to halt cost.
- Story 6.0's `terraform plan` AC already accounts for this (third-pass B1: full bring-up, no incremental diff assertion). Integration tests in 6.1–6.7 cannot run until the first phase-6 apply rebuilds phase-1..5 infra (Aurora cluster, Cognito User Pool, HTTP API, CloudFront, WAF) plus phase-6 Lambdas. The subagent must understand this — the first apply will create ~90+ resources, not just the new domain Lambdas.
- **Resolution (no PRD edit; brief subagent at dispatch).** Include in the per-story brief for 6.0 (and downstream stories that have integration tests) a one-line reminder: "dev infra is currently destroyed; the first apply on your branch will bring up everything from phase 1 through phase 6."

**4. MINOR — `module.dynamodb.chat_rooms_table_name` output exists** ✓ (verified). `module.cloudfront.edge_secret`, `module.api_gateway.authorizer_id`, `module.observability_layer.layer_arn` all exist ✓. No drift on referenced module outputs.

**5. Process — phase-5 hotfix-on-development violation is already a recorded lesson** (per context.md and the hotfix-branch memory). No new lesson required pre-dispatch; any new phase-6 lessons get appended at handoff per the standard checklist.

### Recommendation

One PRD edit required (finding #1: story 6.4 `depends_on` and integration-test setup). Findings #2–#5 are informational only. Recommend the user pick **address** to apply the 6.4 edit, then re-run `/implement-phase 6`.

## 2026-06-09 14:55 brainstorm (re-run verification, 6th pass)

Scope: verify the 5th-pass patch to story 6.4 lands cleanly and no new concerns surfaced after the edit.

### Patch verification

- Story 6.4 line 143: `depends_on: [6.0a, 6.0b, 6.1]` ✓ — `6.1` added, comment explains the rationale.
- Story 6.4 line 166: integration test rewritten — friendship seeded via direct master-credential INSERT into `friendships` (lex-min/max ordering matching `chat_room_id`); ChatRooms row optionally pre-created via direct boto3 PutItem; trailing 409 BLOCKED assertion deleted with explicit pointer to story 6.2 which owns that coverage ✓.
- Story 6.4 negative integration test (line 167) unchanged — still asserts 409 NOT_FRIENDS when blocker and target aren't friends ✓.

### Topological dispatch order (10 stories)

With the patched dependencies, topological order is unambiguous:
`6.0 → 6.0a → 6.0b → 6.1 → 6.4 → 6.2 → 6.3 → 6.5 → 6.6 → 6.7`

Single DAG, no cycles. No story is unreachable.

### New concerns

None. The patch resolves the prior finding without introducing new gaps.

### Recommendation

Patch verified. Proceed to Step 1 (tracking-issue creation) and dispatch.



