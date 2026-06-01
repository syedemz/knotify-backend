"""
pytest configuration for top-level E2E integration tests.

These tests run against the live dev AWS environment — they are NOT
docker-compose tests.  They require:
  - A deployed dev Cognito User Pool (stories 4.1–4.5)
  - A deployed dev Aurora cluster with migrations applied (phase 3)
  - AWS credentials in the execution environment with permissions to:
      cognito-idp: sign_up, admin_confirm_sign_up, admin_initiate_auth,
                   admin_delete_user
      secretsmanager: get_secret_value (aurora master secret)
      ec2+VPC: the test runner must be able to reach the Aurora writer
               endpoint, or else the Aurora endpoint must be temporarily
               reachable from the test runner (bastion / VPN / SSM tunnel)

Required environment variables (no defaults — missing values cause an
immediate, clear error at collection time via the fixture in cognito_signup_test.py):

  COGNITO_USER_POOL_ID           — e.g. eu-central-1_abc123
  COGNITO_INTEGRATION_TEST_CLIENT_ID  — the dev-only client (story 4.2)
  AURORA_HOST                    — writer endpoint of the dev cluster
  AURORA_PORT                    — normally 5432
  AURORA_DBNAME                  — normally "knotify"
  AURORA_MASTER_SECRET_ARN       — ARN of the Aurora-managed master secret
  AWS_REGION                     — e.g. eu-central-1

Run with:
    pytest infrastructure/src/tests/integration/ -v -m integration

Skip in any environment that lacks live AWS access:
    pytest -m "not integration"
"""
