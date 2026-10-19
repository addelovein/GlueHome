#!/usr/bin/env python3
"""Direct BLE control using the lock selected by setup.

Commands: scan (no key consumed); unlock, lock, info, bolt (one key reserved).
Keys live in the shared secure per-lock bank. A reservation is never undone:
failed or uncertain operations retain a pending burn for auth refill to report.
All operations require ownership/authorization. Physical BLE is not simulated
by the offline protocol tests; firmware-specific bolt support remains unknown.
"""
from __future__ import annotations
import argparse
import asyncio
import os
import struct
import sys

# bleak is imported lazily (only when actually driving BLE) so the pure protocol
# functions (crc16_ccitt/build_frame/parse_frame) can be unit-tested without it.
def _require_bleak():
    try:
        from bleak import BleakScanner, BleakClient
        return BleakScanner, BleakClient
    except ImportError:
        sys.exit("ERROR: bleak is required for BLE  ->  pip install bleak")

# ---- target identity (live values; override via env if needed) ---------------
# ---- target identity is resolved through glue_keys.identity at command time ----
from glue_keys import identity, KeyBank
from glue_storage import locked

# Resolve identity at command time, after setup; never select a research lock.
MAX_FRAME = 512

# ---- GATT UUIDs --------------------------------------------------------------
# Verified on-device (GL04A fw 0.32): LOCK_PROTOCOL is write+indicate (NOT notify),
# WAKE_UP is write-with-response, and the wake ack (0x01) arrives on HOST_READY (notify).
# All writes must use response=True; write-without-response is silently dropped here.
CH_LOCK_PROTOCOL = "e4b264d7-4e56-424f-8b5c-d4089ea216ef"   # write + indicate
CH_WAKE_UP = "94a8dfd9-49f7-4365-95d2-d2b94624b72c"         # write (with response)
CH_HOST_READY = "8e939cff-3d83-4f61-8eb9-b5f25d39c15c"      # notify (wake ack = 0x01)

# ---- opcodes (writeup §9) ----------------------------------------------------
CMD_WAKE_UP = 0x01
CMD_LOG_IN = 0x19
CMD_OPERATE = 0x32
CMD_BATTERY = 0x57
CMD_GET_FW = 0x60
CMD_BOLT_ENABLE = 0x75
CMD_BOLT_GET_STATE = 0x76
OP_UNLOCK, OP_LOCK = 0x00, 0x01

RESP = {0: "SUCCESS", 21: "INCORRECT_CHALLENGE", 27: "Overcurrent", 28: "LockIsLocked",
        29: "LockIsUnlocked", 30: "MotorNotReady", 34: "KEY_EXPIRED", 18: "LOGIN_ERROR",
        0xFF: "UNKNOWN_ERROR"}
BOLT = {0xA0: "RETRACTED (unlocked)", 0xA1: "PROTRUDED (locked)"}


# ---- CRC-16/CCITT (poly 0x1021, init 0x0000) ---------------------------------
def crc16_ccitt(data: bytes) -> int:
    crc = 0x0000
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def build_frame(cmd: int, challenge: bytes, payload: bytes = b"") -> bytes:
    assert len(challenge) == 3
    length = len(payload) + 8                     # writeup §4: len = payload + 8
    body = bytes([cmd]) + struct.pack("<H", length) + challenge + payload
    crc = crc16_ccitt(body)
    return body + struct.pack("<H", crc)


def parse_frame(buf: bytes):
    """Strict full-frame parser."""
    if not 8 <= len(buf) <= MAX_FRAME:
        return None
    if int.from_bytes(buf[1:3], "little") != len(buf):
        return None
    if crc16_ccitt(buf[:-2]) != int.from_bytes(buf[-2:], "little"):
        return None
    return buf[0], buf[3:6], buf[6:-2]


