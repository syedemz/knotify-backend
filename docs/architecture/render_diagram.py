"""
Renders docs/architecture/knotify-backend-architecture.png from a hand-laid-out
layered architecture spec. Runs locally: `python docs/architecture/render_diagram.py`.
No network calls. matplotlib only.
"""
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch, Rectangle
from matplotlib.lines import Line2D

# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------
FIG_W, FIG_H = 26, 18
fig, ax = plt.subplots(figsize=(FIG_W, FIG_H), dpi=140)
ax.set_xlim(0, 260)
ax.set_ylim(0, 180)
ax.set_aspect("equal")
ax.axis("off")
fig.patch.set_facecolor("#fafafa")

PALETTE = {
    "client":  ("#e8f0fe", "#1a73e8"),
    "edge":    ("#fef7e0", "#f9ab00"),
    "api":     ("#fce8e6", "#d93025"),
    "compute": ("#e6f4ea", "#188038"),
    "data":    ("#f3e8fd", "#8430ce"),
    "stream":  ("#fff3e0", "#e65100"),
    "orch":    ("#e0f2f1", "#00695c"),
    "cron":    ("#f5f5f5", "#616161"),
    "ext":     ("#fde7f3", "#c2185b"),
}


def box(x, y, w, h, label, kind="compute", fontsize=8, weight="normal", subtitle=None):
    fill, edge = PALETTE[kind]
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.4,rounding_size=1.2",
        linewidth=1.4, edgecolor=edge, facecolor=fill, zorder=2,
    )
    ax.add_patch(patch)
    ax.text(
        x + w / 2, y + h / 2 + (1.2 if subtitle else 0),
        label, ha="center", va="center",
        fontsize=fontsize, fontweight=weight, color="#202124", zorder=3,
    )
    if subtitle:
        ax.text(
            x + w / 2, y + h / 2 - 2.2,
            subtitle, ha="center", va="center",
            fontsize=fontsize - 1.5, color="#5f6368", zorder=3,
        )
    return (x, y, w, h)


def group(x, y, w, h, title, kind="compute"):
    fill, edge = PALETTE[kind]
    patch = FancyBboxPatch(
        (x, y), w, h,
        boxstyle="round,pad=0.6,rounding_size=2",
        linewidth=1.6, edgecolor=edge, facecolor="white", alpha=1.0, zorder=1,
    )
    ax.add_patch(patch)
    # title bar
    title_h = 4
    title_patch = FancyBboxPatch(
        (x, y + h - title_h), w, title_h,
        boxstyle="round,pad=0.4,rounding_size=1.5",
        linewidth=0, edgecolor=edge, facecolor=edge, alpha=0.18, zorder=1,
    )
    ax.add_patch(title_patch)
    ax.text(
        x + 2, y + h - title_h / 2,
        title, ha="left", va="center",
        fontsize=10, fontweight="bold", color=edge, zorder=3,
    )


def arrow(p1, p2, label=None, color="#5f6368", style="-", lw=1.2, curve=0.0,
          label_pos=0.5, dashed=False):
    arr = FancyArrowPatch(
        p1, p2,
        arrowstyle="-|>", mutation_scale=12,
        linewidth=lw, color=color,
        connectionstyle=f"arc3,rad={curve}",
        linestyle="--" if dashed else "-", zorder=4,
    )
    ax.add_patch(arr)
    if label:
        lx = p1[0] + (p2[0] - p1[0]) * label_pos
        ly = p1[1] + (p2[1] - p1[1]) * label_pos
        ax.text(
            lx, ly + 0.6, label, ha="center", va="bottom",
            fontsize=6.5, color=color, zorder=5,
            bbox=dict(boxstyle="round,pad=0.18", fc="#ffffff", ec="none", alpha=0.85),
        )


def center_bottom(b):
    return (b[0] + b[2] / 2, b[1])


def center_top(b):
    return (b[0] + b[2] / 2, b[1] + b[3])


def center_left(b):
    return (b[0], b[1] + b[3] / 2)


