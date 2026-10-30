#!/command/with-contenv bash
# Glue key-refill worker entrypoint. Reads add-on options, maps them to the
# GLUE_* env the CLI expects, and reconciles the ESPHome key bank on a loop.
#
# The `with-contenv` shebang is REQUIRED: it loads the container environment
# the Supervisor injects (including SUPERVISOR_TOKEN) from
# /var/run/s6/container_environment. A plain `#!/usr/bin/env bash` runs without
# that env, so SUPERVISOR_TOKEN is empty and HA access fails even though
# homeassistant_api: true is set. (HA developer docs, add-on tutorial.)
set -euo pipefail

OPTS=/data/options.json

opt() { jq -r --arg k "$1" '.[$k] // empty' "$OPTS"; }

# --- lock identity + Glue login ---------------------------------------------
export GLUE_HA_DEVICE="$(opt ha_device)"
export GLUE_HA_BANK_ENTITY="$(opt ha_bank_entity)"
export GLUE_LOCK_ID="$(opt lock_id)"
export GLUE_LOCK_SERIAL="$(opt lock_serial)"
export GLUE_USERNAME="$(opt glue_username)"
export GLUE_PASSWORD="$(opt glue_password)"

# HA access: by default the worker uses the Supervisor's Core API proxy
# (http://supervisor/core/api) with SUPERVISOR_TOKEN, which HA sets automatically
# — no url or token to configure. Only export GLUE_HA_URL/TOKEN if the user set
# them (to reach a non-add-on HA); an empty export would shadow the proxy default.
HA_URL="$(opt ha_url)"; [ -n "$HA_URL" ] && export GLUE_HA_URL="$HA_URL"
HA_TOKEN="$(opt ha_token)"; [ -n "$HA_TOKEN" ] && export GLUE_HA_TOKEN="$HA_TOKEN"

# Persistent encrypted store lives on /data (survives restarts/updates). The
# controller-ownership record and key-bank tombstone MUST persist here, so the
# storage key must stay constant across restarts — set it once in options.
export XDG_CONFIG_HOME=/data
export GLUE_STORAGE_KEY="$(opt storage_key)"

INTERVAL="$(opt interval_seconds)"; INTERVAL="${INTERVAL:-30}"
LEVEL="$(opt log_level)"; LEVEL="${LEVEL:-info}"

fail() { echo "FATAL: $*" >&2; exit 1; }
[ -n "$GLUE_LOCK_ID" ]   || fail "lock_id is required"
[ -n "$GLUE_USERNAME" ]  || fail "glue_username is required"
[ -n "$GLUE_PASSWORD" ]  || fail "glue_password is required"
[ -n "$GLUE_STORAGE_KEY" ] || fail "storage_key is required (generate: openssl rand -base64 32)"
[ -n "${SUPERVISOR_TOKEN:-}${GLUE_HA_URL:-}" ] || fail "no HA access: the Supervisor did not grant API access (SUPERVISOR_TOKEN missing).
  This almost always means the add-on is running a STALE build without the
  homeassistant_api permission. Fix: open the add-on -> three-dots menu ->
  Rebuild, and confirm the version is >= 0.2.3. If Rebuild does not help,
  uninstall the add-on, remove+re-add this repository in the Add-on Store,
  then reinstall (the github cache is sticky).
  Only if you are NOT on HA OS/Supervised (no Supervisor at all): set ha_url
  + ha_token in the add-on config to reach Home Assistant directly."

echo "Glue refill worker starting (device=$GLUE_HA_DEVICE, interval=${INTERVAL}s, ha=$([ -n "${GLUE_HA_URL:-}" ] && echo "$GLUE_HA_URL" || echo supervisor-proxy))."

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