# ---- session over the LOCK_PROTOCOL characteristic ---------------------------
class GlueSession:
    def __init__(self, client: BleakClient):
        self.client = client
        self.challenge = os.urandom(3)
        self._rx = asyncio.Queue(maxsize=4)
        self._buf = bytearray()
        self._expected = None
        self._failed = False
        self._sending = asyncio.Lock()
        self._ready = asyncio.Event()

    def _on_notify(self, _char, data: bytearray):
        if os.environ.get("GLUE_BLE_DEBUG"):
            print(f"RX[proto] {bytes(data).hex()} (expected={self._expected} failed={self._failed})", file=sys.stderr)
        if self._expected is None or self._failed:
            return
        self._buf.extend(data)
        if len(self._buf) > MAX_FRAME * 2:
            self._buf.clear()
            self._failed = True
            return
        while len(self._buf) >= 3:
            n = int.from_bytes(self._buf[1:3], "little")
            if not 8 <= n <= MAX_FRAME:
                self._failed = True
                self._buf.clear()
                return
            if len(self._buf) < n:
                return
            frame = parse_frame(bytes(self._buf[:n]))
            del self._buf[:n]
            if frame is None:
                # Fail closed; reconnect rather than trusting a resynchronised reply.
                self._failed = True
                self._buf.clear()
                return
            if frame[0] == self._expected and not self._rx.full():
                self._rx.put_nowait(frame)

    async def start(self):
        await self.client.start_notify(CH_LOCK_PROTOCOL, self._on_notify)
        # Wake ack (0x01) is delivered on HOST_READY, a separate notify characteristic.
        await self.client.start_notify(CH_HOST_READY, self._on_ready)

    def _on_ready(self, _char, data: bytearray):
        if data and data[0] == CMD_WAKE_UP and not self._ready.is_set():
            self._ready.set()

    async def wake(self):
        # WAKE_UP has no write-without-response property; a response=False write is
        # dropped by CoreBluetooth. Write with response, then await the HOST_READY ack.
        self._ready.clear()
        await self.client.write_gatt_char(CH_WAKE_UP, bytes([CMD_WAKE_UP]), response=True)
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            # Fall back to a brief settle; some firmwares may ack differently.
            await asyncio.sleep(0.3)

    async def _send(self, cmd, payload=b"", timeout=8.0):
        async with self._sending:
            if self._failed:
                raise RuntimeError("Session failed; reconnect before another command")
            self._buf.clear()
            while not self._rx.empty():
                self._rx.get_nowait()
            self._expected = cmd
            try:
                frame = build_frame(cmd, self.challenge, payload)
                # LOCK_PROTOCOL supports only write-with-response; chunk at the 20-byte
                # ATT payload (MTU 23) and wait for each ack, mirroring the app's long-write.
                if os.environ.get("GLUE_BLE_DEBUG"):
                    print(f"TX[proto] cmd={cmd:#04x} {frame.hex()}", file=sys.stderr)
                for i in range(0, len(frame), 20):
                    await self.client.write_gatt_char(CH_LOCK_PROTOCOL, frame[i:i + 20], response=True)
                loop = asyncio.get_running_loop()
                deadline = loop.time() + timeout
                while True:
                    cmd_r, chal, payload_r = await asyncio.wait_for(self._rx.get(), max(0, deadline - loop.time()))
                    if self._failed:
                        raise RuntimeError("Invalid response framing")
                    if cmd_r != cmd:
                        continue
                    if not payload_r:
                        raise RuntimeError("Missing response status")
                    self.challenge = chal
                    return payload_r[0], payload_r
            except BaseException:
                self._failed = True
                raise
            finally:
                self._expected = None
                self._buf.clear()

    async def login(self, key_bytes: bytes):
        status, _ = await self._send(CMD_LOG_IN, key_bytes)
        return status

    async def operate(self, op: int):
        status, _ = await self._send(CMD_OPERATE, bytes([op]))
        return status

    async def battery(self):
        status, payload = await self._send(CMD_BATTERY)
        if status != 0:
            raise RuntimeError(f"Battery read rejected: {RESP.get(status, status)}")
        return payload[1:]

    async def firmware(self):
        status, payload = await self._send(CMD_GET_FW)
        if status != 0:
            raise RuntimeError(f"Firmware read rejected: {RESP.get(status, status)}")
        return payload[1:]

    async def bolt_probe(self):
        s1, _ = await self._send(CMD_BOLT_ENABLE, bytes([0xAA]))
        if s1 != 0:
            raise RuntimeError(f"Bolt enable rejected: {RESP.get(s1, s1)}")
        s2, payload = await self._send(CMD_BOLT_GET_STATE, bytes([1]))
        if s2 != 0 or len(payload) < 2 or payload[1] not in BOLT:
            raise RuntimeError("Unsupported or invalid bolt-state response")
        return s1, s2, payload[1]


