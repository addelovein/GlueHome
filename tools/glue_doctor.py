#!/usr/bin/env python3
"""glue_doctor — one-shot diagnostics. Run it, paste the ENTIRE output back.

    python tools/glue_doctor.py

It figures out the things we otherwise keep guessing and prints them plainly:

  1. Selected lock (id, serial, model) from your stored setup.
  2. Your Home Assistant entities + ESPHome services — so we know the REAL node
     name and the REAL bank-sensor entity id (no more typos in the add-on form).
  3. The exact values the add-on's Configuration form should contain, already
     validated against the add-on's own rules (it tells you which field is wrong
     and why — e.g. the ESP-device field must be lowercase).

It is read-only: it never unlocks, never locks, never changes HA. No key is
consumed. Secrets (passwords, keys) are NOT printed.

Home Assistant access: it uses the same connection the add-on uses. If you run
this on the HA box it finds the Supervisor token automatically. Otherwise set
two environment variables first (your HA URL and a long-lived token):

    Windows (cmd):   set GLUE_HA_URL=http://homeassistant.local:8123
                     set GLUE_HA_TOKEN=xxxxxxxx
    Mac/Linux:       export GLUE_HA_URL=http://homeassistant.local:8123
                     export GLUE_HA_TOKEN=xxxxxxxx

If you can't get a token, that's fine — it still prints sections 1 and 3.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

SEP = "=" * 72


def hdr(t):
    print("\n" + SEP + "\n" + t + "\n" + SEP)


def kv(label, value=""):
    print(f"{label:<26} {value}")


# ---- the add-on's real schema rules, mirrored so we can explain failures -----
# Must stay in step with glue-refill/config.yaml `schema:`.
ADDON_RULES = [
    ("ha_device", r"^[a-z0-9][a-z0-9_-]*$",
     "ESP device = your ESPHome NODE name: lowercase letters/digits with hyphens "
     "or underscores, no capitals, no spaces, no dots."),
    ("ha_bank_entity", r"^sensor\.[a-z0-9_]+$",
     "Bank sensor = a sensor entity id like sensor.<node>_glue_bank_status "
     "(lowercase, underscores)."),
    ("lock_id", r"^[0-9a-fA-F-]{36}$",
     "Lock id = the 36-char UUID (8-4-4-4-12 hex), not the serial, not your email."),
]


# =====================================================================
# 1. Selected lock
# =====================================================================
def section_lock():
    hdr("1. SELECTED LOCK (from your setup)")
    try:
        from glue_keys import identity
        lock_id, serial = identity()
        model = serial.split(".")[0] if "." in serial else "?"
        kv("lock_id", lock_id)
        kv("lock_serial", serial)
        kv("model", model + ("   <-- GL05 (newer than the test unit)" if model.upper().startswith("GL05") else ""))
        # Bank state (counts only, no key material).
        try:
            from glue_keys import KeyBank
            bank = KeyBank(lock_id)._read()
            kv("keys banked", len(bank.get("keys", [])))
            kv("pending (to report)", bank.get("pending", []))
            kv("retired_through", bank.get("retired_through"))
        except Exception as e:
            kv("bank", f"(could not read: {e})")
        return lock_id, serial
    except Exception as e:
        kv("ERROR", f"No lock selected. Run `glue.py setup` first. ({e})")
        return None, None


# =====================================================================
# 2. Home Assistant entities + ESPHome services
# =====================================================================
def _env(name):
    # On Windows cmd, `set X="..."` keeps the quotes in the value, so a URL can
    # arrive as '"http://...' and break urlsplit. Strip surrounding quotes/space.
    v = os.environ.get(name)
    return v.strip().strip('"').strip("'") if v else v


def _ha_get(path):
    import urllib.request
    url = _env("GLUE_HA_URL")
    token = _env("GLUE_HA_TOKEN")
    sup = os.environ.get("SUPERVISOR_TOKEN")
    if url and token:
        base, bearer = url.rstrip("/") + "/api", token
    elif sup:
        base, bearer = "http://supervisor/core/api", sup
    else:
        raise RuntimeError("no_ha")  # handled by caller
    req = urllib.request.Request(base + "/" + path,
                                 headers={"Authorization": "Bearer " + bearer})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def section_ha(lock_id):
    hdr("2. HOME ASSISTANT: your Glue/ESPHome entities")
    try:
        states = _ha_get("states")
    except RuntimeError:
        print("Skipped — no HA token available.")
        print("To include this, set GLUE_HA_URL + GLUE_HA_TOKEN (see top of file) and rerun.")
        return
    except Exception as e:
        print(f"Could not reach HA: {type(e).__name__}: {e}")
        print("Check GLUE_HA_URL / GLUE_HA_TOKEN, then rerun.")
        return

    # Anything that looks related to the lock: glue/lock entities + bank sensors.
    hits = []
    nodes = set()
    for s in states:
        eid = s.get("entity_id", "")
        low = eid.lower()
        if "glue" in low or low.startswith("lock.") or "bank" in low:
            hits.append((eid, s.get("state", "")))
            # Derive the ESPHome node slug from a bank-status entity id.
            m = re.match(r"sensor\.(.+)_glue_bank_status$", low)
            if m:
                nodes.add(m.group(1))
    if not hits:
        print("No glue/lock/bank entities found. Is the ESP32 added to HA and online?")
    for eid, st in sorted(hits):
        shown = st if len(st) < 60 else st[:57] + "..."
        kv("  " + eid, shown)

    print()
    if nodes:
        for n in sorted(nodes):
            kv(">> ESPHome node name", n + "   (use THIS as 'ESP device' in the add-on)")
    else:
        print(">> Could not derive the node name automatically.")
        print("   Look in the ESPHome dashboard for the node's name (top of the card).")

    # The refill worker pushes keys via esphome.<node>_add_key etc. A 400 on that
    # call means the service name does not exist (wrong node, node offline, or the
    # node YAML does not define the services). List what HA actually exposes.
    print()
    print("ESPHome services HA actually exposes (the worker needs <node>_add_key etc.):")
    try:
        services = _ha_get("services")
        esph = next((d for d in services if d.get("domain") == "esphome"), None)
        names = sorted((esph or {}).get("services", {}).keys())
        if not names:
            print("  ** No esphome services found ** — is the ESPHome integration loaded")
            print("     and the node online? Services only exist while the node is connected.")
        else:
            for nm in names:
                kv("  esphome." + nm)
            needed = ["add_key", "ack_burn", "retire_keys", "resume_keys"]
            for n in sorted(nodes) or [""]:
                print(f"  -- for node '{n}':")
                for suf in needed:
                    want = f"{n}_{suf}"
                    ok = want in names
                    kv(f"     {suf}", f"esphome.{want}  ->  {'PRESENT' if ok else '** MISSING **'}")
    except RuntimeError:
        print("  Skipped — no HA token.")
    except Exception as e:
        print(f"  Could not list services: {type(e).__name__}: {e}")


# =====================================================================
# 3. Add-on config: validate the values
# =====================================================================
def _derive_expected(lock_id, serial):
    """Best-effort expected add-on values from HA + setup, for the user to copy."""
    exp = {"lock_id": lock_id or "", "lock_serial": serial or ""}
    try:
        states = _ha_get("states")
        for s in states:
            eid = s.get("entity_id", "").lower()
            m = re.match(r"sensor\.(.+)_glue_bank_status$", eid)
            if m:
                exp["ha_device"] = m.group(1)
                exp["ha_bank_entity"] = s["entity_id"]
                break
    except Exception:
        pass
    return exp


def section_addon(lock_id, serial):
    hdr("3. ADD-ON CONFIGURATION: correct values + validation")
    exp = _derive_expected(lock_id, serial)
    if exp.get("ha_device") or exp.get("ha_bank_entity"):
        print("Copy these into the add-on Configuration tab (then press Spara/Save):\n")
        kv("  ESP device", exp.get("ha_device", "<node name from section 2>"))
        kv("  Bank sensor", exp.get("ha_bank_entity", "sensor.<node>_glue_bank_status"))
        kv("  Lock id", exp.get("lock_id", "<from setup>"))
        kv("  Lock serial", exp.get("lock_serial", "<from setup>"))
        print("  (Glue username/password + storage_key: as before; not shown here.)")
    else:
        print("Could not auto-derive the ESP device/bank sensor (no HA token).")
        print("The rules below still tell you what each field must look like.\n")

    print("\nField rules (what the add-on will accept):")
    for field, pattern, help_text in ADDON_RULES:
        kv("  " + field, pattern)
        print(f"      {help_text}")
        val = exp.get(field)
        if val:
            ok = re.match(pattern, val) is not None
            kv("      your value", f"{val}  ->  {'OK' if ok else '*** REJECTED ***'}")


# =====================================================================
# 4. (optional) GL05 BLE probe — only if asked, consumes one key
# =====================================================================
def section_ble(lock_id):
    hdr("4. GL05 BLE PROBE (optional) — consumes ONE key, bolt does NOT move")
    print("Skipped by default. To run the BLE diagnostic (stand next to the lock):")
    print("    python tools/glue_gl05_debug.py")


def main():
    print(SEP)
    print("GLUE DOCTOR — copy EVERYTHING below back to whoever is helping you.")
    print(SEP)
    kv("python", sys.version.split()[0])
    kv("platform", sys.platform)
    lock_id, serial = section_lock()
    section_ha(lock_id)
    section_addon(lock_id, serial)
    section_ble(lock_id)
    print("\n" + SEP + "\nEND OF REPORT — paste all of it back.\n" + SEP)


if __name__ == "__main__":
    main()