def center_right(b):
    return (b[0] + b[2], b[1] + b[3] / 2)


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------
ax.text(
    130, 174, "Knotify Backend — AWS Architecture",
    ha="center", va="center", fontsize=22, fontweight="bold", color="#202124",
)
ax.text(
    130, 169.5, "Region eu-central-1 · phases 1–9 deployed · Terraform 1.11.4 · "
    "24 Lambdas, 7 DDB tables, Aurora Postgres 16.4 SLv2, AppSync GraphQL + wss, Step Functions",
    ha="center", va="center", fontsize=10, color="#5f6368", style="italic",
)

# ---------------------------------------------------------------------------
# Layer 1 — Client + Edge
# ---------------------------------------------------------------------------
client = box(110, 155, 40, 8, "Mobile app",
             kind="client", fontsize=11, weight="bold",
             subtitle="React Native (Expo) — JWT + wss")

# Edge layer
group(5, 138, 250, 12, "Edge layer", kind="edge")
cf = box(15, 141, 30, 7, "CloudFront", kind="edge",
         subtitle="prod path, us-east-1")
waf = box(50, 141, 30, 7, "WAFv2", kind="edge",
          subtitle="Common · BadInputs · SQLi · Rate")
cog = box(150, 140, 95, 9, "Cognito User Pool — knotify-dev-user-pool",
          kind="edge", fontsize=9, weight="bold",
          subtitle="email-only · MFA OPTIONAL (TOTP) · Adv-Sec AUDIT · custom: profile_complete · "
                   "PostConfirm + PreTokenGen V2 triggers")

# ---------------------------------------------------------------------------
# Layer 2 — API surfaces
# ---------------------------------------------------------------------------
group(5, 119, 250, 16, "API surfaces (eu-central-1)", kind="api")
apigw = box(20, 123, 90, 9, "API Gateway HTTP — knotify-dev-api",
            kind="api", fontsize=9, weight="bold",
            subtitle="JWT authorizer (Cognito) · 20+ routes /v1/* · throttle 10 burst / 25 rps")
appsync = box(140, 123, 100, 9, "AppSync GraphQL — knotify-dev-chat-api",
              kind="api", fontsize=9, weight="bold",
              subtitle="primary: COGNITO_USER_POOLS · secondary: AWS_IAM (publishers) · wss subs · field-log: ALL")

# ---------------------------------------------------------------------------
# Layer 3 — Compute (split into 4 groups)
# ---------------------------------------------------------------------------
# 3a. REST lambdas (in-VPC)
group(5, 78, 120, 38, "In-VPC Lambdas — REST + AppSync resolver  (private subnets · lambda_sg)",
      kind="compute")

L_profile = box(11, 102, 24, 8, "profile",
                kind="compute", subtitle="aurora_writer\n/v1/profile* /v1/profiles*")
L_blocks  = box(38, 102, 24, 8, "blocks",
                kind="compute", subtitle="blocks_writer\n/v1/blocks*")
L_friends = box(65, 102, 24, 8, "friends",
                kind="compute", subtitle="friends_writer\n/v1/friend* /v1/friend-requests*")
L_bookm   = box(92, 102, 24, 8, "bookmarks",
                kind="compute", subtitle="aurora_writer\n/v1/bookmarks*")
L_match   = box(11, 91, 24, 8, "match",
                kind="compute", subtitle="aurora_reader_match\n/v1/match/*")
L_pushtok = box(38, 91, 24, 8, "push_tokens",
                kind="compute", subtitle="push_tokens\n/v1/push-tokens (NO VPC)")
L_chat    = box(65, 91, 24, 8, "chat_resolver",
                kind="compute", weight="bold",
                subtitle="chat_resolver (dual trust)\nAppSync Lambda DS")
L_postc   = box(92, 91, 24, 8, "cognito_post_confirmation",
                kind="compute", subtitle="cognito_trigger",
                fontsize=7)
L_pretok  = box(11, 80, 24, 8, "cognito_pre_token_generation",
                kind="compute", subtitle="cognito_trigger",
                fontsize=7)
