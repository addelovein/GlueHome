#!/usr/bin/env python3
"""Validate/compile the example using synthetic configuration, never upload it."""
import argparse
import base64
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compile', action='store_true', help='also build the firmware (downloads toolchain)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='glue-esphome-') as directory:
        folder = Path(directory)
        shutil.copytree(root/'esphome/components',folder/'components')
        content = (root/'esphome/glue-lock.example.yaml').read_text()
        content = content.replace('!secret wifi_ssid','"glue-test-network"')
        content = content.replace('!secret wifi_password','"synthetic-wifi-only"')
        # A real (ephemeral) key: recent ESPHome rejects the reserved all-zeros key.
        api_key = base64.b64encode(secrets.token_bytes(32)).decode()
        content = content.replace('!secret api_encryption_key', '"' + api_key + '"')
        config = folder/'example.yaml'; config.write_text(content)
        subprocess.run(['esphome','config',str(config)],check=True)
        command = ['esphome','compile']
        if not args.compile:
            command.append('--only-generate')
        subprocess.run(command+[str(config)],check=True)


if __name__=='__main__':main()
