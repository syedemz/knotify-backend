phase: 2
title: Data layer (Aurora + DynamoDB)
last_updated: 2026-05-21

context_summary: |
  Provisions the entire persistent data layer. Aurora Serverless v2 cluster (Postgres 16 with vector, pg_trgm, pgcrypto extensions), the full relational schema from §5.1 of architecture.md (users, siblings, friendships, friend_requests, bookmarks, blocks) with the immutable-fields trigger, the RLS gender-isolation policy, and the deck_view materialized view per the owner's resolved §13 #4. DynamoDB tables (ChatRooms, ChatRoomMembership, ChatMessages with Streams enabled, MessageReads, Notifications with Streams enabled and the UnreadIndex GSI, PushNotificationTokens). Schema migration tooling is selected here and reused by all later phases that touch the DB. Subsequent phases (Lambda foundations, Cognito trigger, domain Lambdas, match, chat) all assume this layer is in place.

stories:
  - id: 2.1
    title: Aurora cluster Terraform module
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/aurora/main.tf creates an aws_rds_cluster with engine "aurora-postgresql", engine_version "16.x", engine_mode default (Serverless v2), serverlessv2_scaling_configuration min/max ACU configurable by variable, storage_encrypted true, deletion_protection true in prod (variable-driven), backup_retention_period 7 in dev and 30 in prod
      - PubliclyAccessible is false, vpc_security_group_ids references the Aurora SG from phase 1, db_subnet_group_name references the subnet group from phase 1
      - An aws_rds_cluster_parameter_group enables shared_preload_libraries containing the values needed for vector and pg_trgm
      - An aws_rds_cluster_instance of type "db.serverless" is created
      - Module outputs cluster_endpoint, port, master_username, master_password (sensitive)
    notes: ""

  - id: 2.2
    title: Schema migration tooling
    agent: backenddeveloper
    done: false
    depends_on: []
    acceptance_criteria:
      - A migration runner is chosen (sqitch, Flyway, Alembic, or yoyo) and added under infrastructure/db/migrations/
      - A README under infrastructure/db/ documents how to run "migrate up" and "migrate status" against a local Postgres container and against an Aurora endpoint
      - A docker-compose file or Makefile target launches a Postgres 16 container with the vector and pg_trgm extensions for local testing
      - Running "migrate up" against the local Postgres container exits zero with no pending migrations
    notes: ""

  - id: 2.3
    title: users table migration with extensions and indexes
    agent: backenddeveloper
    done: false
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
    done: false
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates the siblings table per §5.1 with ON DELETE CASCADE to users(user_id) and the idx_siblings_user index
      - Deleting a users row from the local Postgres container removes any siblings rows for that user
    notes: ""

  - id: 2.5
    title: friendships and friend_requests tables
    agent: backenddeveloper
    done: false
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates friendships with the (user_a, user_b) primary key, the CHECK (user_a < user_b) constraint, both FKs ON DELETE CASCADE, and the idx_friendships_b index
      - Migration creates friend_requests with the status CHECK constraint, the UNIQUE (from_user_id, to_user_id, status), idx_fr_to_pending, idx_fr_from_pending
      - Inserting a row violating user_a < user_b is rejected with a constraint error in the test
    notes: ""

  - id: 2.6
    title: bookmarks and blocks tables
    agent: backenddeveloper
    done: false
    depends_on: [2.3]
    acceptance_criteria:
      - Migration creates bookmarks (composite PK, both FKs CASCADE, idx_bookmarks_target) per §5.1
      - Migration creates blocks (composite PK, both FKs CASCADE, idx_blocks_blocked) per §5.1
    notes: ""

  - id: 2.7
    title: Row-Level Security policy for gender visibility
    agent: backenddeveloper
    done: false
    depends_on: [2.3]
    acceptance_criteria:
      - Migration runs ALTER TABLE users ENABLE ROW LEVEL SECURITY and creates the policy users_opposite_sex_only per §5.2 that allows a row when sex differs from current_setting("app.requesting_user_sex", true) OR the user_id equals the requesting user
      - Integration test seeds one Male and one Female user, then with SET LOCAL app.requesting_user_id and app.requesting_user_sex configured for the Male user, asserts SELECT FROM users returns exactly the Male's own row plus the Female row
      - Integration test asserts that with no GUC set, the Male user can still see his own row (current_setting fallback to NULL is handled)
    notes: ""

  - id: 2.8
    title: Immutable-fields trigger on users
    agent: backenddeveloper
    done: false
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
    depends_on: []
    acceptance_criteria:
      - File infrastructure/modules/dynamodb/main.tf creates aws_dynamodb_table for ChatRooms (PK room_id string, billing_mode PAY_PER_REQUEST, point_in_time_recovery enabled by variable for prod)
      - aws_dynamodb_table for ChatRoomMembership (PK user_id string, SK room_id string, billing_mode PAY_PER_REQUEST)
      - Both tables have server_side_encryption enabled and tags for Environment and Project
      - terraform plan shows zero changes after a second apply
    notes: ""

  - id: 2.11
    title: DynamoDB ChatMessages and MessageReads tables with stream
    agent: backenddeveloper
    done: false
    depends_on: [2.10]
    acceptance_criteria:
      - ChatMessages table (PK room_id, SK sortable string) has stream_enabled true with stream_view_type NEW_IMAGE
      - MessageReads table (PK room_id, SK user_id) created with PAY_PER_REQUEST
      - The DynamoDB stream ARN of ChatMessages is exposed as a module output named chat_messages_stream_arn
    notes: ""

  - id: 2.12
    title: DynamoDB Notifications table with stream and UnreadIndex GSI
    agent: backenddeveloper
    done: false
    depends_on: [2.10]
    acceptance_criteria:
      - Notifications table (PK user_id, SK sortable) created with PAY_PER_REQUEST and stream_enabled true (NEW_IMAGE)
      - A sparse GSI named UnreadIndex projects only items where read=false; the GSI uses PK user_id and SK notification_id
      - TTL is enabled on attribute "ttl"
      - Stream ARN exposed as notifications_stream_arn
    notes: ""

  - id: 2.13
    title: DynamoDB PushNotificationTokens table
    agent: backenddeveloper
    done: false
    depends_on: [2.10]
    acceptance_criteria:
      - PushNotificationTokens table (PK user_id, SK device_id) created with PAY_PER_REQUEST and SSE
      - Module output push_tokens_table_name returns the table name
    notes: ""

  - id: 2.14
    title: Per-environment data-layer wiring and Terraform tests
    agent: backenddeveloper
    done: false
    depends_on: [2.1, 2.10, 2.11, 2.12, 2.13]
    acceptance_criteria:
      - Both infrastructure/environments/dev/main.tf and prod/main.tf instantiate the aurora and dynamodb modules wired to the networking outputs from phase 1
      - Terraform test (.tftest.hcl) for the aurora module asserts publicly_accessible=false, storage_encrypted=true, and that the security group reference equals the input
      - Terraform test for the dynamodb module asserts billing_mode=PAY_PER_REQUEST on every table and that ChatMessages and Notifications have stream_enabled=true
      - "terraform test" exits zero from infrastructure/modules/aurora/ and infrastructure/modules/dynamodb/
      - A dev apply followed by running all migrations against the dev Aurora cluster completes and "\d users" matches the spec
    notes: ""