# ---- key bank (local, single-use) --------------------------------------------
# ---- commands ----------------------------------------------------------------
async def find_device():
    BleakScanner, _ = _require_bleak()
    _, serial = identity()
    print(f"Scanning for '{serial}' ...")
    dev = await BleakScanner.find_device_by_filter(
        lambda d, ad: (ad.local_name or d.name or "") == serial, timeout=20.0)
    if not dev:
        raise RuntimeError(f"Lock '{serial}' not found in BLE range")
    return dev


async def cmd_scan():
    dev = await find_device()
    print(f'FOUND name={dev.name} address={dev.address}')


async def _with_session(fn):
    _, BleakClient = _require_bleak()
    dev = await find_device()
    print(f"Connecting to {dev.address} ...")
    async with BleakClient(dev) as client:
        print("Connected. Services:", ", ".join(s.uuid[:8] for s in client.services))
        sess = GlueSession(client)
        await sess.start()
        await sess.wake()
        print("Woke lock.")
        return await fn(sess)


async def _authenticated(fn):
    lock_id, _ = identity()
    bank = KeyBank(lock_id)
    # Lock the entire local BLE session so two CLI processes cannot overlap.
    with locked("ble-" + lock_id):
        async def run(sess):
            idx, key = bank.reserve()
            print(f"Reserved single-use key {idx}; uncertain outcomes will not reuse it.")
            status = await sess.login(key)
            if status != 0:
                raise RuntimeError(f"Login rejected: {RESP.get(status, status)}")
            return await fn(sess)
        try:
            return await _with_session(run)
        finally:
            # Do not mask a BLE failure with a network failure. Pending burns
            # remain durable and auth refill retries them even after a crash.
            try:
                import glue_auth
                await asyncio.to_thread(bank.sync, glue_auth.config())
            except (Exception, SystemExit):
                print("Refill pending; retry with glue.py auth refill", file=sys.stderr)


async def cmd_operate(op_name):
    op = OP_UNLOCK if op_name == "unlock" else OP_LOCK
    async def run(sess):
        status = await sess.operate(op)
        expected = (0, 28) if op == OP_LOCK else (0, 29)
        if status not in expected:
            raise RuntimeError(f"Operation rejected: {RESP.get(status, status)}")
        print(f"{op_name.upper()} confirmed: {RESP.get(status, status)}")
    await _authenticated(run)


async def cmd_info():
    print("Firmware/battery read consumes one key.")
    async def run(sess):
        fw = await sess.firmware()
        bat = await sess.battery()
        print(f"firmware payload: {fw.hex()}   battery payload: {bat.hex()}")
    await _authenticated(run)


async def cmd_bolt():
    print("Bolt probe consumes one key; firmware support is not guaranteed.")
    async def run(sess):
        _, _, val = await sess.bolt_probe()
        print(f"bolt state: {BOLT[val]}")
    await _authenticated(run)


def main():
    ap = argparse.ArgumentParser(description="Glue lock BLE smoke test")
    ap.add_argument("command", choices=["scan", "info", "unlock", "lock", "bolt"])
    args = ap.parse_args()
    runners = {
        "scan": cmd_scan, "info": cmd_info, "bolt": cmd_bolt,
        "unlock": lambda: cmd_operate("unlock"), "lock": lambda: cmd_operate("lock"),
    }
    asyncio.run(runners[args.command]())


if __name__ == "__main__":
    main()