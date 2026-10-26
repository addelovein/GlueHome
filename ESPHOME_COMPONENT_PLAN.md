# Glue Lock → ESPHome + Home Assistant — Implementation Plan

# Implementation status (2026-10-15)
This document preserves the original design/research notes. Current implementation
and setup instructions are in `README.md`; remediation and validation evidence
are in `AUDIT.md`. The component targets latest stable ESPHome, minimum 2026.9.1.
Durable key reservations, pending-burn reporting/acknowledgements and an HA REST
refill worker (`glue.py sync --watch`) are now implemented and tested offline.
Older TODO/API-validation statements below are historical. The firmware build,
physical BLE unlock, and end-to-end cloud/HA refill are all **validated on real
hardware** (an ESP32-C3 unlocking the lock from Home Assistant) — treat any
"outstanding / awaits hardware" note further down as historical.

---

Companion to `GLUE_APP_TECHNICAL_WRITEUP.md`. This plans the custom ESPHome external component that exposes the Glue lock (`GL04A.DLXXXX.XXXX`, lock id `<YOUR_LOCK_ID>`) as a native Home Assistant `lock` entity, with the authentication / key-supply architecture spelled out.

Target: operation works with **no cloud at the moment of unlock**; the cloud is touched only to *refill* the single-use key pool, and that side is owned by Home Assistant, not the ESP32.

---

## 1. Responsibility split (the core architectural decision)

Three trust zones. The guiding rule: **the ESP32 never holds your Glue password, and never talks to the cloud.** It only speaks BLE and consumes keys handed to it.

```
┌─────────────────────────────┐     ┌──────────────────────────┐     ┌──────────────┐
│  Home Assistant (trusted)   │     │   ESP32 (near the door)  │     │   The Lock   │
│                             │     │                          │     │   (BLE)      │
│ • Cognito auth → JWT        │     │ • ESPHome custom comp.   │     │              │
│ • GET /keys  (refill pool)  │ ──▶ │ • holds key bank (N keys)│ ──▶ │ wake/login/  │
│ • pushes keys to ESP32      │ MQTT│ • CRC + frame + GATT     │ BLE │ operate      │
│ • holds creds in HA secrets │ /API│ • door reed / bolt sensor│     │              │
│ • reports burns back        │ ◀── │ • reports key consumed   │     │              │
└─────────────────────────────┘     └──────────────────────────┘     └──────────────┘
         cloud side                        offline side                 physical
```

**Why this split:**
- Credentials (Cognito username/password, client secret) live **only** in HA `secrets.yaml`, never flashed to a device that sits physically on a door and could be stolen.
- The ESP32 works **fully offline for unlocking** — it consumes pre-delivered keys. Cloud outage ≠ locked out (until the bank drains).
- Key refill is an HA automation on a schedule, decoupled from the unlock moment.

