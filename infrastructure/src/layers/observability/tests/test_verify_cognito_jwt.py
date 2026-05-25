"""
Unit tests for verify_cognito_jwt helper.

All tests are hermetic: JWKS fetch is mocked at the requests.get boundary
so no real network call is made during the test run.

Story 3.2 AC: "Unit test verify_cognito_jwt rejects a token with wrong issuer,
wrong audience, expired exp, and accepts a known-good signed token."
"""

import json
import time
import unittest
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend

# ---------------------------------------------------------------------------
# Helpers — generate an RSA key pair used to sign test tokens and produce
# a mock JWKS response.  All keys are generated once per module load and
# reused across tests for speed.
# ---------------------------------------------------------------------------

_PRIVATE_KEY = rsa.generate_private_key(
    public_exponent=65537,
    key_size=2048,
    backend=default_backend(),
)
_PUBLIC_KEY = _PRIVATE_KEY.public_key()

_KID = "test-key-id-1"
_USER_POOL_ID = "eu-central-1_TestPool123"
_REGION = "eu-central-1"
_CLIENT_ID = "test-client-id-abc"
_ISSUER = f"https://cognito-idp.{_REGION}.amazonaws.com/{_USER_POOL_ID}"


def _make_jwks_response() -> dict:
    """Return a minimal JWKS dict with the test public key."""
    import base64

    # _PUBLIC_KEY is already an RSAPublicKey — call public_numbers() directly.
    pub_numbers = _PUBLIC_KEY.public_numbers()

    def _int_to_base64url(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        data = n.to_bytes(length, "big")
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": _KID,
                "use": "sig",
                "alg": "RS256",
                "n": _int_to_base64url(pub_numbers.n),
                "e": _int_to_base64url(pub_numbers.e),
            }
        ]
    }


def _sign_token(
    *,
    issuer: str = _ISSUER,
    audience: str = _CLIENT_ID,
    exp_offset: int = 3600,
    kid: str = _KID,
    extra_claims: dict | None = None,
) -> str:
    """Sign a JWT with the test private key."""
    now = int(time.time())
    payload = {
        "iss": issuer,
        "aud": audience,
        "sub": "user-sub-uuid-1234",
        "iat": now,
        "exp": now + exp_offset,
        "token_use": "id",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(
        payload,
        _PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": kid},
    )


def _mock_requests_get(url: str, *args, **kwargs):
    """Fake requests.get that returns the test JWKS for any JWKS URL."""
    mock_response = MagicMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = _make_jwks_response()
    return mock_response


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


class TestVerifyCognitoJwtRejectsWrongIssuer(unittest.TestCase):
    """
    Given a token signed with the correct key,
    when the issuer claim does not match the expected Cognito issuer URL,
    then verify_cognito_jwt raises an exception.
    """

    @patch("requests.get", side_effect=_mock_requests_get)
    def test_given_wrong_issuer_when_verify_then_raises(self, mock_get):
        from knotify_obs import verify_cognito_jwt

        token = _sign_token(issuer="https://evil.example.com/wrong-pool")

        with self.assertRaises(Exception):
            verify_cognito_jwt(token, _USER_POOL_ID, _REGION)


class TestVerifyCognitoJwtRejectsWrongAudience(unittest.TestCase):
    """
    Given a token signed with the correct key and correct issuer,
    when the audience claim does not match the expected client_id,
    then verify_cognito_jwt raises an exception.
    """

    @patch("requests.get", side_effect=_mock_requests_get)
    def test_given_wrong_audience_when_verify_then_raises(self, mock_get):
        from knotify_obs import verify_cognito_jwt

        token = _sign_token(audience="wrong-client-id-xyz")

        with self.assertRaises(Exception):
            verify_cognito_jwt(token, _USER_POOL_ID, _REGION, audience=_CLIENT_ID)


class TestVerifyCognitoJwtRejectsExpiredToken(unittest.TestCase):
    """
    Given a token whose exp claim is in the past,
    when verify_cognito_jwt is called,
    then it raises an exception indicating the token has expired.
    """

    @patch("requests.get", side_effect=_mock_requests_get)
    def test_given_expired_token_when_verify_then_raises(self, mock_get):
        from knotify_obs import verify_cognito_jwt

        token = _sign_token(exp_offset=-1)  # expired 1 second ago

        with self.assertRaises(Exception):
            verify_cognito_jwt(token, _USER_POOL_ID, _REGION)


class TestVerifyCognitoJwtAcceptsKnownGoodToken(unittest.TestCase):
    """
    Given a valid token (correct issuer, audience, non-expired, signed by the
    known key) and a JWKS endpoint returning the matching public key,
    when verify_cognito_jwt is called,
    then it returns the decoded claims dict without raising.
    """

    @patch("requests.get", side_effect=_mock_requests_get)
    def test_given_valid_token_when_verify_then_returns_claims(self, mock_get):
        from knotify_obs import verify_cognito_jwt

        token = _sign_token()

        claims = verify_cognito_jwt(token, _USER_POOL_ID, _REGION, audience=_CLIENT_ID)

        self.assertIsInstance(claims, dict)
        self.assertEqual(claims["iss"], _ISSUER)
        self.assertEqual(claims["sub"], "user-sub-uuid-1234")


if __name__ == "__main__":
    unittest.main()
