# GlueHome audit — 2026-10-15

**Remediation update:** the actionable findings below have been addressed in source and offline regression tests. The ESPHome API finding is corrected: the project targets latest stable ESPHome (2026.9.1 at this review), which does provide `BLEClientBase::register_for_notify()`. The call is retained; the namespace issue is fixed. **Hardware validation since complete:** the firmware has since been built, flashed to an ESP32-C3, and used to unlock the lock end-to-end from Home Assistant, with the key-refill add-on reporting burns and refilling the pool; CI ("Offline and ESPHome checks") builds against latest stable ESPHome and passes. The historical findings below predate that. (Original note, now superseded: "Target compilation and physical/cloud/HA validation remain open, so this is not yet a hardware acceptance sign-off.")

| Original finding | Remediation |
| --- | --- |
| 1: false lock confirmation | Expected-opcode matching; stale replies ignored; one firmware operation per BLE connection |
| 2: wake-write crash | Delayed wake transition with explicit failure handling; no active-key pointer |
| 3: key reuse after failure | Irreversible reservation before LOGIN, atomic process-locked Python bank, synchronous ESP32 persistence; no transmission after persistence failure |
| 4: incomplete refill | Retryable DELETE burn reporting, Python `auth refill`, HA `sync --watch`, device pending/ack status and services |
| 5: ESPHome API | Corrected against 2026.9.1 upstream; current helper retained, namespace fixed, minimum version declared; latest-stable CI compile configured |
| 6: setup/BLE disconnect | Setup provisions shared secure bank; BLE resolves selected serial/id; key ownership validated |
| 7: account cache crossover | Secure cache scoped to user, pool, client, region, API base and credential changes |
| 8: readable exports | Existing extracted directory mode 700 and JSON files mode 600; runtime no longer uses exports; research artifacts ignored |
| 9: Python import | Deferred annotations; regression tests now import and run on pre-3.14 Python |
| 10: shell credential mismatch | Shell shim delegates to the Python dispatcher |
| 11: framing | Strict bounded lengths/CRC, fragmented and concatenated frames handled, corruption fails the session closed |
| 12: active-bank movement | No pointer retained across mutations; ingestion blocked in flight; only acknowledged slots replaced |
| 13: shared persistence | Identity/address-scoped preference key and stored identity verification |
| 14: plaintext token cache | Tokens use the OS vault or AES-GCM encrypted atomic storage; old plaintext cache ignored |
| 15: bolt byte | Response status validated separately from position data |
| 16: setup overwrite | Exclusive config creation with numbered candidates; normalized filenames and quoted labels |

Additional integration safeguard: handing a bank to ESPHome records controller ownership so the Python CLI cannot later reuse keys copied to the device. One worker/store per lock is required. Headless file storage remains available, encrypted using `GLUE_STORAGE_KEY` supplied through the environment/secret manager. Legacy plaintext files are not deleted or silently migrated.

**Checks after remediation:**

- Python 3.14.6: all **24 regression tests pass**, including encrypted-storage round trips/tamper rejection, concurrent reservations in separate processes, and confirmed controller handoff.
- Python 3.9.6 compatibility check: **20 pass, 4 skip** because that interpreter lacks cryptography. The supported runtime baseline is Python 3.10+; the former pre-3.14 import failure is resolved.
- Existing CRC/frame tests pass on both local interpreters; bash syntax and Python syntax checks pass.
- Actual component source passes the C++ host regression harness under AddressSanitizer/UndefinedBehaviorSanitizer: wake failure, duplicate responses, key reservation/reboot, timeout, bank isolation/capacity, failed persistence, corruption and fragmentation. Platform APIs are mocked; this is not a target compile.
- The validator correctly rejects locally installed ESPHome 2026.4.5 as below the required 2026.9.1. Shell network/DNS restrictions prevent installing latest stable and fetching its toolchain. Latest target code generation/build remains **unverified**. Upstream 2026.9.1 BLE registration, descriptor handling, preference and helper APIs were inspected directly.
- Latest-stable compile plus Python/host checks are configured in `.github/workflows/validate.yml`; remote CI has **not** been run. `tools/validate_esphome.py --compile` performs the same build without uploading firmware.
- No real authentication, burn/refill, HA calls, lock operations or firmware uploads were performed during remediation.

The original audit evidence below is historical; line references describe the pre-remediation source.

**Original verdict: not ready for unattended door control or distribution.** The offline frame tests passed on Python 3.14, but operational, onboarding, credential-isolation, and firmware failure-path defects remained.

Scope: all first-party Python tools, bash dispatcher, ESPHome component and example configuration, dependency declaration, README and design claims; selected decompiled Android protocol/API code used as reference. Extracted account data was inspected for schema and permissions without printing secret values. This is not a full audit of the bundled Android application's third-party dependencies or a penetration test of Glue's backend.

