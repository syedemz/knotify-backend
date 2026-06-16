phase: 7
title: Match and deck
last_updated: 2026-06-16 (story 7.0a complete)

context_summary: |
  Implements the matching and swipe-deck endpoints backed by pgvector ranking
  and Postgres hard filters per §5.5 of architecture.md, plus the supporting
  pieces that the phase-6 brainstorm round surfaced as gaps: a dedicated
  knotify-match Lambda scaffold, an extended deck_view that carries the columns
  required for filtering and similarity ranking, a separate refresh Lambda
  running as a privileged role (because app_user is intentionally NOT granted
  EXECUTE on refresh_deck_view()), the preference-vector write-path wired into
  the phase-6 profile PATCH handler, and the cross-cutting onboarding gate
  (custom:profile_complete JWT claim + @require_profile_complete decorator)
  that ensures match and all other business endpoints reject users whose
  profiles are not complete.

  Decisions captured from the 2026-06-15 brainstorm (see
  phasebrainstorms/phase-7-match-brainstorm.md and questions/answers.txt):
    - Issue 1 — Refresh runs in a dedicated Lambda assuming a privileged
      aurora_refresh role; profile Lambda async-invokes it after a
      profile-completion flip.
    - Issue 2 — Opposite-sex filter is baked into the deck handler SQL
      (RLS does not propagate through materialized views).
    - Issue 3 — A new migration (0011) drops and recreates deck_view with the
      additional columns preference_vector and religion (sex, age,
      resident_country_code, current_residence_country are already present
      from migration 0009).
    - Issue 4 — The phase-6 PATCH /v1/profile/me handler is updated to compute
      preference_vector via encode_prefs() and write it in the same UPDATE
      whenever the patch body includes preferences.
    - Issue 7 — Deck pagination: ORDER BY user_id ASC, cursor is the last
      user_id seen, batch size 20.
    - Issue 8 — Hard country filter uses resident_country_code (CHAR(2)).
    - Issue 9 — v1 ships religion-only matching; subsect filter deferred.
    - Issue 15 — Empty-vector fallback: if requester's preference_vector is
      NULL or all zeros, the handler skips cosine ordering and returns results
      ordered by created_at DESC.
    - Onboarding ceremony — Option 3: after profile-complete flips, frontend
      shows a "You're all set! Enter Knotify" CTA, then silently refreshes the
      Cognito session to pick up the updated custom:profile_complete claim.
      No explicit re-login screen.

  Decisions captured from the 2026-06-16 re-brainstorm (see
  phasebrainstorms/phase-7-match-brainstorm.md "2026-06-16 08:20 brainstorm"
  section and questions/answers7.txt):
    - Finding 1 — block_filter() lives in knotify_obs (not knotify_db).
      Stories 7.1 and 7.2 import from knotify_obs.
    - Finding 2 — Deck SQL (story 7.2) aliases deck_view as `dv`; the
      block_filter whitelist gets two new entries: `"u.user_id"` (for 7.1)
      and `"dv.user_id"` (for 7.2).
    - Finding 3 — Empty-vector fallback wording corrected: fallback triggers
      on the requester's vector state alone (NULL or all zeros), not on
      candidate vectors.
    - Finding 4 — Async refresh invoke in story 7.4 fires **outside** the
      `with conn:` block, after the connection's transaction has committed.
    - Finding 5 — Migration 0013 also creates a tiny `refresh_log
      (refreshed_at timestamptz)` table; the refresh Lambda inserts one row
      per actual refresh (not per skip). The advisory-lock integration test
      counts rows and asserts exactly 1.
    - Finding 6 — `custom:profile_complete` becomes a real Cognito custom
      attribute, NOT a per-token Aurora lookup. The phase-6 profile PATCH
      handler calls `cognito-idp admin_update_user_attributes` after the
      `with conn:` block when the flag flips. cognito_pre_token_generation
      copies the attribute into the access-token claim. No Aurora roundtrip
      on the auth path.
    - Finding 7 — 7.0b integration test explicitly triggers a token refresh
      via `cognito-idp initiate_auth` (REFRESH_TOKEN_AUTH) between the
      profile-completion PATCH and the second round of endpoint calls.
    - Finding 8 — Migrations 0011/0012/0013 are assigned to 7.0a/7.0b/7.4.
      If a hotfix lands a migration before dispatch, renumber and update
      cross-references; yoyo applies in filename order.
    - Finding 9 — Story 7.4's aurora-refresh-credential secret reuses the
      existing migrator pattern for app_user_credential (see
      `db_migrator/handler.py`).
    - Finding 10 — `@require_profile_complete` decorator is applied ONLY to
      friends, bookmarks, blocks handlers. It is NOT applied to
      cognito_post_confirmation, cognito_pre_token_generation, or
      db_migrator (Cognito triggers + admin paths).
    - Finding 11 — Decorator implementation lives at
      `infrastructure/src/layers/observability/knotify_obs/_profile_complete.py`,
      one decorator per file (mirrors _edge_secret.py, _chat_room_id.py).
    - External assumption A — psycopg2's vector binding uses the explicit
      `%s::vector` SQL cast; `pgvector.psycopg2.register_vector()` is NOT
      used and pgvector is NOT added to the knotify_db layer manifest.
    - External assumption B — Candidates whose preference_vector is all
      zeros produce NaN cosine distance; we accept arbitrary ordering for
      them (Postgres tends to land them last; no explicit filter added).
    - Profile-completion REQUIRED-field set finalized (see
      WIDENED_CHECK_FIELDS appendix below): 34 fields total. Notable owner
      decisions: phone_number NOT required (Cognito handles re-verify);
      marriage_time NOT required + column gets DEFAULT 'Not Provided' so
      frontend can leave it blank without breaking the CHECK (re-brainstorm
      finding B2 resolution); relation required (self-vs-on-behalf-of indicator); the
      three education *_passing_year fields NOT required;
      partners_religious_level moved to preferences (NOT required);
      photo_url and chosen_profile_avatar NOT required (deferred to
      phase 12).
    - Re-brainstorm finding B1 — Cognito `custom:profile_complete`
      attribute uses option (c): investigate stability first. Add the
      schema block, apply, re-plan to confirm zero diff. If oscillation
      observed (matches the standard-attribute pattern from hotfix #85),
      fall back to a documented two-step apply that lifts/restores
      `ignore_changes = [schema]`. Lessons-learned entry added either way.
    - Re-brainstorm finding B3 — Profile module Terraform gains two new
      inputs: `cognito_user_pool_id` (becomes USER_POOL_ID env var on the
      profile Lambda) and `cognito_user_pool_arn` (scopes the new
      `cognito-idp:AdminUpdateUserAttributes` IAM statement on
      aurora_writer). Sourced from `module.cognito.user_pool_id` /
      `.user_pool_arn` in dev and prod root modules.

  The match queries themselves (POST /v1/match/search) read from users (not
  deck_view), because search exposes ad-hoc filter combinations and benefits
  from the HNSW index on users.preference_vector. The deck (GET
  /v1/match/deck) reads from deck_view exclusively, because the deck is the
  hot read-path and the materialized view exists precisely to serve it.

  Cross-phase impact:
    - Story 7.0b modifies phase-6 code (the cognito_pre_token_generation
      Lambda, the profile Lambda, and every Lambda in phase 6 that should
      be gated by @require_profile_complete). It also adds migration 0012 to
      widen the profile_complete_requires_required_fields CHECK constraint.
    - Story 7.3 modifies phase-6 code (the profile PATCH handler) to write
      preference_vector alongside preferences.
    - Story 7.4 modifies phase-6 code (the profile PATCH handler) to
      async-invoke the refresh Lambda after a successful
      profile_complete_verified flip.
  Each of these stories carries an explicit file-list and redeploy checklist.

stories:
  - id: 7.0
    title: knotify-match Lambda scaffold and Terraform module
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 89
    acceptance_criteria:
      - infrastructure/src/functions/match/ created with handler.py (empty
        dispatcher returning 404 for unknown routes), __init__.py,
        requirements.txt, and tests/ directory mirroring phase-6 layout
      - New IAM role aurora_reader_match (VPC execution, scoped
        secretsmanager:GetSecretValue on knotify-<env>-app-user-credential
        and kms:Decrypt on the corresponding KMS key); module under
        infrastructure/modules/iam_roles/ following the phase-6 pattern
      - New Terraform module infrastructure/modules/match/ (Lambda function
        on ARM64, knotify_db + knotify_obs layers attached, environment
        variables AURORA_HOST/PORT/DBNAME, DB_SECRET_NAME, EDGE_SECRET; no
        routes wired in this story — that's 7.5)
      - dev and prod environments instantiate module "match" with terraform
        validate clean; Makefile package target builds match.zip
      - Unit tests pass against the empty dispatcher (404 for any path)
    notes: |
      Pattern source: phase-6 module "profile" and IAM role aurora_writer.
      No routes are wired here; 7.5 handles that after 7.1 and 7.2 land.

  - id: 7.0a
    title: deck_view extended columns migration
    agent: backenddeveloper
    done: true
    depends_on: []
    tracking_issue: 90
    acceptance_criteria:
      - New migration 0011_deck_view_extended.sql DROPs deck_view and
        recreates it with the original 14 columns PLUS preference_vector
        and religion; idx_deck_user (UNIQUE on user_id) recreated; SELECT
        grant to app_user recreated
      - Rollback migration restores the original 14-column deck_view
      - refresh_deck_view() function untouched (still LANGUAGE sql wrapping
        REFRESH MATERIALIZED VIEW CONCURRENTLY)
      - docker-compose db test confirms a verified user with non-NULL
        preference_vector and religion appears in deck_view with those
        values after refresh
    notes: |
      Materialized views cannot be ALTERed to add columns in Postgres —
      DROP + CREATE is the standard pattern. Re-running the migration
      against a populated cluster will require a refresh post-migration;
      capture that in the rollout note.

  - id: 7.0b
    title: Onboarding enforcement — profile_complete JWT claim, decorator, widened CHECK
    agent: backenddeveloper
    done: false
    depends_on: []
    tracking_issue: 91
    acceptance_criteria:
      - Migration 0012_profile_complete_widen_check.sql replaces the
        profile_complete_requires_required_fields CHECK constraint with the
        widened field set (see WIDENED_CHECK_FIELDS list at the bottom of
        this PRD; 34 flat IS-NOT-NULL fields); rollback migration restores
        the original 5-field check. Same migration sets a column default on
        marriage_time: `ALTER TABLE users ALTER COLUMN marriage_time SET
        DEFAULT 'Not Provided'` and backfills any existing NULL rows:
        `UPDATE users SET marriage_time = 'Not Provided' WHERE
        marriage_time IS NULL` — so the column is never NULL going forward
        and frontend can leave the field blank without breaking the CHECK
        (default fires on INSERT, and the column does not appear in
        REQUIRED, so single users are unaffected)
      - Phase-6 profile PATCH handler's _REQUIRED_FOR_COMPLETION constant
        is widened to match the new CHECK fields exactly — remains a flat
        `frozenset` of 34 column names (no conditional predicates needed
        because marriage_time was moved out of REQUIRED). Existing unit
        and integration tests updated so the flip-to-true path uses the
        new 34-field complete-row fixture
      - Cognito custom attribute `custom:profile_complete` declared on the
        user pool via Terraform (string, mutable, min=1, max=5). Hotfix
        #85 added `lifecycle { ignore_changes = [schema] }` on
        `aws_cognito_user_pool.this` to suppress provider 6.x oscillation
        of STANDARD attribute blocks. **Custom-attribute apply protocol**:
          (1) Add the new `schema { name = "profile_complete" ... }` block
              under the existing schema blocks in
              `infrastructure/modules/cognito/main.tf`.
          (2) Apply in dev and capture the plan/apply output.
          (3) Re-run `terraform plan` immediately afterward with no edits.
              If plan shows zero diff, custom attributes are stable under
              the existing lifecycle and no further action is needed
              (record this in the story PR body as "custom attributes
              confirmed stable").
          (4) If the re-plan shows oscillation (add/remove of the new
              schema block), fall back to a documented two-step apply:
              temporarily lift `ignore_changes = [schema]`, apply, restore
              the lifecycle, apply again to confirm zero diff. Capture the
              full sequence in the PR body and append a lessons-learned
              entry to `C:\Users\syede\Claude-Master\lessons.md`
        Phase-6 profile PATCH handler is extended so that — AFTER the
        `with conn:` block commits and ONLY when profile_complete_verified
        flips from false to true in the same UPDATE — it calls
        `cognito-idp admin_update_user_attributes` to set
        `custom:profile_complete = "true"` on the Cognito user. The call
        is best-effort: on transient failure log a structured warning and
        return 200 to the client (next PATCH or a phase-11 backfill job
        retries). NOTE: this AC supersedes the prior "Aurora-on-every-
        token" approach — there is NO per-request Aurora lookup in the
        auth path
      - Profile module Terraform wiring gains two new input variables:
        `cognito_user_pool_id` (string) and `cognito_user_pool_arn`
        (string), sourced from `module.cognito.user_pool_id` and
        `module.cognito.user_pool_arn` in both dev and prod root modules.
        The profile Lambda's `environment_variables` map gains
        `USER_POOL_ID = var.cognito_user_pool_id`. The aurora_writer IAM
        role policy gains a statement granting
        `cognito-idp:AdminUpdateUserAttributes` scoped to
        `var.cognito_user_pool_arn` (NOT `*`). Unit test on the IAM module
        asserts the new statement's Action and Resource match exactly
      - cognito_pre_token_generation Lambda extended to copy the user's
        `custom:profile_complete` attribute into the access-token's claims
        as `custom:profile_complete` (string "true"/"false"; default "false"
        if the attribute is absent on the user). The Lambda does NOT
        connect to Aurora. Unit test asserts the claim mirrors the source
        attribute and defaults to "false" when missing
      - New shared decorator @require_profile_complete in
        `infrastructure/src/layers/observability/knotify_obs/_profile_complete.py`
        (one file per decorator, mirrors _edge_secret.py and
        _chat_room_id.py). Exported from knotify_obs/__init__.py. Reads
        `custom:profile_complete` from
        `event.requestContext.authorizer.jwt.claims`; if "false", returns
        403 {"error":"profile_incomplete"}; if claim absent, treats as
        false (fail-closed). Unit test covers all three branches
      - Decorator applied to every existing phase-6 Lambda route EXCEPT
        GET /v1/profile/me and PATCH /v1/profile/me (which are the
        onboarding endpoints themselves). Affected files:
        infrastructure/src/functions/friends/handler.py,
        infrastructure/src/functions/bookmarks/handler.py,
        infrastructure/src/functions/blocks/handler.py. Explicitly NOT
        applied to cognito_post_confirmation, cognito_pre_token_generation,
        or db_migrator (Cognito triggers + admin paths)
      - Integration test: a confirmed-but-incomplete user (Aurora row with
        profile_complete_verified=false; Cognito attribute either absent or
        "false") receives a JWT carrying custom:profile_complete=false;
        calls to GET /v1/friends, GET /v1/bookmarks, GET /v1/blocks all
        return 403 {"error":"profile_incomplete"}; GET /v1/profile/me and
        PATCH /v1/profile/me succeed (PATCH that completes the profile
        flips the Aurora flag AND writes "true" to the Cognito attribute).
        The test then explicitly triggers a token refresh via `boto3
        cognito-idp initiate_auth(AuthFlow="REFRESH_TOKEN_AUTH")`; the
        re-issued JWT carries custom:profile_complete=true and the same
        endpoints return 200. Test asserts the explicit refresh call —
        does not rely on access-token TTL expiry
      - Re-packaged profile.zip, friends.zip, bookmarks.zip, blocks.zip,
        cognito_pre_token_generation.zip; terraform validate clean in dev
        and prod
    notes: |
      Frontend ceremony (Option 3) is purely a client-side concern:
      after PATCH returns 200 with profile_complete_verified=true, show
      "You're all set! Enter Knotify" CTA, then call cognitoUser
      .refreshSession() to pick up the new claim. The backend does not
      orchestrate this — it just exposes the claim and enforces the gate.

      Lessons-learned: the @with_edge_secret decorator already lives in
      knotify_obs; follow that pattern for the new decorator. Order of
      decorators on each handler: @with_edge_secret outermost,
      @require_profile_complete inside it, so edge-secret rejection
      happens before any JWT-claims work.

      Eventual-consistency footnote: the Cognito-attribute path is
      eventually consistent with Aurora (a successful PATCH may briefly
      have Aurora=true and Cognito=false, until the admin_update call
      lands). Acceptable — the client must refresh the session anyway
      to pick up any change, and a stale 403 just prompts another
      refresh. Worst-case: the admin_update call fails silently and the
      user stays gated until the next PATCH (or a phase-11 backfill job
      sweeps users with Aurora=true / Cognito=false mismatches).

      Cognito IAM: profile Lambda needs `cognito-idp
      :AdminUpdateUserAttributes` on the user pool ARN. Add to
      aurora_writer's IAM policy in the profile module.

  - id: 7.1
    title: knotify-match POST /v1/match/search
    agent: backenddeveloper
    done: false
    depends_on: [7.0]
    tracking_issue: 92
    acceptance_criteria:
      - Handler for POST /v1/match/search accepts a JSON body with
        {countries (list of CHAR(2) codes against resident_country_code),
        religion (string), age_min (int), age_max (int)} and returns up to
        50 candidate profiles
      - SQL queries the users table directly (not deck_view) and mirrors
        §5.5 of architecture.md, with these specifics:
          * The explicit u.sex predicate is omitted (RLS enforces opposite-
            sex visibility on users); a brief comment notes the rationale
          * Block filtering uses knotify_obs.block_filter("u.user_id");
            the helper lives in the observability layer (NOT knotify_db).
            The shared helper's _ALLOWED_COLUMNS whitelist is extended to
            include both `"u.user_id"` (this story) and `"dv.user_id"`
            (story 7.2) in a single edit so callers don't fight for the
            file; existing alias-qualified entries from hotfix #87 stay
            untouched
          * subsect is NOT a filter in v1 (deferred — see notes)
      - Ranking: ORDER BY u.preference_vector <=> $requester_vector ASC;
        when the requester's preference_vector is NULL or all zeros, the
        handler omits the cosine ORDER BY and falls back to ORDER BY
        created_at DESC for a deterministic ordering. The fallback path
        is exercised by a unit test. Candidate rows whose
        preference_vector is itself NULL or all zeros are NOT filtered
        from the result set — Postgres returns NaN for their cosine
        distance and orders them at the tail of the result (acceptable
        for v1; revisit if user reports complain)
      - Integration test: seed five candidates with known preference
        vectors; the requester whose vector is closest to candidate C
        gets C in position 1
      - Integration test: seed candidate C; requester blocks C; the
        search no longer returns C
      - Integration test (empty-vector fallback): a requester whose
        preference_vector is NULL gets results ordered by created_at DESC,
        not random
    notes: |
      v1 ships religion-only filtering; subsect deferred to a later phase.
      The country filter accepts CHAR(2) codes ONLY (e.g. "GB", "PK") and
      rejects free-text country names at the input-validation layer.

  - id: 7.2
    title: knotify-match GET /v1/match/deck
    agent: backenddeveloper
    done: false
    depends_on: [7.0, 7.0a]
    tracking_issue: 93
    acceptance_criteria:
      - Handler for GET /v1/match/deck reads from deck_view (aliased as
        `dv` in the SQL: `FROM deck_view dv`) and enforces opposite-sex
        visibility via explicit
        "WHERE dv.sex != current_setting('app.requesting_user_sex', true)"
        in the SQL — RLS does not propagate through materialized views,
        so this filter is the only thing enforcing it on the deck path
      - Block filtering uses knotify_obs.block_filter("dv.user_id"); the
        helper lives in the observability layer (NOT knotify_db). The
        `"dv.user_id"` whitelist entry is added in the single
        _ALLOWED_COLUMNS edit performed alongside story 7.1's
        `"u.user_id"` addition (see 7.1 AC bullet on block filtering)
      - Pagination: results are ordered by user_id ASC; the response
        includes a next_cursor field equal to the last user_id in the
        batch (or null when fewer than 20 rows returned). A ?cursor=<uuid>
        query parameter selects rows with user_id > <uuid>; default batch
        size 20
      - Optional ?filters=<url-encoded-json> query parameter accepts the
        same JSON structure as the search body; when present, hard filters
        are applied before pagination
      - Integration test: deck returns only profile_complete_verified=true
        users (seed one verified + one unverified; assert only the verified
        one appears)
      - Integration test: cursor pagination round-trip — first call with
        no cursor returns 20 rows and a next_cursor; second call with that
        cursor returns the next 20 rows starting after the cursor
      - Integration test: opposite-sex filter exercise — a Male requester's
        deck contains zero Male candidates even when seeded
    notes: |
      Cursor is the raw user_id (UUID string). No base64 wrapping in v1 —
      callers should treat next_cursor as opaque but for now it's plain
      text. The empty-vector fallback in 7.1 does NOT apply to the deck;
      the deck does not rank by similarity (it returns the same hard-
      filtered set as search in user_id order, suitable for swipe-style
      browsing). Story 7.6 verifies that the deck's MEMBERSHIP equals the
      search's membership; it does not assert equal ordering.

  - id: 7.3
    title: Preference vector encoder shared utility + profile PATCH integration
    agent: backenddeveloper
    done: false
    depends_on: []
    tracking_issue: 94
    acceptance_criteria:
      - New module infrastructure/src/layers/db/knotify_db/prefs.py (lives
        in the existing knotify_db layer) exposes encode_prefs(prefs:
        dict) -> list[float] implementing the 20-key mapping from §5.5 of
        architecture.md verbatim; PREFERENCE_KEYS is the single source of
        truth
      - Unit test: encode_prefs({}) returns 20 zeros; encode_prefs(
        {"highlyeducated": True}) returns [1.0, 0.0, 0.0, ..., 0.0];
        importing PREFERENCE_KEYS in a second module returns the same list
        object
      - Phase-6 profile PATCH handler updated so that whenever
        "preferences" is in the patch body, the handler ALSO computes
        preference_vector = encode_prefs(preferences) and writes it as
        part of the same UPDATE using the explicit SQL cast
        `preference_vector = %s::vector` (the value is bound as a Python
        list/str via psycopg2's default adapter — pgvector's
        `register_vector()` adapter is NOT used and `pgvector` is NOT
        added to the knotify_db layer manifest). Files modified:
          * infrastructure/src/functions/profile/handler.py
          * infrastructure/src/functions/profile/tests/test_handler.py
          * tests/integration/test_profile.py
      - Unit test (profile): PATCH with preferences in body produces an
        UPDATE that sets both preferences (JSONB) and preference_vector
        (vector(20)) in a single SQL statement
      - Integration test: PATCH /v1/profile/me with
        {"preferences": {"highlyeducated": true, "athletic": true}};
        re-query users; preference_vector column is non-NULL and has 1.0
        at the corresponding indices, 0.0 elsewhere
      - profile.zip re-packaged and module "profile" replanned in dev and
        prod with terraform validate clean
    notes: |
      psycopg2 vector binding: pass the list as a Python list and cast
      with %s::vector in the SQL ("UPDATE users SET preference_vector =
      %s::vector ..."). No pgvector type adapter registration is needed
      for the write path — the cast handles it.

  - id: 7.4
    title: deck_view refresh — dedicated Lambda + privileged role + scheduler + PATCH trigger
    agent: backenddeveloper
    done: false
    depends_on: [7.0a]
    tracking_issue: 95
    acceptance_criteria:
      - Migration 0013_aurora_refresh_role.sql creates a new Postgres role
        aurora_refresh (LOGIN, NOSUPERUSER, NOBYPASSRLS) and GRANTs
        EXECUTE on refresh_deck_view() to it. The same migration creates a
        small instrumentation table `refresh_log(id bigserial PK,
        refreshed_at timestamptz NOT NULL DEFAULT now())` with INSERT
        granted to aurora_refresh; the refresh Lambda inserts exactly one
        row per actual refresh performed (skip-paths do NOT insert). The
        advisory-lock integration test below asserts row count to prove
        only one concurrent invocation did the work. Rollback drops the
        role, the grant, and the table.
        Migrator Lambda generates a random password post-yoyo and stores
        it in a new secret knotify-<env>-aurora-refresh-credential —
        reuses the EXACT pattern used for app_user_credential in phase 2
        (`db_migrator/handler.py`); no new abstraction
      - New Lambda infrastructure/src/functions/refresh_deck_view/ with
        handler that:
          * connects to Aurora using the new aurora-refresh secret (NOT
            the app-user secret)
          * acquires pg_try_advisory_lock(<chosen-constant>); if the lock
            is held, logs "refresh skipped — already running" and returns
            success
          * executes SELECT refresh_deck_view()
          * releases the advisory lock and returns
      - New IAM role aurora_refresh_lambda (VPC execution, scoped
        secretsmanager:GetSecretValue on the aurora-refresh secret and
        kms:Decrypt on its KMS key)
      - aws_cloudwatch_event_rule fires the refresh Lambda every 15
        minutes in both dev and prod
      - Phase-6 profile PATCH handler updated so that when
        profile_complete_verified flips from false to true in the same
        UPDATE, the handler asynchronously invokes the refresh Lambda
        (boto3 lambda.invoke with InvocationType="Event"). The invoke is
        issued **outside** the `with conn:` block — i.e., AFTER the
        connection's transaction has fully committed. If the txn rolls
        back the invoke is skipped (verified by a unit test that raises
        inside the `with` block and asserts no invoke was issued). Files
        modified:
          * infrastructure/src/functions/profile/handler.py
          * infrastructure/src/functions/profile/tests/test_handler.py
          * tests/integration/test_profile.py
          * infrastructure/modules/iam_roles/main.tf (aurora_writer role
            extended with lambda:InvokeFunction on the refresh Lambda's
            ARN)
      - Rebuild + redeploy checklist documented in story PR body:
          * make package FUNC=profile
          * make package FUNC=refresh_deck_view
          * terraform plan / apply in dev
          * confirm scheduled rule visible in AWS console
      - Integration test: insert a verified user via direct Aurora INSERT
        (bypassing PATCH); assert SELECT * FROM deck_view WHERE
        user_id=<new_user> returns no row; invoke the refresh Lambda
        synchronously via boto3; assert the row now appears
      - Integration test (advisory lock): invoke the refresh Lambda twice
        in parallel; both return 200; assert SELECT COUNT(*) FROM
        refresh_log WHERE refreshed_at > <test_start> = 1 (the new
        instrumentation table in migration 0013 is the side-channel)
    notes: |
      The profile Lambda needs lambda:InvokeFunction permission on the
      refresh Lambda's ARN. Add that to aurora_writer's policy in the
      profile IAM role module.

      The refresh Lambda is a singleton in practice (only one schedule,
      only one trigger source besides the schedule). It does not need
      provisioned concurrency.

      Why dedicated Lambda + privileged role (vs. granting app_user
      EXECUTE): keeps the RLS-bearing app_user surface minimal; failure
      modes are isolated; the refresh path can be retried, scaled, and
      observed independently of business endpoints.

  - id: 7.5
    title: HTTP API route wiring for match
    agent: backenddeveloper
    done: false
    depends_on: [7.0, 7.0b, 7.1, 7.2]
    tracking_issue: 96
    acceptance_criteria:
      - POST /v1/match/search and GET /v1/match/deck are registered as
        aws_apigatewayv2_route resources with authorizer_id pointing at
        the existing Cognito JWT authorizer and integration pointing at
        the knotify-match Lambda
      - aws_lambda_permission attached for execute-api invocation (using
        the api_execution_arn output added in hotfix #86, NOT the
        default_stage_arn)
      - Both routes are decorated with @require_profile_complete (from
        7.0b) so incomplete users get 403 profile_incomplete
      - terraform plan in dev shows exactly two new routes + one new
        permission; terraform apply succeeds
      - test_route_wiring.py extended to parametrize the two new routes
        (401-unauthenticated-via-CF and 403-authenticated-via-execute-api-
        no-edge-secret invariants)
    notes: ""

  - id: 7.6
    title: End-to-end match ordering test
    agent: backenddeveloper
    done: false
    depends_on: [7.0, 7.0a, 7.0b, 7.1, 7.2, 7.3, 7.4, 7.5]
    tracking_issue: 97
    acceptance_criteria:
      - New fixture seeded_match_candidates(requester_id, n=10) in
        tests/integration/conftest.py that creates n candidate users with
        deterministic preference vectors (requester_vector +
        known_perturbation), all profile_complete_verified=true and of
        the opposite sex to the requester
      - Integration test tests/integration/match_e2e_test.py signs up a
        requester, completes their profile through the onboarding flow
        (validates the @require_profile_complete decorator gate releases
        after completion), and seeds 10 candidates via the fixture
      - POST /v1/match/search returns all 10 candidates in the expected
        pgvector cosine distance order; the assertion checks the EXACT
        ordering (not just membership)
      - GET /v1/match/deck returns the same SET (membership equality, not
        order equality — deck is user_id-ordered for swipe stability)
      - After the requester blocks the top-ranked candidate, that
        candidate is absent from BOTH responses
      - The test invokes the refresh Lambda explicitly between seeding and
        querying the deck (matches the production trigger path)
    notes: |
      Membership-vs-order distinction is deliberate per the 7.2 cursor
      decision. The deck is a swipe surface, not a leaderboard. Search is
      the ranking surface.


# ============================================================================
# WIDENED_CHECK_FIELDS — final field list for migration 0012
# ============================================================================
#
# Finalized 2026-06-16 per owner direction in questions/answers7.txt and the
# follow-up 2026-06-16 re-brainstorm resolution (marriage_time moved OUT of
# REQUIRED; column gets DEFAULT '0' to keep INSERTs clean).
#
# 34 fields total. Identity bucket unchanged from the prior 5-field CHECK;
# the remaining 29 fields are the widening.
#
# The CHECK constraint for migration 0012 requires every REQUIRED field
# below to be NOT NULL when profile_complete_verified = true. All clauses
# are flat IS NOT NULL — no conditional predicates.
#
#
# REQUIRED (must be non-NULL for profile_complete_verified = true) — 34 fields
# ---------------------------------------------------------------------------
#   Identity (already required by current CHECK; carried forward):
#     - first_name
#     - last_name
#     - sex
#     - birthday
#     - username
#
#   Religion (matching-critical):
#     - religion
#     - subsect
#     - religious_level
#
#   Residence:
#     - current_residence_city
#     - current_residence_country
#     - resident_country_code
#     - district
#
#   Education:
#     - education_level
#     - highest_degree
#     - high_school
#     - higher_secondary
#     - college_name
#     # Note: the three *_passing_year columns are NOT required (owner
#     # answer 4, 2026-06-16): graduation_year, high_school_passing_year,
#     # higher_secondary_passing_year all moved to "explicitly not
#     # required".
#
#   Profession:
#     - job_title
#     - employer_name
#     - employment_type
#     - office_address
#     - professional_category
#     - salary_range
#
#   Family:
#     - fathers_name
#     - fathers_job
#     - father_retired       (boolean: yes/no dropdown on frontend)
#     - mothers_name
#     - mothers_job
#     - mother_retired       (boolean: yes/no dropdown on frontend)
#     - family_residence_address  (full address, not just city)
#
#   Personal status:
#     - marital_status
#     - has_children
#     - move_abroad
#     - relation             (indicates whether the profile is being
#                             created by the user themselves or by someone
#                             on their behalf; required for matching-
#                             context disambiguation)
#
#
# EXPLICITLY NOT REQUIRED — per owner direction
# ---------------------------------------------
#   Vectors / matching:
#     - preferences            (JSONB blob — empty {} is acceptable)
#     - preference_vector      (computed from preferences; NULL/zeros OK)
#     - partners_religious_level (moved to preferences — owner answer 8)
#
#   Media (phase 12):
#     - photo_url              (photo uploads not yet shipped)
#     - chosen_profile_avatar  (phase 12)
#
#   Relations / structure:
#     - siblings               (separate table; no rows required)
#
#   Identity & contact:
#     - phone_number           (owner answer 1; Cognito handles re-verify,
#                               not foundational to profile completion)
#
#   Personal status (2026-06-16 re-brainstorm resolution):
#     - marriage_time          (TEXT, NOT required. Migration 0012 also
#                               sets DEFAULT 'Not Provided' on the column
#                               and backfills existing NULLs to
#                               'Not Provided'. Frontend can leave the
#                               field blank for any user; the column will
#                               never be NULL going forward.
#                               _REQUIRED_FOR_COMPLETION stays a flat
#                               frozenset.)
#
#   Education years (owner answer 4):
#     - graduation_year
#     - high_school_passing_year
#     - higher_secondary_passing_year
#
#
# NOT INCLUDABLE IN CHECK (computed or audit columns)
# ---------------------------------------------------
#   - user_id, email       (already NOT NULL at the column level)
#   - age                  (GENERATED ALWAYS from birthday)
#   - created_at, updated_at, deleted_at (audit)
#   - profile_complete_verified (the flag itself)
#
#
# Migration 0012 CHECK clause — structural shape
# ----------------------------------------------
#   CHECK (
#     profile_complete_verified = false
#     OR (
#       first_name IS NOT NULL
#       AND last_name IS NOT NULL
#       AND sex IS NOT NULL
#       AND birthday IS NOT NULL
#       AND username IS NOT NULL
#       AND religion IS NOT NULL
#       AND subsect IS NOT NULL
#       AND religious_level IS NOT NULL
#       AND current_residence_city IS NOT NULL
#       AND current_residence_country IS NOT NULL
#       AND resident_country_code IS NOT NULL
#       AND district IS NOT NULL
#       AND education_level IS NOT NULL
#       AND highest_degree IS NOT NULL
#       AND high_school IS NOT NULL
#       AND higher_secondary IS NOT NULL
#       AND college_name IS NOT NULL
#       AND job_title IS NOT NULL
#       AND employer_name IS NOT NULL
#       AND employment_type IS NOT NULL
#       AND office_address IS NOT NULL
#       AND professional_category IS NOT NULL
#       AND salary_range IS NOT NULL
#       AND fathers_name IS NOT NULL
#       AND fathers_job IS NOT NULL
#       AND father_retired IS NOT NULL
#       AND mothers_name IS NOT NULL
#       AND mothers_job IS NOT NULL
#       AND mother_retired IS NOT NULL
#       AND family_residence_address IS NOT NULL
#       AND marital_status IS NOT NULL
#       AND has_children IS NOT NULL
#       AND move_abroad IS NOT NULL
#       AND relation IS NOT NULL
#     )
#   )
#
# Story 7.0b implements this. The handler's _REQUIRED_FOR_COMPLETION
# constant in profile/handler.py stays a flat frozenset of the same 34
# column names — no conditional predicates, no structural change. The
# in-Lambda "should we flip the flag" check matches the DB constraint
# 1:1 by simple set membership.
#
# Migration 0012 also includes:
#   ALTER TABLE users ALTER COLUMN marriage_time SET DEFAULT 'Not Provided';
#   UPDATE users SET marriage_time = 'Not Provided' WHERE marriage_time IS NULL;
# so the column is never NULL going forward without forcing the user to
# fill it in. The rollback migration drops the default and does NOT
# restore NULLs (NULLs were a transient state pre-migration).
