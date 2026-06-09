# knotify-observability-layer

Thin Lambda layer wrapping AWS Lambda Powertools for Python. Provides
structured logging, a correlation-ID middleware, Cognito JWT verification,
and (since story 5.6) the edge-secret guard.

## Public API

### `init_logger(service: str) -> Logger`

Returns an `aws_lambda_powertools.Logger` bound to `service`. Reads
`POWERTOOLS_SERVICE_NAME` and `LOG_LEVEL` from the environment; pass an
explicit name to override.

### `correlation_id_middleware`

Callable decorator (Powertools `correlation_id_logger_handler`). Apply to
a Lambda handler to inject the HTTP API request ID into every log record
for that invocation.

### `verify_cognito_jwt(token, user_pool_id, region, *, audience=None)`

Verifies a raw Cognito JWT outside of the HTTP API Gateway authorizer path.
Returns the decoded claims dict on success; raises on any validation failure.
Not needed for ordinary HTTP API routes — the built-in JWT authorizer handles
those. Useful for Lambda authorizers or background-process verification.

---

## Edge-secret guard (story 5.6)

CloudFront injects `x-knotify-edge-secret` on every forwarded request.
Lambdas behind the HTTP API Gateway use this guard to reject traffic that
hits the raw `execute-api` endpoint directly, bypassing CloudFront.

### `EdgeSecretRequired`

Exception raised by `require_edge_secret` when validation fails.

### `@with_edge_secret` — canonical usage for Lambda handlers

Apply as a decorator on every Lambda handler function that must enforce the
edge secret. The decorator calls `require_edge_secret(event)` before the
handler body runs. If validation fails it returns a 403 response immediately;
the handler body is never executed. Any exception other than
`EdgeSecretRequired` propagates unchanged.

```python
import json
from knotify_obs import with_edge_secret

@with_edge_secret
def handler(event, context):
    # By the time execution reaches here, the edge secret has been validated.
    return {"statusCode": 200, "body": json.dumps({"ok": True})}
```

The 403 response shape is fixed across the fleet:

```json
{
  "statusCode": 403,
  "headers": {"Content-Type": "application/json"},
  "body": "{\"error\": \"forbidden\"}"
}
```

### `require_edge_secret(event)` — non-handler use only

Call this directly only from non-handler code paths (utility scripts, custom
invocation wrappers). For Lambda handlers always use `@with_edge_secret`.
Inline `try/except EdgeSecretRequired` inside a handler is an anti-pattern —
it duplicates the 403 translation and risks inconsistent error shapes across
the fleet.

```python
from knotify_obs import require_edge_secret, EdgeSecretRequired

# Only in non-handler call sites:
try:
    require_edge_secret(event)
except EdgeSecretRequired:
    ...
```

---

## Terraform — wiring the edge secret into a Lambda

Every Lambda that applies `@with_edge_secret` must have `EDGE_SECRET`
injected from the CloudFront module output. In the env-level `module` call:

```hcl
module "my_function" {
  source = "../../modules/lambda"
  ...
  environment_variables = {
    EDGE_SECRET = module.cloudfront.edge_secret
  }
}
```

The `edge_secret` output is marked `sensitive = true` in
`infrastructure/modules/cloudfront/outputs.tf`; Terraform redacts it from
plan/apply output automatically.

---

## Integration test environment file

`terraform apply` in `infrastructure/environments/dev/` writes a
`infrastructure/src/tests/integration/.env.test` file (via
`resource "local_file" "integration_test_env"`). That file contains:

```
EXECUTE_API_ENDPOINT=https://<id>.execute-api.eu-central-1.amazonaws.com
DISTRIBUTION_DOMAIN_NAME=d<id>.cloudfront.net
EDGE_SECRET=<64-char-alphanumeric>
```

This file is in `.gitignore` and must never be committed. Phase-5.7 and
later integration tests load it via `python-dotenv` or a simple parser.
