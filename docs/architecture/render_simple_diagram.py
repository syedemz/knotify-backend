"""
Renders docs/architecture/knotify-backend-overview.png — a presentation-friendly
overview of the AWS services used by knotify-backend, for non-engineering audiences.

Uses real AWS service icons (from github.com/mingrammer/diagrams) placed on a clean
matplotlib canvas. Each service has its icon + a one-line plain-English purpose,
with labeled arrows showing the major interactions.
"""
import pathlib
import matplotlib.pyplot as plt
from matplotlib.offsetbox import OffsetImage, AnnotationBbox
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch
from PIL import Image

ICON_DIR = pathlib.Path("docs/architecture/icons")
OUT = "docs/architecture/knotify-backend-overview.png"

# ---------------------------------------------------------------------------
# Canvas — wider + taller so things breathe
# ---------------------------------------------------------------------------
fig, ax = plt.subplots(figsize=(22, 14), dpi=150)
ax.set_xlim(0, 220)
ax.set_ylim(0, 140)
ax.set_aspect("equal")
ax.axis("off")
fig.patch.set_facecolor("#ffffff")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_icon(name, zoom=0.35):
    img = Image.open(ICON_DIR / f"{name}.png").convert("RGBA")
    return OffsetImage(img, zoom=zoom)


def service(cx, cy, icon_name, title, subtitle,
            panel_w=30, panel_h=22, icon_zoom=0.35):
    """Place a service: rounded panel + AWS icon (top) + bold title + one-line purpose."""
    panel = FancyBboxPatch(
        (cx - panel_w / 2, cy - panel_h / 2),
        panel_w, panel_h,
        boxstyle="round,pad=0.5,rounding_size=1.6",
        linewidth=1.5, edgecolor="#cfd6dd", facecolor="#ffffff", zorder=2,
    )
    ax.add_patch(panel)
    # icon sits in the upper third of the panel
    img = load_icon(icon_name, zoom=icon_zoom)
    ab = AnnotationBbox(img, (cx, cy + panel_h * 0.22), frameon=False, zorder=3)
    ax.add_artist(ab)
    # title
    ax.text(cx, cy - panel_h * 0.10, title, ha="center", va="center",
            fontsize=12, fontweight="bold", color="#1f2328", zorder=4)
    # subtitle (purpose)
    ax.text(cx, cy - panel_h * 0.30, subtitle, ha="center", va="center",
            fontsize=9.5, color="#57606a", zorder=4)
    return (cx, cy, panel_w, panel_h)


def arrow(p1, p2, label=None, color="#57606a", lw=2.0, curve=0.0,
          label_pos=0.5, dashed=False, label_offset=(0, 0)):
    arr = FancyArrowPatch(
        p1, p2,
        arrowstyle="-|>", mutation_scale=22,
        linewidth=lw, color=color,
        connectionstyle=f"arc3,rad={curve}",
        linestyle="--" if dashed else "-", zorder=5,
    )
    ax.add_patch(arr)
    if label:
        lx = p1[0] + (p2[0] - p1[0]) * label_pos + label_offset[0]
        ly = p1[1] + (p2[1] - p1[1]) * label_pos + label_offset[1]
        # nudge label perpendicular to arrow direction when curved
        if curve != 0:
            dx, dy = p2[0] - p1[0], p2[1] - p1[1]
            # perpendicular offset proportional to curvature
            length = max((dx * dx + dy * dy) ** 0.5, 1)
            nx, ny = -dy / length, dx / length
            lx += nx * curve * 14
            ly += ny * curve * 14
        ax.text(lx, ly, label, ha="center", va="center",
                fontsize=9.5, color="#1f2328", zorder=6,
                bbox=dict(boxstyle="round,pad=0.35", fc="#ffffff",
                          ec="#d0d7de", alpha=0.96))


def s_top(s):    return (s[0], s[1] + s[3] / 2)
def s_bot(s):    return (s[0], s[1] - s[3] / 2)
def s_left(s):   return (s[0] - s[2] / 2, s[1])
def s_right(s):  return (s[0] + s[2] / 2, s[1])

# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------
ax.text(110, 133, "Knotify Backend — AWS Components Overview",
        ha="center", va="center", fontsize=26, fontweight="bold", color="#1f2328")
ax.text(110, 128, "The cloud services that power the app: how users connect, "
        "how data flows, and how it stays secure.",
        ha="center", va="center", fontsize=13, color="#57606a", style="italic")

# ---------------------------------------------------------------------------
# Layer 1 — User (top)
# ---------------------------------------------------------------------------
user = service(110, 115, "mobile",
               "Mobile App",
               "Knotify on the user's phone",
               panel_w=34, panel_h=22, icon_zoom=0.32)