No lock commands, cloud authentication, key burns, firmware uploads, or production changes were performed. Application source files were not modified. At the time of this audit the directory had no `.git`; it is now a git repository pushed public at github.com/addelovein/GlueHome, so commit history and worktree state are since tracked. Serena activation created project metadata; validation outputs and synthetic harnesses were placed in temporary directories.

## Findings

P1 = resolve before relying on the affected feature; P2 = correctness/security issue to resolve before general release. Reproductions use synthetic values and mocked transports unless explicitly stated otherwise.

### 1. P1 — Unrelated replies can confirm a lock operation

Locations: `esphome/components/glue_lock/glue_lock.cpp:176-207`; `tools/glue_ble.py:132-138`.

Both implementations parse a command byte but ignore it when advancing the transaction. In C++, a successful LOGIN followed by a duplicate successful LOGIN is interpreted as a successful OPERATE and publishes LOCKED. Python accepts a queued BATTERY success as an OPERATE success. Thus the reported state can claim that a door was locked without a corresponding operation acknowledgement.

**Evidence:** host harness running the actual component source with mocked ESPHome/BLE dependencies produced `duplicate_login_publishes_locked=1`. Python reproduced the unrelated-command acceptance.

**Fix:** correlate responses with the expected opcode and transaction phase; discard stale/unrelated replies; only publish state after a matching operation result. Define reconnect, timeout and delayed-response handling explicitly.

### 2. P1 — A failed wake write dereferences a null key pointer

Locations: `glue_lock.cpp:108-111`, `130`, `222-226` (under `esphome/components/glue_lock/`).

`send_frame_()` handles a failed wake write by calling `fail_op_()`, which clears `active_key_`. Its caller then unconditionally enters LOGIN and dereferences that pointer. A transport failure can therefore crash/reset the ESP32.

**Evidence:** AddressSanitizer/UndefinedBehaviorSanitizer reproduced null member access at line 111 and a segmentation fault using the actual source with an injected failed GATT write.

**Fix:** return and propagate write status; stop the state transition immediately after failure. Wake completion/timing also needs an explicit transition instead of the current immediate fallthrough.

### 3. P1 — Consumed keys remain available after timeout/disconnection

Locations: `tools/glue_ble.py:219-245`; `glue_lock.cpp:187-205`, `222-226`; `glue_lock.cpp:42`.

Python marks a key consumed only after the awaited operation returns; the component likewise defers consumption until an operation response. If LOGIN succeeds but the following response is lost, the lock has consumed the key while the local bank still offers it. A lost LOGIN response creates the same ambiguity. Python also has no atomic reservation or interprocess coordination, allowing concurrent commands to select the same key.

**Evidence:** Python and C++ injected successful LOGIN followed by failure both left the key available. C++ reported `keys_after_login_then_timeout=1`.

Persistence is an additional concern: ESPHome 2026.4.5's installed ESP32 preference backend stages `save()` in memory. The component neither explicitly synchronizes critical reservations nor checks persistence failure, so a sudden power loss can restore older key state. ESPHome documents deferred flash persistence in its [preferences configuration](https://esphome.io/components/esphome/#preferences-component).

**Fix:** durably reserve a key before potentially consuming it, treat uncertain outcomes conservatively, synchronize access, and retain a retryable burn-report record. Never silently return an uncertain key to the available bank.

### 4. P1 — The key-refill lifecycle is incomplete

Locations: `tools/glue_auth.py:208-244`; `glue_lock.cpp:204-219`; `ESPHOME_COMPONENT_PLAN.md:80-90`, `162`.

There is a key-fetch command and an ESPHome key-ingestion service, but no implemented cloud burn acknowledgement, device burn event, or HA refill worker. The recovered SDK's `KeyBurnerWorker` calls a separate endpoint for a specific key index (`decompiled/sources/p082i5/InterfaceC1527p.java:62`). The project documents that this acknowledgement advances the five-key pool.

Fetching `/keys` again cannot substitute for this missing lifecycle. After the initial keys are used, these tools alone cannot maintain continuing operation, even with the backend available. Manually replacing the JSON with the same server response also loses local consumed flags.

**Fix:** implement durable consumed-index reporting, server acknowledgement/retry, bank reconciliation and HA replenishment. Until then, describe the integration as a finite-key prototype.

### 5. P1 — Component calls an API absent from installed ESPHome

