phase: 2
title: Data layer (Aurora + DynamoDB)
last_updated: 2026-05-23  # story 2.6 done

context_summary: |
  Provisions the entire persistent data layer. Aurora Serverless v2 cluster (Postgres 16.4 with vector, pg_trgm, pgcrypto extensions loaded via CREATE EXTENSION), the full relational schema from §5.1 of architecture.md (users, siblings, friendships, friend_requests, bookmarks, blocks) with the immutable-fields trigger, the RLS gender-isolation policy, and the deck_view materialized view per the owner's resolved §13 #4. DynamoDB tables (ChatRooms, ChatRoomMembership, ChatMessages with Streams enabled, MessageReads, Notifications with Streams enabled and the UnreadIndex GSI, PushNotificationTokens). Schema migration tooling is selected here (yoyo-migrations, locked in via 2026-05-23 brainstorm) and reused by all later phases that touch the DB. Migrations are validated against a local Postgres 16 Docker container in this phase; the cluster-side migration run is deferred to phase 3 (Lambda foundations), which will stand up a migrator Lambda inside the VPC. Subsequent phases (Lambda foundations, Cognito trigger, domain Lambdas, match, chat) all assume this layer is in place. Per-environment wiring authors both dev and prod main.tf; only dev is applied (prod is paused per docs/PROD_CUTOVER.md). Findings from phasebrainstorms/phase-2-data-layer-brainstorm.md drove the changes from the 2026-05-21 draft.