# ---------------------------------------------------------------------------
# Layer 2 — Security / Edge
# ---------------------------------------------------------------------------
cf  = service(30,  92, "cloudfront",     "CloudFront",      "Global CDN — fast & cached")
waf = service(70,  92, "waf",            "WAF",             "Blocks bad traffic / attacks")
cog = service(150, 92, "cognito",        "Cognito",         "Sign-up, sign-in & tokens")
sm  = service(190, 92, "secretsmanager", "Secrets Manager", "Stores DB passwords safely")

# ---------------------------------------------------------------------------
# Layer 3 — API surfaces
# ---------------------------------------------------------------------------
apigw   = service(55,  68, "apigateway", "API Gateway",       "Handles REST API requests",   panel_w=34)
appsync = service(140, 68, "appsync",    "AppSync (GraphQL)", "Real-time chat & live updates", panel_w=38)

# ---------------------------------------------------------------------------
# Layer 4 — Compute (Lambda) — wide centerpiece
# ---------------------------------------------------------------------------
lam = service(100, 42, "lambda",
              "Lambda  (24 functions)",
              "The app's business logic — runs on demand",
              panel_w=58, panel_h=24, icon_zoom=0.42)

# ---------------------------------------------------------------------------
# Layer 4b — Orchestration (right of Lambda)
# ---------------------------------------------------------------------------
sfn = service(165, 42, "stepfunctions", "Step Functions",
              "Account-deletion workflow", panel_w=32)
eb  = service(200, 42, "eventbridge",   "EventBridge",
              "Scheduled background jobs", panel_w=30)

# ---------------------------------------------------------------------------
# Layer 5 — Data + Ops (bottom)
# ---------------------------------------------------------------------------
aurora = service(35,  15, "aurora",     "Aurora PostgreSQL",
                 "Users, friends, profiles, matches", panel_w=42)
ddb    = service(95,  15, "dynamodb",   "DynamoDB",
                 "Chat messages & notifications",      panel_w=34)
cw     = service(200, 15, "cloudwatch", "CloudWatch",
                 "Logs, metrics & alerts",             panel_w=30)

# ---------------------------------------------------------------------------
# External — Expo Push
# ---------------------------------------------------------------------------
expo_w, expo_h = 34, 22
expo_x, expo_y = 155, 15
expo_panel = FancyBboxPatch(
    (expo_x - expo_w / 2, expo_y - expo_h / 2),
    expo_w, expo_h,
    boxstyle="round,pad=0.5,rounding_size=1.6",
    linewidth=1.5, edgecolor="#c2185b", facecolor="#fde7f3", zorder=2,
)
ax.add_patch(expo_panel)
ax.text(expo_x, expo_y + expo_h * 0.22, "PUSH", ha="center", va="center",
        fontsize=18, fontweight="bold", color="#c2185b", zorder=3,
        bbox=dict(boxstyle="round,pad=0.4", fc="#ffffff", ec="#c2185b", lw=1.5))
ax.text(expo_x, expo_y - expo_h * 0.10, "Expo Push",
        ha="center", va="center", fontsize=12, fontweight="bold",
        color="#1f2328", zorder=4)
ax.text(expo_x, expo_y - expo_h * 0.30,
        "Push notifications to phones",
        ha="center", va="center", fontsize=9.5, color="#57606a", zorder=4)
expo = (expo_x, expo_y, expo_w, expo_h)

# ---------------------------------------------------------------------------
# Layer labels (left margin)
# ---------------------------------------------------------------------------
def layer_label(y, text):
    ax.text(2, y, text, ha="left", va="center",
            fontsize=10, fontweight="bold", color="#8430ce")

layer_label(115, "USER")
layer_label(92,  "SECURITY / EDGE")
layer_label(68,  "APIs")
layer_label(42,  "COMPUTE / WORKFLOWS")
layer_label(15,  "DATA / NOTIFICATIONS / OPS")

# ---------------------------------------------------------------------------
# Arrows — clear, minimal, business-language
# ---------------------------------------------------------------------------

# Mobile → CloudFront (REST traffic)
arrow(s_bot(user), s_top(cf),
      label="REST API calls", color="#1a73e8",
      curve=-0.25, label_pos=0.45)

# Mobile → Cognito (auth)
arrow(s_bot(user), s_top(cog),
      label="Sign in / Sign up", color="#f9ab00",
      curve=0.25, label_pos=0.55)

# Mobile → AppSync (real-time)
arrow((user[0] + 4, user[1] - user[3] / 2), s_top(appsync),
      label="WebSocket (chat)", color="#188038",
      curve=0.05, label_pos=0.55)

# CloudFront → WAF → API Gateway
arrow(s_right(cf), s_left(waf),
      label="filters", color="#f9ab00", label_pos=0.5)
arrow(s_bot(waf), s_top(apigw),
      label="clean traffic", color="#f9ab00", curve=-0.15)

