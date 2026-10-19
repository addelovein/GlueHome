# glue-local

Local BLE and ESPHome/Home Assistant control for a Glue smart lock you own or
have explicit permission to operate. Not affiliated with Glue.

Targets **current stable ESPHome, minimum 2026.9.1**, and Python **3.10+**.
The implementation has offline regression coverage; physical BLE operation,
cloud burn/refill, HA integration and a complete target firmware build still
need validation. Do not interpret passing host tests as a verified door lock.

## Direct BLE setup

```sh
python3 -m pip install -r requirements.txt
python3 tools/glue.py setup
python3 tools/glue.py ble scan
python3 tools/glue.py ble unlock
python3 tools/glue.py auth refill
```

`tools/glue` is a bash shim for the same Python entry point on macOS/Linux.
Setup signs in, selects your lock, saves its identity and provisions its keys.
It generates `esphome/glue-<serial>.yaml`; an existing file is preserved and a
numbered candidate is generated instead. Fill in the BLE MAC and Wi-Fi/API
secret references before building firmware. macOS BLE scans return a CoreBluetooth
UUID, not the hardware MAC ESPHome needs; obtain that MAC from ESPHome BLE tracker
logs or a Linux/Android scanner if setting up from a Mac.

`scan` consumes no key. `unlock`, `lock`, `info` and `bolt` reserve one key before
LOGIN. An uncertain result does **not** return it to the available pool. The CLI
attempts burn reporting/refill after each session; `auth refill` retries after
an outage or process crash. A failed operation exits unsuccessfully.

## Credentials and key storage

Environment values override stored values: `GLUE_USERNAME`, `GLUE_PASSWORD`,
`GLUE_LOCK_ID`, `GLUE_LOCK_SERIAL`, and optional Cognito overrides. On desktop,
the `keyring` library stores secrets under service **`glue-local`** in the OS
credential vault. No legacy `claude/gluehome/*` shell lookup is used.

Headless operation remains supported: provide **`GLUE_STORAGE_KEY`**, a stable
base64-encoded random 32-byte key, from your environment/secret manager. This
explicitly selects AES-GCM encrypted storage at
`~/.config/glue-local/secrets.enc` (or beneath `XDG_CONFIG_HOME`). Keep that key
outside the file and retain it across restarts. There is no plaintext fallback.
Writes are atomic and synchronized between processes; on POSIX the directory
is private and files are mode 600. Windows additionally benefits from encryption
and should retain its normal private user-profile ACLs.

Credentials, account-scoped access/refresh tokens and per-lock key banks use this
store. Old `secrets.json`, `.glue_token.json` and research exports are not loaded
implicitly. Re-run setup to provision the new store. Existing plaintext exports
were permission-restricted in this workspace, not deleted. They and the support
emails/decompiled application are local research material, excluded by
`.gitignore`; do not distribute an archive of the entire working directory.

To set an individual stored value without placing it in command history:

```sh
python3 tools/glue_store.py set ha-token  # prompts without echo
python3 tools/glue.py where
```

`auth token` and `auth keys` are explicit secret-export commands: their output
contains credentials/key material. Routine setup/refill/sync does not print it.

## Home Assistant refill worker

The generated ESPHome configuration exposes `add_key`, `ack_burn` and a
**Glue bank status** sensor. The sensor contains only lock identity, pending
burn indexes and available indexes; no key bytes. The component persists a
reservation synchronously before LOGIN, retains unacknowledged burns over
reboots, and isolates each lock's bank. State reflects the last confirmed
operation, not continuous bolt-position sensing.

Configure these values in the worker's environment or credential store:

| Variable | Value |
| --- | --- |
| `GLUE_HA_URL` | Your HTTPS HA base URL; HTTP is allowed only on loopback |
| `GLUE_HA_TOKEN` | HA long-lived access token |
| `GLUE_HA_DEVICE` | ESPHome node name, e.g. `glue-entrance` |
| `GLUE_HA_BANK_ENTITY` | Actual HA entity ID, e.g. `sensor.glue_bank_status` |
| `GLUE_LOCK_ID`, `GLUE_LOCK_SERIAL` | The same selected lock as the ESPHome YAML |
| `GLUE_USERNAME`, `GLUE_PASSWORD` | Glue credentials unless already stored |
| `GLUE_STORAGE_KEY` | Required on a headless worker without an OS vault |

The ESPHome node must first be added to HA with its encrypted native API enabled.
Check the actual entity/action names in HA; names can be prefixed or renamed.
Then run on a host with these credentials and access to HA:

```sh
python3 tools/glue.py sync          # one reconciliation; never moves the motor
python3 tools/glue.py sync --watch  # reconcile/retry every 15 seconds
```

Run `--watch` under your existing process supervisor for automatic restart, or
schedule the one-shot command. The worker reads the device's pending indexes,
reports each burn using the SDK's DELETE endpoint, acknowledges it to the device,
then delivers replacement keys. Failed calls remain pending for retry. HA service
HTTP success is only dispatch acknowledgement; subsequent bank-state polling
confirms device acceptance. No blanket acknowledgement on an API error.

**One controller owns a lock's key bank.** The first successful HA handoff records
ESPHome ownership and disables direct CLI motor operations for that bank; BLE
scanning remains available. This prevents Python and the ESP32 using copies of
the same single-use key. Use one worker/store per lock; do not copy banks between
hosts or independently replenish the same lock from multiple controllers.

To return to direct BLE, stop the regular sync worker and run
`python3 tools/glue.py sync --controller ble --watch`. It durably pauses the
ESP32, retires its remaining keys, reports and acknowledges them, then provisions
fresh keys for Python. It exits when the transfer is confirmed. Return to ESPHome
with `python3 tools/glue.py sync --controller esphome --watch`, then restart the
regular worker. These transfers need working cloud/HA access and retire unused
keys, but never move the motor. Do not manually clear the ownership record.

Glue's server caps the pool at five keys. Backend outages permit only the
remaining local operations. This does not solve permanent cloud independence.

## Validation

```sh
python3 tools/glue.py test
python3 -m unittest discover -s tests -v
clang++ -std=c++17 -DUSE_ESP32 -fsanitize=address,undefined \
  -Itests/host tests/host/test_lock.cpp -o /tmp/glue-tests
/tmp/glue-tests
python3 tools/validate_esphome.py
python3 tools/validate_esphome.py --compile
```

Install the latest stable ESPHome in a separate environment for the last two
commands: `python3 -m pip install --upgrade 'esphome>=2026.9.1'`. The validator
uses a temporary config with synthetic secrets and never uploads firmware.
CI is configured to run Python regressions, host sanitizer checks and a firmware
build against latest stable ESPHome, including a weekly compatibility check.
CI has not been run remotely from this workspace.

See [AUDIT.md](AUDIT.md) for the original findings and remediation evidence.
`GLUE_APP_TECHNICAL_WRITEUP.md` and `ESPHOME_COMPONENT_PLAN.md` are historical
research/design notes; this README and current source describe the implementation.
