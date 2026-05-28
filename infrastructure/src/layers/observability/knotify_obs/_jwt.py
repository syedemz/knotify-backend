"""Cognito JWT verification helper.

NOTE: This helper is rarely used in v1.  See module docstring in __init__.py.
"""

from __future__ import annotations

import json
from typing import Any

import jwt
import requests
from jwt.algorithms import RSAAlgorithm


def verify_cognito_jwt(
    token: str,
    user_pool_id: str,
    region: str,
    *,
    audience: str | None = None,
) -> dict[str, Any]:
    """Verify a Cognito-issued JWT and return the decoded claims.

    Fetches the Cognito JWKS endpoint for the given User Pool to obtain the
    public key corresponding to the token's `kid` header.  The JWKS fetch is
    performed via `requests.get` so tests can mock it at that boundary without
    any network access.

    Args:
        token:        The raw JWT string.
        user_pool_id: Cognito User Pool ID (e.g. "eu-central-1_AbCdEfGhI").
        region:       AWS region of the User Pool (e.g. "eu-central-1").
        audience:     Expected `aud` claim value.  When None, audience
                      verification is skipped (matches Cognito access tokens
                      which carry `client_id` instead of `aud`).

    Returns:
        Decoded claims dict on success.

    Raises:
        jwt.exceptions.InvalidTokenError (or a subclass) on any verification
        failure: wrong issuer, wrong audience, expired token, bad signature,
        unknown key, etc.
    """
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{user_pool_id}"
    jwks_url = f"{issuer}/.well-known/jwks.json"

    # Fetch the JWKS.  raise_for_status() surfaces HTTP errors as exceptions.
    response = requests.get(jwks_url)
    response.raise_for_status()
    jwks = response.json()

    # Decode the header to find which key to use.
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")

    # Find the matching key in the JWKS.
    matching_key = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid),
        None,
    )
    if matching_key is None:
        raise jwt.exceptions.InvalidKeyError(
            f"No key with kid={kid!r} found in JWKS for {user_pool_id}"
        )

    public_key = RSAAlgorithm.from_jwk(json.dumps(matching_key))

    decode_options: dict[str, Any] = {}
    if audience is None:
        # When no audience is supplied, skip audience verification.
        decode_options["verify_aud"] = False

    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        issuer=issuer,
        audience=audience,
        options=decode_options,
    )