**Correction after target clarification:** this was an installed-tooling mismatch, not an incompatibility with the intended target. [ESPHome 2026.9.1 declares the helper](https://raw.githubusercontent.com/esphome/esphome/2026.9.1/esphome/components/esp32_ble_client/ble_client_base.h). Latest stable is the supported target; the original older-installation observation is retained below for provenance.

Location: `esphome/components/glue_lock/glue_lock.cpp:146`.

`this->parent()->register_for_notify(lc->handle)` is not declared by `BLEClient` or `BLEClientBase` in installed ESPHome 2026.4.5. ESPHome's own BLE sensors invoke `esp_ble_gattc_register_for_notify` with interface, remote address and handle. The component also refers to `espbt::ClientState` without declaring the alias in its own namespace; the relevant installed headers scope their aliases inside their respective component namespaces.

**Evidence:** inspected installed headers and reference components. YAML validation and code generation pass, but those steps do not type-check this C++. The real firmware build stopped at a GitHub DNS/toolchain-download failure, so this is a source/API incompatibility finding, not a completed target-compiler diagnostic.

**Fix:** use the supported API and properly qualified namespace, specify a supported ESPHome version, and require a successful target compile. Revisit the documentation's blanket “API-verified” claim.

### 6. P1 — Setup does not provision the BLE workflow it advertises

Locations: `tools/glue_setup.py:103-118`; `tools/glue_ble.py:49-52`, `163-187`.

Setup fetches keys and discards them, saves the chosen lock serial in `glue_store`, and generates YAML. BLE neither reads that store nor fetches those keys: it scans for a hard-coded serial and reads `extracted/offline_keys.json`. `auth keys` only prints JSON. A fresh installation therefore cannot follow setup → BLE unlock as documented; a copied research directory can instead retain the original target/key bank.

**Evidence:** a complete mocked setup generated only YAML. Its stored selected serial did not match the BLE target. No key file was provisioned.

**Fix:** connect both paths to the selected lock identity and a securely provisioned per-lock key bank; validate each key's `lockId`. Make replenishment an explicit supported operation rather than requiring undocumented output redirection.

### 7. P1 — Token cache is not bound to the configured account

Location: `tools/glue_auth.py:168-186`.

`get_token(cfg)` returns any unexpired token in a single global cache before comparing username, pool, client, region or backend. Changing credentials through environment variables or the secret store can continue authenticating as the previous account. Refresh tokens are similarly unscoped.

**Evidence:** a synthetic account-A token was returned for a request configured as account B, without authentication.

**Fix:** namespace/bind caches to canonical account and authentication configuration; invalidate incompatible access and refresh tokens. Do not rely on token expiry alone as an account match.

### 8. P1 — Extracted access keys are readable by other local users

Locations: `extracted/offline_keys.json`, `extracted/offline_keys_1402.json`; `tools/glue_ble.py:176-181`.

Both extracted key files contain per-lock offline-key material and have mode `0644` under traversable directories. Other local users can read those blobs; current validity was deliberately not tested. `.gitignore` reduces accidental Git inclusion but does not protect files on disk, backups or copied project archives. `mark_consumed()` preserves existing permissions.

**Fix:** move key material to protected per-user storage and enforce restrictive permissions before writing. Exclude research account exports and support emails from distributable artifacts. The README's statement that user keys are never in this repository directory does not match this checkout.

### 9. P2 — BLE commands and tests fail on pre-3.14 Python

Location: `tools/glue_ble.py:110`.

The annotation `client: BleakClient` references a name imported only inside `_require_bleak()`. Python 3.14's deferred annotations hide the error, but earlier Python evaluates it while creating the class.

**Evidence:** `/usr/bin/python3 tools/glue.py test` on Python 3.9.6 fails with `NameError: name 'BleakClient' is not defined`. Python 3.14.6 passes. The same import is used by BLE commands.

**Fix:** defer or quote the annotation/use a typing-only import, and declare/test the minimum supported Python version.

### 10. P2 — Bash entry point uses the retired credential store

Locations: `tools/glue:25-47`; `tools/glue_store.py:21`, `86-97`.

Setup now writes keyring service `glue-local`, while the bash `auth` path requires macOS Keychain service `claude/gluehome/*`. A new setup therefore does not satisfy that wrapper. On Linux, the missing `security` command is swallowed and the wrapper reports that setup is missing. Existing legacy items can override newer credentials; explicit environment credentials are overwritten too.

**Fix:** make the shell shim delegate to `glue.py`, preserving arguments and the documented environment → keyring → file resolution.

### 11. P2 — Receive parsing does not enforce framing or recover from corruption

Locations: `tools/glue_ble.py:93-122`; `esphome/components/glue_lock/glue_protocol.h:62-70`; `glue_lock.cpp:176-183`.

Both parsers accept CRC-valid input whose declared length differs from its actual size. Both receive accumulators append indefinitely after a bad frame instead of discarding/resynchronizing. A corrupt frame followed by a valid frame remains unparseable; concatenated frames are not separated. C++ also accumulates notifications while idle without an explicit size limit.

**Evidence:** both parsers accepted a CRC-correct nine-byte frame declaring length 255. Python failed to deliver a valid response after a corrupt one. The recovered SDK parser checks exact declared length.

**Fix:** enforce valid bounded lengths, consume individual complete frames, bound buffers, and define recovery after invalid input.

### 12. P2 — Bank insertion can move an active key and evict unused keys

Location: `esphome/components/glue_lock/glue_lock.cpp:70-81`, `97`.

When full, insertion defaults to evicting slot zero even if no consumed slot exists. Compaction shifts array elements while `active_key_` is a raw pointer into the array. Refilling during an operation can make the eventual consumption mark apply to another key.

**Evidence:** filling eight slots, beginning an operation using index 1, then adding index 9 changed the active pointer's index to 2 in the host harness.

**Fix:** refuse insertion when no safe slot is available; preserve active records; identify the active key by a stable index or independent immutable operation record.

### 13. P2 — Multiple hubs share the same persisted key bank

Locations: `esphome/components/glue_lock/__init__.py:19`; `glue_lock.cpp:20`.

The component allows multiple instances, but every instance uses `fnv1_hash("glue_key_bank")` as its preference identifier. Two locks on one ESP32 overwrite the same persistent bank and can load each other's keys after restart.

**Fix:** derive the preference identifier from stable per-lock identity and validate stored ownership, or explicitly disallow multiple instances until isolation is implemented.

### 14. P2 — Token-cache writes do not repair insecure permissions

Location: `tools/glue_auth.py:102-109`.

`umask(077)` protects newly created cache files only. Existing `0644` cache files remain readable after being overwritten with access and refresh tokens. Writes are non-atomic, follow symlinks, and do not restore the process umask in a `finally` block after an exception.

**Evidence:** a synthetic cache set to `0644` remained `0644` after `_save_cache()`.

**Fix:** use private atomic storage, enforce permissions on the destination, scope and lock concurrent cache updates, and always restore process state after errors. Prefer the existing credential-store abstraction for long-lived refresh tokens.

### 15. P2 — Bolt probe returns status instead of bolt position

Location: `tools/glue_ble.py:156-159`.

`_send()` defines `payload[0]` as the response status. `bolt_probe()` returns that same byte as the bolt position, so a successful response cannot become `0xA0` or `0xA1` through this path.

**Evidence:** a synthetic success payload `00 A1` returned `(0, 0, 0)`. The recovered SDK separates response status from the remaining payload.

**Fix:** verify the command's response layout, validate status and length, then decode the state from the remaining data. Physical GL04 support remains unverified.

### 16. P2 — Re-running setup overwrites manually completed YAML

Locations: `tools/glue_setup.py:117-118`, `123-174`.

Setup deterministically chooses the same per-serial filename and opens it with `w`. Running it again destroys an existing MAC address and any custom pins, automation or network configuration, replacing the MAC with the placeholder. Door/property/serial strings also enter filenames/YAML without adequate normalization or escaping; ordinary quotes or non-hostname characters can produce invalid configuration.

**Fix:** preserve existing files or generate a separate candidate with explicit replacement handling; serialize YAML safely and validate generated names and paths.

## Validation and limits

- PASS: existing protocol test on Python 3.14.6: 64 CRC comparisons against the extracted Java table, frame round trips and corrupt-CRC rejection.
- PASS: Python syntax parsing for all nine first-party Python modules; `bash -n tools/glue`.
- PASS: ESPHome 2026.4.5 example configuration validation and C++ generation, using synthetic Wi-Fi/API values in a temporary copy.
- FAIL: protocol-test import on Python 3.9.6.
- REPRODUCED: Python account-cache crossover, malformed length acceptance, unrelated-response acceptance, post-login timeout key reuse, bolt status misdecode, insecure existing-cache permissions, corrupt-frame recovery failure, and disconnected setup/BLE provisioning.
- REPRODUCED: actual C++ component logic under mocked platform dependencies: null-pointer crash after wake failure, false LOCKED on duplicate login, post-login timeout key reuse, and active-key movement during bank insertion. This harness deliberately supplies platform shims; it is not proof of ESPHome API compatibility or device behavior.
- BLOCKED: real firmware compile; PlatformIO could not resolve/download the GitHub toolchain. No firmware binary was validated.
- NOT PERFORMED: live Cognito/API calls, real key lifecycle, BLE timing/notification registration, physical lock operation, Home Assistant runtime integration, power-loss tests or dependency vulnerability-database audit. Dependencies are unpinned; no reproducible environment or CI matrix is supplied.

The existing tests cover CRC and small frame examples, not the state machine, credential isolation, onboarding, persistence, refill lifecycle or target compilation. Passing them does not substantiate readiness to control a door.

Recommended order: correct reply correlation and failure transitions; make key lifecycle durable and implement burn/refill; repair provisioning and credential isolation; establish an actual supported ESPHome build; then validate with an owned lock and explicit physical-state observations.
