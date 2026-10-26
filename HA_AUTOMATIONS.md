# Home Assistant automations for the Glue lock

Optional automations that make the lock self-heal when its keys go stale (for
example because you unlocked with the Glue phone app, which burns a key the ESP32
was still holding).

## How recovery works

The recovery is layered, fastest first — the slow cloud step only runs if the
fast offline steps fail:

1. **On the ESP32 (instant, offline):** if a key is rejected as `KEY_EXPIRED`,
   the firmware automatically tries the next banked key in the same Bluetooth
   session, up to 3 keys. A normal stale key never reaches Home Assistant.
2. **The firmware flags the bank (only if all 3 tries were stale):** it sets
   `"stale":true` in the `Glue bank status` sensor and the lock goes to an
   unknown state instead of opening.
3. **Home Assistant refills and retries (the automations below):** the refill
   add-on pulls fresh keys from Glue and pushes them to the ESP32, then the
   unlock is re-fired.

So a single stale key is invisible and instant; only a wholly stale bank (you
burned several keys elsewhere) falls through to the cloud refill.

## Prerequisites

- The ESPHome node is in HA and exposes:
  - a lock entity, e.g. `lock.entrance`
  - `sensor.glue_bank_status` (the JSON bank status)
  - `sensor.glue_keys_remaining`
- The **Glue key-refill add-on** is installed and running. It reconciles HA state
  on its loop but only calls Glue's cloud after an actual unlock (a full, idle
  bank makes zero Glue calls). A lower `interval_seconds` just makes it react to a
  stale bank faster; it does not increase cloud polling.

## 1. Expose "stale" as its own sensor (helper)

Add a template binary sensor so automations can trigger on it cleanly. In your
HA `configuration.yaml` (or a package):

```yaml
template:
  - binary_sensor:
      - name: "Glue bank stale"
        unique_id: glue_bank_stale
        state: >
          {{ (state_attr('sensor.glue_bank_status','stale') if false
              else (states('sensor.glue_bank_status')|from_json).stale) | default(false) }}
```

If `from_json` on the state is awkward in your HA version, simplest alternative:
trigger directly on the raw sensor state containing `"stale":true` (used below).

## 2. Self-heal: on a stale bank, refill then retry the unlock

This fires when the lock failed to open because its keys were stale. It waits for
the refill add-on to replenish keys (it runs on its own interval), then re-issues
the unlock once.

```yaml
automation:
  - alias: "Glue: refill and retry on stale keys"
    mode: single
    trigger:
      # Bank went stale (all offline retries were spent keys).
      - trigger: state
        entity_id: sensor.glue_bank_status
        # Fire whenever the status updates to a stale bank.
    condition:
      - condition: template
        value_template: "{{ '\"stale\":true' in states('sensor.glue_bank_status') }}"
    action:
      # The refill add-on reconciles on its own timer; wait until it has pushed
      # fresh available keys back to the ESP32 (bounded wait).
      - wait_template: >
          {{ (states('sensor.glue_keys_remaining') | int(0)) > 0
             and '\"stale\":true' not in states('sensor.glue_bank_status') }}
        timeout: "00:01:00"
        continue_on_timeout: false
      # Keys are fresh again — retry the unlock once.
      - action: lock.unlock
        target:
          entity_id: lock.entrance
```

Notes:

- `continue_on_timeout: false` means: if the refill never arrives (e.g. Glue's
  cloud is down), give up quietly rather than firing an unlock that will also
  fail. The door simply stays locked — fail-safe.
- `mode: single` prevents overlapping retries if the sensor flaps.
- This retries **once**. If you want it to also notify you when recovery fails,
  add an `else` branch on the wait (a `notify` action).

## 3. (Optional) Low-key warning

Let yourself know before the pool runs dry — e.g. if the cloud has been down and
keys are not refilling:

```yaml
automation:
  - alias: "Glue: warn when keys are low"
    trigger:
      - trigger: numeric_state
        entity_id: sensor.glue_keys_remaining
        below: 2
    action:
      - action: notify.notify
        data:
          message: "Glue lock keys low ({{ states('sensor.glue_keys_remaining') }} left). Check that the refill add-on can reach Glue."
```

## What this does NOT fix

If Glue's cloud is **down**, no fresh keys can be fetched, so a fully stale bank
can't be refilled — the retry times out and the door stays locked (fail-safe).
Your only keys are the unused ones already on the ESP32. This is the fundamental
5-key / cloud-dependency limit, not something an automation can engineer around.
Keep one primary controller (the ESP32) and use the phone app sparingly so the
ESP32's bank rarely goes stale in the first place.
