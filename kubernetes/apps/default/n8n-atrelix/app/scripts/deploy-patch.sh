#!/bin/sh
# Deploy patch for n8n-atrelix — PLACEHOLDER, replace body with the real patch.
#
# Contract (see wrapper.sh):
#   - runs on every pod start, after the payload was downloaded to
#     $DEPLOY_PATCH_FILE and (if pinned) checksum-verified
#   - runs as UID 1000: /data (PVC) and /tmp are writable; the n8n install
#     under /usr/local/lib/node_modules/n8n is root-owned and is NOT —
#     patches needing to change app code belong in the fork image instead
#   - non-zero exit blocks n8n startup (fail-closed)
set -eu

echo "[deploy-patch] stub: no patch actions defined yet (payload at $DEPLOY_PATCH_FILE)"
