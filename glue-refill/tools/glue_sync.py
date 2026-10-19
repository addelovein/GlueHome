"""Reconcile one ESPHome bank through Home Assistant's authenticated REST API.

Run once or with --watch; credentials come only from the shared secure store/env.
GLUE_HA_URL (HTTPS), GLUE_HA_TOKEN, GLUE_HA_DEVICE (ESPHome node name),
GLUE_HA_BANK_ENTITY (sensor entity ID). This worker never operates the motor.
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

import glue_auth
import glue_store
from glue_keys import identity, KeyBank, validate_keys
from glue_storage import locked


def _required(name):
    value = glue_store.get(name)
    if not value:
        raise RuntimeError('Missing ' + glue_store._env_name(name))
    return value


def ha_request(path, data=None):
    base = _required('ha-url').rstrip('/')
    parsed = urlsplit(base)
    # Permit plain HTTP only on loopback or the HA Supervisor's internal hostnames
    # (homeassistant/supervisor/hassio) — container-private networking that never
    # leaves the host. Anything else must use HTTPS.
    _http_ok = ('localhost', '127.0.0.1', '::1', 'homeassistant', 'supervisor', 'hassio')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in _http_ok):
        raise ValueError('Use HTTPS for remote HA, or HTTP on an internal host')
    url = base + '/api/' + path
    req = urllib.request.Request(url,
        data=None if data is None else json.dumps(data).encode(),
        headers={'Authorization': 'Bearer ' + _required('ha-token'), 'Content-Type': 'application/json'})
    try:
        with urllib.request.build_opener(glue_auth._NoRedirect).open(req, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        # Make misconfiguration diagnosable: a 404 is usually a wrong entity id,
        # a 401 a bad/expired token. Print the request so the cause is obvious.
        print(f'HA API {exc.code} for {url} (check ha_token and ha_bank_entity)', flush=True)
        raise


def sync_once(controller=None):
    lock_id, _ = identity()
    device = _required('ha-device').replace('-', '_')
    entity = _required('ha-bank-entity')
    if not re.fullmatch(r'[a-z0-9_]+', device) or not re.fullmatch(r'sensor\.[a-z0-9_]+', entity):
        raise ValueError('Invalid HA device/service or sensor entity name')
    with locked('sync-' + lock_id):
        state = ha_request('states/' + entity)['state']
        if state in ('busy', 'unknown', 'unavailable'):
            return False
        bank = json.loads(state)
        if bank.get('lock') != lock_id:
            raise RuntimeError('ESPHome bank belongs to another lock')
        for field in ('pending', 'available'):
            if not isinstance(bank.get(field), list) or len(bank[field]) > 8 or any(type(i) is not int or not 0 <= i <= 2147483647 for i in bank[field]):
                raise RuntimeError('Invalid ESPHome bank status')
        floor = bank.get('floor', -1)
        if type(floor) is not int or not -1 <= floor <= 2147483647:
            raise RuntimeError('Invalid device retirement watermark')
        if type(bank.get('paused', False)) is not bool or (bank.get('paused', False) and bank['available']):
            raise RuntimeError('Inconsistent device pause state')
        service = 'services/esphome/' + device
        if controller == 'ble':
            if not bank.get('paused', False):
                ha_request(service + '_retire_keys', {})
                return False  # wait for sensor confirmation, not just HTTP dispatch
            cfg = glue_auth.config()
            for index in bank['pending']:
                glue_auth.report_burn(cfg, lock_id, index)
                ha_request(service + '_ack_burn', {'index': index})
            if bank['pending']:
                return False
            with locked('ble-' + lock_id):
                local = KeyBank(lock_id)
                local.retire_through(floor)
                local.sync(cfg)
                with locked(local.name):
                    glue_store.set_('controller-' + lock_id, 'ble')
            return True
        if glue_store.get('controller-' + lock_id) == 'ble' and controller != 'esphome':
            raise RuntimeError('Bank belongs to BLE; explicitly request --controller esphome to transfer it')
        if bank.get('paused', False):
            if controller != 'esphome':
                raise RuntimeError('Device paused; explicitly request --controller esphome')
            cfg = glue_auth.config()
            for index in bank['pending']:
                glue_auth.report_burn(cfg, lock_id, index)
                ha_request(service + '_ack_burn', {'index': index})
            if not bank['pending']:
                ha_request(service + '_resume_keys', {})
            return False
        # Handoff waits for any CLI session to finish. Persist ownership BEFORE
        # handing keys to ESPHome; Python BLE cannot subsequently reuse them.
        with locked('ble-' + lock_id):
            cfg = glue_auth.config()
            local = KeyBank(lock_id)
            local.sync(cfg)
            with locked(local.name):
                glue_store.set_('controller-' + lock_id, 'esphome')
        for index in bank['pending']:
            glue_auth.report_burn(cfg, lock_id, index)
            ha_request(service + '_ack_burn', {'index': index})
        keys = validate_keys(lock_id, glue_auth.api_request(cfg, 'glue-osm-devices/' + quote(lock_id, safe='') + '/keys/'))
        pushed = False
        for key in keys:
            if key['keyIndex'] in bank['pending'] or key['keyIndex'] in bank['available']:
                continue
            ha_request(service + '_add_key', {'index': key['keyIndex'], 'key': key['lockOfflineKeyStr']})
            pushed = True
        # HA returning HTTP 200 only confirms dispatch. Subsequent status polls
        # are authoritative; busy/rejected/lost calls are retried next cycle.
        return not pushed and not bank['pending'] and bool(bank['available']) if controller == 'esphome' else True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch', action='store_true', help='reconcile every 15 seconds; retry outages')
    parser.add_argument('--controller', choices=['ble', 'esphome'], help='explicitly transfer ownership; retires old keys first')
    args = parser.parse_args()
    while True:
        try:
            complete = sync_once(args.controller)
            print('Reconciled' if complete else 'Device busy/offline or transfer pending')
            if complete and args.controller:
                return
        except Exception as exc:
            if not args.watch:
                raise
            print('Sync pending (' + type(exc).__name__ + '); will retry', flush=True)
        if not args.watch:
            return
        time.sleep(15)


if __name__ == '__main__':
    main()
