"""Shared Glue app constants — the same for every user of the (abandoned) Glue
product, extracted from the public Glue Android APK (gluehome.picapau).

These are not per-account secrets: they are baked into the shipped app and are
already extractable by anyone from the public APK on APKPure etc. They are
bundled here only to spare each user from re-extracting them. Your own login,
lock id, and keys are NOT here — those are per-account and discovered at setup.

See GLUE_APP_TECHNICAL_WRITEUP.md §2 for provenance.
"""

# --- cloud / auth (AWS Cognito, eu-west-1) -----------------------------------
API_BASE = "https://mobile.gluehome.com/api/v1"
AWS_REGION = "eu-west-1"
COGNITO_POOL = "eu-west-1_hpPMCTv6Q"
# Android app client (has both id+secret in the APK). iOS client differs and its
# secret is not public, so we use the Android pair.
COGNITO_CLIENT = "65tpra3b7ktm1ivv9a8nmop15p"
COGNITO_SECRET = "20bekvv0vfsv8lqltekcjgj8fu0j7j6miqrsj4b4c4fubech1ns"

# --- BLE GATT (lock) ---------------------------------------------------------
SVC_LOCK = "e4b264d7-4e56-424f-8b5c-d4089ea216ef"
CH_LOCK_PROTOCOL = "e4b264d7-4e56-424f-8b5c-d4089ea216ef"
CH_WAKE_UP = "94a8dfd9-49f7-4365-95d2-d2b94624b72c"

# --- Keychain service-name convention ----------------------------------------
# service = claude/gluehome/<purpose>, account = $USER
# (override with GLUE_KC_PREFIX, e.g. "glue" for a non-claude-managed machine)
import os as _os
KC_PREFIX = _os.environ.get("GLUE_KC_PREFIX", "claude")