stories:
  - id: 2.1
    title: Aurora cluster Terraform module
    agent: backenddeveloper
    done: true
    tracking_issue: 14
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/aurora/main.tf creates an aws_rds_cluster with engine "aurora-postgresql", engine_version pinned to "16.4", auto_minor_version_upgrade true, engine_mode default (Serverless v2), serverlessv2_scaling_configuration min/max ACU configurable by variable (dev defaults min_acu=0.5, max_acu=2.0; prod defaults min_acu=1.0, max_acu=8.0), storage_encrypted true, deletion_protection true in prod and false in dev (variable-driven), skip_final_snapshot true in dev and false in prod (variable-driven), apply_immediately true in dev and false in prod (variable-driven), backup_retention_period 7 in dev and 30 in prod
      - manage_master_user_password is true (Aurora generates and rotates the master credential in AWS Secrets Manager; the secret is not stored in Terraform state)
      - publicly_accessible is false; vpc_security_group_ids references the Aurora SG from phase 1; db_subnet_group_name references the subnet group from phase 1
      - enabled_cloudwatch_logs_exports includes "postgresql"
      - An aws_rds_cluster_parameter_group exists for family "aurora-postgresql16" and is wired to the cluster via db_cluster_parameter_group_name (no shared_preload_libraries entries are required for vector, pg_trgm, or pgcrypto — those are CREATE EXTENSION extensions loaded by migrations in story 2.3, not preloaded libraries)
      - An aws_rds_cluster_instance of type "db.serverless" is created in the cluster
      - Module outputs cluster_endpoint, reader_endpoint, port, master_user_secret_arn (sensitive; the ARN of the Secrets Manager secret managed by Aurora), cluster_resource_id, database_name
    notes: ""

  - id: 2.2
    title: Schema migration tooling (yoyo-migrations)
    agent: backenddeveloper
    done: true
    tracking_issue: 15
    depends_on: []
    acceptance_criteria:
      - yoyo-migrations is the migration runner; raw .sql migrations live under infrastructure/db/migrations/ as numbered files (e.g., 0001_enable_extensions.sql, 0002_create_users.sql)
      - A pyproject.toml or requirements.txt under infrastructure/db/ pins yoyo-migrations and psycopg2-binary so a developer can `pip install -r infrastructure/db/requirements.txt` and use `yoyo apply` and `yoyo list`
      - A README under infrastructure/db/ documents (a) the migration file naming convention, (b) how to run `yoyo apply` and `yoyo list` against the local Postgres container, (c) the deferred cluster-side flow (a migrator Lambda introduced in phase 3 will run yoyo against dev/prod Aurora — phase 2 does not run yoyo against AWS)
      - A docker-compose.yml under infrastructure/db/ launches a Postgres 16 container with the vector and pg_trgm extensions available (image pgvector/pgvector:pg16 or equivalent) for local testing
      - Running `yoyo apply` against the docker-compose Postgres container exits zero, then `yoyo list` shows all migrations in the "applied" state with zero pending
      - A `down`/rollback file accompanies each up migration where reversal is meaningful; running rollback against the local container removes the schema cleanly
    notes: ""

  - id: 2.3
    title: users table migration with extensions and indexes
    agent: backenddeveloper
    done: true
    tracking_issue: 16
    depends_on: [2.2]
    acceptance_criteria:
      - First migration enables extensions vector, pg_trgm, pgcrypto
      - Second migration creates the users table exactly as specified in §5.1 of architecture.md, including the GENERATED ALWAYS AS age column, the email format CHECK, and indexes idx_users_sex_country_religion, idx_users_age, idx_users_prefs_gin, idx_users_vector (hnsw)
      - Running the migration against the local Postgres container leaves "\d users" matching the architecture spec and "\di users*" reporting all four indexes
      - A rollback migration "down" cleanly drops the table and extensions in reverse order
    notes: ""

  - id: 2.4
    title: siblings table migration
    agent: backenddeveloper
    done: true
    tracking_issue: 17
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates the siblings table per §5.1 with ON DELETE CASCADE to users(user_id) and the idx_siblings_user index
      - Deleting a users row from the local Postgres container removes any siblings rows for that user
    notes: ""

  - id: 2.5
    title: friendships and friend_requests tables
    agent: backenddeveloper
    done: true
    tracking_issue: 18
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates friendships with the (user_a, user_b) primary key, the CHECK (user_a < user_b) constraint, both FKs ON DELETE CASCADE, and the idx_friendships_b index
      - Migration creates friend_requests with the status CHECK constraint, the UNIQUE (from_user_id, to_user_id, status), idx_fr_to_pending, idx_fr_from_pending
      - Inserting a row violating user_a < user_b is rejected with a constraint error in the test
    notes: ""

  - id: 2.6
    title: bookmarks and blocks tables
    agent: backenddeveloper
    done: true
    tracking_issue: 19
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates bookmarks (composite PK, both FKs CASCADE, idx_bookmarks_target) per §5.1
      - Migration creates blocks (composite PK, both FKs CASCADE, idx_blocks_blocked) per §5.1
    notes: ""

  - id: 2.7
    title: Row-Level Security policy for gender visibility
    agent: backenddeveloper
    done: false
    tracking_issue: 20
    depends_on: [2.3]
    acceptance_criteria:
      - A migration creates a dedicated non-superuser, non-BYPASSRLS application role (e.g., `app_user`) with SELECT/INSERT/UPDATE on users (and other §5.1 tables, scoped as needed). This role is the role under which Lambda will connect in phase 3 and is the role the RLS integration tests must use — connecting as the master would bypass RLS and make the tests theater.
      - Migration runs ALTER TABLE users ENABLE ROW LEVEL SECURITY, ALTER TABLE users FORCE ROW LEVEL SECURITY, and creates the policy users_opposite_sex_only per §5.2 that allows a row when sex differs from current_setting('app.requesting_user_sex', true) OR the user_id equals current_setting('app.requesting_user_id', true)::uuid
      - Integration test connects as the app_user role (NOT the master), seeds one Male and one Female user (using SET ROLE postgres or a separate setup connection for the inserts), then with SET LOCAL app.requesting_user_id and app.requesting_user_sex configured for the Male user, asserts SELECT FROM users returns exactly the Male's own row plus the Female row
      - Integration test asserts that when the GUCs are NOT set, SELECT FROM users (as the app_user role) returns zero rows — failing closed is the intended behavior, since Lambda is required to always set the GUCs before querying. (This replaces the previous draft criterion which incorrectly asserted that a user could still see their own row with no GUC set — three-valued SQL logic returns NULL → row filtered, not true.)
    notes: ""

  - id: 2.8
    title: Immutable-fields trigger on users
    agent: backenddeveloper
    done: false
    tracking_issue: 21
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates the enforce_immutable_fields trigger function and the trg_users_immutable BEFORE UPDATE trigger per §5.7 of architecture.md
      - Integration test inserts a user and then UPDATE users SET first_name='X' WHERE user_id=...  → the statement raises "Attempted to modify immutable field"
      - Integration test UPDATE users SET job_title='Y' WHERE user_id=... succeeds
    notes: ""

  - id: 2.9
    title: deck_view materialized view and refresh function
    agent: backenddeveloper
    done: false
    tracking_issue: 22
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates the deck_view materialized view exactly as specified in §5.1 of architecture.md with the unique index idx_deck_user
      - A SQL function refresh_deck_view() is created that issues REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view
      - Integration test inserts a user with profile_complete_verified=true and deleted_at NULL, runs refresh_deck_view(), then SELECT FROM deck_view returns that user
    notes: ""

  - id: 2.10
    title: DynamoDB ChatRooms and ChatRoomMembership tables
    agent: backenddeveloper
    done: false
    tracking_issue: 23
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/dynamodb/main.tf creates aws_dynamodb_table for ChatRooms (PK room_id string, billing_mode PAY_PER_REQUEST, point_in_time_recovery enabled in prod and disabled in dev via variable)
      - aws_dynamodb_table for ChatRoomMembership (PK user_id string, SK room_id string, billing_mode PAY_PER_REQUEST)
      - Both tables have server_side_encryption enabled and tags for Environment and Project
      - Both tables have deletion_protection_enabled set from a variable (true in prod, false in dev) — this is the native DynamoDB protection that prevents `terraform destroy` and console deletes from succeeding against prod data. (Note: Terraform's `lifecycle.prevent_destroy` cannot reference variables, so it is intentionally not used here; relying on the native flag keeps dev tear-down simple while keeping prod safe.)
      - terraform plan shows zero changes after a second apply
    notes: ""

  - id: 2.11
    title: DynamoDB ChatMessages and MessageReads tables with stream
    agent: backenddeveloper
    done: false
    tracking_issue: 24
    depends_on: [2.10]
    acceptance_criteria:
      - ChatMessages table (PK room_id string, SK created_at_message_id sortable string per architecture §5.4) has stream_enabled true with stream_view_type NEW_IMAGE, billing_mode PAY_PER_REQUEST
      - MessageReads table (PK room_id, SK user_id) created with PAY_PER_REQUEST
      - Both tables have server_side_encryption enabled, deletion_protection_enabled variable-driven (true in prod, false in dev), and tags for Environment and Project
      - Point_in_time_recovery is enabled in prod and disabled in dev (variable)
      - The DynamoDB stream ARN of ChatMessages is exposed as a module output named chat_messages_stream_arn
    notes: ""

  - id: 2.12
    title: DynamoDB Notifications table with stream and UnreadIndex GSI
    agent: backenddeveloper
    done: false
    tracking_issue: 25
    depends_on: [2.10]
    acceptance_criteria:
      - Notifications table (PK user_id string, SK created_at_notification_id sortable string per architecture §5.4) created with billing_mode PAY_PER_REQUEST and stream_enabled true (stream_view_type NEW_IMAGE)
      - Server_side_encryption enabled, deletion_protection_enabled variable-driven (true in prod, false in dev), tags for Environment and Project
      - Point_in_time_recovery enabled in prod, disabled in dev (variable)
      - A GSI named UnreadIndex is declared with PK user_id and SK notification_id. Sparsity is application-enforced — writers attach the SK attribute only when read=false and remove it when flipping read=true; Terraform only declares the keys and projection. The PRD-level rationale (write-pattern, not a Terraform attribute) is documented in a code comment on the GSI block.
      - TTL is enabled on attribute "ttl"
      - Stream ARN exposed as module output notifications_stream_arn
    notes: ""

  - id: 2.13
    title: DynamoDB PushNotificationTokens table
    agent: backenddeveloper
    done: false
    tracking_issue: 26
    depends_on: [2.10]
    acceptance_criteria:
      - PushNotificationTokens table (PK user_id string, SK device_id string) created with billing_mode PAY_PER_REQUEST and server_side_encryption enabled
      - Deletion_protection_enabled variable-driven (true in prod, false in dev), tags for Environment and Project
      - Module output push_tokens_table_name returns the table name
    notes: ""

  - id: 2.14
    title: Per-environment data-layer wiring and Terraform tests
    agent: backenddeveloper
    done: false
    tracking_issue: 27
    depends_on: [2.1, 2.10, 2.11, 2.12, 2.13]
    acceptance_criteria:
      - Both infrastructure/environments/dev/main.tf and prod/main.tf instantiate the aurora and dynamodb modules wired to the networking outputs from phase 1 (vpc_id, private_subnet_ids via the db_subnet_group_name, aurora_security_group_id). Both environments are authored so `terraform plan` against either env runs cleanly in CI; only the dev environment is applied. Prod apply remains gated per docs/PROD_CUTOVER.md and the existing deploy.yml gate from phase 1.
      - dev.tfvars sets aurora min_acu=0.5, max_acu=2.0, deletion_protection=false, skip_final_snapshot=true, apply_immediately=true, backup_retention_period=7, dynamodb point_in_time_recovery=false, deletion_protection_enabled=false
      - prod.tfvars sets aurora min_acu=1.0, max_acu=8.0, deletion_protection=true, skip_final_snapshot=false, apply_immediately=false, backup_retention_period=30, dynamodb point_in_time_recovery=true, deletion_protection_enabled=true
      - Terraform test (.tftest.hcl) for the aurora module asserts publicly_accessible=false, storage_encrypted=true, manage_master_user_password=true, engine_version="16.4", and that the security group reference equals the input
      - Terraform test for the dynamodb module asserts billing_mode=PAY_PER_REQUEST on every table and that ChatMessages and Notifications have stream_enabled=true
      - `terraform test` exits zero from infrastructure/modules/aurora/ and infrastructure/modules/dynamodb/
      - `terraform plan` against infrastructure/environments/dev/ produces the expected resources (cluster, instance, parameter group, 6 DynamoDB tables) with no errors. The actual `terraform apply` against dev is performed via the deploy.yml pipeline on PR merge into development.
      - Cluster-side migration execution is OUT OF SCOPE for this phase. Migrations are validated against the local docker-compose Postgres container in story 2.2; running yoyo against the dev Aurora cluster is the first acceptance criterion of phase 3 (Lambda foundations), which will introduce a migrator Lambda inside the VPC.
    notes: ""
