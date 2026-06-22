"""
Phase 9 live probe — closes the three coverage gaps left by probe_phase8.py:

  1. AppSync subscription over WebSocket: Kate subscribes to
     onMessageInRoom(roomA) via the realtime endpoint; John sends a
     mutation; verify the subscription delivers the Message payload
     in real time.

  2. Friend-request decline path: Jack -> Kate friend request, Kate
     POST /v1/friend-requests/{id}/decline; verify 200 + declined:true,
     and that the request no longer appears in Kate's inbox.

  3. Aurora row-level state after soft delete: Jack soft-deletes
     (purge_immediately=false), then Aurora Data API SELECT confirms
     the §11.1 step-2 redaction landed (deleted_at NOT NULL, sentinels
     on email/username/first_name/last_name, PII columns NULLed).

3 users created via the same flow as probe_phase8.py (sign_up +
admin_confirm_sign_up + admin_initiate_auth). No admin_create_user.

Run from project root:
    python scripts/probe_phase9.py
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
import urllib.parse
import uuid

import boto3
import requests
import websockets


REGION = "eu-central-1"
USER_POOL_ID = "eu-central-1_MO0oZRQ1b"
CLIENT_ID = "353v3brvscce7bnkh7dphsnd4p"  # knotify-dev-integration-test
DOMAIN = "d3ocejjf37kge5.cloudfront.net"
EDGE_SECRET = "35hX4kxDlB4vcatyhqtKYapRIs3PeULfCAUmvTg1dHu9MJeS0ThMF5y51m371vXt"
REFRESH_LAMBDA = "knotify-refresh-deck-view-dev"
APPSYNC_API_NAME = "knotify-dev-chat-api"
AURORA_CLUSTER_ARN = "arn:aws:rds:eu-central-1:776217504626:cluster:knotify-dev-aurora"
AURORA_SECRET_ARN = (
    "arn:aws:secretsmanager:eu-central-1:776217504626:"
    "secret:knotify-dev-app-user-credential-CYdd9g"
)
AURORA_DB = "knotify"
DELETION_POLL_MAX_ATTEMPTS = 30
DELETION_POLL_SLEEP_SECS = 10
SUBSCRIPTION_RECEIVE_TIMEOUT = 30  # seconds to wait for the pushed Message


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

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


def _rest_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "x-knotify-edge-secret": EDGE_SECRET,
        "Content-Type": "application/json",
    }


def _appsync_headers(token: str) -> dict:
    return {"authorization": token, "Content-Type": "application/json"}


def _decode_jwt(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


# ---------------------------------------------------------------------------
# Cognito + profile bootstrap
# ---------------------------------------------------------------------------

def _build_profile_body(sex: str, first_name: str, last_name: str, username: str) -> dict:
    return {
        "first_name": first_name,
        "last_name": last_name,
        "sex": sex,
        "birthday": "2000-01-01",
        "religion": "Islam",
        "subsect": "Sunni",
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
        "preferences": {"travel": True, "cooking": True},
    }


def _signup(cognito, email: str, password: str) -> str:
    resp = cognito.sign_up(ClientId=CLIENT_ID, Username=email, Password=password)
    cognito.admin_confirm_sign_up(UserPoolId=USER_POOL_ID, Username=email)
    return resp["UserSub"]


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
        headers=_rest_headers(token),
        json_body=body,
    )


def _signup_and_complete(
    cognito, email: str, password: str,
    sex: str, first_name: str, last_name: str, username: str,
) -> tuple[str, str, str]:
    sub = _signup(cognito, email, password)
    tokens = _signin(cognito, email, password)
    r = _patch_profile(tokens["AccessToken"], _build_profile_body(sex, first_name, last_name, username))
    if r.status_code != 200:
        raise RuntimeError(f"PATCH /v1/profile/me for {email} -> HTTP {r.status_code} body={r.text[:300]}")
    fresh = _refresh(cognito, tokens["RefreshToken"])
    claims = _decode_jwt(fresh["AccessToken"])
    if claims.get("custom:profile_complete") != "true":
        raise RuntimeError(f"profile_complete claim did not flip for {email}: {claims.get('custom:profile_complete')!r}")
    return sub, fresh["AccessToken"], tokens["RefreshToken"]


def _delete_cognito(cognito, identity: str) -> None:
    try:
        cognito.admin_delete_user(UserPoolId=USER_POOL_ID, Username=identity)
    except cognito.exceptions.UserNotFoundException:
        pass
    except Exception as exc:
        print(f"  teardown warn ({identity}): {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Aurora warm-up — refresh_deck_view; retry through Serverless v2 cold-start
# ---------------------------------------------------------------------------

def _warm_aurora(lam) -> None:
    print("[0] warm Aurora via refresh_deck_view invoke (sync)")
    for i in range(1, 6):
        t0 = time.time()
        resp = lam.invoke(FunctionName=REFRESH_LAMBDA, InvocationType="RequestResponse")
        dt = time.time() - t0
        err = resp.get("FunctionError")
        print(f"    attempt {i}/5 StatusCode={resp['StatusCode']} FunctionError={err} elapsed={dt:.1f}s")
        if not err:
            return
        print(f"    payload: {resp['Payload'].read().decode()[:300]}")
        if i == 5:
            raise RuntimeError("Aurora warm-up exhausted retries")
        time.sleep(15)


# ---------------------------------------------------------------------------
# AppSync GraphQL helpers (HTTP)
# ---------------------------------------------------------------------------

def _discover_appsync_uris(appsync) -> tuple[str, str, str]:
    """Return (graphql_uri, realtime_uri, host_for_subscription_headers)."""
    next_token = None
    while True:
        kwargs = {"maxResults": 25}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = appsync.list_graphql_apis(**kwargs)
        for api in resp.get("graphqlApis", []):
            if api["name"] == APPSYNC_API_NAME:
                graphql = api["uris"]["GRAPHQL"]
                realtime = api["uris"]["REALTIME"]
                host = graphql.replace("https://", "").rstrip("/").removesuffix("/graphql")
                return graphql, realtime, host
        next_token = resp.get("nextToken")
        if not next_token:
            break
    raise RuntimeError(f"AppSync API '{APPSYNC_API_NAME}' not found")


def _gql(url: str, token: str, query: str, variables: dict | None = None) -> dict:
    body = {"query": query, "variables": variables or {}}
    r = _http_with_retry("POST", url, headers=_appsync_headers(token), json_body=body)
    if r.status_code != 200:
        raise RuntimeError(f"AppSync HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


CREATE_OR_GET_ROOM = """
mutation CreateOrGetRoom($otherUserId: ID!) {
  createOrGetRoom(otherUserId: $otherUserId) {
    roomId
    userA
    userB
    status
    friendshipActive
  }
}
"""

SEND_MESSAGE = """
mutation SendMessage($roomId: ID!, $content: String!) {
  sendMessage(roomId: $roomId, content: $content) {
    messageId
    roomId
    senderId
    content
    contentType
    createdAt
  }
}
"""

ON_MESSAGE_IN_ROOM = """
subscription OnMessageInRoom($roomId: ID!) {
  onMessageInRoom(roomId: $roomId) {
    messageId
    roomId
    senderId
    content
    contentType
    createdAt
  }
}
"""


# ---------------------------------------------------------------------------
# REST helpers — friends + deletion
# ---------------------------------------------------------------------------

def _post_friend_request(token: str, target_sub: str) -> dict:
    r = _http_with_retry(
        "POST",
        f"https://{DOMAIN}/v1/friend-requests",
        headers=_rest_headers(token),
        json_body={"toUserId": target_sub},
    )
    if r.status_code != 201:
        raise RuntimeError(f"POST /v1/friend-requests -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json()


def _get_friend_requests(token: str) -> list[dict]:
    r = _http_with_retry("GET", f"https://{DOMAIN}/v1/friend-requests", headers=_rest_headers(token))
    if r.status_code != 200:
        raise RuntimeError(f"GET /v1/friend-requests -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json().get("friend_requests", [])


def _accept_friend_request(token: str, request_id: str) -> dict:
    r = _http_with_retry(
        "POST",
        f"https://{DOMAIN}/v1/friend-requests/{request_id}/accept",
        headers=_rest_headers(token),
    )
    if r.status_code != 200:
        raise RuntimeError(f"accept -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json()


def _decline_friend_request(token: str, request_id: str) -> tuple[int, dict]:
    r = _http_with_retry(
        "POST",
        f"https://{DOMAIN}/v1/friend-requests/{request_id}/decline",
        headers=_rest_headers(token),
    )
    try:
        body = r.json() if r.text else {}
    except ValueError:
        body = {"_raw": r.text[:300]}
    return r.status_code, body


def _initiate_deletion(token: str, *, purge_immediately: bool) -> str:
    r = _http_with_retry(
        "DELETE",
        f"https://{DOMAIN}/v1/profile/me",
        headers=_rest_headers(token),
        json_body={"purge_immediately": purge_immediately},
    )
    if r.status_code != 202:
        raise RuntimeError(f"DELETE /v1/profile/me (purge={purge_immediately}) -> HTTP {r.status_code} body={r.text[:300]}")
    arn = r.json().get("executionArn")
    if not arn:
        raise RuntimeError(f"missing executionArn: {r.text[:300]}")
    return arn


def _poll_deletion_status(token: str, execution_arn: str) -> str:
    terminal = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
    last = "UNKNOWN"
    for attempt in range(1, DELETION_POLL_MAX_ATTEMPTS + 1):
        r = _http_with_retry(
            "GET",
            f"https://{DOMAIN}/v1/profile/me/deletion-status?executionArn={execution_arn}",
            headers=_rest_headers(token),
        )
        if r.status_code != 200:
            raise RuntimeError(f"GET deletion-status -> HTTP {r.status_code} body={r.text[:300]}")
        last = r.json().get("status", "UNKNOWN")
        if last in terminal:
            return last
        print(f"    poll {attempt}/{DELETION_POLL_MAX_ATTEMPTS}: status={last} — sleep {DELETION_POLL_SLEEP_SECS}s")
        time.sleep(DELETION_POLL_SLEEP_SECS)
    raise RuntimeError(f"deletion did not reach terminal state; last={last}")


# ---------------------------------------------------------------------------
# Aurora Data API
# ---------------------------------------------------------------------------

def _select_user_row(rds_data, user_id: str) -> dict | None:
    """Return the `users` row for user_id formatted as {col: value} (or None).

    Note: the `users` table has FORCE ROW LEVEL SECURITY. The SELECT policy
    (`users_opposite_sex_only`) requires GUC `app.requesting_user_id` to match.
    We wrap the SELECT in a transaction with SET LOCAL so the GUC is in scope.
    """
    txn = rds_data.begin_transaction(
        resourceArn=AURORA_CLUSTER_ARN,
        secretArn=AURORA_SECRET_ARN,
        database=AURORA_DB,
    )
    txn_id = txn["transactionId"]
    try:
        rds_data.execute_statement(
            resourceArn=AURORA_CLUSTER_ARN,
            secretArn=AURORA_SECRET_ARN,
            database=AURORA_DB,
            transactionId=txn_id,
            sql=f"SET LOCAL app.requesting_user_id = '{user_id}'",
        )
        resp = rds_data.execute_statement(
            resourceArn=AURORA_CLUSTER_ARN,
            secretArn=AURORA_SECRET_ARN,
            database=AURORA_DB,
            transactionId=txn_id,
            sql=(
                "SELECT user_id::text AS user_id, "
                "deleted_at::text AS deleted_at, email, username, "
                "first_name, last_name, phone_number, photo_url, "
                "chosen_profile_avatar, preferences::text AS preferences, "
                "preference_vector IS NULL AS preference_vector_is_null "
                "FROM users WHERE user_id = CAST(:uid AS uuid)"
            ),
            parameters=[{"name": "uid", "value": {"stringValue": user_id}}],
            includeResultMetadata=True,
        )
    finally:
        rds_data.commit_transaction(
            resourceArn=AURORA_CLUSTER_ARN,
            secretArn=AURORA_SECRET_ARN,
            transactionId=txn_id,
        )
    records = resp.get("records") or []
    if not records:
        return None
    cols = [c["name"] for c in resp.get("columnMetadata", [])]
    row = records[0]
    out: dict = {}
    for col, cell in zip(cols, row):
        if "isNull" in cell and cell["isNull"]:
            out[col] = None
        elif "stringValue" in cell:
            out[col] = cell["stringValue"]
        elif "booleanValue" in cell:
            out[col] = cell["booleanValue"]
        elif "longValue" in cell:
            out[col] = cell["longValue"]
        else:
            out[col] = cell
    return out


# ---------------------------------------------------------------------------
# AppSync subscription over WebSocket (realtime endpoint)
# ---------------------------------------------------------------------------

async def _subscribe_and_collect(
    realtime_uri: str,
    host: str,
    token: str,
    *,
    room_id: str,
    ready_event: asyncio.Event,
    result_holder: dict,
) -> None:
    """
    Open an AppSync realtime WebSocket, subscribe to onMessageInRoom(roomId),
    signal `ready_event` once the start_ack arrives, then capture the first
    `data` message into result_holder["payload"].
    """
    header_dict = {"Authorization": token, "host": host}
    # AppSync expects standard (padded) base64 for the header query param; the
    # `=` characters must be URL-encoded so they aren't treated as kv separators.
    header_b64 = base64.b64encode(json.dumps(header_dict).encode()).decode()
    payload_b64 = base64.b64encode(b"{}").decode()
    connect_url = (
        f"{realtime_uri}"
        f"?header={urllib.parse.quote(header_b64, safe='')}"
        f"&payload={urllib.parse.quote(payload_b64, safe='')}"
    )

    async with websockets.connect(
        connect_url, subprotocols=["graphql-ws"], max_size=2**20
    ) as ws:
        await ws.send(json.dumps({"type": "connection_init"}))

        # Wait for connection_ack
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            if msg.get("type") == "connection_ack":
                break
            if msg.get("type") == "connection_error":
                raise RuntimeError(f"connection_error: {msg}")

        # Send `start` for the subscription
        sub_id = str(uuid.uuid4())
        start_payload = {
            "id": sub_id,
            "type": "start",
            "payload": {
                "data": json.dumps({
                    "query": ON_MESSAGE_IN_ROOM,
                    "variables": {"roomId": room_id},
                }),
                "extensions": {
                    "authorization": {"Authorization": token, "host": host}
                },
            },
        }
        await ws.send(json.dumps(start_payload))

        # Wait for start_ack (id matches sub_id, type=="start_ack")
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "start_ack" and msg.get("id") == sub_id:
                ready_event.set()
                break
            if mtype == "error":
                raise RuntimeError(f"subscription error: {msg}")

        # Now wait for the data event
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=SUBSCRIPTION_RECEIVE_TIMEOUT)
        except asyncio.TimeoutError:
            result_holder["payload"] = None
            return
        msg = json.loads(raw)
        if msg.get("type") == "data" and msg.get("id") == sub_id:
            result_holder["payload"] = msg.get("payload", {}).get("data", {}).get("onMessageInRoom")
        else:
            result_holder["payload"] = msg

        # Best-effort stop + close
        try:
            await ws.send(json.dumps({"id": sub_id, "type": "stop"}))
        except Exception:
            pass


async def _run_subscription_test(
    *,
    realtime_uri: str,
    host: str,
    kate_token: str,
    john_token: str,
    graphql_url: str,
    room_id: str,
    content: str,
) -> dict | None:
    """Open Kate's subscription, send John's mutation once ready, collect."""
    ready = asyncio.Event()
    holder: dict = {}

    sub_task = asyncio.create_task(
        _subscribe_and_collect(
            realtime_uri, host, kate_token,
            room_id=room_id, ready_event=ready, result_holder=holder,
        )
    )

    # Wait for subscription to be active before publishing the mutation.
    await asyncio.wait_for(ready.wait(), timeout=20)
    # Tiny buffer for AppSync to register the subscription server-side.
    await asyncio.sleep(0.5)

    # Send the mutation off the main thread loop via run_in_executor.
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        lambda: _gql(graphql_url, john_token, SEND_MESSAGE, {"roomId": room_id, "content": content}),
    )

    await sub_task
    return holder.get("payload")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    appsync = boto3.client("appsync", region_name=REGION)
    rds_data = boto3.client("rds-data", region_name=REGION)

    run_id = uuid.uuid4().hex[:8]
    kate_email = f"knotify-p9+kate{run_id}@example.com"
    john_email = f"knotify-p9+john{run_id}@example.com"
    jack_email = f"knotify-p9+jack{run_id}@example.com"
    password = f"Kn0tify!Probe9#{run_id}"

    print(f"== Phase 9 probe — run {run_id} ==")
    print(f"kate:  {kate_email}")
    print(f"john:  {john_email}")
    print(f"jack:  {jack_email}")

    kate_sub = john_sub = jack_sub = None
    kate_tok = john_tok = jack_tok = None
    failures: list[str] = []

    try:
        _warm_aurora(lam)

        print("\n[1] Discover AppSync URIs (GraphQL + realtime)")
        graphql_url, realtime_uri, appsync_host = _discover_appsync_uris(appsync)
        print(f"    graphql={graphql_url}")
        print(f"    realtime={realtime_uri}")
        print(f"    host={appsync_host}")

        print("\n[2] Sign up + complete all three profiles")
        kate_sub, kate_tok, _ = _signup_and_complete(
            cognito, kate_email, password, "Female", "Kate", "Doe", f"kate_p9_{run_id}"
        )
        print(f"    kate sub={kate_sub}")
        john_sub, john_tok, _ = _signup_and_complete(
            cognito, john_email, password, "Male", "John", "Doe", f"john_p9_{run_id}"
        )
        print(f"    john sub={john_sub}")
        jack_sub, jack_tok, _ = _signup_and_complete(
            cognito, jack_email, password, "Male", "Jack", "Ryan", f"jack_p9_{run_id}"
        )
        print(f"    jack sub={jack_sub}")

        # -------------------------------------------------------------
        # Friend-request decline path
        # -------------------------------------------------------------
        print("\n[3] Jack -> Kate friend request, Kate DECLINES")
        jack_req = _post_friend_request(jack_tok, kate_sub)
        jack_req_id = jack_req.get("request_id") or jack_req.get("id")
        print(f"    request_id={jack_req_id}")

        inbox_before = _get_friend_requests(kate_tok)
        ids_before = {(r.get("request_id") or r.get("id")) for r in inbox_before}
        print(f"    kate inbox before decline: {len(inbox_before)} request(s) — contains? {jack_req_id in ids_before}")
        if jack_req_id not in ids_before:
            failures.append("decline pre-check: Jack's request not in Kate's inbox before decline")

        status, decline_body = _decline_friend_request(kate_tok, jack_req_id)
        print(f"    decline HTTP {status} body={json.dumps(decline_body)[:200]}")
        if status != 200:
            failures.append(f"decline: expected HTTP 200, got {status}")
        if not decline_body.get("declined"):
            failures.append(f"decline: body missing declined=true: {decline_body!r}")
        if decline_body.get("request_id") != jack_req_id:
            failures.append(f"decline: body request_id mismatch: {decline_body!r}")

        inbox_after = _get_friend_requests(kate_tok)
        ids_after = {(r.get("request_id") or r.get("id")) for r in inbox_after}
        print(f"    kate inbox after decline: {len(inbox_after)} request(s) — contains? {jack_req_id in ids_after}")
        if jack_req_id in ids_after:
            failures.append("decline post-check: declined request still in Kate's inbox")

        # -------------------------------------------------------------
        # John <-> Kate friendship + room (prep for subscription test)
        # -------------------------------------------------------------
        print("\n[4] John -> Kate friend request, Kate accepts, room A created")
        john_req = _post_friend_request(john_tok, kate_sub)
        john_req_id = john_req.get("request_id") or john_req.get("id")
        _accept_friend_request(kate_tok, john_req_id)
        time.sleep(2)  # let friendship_active propagate

        resp = _gql(graphql_url, john_tok, CREATE_OR_GET_ROOM, {"otherUserId": kate_sub})
        if resp.get("errors"):
            failures.append(f"createOrGetRoom: {resp['errors']}")
            print(f"    !! errors={resp['errors']}")
            return 1
        room_a = resp["data"]["createOrGetRoom"]
        room_a_id = room_a["roomId"]
        print(f"    roomId={room_a_id} status={room_a['status']} friendshipActive={room_a['friendshipActive']}")

        # -------------------------------------------------------------
        # AppSync subscription over WebSocket
        # -------------------------------------------------------------
        print("\n[5] Kate subscribes onMessageInRoom(roomA) via wss; John sends mutation")
        sub_content = f"ws-ping {run_id}"
        payload: dict | None
        try:
            payload = asyncio.run(_run_subscription_test(
                realtime_uri=realtime_uri,
                host=appsync_host,
                kate_token=kate_tok,
                john_token=john_tok,
                graphql_url=graphql_url,
                room_id=room_a_id,
                content=sub_content,
            ))
        except Exception as exc:
            payload = None
            print(f"    !! subscription raised: {exc}")
            failures.append(f"subscription: raised {exc.__class__.__name__}: {str(exc)[:300]}")
        print(f"    subscription payload: {json.dumps(payload)[:300] if payload else payload!r}")
        if payload is None:
            # Already recorded as failure above (either raised or timed out).
            if not any(f.startswith("subscription:") for f in failures):
                failures.append("subscription: no payload received within timeout — wss push did NOT fire")
        elif not payload:
            failures.append("subscription: no payload received within timeout — wss push did NOT fire")
        elif not isinstance(payload, dict) or payload.get("content") != sub_content:
            failures.append(
                f"subscription: payload content mismatch — got {payload!r}, expected content={sub_content!r}"
            )
        elif payload.get("senderId") != john_sub:
            failures.append(
                f"subscription: senderId mismatch — got {payload.get('senderId')!r}, expected {john_sub!r}"
            )
        elif payload.get("roomId") != room_a_id:
            failures.append(
                f"subscription: roomId mismatch — got {payload.get('roomId')!r}, expected {room_a_id!r}"
            )
        else:
            print("    OK — Kate received John's message in real time over wss")

        # -------------------------------------------------------------
        # Aurora soft-delete row inspection
        # -------------------------------------------------------------
        print("\n[6] Jack soft-deletes (purge_immediately=false)")
        pre = _select_user_row(rds_data, jack_sub)
        print(f"    Aurora users row BEFORE: email={pre and pre.get('email')!r} "
              f"username={pre and pre.get('username')!r} "
              f"first_name={pre and pre.get('first_name')!r} "
              f"deleted_at={pre and pre.get('deleted_at')!r}")
        if not pre:
            failures.append("aurora pre-check: Jack's users row missing before soft-delete")

        arn = _initiate_deletion(jack_tok, purge_immediately=False)
        print(f"    executionArn={arn}")
        status = _poll_deletion_status(jack_tok, arn)
        print(f"    final status={status}")
        if status != "SUCCEEDED":
            failures.append(f"jack deletion not SUCCEEDED: {status}")

        # Aurora updates are synchronous within the SFN task, but we give a
        # brief moment for Data API connection pool warming.
        time.sleep(2)

        post = _select_user_row(rds_data, jack_sub)
        print(f"    Aurora users row AFTER:  {json.dumps(post)[:400] if post else post!r}")
        if not post:
            failures.append("aurora post-check: Jack's users row vanished — soft delete should keep the row")
        else:
            if not post.get("deleted_at"):
                failures.append(f"aurora post-check: deleted_at is NULL — got {post.get('deleted_at')!r}")
            expected_email = f"deleted-{jack_sub}@deleted.knotify.local"
            if post.get("email") != expected_email:
                failures.append(
                    f"aurora post-check: email sentinel mismatch — got {post.get('email')!r}, expected {expected_email!r}"
                )
            expected_username = f"[deleted-{jack_sub}]"
            if post.get("username") != expected_username:
                failures.append(
                    f"aurora post-check: username sentinel mismatch — got {post.get('username')!r}, expected {expected_username!r}"
                )
            if post.get("first_name") != "Deleted":
                failures.append(f"aurora post-check: first_name={post.get('first_name')!r}, expected 'Deleted'")
            if post.get("last_name") != "User":
                failures.append(f"aurora post-check: last_name={post.get('last_name')!r}, expected 'User'")
            for nullable_col in ("phone_number", "photo_url", "chosen_profile_avatar"):
                if post.get(nullable_col) is not None:
                    failures.append(
                        f"aurora post-check: {nullable_col}={post.get(nullable_col)!r}, expected NULL"
                    )
            if post.get("preferences") not in ("{}", None):
                failures.append(f"aurora post-check: preferences={post.get('preferences')!r}, expected '{{}}'")
            if not post.get("preference_vector_is_null"):
                failures.append("aurora post-check: preference_vector should be NULL after soft-delete")

        # ----------------------------------------------------------
        print("\n== summary ==")
        if failures:
            print(f"  FAILURES ({len(failures)}):")
            for f in failures:
                print(f"    - {f}")
            return 1
        print("  all assertions passed")
        return 0

    finally:
        print("\n[teardown] sweep Cognito for any survivors (no-op if Jack's SFN deletion succeeded)")
        for ident in (kate_email, kate_sub, john_email, john_sub, jack_email, jack_sub):
            if ident:
                _delete_cognito(cognito, ident)


if __name__ == "__main__":
    sys.exit(main())
