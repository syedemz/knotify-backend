phase: 6
title: Profile, friends, bookmarks, blocks domain Lambdas
last_updated: 2026-06-09  # story 6.0a done. 3rd brainstorm: B1 (drop impossible plan-diff AC), M1 (6.2 declined_auto → 404), M2 (6.2/6.3 depends_on += 6.4), M3 (6.4 HTTP 200 + chat_deactivation_pending), Md2 (6.0b parameterized-binding AC for is_blocked), Mi1 (consolidated phase-11 carryover), Mi2 (app_user_conn pytest.skip). 4th brainstorm: G1 (6.7 owns the consolidated carryover writes to phase-11 + phase-8 PRDs). 5th brainstorm (pre-dispatch readiness): 6.4 depends_on += 6.1 (consumes completed_profile_user fixture); 6.4 integration test rewritten to seed friendship via direct master-credential INSERT (friends Lambda not yet deployed at 6.4 dispatch time); trailing 409 BLOCKED assertion deleted from 6.4 (already covered by 6.2's block-aware test).

context_summary: |
  Ships the first wave of business-logic Lambdas: knotify-profile, knotify-friends, knotify-bookmarks, knotify-blocks. Each Lambda derives user_id from the JWT sub (never from URL or body), sets the RLS session GUCs after authorizing, and uses the shared Aurora layer from phase 3. The corresponding REST routes (per §4.2 migration map) are wired through the HTTP API + JWT authorizer + CloudFront stack from phase 5. The stub /v1/_internal/hello endpoint from phase 5 story 5.7 is removed in story 6.0 BEFORE any domain Lambda lands. Subsequent phases consume these domain Lambdas — chat (phase 8) calls friends and blocks logic to authorize room creation; match (phase 7) calls block lookups to filter results.
  
  Brainstorm-driven structural changes (2026-06-09):
  - New story 6.0a adds the missing `username` UNIQUE constraint via migration 0010 (case-insensitive, partial — allows multiple NULL bootstrap rows). The 1/30-day rename rate limit is deferred to phase 11 (hardening) and tracked there.
  - New story 6.0b adds two shared helpers to the observability layer (`knotify_obs`): `chat_room_id(user_a, user_b)` (used by blocks here and by chat in phase 8) and `block_filter_sql_fragment()` / `is_blocked(conn, a, b)` (used by friends/bookmarks/blocks here, by match in phase 7, by chat in phase 8). Centralizing both primitives prevents per-Lambda re-implementation drift.
  - Each Lambda story (6.1–6.4) now owns its own Terraform wiring (lambda module + integration + route + permission + IAM). Story 6.5 reduces to a regression sweep across all wired routes (401 without JWT, 403 without edge secret).
  - Story 6.6 splits its third criterion off as a docker-compose unit test (no live Lambda needed).
  - The `signed_in_user` fixture from phase 5.7 stays; story 6.1 builds a `completed_profile_user(sex)` fixture on top of it. Every later story consumes the new fixture.

