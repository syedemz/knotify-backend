# knotify_obs

Thin observability wrapper module for Knotify Lambda functions.

## Public API

### `init_logger(service: str) -> Logger`

Returns an `aws_lambda_powertools.Logger` instance bound to the given service
name.  The Lambda module (story 3.1) injects `POWERTOOLS_SERVICE_NAME` and
`LOG_LEVEL` environment variables automatically, so in production you can
call `init_logger(os.environ["POWERTOOLS_SERVICE_NAME"])` at module load time.

```python
from knotify_obs import init_logger

logger = init_logger("knotify-post-confirmation")
logger.info("User confirmed", extra={"user_id": sub})
```

### `correlation_id_middleware`

Callable decorator that injects the HTTP API Gateway request ID as a
correlation ID into every log record emitted during the invocation.  Wrap
your handler with this decorator so distributed traces link across service
calls.

```python
from knotify_obs import correlation_id_middleware

@correlation_id_middleware
def lambda_handler(event, context):
    ...
```

### `verify_cognito_jwt(token, user_pool_id, region, *, audience=None) -> dict`

**Rarely used in v1.** The HTTP API Cognito JWT authorizer (phase 5) handles
routine validation natively — business Lambdas receive pre-verified claims via
`event.requestContext.authorizer.jwt.claims` and do not need to re-verify the
token themselves.

This helper exists for **Lambda authorizers** or other niche paths that may
emerge in **phase 11 hardening** (e.g. a custom authorizer that needs to
validate a token before the API Gateway layer is involved, or a WebSocket
route that does not go through the HTTP API).  Do not add it to business
Lambda handlers without a concrete requirement.

```python
from knotify_obs import verify_cognito_jwt

try:
    claims = verify_cognito_jwt(
        token,
        user_pool_id="eu-central-1_AbCdEfGhI",
        region="eu-central-1",
        audience="your-app-client-id",
    )
except Exception as exc:
    # Token is invalid — reject the request.
    raise
```

JWKS keys are fetched from the Cognito public endpoint on every call.  If
this helper is ever used in a hot path, add an in-memory JWKS cache (the
Powertools JWTAuthorizer helper provides one).

## Layer contents

The layer packages:
- `aws-lambda-powertools==3.29.0`
- `PyJWT[crypto]==2.13.0`  (includes `cryptography` for RS256 verification)
- `requests==2.32.3`
- `knotify_obs/` (this module)

Compatible runtimes: `python3.14`  
Compatible architectures: `arm64`

## Building

```bash
cd infrastructure/src/layers/observability
bash build.sh
```

The output is `knotify-observability-layer.zip`.  Terraform reads this file
via `aws_lambda_layer_version.observability` (see `main.tf` in this
directory's parent Terraform resource file).
