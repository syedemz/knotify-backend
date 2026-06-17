#!/usr/bin/env python3
"""
backfill_cognito_gender.py — one-shot backfill: Aurora users.sex → Cognito `gender`.

Why this exists
---------------
The pre-token-generation Lambda emits the custom:user_sex JWT claim from the
standard Cognito `gender` attribute. Users created before the
user-sex-jwt-propagation hotfix shipped have users.sex set in Aurora (from
the post-confirmation Lambda's _normalize_gender path or from the first
profile PATCH) but no `gender` attribute on their Cognito record. Their next
JWT refresh would carry no custom:user_sex claim, leaving every domain
Lambda's app.requesting_user_sex GUC empty and silently disabling
opposite-sex filtering on the deck.

This script pulls (user_id, sex) for every active user from Aurora and
pushes `gender` to the Cognito user pool via AdminUpdateUserAttributes. It
is idempotent — Cognito accepts repeat writes of the same value as a no-op.

Usage
-----
    python backfill_cognito_gender.py \
        --db-secret <SecretsManagerArn> \
        --user-pool-id <UserPoolId> \
        [--region eu-central-1] \
        [--dry-run]

The script intentionally runs OUTSIDE Lambda — execute it from a developer
workstation or a CI job with credentials that can read the DB secret and
call cognito-idp:AdminUpdateUserAttributes on the user pool.

Exit codes:
  0 — completed (look at logged counts for what actually changed)
  1 — argument or connection error
  2 — at least one per-user call failed (others may have succeeded)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Iterable

import boto3
import psycopg2
import psycopg2.extras

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_cognito_gender")


_CANONICAL = {
    "Male": "Male",
    "Female": "Female",
    "male": "Male",
    "female": "Female",
    "MALE": "Male",
    "FEMALE": "Female",
    "m": "Male",
    "M": "Male",
    "f": "Female",
    "F": "Female",
}


def _normalize_sex(raw: str | None) -> str | None:
    if raw is None:
        return None
    return _CANONICAL.get(raw.strip())


def _load_db_credentials(secret_arn: str, region: str) -> dict:
    sm = boto3.client("secretsmanager", region_name=region)
    raw = sm.get_secret_value(SecretId=secret_arn)["SecretString"]
    return json.loads(raw)


def _connect(secret: dict) -> psycopg2.extensions.connection:
    import os
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"],
        user=secret["username"],
        password=secret["password"],
        connect_timeout=5,
    )


def _fetch_user_sex(conn) -> Iterable[tuple[str, str]]:
    """
    Yields (user_id, sex) for every active user with a non-NULL sex.

    Bypasses RLS by running as the DB master role — the secret used must
    be the application secret, NOT the read-only role.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT user_id::text, sex
            FROM users
            WHERE deleted_at IS NULL
              AND sex IS NOT NULL
            ORDER BY user_id
            """
        )
        for user_id, sex in cur.fetchall():
            yield user_id, sex


def _push_to_cognito(
    cognito,
    user_pool_id: str,
    user_id: str,
    sex: str,
    dry_run: bool,
) -> bool:
    """Returns True on success, False on failure."""
    if dry_run:
        logger.info("dry_run user_id=%s sex=%s", user_id, sex)
        return True
    try:
        cognito.admin_update_user_attributes(
            UserPoolId=user_pool_id,
            Username=user_id,
            UserAttributes=[{"Name": "gender", "Value": sex}],
        )
        return True
    except Exception as exc:
        logger.warning("failed user_id=%s sex=%s error=%s", user_id, sex, exc)
        return False


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-secret", required=True, help="Secrets Manager ARN for Aurora creds")
    parser.add_argument("--user-pool-id", required=True, help="Cognito user pool id")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--dry-run", action="store_true", help="Log what would be written without calling Cognito")
    args = parser.parse_args(argv[1:])

    logger.info(
        "starting db_secret=%s pool=%s region=%s dry_run=%s",
        args.db_secret,
        args.user_pool_id,
        args.region,
        args.dry_run,
    )

    try:
        secret = _load_db_credentials(args.db_secret, args.region)
    except Exception as exc:
        logger.error("failed to load db secret: %s", exc)
        return 1

    try:
        conn = _connect(secret)
    except Exception as exc:
        logger.error("failed to connect to db: %s", exc)
        return 1

    cognito = boto3.client("cognito-idp", region_name=args.region)

    total = 0
    pushed = 0
    skipped = 0
    failed = 0

    try:
        for user_id, raw_sex in _fetch_user_sex(conn):
            total += 1
            sex = _normalize_sex(raw_sex)
            if sex is None:
                logger.warning("skipping user_id=%s unknown_sex=%s", user_id, raw_sex)
                skipped += 1
                continue
            if _push_to_cognito(cognito, args.user_pool_id, user_id, sex, args.dry_run):
                pushed += 1
            else:
                failed += 1
    finally:
        conn.close()

    logger.info(
        "done total=%d pushed=%d skipped=%d failed=%d dry_run=%s",
        total, pushed, skipped, failed, args.dry_run,
    )

    return 2 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
