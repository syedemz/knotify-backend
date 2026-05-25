#!/usr/bin/env bash
# build.sh — build the knotify-db Lambda layer
#
# Produces:
#   python/               — the layer's site-packages directory (Lambda requires
#                           this exact layout under the zip root)
#   knotify-db-layer.zip  — the deployable artifact
#
# Pinned versions (brainstorm N4 — no "latest"; pin for reproducibility):
#   psycopg2-binary  2.9.12 — manylinux_2_28_aarch64 wheel for arm64 Lambda
#                             (2.9.12 dropped the older manylinux2014/glibc-2.17
#                             baseline for arm64 — Amazon Linux 2023 ships
#                             glibc 2.34, so manylinux_2_28 loads fine)
#   pgvector         0.3.6  — pure-Python (py3-none-any), platform-agnostic
#   yoyo-migrations  9.0.0  — pure-Python (py3-none-any), platform-agnostic
#                             (story 3.7 B3 — added so the function can use the
#                             layer rather than bundling yoyo in the function zip)
#
# Target platform: manylinux_2_28_aarch64 (arm64 Lambda runtime on AL2023)
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
  --platform manylinux_2_28_aarch64 \
  --implementation cp \
  --python-version 3.14 \
  --only-binary=:all: \
  --upgrade \
  "psycopg2-binary==2.9.12" \
  "pgvector==0.3.6" \
  "yoyo-migrations==9.0.0"

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
