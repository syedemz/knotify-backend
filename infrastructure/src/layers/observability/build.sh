#!/usr/bin/env bash
# build.sh — build the knotify-observability Lambda layer
#
# Produces:
#   python/               — the layer's site-packages directory (Lambda requires
#                           this exact layout under the zip root)
#   knotify-observability-layer.zip — the deployable artifact
#
# Pinned versions (brainstorm N4 — no "latest"; pin for reproducibility):
#   aws-lambda-powertools 3.29.0 — supports Python 3.13/3.14 since v3.x
#   PyJWT[crypto]         2.13.0 — actively maintained; replaces python-jose
#                                   (brainstorm M5)
#   requests              2.32.3 — used by verify_cognito_jwt for JWKS fetch
#
# Target platform: manylinux2014_aarch64 (arm64 Lambda runtime)
# Runtime compatibility: python3.14
#
# Usage:
#   bash build.sh              — full rebuild
#   bash build.sh --clean      — remove output before rebuilding
#
# Prerequisites: Python 3, pip, zip

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${SCRIPT_DIR}/python"
ZIP_NAME="knotify-observability-layer.zip"
ZIP_PATH="${SCRIPT_DIR}/${ZIP_NAME}"

# ---------------------------------------------------------------------------
# Optional clean
# ---------------------------------------------------------------------------
if [[ "${1:-}" == "--clean" ]]; then
  echo "[build] Cleaning previous output..."
  rm -rf "${OUTPUT_DIR}"
  rm -f "${ZIP_PATH}"
fi

# ---------------------------------------------------------------------------
# Install third-party packages into python/
# ---------------------------------------------------------------------------
echo "[build] Installing packages into ${OUTPUT_DIR}/ ..."
pip install \
  --quiet \
  --target "${OUTPUT_DIR}" \
  --platform manylinux2014_aarch64 \
  --implementation cp \
  --python-version 3.14 \
  --only-binary=:all: \
  --upgrade \
  "aws-lambda-powertools==3.29.0" \
  "PyJWT[crypto]==2.13.0" \
  "requests==2.32.3"

# ---------------------------------------------------------------------------
# Copy the knotify_obs wrapper module into python/ so it is importable at
# the same level as the third-party packages.
# ---------------------------------------------------------------------------
echo "[build] Copying knotify_obs wrapper into ${OUTPUT_DIR}/knotify_obs/ ..."
cp -r "${SCRIPT_DIR}/knotify_obs" "${OUTPUT_DIR}/knotify_obs"

# ---------------------------------------------------------------------------
# Create the zip
# ---------------------------------------------------------------------------
echo "[build] Creating ${ZIP_NAME} ..."
cd "${SCRIPT_DIR}"
zip -r --quiet "${ZIP_NAME}" python/

echo "[build] Done. Artifact: ${ZIP_PATH}"
echo "[build] Layer contents summary:"
echo "  $(du -sh "${OUTPUT_DIR}" | cut -f1)  python/"