stories:
  - id: 6.0
    title: Remove the phase-5 /v1/_internal/hello stub before any domain Lambda lands
    agent: backenddeveloper
    done: true
    tracking_issue: 72
    depends_on: []
    acceptance_criteria:
      - Delete infrastructure/src/functions/hello/ (the entire directory)
      - Delete infrastructure/src/tests/integration/test_edge_smoke.py
      - Remove the `module "hello"` block from infrastructure/environments/dev/main.tf and infrastructure/environments/prod/main.tf
      - Remove the `aws_apigatewayv2_route` for `GET /v1/_internal/hello` (and any associated `aws_apigatewayv2_integration` + `aws_lambda_permission`) from the same files
      - Remove the hello entry from Makefile `package-all`
      - The `signed_in_user` pytest fixture extracted into `infrastructure/src/tests/integration/conftest.py` in phase 5.7 STAYS — phase-6 integration tests reuse it
      - `terraform plan` against dev contains zero `+ create` lines mentioning `hello` or `_internal/hello` (e.g. `terraform plan -no-color | grep -E '(^|\s)\+\s.*(hello|_internal/hello)' | wc -l` is 0). The total plan size is not asserted — dev infra is currently destroyed (see context.md 2026-06-09 destroy run 27188014855), so the first phase-6 apply will be a full bring-up, not an incremental diff. Brainstorm B1 (third pass).
      - `terraform validate` clean; full unit + integration test suite still passes (with the smoke test gone)
    notes: "Brainstorm Md5 (phase-5): the throwaway hello smoke endpoint must be removed BEFORE the first real domain Lambda is introduced — keeping it leaks a public unauthenticated-by-edge-secret-only route. The cleanup is intentionally story 6.0 (not a notes-field tracking item) so it cannot be missed."

  - id: 6.0a
    title: Add `username` UNIQUE constraint (migration 0010)
    agent: backenddeveloper
    done: true
    tracking_issue: 73
    depends_on: [6.0]
    acceptance_criteria:
      - Create `infrastructure/db/migrations/0010_username_unique.sql` adding a case-insensitive partial unique index, e.g. `CREATE UNIQUE INDEX users_username_lower_unique ON users (lower(username)) WHERE username IS NOT NULL;` — partial on NOT NULL so the many NULL-username bootstrap rows do not collide
      - Create the matching `0010_username_unique.rollback.sql` that drops the index
      - The migration applies cleanly via the db_migrator Lambda against the existing dev cluster (no schema rewrite of `users` required — pure index add)
      - A docker-compose unit test under `infrastructure/src/tests/db/test_username_unique.py` inserts two rows with the same `username` (case differing) and asserts the second INSERT raises `psycopg2.errors.UniqueViolation`; also inserts two rows with NULL `username` and asserts both succeed (partial-index correctness)
      - The 1/30-day username rate limit from architecture §5.7 is explicitly DEFERRED to phase 11 (hardening). Brainstorm Mi1 (third pass): the carryover note is no longer written per-story; instead, a single consolidated `## Carryovers from phase 6` block is appended to `implementationplan/phase-11-hardening.md` at phase-completion handoff (see phase-completion checklist below) listing the username rename limit, per-route throttling (story 6.7), and any other phase-6 deferrals
      - `terraform plan` clean against dev; null_resource.db_migrator_invoke fires on apply because `migrations_hash` changes
    notes: "Brainstorm B2 (Option A + E): adds the database-side defense against username races; the API-side 30-day rename limit is deferred."

  - id: 6.0b
    title: Add shared helpers to the observability layer — chat_room_id + block-aware filter
    agent: backenddeveloper
    done: false
    tracking_issue: 74
    depends_on: [6.0]
    acceptance_criteria:
      - In `infrastructure/src/layers/observability/knotify_obs/`, add a new module `_chat_room_id.py` exporting `chat_room_id(user_a: str, user_b: str) -> str` that returns `hashlib.sha256(f"{min}:{max}".encode("utf-8")).hexdigest()` where `min`/`max` are the lexicographically ordered user UUIDs (string compare). Re-export from `knotify_obs.__init__`
      - In the same layer, add `_blocks.py` exporting:
          - (a) `block_filter(other_user_col: str) -> str` — a Python builder function (NOT a raw string template) that returns a SQL fragment of the form `NOT EXISTS (SELECT 1 FROM blocks b WHERE (b.blocker_id = <other_user_col> AND b.blocked_id = %s) OR (b.blocker_id = %s AND b.blocked_id = <other_user_col>))`. Brainstorm NEW-2 — to keep column-name substitution safe, `other_user_col` is validated INSIDE the function against a hard-coded whitelist of legal column references (initial whitelist: `friendships.user_a`, `friendships.user_b`, `friend_requests.requester_id`, `friend_requests.receiver_id`, `bookmarks.bookmarked_user_id`, `users.user_id`). Any value outside the whitelist raises `ValueError`. The function does NOT format user-supplied values into SQL — only the whitelisted column identifier. The caller binds two `%s` parameters (the requesting user's id, twice) at `cur.execute(...)` time.
          - (b) `is_blocked(conn, user_a: str, user_b: str) -> bool` that issues a single SELECT against the `blocks` table for the pair in either direction and returns True if any row exists. Brainstorm Md2 (third pass) — the SELECT MUST be issued via `cur.execute(sql, (user_a, user_b, user_b, user_a))` with parameter binding; no f-string interpolation or string concatenation of `user_a`/`user_b` into the SQL. UUID-format validation is the caller's responsibility (Lambda handlers parse the URL path through a UUID regex before invoking `is_blocked`).
          - Re-export both from `knotify_obs.__init__`.
      - Unit tests (docker-compose; under `infrastructure/src/layers/observability/tests/`):
          - `chat_room_id` is symmetric (`chat_room_id(a,b) == chat_room_id(b,a)`), deterministic across calls, and produces a 64-char hex string
          - `is_blocked` returns True when a forward block exists, True when a reverse block exists, False when no block exists
          - `block_filter("friendships.user_b")` returns the expected SQL fragment; `block_filter("' OR 1=1 --")` raises `ValueError` (injection-attempt rejected by whitelist); the returned fragment, when embedded into a representative SELECT against `friendships`, filters out blocked pairs in both directions
      - The observability layer's build.sh + layer manifest are updated so the new modules ship in the layer ZIP; the existing 22 observability-layer unit tests still pass (no regressions)
      - Layer version bump (Brainstorm NEW-7 — phrasing corrected): all Lambdas that depend on `module.observability_layer.layer_arn` will pick up the new layer version on next apply, because the lambda module resolves the output to the most-recent published `aws_lambda_layer_version` ARN at plan time. The new exports (`chat_room_id`, `block_filter`, `is_blocked`) are additive — no existing imports change behavior. The apply diff is expected to show every existing Lambda's `layers` attribute changing to the new layer ARN; this is benign.
    notes: "Brainstorm M5 + B3 (Option B): shared primitives that prevent per-Lambda duplication. `chat_room_id` is consumed by story 6.4 here and by phase 8 (chat). The block filter SQL/helper is consumed by stories 6.2 (friends), 6.3 (bookmarks), 6.4 (blocks), and later by phase 7 (match) and phase 8 (chat)."

  - id: 6.1
    title: knotify-profile Lambda (including PATCH-completion semantics, username search, and own Terraform wiring)
    agent: backenddeveloper
    done: false
    tracking_issue: 75
    depends_on: [6.0a, 6.0b]
    acceptance_criteria:
      - `src/functions/profile/` implements handlers for GET /v1/profile/me, PATCH /v1/profile/me, GET /v1/profiles?username=..., GET /v1/profiles/{userId}
      - PATCH /v1/profile/me semantics (Brainstorm B1 / Option A — explicit):
          - The request body is validated against a schema accepting all profile fields.
          - For each immutable-after-set field from §5.7 (first_name, last_name, sex, birthday, religion, subsect): the value is accepted only if the column is currently NULL (first set); rejected with HTTP 400 + JSON body `{"error":"immutable_field","fields":[...]}` listing every field the client tried to change after it was already set. Re-PATCHing with the EXACT SAME value as the existing column value is a no-op (HTTP 200), not a 400.
          - The mutable fields from §5.7 are always settable.
          - In the same transaction, if all of {first_name, last_name, sex, birthday, username} are non-NULL after the update, the Lambda sets `profile_complete_verified = true`. The CHECK constraint `profile_complete_requires_required_fields` from migration 0002 remains the DB-side guard; the Lambda's responsibility is to flip the flag when the row qualifies.
          - The trigger `trg_users_immutable` from migration 0008 is defense-in-depth — the Lambda's 400 must fire FIRST so the client gets a clean error rather than a 500 from the DB raise.
      - GET /v1/profiles?username=... uses CASE-INSENSITIVE EXACT MATCH (`WHERE lower(username) = lower($1)`), backed by the unique index from story 6.0a. Returns the deck-view subset (architecture §5.7) for the single matching opposite-sex user. Returns HTTP 404 when no match (or RLS hides the match because the target is same-sex).
      - GET /v1/profiles/{userId} returns only the deck-view subset of fields (§5.7); does not leak email, phone_number, family fields. HTTP 404 when not found OR when RLS hides the row.
      - GET /v1/profile/me returns the FULL profile for the requesting user (RLS exception path — own row is always visible via the `OR user_id = current_setting(...)` clause in the policy).
      - Lambda uses `knotify_db.rls_context()` to set GUCs on every request; the connection is reused module-level (cold-start friendly).
      - `completed_profile_user(sex)` pytest fixture added to `infrastructure/src/tests/integration/conftest.py` (Brainstorm M2 + NEW-6):
          - Parametrized factory that builds on top of `signed_in_user` and PATCHes a minimal complete profile (first_name="Test", last_name="User", sex=<param>, birthday="2000-01-01", username=`f"test_{uuid4().hex[:12]}"`, religion="Other").
          - Step 1: consume `signed_in_user` to get the initial token pair (the token claim is `custom:profile_complete = "false"` at this point).
          - Step 2: PATCH /v1/profile/me with the completion payload using the initial access token. Assert HTTP 200; assert via the master Aurora connection that `profile_complete_verified` flipped to true.
          - Step 3: Brainstorm NEW-6 — PreTokenGeneration only fires at login/refresh, NOT at PATCH. Explicitly call `cognito_client.admin_initiate_auth` (ADMIN_USER_PASSWORD_AUTH) AGAIN to mint a fresh token pair. Assert the NEW id_token and the NEW access_token both carry `custom:profile_complete = "true"`. The initial tokens from `signed_in_user` are NOT mutated (Cognito tokens are immutable) — the fixture discards them and yields only the post-PATCH tokens.
          - Yields a dict matching `signed_in_user`'s shape plus the completed fields and the fresh post-PATCH token pair.
          - Teardown reuses `signed_in_user`'s existing teardown chain (the fixture is layered on top, not parallel).
      - Terraform wiring owned by this story (Brainstorm M6 / Option A):
          - `module "profile"` in dev/main.tf and prod/main.tf using the existing lambda module, with observability + db layers, `aurora_writer` IAM role (Aurora rights via existing role; no DynamoDB access needed for profile), private VPC subnets, DB_SECRET_NAME env var.
          - `aws_apigatewayv2_integration.profile` + four `aws_apigatewayv2_route` resources (GET /v1/profile/me, PATCH /v1/profile/me, GET /v1/profiles, GET /v1/profiles/{userId}) all with `authorization_type = JWT` and `authorizer_id = module.api_gateway.authorizer_id`.
          - `aws_lambda_permission.profile_api_gateway` scoped to the four routes' source_arns.
          - EDGE_SECRET env var injected from `module.cloudfront.edge_secret`; handler decorated with `@with_edge_secret` from `knotify_obs`.
      - CloudWatch log retention is 7 days (confirmed inherited from the `modules/lambda` module's existing log_group resource).
      - Integration test against the dev API (post-apply): `completed_profile_user(sex="Male")` and `completed_profile_user(sex="Female")` are minted, then:
          - GET /v1/profile/me by Male returns full profile (email, etc. all present).
          - GET /v1/profiles?username=<Female's username> by Male returns deck-view fields only (no email, no phone, no family).
          - GET /v1/profiles?username=<other Male's username> by Male returns HTTP 404 (RLS hides same-sex).
          - GET /v1/profiles/{Female's user_id} by Male returns deck-view fields only.
          - PATCH /v1/profile/me by the Male user attempting to change `sex` returns HTTP 400 with `{"error":"immutable_field","fields":["sex"]}`.
          - PATCH /v1/profile/me re-sending the existing first_name returns HTTP 200 (idempotent no-op).
    notes: "Brainstorm B1 (Option A), M2 (Option A), M6 (Option A), Md1 (case-insensitive exact match). The username search backing index is created by story 6.0a; this story consumes it."

  - id: 6.2
    title: knotify-friends Lambda (block-aware, own Terraform wiring)
    agent: backenddeveloper
    done: false
    tracking_issue: 76
    depends_on: [6.0a, 6.0b, 6.4]  # Brainstorm M2 (third pass): block-aware integration test POSTs /v1/blocks → 6.4 must be deployed first.
    acceptance_criteria:
      - `src/functions/friends/` implements GET /v1/friends, DELETE /v1/friends/{userId}, GET /v1/friend-requests, POST /v1/friend-requests, POST /v1/friend-requests/{id}/accept, POST /v1/friend-requests/{id}/decline, DELETE /v1/friend-requests/{id}
      - Block-aware behavior (Brainstorm B3 / Option B — consumes shared helpers from 6.0b):
          - POST /v1/friend-requests calls `is_blocked(conn, requester, target)` before any INSERT. If True, return HTTP 409 + `{"error":"blocked"}`. The block check happens INSIDE the same transaction as the INSERT so a freshly-arriving block (under concurrent load) cannot race the request through.
          - GET /v1/friends queries `friendships` AND joins/applies `BLOCK_FILTER_SQL` from `knotify_obs._blocks` against the candidate `other_user_id` column so any pair where a block exists in either direction is filtered out.
          - GET /v1/friend-requests applies the same filter against both sender and receiver dimensions.
          - POST .../accept and POST .../decline on a `request_id` that no longer exists return HTTP 404 + `{"error":"not_found"}`. Brainstorm M1 (third pass): story 6.4's POST /v1/blocks DELETEs any pending `friend_requests` rows between the pair in the same transaction, so the prior "accept-with-stale-block → HTTP 200 declined_auto" branch is unreachable in practice. The 404 path is the correct contract; no `declined_auto` enum is introduced.
      - POST /v1/friend-requests rejects with HTTP 409 + `{"error":"already_pending"}` when a pending request already exists between the same pair (enforced by the UNIQUE constraint from phase 2)
      - Accepting a request inserts a `friendships` row using `chat_room_id`-style canonical ordering (`user_a` is the lexicographically smaller UUID, `user_b` is the larger one — this is the same lexicographic-min/max contract that backs `chat_room_id`) AND updates the `friend_requests.status` to 'accepted' in a single transaction
      - Lambda uses `knotify_db.rls_context()` on every request; uses the existing `aurora_writer` IAM role
      - Terraform wiring owned by this story (Brainstorm M6 / Option A): `module "friends"` + integration + 7 routes + permission, in both dev and prod main.tf. EDGE_SECRET injected; handler decorated with `@with_edge_secret`.
      - Integration test against the dev API: two `completed_profile_user` instances (opposite sex). A sends request to B, B accepts, GET /v1/friends from both sides shows the other user. A DELETE /v1/friends/{B} removes the friendship row.
      - Block-aware integration test: A blocks B via POST /v1/blocks (story 6.4 is now a hard `depends_on` per third-pass M2, so it is always deployed at this point). A POST /v1/friend-requests {toUserId: B} returns 409 BLOCKED. Additionally: B creates a friend-request to A, A blocks B (which DELETEs the pending request), B's POST /v1/friend-requests/{id}/accept on the now-stale request_id returns HTTP 404 (third-pass M1).
    notes: "Brainstorm B3 (Option B), M6 (Option A). The canonical pairing convention used here (lexicographic min/max of UUIDs) matches the contract from `knotify_obs.chat_room_id` so phase-8 chat sees consistent pair ordering."

  - id: 6.3
    title: knotify-bookmarks Lambda (block-aware, own Terraform wiring)
    agent: backenddeveloper
    done: false
    tracking_issue: 77
    depends_on: [6.0a, 6.0b, 6.4]  # Brainstorm M2 (third pass): integration test POSTs /v1/blocks to verify "bookmark a blocked user → 409" — 6.4 must be deployed first.
    acceptance_criteria:
      - `src/functions/bookmarks/` implements GET /v1/bookmarks, POST /v1/bookmarks, DELETE /v1/bookmarks/{userId}
      - POST is idempotent — a second POST with the same `userId` returns HTTP 200 (not 409). Implementation: `INSERT ... ON CONFLICT (user_id, bookmarked_user_id) DO NOTHING RETURNING *` with the empty `RETURNING` treated as success.
      - POST /v1/bookmarks calls `is_blocked(conn, requester, target)` from `knotify_obs`. If True, return HTTP 409 + `{"error":"blocked"}`. Bookmarks of blocked users are not permitted.
      - GET /v1/bookmarks returns the denormalized deck-view fields for each bookmarked user. The SELECT joins `bookmarks → users` and applies `BLOCK_FILTER_SQL` so bookmarked users who have since blocked the requester (or vice versa) are silently filtered out of the response.
      - Lambda uses `knotify_db.rls_context()`; `aurora_writer` IAM role; module-level connection reuse.
      - Terraform wiring owned by this story (Brainstorm M6): `module "bookmarks"` + integration + 3 routes + permission, in both dev and prod main.tf. EDGE_SECRET; @with_edge_secret.
      - Integration test against the dev API: A (Male) bookmarks two Female profiles, GET returns both with deck-view fields. A DELETEs one, GET returns the remaining one. A bookmarks a third Female who has blocked A — POST returns 409 BLOCKED.
    notes: "Brainstorm B3 (Option B), M6 (Option A)."

  - id: 6.4
    title: knotify-blocks Lambda (new IAM role, chat-room deactivation, own Terraform wiring)
    agent: backenddeveloper
    done: false
    tracking_issue: 78
    depends_on: [6.0a, 6.0b, 6.1]  # 5th brainstorm: 6.1 added because 6.4's integration test consumes the `completed_profile_user` fixture introduced by 6.1.
    acceptance_criteria:
      - `src/functions/blocks/` implements GET /v1/blocks, POST /v1/blocks, DELETE /v1/blocks/{userId}
      - POST /v1/blocks rejects with HTTP 409 + `{"error":"not_friends"}` if no friendship row exists between blocker and target (owner decision 2026-05-24 — blocking is only allowed against current friends).
      - On POST, in a SINGLE Aurora transaction:
          - INSERT INTO blocks (blocker_id, blocked_id, created_at)
          - DELETE the friendship row for the canonical pair (auto-unfriend per owner decision 2026-05-24)
          - DELETE any pending friend_requests rows between the two users in either direction
      - After the Aurora transaction COMMITS, the Lambda calls DynamoDB UpdateItem on the ChatRooms table:
          - `room_id` keyed via `knotify_obs.chat_room_id(blocker_id, blocked_id)` (Brainstorm M5 — the shared helper from story 6.0b)
          - SET status='deactivated', deactivated_reason='blocked', deactivated_by=<blocker_id>, deactivated_at=<NOW iso8601>
          - ConditionExpression: `attribute_exists(room_id) AND (attribute_not_exists(#status) OR #status = :active)` with ExpressionAttributeNames `{"#status": "status"}` and ExpressionAttributeValues `{":active": "active"}`. Brainstorm NEW-4: the conjunctive condition ensures (a) the row exists (no-op when no room was ever created) AND (b) the room is currently `active`. If the room was previously deactivated for ANOTHER reason (e.g., `user_deleted_account`), the SET would clobber that reason — the condition prevents it.
          - The Lambda catches `botocore.exceptions.ClientError` with `Code == "ConditionalCheckFailedException"` (Brainstorm Md3 + NEW-4) and treats it as success (logged at INFO level, not WARN/ERROR). The log line is "block: chat-room deactivation no-op (room absent or already deactivated for another reason)". Brainstorm M3 (third pass) — any other DynamoDB error is caught, logged at ERROR with the boto error code, and the endpoint returns HTTP 200 with response body `{"chat_deactivation_pending": true, ...}` (the rest of the body is the normal block response). The Aurora transaction is NOT rolled back — the block is committed. The `chat_deactivation_pending` flag tells the client the chat room may still appear active until phase 8's per-write status re-check picks up the lag; the lag is self-healing. The client never sees HTTP 500 when the block succeeded at the database. (Returning HTTP 500 with a committed Aurora block would mislead the client into thinking the block failed, even though the user is in fact blocked.)
      - DELETE /v1/blocks/{userId} is the dual operation: DELETE FROM blocks, then DynamoDB UpdateItem to reactivate the chat room. Reactivation per architecture §5.4.1, with safety:
          - SET status='active', REMOVE deactivated_reason, REMOVE deactivated_by, REMOVE deactivated_at, SET reactivated_at=<NOW iso8601>
          - ConditionExpression: `attribute_exists(room_id) AND deactivated_reason = :blocked AND deactivated_by = :unblocker` with ExpressionAttributeValues `{":blocked": "blocked", ":unblocker": <unblocker_id>}`. Brainstorm NEW-5: the reactivation only fires when (a) the room exists, (b) it was deactivated specifically because of a block, AND (c) the block was placed by the same user now removing it. This prevents an unblock from resurrecting rooms deactivated for unrelated reasons (account deletion) or by a different blocker.
          - Same ConditionalCheckFailedException → INFO-level no-op contract as the deactivation path.
      - New IAM role `blocks_writer` added to `modules/iam_roles` (Brainstorm M4 / Option A):
          - Trust policy: lambda.amazonaws.com
          - Attached policies: VPC ENI management (AWSLambdaVPCAccessExecutionRole), Aurora app-user secret GetSecretValue (scoped to `arn:aws:secretsmanager:*:*:secret:knotify-${env}-app-user-credential-*`), and an inline policy granting `dynamodb:UpdateItem` ONLY on `arn:aws:dynamodb:*:*:table/ChatRooms`.
          - Module test extended: 14/14 → 16/16 with two new plan-mode tests asserting the new role exists and has the scoped DynamoDB action.
      - Lambda env vars include `TABLE_CHAT_ROOMS = module.dynamodb.chat_rooms_table_name` (Brainstorm M4).
      - Terraform wiring owned by this story (Brainstorm M6): `module "blocks"` + integration + 3 routes + permission, in both dev and prod main.tf. EDGE_SECRET; @with_edge_secret.
      - Integration test (positive — 5th-brainstorm rewrite): two `completed_profile_user` instances are minted (A=Male, B=Female). The friends Lambda is NOT yet deployed at this point, so the test seeds the friendship row directly via master-credential INSERT into `friendships` using the lex-min/max canonical ordering of the two UUIDs (the same contract that backs `chat_room_id`); optionally INSERTs a pending `friend_requests` row between A and B as well. Optionally pre-creates a ChatRooms row via direct boto3 PutItem keyed on `chat_room_id(A_id, B_id)` with `status='active'` so the deactivation path is exercised. A POST /v1/blocks {userId: B} via the deployed blocks Lambda — assert HTTP 200 — then assert (i) the friendships row is gone, (ii) any pending friend_request row between them is gone, (iii) if the ChatRooms row was pre-created, its `status` is now `'deactivated'` with `deactivated_reason='blocked'` and `deactivated_by=A_id`. If no ChatRooms row was pre-created, the conditional update no-ops and the POST still returns HTTP 200. A DELETE /v1/blocks/B succeeds; if the ChatRooms row exists it reactivates (`status='active'`, deactivated_* attrs removed, `reactivated_at` set). The follow-up "friend-request via the friends Lambda now succeeds" assertion is INTENTIONALLY OMITTED from this story — at 6.4 dispatch time the friends Lambda is not yet deployed (6.2 runs after 6.4 per its `depends_on`); the block-aware friend-request behavior is covered by story 6.2's own integration test.
      - Integration test (negative): A is NOT friends with B, A POST /v1/blocks {userId: B} returns 409 NOT_FRIENDS and writes nothing to Aurora or DynamoDB (assert blocks table row count unchanged, ChatRooms row absent).
    notes: "Brainstorm M4 (Option A), M5 (Option A), Md3 (catch ConditionalCheckFailedException), M6 (Option A). The chat_room_id helper consumed here is the same one phase 8 will use, so the deactivation/reactivation cycle is symmetric with chat-room creation."

  - id: 6.5
    title: HTTP API route wiring regression sweep (replaces batch-wiring; verifies authorization invariants)
    agent: backenddeveloper
    done: false
    tracking_issue: 79
    depends_on: [6.1, 6.2, 6.3, 6.4]
    acceptance_criteria:
      - For every route registered by stories 6.1–6.4, verify (programmatically via an integration test) that:
          - The route exists in the deployed dev HTTP API (lookup via `aws apigatewayv2 get-routes`).
          - `authorization_type` is `JWT` and `authorizer_id` matches `module.api_gateway.authorizer_id`.
          - An unauthenticated request (no Authorization header) via the CloudFront URL returns HTTP 401.
          - An authenticated request that hits the execute-api URL directly (bypassing CloudFront) returns HTTP 403 due to the missing edge secret — confirmed by `@with_edge_secret` rejecting at the Lambda layer.
      - No new Terraform resources land in this story — it is a verification/regression-sweep story only. (If the verification reveals a gap from 6.1–6.4, fix it in the originating story; do not patch it here.)
      - `terraform plan` shows zero diff (everything was wired by upstream stories).
    notes: "Brainstorm M1 (delete hello-stub criterion), M6 (Option A — stories 6.1–6.4 own their own wiring; this story shrinks to a regression sweep)."

  - id: 6.6
    title: RLS session GUC enforcement integration tests
    agent: backenddeveloper
    done: false
    tracking_issue: 80
    depends_on: [6.1, 6.2, 6.3, 6.4, 6.5]
    acceptance_criteria:
      - A new integration test suite `infrastructure/src/tests/integration/test_rls_enforcement.py` drives the deployed dev API end-to-end and verifies opposite-sex enforcement at the API layer:
          - Test: a Male user calls GET /v1/profiles?username=<existing-male-username> and receives HTTP 404 even though the user exists (because RLS hides the row at the DB and the handler converts an empty result to 404).
          - Test: the same Male user calls GET /v1/profile/me and receives his own row (RLS exception path — `OR user_id = current_setting(...)`).
          - Both tests use `completed_profile_user(sex)` from the fixture introduced in story 6.1.
          - Adds an `app_user_conn` pytest fixture in `conftest.py` (Brainstorm N4) that connects to Aurora as `app_user` (not master) using the credential from `knotify-${env}-app-user-credential` — RLS only fires against app_user under FORCE ROW LEVEL SECURITY, so visibility tests that need the policy to apply must use this fixture. Brainstorm Mi2 (third pass): the fixture calls `pytest.skip("app_user credential not yet materialized — first phase-6 apply has not run db_migrator")` if `secretsmanager.GetSecretValue` raises `ResourceNotFoundException`. This makes the test a no-op on a freshly-destroyed env (e.g. the current 2026-06-09 dev destroy state) and runs normally once the secret materializes.
      - A separate docker-compose unit test `infrastructure/src/tests/db/test_rls_fail_closed.py` (Brainstorm M3 / Option A) verifies the fail-closed behavior at the migration level. Seed three users (one extra Male) so both branches of the policy are exercised:
          - Apply all migrations against the local Postgres container.
          - Open a connection as `app_user` (using the local password from `local_init.sql`).
          - Using master credentials (bypassing RLS for the seed), insert THREE users: Male-A, Male-B, Female-C.
          - **Fail-closed assertion (GUCs unset):** as app_user, WITHOUT setting `app.requesting_user_id` / `app.requesting_user_sex` GUCs, execute `SELECT * FROM users`. Assert the result set is EMPTY (the policy evaluates to NULL on every row because both GUC reads return NULL → fail-closed proven).
          - **Policy-applied assertion (GUCs set to Male-A):** as app_user, SET `app.requesting_user_id` = Male-A's UUID and `app.requesting_user_sex` = 'Male', re-execute the SELECT. Assert the result set is EXACTLY TWO ROWS: Male-A's own row (visible via the `user_id = current_setting('app.requesting_user_id')::uuid` OR branch — the identity exception) and Female-C's row (visible via the `sex != 'Male'` branch — the opposite-sex visibility rule). Male-B's row is filtered (same sex as the requester, different identity → neither branch matches).
          - **Policy-applied assertion (GUCs set to Female-C):** RESET GUCs, then SET them to Female-C's UUID and sex='Female'. Assert the result set is EXACTLY TWO ROWS: Female-C's own row and Male-A's row OR Male-B's row depending on which is "her" opposite-sex visible — but actually BOTH Male-A and Male-B are opposite-sex relative to Female, so the result set is THREE ROWS: Female-C + Male-A + Male-B. (Adjust the assertion to expect 3 rows in this branch.)
          - Run via `pytest -m "not integration"` so it executes in the docker-compose unit-test suite, not against AWS.
    notes: "Brainstorm M3 (Option A — split criterion 3 to docker-compose unit), N4 (app_user_conn fixture)."

  - id: 6.7
    title: End-to-end suite for the four domains
    agent: backenddeveloper
    done: false
    tracking_issue: 81
    depends_on: [6.1, 6.2, 6.3, 6.4, 6.5, 6.6]
    acceptance_criteria:
      - `infrastructure/src/tests/integration/test_domains_e2e.py` signs up two opposite-sex users via Cognito (using `completed_profile_user`), then exercises in a single test flow:
          - profile read (GET /v1/profile/me, GET /v1/profiles/{other})
          - profile update (PATCH /v1/profile/me — a mutable field like job_title)
          - friend request send/accept (POST /v1/friend-requests → POST .../accept → GET /v1/friends both sides)
          - bookmark add/list/remove (POST → GET → DELETE → GET)
          - block + unblock affecting friend-request behavior (POST /v1/blocks → POST /v1/friend-requests returns 409 BLOCKED → DELETE /v1/blocks/{userId} → POST /v1/friend-requests now succeeds)
      - Makefile `test-e2e` target added (Brainstorm N2): runs `pytest infrastructure/src/tests/integration/test_domains_e2e.py -v -m integration` with the required env vars sourced from `infrastructure/src/tests/integration/.env.test` (the file written by story 5.6).
      - The entire suite passes against dev when invoked with `make test-e2e` and exits zero.
      - Throttling on hot routes (Brainstorm Md5 / Q12) deferred to phase 11 (hardening). The deferral is recorded by the carryover writes below — not as a separate handoff step.
      - Carryover writes (Brainstorm G1, fourth pass — owns the consolidated carryover write that Mi1 left without a home):
          - Append the following block to `implementationplan/phase-11-hardening.md` immediately after its `context_summary:` field (create the block if absent; if a `## Carryovers from phase 6` block already exists, append the new bullets under it instead of duplicating the heading):
            ```
            ## Carryovers from phase 6
            - 1/30-day `username` rename rate limit (architecture §5.7) — DB-side UNIQUE constraint shipped in phase 6.0a; API-side rate limit must land here. Source: phase-6 story 6.0a.
            - Per-route throttling on hot routes (architecture §13 / Brainstorm Md5/Q12) — phase 6 stories 6.1–6.4 ship default HTTP API stage throttling only (burst=10/rate=25 from phase-5 story 5.1); per-route overrides for /v1/profiles?username=, /v1/friend-requests, /v1/blocks land here. Source: phase-6 story 6.7.
            ```
          - Append the following block to `implementationplan/phase-8-chat.md` immediately after its `context_summary:` field (same idempotency rule):
            ```
            ## Carryovers from phase 6
            - Chat-room-without-backing-friendship is read-only — phase-6 story 6.4 deletes the friendship row on POST /v1/blocks and reactivates the chat room on DELETE /v1/blocks, but does NOT recreate the friendship on unblock. Phase 8's chat-write path must treat an `active` ChatRooms row whose canonical pair has no `friendships` entry as read-only. Source: phase-6 third-pass Md1.
            ```
          - Both writes are idempotent (running them twice is a no-op): the subagent checks for an existing `## Carryovers from phase 6` heading and, if found, only appends bullets that aren't already present.
          - The phase-11 and phase-8 PRD `last_updated:` fields are bumped to today's date in the same edit.
    notes: "Brainstorm N2 (Makefile target), N3 (transitive depends_on expanded for clarity), G1 (fourth pass — own the consolidated carryover write)."
