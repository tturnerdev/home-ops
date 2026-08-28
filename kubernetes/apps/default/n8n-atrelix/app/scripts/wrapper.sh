#!/bin/sh
# Entrypoint wrapper for n8n-atrelix: optional deploy-time patch, then n8n.
#
# On every pod start: if DEPLOY_PATCH_URL is set, download it (optionally
# verified against DEPLOY_PATCH_SHA256), export the payload path as
# DEPLOY_PATCH_FILE, run deploy-patch.sh, then exec the stock entrypoint.
# URL unset -> patch is skipped entirely and n8n boots normally.
#
# Fail-closed: a failed download, checksum mismatch, or non-zero exit from
# deploy-patch.sh blocks startup (crash loop) instead of running half-patched.
set -eu

if [ -n "${DEPLOY_PATCH_URL:-}" ]; then
  DEPLOY_PATCH_FILE="${DEPLOY_PATCH_FILE:-/tmp/deploy-patch.payload}"
  echo "[deploy-patch] downloading payload"
  wget -qO "$DEPLOY_PATCH_FILE" "$DEPLOY_PATCH_URL"

  if [ -n "${DEPLOY_PATCH_SHA256:-}" ]; then
    echo "$DEPLOY_PATCH_SHA256  $DEPLOY_PATCH_FILE" | sha256sum -c -
  else
    echo "[deploy-patch] WARNING: DEPLOY_PATCH_SHA256 not set, payload unverified"
  fi

  export DEPLOY_PATCH_FILE
  echo "[deploy-patch] running deploy-patch.sh"
  sh /patch/deploy-patch.sh
  echo "[deploy-patch] done"
else
  echo "[deploy-patch] DEPLOY_PATCH_URL not set; skipping"
fi

exec tini -- /docker-entrypoint.sh
