#!/usr/bin/env python3
"""
glue_setup.py — one-command onboarding for a Glue lock owner.

Does the whole discovery flow so nobody has to hand-extract a lock id, sub UUID,
or figure out that the Cognito username is their phone number:

  1. Authorization gate (you may only operate locks YOU own).
  2. Login (email OR phone) + password → Cognito token (SRP).
  3. Resolve the canonical Cognito username from the returned JWT.
  4. GET /properties → list YOUR locks → pick one.
  5. Pull keys to confirm it works.
  6. Store password + resolved username + lock id/serial in the Keychain.
  7. Emit a filled ESPHome YAML for your lock.

Run:  python3 glue_setup.py            (interactive)
Deps: boto3, pycognito  (pip install boto3 pycognito)
"""
import base64
import getpass
import json
import os
import sys
import re
import unicodedata
from pathlib import Path
from glue_keys import KeyBank, validate_keys

sys.path.insert(0, os.path.dirname(__file__))
import glue_constants as C  # noqa: E402
import glue_auth  # noqa: E402  (reuses its Cognito + API helpers)
import glue_store  # noqa: E402  (cross-platform secret store)

GATE = """\
================================ AUTHORIZATION =================================
This tool operates a physical door lock over its owner's Glue account.

By continuing you confirm that:
  • You are the owner (or have the owner's explicit permission) of the lock and
    the Glue account you are about to sign in to.
  • You will operate ONLY locks you are authorized to operate.

Glue is an abandoned product; this is a self-service tool for your own lock.
It is NOT for accessing anyone else's lock or account.
===============================================================================
Type  I AGREE  to continue: """


def _kc_set(purpose, value):
    return glue_store.set_(purpose, value)  # cross-platform: keyring -> file


def _jwt_username(token):
    payload = token.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload)).get("username")


def main():
    # 1. gate
    if input(GATE).strip().upper() not in ("I AGREE", "I AGREE."):
        sys.exit("Not confirmed — aborting.")

    # 2. login
    print("\nSign in with the SAME credentials you use in the Glue app.")
    login = input("Email or phone (e.g. +4670...): ").strip()
    password = getpass.getpass("Password (not echoed): ")

    # Pool/client/secret default to the bundled public app constants, but an
    # env var or stored value overrides them (same resolution as glue_auth).
    cfg = {
        "user": login, "password": password,
        "pool": glue_auth._env("GLUE_COGNITO_POOL", C.COGNITO_POOL, store_key="cognito-pool"),
        "client": glue_auth._env("GLUE_COGNITO_CLIENT", C.COGNITO_CLIENT, store_key="cognito-client"),
        "secret": glue_auth._env("GLUE_COGNITO_SECRET", C.COGNITO_SECRET, store_key="cognito-secret"),
        "region": C.AWS_REGION,
    }
    print("\nAuthenticating (Cognito SRP)...")
    try:
        token = glue_auth.get_token(cfg, force=True)
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"Login failed: {e}\n(check credentials; email AND phone are valid aliases)")

    # 3. canonical username
    canon = _jwt_username(token) or login
    print(f"Logged in. Canonical Cognito username: {canon}")

    # 4. discover locks
    print("Discovering your locks...")
    props = glue_auth._api_get("properties/", token)
    locks = []
    for p in props if isinstance(props, list) else []:
        for door in p.get("doors", []):
            for lk in door.get("locks", []):
                locks.append({
                    "property": p.get("name"), "door": door.get("name"),
                    "id": lk.get("id"), "serial": lk.get("serialNumber"),
                    "fw": lk.get("firmwareVersion"),
                })
    if not locks:
        sys.exit("No locks found on this account.")
    print("\nYour locks:")
    for i, lk in enumerate(locks):
        print(f"  [{i}] {lk['property']} / {lk['door']}  serial={lk['serial']}  fw={lk['fw']}")
    try:
        idx = 0 if len(locks) == 1 else int(input(f"Pick a lock [0-{len(locks)-1}]: ") or 0)
        if not 0 <= idx < len(locks):
            raise ValueError()
    except ValueError:
        sys.exit("Invalid lock selection")
    lock = locks[idx]
    if not lock["id"] or not lock["serial"]:
        sys.exit("Selected lock is missing its identity")

    # 5. confirm keys
    keys = validate_keys(lock["id"], glue_auth._api_get(f"glue-osm-devices/{lock['id']}/keys/", token))
    KeyBank(lock["id"]).merge(keys)
    # App constants are bundled; the bank is encrypted/keyring-backed.
    print("Stored credentials, identity and offline key bank.")

    # 6. store credentials + lock identity (cross-platform secret store)
    where = glue_store.backend_name()
    print(f"Storing credentials + lock identity ({where})...")
    _kc_set("username", canon)
    _kc_set("password", password)
    _kc_set("lock-id", lock["id"])
    _kc_set("lock-serial", lock["serial"])
    # app constants come from glue_constants.py; only store if user overrode them
    print("Stored. (App constants come from glue_constants.py.)")

    # 7. emit YAML
    out = Path(__file__).resolve().parent.parent / "esphome" / f"glue-{_slug(lock['serial'])}.yaml"
    written = _emit_yaml(out, lock)
    print(f"ESPHome config: {written}")
    print("Next: glue.py ble scan at the lock. Configure its MAC and flash the ESPHome device.")


