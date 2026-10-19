# Glue key-refill — Home Assistant add-on

A supervised Home Assistant add-on that refills your Glue lock's single-use key
pool. It runs `tools/glue.py sync` on an interval: it reads the ESP32's key-bank
status from HA, reports spent keys to Glue's cloud, and pushes replacement keys
back to the ESP32 over HA's API.

**Why an add-on and not firmware:** refilling requires an AWS Cognito SRP login
with your Glue credentials — heavy crypto plus `boto3`/`pycognito`, and it needs
your account secrets. That cannot and should not live on the ESP32. The add-on
keeps the cloud/credential half on your HA box; the ESP32 only ever holds opaque,
single-use keys and never touches the cloud. See the repo's
`ESPHOME_COMPONENT_PLAN.md` for the full architecture.

This add-on **never operates the lock motor** — it only moves keys.

## Prerequisites

1. The ESPHome Glue node is flashed, added to HA, and exposes the
   `sensor.glue_bank_status` sensor and the `add_key` / `ack_burn` /
   `retire_keys` / `resume_keys` services (see the repo's `ESPHOME_SETUP.md`).
2. You ran `python3 tools/glue.py setup` once to confirm your account works and
   to learn your `lock_id` / `lock_serial`.

## Install (local add-on)

Local add-ons build on your HA machine from a folder you drop into its
`addons` share.

1. **Vendor the CLI into the add-on folder** (copies `tools/` in; the build can
   only see files inside the add-on folder):
   ```sh
   ./vendor.sh
   ```
2. **Copy the whole `glue-refill/` folder** to your HA `addons` share, e.g.
   `\\homeassistant\addons\glue-refill` (Samba add-on) or
   `/addons/glue-refill` on the host.
3. In HA: **Settings → Add-ons → Add-on Store → ⋮ → Check for updates**, then
   find **Glue key-refill worker** under *Local add-ons* and click **Install**.
   The first build downloads Python + deps (a few minutes).

## Configure

On the add-on's **Configuration** tab:

| Option | What to enter |
| --- | --- |
| `ha_url` | `http://homeassistant:8123` works from inside HA; or your HTTPS URL |
| `ha_token` | A Home Assistant **long-lived access token** (Profile → Security) |
| `ha_device` | The ESPHome node name, e.g. `glue-entrance` |
| `ha_bank_entity` | The bank sensor, e.g. `sensor.glue_bank_status` |
| `lock_id` | Your lock UUID (from `glue.py setup`) |
| `lock_serial` | Your lock serial |
| `glue_username` | Your Glue login (the canonical username `setup` printed, or email/phone) |
| `glue_password` | Your Glue password |
| `storage_key` | A **constant** base64 key: run `openssl rand -base64 32` once and keep it |
| `interval_seconds` | How often to reconcile (default 30) |

`storage_key` encrypts the add-on's persistent store on `/data` (the
controller-ownership record and key-bank tombstone). **Keep it the same across
restarts** — changing it loses that state. `ha_token`, `glue_password` and
`storage_key` are write-only password fields.

Start the add-on and watch its **Log** tab. A healthy cycle prints `Reconciled`
or a benign `Device busy/offline` while the ESP32 isn't connected. On the first
successful reconcile the add-on takes **ESPHome ownership** of the key bank,
which disables direct `glue.py ble` unlocks from your Mac (so the two can't reuse
a single-use key). To hand control back to the Mac, stop this add-on and run
`python3 tools/glue.py sync --controller ble --watch` on your computer.

## Notes

- The container needs network only — no USB/BLE. BLE is the ESP32's job.
- Keep `vendor.sh` output fresh: re-run it and rebuild the add-on after changing
  anything in the repo's `tools/`.
- Not Glue-proof: this depends on Glue's cloud staying up to mint tokens and
  refill. When the cloud dies, only the keys already on the ESP32 remain.