L_refresh = box(38, 80, 24, 8, "refresh_deck_view",
                kind="compute", subtitle="aurora_refresh_lambda\n(creds: aurora_refresh)",
                fontsize=7)
L_softdel = box(65, 80, 24, 8, "soft_delete_aurora",
                kind="compute", subtitle="aurora_writer",
                fontsize=7)
L_hardp   = box(92, 80, 24, 8, "hard_purge / db_migrator",
                kind="compute", subtitle="aurora_writer / db_migrator",
                fontsize=7)

# 3b. Out-VPC publisher + maintenance lambdas
group(130, 78, 125, 38, "Out-of-VPC Lambdas — DDB streams · AppSync publish · Cognito · Expo · cleanup",
      kind="compute")

L_rsp     = box(136, 102, 28, 8, "room_state_publisher",
                kind="compute", weight="bold",
                subtitle="DDB stream → AppSync (IAM)")
L_np      = box(167, 102, 28, 8, "notifications_publisher",
                kind="compute", weight="bold",
                subtitle="DDB stream → AppSync (IAM)")
L_pf      = box(198, 102, 28, 8, "push_fanout",
                kind="compute", weight="bold",
                subtitle="2 streams → Expo HTTPS")
L_stale   = box(229, 102, 22, 8, "stale_token_cleanup",
                kind="compute", subtitle="EB daily Scan",
                fontsize=7)

# Deletion lambdas
L_validate = box(136, 91, 22, 8, "validate_deletion_request",
                 kind="compute", subtitle="audit_table",
                 fontsize=7)
L_cogst    = box(161, 91, 22, 8, "cognito_user_state",
                 kind="compute", subtitle="Disable/Delete",
                 fontsize=7)
L_deact    = box(186, 91, 22, 8, "deactivate_chat_rooms",
                 kind="compute", subtitle="DDB",
                 fontsize=7)
L_anon     = box(211, 91, 22, 8, "anonymize_chat_messages",
                 kind="compute", subtitle="DDB loop",
                 fontsize=7)
L_hard     = box(136, 80, 22, 8, "hard_delete_user_chat_messages",
                 kind="compute", subtitle="DDB loop",
                 fontsize=6.5)
L_delp     = box(161, 80, 22, 8, "delete_dynamodb_personal_data",
                 kind="compute", subtitle="DDB",
                 fontsize=7)
L_audit    = box(186, 80, 22, 8, "write_audit_log",
                 kind="compute", subtitle="audit_table",
                 fontsize=7)
L_orch     = box(211, 80, 22, 8, "orchestrated by SFN →",
                 kind="orch", subtitle="(arrow into SFN)",
                 fontsize=7)

# ---------------------------------------------------------------------------
# Layer 4 — Data
# ---------------------------------------------------------------------------
# Aurora
group(5, 45, 60, 28, "DB tier (db subnets · aurora_sg)", kind="data")
aurora = box(10, 50, 50, 18,
             "Aurora PostgreSQL 16.4\nServerless v2 (ACU 0.5–2.0)",
             kind="data", fontsize=10, weight="bold",
             subtitle="db: knotify · RLS ON · Data API: ON\nusers · friendships · friend_requests\nblocks · bookmarks · deck_view")

# DynamoDB tables
group(70, 45, 185, 28, "DynamoDB tables (PAY_PER_REQUEST · KMS · PITR)", kind="data")
t_rooms = box(75, 60, 22, 9, "ChatRooms", kind="data",
              subtitle="PK room_id\nSTREAM NEW+OLD",
              fontsize=8, weight="bold")
t_mem   = box(100, 60, 22, 9, "ChatRoomMembership", kind="data",
              subtitle="PK user_id\nSK room_id",
              fontsize=7.5, weight="bold")
t_msg   = box(125, 60, 22, 9, "ChatMessages", kind="data",
              subtitle="PK room_id · SK ts#ulid\nSTREAM NEW_IMAGE",
              fontsize=8, weight="bold")