def _slug(value):
    ascii_value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")[:40] or "lock"


def _emit_yaml(path, lock):
    # JSON double-quoted strings are valid YAML scalars.
    name = _slug(lock["serial"])
    label = json.dumps(lock["door"] or "Entrance", ensure_ascii=True)
    lock_id = json.dumps(lock["id"])
    yaml = f"""# Generated configuration. Existing files are never overwritten.
esphome:
  name: glue-{name}
  min_version: 2026.9.1

external_components:
  - source: {{ type: local, path: components }}

esp32:
  board: esp32dev
  framework: {{ type: arduino }}

logger:
wifi:
  ssid: !secret wifi_ssid
  password: !secret wifi_password
api:
  encryption:
    key: !secret api_encryption_key
  services:
    - service: add_key
      variables: {{ index: int, key: string }}
      then:
        - lambda: 'if (index >= 0) id(glue_hub).add_offline_key((uint32_t) index, key);'
    - service: ack_burn
      variables: {{ index: int }}
      then:
        - lambda: 'if (index >= 0) id(glue_hub).acknowledge_burn((uint32_t) index);'
    - service: retire_keys
      then:
        - lambda: 'id(glue_hub).retire_keys();'
    - service: resume_keys
      then:
        - lambda: 'id(glue_hub).resume_keys();'
ota:
  - platform: esphome

esp32_ble_tracker:
ble_client:
  - mac_address: AA:BB:CC:DD:EE:FF
    id: glue_ble
    auto_connect: true

glue_lock:
  - id: glue_hub
    ble_client_id: glue_ble
    lock_id: {lock_id}

lock:
  - platform: glue_lock
    glue_lock_id: glue_hub
    name: {label}

sensor:
  - platform: template
    name: "Glue keys remaining"
    lambda: 'return id(glue_hub).keys_remaining();'
    update_interval: 5s

text_sensor:
  - platform: template
    name: "Glue bank status"
    lambda: 'return id(glue_hub).bank_status();'
    update_interval: 5s
"""
    path = Path(path)
    # Exclusive creation protects hand-edited configs and symlink targets.
    for n in range(1000):
        candidate = path if n == 0 else path.with_name(path.stem + f".candidate-{n}.yaml")
        try:
            with candidate.open("x", encoding="utf-8") as f:
                f.write(yaml)
            return candidate
        except FileExistsError:
            continue
    raise RuntimeError("Too many existing configuration candidates")


if __name__ == "__main__":
    main()
