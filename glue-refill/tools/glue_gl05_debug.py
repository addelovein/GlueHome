#!/usr/bin/env python3
"""GL05 diagnostic probe — gathers everything we need to add GL05 support.

Run this AT the lock, from inside the GlueHome-main folder, AFTER `glue.py setup`
(so the lock is selected and keys are banked):

    python3 tools/glue_gl05_debug.py

What it does, and why it is safe:
  * Dumps the full GATT table (every service + characteristic UUID + properties).
    The GL04A GATT is known; the GL05A may differ, and the firmware needs the
    real UUIDs. No key is consumed by this part.
  * Reserves exactly ONE offline key, logs in ONCE, and in that single session
    probes read-only opcodes (firmware, battery, bolt position, door state,
    device time). It never sends UNLOCK or LOCK — the bolt does not move.
  * Prints the raw status byte of every probe, even failures, so a
    NOT_IMPLEMENTED / rejected opcode is just as informative as a success.

Copy the ENTIRE output back to whoever is adding GL05 support.

The one key this burns is reported + refilled automatically on the next
`glue.py auth refill` (or by the HA add-on), exactly like a normal unlock.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

# Make the sibling tools importable whether run as `tools/glue_gl05_debug.py`
# or from inside tools/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import glue_ble as gb  # reuses the validated transport (frames, CRC, session)
from glue_keys import identity, KeyBank
from glue_storage import locked


SEP = "=" * 70


def _line(label, value=""):
    print(f"{label:<28} {value}")


# ---- opcodes we probe (all read-only; none move the bolt) --------------------
# From writeup §9 (DeviceCommand.java). We send each and print the raw status so
# a GL05 that answers an opcode GL04 refused shows up immediately.
PROBES = [
    # (name, opcode, payload, note)
    ("GET_FIRMWARE_VERSION", 0x60, b"", "firmware string"),
    ("BATTERY_LEVEL", 0x57, b"", "battery payload"),
    ("BOLT_POSITION_ENABLE", 0x75, bytes([0xAA]), "enable bolt sensor (GL05 feature)"),
    ("BOLT_POSITION_GET_STATE", 0x76, bytes([0x01]), "RETRACTED=0xA0 / PROTRUDED=0xA1"),
    ("DEVICE_GET_TIME", 0xA0, b"", "GL04 returned NOT_IMPLEMENTED"),
    ("GET_ACCESS_LIST_VERSION", 0x56, b"", "command-list version on device"),
]


async def _probe_opcode(sess, name, opcode, payload, note):
    """Send one opcode, print status + payload, never raise (diagnostic)."""
    try:
        status, full = await sess._send(opcode, payload, timeout=6.0)
        meaning = gb.RESP.get(status, f"status={status}")
        rest = full[1:].hex() if len(full) > 1 else ""
        bolt = ""
        if opcode == 0x76 and len(full) > 1:
            bolt = f"  -> {gb.BOLT.get(full[1], 'UNKNOWN bolt byte')}"
        _line(f"  {name}", f"status=0x{status:02x} ({meaning}) payload={rest or '-'}{bolt}")
        print(f"      [{note}]")
        return status
    except Exception as e:
        _line(f"  {name}", f"NO/INVALID RESPONSE: {type(e).__name__}: {e}")
        print(f"      [{note}]")
        # A failed send poisons the session's framing; caller re-logs if needed.
        return None


async def dump_gatt(client):
    print(SEP)
    print("GATT TABLE (copy this whole block)")
    print(SEP)
    for svc in client.services:
        print(f"service {svc.uuid}")
        for ch in svc.characteristics:
            props = ",".join(ch.properties)
            print(f"    char {ch.uuid}  [{props}]")
    # Flag which of the known GL04 chars are present.
    known = {
        gb.CH_LOCK_PROTOCOL: "LOCK_PROTOCOL",
        gb.CH_WAKE_UP: "WAKE_UP",
        gb.CH_HOST_READY: "HOST_READY",
    }
    all_uuids = {c.uuid.lower() for s in client.services for c in s.characteristics}
    print("-" * 70)
    for uuid, nm in known.items():
        present = "PRESENT" if uuid.lower() in all_uuids else "** MISSING **"
        _line(f"  {nm}", present)
    print(SEP)


async def run():
    print(SEP)
    print("GLUE GL05 DIAGNOSTIC PROBE")
    print(SEP)
    lock_id, serial = identity()
    _line("Selected lock id", lock_id)
    _line("Selected serial", serial)
    _line("Model (from serial)", serial.split(".")[0] if "." in serial else "?")
    print(SEP)

    _, BleakClient = gb._require_bleak()
    dev = await gb.find_device()
    _line("BLE device name", dev.name)
    _line("BLE address", dev.address)

    bank = KeyBank(lock_id)
    with locked("ble-" + lock_id):
        async with BleakClient(dev) as client:
            await dump_gatt(client)

            sess = gb.GlueSession(client)
            await sess.start()
            await sess.wake()
            print(f"Woke lock. wake_ack={'yes' if sess._ready.is_set() else 'FALLBACK (no HOST_READY ack)'}")

            idx, key = bank.reserve()
            _line("Reserved key index", idx)
            status = await sess.login(key)
            _line("LOGIN status", f"0x{status:02x} ({gb.RESP.get(status, status)})")
            if status != 0:
                print("\nLOGIN was rejected — the probes below would fail too.")
                print("If this is KEY_EXPIRED (0x22), run `glue.py auth refill` and rerun.")
                return

            print("\n--- read-only opcode probes (bolt does NOT move) ---")
            for name, opcode, payload, note in PROBES:
                st = await _probe_opcode(sess, name, opcode, payload, note)
                # If the session framing got poisoned by a rejected opcode,
                # re-establish wake+login so the remaining probes are meaningful.
                if sess._failed:
                    print("      (session framing reset; re-logging in to continue probes)")
                    break

            print(SEP)
            print("DONE. Copy everything above this line back.")
            print(SEP)

    # Report the burned key + refill, same as a real operation.
    try:
        import glue_auth
        await asyncio.to_thread(bank.sync, glue_auth.config())
        print("Key reported + bank refilled.")
    except (Exception, SystemExit):
        print("Refill pending; run `python3 tools/glue.py auth refill` when online.",
              file=sys.stderr)


def main():
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