t_reads = box(150, 60, 22, 9, "MessageReads", kind="data",
              subtitle="PK room_id\nSK user_id",
              fontsize=8, weight="bold")
t_notif = box(175, 60, 22, 9, "Notifications", kind="data",
              subtitle="PK user_id · SK ts#ulid\nGSI UnreadIndex · TTL 90d",
              fontsize=7.5, weight="bold")
t_push  = box(200, 60, 22, 9, "PushNotificationTokens", kind="data",
              subtitle="PK user_id\nSK device_id",
              fontsize=7, weight="bold")
t_aud   = box(225, 60, 25, 9, "account_deletion_audit", kind="data",
              subtitle="PK user_id · SK event_id\nTTL 7y",
              fontsize=7.5, weight="bold")

# Streams indicators
ax.text(86, 56, "▼ stream", ha="center", va="center", fontsize=7, color="#e65100", fontweight="bold")
ax.text(136, 56, "▼ stream", ha="center", va="center", fontsize=7, color="#e65100", fontweight="bold")
ax.text(186, 56, "▼ stream", ha="center", va="center", fontsize=7, color="#e65100", fontweight="bold")

# ---------------------------------------------------------------------------
# Layer 5 — Orchestration + Cron + External
# ---------------------------------------------------------------------------
group(5, 8, 90, 32, "Account-deletion state machine", kind="orch")
sfn = box(10, 26, 80, 11,
          "Step Functions STANDARD\nknotify-dev-account-deletion",
          kind="orch", fontsize=10, weight="bold",
          subtitle="2 branches: SoftDelete (default) · PurgeImmediately\n"
                   "9 task lambdas · global Catch → audit + CloudWatch metric")
ax.text(50, 22, "Soft: Validate → DisableCog → DeactivateRooms → "
        "‖[ SoftDeleteAurora, DeleteDDBPersonal, AnonymizeMsgs(loop) ]‖ → DeleteCog → AuditLog",
        ha="center", va="center", fontsize=6.5, color="#00695c")
ax.text(50, 18, "Purge: Validate → DisableCog → DeactivateRooms → "
        "‖[ SoftDel→HardPurgeNow, DeleteDDBPersonal, HardDeleteMsgs(loop) ]‖ → DeleteCog → AuditLog",
        ha="center", va="center", fontsize=6.5, color="#00695c")
ax.text(50, 13, "Catch handler: RecordFailureAudit → EmitDeletionFailedMetric → Fail",
        ha="center", va="center", fontsize=6.5, color="#d93025", fontweight="bold")

group(100, 25, 60, 15, "EventBridge schedules", kind="cron")
eb_deck = box(105, 31, 25, 6, "rate(15 min)\nrefresh_deck_view",
              kind="cron", fontsize=7)
eb_stale = box(133, 31, 25, 6, "rate(1 day)\nstale_token_cleanup",
               kind="cron", fontsize=7)

group(165, 25, 90, 15, "External / Secrets", kind="ext")
expo = box(170, 31, 28, 6, "Expo Push API",
           kind="ext", fontsize=8, weight="bold",
           subtitle="HTTPS /send")
sm = box(202, 30, 50, 8, "Secrets Manager",
         kind="data", fontsize=9, weight="bold",
         subtitle="aurora master · app_user · aurora_refresh · expo_push (prod)")

# CI/CD note
ax.text(50, 5, "CI/CD: .github/workflows/deploy.yml — push to main/development → "
        "validate (fmt/lint/tfsec/tflint) → plan-{env} → apply-{env} on main.",
        ha="center", va="center", fontsize=7.5, color="#5f6368", style="italic")
ax.text(50, 2.5, "Smoke: smoke-test.yml on smoke-test/** branches. Lambdas built via `make package-all` (Python 3.14, arm64).",
        ha="center", va="center", fontsize=7.5, color="#5f6368", style="italic")

