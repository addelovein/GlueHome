#!/usr/bin/env python3
"""
test_protocol.py — offline self-audit of the Glue frame/CRC logic in glue_ble.py.

Runs WITHOUT the lock. Validates the Python CRC + frame builder against the exact
table-driven CRC extracted from the decompiled SDK (O.java), so we know the smoke
test is correct before burning a key at the door.

  python3 test_protocol.py            # expects the decompiled tree at ../decompiled
  python3 test_protocol.py --no-java  # skip the Java cross-check, run internal tests only
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
from glue_ble import crc16_ccitt, build_frame, parse_frame  # noqa: E402

O_JAVA = os.path.join(os.path.dirname(__file__), "..", "decompiled", "sources",
                      "gluehome", "gluetooth", "sdk", "v2", "internals", "ble", "O.java")


def java_table():
    src = open(O_JAVA).read()
    tbl = [int(x) for x in re.search(r"\{([0-9,\s]+)\}", src).group(1).split(",")]
    assert len(tbl) == 256 and tbl[1] == 4129, "unexpected CRC table"
    return tbl


def crc_from_java(table, data):
    crc = 0
    for b in data:
        crc = table[((crc >> 8) ^ (b & 0xFF)) & 0xFF] ^ ((crc << 8) & 0xFF00)
    return crc & 0xFFFF


def main():
    no_java = "--no-java" in sys.argv
    fails = 0
    if crc16_ccitt(b"123456789") != 0x31C3:
        print("FAIL standard CRC-16/XMODEM check vector"); fails += 1

    # 1. CRC cross-check against the real Java table
    if not no_java:
        try:
            tbl = java_table()
            for n in range(0, 64):
                d = bytes((i * 7 + n) & 0xFF for i in range(n))
                if crc16_ccitt(d) != crc_from_java(tbl, d):
                    print(f"FAIL CRC mismatch at len {n}"); fails += 1
            print(f"[ok] CRC matches O.java table over 64 lengths" if not fails else "")
        except FileNotFoundError:
            print("[skip] O.java not found; run with --no-java or from the repo root")

    # 2. frame round-trips (build -> parse)
    for cmd, chal, pl in [(0x32, b"\x01\x02\x03", b"\x00"),
                          (0x19, b"\xaa\xbb\xcc", b"K" * 112),
                          (0x57, b"\x00\x00\x00", b"")]:
        f = build_frame(cmd, chal, pl)
        # length field = payload + 8
        length = f[1] | (f[2] << 8)
        if length != len(pl) + 8:
            print(f"FAIL length field {length} != {len(pl)+8}"); fails += 1
        p = parse_frame(f)
        if p is None or p[0] != cmd or p[1] != chal or p[2] != pl:
            print(f"FAIL round-trip for cmd {cmd:#x}"); fails += 1
    if not fails:
        print("[ok] frame build/parse round-trips")

    # 3. corrupt CRC must be rejected (lock drops these silently)
    f = bytearray(build_frame(0x32, b"\x01\x02\x03", b"\x00"))
    f[-1] ^= 0xFF
    if parse_frame(bytes(f)) is not None:
        print("FAIL corrupt CRC not rejected"); fails += 1
    else:
        print("[ok] corrupt-CRC frames rejected")

    # 4. unlock frame golden bytes (documents expected wire format)
    f = build_frame(0x32, b"\x00\x00\x00", b"\x00")
    print(f"[info] UNLOCK frame (challenge=000000): {f.hex()}")
    # cmd=32 len=0900(9 LE) chal=000000 payload=00 crc=??
    assert f[0] == 0x32 and f[1] == 0x09 and f[2] == 0x00, "unlock header wrong"
    if f.hex() != "320900000000008c71":
        print("FAIL complete unlock golden frame"); fails += 1

    print("\nRESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILURES")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
