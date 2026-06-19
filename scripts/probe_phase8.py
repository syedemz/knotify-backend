"""
Phase 8 live probe — end-to-end chat (AppSync + DynamoDB Streams + push fan-out).

Scenario:
  - Sign up 3 users: Kate Doe (Female), John Doe (Male), Jack Ryan (Male)
  - Complete all 3 profiles
  - John -> Kate friend request, Kate accepts -> friendship_active=true (story 8.9b)
  - Jack -> Kate friend request, Kate accepts -> friendship_active=true
  - John createOrGetRoom with Kate -> room A
  - Jack createOrGetRoom with Kate -> room B
  - John sendMessage(roomA): "Hello Babe , Send some pictures"
  - Kate sendMessage(roomA): "no we dont know each other yet"
  - Verify both messages present in roomA via messagesByChatRoom
  - Verify ZERO of those two messages leak into roomB (room isolation)
  - Kate blocks John  -> roomA status=deactivated, friendshipActive=false (story 8.9a),
                        friendship deleted (story 8.9 blocks Lambda extension)
  - Kate unfriends Jack -> roomB friendshipActive=false; room NOT deleted
                          (DELETE /v1/friends/{userId} only flips the flag per
                          friends handler line 398-401).

Run from project root:
    python scripts/probe_phase8.py
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
USER_POOL_ID = "eu-central-1_An4jEkG7t"
CLIENT_ID = "ugvgg3o1ldqt01df7j0artse1"  # knotify-dev-integration-test
DOMAIN = "d19o999v109625.cloudfront.net"
EDGE_SECRET = "v4td8wA2j1IlUy9RfAgi0W46aC5ZrIJYCltHWGTGDUSQXeERbhYoKPETGeFF5d8W"
REFRESH_LAMBDA = "knotify-refresh-deck-view-dev"
APPSYNC_API_NAME = "knotify-dev-chat-api"


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
    # AppSync with AMAZON_COGNITO_USER_POOLS auth expects the raw JWT in the
    # `authorization` header (no "Bearer " prefix).
    return {
        "authorization": token,
        "Content-Type": "application/json",
    }


def _decode(token: str) -> dict:
    payload = token.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))


def _build_profile_body(sex: str, first_name: str, last_name: str, username: str) -> dict:
    """All 34 required-for-completion fields + preferences for vector."""
    return {
        # immutable identity
        "first_name": first_name,
        "last_name": last_name,
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
        "preferences": {"travel": True, "cooking": True},
    }


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
        headers=_rest_headers(token),
        json_body=body,
    )


def _warm_aurora(lam) -> None:
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


def _discover_appsync_url(appsync) -> str:
    """Look up the AppSync GraphQL endpoint by API name."""
    next_token = None
    while True:
        kwargs = {"maxResults": 25}
        if next_token:
            kwargs["nextToken"] = next_token
        resp = appsync.list_graphql_apis(**kwargs)
        for api in resp.get("graphqlApis", []):
            if api["name"] == APPSYNC_API_NAME:
                return api["uris"]["GRAPHQL"]
        next_token = resp.get("nextToken")
        if not next_token:
            break
    raise RuntimeError(f"AppSync API '{APPSYNC_API_NAME}' not found in region {REGION}")


def _gql(url: str, token: str, query: str, variables: dict | None = None) -> dict:
    body = {"query": query, "variables": variables or {}}
    r = _http_with_retry("POST", url, headers=_appsync_headers(token), json_body=body)
    if r.status_code != 200:
        raise RuntimeError(f"AppSync HTTP {r.status_code}: {r.text[:400]}")
    parsed = r.json()
    if parsed.get("errors"):
        # Don't raise — surface errors so caller can decide whether they were expected
        return parsed
    return parsed


def _signup_and_complete(
    cognito,
    email: str,
    password: str,
    sex: str,
    first_name: str,
    last_name: str,
    username: str,
) -> tuple[str, str, str]:
    """Sign up, confirm, sign in, PATCH profile, refresh JWT.

    Returns (sub, access_token_with_profile_complete_claim, refresh_token).
    """
    sub = _signup(cognito, email, password)
    tokens = _signin(cognito, email, password)
    r = _patch_profile(tokens["AccessToken"], _build_profile_body(sex, first_name, last_name, username))
    if r.status_code != 200:
        raise RuntimeError(f"PATCH /v1/profile/me for {email} -> HTTP {r.status_code} body={r.text[:300]}")
    # Refresh so custom:profile_complete = true lands in the JWT
    fresh = _refresh(cognito, tokens["RefreshToken"])
    claims = _decode(fresh["AccessToken"])
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
# REST helpers — friend request flow
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
        raise RuntimeError(f"POST /v1/friend-requests/{request_id}/accept -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json()


def _get_friends(token: str) -> list[dict]:
    r = _http_with_retry("GET", f"https://{DOMAIN}/v1/friends", headers=_rest_headers(token))
    if r.status_code != 200:
        raise RuntimeError(f"GET /v1/friends -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json().get("friends", [])


def _post_block(token: str, target_sub: str) -> dict:
    r = _http_with_retry(
        "POST",
        f"https://{DOMAIN}/v1/blocks",
        headers=_rest_headers(token),
        json_body={"userId": target_sub},
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST /v1/blocks -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json() if r.text else {}


def _delete_friend(token: str, target_sub: str) -> dict:
    r = _http_with_retry(
        "DELETE",
        f"https://{DOMAIN}/v1/friends/{target_sub}",
        headers=_rest_headers(token),
    )
    if r.status_code != 200:
        raise RuntimeError(f"DELETE /v1/friends/{target_sub} -> HTTP {r.status_code} body={r.text[:300]}")
    return r.json()


# ---------------------------------------------------------------------------
# GraphQL operations
# ---------------------------------------------------------------------------

CREATE_OR_GET_ROOM = """
mutation CreateOrGetRoom($otherUserId: ID!) {
  createOrGetRoom(otherUserId: $otherUserId) {
    roomId
    userA
    userB
    status
    friendshipActive
    createdAt
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

LIST_MY_ROOMS = """
query ListMyRooms {
  listMyRooms {
    roomId
    userA
    userB
    status
    friendshipActive
    deactivatedReason
    deactivatedAt
    deactivatedBy
    lastMessagePreview
  }
}
"""

MESSAGES_BY_ROOM = """
query MessagesByRoom($roomId: ID!, $limit: Int) {
  messagesByChatRoom(roomId: $roomId, limit: $limit) {
    items {
      messageId
      senderId
      content
      contentType
      createdAt
    }
    nextToken
  }
}
"""


# ---------------------------------------------------------------------------
# Main probe
# ---------------------------------------------------------------------------

def main() -> int:
    cognito = boto3.client("cognito-idp", region_name=REGION)
    lam = boto3.client("lambda", region_name=REGION)
    appsync = boto3.client("appsync", region_name=REGION)

    run_id = uuid.uuid4().hex[:8]
    kate_email = f"knotify-probe+kate{run_id}@example.com"
    john_email = f"knotify-probe+john{run_id}@example.com"
    jack_email = f"knotify-probe+jack{run_id}@example.com"
    password = f"Kn0tify!Probe#{run_id}"

    print(f"== Phase 8 probe — run {run_id} ==")
    print(f"kate:  {kate_email}")
    print(f"john:  {john_email}")
    print(f"jack:  {jack_email}")

    kate_sub = john_sub = jack_sub = None
    kate_tok = john_tok = jack_tok = None
    kate_refresh = john_refresh = jack_refresh = None
    failures: list[str] = []

    try:
        _warm_aurora(lam)

        print("\n[1] Discover AppSync GraphQL endpoint")
        graphql_url = _discover_appsync_url(appsync)
        print(f"    {graphql_url}")

        print("\n[2] Sign up + complete profiles for all three users")
        print("    -- Kate (Female)")
        kate_sub, kate_tok, kate_refresh = _signup_and_complete(
            cognito, kate_email, password, "Female", "Kate", "Doe", f"kate_{run_id}"
        )
        print(f"       sub={kate_sub}")
        print("    -- John (Male)")
        john_sub, john_tok, john_refresh = _signup_and_complete(
            cognito, john_email, password, "Male", "John", "Doe", f"john_{run_id}"
        )
        print(f"       sub={john_sub}")
        print("    -- Jack (Male)")
        jack_sub, jack_tok, jack_refresh = _signup_and_complete(
            cognito, jack_email, password, "Male", "Jack", "Ryan", f"jack_{run_id}"
        )
        print(f"       sub={jack_sub}")

        print("\n[3] John -> Kate friend request")
        john_req = _post_friend_request(john_tok, kate_sub)
        john_req_id = john_req.get("request_id") or john_req.get("id")
        print(f"    request_id={john_req_id}  raw={json.dumps(john_req)[:200]}")

        print("[4] Jack -> Kate friend request")
        jack_req = _post_friend_request(jack_tok, kate_sub)
        jack_req_id = jack_req.get("request_id") or jack_req.get("id")
        print(f"    request_id={jack_req_id}  raw={json.dumps(jack_req)[:200]}")

        print("[5] Kate lists incoming friend requests")
        incoming = _get_friend_requests(kate_tok)
        print(f"    count={len(incoming)}")
        for row in incoming:
            print(f"      - {row}")

        print("[6] Kate accepts John's request")
        accepted_john = _accept_friend_request(kate_tok, john_req_id)
        print(f"    -> {json.dumps(accepted_john)[:300]}")

        print("[7] Kate accepts Jack's request")
        accepted_jack = _accept_friend_request(kate_tok, jack_req_id)
        print(f"    -> {json.dumps(accepted_jack)[:300]}")

        # Friendship_active flip is async via friends Lambda extension (story 8.9b)
        # — but it's an in-process post-commit call, so a brief wait covers replication.
        print("    sleep 2s for friendship_active to propagate to DDB")
        time.sleep(2)

        print("\n[8] John createOrGetRoom(otherUserId=kate)  -> room A")
        resp = _gql(graphql_url, john_tok, CREATE_OR_GET_ROOM, {"otherUserId": kate_sub})
        if resp.get("errors"):
            failures.append(f"createOrGetRoom (john->kate): {resp['errors']}")
            print(f"    !! errors={resp['errors']}")
            return 1
        room_a = resp["data"]["createOrGetRoom"]
        room_a_id = room_a["roomId"]
        print(f"    roomId={room_a_id} status={room_a['status']} friendshipActive={room_a['friendshipActive']}")

        print("[8b] Idempotency — John calls createOrGetRoom(kate) again; must return same roomId")
        resp = _gql(graphql_url, john_tok, CREATE_OR_GET_ROOM, {"otherUserId": kate_sub})
        if resp.get("errors"):
            failures.append(f"createOrGetRoom idempotency call: {resp['errors']}")
        else:
            room_a_again = resp["data"]["createOrGetRoom"]["roomId"]
            print(f"    second-call roomId={room_a_again}")
            if room_a_again != room_a_id:
                failures.append(f"idempotency broken: 1st={room_a_id} 2nd={room_a_again}")

        print("[9] Jack createOrGetRoom(otherUserId=kate) -> room B")
        resp = _gql(graphql_url, jack_tok, CREATE_OR_GET_ROOM, {"otherUserId": kate_sub})
        if resp.get("errors"):
            failures.append(f"createOrGetRoom (jack->kate): {resp['errors']}")
            print(f"    !! errors={resp['errors']}")
            return 1
        room_b = resp["data"]["createOrGetRoom"]
        room_b_id = room_b["roomId"]
        print(f"    roomId={room_b_id} status={room_b['status']} friendshipActive={room_b['friendshipActive']}")

        if room_a_id == room_b_id:
            failures.append(f"room IDs collide: A=B={room_a_id}")

        print("\n[10] John sends 'Hello Babe , Send some pictures' to Kate (room A)")
        msg1_text = "Hello Babe , Send some pictures"
        resp = _gql(graphql_url, john_tok, SEND_MESSAGE, {"roomId": room_a_id, "content": msg1_text})
        if resp.get("errors"):
            failures.append(f"sendMessage (john): {resp['errors']}")
            print(f"    !! errors={resp['errors']}")
        else:
            m1 = resp["data"]["sendMessage"]
            print(f"    messageId={m1['messageId']} senderId={m1['senderId']}")
            if m1["senderId"] != john_sub:
                failures.append(f"sendMessage server-derived senderId mismatch: got {m1['senderId']} expected {john_sub}")

        print("[11] Kate replies 'no we dont know each other yet' (room A)")
        msg2_text = "no we dont know each other yet"
        resp = _gql(graphql_url, kate_tok, SEND_MESSAGE, {"roomId": room_a_id, "content": msg2_text})
        if resp.get("errors"):
            failures.append(f"sendMessage (kate): {resp['errors']}")
            print(f"    !! errors={resp['errors']}")
        else:
            m2 = resp["data"]["sendMessage"]
            print(f"    messageId={m2['messageId']} senderId={m2['senderId']}")
            if m2["senderId"] != kate_sub:
                failures.append(f"sendMessage server-derived senderId mismatch: got {m2['senderId']} expected {kate_sub}")

        print("\n[12] Kate queries messagesByChatRoom(roomA) — expect both messages present")
        resp = _gql(graphql_url, kate_tok, MESSAGES_BY_ROOM, {"roomId": room_a_id, "limit": 50})
        if resp.get("errors"):
            failures.append(f"messagesByChatRoom (room A): {resp['errors']}")
        else:
            items = resp["data"]["messagesByChatRoom"]["items"]
            print(f"    items={len(items)}")
            for it in items:
                print(f"      - [{it['senderId'][:8]}] {it['content']!r}")
            contents = {it["content"] for it in items}
            if msg1_text not in contents:
                failures.append(f"room A missing John's message: {msg1_text!r}")
            if msg2_text not in contents:
                failures.append(f"room A missing Kate's reply: {msg2_text!r}")

        print("\n[13] Kate queries messagesByChatRoom(roomB) — expect ZERO leakage")
        resp = _gql(graphql_url, kate_tok, MESSAGES_BY_ROOM, {"roomId": room_b_id, "limit": 50})
        if resp.get("errors"):
            failures.append(f"messagesByChatRoom (room B): {resp['errors']}")
        else:
            items = resp["data"]["messagesByChatRoom"]["items"]
            print(f"    items={len(items)}")
            contents = {it["content"] for it in items}
            if msg1_text in contents or msg2_text in contents:
                failures.append(f"room B leaked content from room A: contents={contents!r}")
            else:
                print(f"    OK — no leakage")

        print("\n[14] Kate listMyRooms — expect rooms A and B both active, friendshipActive=true,")
        print("     and room A first (sorted by lastMessageAt desc; only A has messages)")
        resp = _gql(graphql_url, kate_tok, LIST_MY_ROOMS)
        if resp.get("errors"):
            failures.append(f"listMyRooms pre-block: {resp['errors']}")
        else:
            ordered = resp["data"]["listMyRooms"]
            rooms = {r["roomId"]: r for r in ordered}
            print(f"    rooms={len(ordered)}")
            for r in ordered:
                print(f"      - {r['roomId'][:12]}.. status={r['status']} "
                      f"friendshipActive={r['friendshipActive']} lastMsg={r.get('lastMessagePreview')!r}")
            for rid in (room_a_id, room_b_id):
                if rid not in rooms:
                    failures.append(f"pre-block listMyRooms missing roomId={rid}")
                elif rooms[rid]["status"] != "active":
                    failures.append(f"pre-block room {rid} status={rooms[rid]['status']} expected active")
                elif not rooms[rid]["friendshipActive"]:
                    failures.append(f"pre-block room {rid} friendshipActive=false expected true")
            # Ordering: room A should sort before room B because A has messages and B doesn't.
            try:
                idx_a = next(i for i, r in enumerate(ordered) if r["roomId"] == room_a_id)
                idx_b = next(i for i, r in enumerate(ordered) if r["roomId"] == room_b_id)
                if idx_a > idx_b:
                    failures.append(f"listMyRooms ordering: roomA@{idx_a} should precede roomB@{idx_b} "
                                    f"(A has messages, B does not)")
            except StopIteration:
                pass  # already captured as missing-room failure above

        print("\n[15] Kate blocks John  -> expect room A deactivated, friendshipActive=false")
        block_resp = _post_block(kate_tok, john_sub)
        print(f"    POST /v1/blocks -> {json.dumps(block_resp)[:300]}")

        # Block path: friends Lambda extension (8.9) writes to DDB ChatRooms which
        # triggers DDB stream -> room_state_publisher Lambda (8.9a) -> AppSync.
        # Allow up to ~10s for the stream to drain.
        print("    waiting up to 10s for stream propagation")
        time.sleep(5)

        resp = _gql(graphql_url, kate_tok, LIST_MY_ROOMS)
        if resp.get("errors"):
            failures.append(f"listMyRooms post-block: {resp['errors']}")
        else:
            rooms = {r["roomId"]: r for r in resp["data"]["listMyRooms"]}
            ra = rooms.get(room_a_id)
            if not ra:
                failures.append(f"post-block: room A {room_a_id} not in Kate's listMyRooms")
            else:
                print(f"    room A: status={ra['status']} friendshipActive={ra['friendshipActive']} "
                      f"deactivatedReason={ra.get('deactivatedReason')} deactivatedBy={ra.get('deactivatedBy')}")
                if ra["status"] != "deactivated":
                    # Stream may still be in flight; retry once.
                    print("    status not yet deactivated — retrying in 5s")
                    time.sleep(5)
                    resp = _gql(graphql_url, kate_tok, LIST_MY_ROOMS)
                    ra = {r["roomId"]: r for r in resp["data"]["listMyRooms"]}.get(room_a_id, ra)
                    print(f"    room A retry: status={ra['status']} friendshipActive={ra['friendshipActive']}")
                if ra["status"] != "deactivated":
                    failures.append(f"post-block room A status={ra['status']} expected deactivated")
                if ra["friendshipActive"]:
                    failures.append(f"post-block room A friendshipActive=true expected false")

        print("\n[15b] Post-block read-only gate — John tries sendMessage(roomA); expect refusal")
        resp = _gql(graphql_url, john_tok, SEND_MESSAGE, {
            "roomId": room_a_id,
            "content": "should fail — friendship is gone",
        })
        if resp.get("errors"):
            err_str = json.dumps(resp["errors"])[:300]
            print(f"    OK — AppSync refused: {err_str}")
        elif resp.get("data", {}).get("sendMessage"):
            failures.append(f"post-block read-only gate: sendMessage succeeded but should have been refused "
                            f"(messageId={resp['data']['sendMessage'].get('messageId')})")
        else:
            print(f"    OK — no message returned: {json.dumps(resp)[:200]}")

        print("\n[16] Kate GET /v1/friends — expect John gone (block auto-unfriends)")
        friends_after_block = _get_friends(kate_tok)
        friend_ids = {f.get("user_id") or f.get("userId") for f in friends_after_block}
        print(f"    friends={len(friends_after_block)} ids={friend_ids}")
        if john_sub in friend_ids:
            failures.append(f"post-block: John still in Kate's friend list")
        if jack_sub not in friend_ids:
            failures.append(f"post-block: Jack missing from Kate's friend list (should still be friends)")

        print("\n[16b] Re-friend blocked — John POST /v1/friend-requests {toUserId: kate}; expect 409 blocked")
        r = _http_with_retry(
            "POST",
            f"https://{DOMAIN}/v1/friend-requests",
            headers=_rest_headers(john_tok),
            json_body={"toUserId": kate_sub},
        )
        print(f"    HTTP {r.status_code} body={r.text[:200]}")
        if r.status_code != 409:
            failures.append(f"re-friend blocked: expected 409, got {r.status_code} body={r.text[:200]}")
        else:
            try:
                err = r.json().get("error")
                if err != "blocked":
                    failures.append(f"re-friend blocked: expected error='blocked', got {err!r}")
            except ValueError:
                failures.append(f"re-friend blocked: 409 body not JSON: {r.text[:200]}")

        print("\n[17] Kate unfriends Jack (DELETE /v1/friends/{jack_sub})")
        unfriend_resp = _delete_friend(kate_tok, jack_sub)
        print(f"    DELETE /v1/friends/{jack_sub[:8]}.. -> {json.dumps(unfriend_resp)[:200]}")

        print("    sleep 3s for friendship_active=false to propagate to DDB")
        time.sleep(3)

        print("[18] Kate listMyRooms — inspect room B after unfriend")
        resp = _gql(graphql_url, kate_tok, LIST_MY_ROOMS)
        if resp.get("errors"):
            failures.append(f"listMyRooms post-unfriend: {resp['errors']}")
        else:
            rooms = {r["roomId"]: r for r in resp["data"]["listMyRooms"]}
            rb = rooms.get(room_b_id)
            if rb is None:
                print(f"    room B is GONE from listMyRooms (count={len(rooms)})")
                # Per friends handler (line 398-401), unfriend only flips
                # friendship_active=false; it does NOT delete the row. Absence
                # would mean a different cleanup path fired.
            else:
                print(f"    room B: status={rb['status']} friendshipActive={rb['friendshipActive']}")
                if rb["friendshipActive"]:
                    failures.append(f"post-unfriend room B friendshipActive=true expected false")
                # Expectation per current code: status stays 'active', flag flips to false.
                # We do NOT fail on status==active because the spec leaves cleanup undecided.

        print("\n[19] @aws_iam directive — Kate (JWT) tries _publishRoomDeactivated; expect Unauthorized")
        publish_mutation = """
        mutation Pub($roomId: ID!, $payload: AWSJSON!) {
          _publishRoomDeactivated(roomId: $roomId, payload: $payload) { roomId status }
        }
        """
        resp = _gql(graphql_url, kate_tok, publish_mutation, {
            "roomId": room_a_id,
            "payload": json.dumps({"reason": "probe-test"}),
        })
        if resp.get("errors"):
            err_blob = json.dumps(resp["errors"])[:300]
            print(f"    OK — AppSync refused: {err_blob}")
            # Sanity: error should be Unauthorized / Forbidden, not a runtime resolver error.
            if "Unauthorized" not in err_blob and "not authorized" not in err_blob.lower() and "forbidden" not in err_blob.lower():
                print(f"    note: refusal reason differs from expected Unauthorized")
        elif resp.get("data", {}).get("_publishRoomDeactivated"):
            failures.append(f"@aws_iam directive bypass: JWT client successfully called _publishRoomDeactivated")
        else:
            failures.append(f"@aws_iam directive: ambiguous response (no errors, no data): {json.dumps(resp)[:200]}")

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
        print("\n[teardown] deleting Cognito users (DDB rows + Aurora rows remain — VPC-private)")
        for ident in (kate_email, kate_sub, john_email, john_sub, jack_email, jack_sub):
            if ident:
                _delete_cognito(cognito, ident)


if __name__ == "__main__":
    sys.exit(main())