# ---------------------------------------------------------------------------
# Arrows — primary flows
# ---------------------------------------------------------------------------
# Client → CloudFront/WAF → APIGW
arrow(center_bottom(client), center_top(cf), label="REST + JWT", curve=-0.15, color="#1a73e8")
arrow((cf[0] + cf[2], cf[1] + cf[3] / 2), (waf[0], waf[1] + waf[3] / 2), color="#f9ab00")
arrow(center_bottom(waf), center_top(apigw), color="#d93025", curve=0.0)
arrow(center_bottom(client), center_top(appsync), label="GraphQL + wss", color="#1a73e8", curve=0.15)
arrow((center_bottom(client)[0] + 10, center_bottom(client)[1]), center_top(cog),
      label="USER_SRP auth", color="#1a73e8", curve=0.4)

# Cognito triggers
arrow(center_bottom(cog), (L_postc[0] + L_postc[2] / 2, L_postc[1] + L_postc[3]),
      label="PostConfirmation", color="#f9ab00", curve=-0.25)
arrow((cog[0] + 5, cog[1]), (L_pretok[0] + L_pretok[2] / 2, L_pretok[1] + L_pretok[3]),
      label="PreTokenGen V2", color="#f9ab00", curve=-0.3)

# API GW → REST lambdas (single bus arrow with labels)
arrow(center_bottom(apigw), center_top(L_profile), curve=-0.1, color="#d93025")
arrow(center_bottom(apigw), center_top(L_blocks), color="#d93025")
arrow(center_bottom(apigw), center_top(L_friends), color="#d93025")
arrow(center_bottom(apigw), center_top(L_bookm), color="#d93025")
arrow(center_bottom(apigw), center_top(L_match), curve=0.2, color="#d93025")
arrow(center_bottom(apigw), center_top(L_pushtok), curve=0.25, color="#d93025")

# AppSync → chat_resolver
arrow(center_bottom(appsync), center_top(L_chat),
      label="UNIT resolvers:\ncreateOrGetRoom, sendMessage,\nmarkAsRead, listMyRooms, messagesByChatRoom",
      curve=-0.2, color="#d93025", label_pos=0.4)
# AppSync → membership check (subscribe-time pipeline fn)
arrow((appsync[0] + appsync[2] - 5, appsync[1]), center_top(t_mem),
      label="check_room_membership (subscribe)", color="#d93025", curve=0.35, label_pos=0.7)

# REST lambdas → Aurora
for b in (L_profile, L_blocks, L_friends, L_bookm, L_match):
    arrow(center_bottom(b), center_top(aurora), color="#8430ce", curve=-0.1)

# Cognito triggers → Aurora
arrow(center_bottom(L_postc), center_top(aurora), color="#8430ce", curve=0.3)
arrow(center_bottom(L_pretok), center_top(aurora), color="#8430ce", curve=0.2)
arrow(center_bottom(L_softdel), center_top(aurora), color="#8430ce")
arrow(center_bottom(L_hardp), center_top(aurora), color="#8430ce", curve=0.2)
arrow(center_bottom(L_refresh), center_top(aurora), color="#8430ce")

# chat_resolver → DDB
for tgt in (t_rooms, t_mem, t_msg, t_reads, t_notif):
    arrow(center_bottom(L_chat), center_top(tgt), color="#8430ce", curve=0.05)

# push_tokens → PushNotificationTokens
arrow(center_bottom(L_pushtok), center_top(t_push), color="#8430ce", curve=0.0)

# Streams → publisher lambdas
arrow(center_top(t_rooms), center_bottom(L_rsp),
      label="ChatRooms stream", color="#e65100", curve=0.25, lw=1.6)
arrow(center_top(t_msg), center_bottom(L_pf),
      label="ChatMessages stream", color="#e65100", curve=0.0, lw=1.6)
arrow(center_top(t_notif), center_bottom(L_np),
      label="Notifications stream", color="#e65100", curve=-0.15, lw=1.6)
arrow((t_notif[0] + t_notif[2] - 3, t_notif[1] + t_notif[3]),
      (L_pf[0] + 3, L_pf[1]), color="#e65100", curve=0.2, lw=1.4)