**Consequence of the 5-key server cap (from writeup §8):** HA can only ever hold ~5 unused keys at a time (the server won't hand out more). So the "bank" is shallow. This is the single biggest constraint and shapes everything below. See §5.

---

## 2. Authentication (Home Assistant side)

The ESP32 is **not** involved here. HA obtains a Cognito **access-token JWT** and uses it as `Authorization: Bearer` against `https://mobile.gluehome.com/api/`.

### 2a. Credential storage
> **Current state:** the tools now use a cross-platform store under keyring
> service **`glue-local`** (macOS Keychain / Windows Credential Locker / Linux
> Secret Service), or an encrypted file for headless use. The `claude/gluehome/*`
> names below are historical (an early local-dev layout).

**Local dev (this machine): macOS Keychain** — the tools read via the `tools/glue` wrapper, nothing in plaintext. Service names (`-a "$USER"`):
- `claude/gluehome/username`, `claude/gluehome/password` — your Glue login (the real secrets)
- `claude/gluehome/cognito-client` = `65tpra3b7ktm1ivv9a8nmop15p` (Android app client)
- `claude/gluehome/cognito-secret` = Android client secret (`strings.xml`; app constant, not a user secret)
- `claude/gluehome/cognito-pool` = `eu-west-1_hpPMCTv6Q`

> **Use the Android client pair.** There are two app clients on the same pool (Android `65tpra3b…` + iOS `4og3rhns…`); each id has its own secret and `SECRET_HASH` must pair id↔secret correctly. We have both halves only for Android.

**On the HA host:** mirror these into HA `secrets.yaml` (`glue_username`, etc.) or a Keychain-equivalent/secret store on that box. **Never** put any of this on the ESP32.

### 2b. Token acquisition — two options

**Option 1 — live Bearer capture (no password stored, manual).**
You capture a fresh Bearer token in Charles and paste it into an HA `input_text` helper. TTL = 60 min, so this is only for testing / occasional use, not unattended operation. No credentials at rest.

**Option 2 — Cognito self-auth (unattended, recommended for a real deployment).**
HA mints tokens itself via AWS Cognito `InitiateAuth`. Flow = `USER_PASSWORD_AUTH` (or `USER_SRP_AUTH`) against `cognito-idp.eu-west-1.amazonaws.com`, pool `eu-west-1_hpPMCTv6Q`, with the app client id + **client secret** (the secret means every call needs a `SECRET_HASH` = Base64(HMAC-SHA256(username+clientId, clientSecret))).

Implementation choices for Option 2, best first:
- **A small HA `pyscript` / AppDaemon / custom `shell_command`** running a ~40-line Python script using `boto3` (`cognito-idp.initiate_auth`) or raw `requests` to the Cognito JSON API. Returns `AccessToken`; cache it with its `exp`; refresh on demand. This is the clean path — boto3 handles `SECRET_HASH` and the `RefreshToken` grant so you re-auth with the password only rarely.
- A standalone companion container (tiny FastAPI) that HA calls — if you'd rather keep it out of HA's process.

> **DECIDED: Option 2 (Cognito self-auth, unattended).** Build with boto3 in an HA `shell_command`/pyscript; Option 1 (manual Bearer capture) kept only as a first-light test fallback.

**Flow nuance (verified in `Cognito.java`):** the app uses `CognitoUserPool(ctx, poolId, clientId, clientSecret, region)` with `AuthenticationDetails(user, password, null)` → AWS **`USER_SRP_AUTH`** (SRP, password never sent in clear). The client secret is present, so **every call needs `SECRET_HASH` = Base64(HMAC-SHA256(username + clientId, clientSecret))**.
Our script will try, in order:
1. **`USER_PASSWORD_AUTH`** via boto3 `initiate_auth` (simplest — one call, boto3 builds nothing SRP). Works **iff** that auth flow is enabled on the app client (`ALLOW_USER_PASSWORD_AUTH`). Many pools allow it even when the app uses SRP.
2. If the pool rejects password auth (`InvalidParameterException`/`NotAuthorized` for flow) → **`USER_SRP_AUTH`** via the `pysrp`/`warrant` SRP helper (or boto3 `AdminInitiateAuth` is not available to a user; use the SRP challenge/response dance). Both still require `SECRET_HASH`.
Either way the result is an **AccessToken + RefreshToken**; subsequent refreshes use `REFRESH_TOKEN_AUTH` (also needs `SECRET_HASH`), so the password is used only on first login / refresh-token expiry.

**✅ VALIDATED LIVE (2026-10-14):** auth works end to end — token mints, `keys` returns the 5-key pool. Two facts learned, baked into the tooling:
- **The pool has `USER_PASSWORD_AUTH` DISABLED** → the **SRP flow is required**. Helper: **`pip install pycognito`** (maintained; `warrant` is broken on Python ≥3.10). The Android client id/secret pair + `SECRET_HASH` are correct.
- **The Cognito username is the `sub` UUID `<YOUR_COGNITO_SUB_UUID>`, NOT the email.** Email is only a profile attribute. Keychain `claude/gluehome/username` holds the UUID.

### 2c. Token lifecycle
- Access token TTL 60 min. Cognito issues a **refresh token** (long-lived) at first login.
- HA stores refresh token (encrypted/secret), uses it to mint new access tokens without re-sending the password. Password is only used on first auth or when the refresh token is rejected.
- On 401 from the Glue API → force a refresh, retry once.

---

## 3. Key refill (Home Assistant side)

An HA automation/script, on a schedule + on-demand:

1. Ensure a valid access token (§2c).
2. `GET /api/v1/glue-osm-devices/{lockId}/keys/` with `Authorization: Bearer …` and header `glue-requires-authorization: true`.
3. Response = up to 5 `{lockId, keyIndex, lockOfflineKeyStr}`. Diff against what the ESP32 reports it already holds; push any **new** indexes to the ESP32 (§4b).
4. Also pull `GET /…/command-list` + `/command-list/version` once; store the command-list blob (the ESP32 relays it to the lock only if the lock's access-list version is behind — see §6).

**Schedule:** the pool only advances when a key is *consumed and reported*, so refilling is event-driven: after each successful unlock-burn, HA re-pulls `/keys` to top the bank back to 5. A periodic (e.g. hourly) pull is a safety net.

---

## 4. The ESP32 custom component (ESPHome, C++)

An **external component** (`esphome::Component` + `esp32_ble_client`). Plain YAML can't express the stateful protocol (CRC, challenge echo, chunking), so the protocol lives in C++.

### 4a. What it ports from the decompiled SDK (writeup §4/§9)
- **CRC-16/CCITT** (poly 0x1021, init 0x0000, little-endian append) — from `O.java`.
- **Frame builder:** `[cmd:1][len:2 LE][challenge:3][payload:N][crc:2 LE]`, `len = payload+8` — from `BLEProtocol$Packet` / `FrameBuilder`.
- **Challenge handling:** seed 3 random bytes; on each reply copy the lock's returned challenge into the next frame (`B5/a.java` semantics).
- **20-byte chunked GATT writes**; reassemble notifications.
- **Sequence:** `WAKE_UP(0x01 raw)` → `LOG_IN(0x19, key)` → `OPERATE(0x32, 0x00 unlock / 0x01 lock)`; parse response status byte → `SUCCESS` / `LockIsLocked(28)` / `LockIsUnlocked(29)` / `MotorNotReady(30)` / `Overcurrent(27)` / `KEY_EXPIRED(34)` / `INCORRECT_CHALLENGE(21)`.
- GATT: lock service `E4B264D7-4E56-424F-8B5C-D4089EA216EF`, `LOCK_PROTOCOL` char (same UUID), `WAKE_UP` char `94A8DFD9-49F7-4365-95D2-D2B94624B72C`.

### 4b. Key bank on the ESP32
- Stored in ESP32 **NVS/flash** (`esphome::ESPPreferences`) so it survives reboots: a small ring of `{keyIndex, keyBytes[112], consumed:bool}`.
- **Receives keys from HA** via a service call (HA → ESP32): an ESPHome `api` user service `add_offline_key(index, b64)` the component registers; HA calls it over the native API (encrypted). Keys never transit MQTT in clear if native-API encryption is on.
- **On unlock:** pick the lowest unconsumed index, use it, mark consumed, persist, and **fire an event back to HA** (`burned_key(index)`) so HA re-pulls the pool. Also fires a sensor update `keys_remaining`.
- `keys_remaining` exposed as a HA sensor → alert/automation when low (e.g. ≤2).

### 4c. Entities exposed to HA
- `lock.glue_entrance` — the lock entity (lock/unlock/open-ish).
- `binary_sensor.glue_door` — door reed (if fitted).
- `binary_sensor.glue_bolt` — internal bolt Hall/reed (if fitted, §7 writeup).
- `sensor.glue_keys_remaining`, `sensor.glue_battery` (from `BATTERY_LEVEL 0x57`), `sensor.glue_rssi`.
- `text_sensor.glue_last_result` (last OPERATE response code) — for debugging.

### 4d. State machine (lock entity)
Precedence (writeup §11): **bolt sensor > OPERATE-response confirmation > door-reed assumption.** Door-open ⇒ report `unlocked`/unavailable, never `locked`.

```
IDLE → (command/auto-trigger) → CONNECTING → WAKE → LOGIN → OPERATE
     → on SUCCESS: set state from the operation; on LockIsLocked/Unlocked: adopt reported state
     → on MotorNotReady/Overcurrent: FAIL (surface to HA, keep prior state)
     → on KEY_EXPIRED/INCORRECT_CHALLENGE: mark key bad, retry next key once, else FAIL
     → DISCONNECT
If bolt sensor present: its GPIO edge updates state independently at any time (truth).
```

---

## 5. The hard constraint: 5-key cap → design implications

- With auto-lock-on-close (writeup §11 Option B), **every close offline burns a key.** 5 keys = 5 closes before the bank is dry and (if cloud is down) you can't refill.
- **Mitigations baked into the plan:**
  - Close behavior is **config-selectable** (always-lock / always-unlock / sensor-only). Default to **sensor-only** to protect the budget; opt into auto-act knowingly.
  - When auto-act is on: only auto-act **if `keys_remaining > threshold` OR cloud reachable**; else just report state, don't burn.
  - HA refills immediately after each burn, so in normal (online) operation the bank self-heals to 5.
  - `keys_remaining ≤ 2` → HA notification.
- **This component is NOT Glue-proof** — it depends on Glue's API staying up to refill. True independence is the hardware/SWD track (writeup §10). State that clearly in the HA dashboard.

---

## 6. Command-list relay (optional, for completeness)

The lock gates operations on an **access-list version**. If the lock's version falls behind the server's, the SDK pushes the new list: `UPDATE_COMMAND_LIST(0x42)` chunks → `COMMIT_COMMAND_LIST(0x43)`. For an offline-only setup this rarely matters (your list is already v72 and valid), but the component should:
- Read lock access-list version (`GET_ACCESS_LIST_VERSION 0x56`) after login.
- If behind the stored command-list version, relay the HA-provided blob. Otherwise skip.
- The blob is server-signed and opaque (can't be forged) — ESP32 is a courier only.

---

## 7. Build & validation order

Status legend: ✅ done/validated offline · ⏳ waiting on physical lock · ⬜ not started

1. ⏳ **`bleak` smoke test** (`tools/glue_ble.py`) — WRITTEN & protocol-validated offline. Run wake→login→operate at the lock with one real key. **Gate: do not finalize C++ BLE until this opens the door.** Also probes `BOLT_POSITION_*`.
2. ✅ **Protocol self-audit** (`tools/test_protocol.py`) — CRC verified byte-for-byte vs `O.java` table (5000 inputs, 0 mismatch); frame round-trip; corrupt-CRC rejection. C++ (`esphome/components/glue_lock/glue_protocol.h`) produces the **identical golden unlock frame** `320900000000008c71` as Python/Java.
3. ⏳ **Cognito auth (HA side)** (`tools/glue_auth.py`) — WRITTEN; `SECRET_HASH` math self-tested. Needs one live run with your creds (`token` → should print a JWT; `keys` → 5 keys). Not lock-dependent — can validate anytime online.
4. 🟡 **ESPHome component v1** (`esphome/components/glue_lock/`) — WRITTEN & API-verified against ESPHome source; protocol header + Python config compile. `lock` entity + BLE `BLEClientNode` + NVS key bank. **Needs: ESPHome toolchain compile + on-device BLE run at the lock.**
5. 🟡 **Key bank + HA key-push service** — done in v1: `add_offline_key()` + the `api.services` `add_key` in the example YAML; `keys_remaining` sensor. Burn-report-back event to HA = TODO.
6. ⬜ **Door reed binary sensor + config-selectable close behavior.**
7. ⬜ **Optional: internal bolt sensor** (during the SWD teardown, writeup §10/§11) → promote state to truth.

**Component files:** `esphome/components/glue_lock/{__init__.py, lock.py, glue_lock.h, glue_lock.cpp, glue_protocol.h}` + `esphome/glue-lock.example.yaml`. Every ESPHome API call cross-checked against `esphome/dev` source (BLEClientNode gattc_event_handler, register_for_notify, get_characteristic/get_gattc_if/get_conn_id, base64_decode sig, LockState enum, make_preference). Verified offline; the only unverifiable-without-hardware bits are the live BLE exchange and ESPHome's full codegen.

**Artifacts built so far (lock not required):**
- `tools/glue_auth.py` — Cognito self-auth + key/command-list puller (HA side).
- `tools/glue_ble.py` — BLE smoke test (run at the lock).
- `tools/test_protocol.py` — offline protocol self-audit (passes).
- `esphome/components/glue_lock/glue_protocol.h` — C++ frame/CRC, cross-checked identical to Python/Java.

---

## 8. Open decisions (for you)

- [x] **Auth route:** DECIDED — Cognito self-auth in HA (boto3, unattended), password in HA `secrets.yaml`.
- [ ] **Where the auth/refill code runs:** HA `pyscript`/`shell_command` vs. a tiny companion container. Default: `shell_command` + boto3.
- [ ] **ESP32 board** + whether native-API encryption is enabled (for key push).
- [ ] **Default close behavior:** sensor-only (protects keys) vs. always-lock. Plan defaults to sensor-only.
- [ ] Confirm the `bleak` smoke test can run near the lock (laptop/Pi in BLE range) before any C++.

---

## Reference (from `GLUE_APP_TECHNICAL_WRITEUP.md`)
- Protocol / opcodes / CRC / frame: §4, §9. Key cap + live findings: §8. State/sensor design: §11. Hardware failsafe: §10. Secrets lookup: §2.
- Live IDs: lock `<YOUR_LOCK_ID>`, serial `GL04A.DLXXXX.XXXX`, fw 0.32/hw 3, command-list v72, no paired hub. API `https://mobile.gluehome.com/api/`, Cognito pool `eu-west-1_hpPMCTv6Q`.
