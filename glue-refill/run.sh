#!/usr/bin/env bash
# Glue key-refill worker entrypoint. Reads add-on options, maps them to the
# GLUE_* env the CLI expects, and reconciles the ESPHome key bank on a loop.
set -euo pipefail

OPTS=/data/options.json

opt() { jq -r --arg k "$1" '.[$k] // empty' "$OPTS"; }

# --- required: HA access + lock identity + Glue login -----------------------
export GLUE_HA_URL="$(opt ha_url)"
export GLUE_HA_TOKEN="$(opt ha_token)"
export GLUE_HA_DEVICE="$(opt ha_device)"
export GLUE_HA_BANK_ENTITY="$(opt ha_bank_entity)"
export GLUE_LOCK_ID="$(opt lock_id)"
export GLUE_LOCK_SERIAL="$(opt lock_serial)"
export GLUE_USERNAME="$(opt glue_username)"
export GLUE_PASSWORD="$(opt glue_password)"

# Persistent encrypted store lives on /data (survives restarts/updates). The
# controller-ownership record and key-bank tombstone MUST persist here, so the
# storage key must stay constant across restarts — set it once in options.
export XDG_CONFIG_HOME=/data
export GLUE_STORAGE_KEY="$(opt storage_key)"

INTERVAL="$(opt interval_seconds)"; INTERVAL="${INTERVAL:-30}"
LEVEL="$(opt log_level)"; LEVEL="${LEVEL:-info}"

fail() { echo "FATAL: $*" >&2; exit 1; }
[ -n "$GLUE_HA_URL" ]    || fail "ha_url is required"
[ -n "$GLUE_HA_TOKEN" ]  || fail "ha_token is required"
[ -n "$GLUE_LOCK_ID" ]   || fail "lock_id is required"
[ -n "$GLUE_USERNAME" ]  || fail "glue_username is required"
[ -n "$GLUE_PASSWORD" ]  || fail "glue_password is required"
[ -n "$GLUE_STORAGE_KEY" ] || fail "storage_key is required (generate: openssl rand -base64 32)"

echo "Glue refill worker starting (device=$GLUE_HA_DEVICE, interval=${INTERVAL}s)."

# One-shot reconcile per tick — gives a genuinely configurable interval and a
# clean exit per cycle. A failure is logged and retried next tick, never fatal.
while true; do
  if python3 /app/tools/glue.py sync; then
    :
  else
    echo "Reconcile cycle failed; will retry in ${INTERVAL}s." >&2
  fi
  sleep "$INTERVAL"
done
