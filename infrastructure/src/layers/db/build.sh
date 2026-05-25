#!/usr/bin/env bash
# build.sh — build the knotify-db Lambda layer
#
# Produces:
#   python/               — the layer's site-packages directory (Lambda requires
#                           this exact layout under the zip root)
#   knotify-db-layer.zip  — the deployable artifact
#
# Pinned versions (brainstorm N4 — no "latest"; pin for reproducibility):
#   psycopg2-binary  2.9.12 — manylinux2014_aarch64 wheel for arm64 Lambda
#   pgvector         0.3.6  — Python client for pgvector (brainstorm N3 — kept)
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
ZIP_NAME="knotify-db-layer.zip"
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
  "psycopg2-binary==2.9.12" \
  "pgvector==0.3.6"

# ---------------------------------------------------------------------------
# Copy the knotify_db wrapper module into python/ so it is importable at
# the same level as the third-party packages.
# ---------------------------------------------------------------------------
echo "[build] Copying knotify_db wrapper into ${OUTPUT_DIR}/knotify_db/ ..."
cp -r "${SCRIPT_DIR}/knotify_db" "${OUTPUT_DIR}/knotify_db"

# ---------------------------------------------------------------------------
# Create the zip
# ---------------------------------------------------------------------------
echo "[build] Creating ${ZIP_NAME} ..."
cd "${SCRIPT_DIR}"
zip -r --quiet "${ZIP_NAME}" python/

echo "[build] Done. Artifact: ${ZIP_PATH}"
echo "[build] Layer contents summary:"
echo "  $(du -sh "${OUTPUT_DIR}" | cut -f1)  python/"