# Cognito → AppSync (token validation, dashed = background)
arrow(s_bot(cog), s_top(appsync),
      label="validates JWT", color="#8e6c00",
      curve=-0.15, dashed=True)

# API Gateway → Lambda
arrow(s_bot(apigw), (lam[0] - lam[2] / 2 + 8, lam[1] + lam[3] / 2),
      label="invokes (profile, friends,\nblocks, matches, push tokens)",
      color="#d93025", curve=-0.05)

# AppSync → Lambda
arrow(s_bot(appsync), (lam[0] + 14, lam[1] + lam[3] / 2),
      label="invokes (chat ops)",
      color="#d93025", curve=0.05)

# Lambda → Aurora
arrow((lam[0] - lam[2] / 2 + 6, lam[1] - lam[3] / 2), s_top(aurora),
      label="reads / writes\nrelational data",
      color="#8430ce", curve=-0.05)

# Lambda → DynamoDB
arrow((lam[0], lam[1] - lam[3] / 2), s_top(ddb),
      label="reads / writes\nchat & notifications",
      color="#8430ce", curve=0.0)

# Lambda ↔ Secrets Manager (fetch DB password)
arrow((lam[0] + lam[2] / 2 - 4, lam[1] + 4), s_bot(sm),
      label="fetches DB password", color="#57606a",
      curve=0.40, dashed=True, label_pos=0.55)

# DynamoDB stream → Lambda (push fanout)
arrow((ddb[0] + ddb[2] / 2 - 4, ddb[1] + ddb[3] / 2),
      (lam[0] + 4, lam[1] - lam[3] / 2),
      label="new message →\nfanout Lambda",
      color="#e65100", curve=-0.20, lw=2.0)

# Lambda → AppSync (publish events to subscribers)
arrow((lam[0] + 18, lam[1] + lam[3] / 2),
      (appsync[0] - 6, appsync[1] - appsync[3] / 2),
      label="pushes events\nto subscribers",
      color="#188038", curve=-0.18, lw=2.0)

# Lambda → Expo Push
arrow((lam[0] + lam[2] / 2 - 4, lam[1] - lam[3] / 2),
      (expo_x - expo_w / 2 + 4, expo_y + expo_h / 2 - 2),
      label="sends push",
      color="#c2185b", curve=0.15, lw=1.8)

# Step Functions → Lambda (deletion steps)
arrow(s_left(sfn), (lam[0] + lam[2] / 2 - 2, lam[1] + 4),
      label="orchestrates\n9 deletion steps",
      color="#00695c", curve=-0.05)

# Step Functions → Cognito (admin delete)
arrow(s_top(sfn), (cog[0] + 6, cog[1] - cog[3] / 2),
      label="disables /\ndeletes account",
      color="#00695c", curve=0.30, dashed=True)

# EventBridge → Lambda (scheduled)
arrow(s_left(eb), (lam[0] + lam[2] / 2 - 2, lam[1] - 2),
      label="every 15 min /\nonce daily",
      color="#616161", curve=-0.15)

# Lambda → CloudWatch (logs/metrics) — dashed
arrow((lam[0] + lam[2] / 2 - 2, lam[1] - lam[3] / 2),
      (cw[0] - cw[2] / 2 + 2, cw[1] + cw[3] / 2),
      label="logs & metrics",
      color="#5f6368", curve=-0.30, dashed=True, lw=1.4)

# ---------------------------------------------------------------------------
# Legend (bottom-left)
# ---------------------------------------------------------------------------
lx, ly = 5, 4
ax.text(lx, ly + 2.5, "Arrow legend", fontsize=10,
        fontweight="bold", color="#1f2328")
ax.add_line(plt.Line2D([lx, lx + 6], [ly, ly], color="#57606a", lw=2.0))
ax.text(lx + 7, ly, "Data flow", fontsize=9.5, va="center", color="#1f2328")
ax.add_line(plt.Line2D([lx + 22, lx + 28], [ly, ly],
                       color="#57606a", lw=2.0, linestyle="--"))
ax.text(lx + 29, ly, "Background / support", fontsize=9.5, va="center", color="#1f2328")

# ---------------------------------------------------------------------------
# Footer
# ---------------------------------------------------------------------------
ax.text(135, 5,
        "All services run in AWS eu-central-1. Lambdas live inside a private network (VPC) "
        "— the database is never directly exposed to the internet.",
        ha="center", va="center", fontsize=10, color="#57606a", style="italic")
ax.text(135, 2,
        "Deployed automatically via GitHub Actions: every change is checked, planned, and applied to the dev environment.",
        ha="center", va="center", fontsize=10, color="#57606a", style="italic")

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------
plt.tight_layout()
plt.savefig(OUT, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
print(f"wrote {OUT}")
