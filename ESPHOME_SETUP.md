# ESPHome setup — Glue lock in Home Assistant

End goal: your Glue lock appears as a `lock` entity in Home Assistant, driven by
a small ESP32 over BLE, with Home Assistant refilling the single-use key pool.

**Status of this path:** the firmware code compiles and validates, the BLE
protocol is proven on real hardware (from a Mac), but the end-to-end run *on an
ESP32* has not been done yet. Expect to debug the first flash. The three things
ESPHome can't do for you are called out as **MANUAL** below.

Two separate systems do two separate jobs:

| Tool | Retrieves your lock IDs / keys? | Job |
| --- | --- | --- |
| `glue.py setup` (this repo) | **Yes** | Logs into Glue, discovers your lock, pulls keys, writes the ESPHome YAML with your lock ID filled in |
| ESPHome | No | Compiles and flashes firmware to the ESP32 |
| `glue.py sync` (this repo) | — | Runs alongside HA: pushes keys to the ESP32 and reports burns |

ESPHome never talks to Glue's cloud. Credentials and keys live on your
machine / HA, never on the ESP32.

---

## What you need

- An **ESP32 dev board** (e.g. `esp32dev` / ESP32-WROOM, ~$5–10) and a USB cable.
  Plain ESP32; not required to be ESP32-S3/C3. The board sits within BLE range of
  the lock (same room / through one wall).
- **Home Assistant** running (any install type), to hold keys and show the lock.
- **Python 3.10+** on a computer, to run `glue.py setup` and the refill worker.
- Your **Glue account** login (email/phone + password).

---

## Step 1 — Install ESPHome

Pick one:

- **HA add-on (easiest if you run HA):** Settings → Add-ons → Add-on Store →
  ESPHome → Install → Start. Use its web UI for the flash in Step 5.
- **Standalone CLI:** `pip install esphome` (Python 3.10+). Flash from a terminal.
- **Docker:** `ghcr.io/esphome/esphome`.

Any of these is a few minutes. This is the "easy install" part.

## Step 2 — Discover your lock and generate the config (automatic)

From this repo, on a computer with your Glue login:

```sh
python3 -m pip install -r requirements.txt
python3 tools/glue.py setup
```

This signs you in (Cognito SRP), lists your locks, lets you pick one, pulls your
5-key pool, stores credentials + lock identity in your OS keychain, and writes:

```
esphome/glue-<serial>.yaml      # your lock ID is already baked in
```

You do **not** type any lock ID by hand. An existing file is never overwritten —
a numbered candidate is written instead.

## Step 3 — Fill in Wi-Fi and API secrets  (MANUAL)

The generated YAML references `!secret` values. Create `esphome/secrets.yaml`
(already git-ignored) next to the config:

```yaml
wifi_ssid: "YourWiFiName"
wifi_password: "YourWiFiPassword"
api_encryption_key: "GENERATE_ME"      # see below
```

Generate a real API key (the native-API encryption HA uses to talk to the node):

```sh
openssl rand -base64 32
```

Paste that as `api_encryption_key`. Do **not** use an all-zeros key — recent
ESPHome rejects it.

## Step 4 — First flash (gets you the lock's BLE MAC)  (MANUAL, chicken-and-egg)

The generated YAML has a placeholder MAC: `mac_address: AA:BB:CC:DD:EE:FF`.
ESPHome needs the lock's real hardware MAC, which a Mac BLE scan can't give you
(macOS returns a CoreBluetooth UUID, not the MAC). The ESP32 itself finds it:

1. Flash the node once *as-is* (placeholder MAC is fine for this step):
   - HA add-on: open the config, click **Install → Plug into this computer** (or
     wirelessly after the first USB flash).
   - CLI: `esphome run esphome/glue-<serial>.yaml` (connect the ESP32 by USB).
2. Watch the node's **logs** for the `esp32_ble_tracker` lines. Find the device
   whose name is your lock serial (`GL04A.DL…`); its MAC is printed beside it,
   e.g. `Found device AA:BB:CC:DD:EE:FF  …  GL04A.DL…`.
   - Alternatively, scan with an Android app like **nRF Connect** and read the MAC
     there.
3. Put that real MAC into `mac_address:` in the YAML, save, and **flash again**.

After this, the node connects to the lock on boot.

## Step 5 — Add the node to Home Assistant

HA usually auto-discovers the ESPHome node (Settings → Devices & Services →
a discovered **ESPHome** entry → Configure). It asks for the encryption key —
that's the `api_encryption_key` from Step 3. Once added you'll see:

- `lock.entrance` (or your lock's name) — the lock control
- `sensor.glue_keys_remaining`
- `sensor.glue_bank_status`

The lock will show state but can't operate yet — it has no keys. That's Step 6.

## Step 6 — Push keys to the ESP32, keep them refilled  (the refill worker)

The firmware holds no credentials by design; HA (via this repo's worker) pushes
keys and reports each burn. Set these for the worker — environment or the secret
store (`python3 tools/glue_store.py set <name>` prompts without echo):

| Variable | Value |
| --- | --- |
| `GLUE_HA_URL` | Your HA base URL (HTTPS; HTTP only on loopback) |
| `GLUE_HA_TOKEN` | An HA long-lived access token (Profile → Security) |
| `GLUE_HA_DEVICE` | The ESPHome node name, e.g. `glue-<serial>` |
| `GLUE_HA_BANK_ENTITY` | The bank sensor entity, e.g. `sensor.glue_bank_status` |

(`GLUE_LOCK_ID`, `GLUE_LOCK_SERIAL`, `GLUE_USERNAME`, `GLUE_PASSWORD` are already
stored by `setup`. On a headless worker with no OS vault, also set
`GLUE_STORAGE_KEY`.)

Then run the worker on a host that can reach HA:

```sh
python3 tools/glue.py sync          # one reconciliation (never moves the motor)
python3 tools/glue.py sync --watch  # reconcile / retry every 15s
```

Run `--watch` under a supervisor (systemd, Docker restart policy, HA
`shell_command`) so it stays up. It reads the node's pending burns, reports them
to Glue, acknowledges them to the node, and delivers replacement keys so the bank
self-heals toward 5. The first successful handoff records **ESPHome as the owner**
of that lock's key bank and disables direct CLI unlocks — this is deliberate, so
Python and the ESP32 never reuse the same single-use key.

Now unlocking from HA (or an HA automation) drives the lock.

## Step 7 — (optional) door sensor / auto-lock

See `ESPHOME_COMPONENT_PLAN.md` §11. A door reed switch on a GPIO lets HA report
open/closed and optionally auto-act on close. **Caveat:** every offline
lock/unlock burns a key; with only 5 and a server-side cap, auto-acting on every
close can drain the pool if the cloud is down. Keep auto-act online-only or
rate-limited.

---

## Switching control back to the Mac/Python path

Because one controller owns the bank at a time:

```sh
# stop the regular worker first, then:
python3 tools/glue.py sync --controller ble --watch       # hand ownership to Python
# ... later, hand back to the ESP32:
python3 tools/glue.py sync --controller esphome --watch
```

These retire unused keys and provision fresh ones; they need cloud/HA access and
never move the motor.

## Reality check

- **Not Glue-proof.** This depends on Glue's API staying up to refill keys. When
  the API dies, you have only the keys already on the ESP32, then the offline
  path stops. True independence is the hardware/SWD track (`…WRITEUP.md` §10),
  not started.
- **State is confirmed-operation based**, not continuous bolt sensing (this GL04's
  bolt sensor is disabled in firmware). "Locked"/"unlocked" reflects the last
  successful command, plus any door sensor you add.