# Publishers → AppSync (back-up)
arrow(center_top(L_rsp), (appsync[0] + 60, appsync[1]),
      label="IAM SigV4\n_publishRoomDe/Reactivated", color="#188038", curve=-0.3, label_pos=0.6)
arrow(center_top(L_np), (appsync[0] + 80, appsync[1]),
      label="IAM SigV4\npublishNotification\n_publishFriendRequestUpdated",
      color="#188038", curve=0.0, label_pos=0.65)

# push_fanout → Expo
arrow(center_bottom(L_pf), center_top(expo),
      label="HTTPS /send", color="#c2185b", curve=0.0, lw=1.6)

# EventBridge → lambdas
arrow(center_top(eb_deck), (L_refresh[0] + L_refresh[2] / 2, L_refresh[1]),
      color="#616161", curve=-0.3, lw=1.4)
arrow(center_top(eb_stale), center_bottom(L_stale), color="#616161", curve=0.0, lw=1.4)

# Stale token cleanup → push tokens table
arrow(center_bottom(L_stale), (t_push[0] + t_push[2] / 2, t_push[1] + t_push[3]),
      color="#8430ce", curve=0.0)

# Step Functions → deletion lambdas (bundled arrow)
arrow(center_top(sfn),
      (L_validate[0] + L_validate[2] / 2, L_validate[1]),
      label="invoke 9 task types", color="#00695c", curve=0.2, lw=1.6, label_pos=0.4)
for b in (L_cogst, L_deact, L_anon, L_hard, L_delp, L_audit):
    arrow(center_top(sfn), (b[0] + b[2] / 2, b[1]), color="#00695c", curve=0.15, lw=1.0)

# Soft/hard delete arrows from SFN to in-VPC deletion lambdas
arrow(center_top(sfn), center_bottom(L_softdel), color="#00695c", curve=-0.05, lw=1.0)
arrow(center_top(sfn), center_bottom(L_hardp), color="#00695c", curve=0.05, lw=1.0)

# cognito_user_state → Cognito (admin disable/delete)
arrow(center_top(L_cogst), (cog[0] + cog[2] - 10, cog[1]),
      label="AdminDisable/Delete", color="#f9ab00", curve=0.4, dashed=True, label_pos=0.7)

# Profile → refresh_deck_view async
arrow(center_left(L_refresh), center_right(L_profile),
      label="async invoke\non profile_complete flip",
      color="#188038", curve=0.5, lw=1.0, label_pos=0.5)

# Aurora ↔ Secrets Manager
arrow((aurora[0] + aurora[2], aurora[1] + 5),
      (sm[0] + 5, sm[1] + sm[3]),
      label="managed master pw", color="#8430ce", curve=-0.4, dashed=True, label_pos=0.5)

# ---------------------------------------------------------------------------
# Legend
# ---------------------------------------------------------------------------
legend_items = [
    ("Client",          PALETTE["client"][1]),
    ("Edge / Auth",     PALETTE["edge"][1]),
    ("API surface",     PALETTE["api"][1]),
    ("Lambda compute",  PALETTE["compute"][1]),
    ("Data store",      PALETTE["data"][1]),
    ("DDB Stream",      PALETTE["stream"][1]),
    ("Orchestration",   PALETTE["orch"][1]),
    ("Cron",            PALETTE["cron"][1]),
    ("External",        PALETTE["ext"][1]),
]
lx, ly = 175, 4
ax.text(lx - 2, ly + 5, "Legend", fontsize=9, fontweight="bold", color="#202124")
for i, (name, c) in enumerate(legend_items):
    col = i % 5
    row = i // 5
    cx = lx + col * 15
    cy = ly + 2 - row * 3
    ax.add_patch(Rectangle((cx, cy), 2, 2, facecolor=c, edgecolor=c, alpha=0.4))
    ax.text(cx + 3, cy + 1, name, fontsize=7, va="center", color="#202124")

# Save
out = "C:/Users/syede/Claude-Master/knotify-backend/docs/architecture/knotify-backend-architecture.png"
plt.tight_layout()
plt.savefig(out, dpi=140, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"wrote {out}")
