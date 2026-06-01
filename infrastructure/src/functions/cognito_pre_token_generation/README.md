# cognito_pre_token_generation

Cognito PreTokenGeneration V2 Lambda trigger (story 4.4).

## Purpose

Embeds the `custom:profile_complete` claim on both the ID token and the access
token on every token issuance (sign-in and token refresh). This is layer 2 of
the §13a profile-completion enforcement model — API endpoints and AppSync
resolvers check this claim to gate access to features that require a completed
profile.

## Trigger contract (V2 shape)

Supported trigger sources:
- `TokenGeneration_Authentication` — fired at sign-in
- `TokenGeneration_RefreshTokens` — fired at token refresh

Both sources share the same V2 event shape. The handler reads the event from
Cognito, queries Aurora, and returns the mutated event with the claim set.

### Request

```json
{
  "version": "2",
  "triggerSource": "TokenGeneration_Authentication",
  "request": {
    "userAttributes": {
      "sub": "<uuid>",
      "email": "user@example.com"
    },
    "scopes": []
  },
  "response": {}
}
```

### Response (mutations applied to `event["response"]`)

```json
{
  "claimsAndScopeOverrideDetails": {
    "idTokenGeneration": {
      "claimsToAddOrOverride": {
        "custom:profile_complete": "true"
      }
    },
    "accessTokenGeneration": {
      "claimsToAddOrOverride": {
        "custom:profile_complete": "true"
      }
    }
  }
}
```

The claim value is the string `"true"` or `"false"` — Cognito custom claims
are always strings.

## Database query

One SELECT per token issuance (Md1 — one DB hit per token issue):

```sql
SELECT profile_complete_verified FROM users WHERE user_id = %s
```

`user_id` is the Cognito `sub` claim, which is stored as the primary key of
the `users` table. The lookup is a primary-key scan (O(1)).

### Cold-start latency (Md1)

The first invocation after a Lambda cold start incurs a 200ms–1s ENI
warm-up penalty while the Lambda's VPC network interface initializes. This is
acceptable for pre-launch volumes. Provisioned concurrency will be considered
in phase 11 if observed latency exceeds the token-issuance SLA.

## Missing-row branch (Md2)

If no `users` row exists for the `sub` (for example, a race condition between
sign-up and PostConfirmation, or a manual delete), the handler:

1. Logs a structured `WARNING` at level `users_row_missing_for_pre_token_generation`.
2. Returns `custom:profile_complete = "false"` on **both** tokens.
3. **Does NOT raise.** An unhandled exception in PreTokenGeneration blocks the
   user's login entirely — fail-open on the claim is safer than blocking login.

## Error handling

Any DB-level exception (connection failure, query error) is caught, logged as
`ERROR pre_token_generation_db_error`, and the claim defaults to `"false"` on
both tokens. The handler never raises.

## Deployment

Deployed via the shared `lambda` Terraform module (phase 3 story 3.1) with:

| Parameter | Value |
|-----------|-------|
| IAM role | `cognito_trigger` (shared with `cognito_post_confirmation` — story 3.4) |
| Layers | `observability_layer` + `db_layer` (stories 3.2, 3.3) |
| VPC subnets | Private subnets from the networking module (phase 1) |
| Security groups | `lambda_security_group_id` (networking module) |
| Env var `DB_SECRET_NAME` | `knotify-<env>-app-user-credential` (friendly name, NOT an ARN) |

The User Pool wiring uses `pre_token_generation_config { lambda_version = "V2_0" }`
inside the pool's `lambda_config` block. The V1 `pre_token_generation` field is
**explicitly forbidden** — it cannot coexist with the V2 config and would
silently downgrade the trigger shape.

Advanced Security Mode must be `AUDIT` or `ENFORCED` on the User Pool (default
is `AUDIT`). This is a hard AWS requirement for V2 PreTokenGeneration triggers.

## Testing

Unit tests (no external dependencies):

```sh
pytest infrastructure/src/functions/cognito_pre_token_generation/tests/test_handler_unit.py -v
```

Integration tests (require docker-compose Postgres container):

```sh
pytest infrastructure/src/functions/cognito_pre_token_generation/tests/ -v -m integration
```

Terraform plan-mode tests (hermetic — no AWS credentials):

```sh
cd infrastructure/modules/cognito
terraform test
```
