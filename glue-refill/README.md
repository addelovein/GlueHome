# Glue key-refill — Home Assistant add-on

Keeps your Glue lock's keys topped up so it keeps working in Home Assistant.
You install it once, fill in a short form, and press Start. No terminal, no files
to copy.

## Install (one click)

1. In Home Assistant: **Settings → Add-ons → Add-on Store**.
2. Top-right **⋮ menu → Repositories**.
3. Paste this repository's URL and press **Add**, then close:

   ```
   https://github.com/addelovein/GlueHome
   ```

4. Back in the store, scroll to **Glue lock add-ons → Glue key-refill**, open it,
   and press **Install**. (The first install takes a few minutes while it builds.)

## Set it up

Open the add-on's **Configuration** tab and fill in the boxes:

| Box | What to type |
| --- | --- |
| Glue username | The **canonical username** `glue.py setup` printed (an account UUID or phone) — **not your email** |
| Glue password | Your Glue app password |
| Lock id | From the one-time `glue.py setup` run (a long UUID) |
| Lock serial | Your lock's serial, e.g. `GL04A.DL...` |
| ESP device | Your ESPHome node name, e.g. `gluelock-esp32` |
| Bank sensor | The bank status entity, e.g. `sensor.<node>_glue_bank_status` |
| Storage key | Run `openssl rand -base64 32` once and paste the whole string (incl. the trailing `=`). **Keep it the same forever.** |
| HA token / HA url | **Leave blank.** The add-on reaches Home Assistant through the Supervisor automatically. Only fill these to point at a non-add-on HA. |

Press **Save**, then the **Info** tab → **Start**. Turn on **Start on boot** and
**Watchdog** so it runs itself.

Open the **Log** tab: `Reconciled` means it's working. `Device busy/offline` just
means the ESP32 isn't connected yet — harmless.

That's it. Your lock now stays supplied with keys and works from Home Assistant.

---

## What it does and why it exists

Your Glue lock uses one-time keys. This add-on logs into your Glue account,
reports used keys, and sends fresh ones to the little ESP32 by your door — all
through Home Assistant. The ESP32 never holds your password and never talks to
the internet; this add-on handles that part safely on your HA box.

**Heads-up:** once this add-on runs, it "owns" the keys, so the `glue.py ble`
command on your computer stops unlocking (so the two can't fight over the same
one-time key). To unlock from your computer again, stop this add-on and run
`python3 tools/glue.py sync --controller ble --watch`.

**Self-healing when keys go stale:** if you unlock with the Glue phone app, it
burns a key the ESP32 was holding. The ESP32 automatically tries its next key
(up to 3) on the spot; if all are stale it flags the bank and Home Assistant can
refill and retry. Optional automations for that are in
[`HA_AUTOMATIONS.md`](https://github.com/addelovein/GlueHome/blob/main/HA_AUTOMATIONS.md).
A lower **interval_seconds** reacts faster to a stale bank. It does **not** poll
Glue more: the worker only calls Glue after an actual unlock — a full, idle bank
makes zero Glue cloud calls, no matter the interval.

**Not magic forever:** it depends on Glue's servers still being online to fetch
new keys. If Glue's servers die, only the keys already on the ESP32 remain.

See `ESPHOME_SETUP.md` in the repository for flashing the ESP32 itself.
