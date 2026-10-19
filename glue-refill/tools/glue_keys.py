"""Per-lock durable key bank. Reservation is irreversible, including uncertain BLE outcomes."""
import base64
import json
import uuid
from urllib.parse import quote

import glue_store
from glue_storage import locked


def identity():
    lock_id = glue_store.get('lock-id')
    serial = glue_store.get('lock-serial')
    if not lock_id or not serial:
        raise RuntimeError('No lock selected; run glue.py setup or set GLUE_LOCK_ID and GLUE_LOCK_SERIAL')
    return str(uuid.UUID(lock_id)), serial


def validate_keys(lock_id, keys):
    if not isinstance(keys, list) or len(keys) > 8:
        raise ValueError('Key response must be a bounded list (at most 8 entries)')
    seen = set()
    for k in keys:
        if k.get('lockId') != lock_id:
            raise ValueError('Key belongs to a different lock')
        idx = k.get('keyIndex')
        if type(idx) is not int or not 0 <= idx <= 2147483647 or idx in seen:
            raise ValueError('Invalid or duplicate key index')
        seen.add(idx)
        if len(base64.b64decode(k['lockOfflineKeyStr'], validate=True)) != 112:
            raise ValueError('Invalid offline key length')
    return keys


class KeyBank:
    def __init__(self, lock_id):
        self.lock_id = lock_id
        self.name = 'bank-' + lock_id

    def _read(self):
        bank = json.loads(glue_store.get(self.name, '{"keys":[],"retired_through":-1,"pending":[]}'))
        validate_keys(self.lock_id, bank['keys'])
        return bank

    def _save(self, bank):
        glue_store.set_(self.name, json.dumps(bank))

    def merge(self, keys):
        validate_keys(self.lock_id, keys)
        with locked(self.name):
            bank = self._read()
            # /keys is the authoritative pool snapshot. Keep a compact monotonic
            # tombstone so an old response can never resurrect consumed keys.
            incoming = {k['keyIndex']: k for k in keys if k['keyIndex'] > bank['retired_through']}
            bank['keys'] = sorted(incoming.values(), key=lambda k: k['keyIndex'])
            self._save(bank)
            return len(bank['keys'])

    def reserve(self):
        with locked(self.name):
            if glue_store.get('controller-' + self.lock_id) == 'esphome':
                raise RuntimeError('This bank is assigned to ESPHome; direct BLE use would reuse device keys')
            bank = self._read()
            if not bank['keys']:
                raise RuntimeError('No unused keys; run glue.py auth refill')
            key = bank['keys'].pop(0)
            idx = key['keyIndex']
            bank['retired_through'] = idx
            bank['pending'].append(idx)
            self._save(bank)  # must succeed before any LOGIN bytes are sent
            return idx, base64.b64decode(key['lockOfflineKeyStr'], validate=True)

    def retire_through(self, index):
        """Import the device's durable tombstone during a controller handoff."""
        with locked(self.name):
            bank = self._read()
            bank['retired_through'] = max(bank['retired_through'], index)
            bank['keys'] = [k for k in bank['keys'] if k['keyIndex'] > bank['retired_through']]
            self._save(bank)

    def sync(self, cfg):
        import glue_auth
        # Serialize refill workers, but permit operations to reserve while HTTP is in flight.
        with locked(self.name + '-sync'):
            with locked(self.name):
                pending = list(self._read()['pending'])
            for idx in pending:
                glue_auth.report_burn(cfg, self.lock_id, idx)
                with locked(self.name):
                    bank = self._read()
                    bank['pending'] = [i for i in bank['pending'] if i != idx]
                    self._save(bank)
            keys = glue_auth.api_request(cfg, 'glue-osm-devices/' + quote(self.lock_id, safe='') + '/keys/')
            return self.merge(keys)
