"""
Phase 7 live probe — happy-path /v1/match/search + /v1/match/deck.

Creates two opposite-sex test users via Cognito, completes both profiles via
PATCH /v1/profile/me with all 34 required fields, refreshes the requester's
JWT so the new custom:profile_complete claim propagates, invokes the
refresh_deck_view Lambda synchronously, then exercises both match endpoints.

Run from project root:
    python scripts/probe_phase7.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import uuid

import boto3
import requests


REGION = "eu-central-1"
USER_POOL_ID = "eu-central-1_YnzRDU4EZ"
CLIENT_ID = "213tgsm22heuvf49gpdoef096m"  # knotify-dev-integration-test
DOMAIN = "d3sfhc4jobfipj.cloudfront.net"
EDGE_SECRET = "kUIOgUn7pJIoj9jVeZrTb1uEw8aRE245NquhLT9w0Cmf8MY0fCSwtY0TXvP6Irbn"
REFRESH_LAMBDA = "knotify-refresh-deck-view-dev"


def _http_with_retry(method: str, url: str, *, headers, json_body=None, attempts=4):
    last_exc = None
    for i in range(attempts):
        try:
            return requests.request(method, url, headers=headers, json=json_body, timeout=30)
        except requests.exceptions.ConnectionError as exc:
            last_exc = exc
            wait = 2 ** i
            print(f"    retry {i+1}/{attempts} after connection error: {exc.__class__.__name__} (sleep {wait}s)")
            time.sleep(wait)
    raise last_exc


def _headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "x-knotify-edge-secret": EDGE_SECRET,
        "Content-Type": "application/json",
    }


def _decode(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _build_profile_body(sex: str, username: str) -> dict:
    """All 34 required-for-completion fields + preferences for vector."""
    body = {
        # immutable identity
        "first_name": "Probe",
        "last_name": "User",
        "sex": sex,
        "birthday": "2000-01-01",
        "religion": "Islam",
        "subsect": "Sunni",
        # mutable
        "username": username,
        "religious_level": "Practising",
        "current_residence_city": "London",
        "current_residence_country": "United Kingdom",
        "resident_country_code": "GB",
        "district": "Westminster",
        "education_level": "Bachelors",
        "highest_degree": "BSc Computer Science",
        "high_school": "London High School",
        "higher_secondary": "London Higher Secondary",
        "college_name": "King's College London",
        "job_title": "Software Engineer",
        "employer_name": "TestCo",
        "employment_type": "Full-time",
        "office_address": "1 Test St, London",
        "professional_category": "Technology",
        "salary_range": "50000-75000",
        "fathers_name": "Father Name",
        "fathers_job": "Engineer",
        "father_retired": False,
        "mothers_name": "Mother Name",
        "mothers_job": "Teacher",
        "mother_retired": False,
        "family_residence_address": "2 Family Rd, London",
        "marital_status": "Single",
        "has_children": False,
        "move_abroad": True,
        "relation": "Self",
        # extras
        "preferences": {"travel": True, "cooking": True},
    }
    return body


def _signup(cognito, email: str, password: str) -> str:
    resp = cognito.sign_up(ClientId=CLIENT_ID, Username=email, Password=password)
    sub = resp["UserSub"]
    cognito.admin_confirm_sign_up(UserPoolId=USER_POOL_ID, Username=email)
    return sub


def _signin(cognito, email: str, password: str) -> dict:
    resp = cognito.admin_initiate_auth(
        UserPoolId=USER_POOL_ID,
        ClientId=CLIENT_ID,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    return resp["AuthenticationResult"]


def _refresh(cognito, refresh_token: str) -> dict:
    resp = cognito.initiate_auth(
        ClientId=CLIENT_ID,
        AuthFlow="REFRESH_TOKEN_AUTH",
        AuthParameters={"REFRESH_TOKEN": refresh_token},
    )
    return resp["AuthenticationResult"]


def _patch_profile(token: str, body: dict) -> requests.Response:
    return _http_with_retry(
        "PATCH",
        f"https://{DOMAIN}/v1/profile/me",
        headers=_headers(token),
        json_body=body,
    )


def _warm_aurora(lam) -> None:
    # Dev Aurora runs at min_acu=0 and pauses to zero compute when idle. The
    # first PATCH after idle pays a 10–20s wake-from-paused tax, which (with
    # the Cognito admin call and async lambda.invoke layered on top) blows
    # the profile Lambda's 30s timeout on the completion flip. Invoking the
    # refresh Lambda synchronously here forces Aurora awake before the probe
    # touches the PATCH path.
    print("[0] warm Aurora via refresh_deck_view invoke (sync)")
    t0 = time.time()
    resp = lam.invoke(FunctionName=REFRESH_LAMBDA, InvocationType="RequestResponse")
    dt = time.time() - t0
    err = resp.get("FunctionError")
    print(f"    StatusCode={resp['StatusCode']} FunctionError={err} elapsed={dt:.1f}s")
    if err:
        payload = resp["Payload"].read().decode()
        print(f"    payload: {payload[:300]}")
        raise RuntimeError(f"Aurora warm-up failed: {err}")


def _delete_cognito(cognito, username: str) -> None:
    """Delete by sub (canonical Username in pools using sub as alias)."""
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=username)
    except cognito.exceptions.UserNotFoundException:
        pass
    except Exception as exc:
        print(f"  teardown warn ({username}): {exc}", file=sys.stderr)


def main() -> int:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)

    run_id = uuid.uuid4().hex[:8]
    req_email = f"knotify-probe+req{run_id}@example.com"
    req_pw = f"Kn0tify!Probe#{run_id}"
    cand_email = f"knotify-probe+cand{run_id}@example.com"
    cand_pw = f"Kn0tify!Probe#{run_id}"

    print(f"== Phase 7 probe — run {run_id} ==")
    print(f"requester: {req_email}")
    print(f"candidate: {cand_email}")

    req_sub = cand_sub = None
    try:
        _warm_aurora(lam)

        # --- candidate first (so requester's deck pulls her in)
        print("\n[1] sign up + confirm candidate (Female)")
        cand_sub = _signup(cognito, cand_email, cand_pw)
        cand_tokens = _signin(cognito, cand_email, cand_pw)
        print(f"    cand sub={cand_sub}")

        print("[2] PATCH candidate profile (34 fields + preferences)")
        r = _patch_profile(
            cand_tokens["AccessToken"],
            _build_profile_body("Female", f"cand_{run_id}"),
        )
        print(f"    HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"    body: {r.text[:500]}")
            return 1

        # --- requester
        print("\n[3] sign up + confirm requester (Male)")
        req_sub = _signup(cognito, req_email, req_pw)
        req_tokens = _signin(cognito, req_email, req_pw)
        print(f"    req sub={req_sub}")

        print("[4] verify gate CLOSED — POST /v1/match/search before completion")
        r = _http_with_retry(
            "POST",
            f"https://{DOMAIN}/v1/match/search",
            headers=_headers(req_tokens["AccessToken"]),
            json_body={},
        )
        print(f"    HTTP {r.status_code}  body={r.text[:200]}")
        if r.status_code != 403:
            print(f"    !! expected 403, got {r.status_code}")

        print("[5] PATCH requester profile (34 fields + preferences)")
        r = _patch_profile(
            req_tokens["AccessToken"],
            _build_profile_body("Male", f"req_{run_id}"),
        )
        print(f"    HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"    body: {r.text[:500]}")
            return 1

        print("[6] refresh requester JWT (REFRESH_TOKEN_AUTH)")
        fresh = _refresh(cognito, req_tokens["RefreshToken"])
        fresh_token = fresh["AccessToken"]
        claims = _decode(fresh_token)
        pc = claims.get("custom:profile_complete")
        print(f"    custom:profile_complete = {pc!r}")
        if pc != "true":
            print("    !! claim did not flip to 'true'")
            return 1

        print("[7] invoke refresh_deck_view Lambda (synchronous)")
        # let cognito attribute propagate briefly
        time.sleep(2)
        resp = lam.invoke(FunctionName=REFRESH_LAMBDA, InvocationType="RequestResponse")
        print(f"    StatusCode={resp['StatusCode']} FunctionError={resp.get('FunctionError')}")
        payload = resp["Payload"].read().decode()
        print(f"    payload: {payload[:300]}")

        print("\n[8] POST /v1/match/search (happy path)")
        r = _http_with_retry(
            "POST",
            f"https://{DOMAIN}/v1/match/search",
            headers=_headers(fresh_token),
            json_body={"countries": ["GB"], "religion": "Islam", "age_min": 18, "age_max": 60},
        )
        print(f"    HTTP {r.status_code}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        results = body.get("results", [])
        print(f"    results count: {len(results)}")
        if results:
            for row in results[:5]:
                print(f"      - {row.get('user_id')} sex={row.get('sex')} username={row.get('username')}")
            cand_in_search = any(row.get("user_id") == cand_sub for row in results)
            print(f"    candidate present? {cand_in_search}")
        else:
            print(f"    body: {json.dumps(body)[:400]}")

        print("\n[9] GET /v1/match/deck (happy path)")
        r = _http_with_retry(
            "GET",
            f"https://{DOMAIN}/v1/match/deck",
            headers=_headers(fresh_token),
        )
        print(f"    HTTP {r.status_code}")
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text}
        results = body.get("results", [])
        cursor = body.get("next_cursor")
        print(f"    results count: {len(results)}  next_cursor={cursor}")
        if results:
            for row in results[:5]:
                print(f"      - {row.get('user_id')} sex={row.get('sex')} username={row.get('username')}")
            cand_in_deck = any(row.get("user_id") == cand_sub for row in results)
            print(f"    candidate present? {cand_in_deck}")
        else:
            print(f"    body: {json.dumps(body)[:400]}")

        print("\n== probe complete ==")
        return 0

    finally:
        print("\n[teardown] deleting Cognito users (Aurora rows remain — VPC-private)")
        # Try by email first (preferred_username), then by sub as fallback.
        for ident in (req_email, req_sub):
            if ident:
                _delete_cognito(cognito, ident)
        for ident in (cand_email, cand_sub):
            if ident:
                _delete_cognito(cognito, ident)


if __name__ == "__main__":
    sys.exit(main())
